"""桌宠「计时提醒」接口，仅允许回环地址访问。

桌宠屏幕上的提醒面板用它做两件事：
- ``GET /timers``           打开/刷新时拉取当前提醒清单
- ``POST /timers/cancel``   点面板上的「×」取消某一条

取消后由本处理器把新的清单推给所有支持该特性的连接（桌宠面板跟着更新），
所以点界面按钮和语音说「取消提醒」走的是同一份状态。
"""

import asyncio
import ipaddress
from typing import Any, Dict, List, Optional

from aiohttp import web

from config.logger import setup_logging
from plugins_func.functions.timer_reminder import (
    cancel,
    cancel_id,
    push_timer_list,
    public_timers_ms,
    timer_supported,
)

TAG = __name__


class TimerHandler:
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

    async def handle_list(self, request: web.Request) -> web.Response:
        self._allow_local(request)
        return web.json_response({"ok": True, "timers": public_timers_ms()})

    async def handle_cancel(self, request: web.Request) -> web.Response:
        self._allow_local(request)
        try:
            payload = await request.json()
        except (ValueError, TypeError):
            payload = None
        body = payload if isinstance(payload, dict) else {}

        removed, error = self._cancel_by_body(body)
        if error:
            return web.json_response({"ok": False, "error": error}, status=400)

        await self._push_all()
        self.logger.bind(tag=TAG).info(
            f"桌宠取消提醒 | 取消 {len(removed)} 个 | 剩余 {len(public_timers_ms())} 个"
        )
        return web.json_response(
            {
                "ok": True,
                "cancelled": [timer.id for timer in removed],
                "timers": public_timers_ms(),
            }
        )

    def _cancel_by_body(self, body: Dict[str, Any]):
        """按请求体取消：优先 id（面板点×），其次序号/内容，都没给就全取消。"""
        timer_id = str(body.get("id") or "").strip()
        if timer_id:
            timer = cancel_id(timer_id)
            if timer is None:
                return [], "这个提醒已经不在了。"
            return [timer], None
        return cancel(str(body.get("ref") or ""), all_=bool(body.get("all")))

    async def _push_all(self) -> None:
        """把最新清单推给所有支持 timer 特性的连接（桌宠面板跟着变）。"""
        connections = getattr(self.websocket_server, "active_connections", None) or set()
        tasks: List[Any] = [
            push_timer_list(conn) for conn in list(connections) if timer_supported(conn)
        ]
        if not tasks:
            return
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for error in results:
            if isinstance(error, BaseException):
                self.logger.bind(tag=TAG).warning(
                    f"推送提醒清单失败 | {type(error).__name__}: {error}"
                )
