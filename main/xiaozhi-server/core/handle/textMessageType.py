from enum import Enum


class TextMessageType(Enum):
    """消息类型枚举"""
    HELLO = "hello"
    ABORT = "abort"
    LISTEN = "listen"
    IOT = "iot"
    MCP = "mcp"
    SERVER = "server"
    PING = "ping"
    # 桌宠音量状态上报（客户端 → 服务端，配合 features.volume 与 set_pet_volume 工具）
    PET_VOLUME = "pet_volume"
