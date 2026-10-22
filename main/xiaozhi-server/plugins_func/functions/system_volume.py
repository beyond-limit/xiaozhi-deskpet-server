"""系统音量插件（语音："音量调到 50%""声音大一点""静音"）。

用途：把"调音量"这类请求作用到**电脑的系统主音量**（默认播放设备，与任务栏音量滑杆一致），
例如：``音量调到 50%``、``声音大一点``、``音量小 20``、``声音太大了``、
``静音``、``取消静音``、``现在音量是多少``。

设计要点：

1. **三个工具各管一件事**：``set_volume``（调到指定值 / 相对调整）、
   ``mute_volume``（静音开关）、``get_volume``（读当前值）。
   每个工具的 description 都写明"这类请求必须调用本工具"——只口头答应等于没调
   （同 timer_reminder 的教训：只写"…时调用"时，模型会直接回"好的，帮你调大"而不调工具）。
2. **口语数字与口语方向都由本插件解析**：``50%`` / ``五十`` / ``百分之五十`` / ``最大`` /
   ``一半`` / ``+10`` / ``调大一点`` 都要能认，大模型只需把用户原话原样传进来。
3. **写后读回真实值再播报**：播报的百分比来自设备当前值，不拿参数当结论
   （避免"说调到了 50%，其实没生效"）。
4. **调到 0 以上时顺带取消静音**：用户说"调到 50%"就是要听到声音；
   音量 0 不等于静音（静音保留音量值），播报里不混为一谈。
5. 经 ``pycaw``（comtypes 封装的 Windows Core Audio）操作默认播放设备；
   切换默认设备/句柄失效时**自动重建连接重试一次**，再失败才如实报错。
6. 一切失败都如实播报（缺 pycaw / 音频接口异常），不静默、不假装成功。
"""

import re
import threading
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

from config.logger import setup_logging
from plugins_func.register import Action, ActionResponse, ToolType, register_function

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

DEFAULT_STEP = 10  # "调大一点/小一点"没给具体数值时的默认步进（百分点）

_LOCK = threading.RLock()
_volume_iface = None  # 缓存的默认设备主音量接口（失效时置 None 重建）


# ── 配置 ────────────────────────────────────────────────────────────────────
def _plugin_config(conn: "ConnectionHandler") -> Dict[str, Any]:
    plugins = conn.config.get("plugins") or {}
    return plugins.get("system_volume") or {}


def _step_config(conn: "ConnectionHandler") -> int:
    try:
        return min(max(int(_plugin_config(conn).get("step", DEFAULT_STEP)), 1), 50)
    except (TypeError, ValueError):
        return DEFAULT_STEP


# ── 音量读写（pycaw / Core Audio）────────────────────────────────────────────
def _endpoint_locked():
    """取（必要时创建）默认播放设备的主音量接口。调用方须持有 _LOCK。"""
    global _volume_iface
    if _volume_iface is None:
        from pycaw.pycaw import AudioUtilities

        device = AudioUtilities.GetSpeakers()
        # 新版 pycaw 返回 AudioDevice（接口在 .EndpointVolume），旧版直接返回接口本身
        _volume_iface = getattr(device, "EndpointVolume", None) or device
    return _volume_iface


def _with_device(op):
    """执行一次音频接口操作；句柄失效（换默认设备等）时重建连接重试一次。"""
    global _volume_iface
    with _LOCK:
        try:
            return op(_endpoint_locked())
        except Exception:
            _volume_iface = None
            return op(_endpoint_locked())


def _read_state() -> Tuple[int, bool]:
    """读当前（音量百分比 0~100，是否静音）。"""

    def _once(vol):
        return vol.GetMasterVolumeLevelScalar(), bool(vol.GetMute())

    level, muted = _with_device(_once)
    return int(round(level * 100)), muted


def _write_level(percent: int) -> None:
    value = max(0.0, min(1.0, percent / 100.0))
    _with_device(lambda vol: vol.SetMasterVolumeLevelScalar(value, None))


def _write_mute(muted: bool) -> None:
    _with_device(lambda vol: vol.SetMute(bool(muted), None))


def _device_error(log, where: str, exc: Exception) -> ActionResponse:
    log.error(f"{where} | 音量控制失败 | {type(exc).__name__}: {exc}")
    return ActionResponse(
        Action.RESPONSE,
        None,
        "音量操作没成功：这台电脑的音频接口暂时用不了，稍后再试一次吧。",
    )


# ── 参数解析 ────────────────────────────────────────────────────────────────
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


def _pick(extra: Dict[str, Any], names: Tuple[str, ...]) -> Any:
    """从"多出来的参数"里挑一个能用的（模型自创参数名时的兜底）。"""
    for name in names:
        value = extra.get(name)
        if value not in (None, ""):
            return value
    return None


def _clamp(value: int, low: int = 0, high: int = 100) -> int:
    return max(low, min(high, value))


def _cn_number(raw: str) -> Optional[int]:
    """中文数字 → 整数（支持"五十""十五""一百二十"这类写法）。"""
    text = str(raw or "").strip()
    if not text or not all(ch in _CN_DIGITS or ch in "十百" for ch in text):
        return None
    total = 0
    if "百" in text:
        head, text = text.split("百", 1)
        total += (_CN_DIGITS.get(head, 1) if head else 1) * 100
    if "十" in text:
        tens, ones = text.split("十", 1)
        total += (_CN_DIGITS.get(tens, 1) if tens else 1) * 10
        total += _CN_DIGITS.get(ones, 0) if ones else 0
    elif text:
        total += _CN_DIGITS.get(text, 0)
    return total


def _parse_level(raw: Any) -> Optional[int]:
    """解析"目标音量"为 0~100：50 / "50%" / "百分之五十" / "最大" / "一半"。"""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return _clamp(int(round(raw)))
    text = str(raw).strip().lower()
    if not text:
        return None
    text = text.replace("％", "%").replace("百分之", "")
    if any(word in text for word in ("最大", "最高", "满", "全部")):
        return 100
    if any(word in text for word in ("最小", "最低", "没声", "无声")):
        return 0
    if "一半" in text:
        return 50
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if match:
        return _clamp(int(round(float(match.group(0)))))
    cn = _cn_number(text)
    if cn is not None:
        return _clamp(cn)
    return None


def _parse_delta(raw: Any, step: int) -> Optional[int]:
    """解析"相对调整量"：10 / "+10" / "调大"（→+step）/ "小一点"（→-step）。"""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return _clamp(int(round(raw)), -100, 100)
    text = str(raw).strip().lower()
    if not text:
        return None
    match = re.search(r"[+-]?\d+(?:\.\d+)?", text)
    if match:
        return _clamp(int(round(float(match.group(0)))), -100, 100)
    negative = any(word in text for word in ("小", "低", "减", "down"))
    # "小一点/大一些"里的"一/一些"是模糊量词，不是数字，必须剔除
    # （否则"小一点"会被解析成 -1 而不是 -step）
    cleaned = re.sub(r"(一点儿|一点|一些|点儿|些许)", "", text)
    cn_match = re.search(r"[零〇一二两三四五六七八九十百]+", cleaned)
    if cn_match:
        cn = _cn_number(cn_match.group(0))
        if cn is not None:
            return _clamp(-cn if negative else cn, -100, 100)
    if any(word in text for word in ("大", "高", "加", "up")):
        return step
    if negative:
        return -step
    return None


def _parse_mute(raw: Any) -> Optional[bool]:
    """解析静音意图：True 静音 / False 取消静音 / None 没说清（按切换处理）。"""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if not text:
        return None
    if any(
        word in text
        for word in ("取消", "恢复", "解除", "别静音", "不要静音", "unmute", "off", "false", "no", "0")
    ):
        return False
    if any(word in text for word in ("静音", "无声", "别出声", "mute", "on", "true", "yes", "1")):
        return True
    return None


# ── 工具描述 ────────────────────────────────────────────────────────────────
SET_VOLUME_DESC = {
    "type": "function",
    "function": {
        "name": "set_volume",
        "description": (
            "【系统音量】调大／调小／调到指定大小**电脑的系统音量**（右下角那个音量）。"
            "**这类请求必须调用本工具**：只回一句“好的，帮你调大”等于没调，音量不会变。"
            "当用户说“电脑音量调到 50%”“系统音量小 20”“扬声器声音太大了”"
            "“把电脑声音调到 30”“系统音量开到最大”这类**明确指电脑/系统/扬声器音量**的话时调用。"
            "用户给了具体数值 → 填 level（0~100 的整数）；"
            "只说“大一点／小一点”这类相对调整 → 填 delta（调大填正数如 10，调小填负数如 -10）。"
            "两个参数最多填一个，不要同时填；也不要把对话上文里的旧数值当成本次的 level。"
            "**如果用户是让桌宠说话大声点/调它的说话或音乐音量，即使不带主语（“声音大一点”），"
            "也要用 set_pet_volume，不要用本工具。**"
            "“静音／取消静音”用 mute_volume；问“现在音量多少”见 get_volume。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "level": {
                    "type": "integer",
                    "description": "目标音量百分比（0~100 的整数）。用户说“调到 50%”就填 50。",
                },
                "delta": {
                    "type": "integer",
                    "description": (
                        "在**当前音量**基础上的调整量，调大填正数（如 10），"
                        "调小填负数（如 -10）。只在用户没给具体数值时使用。"
                    ),
                },
            },
        },
    },
}

MUTE_VOLUME_DESC = {
    "type": "function",
    "function": {
        "name": "mute_volume",
        "description": (
            "【静音开关】**把电脑静音／取消静音**。**这类请求必须调用本工具**，"
            "只口头答应等于没静音。"
            "用户说“静音”“别出声了”“把声音关掉”“先静音一下” → mute 填 true；"
            "说“取消静音”“恢复声音”“别静音了”“把声音开回来” → mute 填 false。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mute": {
                    "type": "boolean",
                    "description": "true = 静音，false = 取消静音。",
                },
            },
        },
    },
}

GET_VOLUME_DESC = {
    "type": "function",
    "function": {
        "name": "get_volume",
        "description": (
            "读取当前**电脑的系统音量**与静音状态。用户明确问“电脑音量多少”“系统音量多大”"
            "“电脑是不是静音了”时调用。**必须调用本工具看真实数值**，不要凭印象回答。"
            "如果用户问的是**桌宠自己**的音量（说话/音乐，常不带“电脑”二字），用 get_pet_volume。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}


# ── 工具实现 ────────────────────────────────────────────────────────────────
@register_function("set_volume", SET_VOLUME_DESC, ToolType.SYSTEM_CTL)
async def set_volume(
    conn: "ConnectionHandler",
    level: Any = None,
    delta: Any = None,
    **extra,
) -> ActionResponse:
    """调节系统音量：绝对设置（level）或相对调整（delta）。"""
    log = conn.logger.bind(tag=TAG)

    if level is None:
        level = _pick(
            extra,
            ("volume", "volume_level", "level_percent", "percent", "value", "to", "target", "number"),
        )
    if delta is None:
        delta = _pick(extra, ("change", "by", "step", "adjust", "amount", "offset"))

    target_level = _parse_level(level)
    target_delta = _parse_delta(delta, _step_config(conn))

    if target_level is None and target_delta is None:
        log.info(f"set_volume | 参数没能解析 | level={level!r} | delta={delta!r}")
        return ActionResponse(
            Action.RESPONSE,
            None,
            "要把音量调到多少？可以说“调到 50%”，或者“调大一点”。",
        )

    try:
        current, muted = _read_state()
    except Exception as exc:
        return _device_error(log, "set_volume/read", exc)

    if target_level is not None:
        target = _clamp(target_level)
        mode = "absolute"
    else:
        target = _clamp(current + target_delta)
        mode = "relative"

    if target == current:
        log.info(f"set_volume | 无变化 | current={current} target={target}")
        if target >= 100:
            return ActionResponse(Action.RESPONSE, None, "音量已经是最大的 100% 了。")
        if target <= 0:
            return ActionResponse(Action.RESPONSE, None, "音量已经在最低的 0% 了。")
        return ActionResponse(Action.RESPONSE, None, f"音量已经是 {target}% 了。")

    try:
        _write_level(target)
        unmuted = False
        if target > 0 and muted:
            # 用户说"调到 X%"就是要听到声音，静音挡着的话顺手取消
            _write_mute(False)
            unmuted = True
        real, _ = _read_state()
    except Exception as exc:
        return _device_error(log, "set_volume/write", exc)

    if mode == "relative" and target >= 100:
        text = f"音量已经调到最大 {real}% 了。"
    elif mode == "relative" and target <= 0:
        text = "音量已经调到最低 0% 了。"
    elif mode == "relative":
        text = f"音量{'调高' if target_delta > 0 else '调低'}到 {real}%。"
    else:
        text = f"系统音量已调到 {real}%。"
    if unmuted:
        text += "也帮你取消了静音。"

    log.info(f"set_volume | {mode} | {current}% -> {real}% | unmuted={unmuted}")
    return ActionResponse(Action.RESPONSE, None, text)


@register_function("mute_volume", MUTE_VOLUME_DESC, ToolType.SYSTEM_CTL)
async def mute_volume(
    conn: "ConnectionHandler",
    mute: Any = None,
    **extra,
) -> ActionResponse:
    """静音／取消静音。"""
    log = conn.logger.bind(tag=TAG)

    if mute is None:
        mute = _pick(extra, ("action", "state", "mode", "value", "mute_state", "switch", "enable"))
    want = _parse_mute(mute)

    try:
        current, muted = _read_state()
    except Exception as exc:
        return _device_error(log, "mute_volume/read", exc)

    target = (not muted) if want is None else want

    if target == muted:
        log.info(f"mute_volume | 已经是目标状态 | muted={muted} want={want!r}")
        if muted:
            return ActionResponse(Action.RESPONSE, None, f"现在就是静音的（音量 {current}%）。")
        return ActionResponse(Action.RESPONSE, None, f"现在没有静音，音量 {current}%。")

    try:
        _write_mute(target)
        real, real_muted = _read_state()
    except Exception as exc:
        return _device_error(log, "mute_volume/write", exc)

    log.info(f"mute_volume | muted={real_muted} | volume={real}%")
    if real_muted:
        return ActionResponse(Action.RESPONSE, None, f"已静音，当前音量 {real}%。")
    return ActionResponse(Action.RESPONSE, None, f"已取消静音，当前音量 {real}%。")


@register_function("get_volume", GET_VOLUME_DESC, ToolType.SYSTEM_CTL)
async def get_volume(conn: "ConnectionHandler") -> ActionResponse:
    """读取当前系统音量与静音状态。"""
    log = conn.logger.bind(tag=TAG)
    try:
        level, muted = _read_state()
    except Exception as exc:
        return _device_error(log, "get_volume/read", exc)

    if muted:
        return ActionResponse(Action.RESPONSE, None, f"当前系统音量 {level}%，正处于静音状态。")
    return ActionResponse(Action.RESPONSE, None, f"当前系统音量 {level}%，没有静音。")
