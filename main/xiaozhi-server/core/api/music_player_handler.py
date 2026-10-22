"""服务端音乐播放接口（方案B：音乐从服务器本机扬声器播放）。

与其它桌宠设置接口一致，只放行回环地址。桌宠的「音乐音量」滑杆与设置窗口
后续可以直接调这些接口，不必再依赖客户端 `<audio>` 通道。

- ``GET  /music-player/status``  当前播放状态（未启动时返回 running=false）
- ``POST /music-player/volume``  ``{"value": 0.35}``（0~1）
- ``POST /music-player/stop``
- ``POST /music-player/play``    ``{"file": "...", "volume": 0.35}``（仅允许曲库/缓存目录）
"""

import asyncio
import ipaddress
import os
from typing import Any, Dict

from aiohttp import web

from config.logger import setup_logging
from core.utils.music_player import MusicPlayerError, get_music_player
from plugins_func.functions.online_music import _cache_dir, _music_dirs

TAG = __name__


class MusicPlayerHandler:
    def __init__(self, config: Dict[str, Any], websocket_server=None):
        self.config = config
        self.websocket_server = websocket_server
        self.logger = setup_logging()
        self.plugin_config = (config.get("plugins") or {}).get("online_music") or {}

    def _allow_local(self, request: web.Request) -> None:
        remote = request.remote or ""
        try:
            if not ipaddress.ip_address(remote).is_loopback:
                raise web.HTTPForbidden(text="local access only")
        except ValueError as error:
            raise web.HTTPForbidden(text="local access only") from error

    def _allowed_roots(self) -> list[str]:
        roots = []
        for directory in _music_dirs(self.plugin_config):
            if directory and directory not in roots:
                roots.append(os.path.abspath(directory))
        cache = os.path.abspath(_cache_dir(self.plugin_config))
        if cache and cache not in roots:
            roots.append(cache)
        return roots

    def _resolve_allowed(self, raw: str) -> str:
        """只允许曲库 / 在线缓存目录内的文件（basename 收敛，防路径穿越）。"""
        name = os.path.basename(str(raw or "").strip())
        if not name:
            raise ValueError("缺少文件名")
        for root in self._allowed_roots():
            path = os.path.join(root, name)
            if os.path.isfile(path):
                return path
        raise ValueError(f"曲库与缓存里都没有这个文件：{name}")

    async def handle_status(self, request: web.Request) -> web.Response:
        self._allow_local(request)
        player = get_music_player()
        if not player.is_alive():
            return web.json_response(
                {
                    "ok": True,
                    "running": False,
                    "state": {
                        "playing": False,
                        "paused": False,
                        "volume": float(
                            self.plugin_config.get("server_volume", 0.35)
                        ),
                    },
                }
            )
        try:
            state = await asyncio.to_thread(player.status)
        except MusicPlayerError as error:
            return web.json_response({"ok": False, "error": str(error)}, status=503)
        return web.json_response({"ok": True, "running": True, "state": state})

    async def handle_volume(self, request: web.Request) -> web.Response:
        self._allow_local(request)
        try:
            payload = await request.json() if request.can_read_body else {}
            if not isinstance(payload, dict):
                raise ValueError("请求格式不正确")
            value = float(payload.get("value"))
        except (ValueError, TypeError) as error:
            return web.json_response({"ok": False, "error": str(error)}, status=400)

        value = min(1.0, max(0.0, value))
        try:
            state = await asyncio.to_thread(get_music_player().set_volume, value)
        except MusicPlayerError as error:
            return web.json_response({"ok": False, "error": str(error)}, status=503)
        self.logger.bind(tag=TAG).info(f"音乐音量 | {value:.2f}")
        return web.json_response({"ok": True, "state": state})

    async def handle_stop(self, request: web.Request) -> web.Response:
        self._allow_local(request)
        player = get_music_player()
        if not player.is_alive():
            return web.json_response({"ok": True, "running": False})
        try:
            state = await asyncio.to_thread(player.stop)
        except MusicPlayerError as error:
            return web.json_response({"ok": False, "error": str(error)}, status=503)
        self.logger.bind(tag=TAG).info("服务端音乐已停止（HTTP）")
        return web.json_response({"ok": True, "state": state})

    async def handle_play(self, request: web.Request) -> web.Response:
        self._allow_local(request)
        try:
            payload = await request.json() if request.can_read_body else {}
            if not isinstance(payload, dict):
                raise ValueError("请求格式不正确")
            path = self._resolve_allowed(payload.get("file", ""))
            volume = float(
                payload.get("volume", self.plugin_config.get("server_volume", 0.35))
            )
            fade = float(payload.get("fade", self.plugin_config.get("server_fade_in", 0.8)))
            title = str(payload.get("title") or "")
        except (ValueError, TypeError) as error:
            return web.json_response({"ok": False, "error": str(error)}, status=400)

        try:
            state = await asyncio.to_thread(
                get_music_player().play,
                path,
                min(1.0, max(0.0, volume)),
                max(0.0, fade),
                title,
            )
        except MusicPlayerError as error:
            return web.json_response({"ok": False, "error": str(error)}, status=503)
        self.logger.bind(tag=TAG).info(f"服务端音乐开始（HTTP） | file={path}")
        return web.json_response({"ok": True, "state": state})
