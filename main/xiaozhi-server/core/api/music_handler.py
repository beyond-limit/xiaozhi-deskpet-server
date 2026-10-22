"""点播音乐文件下载接口：给桌宠客户端的"背景音乐模式"提供音频文件。

背景音乐模式（blueprint）：
- 歌曲不再走 TTS 音频流（那会驱动 Live2D 口型、且被后续对话打断），
  而是由客户端用独立播放器播放在本接口提供的文件；
- 服务端只发一条 `{"type": "bgm", "action": "play", "url": ...}` 指令。

安全约束：
- 只允许 `plugins.online_music` 配置的曲库目录（music_dirs）与在线缓存目录
  下的文件；文件名取 basename，杜绝路径穿越；
- 只允许音频扩展名。
"""

import os

from aiohttp import web

from config.logger import setup_logging
from plugins_func.functions.online_music import (
    AUDIO_EXTS,
    _cache_dir,
    _music_dirs,
)

TAG = __name__


class MusicHandler:
    def __init__(self, config: dict):
        self.logger = setup_logging()
        plugin_config = (config.get("plugins") or {}).get("online_music") or {}

        roots = []
        for directory in _music_dirs(plugin_config):
            if directory and directory not in roots:
                roots.append(directory)
        cache_dir = _cache_dir(plugin_config)
        if cache_dir and cache_dir not in roots:
            roots.append(cache_dir)
        self.roots = roots
        self.logger.bind(tag=TAG).debug(f"音乐文件服务目录: {self.roots}")

    async def handle_get(self, request: web.Request):
        filename = os.path.basename(request.match_info.get("filename", ""))
        if not filename or not filename.lower().endswith(AUDIO_EXTS):
            return web.Response(status=404, text="not found")

        for root in self.roots:
            path = os.path.join(root, filename)
            if os.path.isfile(path):
                return web.FileResponse(path)

        self.logger.bind(tag=TAG).debug(f"点播文件不存在: {filename}")
        return web.Response(status=404, text="not found")
