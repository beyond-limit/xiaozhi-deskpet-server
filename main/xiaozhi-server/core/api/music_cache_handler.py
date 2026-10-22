"""桌宠「音乐缓存」接口：查看 / 清理在线点歌的下载缓存。

与 `local_apps_handler` / `file_search_handler` 一致：桌宠设置窗口（Electron）
通过 127.0.0.1 调用，处理器内部只放行回环地址。
"""

import asyncio
import ipaddress
from typing import Any, Dict

from aiohttp import web

from config.logger import setup_logging
from plugins_func.functions.online_music import cache_snapshot, remove_cached_songs

TAG = __name__


class MusicCacheHandler:
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

    async def handle_list(self, request: web.Request) -> web.Response:
        """缓存目录 + 文件清单（名字 / 大小 / 修改时间）。"""
        self._allow_local(request)
        state = cache_snapshot(self.plugin_config)
        state["ok"] = True
        return web.json_response(state)

    async def handle_clear(self, request: web.Request) -> web.Response:
        """删除缓存：``{"names": [...]}`` 删指定文件，``{"all": true}`` 清空全部。"""
        self._allow_local(request)
        try:
            payload = await request.json() if request.can_read_body else {}
            if payload is None:
                payload = {}
            if not isinstance(payload, dict):
                raise ValueError("请求格式不正确")

            names = payload.get("names")
            if isinstance(names, str):
                names = [names]
            if names is not None and not isinstance(names, (list, tuple)):
                raise ValueError("要删除的缓存文件名格式不正确")
            clear_all = bool(payload.get("all"))
            if clear_all:
                names = None
            elif not names:
                raise ValueError("请先选择要删除的缓存")

            result = await asyncio.to_thread(
                remove_cached_songs, self.plugin_config, names, ""
            )
        except (ValueError, TypeError, web.HTTPBadRequest) as error:
            return web.json_response({"ok": False, "error": str(error)}, status=400)

        state = cache_snapshot(self.plugin_config)
        self.logger.bind(tag=TAG).info(
            f"音乐缓存已清理 | mode={'all' if clear_all else 'selected'} "
            f"| removed={len(result['removed'])} | freed={result['bytes']} "
            f"| left={state['total']}"
        )
        state.update(
            {
                "ok": True,
                "removed": result["removed"],
                "failed": result["failed"],
                "freed_bytes": result["bytes"],
                "all": clear_all,
            }
        )
        return web.json_response(state)
