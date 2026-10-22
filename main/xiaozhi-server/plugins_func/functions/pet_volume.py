"""桌宠独立音量插件（语音："你说话声音大一点""音乐声音小一点"）。

用途：把"调桌宠音量"这类请求作用到**桌宠客户端自己的两路音量**上，
与电脑的系统音量（`system_volume` 插件）区分开：
    - 说话音量（speech）：桌宠播报语音（TTS）的音量；
    - 音乐音量（music） ：背景音乐（点歌）的音量。
例如：``你说话声音大一点``、``音乐声音小一点``、``说话音量调到 80%``、
``把音乐调到 30``、``桌宠音量是多少``。

设计要点：

1. **服务端下发指令、客户端执行**（客户端声明 ``hello.features.volume`` 才支持）：
   本插件下发 ``{"type":"volume","target":...,"action":"adjust","delta":±N}``
   或 ``{"type":"volume","target":...,"action":"set","level":0~100}``，
   桌宠本地计算/clamp/落盘后回报真实值（``{"type":"pet_volume",...}``）。
2. **播报用最近一次上报的状态**（``conn.pet_volume_state``，由
   core/handle/textHandler/petVolumeMessageHandler.py 维护）：
   这条链路**异步不等待**（不给对话路径加延迟），所以——
     - 绝对设置：播报目标值（客户端 clamp 范围与本插件一致）；
     - 相对调整：有状态下播报"调整后的值"，状态未知时只播报"已调大/调小"；
     - 查询：读状态；从未上报过则如实说"还没收到回报"。
3. **不支持就明说**：客户端没声明 features.volume（如 ESP32）时明确回
   "不支持单独调桌宠音量"，不退化成静默失败。
4. **参数兜底**（模型不按 schema 来的老问题）：target 认中文（说话/语音/音乐/歌曲…），
   level/delta 的口语解析直接复用 system_volume（"大一点"→+step、"百分之五十"→50）。
5. 音频链路分工：说话音量 = TTS 增益（`speechGain`，插在口型 analyser 之后）；
   音乐音量 = 背景音乐 ``<audio>`` 元素的 volume。两者互不影响，与系统音量也互不影响。
"""

import asyncio
import json
from typing import TYPE_CHECKING, Any, Dict, Optional

from config.logger import setup_logging
from plugins_func.register import Action, ActionResponse, ToolType, register_function

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

DEFAULT_STEP = 10  # "大一点/小一点"没给具体数值时的默认步进（百分点）

# 目标名解析（口语 → speech/music）。先查音乐：含"音乐/歌"的词优先，
# 避免"背景音乐"这类词被其它关键字抢先匹配。
_MUSIC_WORDS = ("音乐", "歌曲", "歌", "伴奏", "music", "song", "bgm")
_SPEECH_WORDS = ("说话", "语音", "播报", "你的声", "自己说", "讲话", "speech", "voice", "tts")


def _plugin_state(conn: "ConnectionHandler") -> Dict[str, Optional[float]]:
    state = getattr(conn, "pet_volume_state", None)
    return state if isinstance(state, dict) else {}


def _volume_supported(conn: "ConnectionHandler") -> bool:
    """客户端是否在 hello.features 里声明了 volume（桌宠声明，ESP32 没有）。"""
    return bool((conn.features or {}).get("volume"))


def _music_on_server(conn: "ConnectionHandler") -> bool:
    """音乐是否由服务端播放进程输出（方案B）。

    判定与 `online_music._play_file` 保持一致：``playback: server`` 强制，
    或 ``auto`` 且客户端没声明 ``features.bgm``（ESP32）。
    """
    plugin = (getattr(conn, "config", None) or {}).get("plugins") or {}
    config = plugin.get("online_music") or {}
    mode = str(config.get("playback", "auto") or "auto").strip().lower()
    bgm_supported = bool((getattr(conn, "features", None) or {}).get("bgm"))
    return mode == "server" or (mode == "auto" and not bgm_supported)


def _parse_target(raw: Any) -> Optional[str]:
    text = str(raw or "").strip().lower()
    if not text:
        return None
    if any(word in text for word in _MUSIC_WORDS):
        return "music"
    if any(word in text for word in _SPEECH_WORDS):
        return "speech"
    return None


def _target_label(target: str) -> str:
    return "音乐音量" if target == "music" else "说话音量"


def _percent_text(value: Optional[float]) -> str:
    if value is None:
        return "未知"
    return f"{round(value * 100)}%"


async def _send(conn: "ConnectionHandler", payload: Dict[str, Any]) -> bool:
    websocket = getattr(conn, "websocket", None)
    if not websocket:
        return False
    try:
        await websocket.send(json.dumps(payload, ensure_ascii=False))
        return True
    except Exception as error:
        logger.bind(tag=TAG).warning(f"下发音量指令失败 | {type(error).__name__}: {error}")
        return False


# ── 工具描述 ────────────────────────────────────────────────────────────────
SET_PET_VOLUME_DESC = {
    "type": "function",
    "function": {
        "name": "set_pet_volume",
        "description": (
            "【桌宠音量】调节**桌宠自己**的音量：说话音量（它播报语音的声音）"
            "或音乐音量（点歌播放的背景音乐）——两者分开控制。"
            "**这类请求必须调用本工具**：只回“好的，帮你调大”等于没调。"
            "用户说“你说话声音大一点”“说话声音小一点”“你的声音太大了”"
            "“音乐声音小一点”“把音乐调到 30”“背景音乐太吵了”“说话音量调到 80%”"
            "这类话时调用。target 填 speech（说话/语音/你的声音）或 music（音乐/歌/背景音乐）；"
            "给了具体数值填 level（0~100），只说“大一点/小一点”填 delta（调大正数如 10，调小负数如 -10）。"
            "**不带主语的口语**（“声音大一点”“音量小一点”“声音太大了”）也按说话音量处理"
            "——用户在跟你说话，多半是想让你说大声点/小声点；"
            "只有明确说“电脑音量／系统音量／扬声器音量”时才用 set_volume（那是电脑总音量）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "调哪一路：speech＝说话音量；music＝音乐/背景音乐音量。",
                },
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
            "required": ["target"],
        },
    },
}

GET_PET_VOLUME_DESC = {
    "type": "function",
    "function": {
        "name": "get_pet_volume",
        "description": (
            "读取**桌宠自己**的当前音量（说话音量与音乐音量，两路分开）。"
            "用户问“你说话声音多大”“音乐音量多少”“桌宠音量是多少”时调用；"
            "**笼统地问“现在音量是多少”“音量多大”而没有明确说“电脑/系统”时，也用本工具**"
            "（用户多半在问你的音量）。"
            "**必须调用本工具看真实数值**，不要凭印象回答。"
            "注意：明确问“电脑音量/系统音量多少”要用 get_volume，不是本工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}


# ── 工具实现 ────────────────────────────────────────────────────────────────
@register_function("set_pet_volume", SET_PET_VOLUME_DESC, ToolType.SYSTEM_CTL)
async def set_pet_volume(
    conn: "ConnectionHandler",
    target: str = "",
    level: Any = None,
    delta: Any = None,
    **extra,
) -> ActionResponse:
    """调节桌宠自己的说话音量 / 音乐音量（下发指令，客户端执行后回报）。"""
    log = conn.logger.bind(tag=TAG)
    # 口语解析与 system_volume 完全一致（"大一点"→+step、"百分之五十"→50…）
    from plugins_func.functions.system_volume import (
        _parse_delta,
        _parse_level,
        _pick,
    )

    if not target:
        target = _pick(extra, ("kind", "type", "which", "scope", "name", "part"))
    goal = _parse_target(target)
    if goal is None:
        log.info(f"set_pet_volume | 目标没解析出来 | target={target!r} extra={list(extra)}")
        return ActionResponse(
            Action.RESPONSE,
            None,
            "是要调说话的音量，还是音乐的音量？",
        )

    if level is None:
        level = _pick(
            extra,
            ("volume", "volume_level", "level_percent", "percent", "value", "to", "target_level", "number"),
        )
    if delta is None:
        delta = _pick(extra, ("change", "by", "step", "adjust", "amount", "offset"))

    target_level = _parse_level(level)
    target_delta = _parse_delta(delta, DEFAULT_STEP)

    server_music = goal == "music" and _music_on_server(conn)
    if not _volume_supported(conn) and not server_music:
        log.info(f"set_pet_volume | 客户端不支持 features.volume | target={goal}")
        return ActionResponse(
            Action.RESPONSE,
            None,
            "这台设备不支持单独调节桌宠音量。",
        )

    if target_level is None and target_delta is None:
        log.info(f"set_pet_volume | 参数没能解析 | target={goal} level={level!r} delta={delta!r}")
        return ActionResponse(
            Action.RESPONSE,
            None,
            "要把音量调到多少？可以说“调到 50%”，或者“调大一点”。",
        )

    # 方案B：音乐由服务端播放进程输出时，音量直接调播放器增益（不走客户端 WS）
    if server_music:
        try:
            from core.utils.music_player import get_music_player

            player = get_music_player()
            if target_level is not None:
                level_value = max(0, min(100, target_level))
                await asyncio.to_thread(player.set_volume, level_value / 100.0)
                log.info(f"set_pet_volume | server music | set | level={level_value}")
                return ActionResponse(
                    Action.RESPONSE, None, f"{_target_label(goal)}已调到 {level_value}%。"
                )

            state = await asyncio.to_thread(player.status)
            current = state.get("volume")
            if current is None:
                return ActionResponse(Action.RESPONSE, None, "没读到服务端音乐音量，稍后再试试。")
            value = max(0.0, min(1.0, float(current) + target_delta / 100.0))
            await asyncio.to_thread(player.set_volume, value)
            percent = round(value * 100)
            if percent >= 100:
                text = f"{_target_label(goal)}已经调到最大 100% 了。"
            elif percent <= 0:
                text = f"{_target_label(goal)}已经调到最低 0% 了。"
            else:
                text = f"{_target_label(goal)}已{'调高' if target_delta > 0 else '调低'}到 {percent}%。"
            log.info(
                f"set_pet_volume | server music | adjust | delta={target_delta} | 播报={text}"
            )
            return ActionResponse(Action.RESPONSE, None, text)
        except Exception as error:
            log.warning(
                f"set_pet_volume | 服务端音乐音量调节失败 | {type(error).__name__}: {error}"
            )
            return ActionResponse(Action.RESPONSE, None, "音乐没调成功：服务端播放器不可用。")

    state = _plugin_state(conn)
    current = state.get(goal)  # 0~1 或 None（客户端还没上报过）

    if target_level is not None:
        level_value = max(0, min(100, target_level))
        sent = await _send(
            conn, {"type": "volume", "target": goal, "action": "set", "level": level_value}
        )
        if not sent:
            return ActionResponse(Action.RESPONSE, None, "音量没调成功：和桌宠的连接好像断了。")
        log.info(f"set_pet_volume | set | target={goal} level={level_value}")
        return ActionResponse(Action.RESPONSE, None, f"{_target_label(goal)}已调到 {level_value}%。")

    # 相对调整（delta）
    sent = await _send(
        conn, {"type": "volume", "target": goal, "action": "adjust", "delta": target_delta}
    )
    if not sent:
        return ActionResponse(Action.RESPONSE, None, "音量没调成功：和桌宠的连接好像断了。")

    if current is not None:
        new_percent = max(0, min(100, round(current * 100) + target_delta))
        if new_percent >= 100:
            text = f"{_target_label(goal)}已经调到最大 100% 了。"
        elif new_percent <= 0:
            text = f"{_target_label(goal)}已经调到最低 0% 了。"
        else:
            text = f"{_target_label(goal)}已{'调高' if target_delta > 0 else '调低'}到 {new_percent}%。"
        log.info(f"set_pet_volume | adjust | target={goal} delta={target_delta} | 播报={text}")
        return ActionResponse(Action.RESPONSE, None, text)

    word = "调大了" if target_delta > 0 else "调小了"
    log.info(f"set_pet_volume | adjust | target={goal} delta={target_delta} | 状态未知（客户端还没上报）")
    return ActionResponse(
        Action.RESPONSE,
        None,
        f"已经帮你把{'音乐声音' if goal == 'music' else '说话声音'}{word}。",
    )


@register_function("get_pet_volume", GET_PET_VOLUME_DESC, ToolType.SYSTEM_CTL)
async def get_pet_volume(conn: "ConnectionHandler") -> ActionResponse:
    """读取桌宠自己的两路音量（说话/音乐）。"""
    log = conn.logger.bind(tag=TAG)
    server_music = _music_on_server(conn)
    if not _volume_supported(conn) and not server_music:
        return ActionResponse(Action.RESPONSE, None, "这台设备不支持单独调节桌宠音量。")

    state = _plugin_state(conn)
    speech = state.get("speech")
    music = state.get("music")
    if server_music and music is None:
        try:
            from core.utils.music_player import get_music_player

            state = await asyncio.to_thread(get_music_player().status)
            music = state.get("volume")
        except Exception as error:
            log.warning(
                f"get_pet_volume | 读取服务端音乐音量失败 | "
                f"{type(error).__name__}: {error}"
            )
    if speech is None and music is None:
        log.info("get_pet_volume | 客户端还没上报过音量")
        return ActionResponse(Action.RESPONSE, None, "还没收到桌宠的音量回报，稍等一下再问我。")

    log.info(f"get_pet_volume | speech={_percent_text(speech)} music={_percent_text(music)}")
    return ActionResponse(
        Action.RESPONSE,
        None,
        f"桌宠现在：说话音量 {_percent_text(speech)}，音乐音量 {_percent_text(music)}。",
    )
