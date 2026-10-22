"""桌宠本机软件设置接口，仅允许回环地址访问。"""

import asyncio
import ipaddress
from typing import Any, Dict

from aiohttp import web

from config.logger import setup_logging
from plugins_func.functions.local_app_catalog import (
    add_manual_app,
    get_catalog_state,
    refresh_connections,
    remove_manual_app,
    replace_scanned_apps,
    scan_installed_apps,
    set_apps_close,
)

TAG = __name__


class LocalAppsHandler:
    def __init__(self, config: Dict[str, Any], websocket_server=None):
        self.config = config
        self.websocket_server = websocket_server
        self.logger = setup_logging()

    def _local_config(self) -> Dict[str, Any]:
        return (self.config.get("plugins", {}).get("local_app", {}) or {})

    def _allow_local(self, request: web.Request) -> None:
        remote = request.remote or ""
        try:
            if not ipaddress.ip_address(remote).is_loopback:
                raise web.HTTPForbidden(text="local access only")
        except ValueError as error:
            raise web.HTTPForbidden(text="local access only") from error

    def _refresh(self) -> int:
        connections = getattr(self.websocket_server, "active_connections", set())
        return refresh_connections(connections)

    async def handle_list(self, request: web.Request) -> web.Response:
        self._allow_local(request)
        return web.json_response(get_catalog_state(self._local_config()))

    async def handle_scan(self, request: web.Request) -> web.Response:
        self._allow_local(request)
        scanned = await asyncio.to_thread(scan_installed_apps)
        await asyncio.to_thread(replace_scanned_apps, scanned)
        refreshed = self._refresh()
        state = get_catalog_state(self._local_config())
        state.update({"ok": True, "scan_count": len(scanned), "refreshed_connections": refreshed})
        self.logger.bind(tag=TAG).info(
            f"本机软件重新扫描完成 | scanned={len(scanned)} | total={state['total']} | connections={refreshed}"
        )
        return web.json_response(state)

    async def handle_add(self, request: web.Request) -> web.Response:
        self._allow_local(request)
        try:
            payload = await request.json()
            app = await asyncio.to_thread(add_manual_app, payload)
        except (ValueError, TypeError, web.HTTPBadRequest) as error:
            return web.json_response({"ok": False, "error": str(error)}, status=400)
        refreshed = self._refresh()
        state = get_catalog_state(self._local_config())
        state.update({"ok": True, "app": app, "refreshed_connections": refreshed})
        return web.json_response(state)

    async def handle_set_close(self, request: web.Request) -> web.Response:
        """批量设置「是否允许语音关闭」（列表里的单行开关也走这个接口）。"""
        self._allow_local(request)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("请求格式不正确")
            app_ids = payload.get("ids")
            if isinstance(app_ids, str):
                app_ids = [app_ids]
            if not isinstance(app_ids, (list, tuple)):
                raise ValueError("请先选择要设置的软件")
            allow = bool(payload.get("allow"))
            result = await asyncio.to_thread(
                set_apps_close, app_ids, allow, self._local_config()
            )
        except (ValueError, TypeError, web.HTTPBadRequest) as error:
            return web.json_response({"ok": False, "error": str(error)}, status=400)
        refreshed = self._refresh()
        state = get_catalog_state(self._local_config())
        state.update(
            {
                "ok": True,
                "updated": result["updated"],
                "updated_names": result["names"],
                "unsupported": result["unsupported"],
                "missing": result["missing"],
                "allow": result["allow"],
                "refreshed_connections": refreshed,
            }
        )
        self.logger.bind(tag=TAG).info(
            f"语音关闭权限已更新 | allow={result['allow']} | updated={result['updated']} "
            f"| unsupported={result['unsupported']} | missing={len(result['missing'])}"
        )
        return web.json_response(state)

    async def handle_remove(self, request: web.Request) -> web.Response:
        self._allow_local(request)
        app_id = request.match_info.get("app_id", "")
        removed = await asyncio.to_thread(remove_manual_app, app_id)
        if not removed:
            return web.json_response({"ok": False, "error": "没有找到这条手动软件记录"}, status=404)
        refreshed = self._refresh()
        state = get_catalog_state(self._local_config())
        state.update({"ok": True, "refreshed_connections": refreshed})
        return web.json_response(state)
