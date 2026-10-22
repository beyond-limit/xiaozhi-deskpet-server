"""在线点歌插件（play_song）。

用途
----
让桌宠能播放"用户真正想听的歌"，不再局限于服务端 `music/` 目录里预置的
那几首（上游 `play_music` 找不到指定歌曲时会"随机播一首"，用户会误以为
"只会播固定的几首歌"）。本插件提供两类能力，按顺序尝试：

1. **本地曲库**：扫描 `plugins.online_music.music_dirs` 配置的目录，
   按歌名 /「歌手 - 歌名」做模糊匹配（归一化 + 包含关系 + difflib 兜底）。
2. **在线点歌**：本地没有时联网找歌，分两阶段：
   - 阶段 1：并发搜索**网易云 + 咪咕**（快，直链/接口取流）；
   - 阶段 2：两者都拿不到时，用 **B 站**现场版/搬运兜底（web 搜索 + yt-dlp 取音频），
     专治"版权独家"的歌（如林俊杰《黑夜问白天》）。
   候选按歌名/歌手/版本噪音打分排序、过滤，逐个尝试取流，下载到缓存目录；
   下一次点同一首歌直接命中缓存，不再联网。

设计要点
--------
- **绝不乱播 / 不播错人**：本地与在线都找不到时，明确回一句"没找到"，
  不回落随机播放；且一旦查询里带了歌手名（"林俊杰的黑夜问白天"），
  就只尝试这位歌手的版本——拿不到就如实说明，绝不拿别人翻唱的糊弄用户。
  B 站没有歌手字段，靠"其他音源确认的歌手名"反查标题（标题里带了这位歌手
  才放行），保证兜底源也不会播错人。
- **三音源互补**：网易云直链命中率高但版权受限的歌会 302 到 HTML；咪咕
  曲库偏正版（实测林俊杰《达尔文》可下载）；B 站有大量现场版/搬运，能覆盖
  前两者都拿不到的歌。两快源并发搜索、合并打分，同分时网易云优先。
- **缓存复用**：在线歌曲落盘到 `cache_dir`，文件名规整为「歌手 - 歌名.扩展名」，
  后续点歌优先在缓存里模糊匹配。
- **best-effort**：版权受限的歌（网易云 302 到 HTML、咪咕回"暂不提供试听地址"）
  靠"候选逐个尝试 + 内容校验（Content-Type / 音频魔数）"兜底，全部失败才回退话术。
- **不阻塞事件循环**：搜索、下载、目录扫描均为阻塞 IO，统一放进
  `asyncio.to_thread`；推送 TTS 队列在事件循环线程里做（与上游一致）。

配置（data/.config.yaml 的 plugins 段）
--------------------------------------
    online_music:
      music_dirs:                # 本地曲库目录（可多个）
        - ./music                # 相对路径基于服务端工作目录
        - "%USERPROFILE%/Music"  # 也可以挂自己电脑上的音乐目录
      online: true               # 本地没有时是否联网点歌
      cache_dir: ./music/online  # 在线歌曲缓存目录（自动创建）
      search_limit: 10           # 每个音源的搜索候选数
      max_candidates: 5          # 最多尝试取流的候选数（覆盖多个音源）
      timeout: 8                 # 单次 HTTP 超时（秒）
      max_file_mb: 30            # 单曲下载大小上限
      bilibili: true             # B 站兜底源（需 yt-dlp）
      bilibili_limit: 6          # B 站搜索取前几个视频

依赖：B 站音源需要 `pip install yt-dlp`（纯 Python 包，无需 ffmpeg 转码——
下载的 m4a/webm 由播放链路直接解码）。未安装时该源自动跳过，不影响前两源。
"""

import asyncio
import difflib
import glob
import json
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

from config.logger import setup_logging
from core.providers.tts.dto.dto import ContentType, SentenceType, TTSMessageDTO
from core.utils.util import get_local_ip
from plugins_func.register import Action, ActionResponse, ToolType, register_function

# B 站会话（带 Cookie 预热，模块级复用；失效时置 None 让其重建）
_BILI_SESSION: Optional[requests.Session] = None

# 服务端播放的"延迟起播"任务（保存引用，避免被 GC 提前回收）
_BACKGROUND_TASKS: set[asyncio.Task] = set()

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".p3")

DEFAULT_MUSIC_DIR = "./music"
DEFAULT_CACHE_DIR = "./music/online"
DEFAULT_SEARCH_LIMIT = 10
DEFAULT_MAX_CANDIDATES = 5
DEFAULT_TIMEOUT = 8
DEFAULT_MAX_FILE_MB = 30

# 小于该大小的文件视为下载失败（错误页 / 试听片段）
MIN_VALID_FILE_BYTES = 100 * 1024

# 判定"与用户点的歌相关"的最低歌名相似度（0~100）。
# 低于该值且歌手也没命中的候选一律丢弃——否则胡编的歌名也会被网易云的
# 模糊搜索配上毫不相关的歌曲（实测："紫色小马会跳舞xyzzy" 曾匹配到
# 相似度只有 3 分的无关歌曲）。
MATCH_THRESHOLD = 45.0

# 本地曲库扫描缓存：{目录: (扫描时间, [文件路径])}，避免每次点歌全盘扫描
_DIR_CACHE: Dict[str, Tuple[float, List[str]]] = {}
DIR_CACHE_TTL = 60

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# ── 在线源 ──────────────────────────────────────────────────────────────────
# 网易云：免 Key 搜索 + outer 直链。命中率高但版权受限的歌会 302 到 HTML。
# 咪咕：免 Key 搜索 + listenSong.do 取流。曲库偏正版，能补上网易云部分拿不到的歌
#       （实测：林俊杰《达尔文》在咪咕可下载；《黑夜问白天》两源都受版权限制）。
# B 站：web 搜索 + yt-dlp 取音频（现场版 / 搬运）。前两源拿不到时才启用，
#       作为"版权独家歌"的最后兜底（实测：林俊杰《黑夜问白天》有多个现场版）。
# 两个快源（网易云/咪咕）并发搜索、合并打分，同分时按 _SOURCE_ORDER 优先。
NETEASE_SEARCH_API = "https://music.163.com/api/search/get/web"
NETEASE_OUTER_URL = "https://music.163.com/song/media/outer/url?id={song_id}.mp3"
# 热歌榜（"随便放首歌"用）：免 Key 的公开歌单接口，返回 200 首带歌手名的曲目
NETEASE_TOPLIST_API = "https://music.163.com/api/playlist/detail"
NETEASE_HOT_LIST_ID = 3778678  # 网易云「热歌榜」
HOT_RANDOM_POOL = 50  # 只在前 50 里随机：再往后的长尾容易搜不到/受版权限制

MIGU_SEARCH_API = "https://app.c.nf.migu.cn/MIGUM2.0/v1.0/content/search_all.do"
MIGU_LISTEN_API = "https://app.c.nf.migu.cn/MIGUM2.0/v1.0/content/sub/listenSong.do"
MIGU_UA = (
    "Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/90.0 Mobile Safari/537.36"
)
MIGU_TONES = ("LQ", "HQ")  # 依次尝试的音质档位

BILIBILI_SEARCH_API = "https://api.bilibili.com/x/web-interface/search/type"
BILIBILI_VIDEO_URL = "https://www.bilibili.com/video/{bvid}"
DEFAULT_BILIBILI_LIMIT = 6

# 同分候选的源优先级（数字越小越先尝试）
_SOURCE_ORDER = {"netease": 0, "migu": 1, "bilibili": 2}

# 版本噪音词：这些版本一般不是用户想要的原曲（除非用户点名要这些版本）
NOISE_WORDS = (
    "伴奏", "纯音乐", "翻唱", "cover", "remix", "dj", "铃声", "片段",
    "钢琴", "吉他", "古筝", "八音盒", "电音", "慢速", "加速", "降调", "升调",
    "sped", "slowed", "8d", "3d", "环绕", "合唱", "群星",
    "现场", "live", "演唱会", "音乐节", "致敬", "模仿",
    "教学", "教程", "指弹", "民乐", "萨克斯", "小提琴", "口琴", "排箫",
    "哄睡", "助眠", "纯享", "串烧", "合辑", "集锦", "男声", "女声",
    # B 站视频标题里常见的非歌曲内容（鼓谱/音高测量/解说等）
    "鼓谱", "乐谱", "琴谱", "音高", "混剪", "解说", "reaction", "鬼畜", "弹唱", "循环",
)

# 点歌文本里的口语前缀，只会从开头剥掉（歌名本身含这些字时不误删）
_QUERY_PREFIX_NOISE = (
    "播放", "来一首", "来首", "放一首", "放首", "点一首", "点首",
    "我想听", "我要听", "想听", "给我放", "给我来", "帮我放", "放一下", "听一下",
)

# 「随便放首歌」类请求：命中则随机播本地曲库
_RANDOM_EXACT = (
    "", "random", "any", "音乐", "歌曲", "歌", "首歌", "点歌", "听歌", "来歌", "放歌",
)
_RANDOM_HINTS = (
    "随便", "随机", "任意",
    "来点音乐", "放点音乐", "放点歌",
    "来首歌", "来一首歌", "放首歌", "放一首歌", "点首歌", "点一首歌",
)

_PUNCT_RE = re.compile(r"[\s\-—_·、,.，。!！?？:：;；'\"“”‘’()（）\[\]【】<>《》/\\|+&~`@#$%^*=]+")
_ILLEGAL_FILENAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')

PLAY_SONG_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "play_song",
        "description": (
            "播放音乐、歌曲、点歌。当用户说“播放/我想听/来一首某首歌”“放首歌”时调用。"
            "song_name 填用户要听的歌名（可以带歌手，如“周杰伦的稻香”）；"
            "用户没指定歌名、只是说“随便放首歌/来点音乐/来首歌”时填 random——"
            "服务端会先取当前热歌榜、再随机挑一首播，不要自己编一个歌名填进来。"
            "本工具会先在本地曲库查找，本地没有会自动联网搜索并下载，"
            "不需要用户手动放文件，也不要回答“只能播放固定的歌”。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "song_name": {
                    "type": "string",
                    "description": (
                        "歌曲名称，可带歌手名，如“稻香”“周杰伦的稻香”“Jay Chou Shape of You”；"
                        "用户没有指定具体歌名时填 random（会从当前热歌榜随机挑一首）。"
                    ),
                }
            },
            "required": ["song_name"],
        },
    },
}


# ── 配置解析 ─────────────────────────────────────────────────────────────────
def _plugin_config(conn: "ConnectionHandler") -> Dict[str, Any]:
    config = conn.config.get("plugins", {}) or {}
    return config.get("online_music", {}) or {}


def _expand_path(raw: str) -> str:
    """展开 %ENV% / ~ 并把相对路径解析为基于服务端工作目录的绝对路径。"""
    value = os.path.expandvars(os.path.expanduser(str(raw or "").strip()))
    if value and not os.path.isabs(value):
        value = os.path.abspath(value)
    return value


def _music_dirs(config: Dict[str, Any]) -> List[str]:
    raw = config.get("music_dirs")
    if isinstance(raw, str):
        raw = [raw]
    dirs = [_expand_path(item) for item in (raw or []) if str(item or "").strip()]
    if not dirs:
        dirs = [_expand_path(DEFAULT_MUSIC_DIR)]
    return dirs


def _cache_dir(config: Dict[str, Any]) -> str:
    raw = config.get("cache_dir") or DEFAULT_CACHE_DIR
    return _expand_path(raw)


def _online_enabled(config: Dict[str, Any]) -> bool:
    return bool(config.get("online", True))


def _hot_random_enabled(config: Dict[str, Any]) -> bool:
    """“随便放首歌”是否走热歌榜（关掉则退回上一版：随机播本地曲库）。"""
    return bool(config.get("hot_random", True))


def _bilibili_enabled(config: Dict[str, Any]) -> bool:
    """B 站兜底源开关（需要 yt-dlp；默认开启，前两源拿不到时才用）。"""
    return bool(config.get("bilibili", True))


def _bgm_enabled(config: Dict[str, Any]) -> bool:
    """背景音乐模式开关（客户端声明支持 features.bgm 时生效）。

    开启后歌曲由客户端用独立播放器播放（服务端只下发文件 URL）：
    音乐不驱动口型、也不会被后续对话打断；关掉则回退到走 TTS 音频流
    （与 ESP32 等设备一致）。
    """
    return bool(config.get("background", True))


def _bgm_supported(conn: "ConnectionHandler") -> bool:
    return bool((getattr(conn, "features", None) or {}).get("bgm"))


def _bgm_volume(config: Dict[str, Any]) -> float:
    try:
        value = float(config.get("background_volume", 0.35))
    except (TypeError, ValueError):
        value = 0.35
    return min(max(value, 0.05), 1.0)


def _playback_mode(config: Dict[str, Any]) -> str:
    """播放位置：auto / client / device / server。

    - ``auto``（默认）：客户端声明 ``features.bgm`` 就走客户端播放，否则走服务端播放；
    - ``client``：强制客户端 ``<audio>``（桌宠）；
    - ``device``：强制走 TTS 音频流（上游老行为，ESP32 原生支持）；
    - ``server``：强制服务端播放进程（音乐从服务器本机扬声器出声）。
    """
    value = str(config.get("playback", "auto") or "auto").strip().lower()
    return value if value in ("auto", "client", "device", "server") else "auto"


def _server_volume(config: Dict[str, Any]) -> float:
    try:
        value = float(config.get("server_volume", 0.35))
    except (TypeError, ValueError):
        value = 0.35
    return min(max(value, 0.0), 1.0)


def _server_fade(config: Dict[str, Any]) -> float:
    try:
        return max(0.0, float(config.get("server_fade_in", 0.8)))
    except (TypeError, ValueError):
        return 0.8


def _server_delay(config: Dict[str, Any]) -> float:
    """先让 TTS 播报“正在为您播放”，延迟一点再起音乐，避免盖住播报。"""
    try:
        return max(0.0, float(config.get("server_start_delay", 1.0)))
    except (TypeError, ValueError):
        return 1.0


def _music_url(conn: "ConnectionHandler", path: str) -> str:
    """把曲库里的音频文件转成客户端可访问的 HTTP URL（/music/{filename}）。"""
    server = conn.config.get("server") or {}
    try:
        port = int(server.get("http_port") or 8003)
    except (TypeError, ValueError):
        port = 8003
    host = str(server.get("ip") or "").strip()
    if not host or host in ("0.0.0.0", "::"):
        host = get_local_ip() or "127.0.0.1"
    return f"http://{host}:{port}/music/{quote(os.path.basename(path))}"


def _int_config(config: Dict[str, Any], key: str, default: int) -> int:
    try:
        value = int(config.get(key, default))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


# ── 文本归一化与匹配 ─────────────────────────────────────────────────────────
def _norm(text: Any) -> str:
    """归一化：小写、去空白与标点，便于歌名匹配。"""
    return _PUNCT_RE.sub("", str(text or "").strip().lower())


def _clean_query(raw: str) -> str:
    """剥掉口语前缀（“播放”“来一首”…）与书名号，保留歌名本身。

    清洗后可能为空（例如只说“播放”），调用方需自行兜底。
    """
    value = str(raw or "").strip().strip("《》\"'“”‘’ ")
    changed = True
    while changed and len(value) > 1:
        changed = False
        for word in _QUERY_PREFIX_NOISE:
            if value.startswith(word):
                value = value[len(word):].strip("《》\"'“”‘’ ,，。")
                changed = True
    for tail in ("这首歌", "这首", "吧", "谢谢"):
        if value.endswith(tail) and len(value) > len(tail):
            value = value[: -len(tail)].strip("《》\"'“”‘’ ,，。")
    return value.strip()


def _query_tokens(query: str) -> List[str]:
    """把点歌文本拆成可用于匹配的片段（第一个是完整串，权重最高）。

    - 「稻香 周杰伦」这类"歌名 歌手"文本：按空格拆，歌手/歌名各自成 token；
    - 「周杰伦的稻香」这类"歌手+的+歌名"文本：取"的"后面的尾段，
      让「稻香」能命中只写歌名的文件/歌曲。
    """
    cleaned_raw = _clean_query(query)
    cleaned = _norm(cleaned_raw)
    tokens = [cleaned] if cleaned else []
    parts = [p for p in re.split(r"\s+", cleaned_raw) if p.strip()]
    if len(parts) > 1:
        tokens.extend(_norm(p) for p in parts)
    else:
        match = re.match(r"^.{1,12}的(.{2,})$", cleaned)
        if match and match.group(1) != cleaned:
            tokens.append(match.group(1))

    unique: List[str] = []
    for token in tokens:
        if token and token not in unique:
            unique.append(token)
    return unique


def _is_random_request(query: str) -> bool:
    """判断是否"随便放首歌"（未指定具体曲目），此时随机播本地曲库。"""
    raw_norm = _norm(query)
    if not raw_norm or raw_norm in ("random", "any"):
        return True
    cleaned = _norm(_clean_query(query))
    for text in (cleaned, raw_norm):
        if not text or text in _RANDOM_EXACT:
            return True
        if any(hint in text for hint in _RANDOM_HINTS):
            return True
    return False


def _name_score(token: str, name: str) -> float:
    """单个片段与候选歌名的相似度（0 ~ 100）。

    包含关系要区分方向：
    - 候选名包含查询（token in name，如查询「稻香」候选「稻香(治愈版)」）→ 正常高分；
    - 查询包含候选名（name in token）→ 候选名只占查询的一小段时是"假匹配"，
      按长度比例打折（实测：咪咕对无结果查询会宽松返回《跳舞》，
      「紫色小马会跳舞」曾被它骗到 74 分而误播）。
    """
    if not token or not name:
        return 0.0
    if name == token:
        return 100.0
    if token in name:
        return 60.0 + 40.0 * (len(token) / len(name))
    if name in token:
        ratio = len(name) / len(token)
        if ratio < 0.35:
            return 40.0 * ratio  # 弱相似，必然低于 MATCH_THRESHOLD
        return 60.0 + 40.0 * ratio
    return difflib.SequenceMatcher(None, token, name).ratio() * 55.0


def _match_local(query: str, files: List[str]) -> Optional[str]:
    """在本地文件列表里找最匹配 query 的一首，低于阈值视为没找到。

    与在线逻辑一致：查询里带了歌手名（如「林俊杰的黑夜问白天」）时，
    文件名里歌手对不上的版本一律跳过——否则缓存里的翻唱版会抢先命中，
    用户点"林俊杰"却听到别人的版本（实测踩过）。
    """
    tokens = _query_tokens(query)
    if not tokens:
        return None

    best: Optional[str] = None
    best_score = 0.0
    for path in files:
        stem = _norm(Path(path).stem)
        file_artist, file_title = _split_stem(Path(path).stem)
        title = _norm(file_title) or stem
        artist_parts = [
            part
            for part in (_norm(item) for item in re.split(r"[、/,&;；]+", file_artist))
            if len(part) >= 2
        ]

        # 歌名匹配：取与歌名部分最相似的 token
        name_token, name_score = "", 0.0
        for idx, token in enumerate(tokens):
            value = _name_score(token, title)
            if idx == 0 and value >= 60:
                value += 8.0  # 完整查询命中优先
            if value > name_score:
                name_token, name_score = token, value

        # 查询里其余的 token：要么出现在文件名里，要么与文件名歌手对得上；
        # 否则视为"点名了别的歌手"，跳过这个文件（交给在线去找）
        skip = False
        for token in tokens:
            if token == name_token or len(token) < 2:
                continue
            if token in stem:
                continue
            if any(
                token == part or token.startswith(part) or token.endswith(part)
                for part in artist_parts
            ):
                continue
            skip = True
            break
        if skip:
            continue

        if name_score > best_score:
            best, best_score = path, name_score
    return best if best_score >= 60 else None


# ── 本地文件扫描 ─────────────────────────────────────────────────────────────
def _list_audio_files(dirs: List[str]) -> List[str]:
    """扫描目录下所有音频文件（带 60s 缓存，避免每次点歌全盘扫描）。"""
    now = time.time()
    files: List[str] = []
    for raw_dir in dirs:
        if not raw_dir:
            continue
        directory = Path(raw_dir)
        if not directory.is_dir():
            continue
        cached = _DIR_CACHE.get(str(directory))
        if cached and now - cached[0] < DIR_CACHE_TTL:
            files.extend(cached[1])
            continue
        try:
            found = [
                str(p)
                for p in directory.rglob("*")
                if p.suffix.lower() in AUDIO_EXTS and p.is_file()
            ]
        except OSError as error:
            logger.bind(tag=TAG).warning(f"扫描音乐目录失败 | dir={directory} | {error}")
            found = []
        _DIR_CACHE[str(directory)] = (now, found)
        files.extend(found)
    return files


# ── 缓存管理（设置窗口 / 语音"清理音乐缓存"都用这一套）──────────────────────
def _cache_files(cache_dir: str) -> List[Dict[str, Any]]:
    """列出缓存目录里的音频文件（名字 / 字节数 / 修改时间），新的在前。"""
    directory = Path(cache_dir)
    if not directory.is_dir():
        return []
    items: List[Dict[str, Any]] = []
    for path in directory.iterdir():
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTS:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append(
            {"name": path.name, "size": stat.st_size, "mtime": int(stat.st_mtime)}
        )
    items.sort(key=lambda item: (-item["mtime"], item["name"]))
    return items


def cache_snapshot(config: Dict[str, Any]) -> Dict[str, Any]:
    """缓存现状：目录 / 是否已创建 / 文件清单 / 总首数 / 总字节。

    `scripts` 那侧（HTTP 接口）与设置窗口都读这一份，避免两处口径不一致。
    """
    cache_dir = _cache_dir(config)
    files = _cache_files(cache_dir)
    return {
        "dir": cache_dir,
        "exists": os.path.isdir(cache_dir),
        "files": files,
        "total": len(files),
        "bytes": sum(item["size"] for item in files),
    }


def _cache_target(cache_dir: str, name: Any) -> Optional[str]:
    """把前端/模型给的文件名收敛成缓存目录里的真实文件（拒绝路径穿越）。"""
    raw = str(name or "").strip()
    base = os.path.basename(raw)
    if not base or base != raw:
        return None
    if Path(base).suffix.lower() not in AUDIO_EXTS:
        return None
    path = os.path.join(cache_dir, base)
    return path if os.path.isfile(path) else None


def remove_cached_songs(
    config: Dict[str, Any],
    names: Optional[List[Any]] = None,
    keyword: str = "",
) -> Dict[str, Any]:
    """删除音乐缓存。

    - `names` 非空：只删这些文件（接口/列表勾选用）；
    - `names` 为空且 `keyword` 非空：删文件名里含该词的缓存（"把月光删了"）；
    - 两者都为空：清空缓存目录里的全部音频。
    返回 ``{"removed": [...], "failed": [...], "bytes": 释放字节数}``。
    """
    cache_dir = _cache_dir(config)
    targets: List[str] = []

    if names:
        for name in names:
            path = _cache_target(cache_dir, name)
            if path:
                targets.append(path)
    else:
        files = _cache_files(cache_dir)
        if keyword:
            key = _norm(keyword)
            files = [item for item in files if key and key in _norm(item["name"])]
        targets = [os.path.join(cache_dir, item["name"]) for item in files]

    removed: List[str] = []
    failed: List[str] = []
    freed = 0
    for path in targets:
        try:
            size = os.path.getsize(path)
            os.remove(path)
        except OSError as error:
            logger.bind(tag=TAG).warning(f"删除缓存失败 | file={path} | {error}")
            failed.append(os.path.basename(path))
            continue
        removed.append(os.path.basename(path))
        freed += size

    if removed:
        # 目录内容变了，清掉 60s 扫描缓存，免得刚删掉又被"匹配"到
        _DIR_CACHE.pop(str(Path(cache_dir)), None)

    logger.bind(tag=TAG).info(
        f"清理音乐缓存 | dir={cache_dir} | keyword={keyword!r} | "
        f"removed={len(removed)} | failed={len(failed)} | freed={freed}"
    )
    return {"removed": removed, "failed": failed, "bytes": freed}


# ── 搜索（网易云 + 咪咕并发）与打分 ─────────────────────────────────────────
def _artists_text(song: Dict[str, Any]) -> str:
    artists = song.get("artists") or song.get("ar") or []
    names = []
    for artist in artists:
        if isinstance(artist, dict) and artist.get("name"):
            names.append(str(artist["name"]))
        elif isinstance(artist, str) and artist.strip():
            names.append(artist.strip())
    return "、".join(names)


def _song_display(song: Dict[str, Any]) -> str:
    name = str(song.get("name") or "").strip() or "未知歌曲"
    artists = _artists_text(song)
    # 标题里已带歌手名（B 站视频标题常见）时不重复，避免"林俊杰 - …林俊杰…"
    if artists and _norm(artists) in _norm(name):
        return name
    return f"{artists} - {name}" if artists else name


def _song_label(song: Dict[str, Any]) -> str:
    """缓存文件名 / 播报用的简洁名：「歌手 - 歌名」。

    优先用 title_hint（查询里对应的歌名）——B 站候选的 name 是视频标题，
    直接用它做文件名/播报会念出"在百万豪装录音棚大声听 林俊杰《…》"这种全名，
    实测用户明确不喜欢。
    """
    title = (
        str(song.get("title_hint") or "").strip()
        or str(song.get("name") or "").strip()
        or "未知歌曲"
    )
    artists = _artists_text(song)
    if artists and _norm(artists) not in _norm(title):
        return f"{artists} - {title}"
    return title


def _search_netease(query: str, limit: int, timeout: int) -> List[Dict[str, Any]]:
    """网易云公开搜索接口（免 Key，best-effort）。"""
    session = requests.Session()
    try:
        resp = session.get(
            NETEASE_SEARCH_API,
            params={
                "csrf_token": "",
                "s": query,
                "type": 1,
                "offset": 0,
                "limit": limit,
            },
            headers={"User-Agent": UA, "Referer": "https://music.163.com/"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    finally:
        session.close()

    songs = (data.get("result") or {}).get("songs") or []
    return [song for song in songs if isinstance(song, dict) and song.get("id")]


def _fetch_hot_songs(limit: int = HOT_RANDOM_POOL, timeout: int = 6) -> List[str]:
    """拉当前热歌榜，返回「歌手 歌名」形式的点歌词（best-effort，失败返回空表）。

    给"随便放首歌"用：先知道现在什么歌火，再随机挑一首，而不是在本地曲库里
    反复放那几首。搜索接口对「歌手 歌名」的命中率比「歌名 - 歌手」高，所以按前者拼。
    """
    session = requests.Session()
    try:
        resp = session.get(
            NETEASE_TOPLIST_API,
            params={"id": NETEASE_HOT_LIST_ID},
            headers={"User-Agent": UA, "Referer": "https://music.163.com/"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as error:
        logger.bind(tag=TAG).warning(f"拉热歌榜失败 | {type(error).__name__}: {error}")
        return []
    finally:
        session.close()

    tracks = (data.get("result") or {}).get("tracks") or []
    songs: List[str] = []
    for track in tracks[: max(1, limit)]:
        if not isinstance(track, dict):
            continue
        title = str(track.get("name") or "").strip()
        if not title:
            continue
        artists = "/".join(
            str(artist.get("name") or "").strip()
            for artist in (track.get("artists") or [])
            if isinstance(artist, dict) and artist.get("name")
        )
        songs.append(f"{artists} {title}".strip())
    return songs


def _hot_random_query(limit: int = HOT_RANDOM_POOL) -> str:
    """热歌榜里随机挑一首，返回「歌手 歌名」；榜单拉不到时返回空串。"""
    songs = _fetch_hot_songs(limit)
    if not songs:
        return ""
    return random.choice(songs)


def _search_migu(query: str, limit: int, timeout: int) -> List[Dict[str, Any]]:
    """咪咕音乐公开搜索接口（免 Key，best-effort）。

    只保留能在咪咕取到流所必需的字段（copyrightId / contentId），
    并把结果归一化成与网易云相同的形状，方便共用打分逻辑。
    """
    session = requests.Session()
    try:
        resp = session.get(
            MIGU_SEARCH_API,
            params={
                "text": query,
                "pageNo": 1,
                "pageSize": limit,
                "isCorrect": 1,
                "isCopyright": 1,
                "sort": 1,
                "searchSwitch": '{"song":1}',
            },
            headers={"User-Agent": MIGU_UA, "Referer": "https://m.music.migu.cn/"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    finally:
        session.close()

    songs = (data.get("songResultData") or {}).get("result") or []
    normalized = []
    for song in songs:
        if not isinstance(song, dict) or not song.get("copyrightId"):
            continue
        normalized.append(
            {
                "source": "migu",
                "id": song.get("id") or song.get("copyrightId"),
                "name": song.get("name") or song.get("songName") or "",
                "artists": [
                    {"name": item.get("name", "")}
                    for item in (song.get("singers") or [])
                    if isinstance(item, dict)
                ],
                "duration": song.get("duration") or 0,
                "copyrightId": song.get("copyrightId"),
                "contentId": song.get("contentId"),
                "raw": song,
            }
        )
    return normalized


def _strip_html(text: Any) -> str:
    """B 站搜索结果的标题带 <em> 高亮标签，去掉后再用。"""
    value = re.sub(r"<[^>]+>", "", str(text or ""))
    return value.replace("&amp;", "&").replace("&quot;", '"').strip()


def _parse_bili_duration(value: Any) -> int:
    """B 站搜索返回的时长是 "04:52" / "1:02:33" 这类字符串，统一转成秒。"""
    if isinstance(value, (int, float)):
        return int(value)
    parts = [int(part) for part in re.findall(r"\d+", str(value or ""))]
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def _clean_bili_title(title: Any) -> str:
    """去掉 B 站标题里的画质标签与书名号噪声，保留 Live/现场等有效信息。"""
    value = _strip_html(title)
    value = re.sub(r"[【】\[\]]", " ", value)
    value = re.sub(
        r"(?i)\b(4k|8k|1080p|720p|480p|60fps|30fps|hi[- ]?res|hdr|无损|高音质|杜比|动态歌词|官方mv)\b",
        " ",
        value,
    )
    return re.sub(r"\s+", " ", value).strip()


def _bili_session() -> requests.Session:
    """带 Cookie 预热的 B 站会话（复用）。

    B 站搜索接口对"无 Cookie + 连续请求"会返回 412，所以先访问一次主页
    拿到 buvid3 等 Cookie；会话复用，避免每次点歌都重复预热。
    """
    global _BILI_SESSION
    if _BILI_SESSION is not None:
        return _BILI_SESSION

    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com/"})
    try:
        session.get("https://www.bilibili.com/", timeout=8)
    except requests.RequestException as error:
        logger.bind(tag=TAG).info(f"B 站预热失败（仍尝试直接搜索）: {error}")
    _BILI_SESSION = session
    return session


def _search_bilibili(query: str, limit: int, timeout: int) -> List[Dict[str, Any]]:
    """B 站 web 搜索接口（结构化 JSON，含 bvid / 标题 / 时长）。

    B 站没有"歌手"字段，标题就是全部信息；候选的歌手判定在
    `_rank_candidates` 里用"其他音源确认的歌手名"反查标题完成。
    """
    global _BILI_SESSION

    params = {"search_type": "video", "keyword": query, "page": 1}
    data = None
    for attempt in (1, 2):
        session = _bili_session()
        try:
            resp = session.get(BILIBILI_SEARCH_API, params=params, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as error:  # 412 风控 / JSON 解析失败 / 网络异常
            logger.bind(tag=TAG).info(
                f"B 站搜索失败（第 {attempt} 次）| query={query!r} | "
                f"{type(error).__name__}: {error}"
            )
            _BILI_SESSION = None  # 会话可能已失效，重建后再试一次
    if data is None:
        raise RuntimeError("B 站搜索失败（重试后仍不可用）")

    results = (data.get("data") or {}).get("result") or []
    songs = []
    for item in results[:limit]:
        if not isinstance(item, dict) or not item.get("bvid"):
            continue
        songs.append(
            {
                "source": "bilibili",
                "id": item.get("bvid"),
                "name": _clean_bili_title(item.get("title")),
                "artists": [],  # B 站无歌手字段
                "duration": _parse_bili_duration(item.get("duration")) * 1000,
                "url": BILIBILI_VIDEO_URL.format(bvid=item.get("bvid")),
            }
        )
    return songs


def _search_all(query: str, limit: int, timeout: int) -> List[Dict[str, Any]]:
    """并发搜索全部音源并合并候选；单源失败不影响其它源。"""
    log = logger.bind(tag=TAG)
    merged: List[Dict[str, Any]] = []

    def run(name: str, searcher) -> List[Dict[str, Any]]:
        try:
            songs = searcher(query, limit, timeout)
        except Exception as error:
            log.warning(f"音源 {name} 搜索失败 | query={query!r} | {type(error).__name__}: {error}")
            return []
        for song in songs:
            song["source"] = name
        return songs

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(run, "netease", _search_netease): "netease",
            pool.submit(run, "migu", _search_migu): "migu",
        }
        for future in as_completed(futures):
            merged.extend(future.result())

    return merged


def _score_song(
    song: Dict[str, Any], tokens: List[str], full_query: str
) -> Tuple[float, float, str, str]:
    """给搜索候选打分。

    返回 ``(排序总分, 歌名相关度, 命中的歌手名或空串, 最匹配的歌名片段)``：
    - 歌名相关度用于"是否真的相关"的准入判断（见 MATCH_THRESHOLD）；
    - 排序总分在此基础上叠加歌手加分、版本噪音与异常时长扣分；
    - 命中的歌手名用于"只播这位歌手的版本"过滤和失败话术；
    - 最匹配的歌名片段是"查询里对应的歌名"，供播报/命名使用——
      B 站候选的 name 是视频标题，播报时不能用它（用户要求只报歌名）。
    """
    name = _norm(song.get("name"))
    if not name:
        return -999.0, 0.0, "", ""

    name_best = 0.0
    name_token = ""
    for idx, token in enumerate(tokens):
        value = _name_score(token, name)
        if idx == 0 and value >= 60:
            value += 8.0  # 完整查询（含歌手）命中优先
        if value > name_best:
            name_best, name_token = value, token

    # 歌手命中要求"与查询的某个片段对齐"（相等 / 是片段的前后缀），
    # 不能只看子串：否则「紫色小马会跳舞」会被歌手「小马」误命中，
    # 放行完全不相干的歌曲（实测踩过）。
    artist_name = ""
    artists_raw = _artists_text(song)
    for part in re.split(r"[、/,&;；]+", artists_raw):
        normalized = _norm(part)
        if len(normalized) < 2:
            continue
        if any(
            token == normalized
            or token.startswith(normalized)
            or token.endswith(normalized)
            for token in tokens
        ):
            artist_name = part.strip()
            break

    score = name_best + (45.0 if artist_name else 0.0)

    # 同分微调：歌手名越短越可能是原唱本人 / 搬运原曲的账号，
    # 越长的越像 remix 营销号（优先让用户听到接近原版的版本）
    whole_artists = _norm(artists_raw)
    if whole_artists:
        score -= min(len(whole_artists), 12) * 0.4

    for word in NOISE_WORDS:
        if word in name and word not in full_query:
            score -= 55.0

    try:
        duration = int(song.get("duration") or song.get("dt") or 0) / 1000.0
    except (TypeError, ValueError):
        duration = 0.0
    if duration:
        if duration < 40:
            score -= 200.0  # 试听片段
        elif duration < 70:
            score -= 50.0
        elif duration > 900:
            score -= 40.0  # 合集 / 串烧
    return score, name_best, artist_name, name_token


def _title_hint(
    query: str, matched: str, artist: str, song: Dict[str, Any]
) -> str:
    """给播报/文件名挑一个"像歌名"的短标题。

    匹配到的 token 有时是整串查询去掉空格后的样子（“G.E.M.邓紫棋 唯一” →
    “gem邓紫棋唯一”），直接拿它播报会念出一句怪话。这种情况退回查询里
    去掉歌手之后的那一截（“唯一”）。
    """
    if _norm(matched) != _norm(query):
        return matched
    artist_norm = _norm(artist)
    song_name = _norm(song.get("name") or "")
    candidates = [
        token
        for token in _query_tokens(query)
        if _norm(token) != _norm(matched)
        and len(_norm(token)) >= 2
        and not (artist_norm and _norm(token) in artist_norm)
        and _norm(token) in song_name
    ]
    if not candidates:
        return matched
    # 越长的越可能是歌名（“唯一”优于“你的”这类碎片）
    return max(candidates, key=lambda token: len(_norm(token)))


def _rank_candidates(
    songs: List[Dict[str, Any]],
    query: str,
    max_candidates: int,
    named_artist: str = "",
) -> List[Tuple[float, str, Dict[str, Any]]]:
    """排序并过滤候选；与用户点的歌完全不相关的候选一律丢弃。

    放行条件：
    - 歌名相关度达标；或
    - 查询整体就是一个词（如「周杰伦」，说明用户只给了歌手名）
      且该歌手命中——此时允许播这位歌手的歌。

    另外：一旦查询里带了歌手名（存在"歌手对齐命中"的候选），
    就**只保留这位歌手的版本**——用户点"林俊杰的黑夜问白天"，
    宁可如实说播不了，也不能拿别人翻唱的版本糊弄（实测踩过）。

    named_artist：外部已知的"用户点名的歌手"（分阶段搜索时跨阶段共享，
    B 站等无歌手字段的音源靠它反查标题——标题里带了这位歌手才放行）。
    """
    tokens = _query_tokens(query)
    full_query = tokens[0] if tokens else ""
    single_token = len(tokens) <= 1
    entries: List[Tuple[float, str, Dict[str, Any]]] = []
    for song in songs:
        total, name_best, artist_name, name_token = _score_song(song, tokens, full_query)
        if name_best >= MATCH_THRESHOLD or (artist_name and single_token):
            # 记下"查询里对应的歌名"（title_hint）：播报/命名用它而不是原始标题，
            # 否则会念出 B 站视频全名（用户明确不要）。相关度不够时不记。
            if name_best >= 60 and name_token:
                song["title_hint"] = _title_hint(
                    query, name_token, artist_name, song
                )
            entries.append([total, artist_name, song])

    if not named_artist:
        # 从有"歌手字段"的音源结果里确定用户点名的歌手
        named_artist = next(
            (
                entry[1]
                for entry in entries
                if entry[1] and entry[2].get("source") != "bilibili"
            ),
            "",
        )

    if named_artist:
        hint = _norm(named_artist)
        kept = []
        for entry in entries:
            song = entry[2]
            if not entry[1] and song.get("source") == "bilibili":
                # B 站没有歌手字段：标题里带了这位歌手（现场版/搬运）才放行，
                # 并把歌手写回候选，让播报和文件名都带上歌手信息
                if hint and hint in _norm(song.get("name")):
                    entry[1] = named_artist
                    if not song.get("artists"):
                        song["artists"] = [{"name": named_artist}]
            if entry[1]:
                kept.append(entry)
        entries = kept

    # 排序：分数优先，同分时按音源顺序（网易云在前，直链下载更快更稳）
    entries.sort(
        key=lambda item: (-item[0], _SOURCE_ORDER.get(item[2].get("source"), 9))
    )

    return [(entry[0], entry[1], entry[2]) for entry in entries[: max(1, max_candidates)]]


# ── 下载 ─────────────────────────────────────────────────────────────────────
def _safe_filename(text: str) -> str:
    cleaned = _ILLEGAL_FILENAME_RE.sub("_", str(text or "")).strip(" .")
    return (cleaned or "unknown")[:120]


def _looks_like_audio(header: bytes) -> bool:
    if len(header) < 4:
        return False
    if header[:3] == b"ID3":
        return True
    if header[0] == 0xFF and (header[1] & 0xE0) == 0xE0:  # MPEG 帧同步
        return True
    if header[:4] in (b"RIFF", b"OggS", b"fLaC"):
        return True
    if header[4:8] == b"ftyp":  # m4a / mp4 容器
        return True
    if header[:4] == b"\x1a\x45\xdf\xa3":  # webm（EBML 头，B 站音频轨）
        return True
    return False


def _stream_response_to_file(resp, tmp: str, max_bytes: int, label: str) -> Optional[int]:
    """把流式响应写入临时文件；校验内容确实是音频，超出大小上限则放弃。

    返回写入的字节数；失败返回 None。
    """
    log = logger.bind(tag=TAG)
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if ctype and not ctype.startswith("audio"):
        log.info(f"直链不是音频 | {label} | ctype={ctype}")
        return None

    total = 0
    header = b""
    with open(tmp, "wb") as fp:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            if not header:
                header = chunk[:16]
                if not _looks_like_audio(header):
                    log.info(f"直链内容不是音频 | {label} | head={header[:8]!r}")
                    return None
            fp.write(chunk)
            total += len(chunk)
            if total > max_bytes:
                log.warning(f"文件超过上限，放弃 | {label} | >{max_bytes // 1024 // 1024}MB")
                return None
    return total


def _finalize_download(tmp: str, target: str, total: int, label: str) -> Optional[str]:
    """校验文件大小并原子改名；过小视为失败并清理临时文件。"""
    log = logger.bind(tag=TAG)
    if total < MIN_VALID_FILE_BYTES:
        log.info(f"文件过小，视为失败 | {label} | bytes={total}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
    os.replace(tmp, target)
    # 缓存目录内容已变化，清掉扫描缓存让下次点歌立刻能找到
    _DIR_CACHE.pop(str(Path(os.path.dirname(target))), None)
    return target


def _download_netease(
    song: Dict[str, Any], tmp: str, timeout: int, max_bytes: int, label: str
) -> Optional[int]:
    """网易云 outer 直链。版权受限的歌会 302 到 HTML 页面，靠内容校验拦掉。"""
    log = logger.bind(tag=TAG)
    url = NETEASE_OUTER_URL.format(song_id=song.get("id"))
    session = requests.Session()
    try:
        with session.get(
            url,
            headers={"User-Agent": UA, "Referer": "https://music.163.com/"},
            timeout=timeout,
            stream=True,
        ) as resp:
            if resp.status_code != 200:
                log.info(f"直链不可用 | {label} | status={resp.status_code}")
                return None
            return _stream_response_to_file(resp, tmp, max_bytes, label)
    except requests.RequestException as error:
        log.info(f"下载失败 | {label} | {type(error).__name__}: {error}")
        return None
    finally:
        session.close()


def _download_migu(
    song: Dict[str, Any], tmp: str, timeout: int, max_bytes: int, label: str
) -> Optional[int]:
    """咪咕 listenSong.do 取流；按音质档位依次尝试。

    受限歌曲会返回 200 + JSON（"暂不提供试听地址"），同样靠内容校验拦掉。
    """
    log = logger.bind(tag=TAG)
    session = requests.Session()
    try:
        for tone in MIGU_TONES:
            url = MIGU_LISTEN_API
            params = {
                "toneFlag": tone,
                "netType": "00",
                "userId": "",
                "ua": "Android_migu",
                "version": "5.0.1",
                "copyrightId": song.get("copyrightId"),
                "contentId": song.get("contentId"),
                "resourceType": 2,
                "channel": 0,
            }
            try:
                with session.get(
                    url,
                    params=params,
                    headers={"User-Agent": MIGU_UA, "Referer": "https://m.music.migu.cn/"},
                    timeout=timeout,
                    stream=True,
                ) as resp:
                    if resp.status_code != 200:
                        log.info(f"咪咕取流不可用 | {label} | tone={tone} status={resp.status_code}")
                        continue
                    total = _stream_response_to_file(resp, tmp, max_bytes, f"{label}({tone})")
                    if total is not None:
                        return total
            except requests.RequestException as error:
                log.info(f"咪咕取流失败 | {label} | tone={tone} {type(error).__name__}: {error}")
                continue
        return None
    finally:
        session.close()


def _download_bilibili(
    song: Dict[str, Any],
    cache_dir: str,
    timeout: int,
    max_bytes: int,
    label: str,
) -> Optional[str]:
    """用 yt-dlp 从 B 站取音频轨（m4a / webm），返回落盘路径。

    yt-dlp 自己处理 B 站的 DASH 直链与 Referer，并带 .part 机制，
    所以这里直接用最终文件名下载，不再做二次改名。
    """
    log = logger.bind(tag=TAG)
    try:
        import yt_dlp
    except ImportError:
        log.warning("未安装 yt-dlp，B 站音源不可用（pip install yt-dlp）")
        return None

    base = os.path.join(cache_dir, _safe_filename(label))
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "format": "bestaudio/best",
        "outtmpl": base + ".%(ext)s",
        "socket_timeout": timeout,
        "noplaylist": True,
        "max_filesize": max_bytes,
        "retries": 1,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(song.get("url"), download=True)
        path = ydl.prepare_filename(info)
    except Exception as error:  # yt-dlp 的异常类型随版本变化，统一兜底
        log.info(f"B 站下载失败 | {label} | {type(error).__name__}: {error}")
        return None

    if not path or not os.path.exists(path):
        # 扩展名可能被 yt-dlp 调整（.m4a / .webm），按前缀兜底查找
        matches = [
            item
            for item in glob.glob(base + ".*")
            if not item.endswith(".part")
        ]
        path = matches[0] if matches else ""
    if not path:
        log.info(f"B 站下载未落盘 | {label}")
        return None

    size = os.path.getsize(path)
    if size < MIN_VALID_FILE_BYTES:
        log.info(f"B 站文件过小，视为失败 | {label} | bytes={size}")
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    with open(path, "rb") as fp:
        if not _looks_like_audio(fp.read(16)):
            log.info(f"B 站文件不是音频 | {label}")
            return None
    log.info(f"B 站下载成功 | {os.path.basename(path)} | {size / 1024 / 1024:.2f}MB")
    return path


def _download_song(
    song: Dict[str, Any],
    cache_dir: str,
    timeout: int,
    max_file_mb: int,
) -> Optional[str]:
    """尝试下载一首歌到缓存目录，成功返回文件路径。

    按音源分派；统一做临时文件 + 内容校验 + 原子改名，
    避免半个文件/错误页被后续扫描当成有效歌曲。
    """
    label = _song_label(song)
    target = os.path.join(cache_dir, _safe_filename(label) + ".mp3")
    tmp = target + ".part"
    max_bytes = max_file_mb * 1024 * 1024

    os.makedirs(cache_dir, exist_ok=True)
    if song.get("source") == "bilibili":
        path = _download_bilibili(song, cache_dir, timeout, max_bytes, label)
        if path:
            _DIR_CACHE.pop(str(Path(cache_dir)), None)
        return path
    if song.get("source") == "migu":
        total = _download_migu(song, tmp, timeout, max_bytes, label)
    else:
        total = _download_netease(song, tmp, timeout, max_bytes, label)

    if total is None:
        # 失败路径可能留下空的 / 半截的 .part 文件，清理掉
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
    return _finalize_download(tmp, target, total, label)


# ── 解析：本地 → 缓存 → 在线 ─────────────────────────────────────────────────
def _resolve_offline(
    config: Dict[str, Any], query: str
) -> Tuple[Optional[str], Optional[Dict[str, Any]], str]:
    """本地曲库 + 在线缓存里找歌。返回 (路径, 歌曲信息, 失败说明)。"""
    dirs = _music_dirs(config)
    cache_dir = _cache_dir(config)

    files = _list_audio_files(dirs)
    path = _match_local(query, files)
    if path:
        return path, None, ""

    # 下载缓存也算本地曲库（只是没配置在 music_dirs 里）
    if os.path.isdir(cache_dir) and os.path.abspath(cache_dir) not in [
        os.path.abspath(item) for item in dirs
    ]:
        path = _match_local(query, _list_audio_files([cache_dir]))
        if path:
            return path, None, ""
    return None, None, ""


def _fetch_online(
    config: Dict[str, Any], query: str, display: str = ""
) -> Tuple[Optional[str], Optional[Dict[str, Any]], str]:
    """联网搜索并下载。返回 (路径, 歌曲信息, 失败话术)。

    query 是清洗过的搜索词，display 是给用户看的原始说法（用于话术）。

    两阶段：
    1. 网易云 + 咪咕（快，直链下载）；
    2. 都拿不到时再查 B 站现场版/搬运（yt-dlp 取音频，慢但能补版权独家歌）。
    """
    log = logger.bind(tag=TAG)
    shown = display or query
    search_limit = _int_config(config, "search_limit", DEFAULT_SEARCH_LIMIT)
    max_candidates = _int_config(config, "max_candidates", DEFAULT_MAX_CANDIDATES)
    timeout = _int_config(config, "timeout", DEFAULT_TIMEOUT)
    max_file_mb = _int_config(config, "max_file_mb", DEFAULT_MAX_FILE_MB)
    cache_dir = _cache_dir(config)

    try:
        songs = _search_all(query, search_limit, timeout)
    except Exception as error:  # 网络异常不应中断整轮对话
        log.error(f"在线搜索失败 | query={query!r} | {type(error).__name__}: {error}")
        return None, None, f"联网找《{shown}》失败了，检查一下网络再试试。"

    candidates = _rank_candidates(songs, query, max_candidates) if songs else []
    found_any = bool(candidates)
    if songs and not candidates:
        log.info(f"搜索结果里没有与《{shown}》相关的版本 | query={query!r} | hits={len(songs)}")

    # 候选里带歌手名 ⇒ 用户点名了歌手，此时只尝试这位歌手的版本（见 _rank_candidates）
    target_artist = candidates[0][1] if candidates else ""
    target_title = str(candidates[0][2].get("name") or "").strip() if candidates else ""

    if candidates:
        log.info(
            "在线候选 | "
            + " ; ".join(
                f"[{score:.0f}][{song.get('source')}] {_song_display(song)}"
                + (f"(歌手:{artist})" if artist else "")
                for score, artist, song in candidates
            )
        )
        for score, artist, song in candidates:
            path = _download_song(song, cache_dir, timeout, max_file_mb)
            if path:
                return path, song, ""
            log.info(
                f"候选不可播放，尝试下一个 | {_song_display(song)} | "
                f"source={song.get('source')} | score={score:.0f}"
            )

    # 阶段 2：B 站兜底（现场版 / 搬运），仅在前面全部失败且功能开启时
    if _bilibili_enabled(config):
        bili_limit = _int_config(config, "bilibili_limit", DEFAULT_BILIBILI_LIMIT)
        try:
            bili_songs = _search_bilibili(query, bili_limit, timeout)
        except Exception as error:
            log.warning(f"B 站搜索失败 | query={query!r} | {type(error).__name__}: {error}")
            bili_songs = []

        # named_artist 跨阶段共享：B 站靠它反查标题里的歌手，避免播错人
        bili_candidates = _rank_candidates(
            bili_songs, query, max_candidates, named_artist=target_artist
        ) if bili_songs else []
        if bili_candidates:
            found_any = True
            log.info(
                "B 站候选 | "
                + " ; ".join(
                    f"[{score:.0f}] {_song_display(song)}"
                    + (f"(歌手:{artist})" if artist else "")
                    for score, artist, song in bili_candidates
                )
            )
            for score, artist, song in bili_candidates:
                path = _download_song(song, cache_dir, timeout, max_file_mb)
                if path:
                    return path, song, ""
                log.info(f"B 站候选不可播放，尝试下一个 | {_song_display(song)} | score={score:.0f}")

    if not found_any:
        # 三个音源都没找到相关的歌（不是"找到了但播不了"）
        return None, None, f"没找到《{shown}》这首歌，换个歌名试试？"
    if not target_title:
        target_title = shown
    if target_artist:
        # 点名了歌手但拿不到：如实说明，不拿别的歌手的版本糊弄
        return (
            None,
            None,
            f"找到《{target_title}》了，但 {target_artist} 的版本受版权限制播不了，换一首试试？",
        )
    return None, None, f"《{target_title}》找到了，但都没有可播放的版本，换首歌试试？"


# ── 播放（复用 TTS 文本 + FILE 通道，与上游 play_music 一致） ────────────────
def _split_stem(stem: str) -> Tuple[str, str]:
    """把「歌手 - 歌名」文件名拆成 (歌手, 歌名)，拆不出时歌手为空。"""
    for sep in (" - ", "-", "—"):
        if sep in stem:
            left, right = stem.split(sep, 1)
            left, right = left.strip(), right.strip()
            if left and right:
                return left, right
    return "", stem.strip()


def _play_prompt(song: Optional[Dict[str, Any]], path: str) -> str:
    if song is not None:
        # 优先用查询里对应的歌名（title_hint），不要念 B 站视频全名
        title = (
            str(song.get("title_hint") or "").strip()
            or str(song.get("name") or "").strip()
            or Path(path).stem
        )
        artist = _artists_text(song)
    else:
        artist, title = _split_stem(Path(path).stem)
    # 标题里已经带歌手名（B 站视频标题常见）时不再重复"某某的《…》"，避免拗口
    # 话术走口语（"给你放一首"），不用"正在为您播放/请欣赏"这类播音腔
    if artist and _norm(artist) not in _norm(title):
        return random.choice(
            [
                f"给你放一首{artist}的《{title}》",
                f"来，{artist}的《{title}》",
                f"{artist}的《{title}》，走起",
            ]
        )
    return random.choice(
        [f"给你放《{title}》", f"来一首《{title}》", f"《{title}》，放着呢"]
    )


def _push_report_text(conn: "ConnectionHandler", text: str) -> None:
    """把一句播报文本推给 TTS 队列（会正常合成语音、驱动口型）。

    注意：这里**不推 FILE**，所以必须显式补一个 LAST 标记，
    否则文本会滞留在 TTS 文本缓冲里不落定。
    """
    conn.tts.store_tts_text(conn.sentence_id, text)
    if conn.intent_type == "intent_llm":
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=conn.sentence_id,
                sentence_type=SentenceType.FIRST,
                content_type=ContentType.ACTION,
            )
        )
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.MIDDLE,
            content_type=ContentType.TEXT,
            content_detail=text,
        )
    )
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.LAST,
            content_type=ContentType.ACTION,
        )
    )


async def _play_file_bgm(
    conn: "ConnectionHandler",
    path: str,
    song: Optional[Dict[str, Any]],
    source: str,
    started_at: float,
) -> ActionResponse:
    """背景音乐模式：播报走 TTS（正常口型），音乐交给客户端独立播放器。

    音乐不进 TTS 音频流 ⇒ 不驱动口型、也不会被后续对话
    （client_abort 清空 TTS 队列）打断；客户端会在播报结束后再起音乐。
    """
    log = conn.logger.bind(tag=TAG)
    config = _plugin_config(conn)
    text = _play_prompt(song, path)
    title = _song_label(song) if song else Path(path).stem

    _push_report_text(conn, text)

    payload = {
        "type": "bgm",
        "action": "play",
        "url": _music_url(conn, path),
        "title": title,
        "volume": _bgm_volume(config),
    }
    await conn.websocket.send(json.dumps(payload, ensure_ascii=False))

    log.info(
        f"play_song | mode=bgm | source={source} | file={path} | "
        f"耗时={time.time() - started_at:.2f}s | 提示语={text!r}"
    )
    return ActionResponse(Action.RECORD, f"播放 {title}", text)


async def _play_file_server(
    conn: "ConnectionHandler",
    path: str,
    song: Optional[Dict[str, Any]],
    source: str,
    started_at: float,
) -> ActionResponse:
    """服务端播放模式（方案B）：音乐从服务器本机扬声器播放。

    与客户端 BGM 的区别：
    - 音乐不占设备音频流，ESP32 也能"边说话边放歌"；
    - 播放进程与语音会话解耦，对话打断 / 桌宠退出都不影响；
    - 播报仍然走 TTS（"正在为您播放…"），延迟 ``server_start_delay`` 后起音乐。
    """
    log = conn.logger.bind(tag=TAG)
    config = _plugin_config(conn)
    text = _play_prompt(song, path)
    title = _song_label(song) if song else Path(path).stem
    volume = _server_volume(config)
    fade = _server_fade(config)
    delay = _server_delay(config)

    # 防双播：客户端可能正在放上一首（客户端 BGM 与服务端播放只能有一路出声）
    if _bgm_supported(conn):
        try:
            await conn.websocket.send(
                json.dumps({"type": "bgm", "action": "stop"}, ensure_ascii=False)
            )
        except Exception as error:
            log.warning(f"停止客户端旧音乐失败 | {type(error).__name__}: {error}")

    # 先播报（走设备 TTS），再延迟起音乐，避免前奏盖住"正在为您播放"
    _push_report_text(conn, text)

    async def _start_later() -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            from core.utils.music_player import get_music_player

            player = get_music_player()
            state = await asyncio.to_thread(player.play, path, volume, fade, title)
            log.info(
                f"play_song | mode=server | source={source} | file={path} | "
                f"volume={volume:.2f} | 状态={state.get('playing')} | "
                f"耗时={time.time() - started_at:.2f}s"
            )
        except Exception as error:
            log.error(
                f"服务端音乐播放失败 | file={path} | "
                f"{type(error).__name__}: {error}"
            )
            try:
                _push_report_text(conn, "音乐播放失败了，稍后再试试。")
            except Exception:
                pass

    task = asyncio.create_task(_start_later())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)

    log.info(
        f"play_song | mode=server-scheduled | source={source} | file={path} | "
        f"delay={delay:.1f}s | 提示语={text!r}"
    )
    return ActionResponse(Action.RECORD, f"播放 {title}", text)


async def _play_file(
    conn: "ConnectionHandler",
    path: str,
    song: Optional[Dict[str, Any]],
    source: str,
    started_at: float,
) -> ActionResponse:
    config = _plugin_config(conn)
    mode = _playback_mode(config)
    bgm_supported = _bgm_supported(conn)

    # ESP32 等没有 features.bgm 的设备：默认走服务端播放，音乐与语音彻底分开
    if mode == "server" or (
        mode == "auto" and not bgm_supported and _bgm_enabled(config)
    ):
        return await _play_file_server(conn, path, song, source, started_at)

    if mode in ("auto", "client") and bgm_supported and _bgm_enabled(config):
        return await _play_file_bgm(conn, path, song, source, started_at)

    log = conn.logger.bind(tag=TAG)
    text = _play_prompt(song, path)
    display = _song_label(song) if song else Path(path).stem

    conn.tts.store_tts_text(conn.sentence_id, text)
    if conn.intent_type == "intent_llm":
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=conn.sentence_id,
                sentence_type=SentenceType.FIRST,
                content_type=ContentType.ACTION,
            )
        )
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.MIDDLE,
            content_type=ContentType.TEXT,
            content_detail=text,
        )
    )
    conn.tts.tts_text_queue.put(
        TTSMessageDTO(
            sentence_id=conn.sentence_id,
            sentence_type=SentenceType.MIDDLE,
            content_type=ContentType.FILE,
            content_file=path,
        )
    )
    if conn.intent_type == "intent_llm":
        conn.tts.tts_text_queue.put(
            TTSMessageDTO(
                sentence_id=conn.sentence_id,
                sentence_type=SentenceType.LAST,
                content_type=ContentType.ACTION,
            )
        )

    log.info(
        f"play_song | mode=stream | source={source} | file={path} | "
        f"耗时={time.time() - started_at:.2f}s | 提示语={text!r}"
    )
    return ActionResponse(Action.RECORD, f"播放 {display}", text)


async def _play_random(
    conn: "ConnectionHandler", config: Dict[str, Any], started_at: float
) -> ActionResponse:
    """「随便放首歌」：热歌榜随机一首优先，拿不到才退回本地曲库随机。

    返回给用户的永远是"正在播放 X"，用户能听到当前热门而不是那几首老歌；
    热歌榜/下载失败时不假装放成功，退回本地随机时也会说明。
    """
    log = conn.logger.bind(tag=TAG)
    download_failed = False

    if _online_enabled(config) and _hot_random_enabled(config):
        pick = await asyncio.to_thread(_hot_random_query)
        if pick:
            log.info(f"random | 热歌榜随机命中 | pick={pick!r}")
            # 榜单里的歌之前点过就直接用缓存，否则走正常在线点歌链路
            path, song, _ = await asyncio.to_thread(_resolve_offline, config, pick)
            if path:
                return await _play_file(conn, path, song, "local", started_at)
            path, song, message = await asyncio.to_thread(
                _fetch_online, config, _clean_query(pick) or pick, pick
            )
            if path:
                return await _play_file(conn, path, song, "online", started_at)
            download_failed = True
            log.warning(f"random | 热歌榜这首没下成 | pick={pick!r} | {message}")
        else:
            log.warning("random | 热歌榜没拉到，退回本地随机")

    dirs = _music_dirs(config) + [_cache_dir(config)]
    files = await asyncio.to_thread(_list_audio_files, dirs)
    if not files:
        text = (
            "热歌榜里随机挑的那首没下成，本地也还没歌，稍后再试试吧。"
            if download_failed
            else "热歌榜没取到，本地也还没歌，稍后再试试吧。"
        )
        return ActionResponse(Action.RESPONSE, None, text)
    log.info("random | 退回本地曲库随机")
    return await _play_file(conn, random.choice(files), None, "random", started_at)


@register_function("play_song", PLAY_SONG_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def play_song(conn: "ConnectionHandler", song_name: str = ""):
    """点歌：本地曲库优先，其次在线搜索下载，最后复用 TTS 通道播放。"""
    started_at = time.time()
    log = conn.logger.bind(tag=TAG)
    config = _plugin_config(conn)
    query = str(song_name or "").strip()
    log.info(f"play_song 请求 | song_name={query!r}")

    try:
        # 「随便放首歌」→ 先取当前热歌榜，随机挑一首在线点播；取不到再退回本地随机
        if _is_random_request(query):
            return await _play_random(conn, config, started_at)

        # 1) 本地曲库 / 在线缓存
        path, song, _ = await asyncio.to_thread(_resolve_offline, config, query)
        if path:
            return await _play_file(conn, path, song, "local", started_at)

        # 2) 在线点歌
        if not _online_enabled(config):
            return ActionResponse(
                Action.RESPONSE,
                None,
                f"本地曲库没有《{query}》这首歌，联网点歌是关着的。",
            )

        search_query = _clean_query(query) or query
        path, song, message = await asyncio.to_thread(
            _fetch_online, config, search_query, query
        )
        if not path:
            return ActionResponse(Action.RESPONSE, None, message)
        return await _play_file(conn, path, song, "online", started_at)
    except Exception as error:
        log.error(f"点歌失败 | song_name={query!r} | {type(error).__name__}: {error}")
        return ActionResponse(Action.RESPONSE, None, "点歌时出错了，稍后再试试。")


STOP_MUSIC_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "stop_music",
        "description": (
            "停止正在播放的音乐。当用户说“停止播放/别放了/关掉音乐/停止音乐/我不想听了”时调用。"
            "当前没有在播放音乐时调用也没有副作用，不要因为不确定而拒绝调用。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


@register_function("stop_music", STOP_MUSIC_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def stop_music(conn: "ConnectionHandler"):
    """停止音乐：客户端 BGM / 服务端播放进程 / TTS 流三条路都做幂等停止。"""
    log = conn.logger.bind(tag=TAG)
    config = _plugin_config(conn)
    mode = _playback_mode(config)
    bgm_supported = _bgm_supported(conn)
    server_owned = mode == "server" or (
        mode == "auto" and not bgm_supported and _bgm_enabled(config)
    )
    if bgm_supported:
        try:
            await conn.websocket.send(
                json.dumps({"type": "bgm", "action": "stop"}, ensure_ascii=False)
            )
        except Exception as error:
            log.warning(f"下发停止音乐指令失败 | {type(error).__name__}: {error}")

    # 无论当前配置是哪一路，都顺手停一下服务端播放器（没在播时开销可忽略）
    try:
        from core.utils.music_player import get_music_player

        player = get_music_player()
        if player.is_alive():
            await asyncio.to_thread(player.stop)
            log.info("stop_music | 服务端播放进程已停止")
    except Exception as error:
        log.warning(f"停止服务端音乐失败 | {type(error).__name__}: {error}")

    if not bgm_supported and not server_owned:
        # 设备模式（ESP32 原生）：音乐在 TTS 音频流里排队发送，
        # 只能靠打断信号把队列清掉（下一轮对话会自动复位该标志）。
        conn.client_abort = True
        await asyncio.sleep(0.3)
        conn.client_abort = False

    text = "好，音乐关了"
    _push_report_text(conn, text)
    log.info(f"stop_music | 已下发停止指令 | mode={mode}")
    return ActionResponse(Action.RECORD, "已停止播放音乐", text)


CLEAR_MUSIC_CACHE_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "clear_music_cache",
        "description": (
            "清理本地音乐缓存（在线点歌下载到缓存目录里的音频文件）。"
            "缓存里的歌一旦下错，之后点同一首歌会一直播这一版，必须删掉缓存才会重新找。"
            "当用户说“清理音乐缓存”“把这首歌的缓存删了”“刚才那首不对，删掉重新下”"
            "“缓存里那首月光不要了”时调用本工具。"
            "song_name 留空表示清空全部缓存；填歌名或歌手表示只删名字里含这个词的缓存。"
            "只删缓存文件，不会动 music_dirs 里的本地曲库。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "song_name": {
                    "type": "string",
                    "description": (
                        "可选。只清理名字里含这个词的缓存，填用户说的歌名或歌手（如“月光”“胡彦斌”）；"
                        "用户要求全部清空时留空。不要把“全部”“缓存”这类词填进来。"
                    ),
                }
            },
            "required": [],
        },
    },
}


@register_function(
    "clear_music_cache", CLEAR_MUSIC_CACHE_FUNCTION_DESC, ToolType.SYSTEM_CTL
)
async def clear_music_cache(conn: "ConnectionHandler", song_name: str = ""):
    """清理在线点歌的缓存文件（可按歌名/歌手过滤，留空清空全部）。"""
    config = _plugin_config(conn)
    keyword = _clean_query(str(song_name or "")) or str(song_name or "").strip()
    result = await asyncio.to_thread(remove_cached_songs, config, None, keyword)

    removed = result["removed"]
    freed_mb = result["bytes"] / 1024 / 1024
    logger.bind(tag=TAG).info(
        f"clear_music_cache 工具调用 | keyword={keyword!r} | removed={removed} "
        f"| failed={result['failed']}"
    )

    if removed:
        what = f"《{keyword}》的缓存" if keyword else "全部音乐缓存"
        return ActionResponse(
            Action.RESPONSE,
            None,
            f"已经清理{what}，共{len(removed)}首（{freed_mb:.1f}兆），"
            "下次点这首歌会重新联网找。",
        )
    if keyword:
        return ActionResponse(
            Action.RESPONSE, None, f"缓存里没有《{keyword}》，不用清理。"
        )
    return ActionResponse(Action.RESPONSE, None, "音乐缓存本来就是空的，没什么可清理的。")
