import asyncio
from aiohttp import web
from config.logger import setup_logging
from core.api.ota_handler import OTAHandler
from core.api.vision_handler import VisionHandler
from core.api.music_handler import MusicHandler
from core.api.music_cache_handler import MusicCacheHandler
from core.api.music_player_handler import MusicPlayerHandler
from core.api.local_apps_handler import LocalAppsHandler
from core.api.file_search_handler import FileSearchHandler
from core.api.desktop_save_handler import DesktopSaveHandler
from core.api.timer_handler import TimerHandler

TAG = __name__


class SimpleHttpServer:
    def __init__(self, config: dict, websocket_server=None):
        self.config = config
        self.logger = setup_logging()
        self.ota_handler = OTAHandler(config)
        self.vision_handler = VisionHandler(config)
        self.music_handler = MusicHandler(config)
        self.music_cache_handler = MusicCacheHandler(config, websocket_server)
        self.music_player_handler = MusicPlayerHandler(config, websocket_server)
        self.local_apps_handler = LocalAppsHandler(config, websocket_server)
        self.file_search_handler = FileSearchHandler(config, websocket_server)
        self.desktop_save_handler = DesktopSaveHandler(config, websocket_server)
        self.timer_handler = TimerHandler(config, websocket_server)

    def _get_websocket_url(self, local_ip: str, port: int) -> str:
        """获取websocket地址

        Args:
            local_ip: 本地IP地址
            port: 端口号

        Returns:
            str: websocket地址
        """
        server_config = self.config["server"]
        websocket_config = server_config.get("websocket")

        if websocket_config and "你" not in websocket_config:
            return websocket_config
        else:
            return f"ws://{local_ip}:{port}/xiaozhi/v1/"

    async def start(self):
        try:
            server_config = self.config["server"]
            read_config_from_api = self.config.get("read_config_from_api", False)
            host = server_config.get("ip", "0.0.0.0")
            port = int(server_config.get("http_port", 8003))

            if port:
                app = web.Application()

                if not read_config_from_api:
                    # 如果没有开启智控台，只是单模块运行，就需要再添加简单OTA接口，用于下发websocket接口
                    app.add_routes(
                        [
                            web.get("/xiaozhi/ota/", self.ota_handler.handle_get),
                            web.post("/xiaozhi/ota/", self.ota_handler.handle_post),
                            web.options(
                                "/xiaozhi/ota/", self.ota_handler.handle_options
                            ),
                            # 下载接口，仅提供 data/bin/*.bin 下载
                            web.get(
                                "/xiaozhi/ota/download/{filename}",
                                self.ota_handler.handle_download,
                            ),
                            web.options(
                                "/xiaozhi/ota/download/{filename}",
                                self.ota_handler.handle_options,
                            ),
                        ]
                    )
                # 添加路由
                app.add_routes(
                    [
                        web.get("/mcp/vision/explain", self.vision_handler.handle_get),
                        web.post(
                            "/mcp/vision/explain", self.vision_handler.handle_post
                        ),
                        web.options(
                            "/mcp/vision/explain", self.vision_handler.handle_options
                        ),
                        # 点播音乐文件下载（桌宠"背景音乐模式"用）
                        web.get("/music/{filename}", self.music_handler.handle_get),
                        # 桌宠「音乐缓存」查看 / 清理（仅回环地址）
                        web.get("/music-cache", self.music_cache_handler.handle_list),
                        web.post("/music-cache/clear", self.music_cache_handler.handle_clear),
                        # 服务端音乐播放（方案B：音乐从服务器本机扬声器播放，仅回环地址）
                        web.get("/music-player/status", self.music_player_handler.handle_status),
                        web.post("/music-player/play", self.music_player_handler.handle_play),
                        web.post("/music-player/stop", self.music_player_handler.handle_stop),
                        web.post("/music-player/volume", self.music_player_handler.handle_volume),
                        # 桌宠软件设置（处理器内部限制仅回环地址可访问）
                        web.get("/local-apps", self.local_apps_handler.handle_list),
                        web.post("/local-apps/scan", self.local_apps_handler.handle_scan),
                        web.post("/local-apps/manual", self.local_apps_handler.handle_add),
                        web.post("/local-apps/close", self.local_apps_handler.handle_set_close),
                        web.delete(
                            "/local-apps/manual/{app_id}",
                            self.local_apps_handler.handle_remove,
                        ),
                        # 桌宠「查找范围设置」（处理器内部限制仅回环地址可访问）
                        web.get("/file-search/settings", self.file_search_handler.handle_get),
                        web.post("/file-search/settings", self.file_search_handler.handle_save),
                        # 桌宠「默认保存位置」（处理器内部限制仅回环地址可访问）
                        web.get("/desktop-save/settings", self.desktop_save_handler.handle_get),
                        web.post("/desktop-save/settings", self.desktop_save_handler.handle_save),
                        # 桌宠「计时提醒」面板（处理器内部限制仅回环地址可访问）
                        web.get("/timers", self.timer_handler.handle_list),
                        web.post("/timers/cancel", self.timer_handler.handle_cancel),
                    ]
                )

                # 运行服务
                runner = web.AppRunner(app)
                await runner.setup()
                site = web.TCPSite(runner, host, port)
                await site.start()

                # 保持服务运行
                while True:
                    await asyncio.sleep(3600)  # 每隔 1 小时检查一次
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"HTTP服务器启动失败: {e}")
            import traceback

            self.logger.bind(tag=TAG).error(f"错误堆栈: {traceback.format_exc()}")
            raise
