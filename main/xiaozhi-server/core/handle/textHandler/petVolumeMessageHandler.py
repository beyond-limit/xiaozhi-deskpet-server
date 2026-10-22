"""桌宠音量状态上报（上行消息 ``type=pet_volume``）。

桌宠（声明 ``hello.features.volume``）在以下时机主动上报自己的两路音量（0~1）：
  - WS 会话建立（收到 hello）之后；
  - 用户在右键菜单拖动音量滑杆（松手）之后；
  - 执行完服务端下发的 volume 指令之后。

服务端把最新值缓存在 ``conn.pet_volume_state``，供 ``set_pet_volume`` 做相对调整
与 ``get_pet_volume`` 查询（见 plugins_func/functions/pet_volume.py）。
这条链路是**异步、单向**的：工具下发指令后不等待回报（不给对话路径加延迟），
回报到达只是让服务端的状态与播报更准。
"""

from typing import Any, Dict

from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType

TAG = __name__


def _clamp01(value: Any):
    """把上报值解析成 0~1；解析不出来返回 None（该路音量保持未知）。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return min(1.0, max(0.0, num))


def _percent(value) -> str:
    return "未知" if value is None else f"{round(value * 100)}%"


class PetVolumeTextMessageHandler(TextMessageHandler):
    """桌宠音量状态上报处理器"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.PET_VOLUME

    async def handle(self, conn, msg_json: Dict[str, Any]) -> None:
        speech = _clamp01(msg_json.get("speech"))
        music = _clamp01(msg_json.get("music"))
        conn.pet_volume_state = {"speech": speech, "music": music}
        conn.logger.bind(tag=TAG).info(
            f"桌宠音量上报 | 说话={_percent(speech)} 音乐={_percent(music)}"
        )
