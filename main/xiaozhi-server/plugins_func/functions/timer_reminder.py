"""计时提醒 / 定时任务插件（桌宠：语音设提醒 → 屏幕倒计时 → 到点自动执行 + 播报）。

用途：让用户用一句话设个提醒或定时任务，到点了小智主动开口，例如：
``十分钟后提醒我喝水``、``下午三点提醒我开会``、``十秒后打开QQ``、``五分钟后关掉微信``、
``有哪些提醒``、``取消提醒``。

设计要点：

1. **两个入口分开**：``set_timer``（倒计时）与 ``set_reminder``（定点）。
   时长/时刻**都由本插件解析**，大模型只需把用户原话原样传进来 ——
   ``十分钟`` / ``1分30秒`` / ``半小时`` / ``一个半小时`` / ``下午三点半`` 都要能认，
   免得大模型自己换算错。
2. **提醒可以带动作**：``set_timer(duration="10秒", app_action="open", app_name="QQ")``
   到点直接开软件，``app_action="close"`` 则关软件（复用 local_app 插件的清单与启动逻辑）。
   动作在到点后放到线程池执行（启动软件要等窗口，不能堵住事件循环），
   执行结果（含"清单里没有这个软件"这类失败原因）如实播报。
3. **到点由服务端主动发起**，不需要用户再说话，走两条路：
   - 桌宠（``hello.features.timer``）收到 ``{"type":"timer","action":"fire",...}``，
     屏幕提示 + 本地倒计时刷新；
   - 文字推进 TTS 队列（正常合成语音、驱动口型），
     不支持该特性的客户端（ESP32 等）只播报，行为与从前一致。
4. **"到点动作"即使客户端断开也会执行**（用户要的是"十秒后打开QQ"，不是"如果你还在线就打开"）；
   只是断了就不再播报。
5. **提醒存续期只在内存里**（服务端重启就没了），所以对外明确说"正在计时的提醒"，
   不假装有持久化。
6. **护栏**：最多同时 5 个、单条最长 24 小时、最短 1 秒；超限直接告诉用户理由，
   不在后台静默丢弃。
7. **一条调度线程管所有提醒**（0.5 秒轮询，不是每个提醒起一条线程）；
   到期后交给该连接自己的 asyncio 事件循环执行，
   避免在别的线程里碰 WebSocket。
8. **到点事件写回对话历史、每轮注入真实清单**：模型看不到服务端内存，
   不这么做它会把"历史里设过一条"当成"现在还有一条"，用户重复设同一任务时
   会答"刚才已经设置过了"（即使上一条早就执行完了，见 PROGRESS §35）。
"""

import asyncio
import json
import os
import re
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from config.config_loader import get_project_dir
from config.logger import setup_logging
from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO
from core.utils.dialogue import Message
from plugins_func.register import Action, ActionResponse, ToolType, register_function

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

# ── 护栏 ────────────────────────────────────────────────────────────────────
DEFAULT_MAX_TIMERS = 5  # 同时存在的提醒上限
MIN_SECONDS = 1  # 最短 1 秒（"0 秒后提醒"就是 immediate，交给别的方式说）
MAX_SECONDS = 24 * 3600  # 最长 24 小时
TICK_SECONDS = 0.5  # 调度线程轮询间隔

# 持久化：服务端重启后提醒不丢（原来的设计是纯内存）。
# 自检脚本会通过环境变量把路径指到临时目录，避免覆盖正在运行的服务端状态。
TIMERS_FILE = (
    os.environ.get("XIAOZHI_TIMERS_FILE")
    or os.path.join(get_project_dir(), "data", "timers.json")
)
# 到点但设备不在线：先存下来，下次 hello 时补播
PENDING_FILE = (
    os.environ.get("XIAOZHI_PENDING_TIMERS_FILE")
    or os.path.join(get_project_dir(), "data", "pending_timers.json")
)
MAX_PENDING_PER_DEVICE = 5
MAX_PENDING_TOTAL = 50

_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

# 单位 → 秒（"个小时"必须排在"小时"前面，否则"1个小时"会被截成"1小时"后面残留"个"）
_UNIT_SECONDS: List[Tuple[str, int]] = [
    ("个小时", 3600),
    ("小时", 3600),
    ("钟头", 3600),
    ("点钟", 3600),
    ("天", 86400),
    ("时", 3600),
    ("分钟", 60),
    ("秒钟", 1),
    ("分", 60),
    ("秒", 1),
    ("d", 86400),
    ("h", 3600),
    ("min", 60),
    ("s", 1),
]
# 长单位优先（正则交替项顺序决定匹配长度），保证"分钟"不被当成"分"
_UNIT_ALT = "|".join(
    re.escape(unit) for unit in sorted({u for u, _ in _UNIT_SECONDS}, key=len, reverse=True)
)
_NUMBER_ALT = r"\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百]+"
_DURATION_RE = re.compile(rf"({_NUMBER_ALT})\s*({_UNIT_ALT})", re.IGNORECASE)

# 时段词 → 12 小时制的基准（凌晨/早上/上午都是 0，下午/晚上是 12）
_PERIODS: List[Tuple[str, int]] = [
    ("凌晨", 0),
    ("早上", 0),
    ("上午", 0),
    ("中午", 12),
    ("下午", 12),
    ("傍晚", 12),
    ("晚上", 12),
    ("夜里", 12),
    ("深夜", 12),
]
# "晚上12点"指的是午夜 0 点（次日），"中午12点"才是正午
_MIDNIGHT_WORDS = ("晚上", "夜里", "深夜")
_DAY_OFFSETS = (("大后天", 3), ("后天", 2), ("明天", 1), ("明早", 1), ("今天", 0))

# 大模型有时会把整句塞进 label（"提醒我喝水""记得交表"），前缀去掉，
# 免得播报时变成"时间到，提醒我喝水"
_LABEL_PREFIXES = (
    "提醒我一下",
    "提醒一下我",
    "提醒我",
    "提醒",
    "提示我",
    "告诉我",
    "叫我",
    "喊我",
    "记得",
    "别忘了",
    "别忘记",
    "到时候",
)
_LABEL_TAIL_RE = re.compile(r"[，。！？、；：,.!?;:~～\s]+$")


# ── 文本工具 ────────────────────────────────────────────────────────────────
def _normalize(text: str) -> str:
    """归一化：NFKC（全角数字/冒号转半角）+ 去空白 + 小写。"""
    value = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    return "".join(value.split())


def _to_int(text: str) -> Optional[int]:
    """阿拉伯数字或中文数字（支持到"一百二十"）→ int。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    # 注意字符集判定要带上"十/百"：否则"十点""一百二十秒"会被当成非法输入直接丢掉
    # （踩过：判定只认数字字符，结果"十分钟"解析不出任何时长）
    if not all(ch in _CN_DIGITS or ch in "十百" for ch in raw):
        return None
    total = 0
    if "百" in raw:
        head, raw = raw.split("百", 1)
        total += (_CN_DIGITS.get(head, 1) if head else 1) * 100
    if "十" in raw:
        tens, ones = raw.split("十", 1)
        total += (_CN_DIGITS.get(tens, 1) if tens else 1) * 10
        total += _CN_DIGITS.get(ones, 0) if ones else 0
    elif raw:
        total += _CN_DIGITS.get(raw, 0)
    return total


def _unit_seconds(unit: str) -> Optional[int]:
    lowered = str(unit or "").lower()
    for name, seconds in _UNIT_SECONDS:
        if name == lowered:
            return seconds
    return None


def _human_seconds(seconds: float) -> str:
    """秒 → 口语化时长（"1小时20分""10分钟""25秒"）。"""
    total = int(max(0, round(seconds)))
    if total < 60:
        return f"{total}秒"
    if total < 3600:
        minutes, rest = divmod(total, 60)
        return f"{minutes}分{rest}秒" if rest else f"{minutes}分钟"
    hours, rest = divmod(total, 3600)
    if not rest:
        return f"{hours}小时"
    minutes = rest // 60
    secs = rest % 60
    if secs:
        return f"{hours}小时{minutes}分{secs}秒"
    return f"{hours}小时{minutes}分"


def _cn_clock(moment: datetime) -> str:
    """时刻 → 口语（"下午3点30分""上午9点"），用 12 小时制，TTS 念起来自然。"""
    hour = moment.hour
    minute = moment.minute
    if hour == 0:
        period, shown = "凌晨", 0
    elif hour < 5:
        period, shown = "凌晨", hour
    elif hour < 12:
        period, shown = "上午", hour
    elif hour == 12:
        period, shown = "中午", 12
    elif hour < 18:
        period, shown = "下午", hour - 12
    else:
        period, shown = "晚上", hour - 12
    text = f"{period}{shown}点"
    if minute:
        text += f"{minute}分"
    return text


def _clean_label(text: str) -> str:
    raw = str(text or "").strip()
    for prefix in _LABEL_PREFIXES:
        if raw.startswith(prefix):
            raw = raw[len(prefix) :].strip()
            break
    raw = _LABEL_TAIL_RE.sub("", raw)
    # 极端情况：整句被塞进 label（"10分钟后提醒我喝水"），把开头的时间说法切掉
    raw = re.sub(rf"^({_NUMBER_ALT})\s*({_UNIT_ALT})(之?后|以?后|后)", "", raw).strip()
    return raw


# ── 到点动作（定时开/关本机软件）────────────────────────────────────────────
# 动作返回值约定：""=没有动作（纯提醒）；"open"/"close"=已识别的动作；
# "?"=大模型传了东西但认不出是什么操作（调用方要给用户一句提示）
_ACTION_WORDS = (
    ("open", ("打开", "启动", "开一下", "打开一下", "open", "launch", "start", "run")),
    ("close", ("关闭", "关掉", "退出", "关一下", "关闭掉", "close", "quit", "exit", "kill")),
)


def parse_app_action(text: str) -> str:
    """把大模型传来的动作说法规范成 open/close（认不出返回 "?"，空表示纯提醒）。"""
    raw = _normalize(text)
    if not raw or raw in ("none", "remind", "none_action", "无", "提醒", "null"):
        return ""
    for action, words in _ACTION_WORDS:
        if any(word == raw or word in raw for word in words):
            return action
    return "?"


def action_verb(action: str) -> str:
    """动作的中文说法（用于播报/自动生成提醒内容）。"""
    return "打开" if action != "close" else "关闭"


# ── 时长 / 时刻解析 ──────────────────────────────────────────────────────────
def parse_duration(text: str) -> Optional[int]:
    """``10分钟`` / ``1分30秒`` / ``半小时`` / ``一个半小时`` → 秒；认不出返回 None。"""
    raw = _normalize(text)
    if not raw:
        return None

    # 1) "一个半小时" / "两个半小时" = n 小时 + 30 分（必须先于"半小时"判断，
    #    否则"一个半小时"会被当成 30 分钟）
    match = re.search(rf"({_NUMBER_ALT})\s*个半\s*(小时|钟头)", raw)
    if match:
        count = _to_int(match.group(1))
        if count:
            return int(count * 3600 + 1800)
    # 2) "半小时" / "一刻钟"
    if re.search(r"半\s*个?\s*(小时|钟头)", raw):
        return 1800
    if "一刻钟" in raw:
        return 900

    total = 0.0
    found = False
    # 3) "1分半" = 1分30秒、"2小时半" = 2小时30分（口语里"...半"跟在单位后面）
    half = re.search(rf"({_NUMBER_ALT})\s*(分|小时|钟头)\s*半", raw)
    if half:
        base = _to_int(half.group(1)) or 0
        if half.group(2) == "分":
            total += base * 60 + 30
        else:
            total += base * 3600 + 1800
        found = True
        # 切掉这段，免得通用匹配再把它算一遍（1分半会被算成 60+90）
        raw = raw[: half.start()] + raw[half.end() :]
    for match in _DURATION_RE.finditer(raw):
        value = _to_int(match.group(1))
        seconds = _unit_seconds(match.group(2))
        if value is None or seconds is None:
            continue
        total += value * seconds
        found = True
    if not found:
        return None
    return int(total)


def parse_clock(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """``下午三点`` / ``三点半`` / ``15:30`` / ``明天早上7点`` → datetime；认不出返回 None。"""
    raw = _normalize(text)
    if not raw:
        return None
    now = now or datetime.now()

    day_offset = 0
    for word, offset in _DAY_OFFSETS:
        if word in raw:
            day_offset = offset
            break

    period = None
    for word, base in _PERIODS:
        if word in raw:
            period = base
            break

    # 15:30 形式
    match = re.search(r"(\d{1,2})\s*[:：]\s*(\d{1,2})", raw)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
    else:
        match = re.search(rf"({_NUMBER_ALT})\s*[点时時]", raw)
        if not match:
            return None
        hour = _to_int(match.group(1))
        if hour is None:
            return None
        minute = 0
        tail = raw[match.end() :].strip()
        if tail.startswith("半"):
            minute = 30
        else:
            # "8点30"（口语里常省略"分"）与"三点十五分"都要认
            minute_match = re.match(rf"({_NUMBER_ALT})\s*分?", tail)
            if minute_match:
                minute = _to_int(minute_match.group(1)) or 0

    if hour > 23 or minute > 59:
        return None

    if period is not None:
        # 只有"晚上/夜里/深夜12点"才是午夜 0 点（且算次日）；
        # "中午12点""下午12点"就是正午 12:00（踩过：一律按午夜处理会把中午提醒推到明天）
        if (
            period == 12
            and hour == 12
            and any(word in raw for word in _MIDNIGHT_WORDS)
        ):
            hour = 0
            day_offset += 1
        elif hour < 12:
            hour = hour + period
    elif 0 < hour <= 12:
        # 没说时段：先按 12 小时制理解；这个点今天已经过了，就看下午那一次
        candidate = (now + timedelta(days=day_offset)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if candidate <= now:
            afternoon = candidate + timedelta(hours=12)
            if afternoon > now and (hour % 12) + 12 <= 23:
                hour = (hour % 12) + 12

    target = (now + timedelta(days=day_offset)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    while target <= now:
        target += timedelta(days=1)
    return target


# ── 提醒存储 ────────────────────────────────────────────────────────────────
class _Timer:
    __slots__ = (
        "id",
        "label",
        "kind",
        "seconds",
        "created_at",
        "due_at",
        "conn",
        "action",
        "app",
        "device_id",
        "server",
        "config",
    )

    def __init__(
        self,
        timer_id: str,
        label: str,
        kind: str,
        seconds: int,
        created_at: float,
        due_at: float,
        conn: "ConnectionHandler" = None,
        action: str = "",
        app: str = "",
        device_id: str = "",
        server: Any = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        self.id = timer_id
        self.label = label
        self.kind = kind  # countdown / clock
        self.seconds = seconds
        self.created_at = created_at
        self.due_at = due_at
        self.conn = conn
        self.action = action  # "" 纯提醒 / open / close（定时任务）
        self.app = app  # 要操作的软件名（动作非空时有值）
        self.device_id = str(
            device_id
            or getattr(conn, "device_id", "")
            or getattr(conn, "session_id", "")
            or ""
        )
        self.server = server if server is not None else getattr(conn, "server", None)
        # 定时任务到点时即使设备离线也要执行，所以配置要跟着 timer 走
        self.config = (
            config
            if config is not None
            else (getattr(conn, "config", None) or {})
        )


_TIMERS: Dict[str, _Timer] = {}
_LOCK = threading.RLock()
_SCHEDULER: Optional[threading.Thread] = None
_PENDING_LOCK = threading.RLock()


# ── 持久化 ──────────────────────────────────────────────────────────────────
def _atomic_write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.{os.getpid()}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def _persist_timers() -> None:
    """把未到点的提醒落盘（服务端重启后仍能恢复）。"""
    with _LOCK:
        items = [
            {
                "id": timer.id,
                "label": timer.label,
                "kind": timer.kind,
                "seconds": timer.seconds,
                "created_at": timer.created_at,
                "due_at": timer.due_at,
                "action": timer.action,
                "app": timer.app,
                "device_id": timer.device_id,
            }
            for timer in _TIMERS.values()
        ]
    try:
        _atomic_write_json(TIMERS_FILE, {"timers": items})
    except OSError as error:
        logger.bind(tag=TAG).warning(f"提醒落盘失败 | {type(error).__name__}: {error}")


def _read_pending() -> List[Dict[str, Any]]:
    if not os.path.isfile(PENDING_FILE):
        return []
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as error:
        logger.bind(tag=TAG).warning(
            f"读取离线提醒失败，按空处理 | {type(error).__name__}: {error}"
        )
        return []
    items = payload.get("pending") if isinstance(payload, dict) else None
    return [item for item in (items or []) if isinstance(item, dict)]


def _write_pending(items: List[Dict[str, Any]]) -> None:
    try:
        _atomic_write_json(PENDING_FILE, {"pending": items})
    except OSError as error:
        logger.bind(tag=TAG).warning(f"离线提醒落盘失败 | {type(error).__name__}: {error}")


def _queue_pending(timer: _Timer) -> None:
    """到点但设备不在线：记下来，等它下次上线补播。"""
    text = _fire_text(timer)
    if timer.action:
        text += f"（当时设备不在线，已尝试{action_verb(timer.action)}“{timer.app}”）"
    entry = {
        "id": timer.id,
        "device_id": timer.device_id,
        "text": text,
        "label": timer.label,
        "due_at": timer.due_at,
        "created_at": time.time(),
    }
    with _PENDING_LOCK:
        items = [item for item in _read_pending() if str(item.get("id")) != timer.id]
        items.append(entry)

        same = [
            item for item in items if str(item.get("device_id")) == timer.device_id
        ]
        if len(same) > MAX_PENDING_PER_DEVICE:
            drop = {
                str(item.get("id"))
                for item in sorted(same, key=lambda item: item.get("due_at", 0))[
                    : len(same) - MAX_PENDING_PER_DEVICE
                ]
            }
            items = [item for item in items if str(item.get("id")) not in drop]
        if len(items) > MAX_PENDING_TOTAL:
            items = sorted(items, key=lambda item: item.get("due_at", 0))[
                -MAX_PENDING_TOTAL:
            ]
        _write_pending(items)
    logger.bind(tag=TAG).info(
        f"提醒到点但设备不在线，已加入补播队列 | id={timer.id} | "
        f"device={timer.device_id or '-'} | text={text!r}"
    )


def load_persisted_timers(
    config: Optional[Dict[str, Any]] = None, server: Any = None
) -> int:
    """从 data/timers.json 恢复未到点的提醒（服务端启动时调用一次）。"""
    if not os.path.isfile(TIMERS_FILE):
        return 0
    try:
        with open(TIMERS_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as error:
        logger.bind(tag=TAG).warning(
            f"读取已保存提醒失败，跳过恢复 | {type(error).__name__}: {error}"
        )
        return 0

    records = payload.get("timers") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return 0

    now = time.time()
    restored = 0
    expired = 0
    with _LOCK:
        for record in records:
            if not isinstance(record, dict):
                continue
            try:
                timer_id = str(record["id"])
                due_at = float(record["due_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if timer_id in _TIMERS:
                continue
            # 超过 24 小时的历史提醒不再补，避免一启动就冒出一堆旧提醒
            if due_at < now - 24 * 3600:
                expired += 1
                continue
            _TIMERS[timer_id] = _Timer(
                timer_id=timer_id,
                label=str(record.get("label") or ""),
                kind=str(record.get("kind") or "countdown"),
                seconds=int(record.get("seconds") or 0),
                created_at=float(record.get("created_at") or now),
                due_at=due_at,
                conn=None,
                action=str(record.get("action") or ""),
                app=str(record.get("app") or ""),
                device_id=str(record.get("device_id") or ""),
                server=server,
                config=config or {},
            )
            restored += 1
    if restored:
        _ensure_scheduler()
    logger.bind(tag=TAG).info(
        f"恢复已保存提醒 | restored={restored} | expired={expired}"
    )
    return restored


def _ensure_scheduler() -> None:
    """保证调度线程在跑（惰性启动，没有提醒时线程自然空转，开销可忽略）。"""
    global _SCHEDULER
    with _LOCK:
        thread = _SCHEDULER
        if thread is not None and thread.is_alive():
            return
        _SCHEDULER = threading.Thread(
            target=_scheduler_loop, name="timer-reminder", daemon=True
        )
        _SCHEDULER.start()


def _scheduler_loop() -> None:
    while True:
        due: List[_Timer] = []
        try:
            with _LOCK:
                now = time.time()
                for timer in list(_TIMERS.values()):
                    if timer.due_at <= now:
                        due.append(timer)
                        del _TIMERS[timer.id]
            if due:
                _persist_timers()
            for timer in due:
                _schedule_fire(timer)
        except Exception as error:  # 调度线程绝不能因为一条提醒挂掉
            logger.bind(tag=TAG).error(
                f"计时调度异常 | {type(error).__name__}: {error}"
            )
        time.sleep(TICK_SECONDS)


def _conn_alive(conn) -> bool:
    """连接对象是否仍然可用于推送（websocket 已关闭 / stop_event 已置位都不算）。"""
    if conn is None:
        return False
    if getattr(conn, "websocket", None) is None:
        return False
    stop_event = getattr(conn, "stop_event", None)
    if stop_event is not None and stop_event.is_set():
        return False
    state = getattr(getattr(conn, "websocket", None), "state", None)
    if state is not None and getattr(state, "name", "") == "CLOSED":
        return False
    if getattr(getattr(conn, "websocket", None), "closed", None) is True:
        return False
    return True


def _find_live_conn(timer: _Timer):
    """按 device_id 找当前在线连接（session_id 每次连接都变，不能当键）。"""
    server = timer.server
    table = getattr(server, "connections_by_device", None)
    if isinstance(table, dict):
        # 走真实服务端：注册表里没有就是真的离线，绝不退回旧连接
        # （旧连接的 websocket 对象还在，但早已关闭，退回去会把提醒静默吞掉）
        conn = table.get(timer.device_id) if timer.device_id else None
        return conn if _conn_alive(conn) else None

    # 兼容自检/旧数据：没有服务端注册表时才退回创建定时器时的连接
    conn = timer.conn
    return conn if _conn_alive(conn) else None


class _TimerContext:
    """给"设备离线时执行定时任务"用的最小上下文（local_app 只用到这两个字段）。"""

    def __init__(self, config: Optional[Dict[str, Any]]):
        self.config = config or {}
        self.logger = logger


def _offline_fire(timer: _Timer) -> None:
    """设备不在线：动作照做，播报留给下次上线补。"""
    log = logger.bind(tag=TAG)
    if timer.action:
        try:
            result = _run_app_action(_TimerContext(timer.config), timer.action, timer.app)
            detail = (result.response or result.result or "").strip() if result else ""
            log.info(
                f"离线定时任务已执行 | id={timer.id} | "
                f"{action_verb(timer.action)}{timer.app} | 结果={detail!r}"
            )
        except Exception as error:
            log.error(
                f"离线定时任务执行失败 | id={timer.id} | "
                f"{action_verb(timer.action)}{timer.app} | "
                f"{type(error).__name__}: {error}"
            )
    _queue_pending(timer)


def _schedule_fire(timer: _Timer) -> None:
    conn = _find_live_conn(timer)
    loop = getattr(conn, "loop", None) if conn is not None else None
    loop_alive = loop is not None and not getattr(loop, "is_closed", lambda: False)()
    if conn is None or not loop_alive:
        logger.bind(tag=TAG).warning(
            f"提醒到点但设备不在线，转离线处理 | id={timer.id} | "
            f"device={timer.device_id or '-'}"
        )
        threading.Thread(
            target=_offline_fire, args=(timer,), name="timer-offline", daemon=True
        ).start()
        return
    try:
        future = asyncio.run_coroutine_threadsafe(_fire(conn, timer), loop)
        future.add_done_callback(_fire_done)
    except RuntimeError as error:
        logger.bind(tag=TAG).warning(
            f"提醒到点但连接的事件循环不可用，转离线处理 | id={timer.id} | {error}"
        )
        threading.Thread(
            target=_offline_fire, args=(timer,), name="timer-offline", daemon=True
        ).start()


async def deliver_pending_timers(conn: "ConnectionHandler") -> int:
    """设备上线后补播"到点时它不在线"的提醒（由 hello 处理调用）。"""
    device_id = str(
        getattr(conn, "device_id", "") or getattr(conn, "session_id", "") or ""
    )
    if not device_id:
        return 0

    # 等 TTS 初始化（最多 3 秒），与 checkWakeupWords 同一套写法
    for _ in range(30):
        if getattr(conn, "tts", None):
            break
        await asyncio.sleep(0.1)
    else:
        logger.bind(tag=TAG).warning(
            f"上线补播时 TTS 还没就绪，保留待下次 | device={device_id}"
        )
        return 0

    with _PENDING_LOCK:
        items = _read_pending()
        mine = [
            item for item in items if str(item.get("device_id")) == device_id
        ]
        if mine:
            _write_pending(
                [item for item in items if str(item.get("device_id")) != device_id]
            )
    if not mine:
        return 0

    mine.sort(key=lambda item: item.get("due_at", 0))
    parts: List[str] = []
    for item in mine[:3]:
        text = str(item.get("text") or "").strip()
        if text:
            parts.append(text)
    if len(mine) > 3:
        parts.append(f"等共 {len(mine)} 条")
    text = "你有已经到点的提醒：" + "；".join(parts)

    _speak(conn, text)
    _remember_spoken(conn, text)
    await push_timer_list(conn)
    logger.bind(tag=TAG).info(
        f"上线补播到点提醒 | device={device_id} | count={len(mine)} | text={text!r}"
    )
    return len(mine)


def _fire_done(future) -> None:
    """到点任务若抛异常，必须自己捞出来落日志。

    ``run_coroutine_threadsafe`` 返回的 Future 不取结果就把异常吞了，
    表现是"到点了却什么也没发生，日志里也查不到任何线索"（实测踩过）。
    """
    try:
        future.result()
    except Exception as error:
        logger.bind(tag=TAG).error(
            f"到点任务执行失败 | {type(error).__name__}: {error}"
        )


# ── 到点执行 / 播报 ─────────────────────────────────────────────────────────
def _fire_text(timer: _Timer) -> str:
    label = (timer.label or "").strip()
    if not label and timer.action:
        label = f"{action_verb(timer.action)}{timer.app}"
    return f"时间到，{label}" if label else "时间到啦"


def _run_app_action(
    conn: "ConnectionHandler", action: str, app_name: str
) -> ActionResponse:
    """到点执行的本地软件动作（同步；调用方负责扔进线程里跑）。

    单独抽成一层是为了让自检能替换掉它 —— 自检不能真的去开关用户的软件。
    真正的实现直接复用 local_app 插件的"解析软件名 + 启动/关闭"逻辑，
    这样定时任务与语音直接说"打开QQ"走的是同一套清单与同一条代码路径。
    """
    from plugins_func.functions.local_app import (
        _close_local_app_sync,
        _launch_local_app_sync,
    )

    if action == "close":
        return _close_local_app_sync(conn, app_name)
    return _launch_local_app_sync(conn, app_name, "")


def _remember_spoken(conn: "ConnectionHandler", text: str) -> None:
    """把到点主动说出的话写进对话历史。

    不写的话，模型对"这条提醒还在不在"的认知永远停在 ``set_timer`` 那一刻：
    到点播报它不知道，用户用同样的话再设一次，它就答"刚才已经设置过了"
    （或谎称"已经设好了"而根本不调用工具）。历史里补上这条，
    模型才能看出"上一条已经执行完"，把再一次请求当成新任务。
    """
    dialogue = getattr(conn, "dialogue", None)
    if dialogue is None or not str(text or "").strip():
        return
    try:
        dialogue.put(Message(role="assistant", content=text))
    except Exception as error:  # 写历史失败不能影响提醒本身
        logger.bind(tag=TAG).warning(
            f"到点播报写入对话历史失败 | {type(error).__name__}: {error}"
        )


def prompt_state() -> str:
    """给大模型的"当前提醒"事实（每轮注入 system），避免它拿历史当现状。

    模型看不到服务端内存里的清单：到点前它只知道"我设过一条"，到点后也不知道
    "那条已经结束"。把真实清单摆出来，"还有没有提醒"就不需要它猜。
    """
    with _LOCK:
        timers = sorted(_TIMERS.values(), key=lambda item: item.due_at)
    if not timers:
        return (
            "<current_timers>当前没有正在计时的提醒"
            "（到点的提醒执行完就已经结束，不会留在清单里）。</current_timers>"
        )
    return (
        f"<current_timers>正在计时的提醒 {len(timers)} 条：{_list_text(timers)}。"
        "清单里没有的就是已经结束的提醒；用户再提同样的任务要按新请求重新设置。"
        "</current_timers>"
    )


def _speak(
    conn: "ConnectionHandler", text: str, sentence_id: Optional[str] = None
) -> None:
    """把提醒文本推进 TTS 队列（会正常合成语音、驱动口型）。

    与音乐插件同一条路：不推 FILE，所以要自己补一个 LAST 标记，
    否则文本会滞留在缓冲里不落定。

    ``sentence_id`` 默认新开一句（占住 ``conn.sentence_id``）。传具体值时表示
    "跟在那一句后面说" —— 注意别在**同一次到点**里连着发两句：要么共用 id
    （后一句的 sentence_start 会被音频流控排到队尾，客户端收不到文本），
    要么各自换 id（前一句被 TTS 线程按旧消息丢掉），两条路都会丢一句。
    """
    if sentence_id is None:
        conn.sentence_id = uuid.uuid4().hex
        sentence_id = conn.sentence_id
    # 上一轮用户打断过的话 client_abort 会一直是 True，这里必须复位，
    # 否则 TTS 线程会把这条提醒直接丢掉（到点却不说话）
    conn.client_abort = False
    conn.tts.store_tts_text(sentence_id, text)
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=sentence_id,
            sentence_type=SentenceType.FIRST,
            content_type=ContentType.ACTION,
        )
    )
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=sentence_id,
            sentence_type=SentenceType.MIDDLE,
            content_type=ContentType.TEXT,
            content_detail=text,
        )
    )
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=sentence_id,
            sentence_type=SentenceType.LAST,
            content_type=ContentType.ACTION,
        )
    )


async def _send(conn: "ConnectionHandler", payload: Dict[str, Any]) -> bool:
    websocket = getattr(conn, "websocket", None)
    if not websocket:
        return False
    try:
        await websocket.send(json.dumps(payload, ensure_ascii=False))
        return True
    except Exception as error:
        logger.bind(tag=TAG).warning(
            f"下发计时消息失败 | {type(error).__name__}: {error}"
        )
        return False


def timer_supported(conn: "ConnectionHandler") -> bool:
    """客户端是否在 hello.features 里声明了 timer（桌宠声明，ESP32 没有）。"""
    return bool((getattr(conn, "features", None) or {}).get("timer"))


async def push_timer_list(conn: "ConnectionHandler") -> None:
    """把当前提醒清单下发给桌宠（面板按到期时刻本地倒计时，不用服务端持续推送）。

    时间戳统一用**毫秒**（前端 Date.now() 同一单位），与 HTTP 接口是同一份数据；
    秒/毫秒混用会让面板直接显示成 1970 年。
    """
    if not timer_supported(conn):
        return
    await _send(
        conn, {"type": "timer", "action": "list", "timers": public_timers_ms()}
    )


async def _fire(conn: "ConnectionHandler", timer: _Timer) -> None:
    log = conn.logger.bind(tag=TAG)
    text = _fire_text(timer)
    # 先落一条"开始处理"：万一后面某步抛异常（异常由 _fire_done 兜住），
    # 日志里也能看出"到点这件事发生过"，不至于全无线索
    log.info(
        f"提醒到点，开始处理 | id={timer.id} | label={timer.label!r} | "
        f"动作={timer.action or '无'}{timer.app}"
    )
    panel_sent = True
    if timer_supported(conn):
        panel_sent = await _send(
            conn,
            {
                "type": "timer",
                "action": "fire",
                "id": timer.id,
                "label": timer.label,
                "text": text,
                "app_action": timer.action,
                "app": timer.app,
            },
        )
    # 客户端已经断开就别往队列里塞了（TTS provider 可能已关闭）。
    # 但**定时任务的动作照做** —— 用户要的是"十秒后打开QQ"，不是"如果你还在线就打开"。
    stop_event = getattr(conn, "stop_event", None)
    # 面板消息发不出去，通常说明连接刚断（调度线程与断线之间存在竞态）
    disconnected = (stop_event is not None and stop_event.is_set()) or not panel_sent

    if not timer.action:
        if disconnected:
            log.info(f"提醒到点但连接已断开，转入补播队列 | id={timer.id}")
            _queue_pending(timer)
            return
        _speak(conn, text)
        _remember_spoken(conn, text)
        await push_timer_list(conn)
        log.info(f"提醒到点完成 | id={timer.id} | 播报={text!r}")
        return

    # 定时任务：先执行（启动软件要等窗口，通常 0.5~3 秒），拿到结果后**一次**播报。
    # 不做"先说一句、再说结果"的两段式：两句播报要么共用 sentence_id
    # （后一句的 sentence_start 会被音频流控排在队尾，客户端收不到文本），
    # 要么各自换 id（前一句会被 TTS 线程按 sentence_id 当旧消息丢掉）——
    # 实测两种都会丢一句。屏幕上的即时反馈交给 fire 消息（面板高亮 + toast）。
    verb = action_verb(timer.action)
    try:
        # 启动软件要等窗口（可能几秒），必须放到线程里，不能堵住事件循环
        result = await asyncio.to_thread(
            _run_app_action, conn, timer.action, timer.app
        )
        result_text = ""
        if result is not None:
            result_text = (result.response or result.result or "").strip()
    except Exception as error:
        log.error(
            f"定时任务执行异常 | id={timer.id} | {verb}{timer.app} | "
            f"{type(error).__name__}: {error}"
        )
        result_text = f"{verb}“{timer.app}”时出错了，{error}。"
    if not result_text:
        result_text = f"已经帮你{verb}“{timer.app}”了。"
    log.info(
        f"定时任务到点 | id={timer.id} | {verb}{timer.app} | 结果={result_text!r}"
    )
    if disconnected:
        log.info(f"提醒到点但连接已断开，动作已执行，播报转入补播队列 | id={timer.id}")
        _queue_pending(timer)
        return
    fire_text = f"时间到，{result_text}"
    _speak(conn, fire_text)
    _remember_spoken(conn, fire_text)
    await push_timer_list(conn)


# ── 对外数据（桌宠 HTTP 接口 / 工具共用）───────────────────────────────────
def public_timers() -> List[Dict[str, Any]]:
    """当前提醒清单（按到期时间升序），不含连接对象，可直接序列化。"""
    with _LOCK:
        timers = sorted(_TIMERS.values(), key=lambda item: item.due_at)
    return [
        {
            "id": timer.id,
            "label": timer.label,
            "kind": timer.kind,
            "seconds": timer.seconds,
            "created_at": timer.created_at,
            "due_at": timer.due_at,
            "app_action": timer.action,
            "app": timer.app,
        }
        for timer in timers
    ]


def public_timers_ms() -> List[Dict[str, Any]]:
    """给桌宠的清单：时间戳换成毫秒（前端 Date.now() 同一单位）。"""
    return [
        {
            "id": timer["id"],
            "label": timer["label"],
            "kind": timer["kind"],
            "due_at": int(round(timer["due_at"] * 1000)),
            "seconds": timer["seconds"],
            "app_action": timer["app_action"],
            "app": timer["app"],
        }
        for timer in public_timers()
    ]


def cancel(ref: str = "", all_: bool = False) -> Tuple[List[_Timer], Optional[str]]:
    """取消提醒。ref 支持序号（"第一个"/"2"）或提醒内容关键词；为空表示全部。"""
    with _LOCK:
        timers = sorted(_TIMERS.values(), key=lambda item: item.due_at)
        if not timers:
            return [], "现在没有正在计时的提醒。"

        raw = _normalize(ref)
        if all_ or not raw or "全部" in raw or "所有" in raw:
            removed = timers
            _drop_timers(removed)
            return removed, None

        # 1) 序号："第一个" / "第2个" / "3"
        ordinal = _to_int(re.sub(r"^第|\s*个?$", "", raw)) if raw else None
        if ordinal is not None:
            if not 1 <= ordinal <= len(timers):
                return [], f"只有 {len(timers)} 个提醒，没有第 {ordinal} 个。"
            target = timers[ordinal - 1]
            _drop_timers([target])
            return [target], None

        # 2) 内容关键词
        matched = [timer for timer in timers if raw and raw in _normalize(timer.label)]
        if not matched:
            return (
                [],
                f"正在计时的提醒里没有“{str(ref or '').strip()}”，"
                f"现在有：{_list_text(timers)}",
            )
        _drop_timers(matched)
        return matched, None


def _drop_timers(timers: List[_Timer]) -> None:
    """删除提醒并落盘（调用方持有 _LOCK 或在其内部调用；RLock 可重入）。"""
    for timer in timers:
        _TIMERS.pop(timer.id, None)
    _persist_timers()


def cancel_id(timer_id: str) -> Optional[_Timer]:
    """按 id 精确取消（桌宠面板点「×」走 HTTP 接口，直接给 id）。"""
    with _LOCK:
        removed = _TIMERS.pop(timer_id, None)
    if removed is not None:
        _persist_timers()
    return removed


def _list_text(timers: List[_Timer]) -> str:
    parts = []
    for timer in timers:
        moment = datetime.fromtimestamp(timer.due_at)
        when = (
            _human_seconds(max(0, timer.due_at - time.time()))
            if timer.kind == "countdown"
            else _cn_clock(moment)
        )
        parts.append(f"{when}后{timer.label}" if timer.label else f"{when}后")
    return "；".join(parts)


def _created_text(timer: _Timer) -> str:
    label = (timer.label or "").strip()
    when = (
        _cn_clock(datetime.fromtimestamp(timer.due_at))
        if timer.kind == "clock"
        else f"{_human_seconds(timer.seconds)}后"
    )
    if timer.action:
        # 定时任务：说清楚到点会做什么，而不是"提醒你打开QQ"
        return f"好的，{when}帮你{action_verb(timer.action)}“{timer.app}”。"
    head = f"好的，{when}提醒你"
    return f"{head}{label}。" if label else f"{head}。"


def _cancelled_text(removed: List[_Timer]) -> str:
    if len(removed) == 1:
        label = (removed[0].label or "").strip()
        return f"已经取消{label}提醒。" if label else "已经取消那个提醒。"
    return f"已经取消了 {len(removed)} 个提醒。"


# ── 配置 ────────────────────────────────────────────────────────────────────
def _plugin_config(conn: "ConnectionHandler") -> Dict[str, Any]:
    plugins = conn.config.get("plugins") or {}
    return plugins.get("timer_reminder") or {}


def _int_config(payload: Dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(payload.get(key, default))
    except (TypeError, ValueError):
        return default
    return min(max(value, low), high)


def _create(
    conn: "ConnectionHandler",
    kind: str,
    seconds: int,
    due_at: float,
    label: str,
    action: str = "",
    app: str = "",
) -> _Timer:
    created_at = time.time()
    timer = _Timer(
        timer_id=uuid.uuid4().hex[:8],
        label=label,
        kind=kind,
        seconds=seconds,
        created_at=created_at,
        due_at=due_at,
        conn=conn,
        action=action,
        app=app,
        device_id=str(
            getattr(conn, "device_id", "") or getattr(conn, "session_id", "") or ""
        ),
        server=getattr(conn, "server", None),
        config=getattr(conn, "config", None) or {},
    )
    with _LOCK:
        _TIMERS[timer.id] = timer
    _persist_timers()
    _ensure_scheduler()
    return timer


# ── 工具描述 ────────────────────────────────────────────────────────────────
SET_TIMER_DESC = {
    "type": "function",
    "function": {
        "name": "set_timer",
        "description": (
            "【定时提醒 / 定时任务】设一个倒计时：到点后小智主动开口提醒，"
            "或者到点**自动打开／关闭某个本机软件**。"
            "**这类请求必须调用本工具**：只回一句“好的，X 分钟后提醒你”“好的，到时候帮你打开”"
            "等于什么都没做（提醒不会响、软件也不会打开），也不要用 direct_answer 复述一遍。"
            "当用户说“十分钟后提醒我喝水”“5分钟后提醒我关火”“半分钟后提醒我”“计时 3 分钟”"
            "“十秒后打开QQ”“五分钟后关掉微信”“8秒后打开记事本”这类“多久之后”的请求时调用。"
            "时长原样传进来（如“10分钟”“1分30秒”“半小时”），本工具自己会换算，不要自己算成秒。"
            "**打开/关闭软件不要塞进 label**，要填 app_action 与 app_name"
            "（例：“十秒后打开QQ” → duration=“10秒”、app_action=“open”、app_name=“QQ”、label 留空；"
            "“五分钟后关掉微信” → duration=“5分钟”、app_action=“close”、app_name=“微信”）。"
            "用户说的是具体时刻（“三点提醒我开会”）时用 set_reminder，不要用本工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "duration": {
                    "type": "string",
                    "description": (
                        "要等多久，按用户说的原样填，如“10分钟”“1分30秒”“半小时”"
                        "“一个半小时”“90秒”“3h”。"
                    ),
                },
                "label": {
                    "type": "string",
                    "description": (
                        "提醒内容，即到点时要提醒用户做什么，如“喝水”“关火”“休息一下”。"
                        "用户没说做什么时留空；要操作软件时也留空（会自动说成“打开QQ”）。"
                    ),
                },
                "app_action": {
                    "type": "string",
                    "description": (
                        "到点要执行的本机软件操作：open 表示打开（启动）软件，"
                        "close 表示关闭软件。纯提醒（“提醒我喝水”）留空不填。"
                    ),
                },
                "app_name": {
                    "type": "string",
                    "description": (
                        "要操作的软件名，按用户说的填，如“QQ”“微信”“记事本”。"
                        "只有 app_action 有值时才需要填。"
                    ),
                },
            },
            "required": ["duration"],
        },
    },
}

SET_REMINDER_DESC = {
    "type": "function",
    "function": {
        "name": "set_reminder",
        "description": (
            "【定点提醒 / 定点任务】到点小智主动开口提醒，或自动打开／关闭某个本机软件。"
            "**这类请求必须调用本工具**：口头答应或用 direct_answer 复述一遍都等于没设上。"
            "当用户说“下午三点提醒我开会”“明天早上7点叫我起床”“晚上八点半提醒我看直播”"
            "“明天早上八点打开QQ”这类指定时刻的请求时调用。"
            "时刻原样传进来（如“下午三点”“15:30”“明天早上7点”），"
            "本工具自己会换算，不要自己算成时间戳。"
            "打开/关闭软件时填 app_action（open/close）与 app_name，label 留空"
            "（例：“明天早上八点打开QQ” → time_text=“明天早上8点”、app_action=“open”、app_name=“QQ”）。"
            "用户说的是时长（“十分钟后提醒我”）时用 set_timer，不要用本工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "time_text": {
                    "type": "string",
                    "description": (
                        "提醒的时刻，按用户说的原样填，如“下午三点”“三点半”“15:30”"
                        "“晚上八点”“明天早上7点”。"
                    ),
                },
                "label": {
                    "type": "string",
                    "description": (
                        "提醒内容，即到点时要提醒用户做什么，如“开会”“起床”。"
                        "用户没说做什么时留空；要操作软件时也留空。"
                    ),
                },
                "app_action": {
                    "type": "string",
                    "description": (
                        "到点要执行的本机软件操作：open 表示打开（启动）软件，"
                        "close 表示关闭软件。纯提醒留空不填。"
                    ),
                },
                "app_name": {
                    "type": "string",
                    "description": (
                        "要操作的软件名，按用户说的填，如“QQ”“微信”。"
                        "只有 app_action 有值时才需要填。"
                    ),
                },
            },
            "required": ["time_text"],
        },
    },
}

LIST_TIMERS_DESC = {
    "type": "function",
    "function": {
        "name": "list_timers",
        "description": (
            "列出当前正在计时的提醒。当用户问“有哪些提醒”“我设了什么提醒”"
            "“还有多久”“提醒还在吗”时调用。"
            "**必须调用本工具看真实清单**，不要凭印象回答（凭印象会说错）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}

CANCEL_TIMER_DESC = {
    "type": "function",
    "function": {
        "name": "cancel_timer",
        "description": (
            "取消正在计时的提醒。当用户说“取消提醒”“别提醒我了”“把那个喝水提醒取消掉”"
            "“提醒都取消”时调用。ref 留空表示全部取消。"
            "**必须调用本工具**：只口头答应等于没取消，到点照样会响。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": (
                        "要取消哪一个：填用户说的序号（“第一个”“第2个”）"
                        "或提醒内容（“喝水提醒”）；用户没指明时留空，表示全部取消。"
                    ),
                },
            },
        },
    },
}


# ── 工具实现 ────────────────────────────────────────────────────────────────
def _pick(extra: Dict[str, Any], names: Tuple[str, ...]) -> str:
    """从"多出来的参数"里挑一个能用的。

    大模型（尤其走文本标记那条路时）会自己发明参数名 —— 实测见过
    ``set_reminder(time="5秒后", content="喝水")``。与其直接报参数错误，
    不如按别名接住。
    """
    for name in names:
        if name not in extra:
            continue
        value = extra.get(name)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            if name in ("seconds", "delay_seconds", "delay", "duration"):
                return f"{int(value)}秒"
            if name == "minutes":
                return f"{int(value)}分钟"
            if name == "hours":
                return f"{int(value)}小时"
    return ""


def resolve_when(text: str, prefer: str) -> Tuple[Optional[int], Optional[datetime]]:
    """把时间说法解析成 ``(倒计时秒数, 到期时刻)``，两种说法互相兜底。

    实测大模型经常把两者塞错位置（"5秒后"填进 time_text、"下午三点"填进 duration），
    所以每个入口先按自己的偏好解析，失败就试另一种，能救回一大半。
    """
    raw = str(text or "").strip()
    if not raw:
        return None, None
    if prefer == "clock":
        target = parse_clock(raw)
        if target is not None:
            return None, target
        seconds = parse_duration(raw)
        return (seconds, None) if seconds is not None else (None, None)
    seconds = parse_duration(raw)
    if seconds is not None:
        return seconds, None
    target = parse_clock(raw)
    return (None, target) if target is not None else (None, None)


def _store_timer(
    conn: "ConnectionHandler",
    kind: str,
    seconds: int,
    due_at: float,
    label: str,
    action: str,
    app: str,
    config: Dict[str, Any],
) -> Tuple[Optional[_Timer], Optional[str]]:
    """护栏（数量上限）+ 落库，返回 ``(timer, error_text)``。"""
    max_timers = _int_config(config, "max_timers", DEFAULT_MAX_TIMERS, 1, 50)
    with _LOCK:
        if len(_TIMERS) >= max_timers:
            return None, f"同时最多只能有 {max_timers} 个提醒，先取消一个再设新的吧。"
        return _create(conn, kind, seconds, due_at, label, action, app), None


def _finish_create(
    timer: _Timer, action: str, app: str, log, name: str
) -> ActionResponse:
    """创建成功后的统一收尾：记日志（下发清单由调用方做）+ 返回要念的话。"""
    log.info(
        f"{name} | id={timer.id} | {timer.seconds}s | 到期="
        f"{datetime.fromtimestamp(timer.due_at).strftime('%m-%d %H:%M:%S')} | "
        f"label={timer.label!r} | 动作={action or '无'}{app} | 现有 {len(public_timers())} 个"
    )
    return ActionResponse(Action.RESPONSE, None, _created_text(timer))


def _resolve_action(
    app_action: str, app_name: str
) -> Tuple[str, str, Optional[str]]:
    """解析"到点要做什么"的参数，返回 ``(action, app, error_text)``。

    ``error_text`` 非空时调用方直接把它当回复返回（参数不完整/听不懂）；
    空字符串的 action 表示这是纯提醒。
    """
    action = parse_app_action(app_action)
    app = str(app_name or "").strip()
    if action == "?":
        return "", "", "没听清是要打开还是关闭，再说一次，比如“十秒后打开QQ”。"
    if not action:
        return "", "", None
    if not app:
        return "", "", "要操作哪个软件呢？说个名字，比如“十秒后打开QQ”。"
    return action, app[:40], None


_LABEL_ACTION_RE = re.compile(r"^(打开|启动|开一下|关闭|关掉|退出)\s*(.+)$")


def _infer_action_from_label(
    conn: "ConnectionHandler", label: str
) -> Optional[Tuple[str, str]]:
    """提醒内容写成“打开QQ”时也当定时任务处理。

    大模型经常把“十秒后打开记事本”整句塞进 label 而不填 app_action（实测），
    所以这里做一层兜底识别；但**只有这个软件真的在清单里**才转成动作 ——
    “提醒我打开冰箱”这种要老老实实当提醒，不能到点了播报“没找到冰箱这个软件”。
    """
    match = _LABEL_ACTION_RE.match(label)
    if not match:
        return None
    action = parse_app_action(match.group(1))
    app = match.group(2).strip()
    if not action or action == "?" or not app:
        return None
    try:
        from plugins_func.functions.local_app import _find_app
        from plugins_func.functions.local_app import _plugin_config as _apps_config

        apps = [
            item
            for item in (_apps_config(conn).get("apps") or [])
            if isinstance(item, dict)
        ]
    except Exception as error:  # 清单读不到就老老实实当提醒
        logger.bind(tag=TAG).warning(f"推断定时任务时读取软件清单失败 | {error}")
        return None
    if not _find_app(apps, app):
        return None
    return action, app[:40]


@register_function("set_timer", SET_TIMER_DESC, ToolType.SYSTEM_CTL)
async def set_timer(
    conn: "ConnectionHandler",
    duration: str = "",
    label: str = "",
    app_action: str = "",
    app_name: str = "",
    **extra,
) -> ActionResponse:
    """倒计时提醒/定时任务：多久之后提醒我，或者多久之后开关某个软件。"""
    log = conn.logger.bind(tag=TAG)
    config = _plugin_config(conn)

    when_text = str(duration or "").strip() or _pick(
        extra,
        ("duration", "time", "delay", "delay_seconds", "after", "interval", "seconds", "minutes", "hours"),
    )
    seconds, target = resolve_when(when_text, "countdown")
    if seconds is None and target is None:
        log.info(f"set_timer | 时间没解析出来 | duration={when_text!r}")
        return ActionResponse(
            Action.RESPONSE,
            None,
            "没听清要等多久，再说一次，比如“十分钟后提醒我喝水”。",
        )
    if target is not None:
        # 模型把时刻塞进了 duration（"下午三点"），按定点处理
        seconds = int((target - datetime.now()).total_seconds())
        if seconds < MIN_SECONDS:
            return ActionResponse(
                Action.RESPONSE, None, "这个时间已经过了，换个还没到的时间吧。"
            )
        kind, due_at = "clock", target.timestamp()
    else:
        if seconds < MIN_SECONDS:
            return ActionResponse(
                Action.RESPONSE, None, f"时间太短了，至少要先等 {MIN_SECONDS} 秒钟。"
            )
        if seconds > MAX_SECONDS:
            return ActionResponse(
                Action.RESPONSE, None, "时间太长了，一次最多只能设 24 小时。"
            )
        kind, due_at = "countdown", time.time() + seconds

    label = str(label or "").strip() or _pick(
        extra, ("label", "content", "text", "message", "reason", "what", "note")
    )
    app_action = str(app_action or "").strip() or _pick(
        extra, ("action", "op", "operation", "do", "command")
    )
    app_name = str(app_name or "").strip() or _pick(
        extra, ("app", "application", "name", "target", "program", "software")
    )
    if app_name and not app_action:
        # 只给了软件名不给动作 → 默认是"打开"
        app_action = "open"

    action, app, error = _resolve_action(app_action, app_name)
    if error:
        log.info(
            f"set_timer | 动作参数不完整 | app_action={app_action!r} | app_name={app_name!r}"
        )
        return ActionResponse(Action.RESPONSE, None, error)

    cleaned = _clean_label(label)
    if not action and cleaned:
        inferred = _infer_action_from_label(conn, cleaned)
        if inferred:
            action, app = inferred
            cleaned = ""
    if action and not cleaned:
        cleaned = f"{action_verb(action)}{app}"

    timer, error = _store_timer(
        conn, kind, seconds, due_at, cleaned, action, app, config
    )
    if error:
        return ActionResponse(Action.RESPONSE, None, error)
    await push_timer_list(conn)
    return _finish_create(timer, action, app, log, "set_timer")


@register_function("set_reminder", SET_REMINDER_DESC, ToolType.SYSTEM_CTL)
async def set_reminder(
    conn: "ConnectionHandler",
    time_text: str = "",
    label: str = "",
    app_action: str = "",
    app_name: str = "",
    **extra,
) -> ActionResponse:
    """定点提醒/定点任务：几点提醒我，或者几点开关某个软件。"""
    log = conn.logger.bind(tag=TAG)
    config = _plugin_config(conn)

    when_text = str(time_text or "").strip() or _pick(
        extra, ("time_text", "time", "when", "at", "clock", "delay", "after", "seconds")
    )
    target_seconds, target = resolve_when(when_text, "clock")
    if target_seconds is None and target is None:
        log.info(f"set_reminder | 时刻没解析出来 | time_text={when_text!r}")
        return ActionResponse(
            Action.RESPONSE,
            None,
            "没听清是几点，再说一次，比如“下午三点提醒我开会”。",
        )
    if target is None:
        # 模型把时长塞进了 time_text（"5秒后"），按倒计时处理
        seconds = target_seconds
        if seconds < MIN_SECONDS:
            return ActionResponse(
                Action.RESPONSE, None, f"时间太短了，至少要先等 {MIN_SECONDS} 秒钟。"
            )
        if seconds > MAX_SECONDS:
            return ActionResponse(
                Action.RESPONSE, None, "时间太长了，一次最多只能设 24 小时。"
            )
        kind, due_at = "countdown", time.time() + seconds
    else:
        seconds = int((target - datetime.now()).total_seconds())
        if seconds < MIN_SECONDS:
            return ActionResponse(
                Action.RESPONSE, None, "这个时间已经过了，换个还没到的时间吧。"
            )
        if seconds > MAX_SECONDS:
            return ActionResponse(
                Action.RESPONSE, None, "时间太远了，只能设 24 小时以内的提醒。"
            )
        kind, due_at = "clock", target.timestamp()

    label = str(label or "").strip() or _pick(
        extra, ("label", "content", "text", "message", "reason", "what", "note")
    )
    app_action = str(app_action or "").strip() or _pick(
        extra, ("action", "op", "operation", "do", "command")
    )
    app_name = str(app_name or "").strip() or _pick(
        extra, ("app", "application", "name", "target", "program", "software")
    )
    if app_name and not app_action:
        app_action = "open"

    action, app, error = _resolve_action(app_action, app_name)
    if error:
        log.info(
            f"set_reminder | 动作参数不完整 | app_action={app_action!r} | app_name={app_name!r}"
        )
        return ActionResponse(Action.RESPONSE, None, error)

    cleaned = _clean_label(label)
    if not action and cleaned:
        inferred = _infer_action_from_label(conn, cleaned)
        if inferred:
            action, app = inferred
            cleaned = ""
    if action and not cleaned:
        cleaned = f"{action_verb(action)}{app}"

    timer, error = _store_timer(
        conn, kind, seconds, due_at, cleaned, action, app, config
    )
    if error:
        return ActionResponse(Action.RESPONSE, None, error)
    await push_timer_list(conn)
    return _finish_create(timer, action, app, log, "set_reminder")


@register_function("list_timers", LIST_TIMERS_DESC, ToolType.SYSTEM_CTL)
async def list_timers(conn: "ConnectionHandler") -> ActionResponse:
    """列出正在计时的提醒。"""
    with _LOCK:
        timers = sorted(_TIMERS.values(), key=lambda item: item.due_at)
    if not timers:
        return ActionResponse(Action.RESPONSE, None, "现在没有正在计时的提醒。")
    return ActionResponse(
        Action.RESPONSE, None, f"现在有 {len(timers)} 个提醒：{_list_text(timers)}。"
    )


@register_function("cancel_timer", CANCEL_TIMER_DESC, ToolType.SYSTEM_CTL)
async def cancel_timer(conn: "ConnectionHandler", ref: str = "") -> ActionResponse:
    """取消正在计时的提醒。"""
    log = conn.logger.bind(tag=TAG)
    removed, error = cancel(ref)
    if error:
        log.info(f"cancel_timer | 没有取消掉 | ref={ref!r} | {error}")
        return ActionResponse(Action.RESPONSE, None, error)

    await push_timer_list(conn)
    log.info(f"cancel_timer | 取消 {len(removed)} 个 | ref={ref!r}")
    return ActionResponse(Action.RESPONSE, None, _cancelled_text(removed))
