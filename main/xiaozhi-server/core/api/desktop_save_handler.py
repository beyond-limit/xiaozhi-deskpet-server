"""桌宠「默认保存位置」接口，仅允许回环地址访问。

与 `file_search_handler` / `local_apps_handler` 同一套约定：
设置窗口（Electron）通过 127.0.0.1 调用，保存后**立即生效**——``desktop_save``
每次落盘都会重新读 ``data/desktop_save.json``，不需要重启服务端。
"""

import asyncio
import ipaddress
from typing import Any, Dict

from aiohttp import web

from config.logger import setup_logging
from plugins_func.functions.desktop_save import describe_settings, save_settings

TAG = __name__


class DesktopSaveHandler:
    def __init__(self, config: Dict[str, Any], websocket_server=None):
        self.config = config
        self.websocket_server = websocket_server
        self.logger = setup_logging()

    def _allow_local(self, request: web.Request) -> None:
        remote = request.remote or ""
        try:
            if not ipaddress.ip_address(remote).is_loopback:
                raise web.HTTPForbidden(text="local access only")
        except ValueError as error:
            raise web.HTTPForbidden(text="local access only") from error

    async def handle_get(self, request: web.Request) -> web.Response:
        self._allow_local(request)
        state = await asyncio.to_thread(describe_settings)
        return web.json_response(state)

    async def handle_save(self, request: web.Request) -> web.Response:
        self._allow_local(request)
        try:
            payload = await request.json()
        except (ValueError, TypeError):
            return web.json_response(
                {"ok": False, "error": "请求内容不是有效 JSON"}, status=400
            )
        try:
            state = await asyncio.to_thread(save_settings, payload)
        except ValueError as error:
            return web.json_response({"ok": False, "error": str(error)}, status=400)

        state["saved"] = True
        self.logger.bind(tag=TAG).info(
            f"默认保存位置已更新 | 目录={state.get('effective_dir')}"
        )
        return web.json_response(state)
