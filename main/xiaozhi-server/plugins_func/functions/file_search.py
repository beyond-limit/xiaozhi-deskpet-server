"""本机文件查找插件（桌宠：语音找文件 → 屏幕列结果 → 一句话打开）。

用途：让用户用一句话在这台电脑上找文件，例如：
``帮我找一下桌面上的报告``、``找 PDF``、``找一下上周的图片``。
命中后在桌宠屏幕上列出可点击的结果，用户说``打开第一个``即可打开。

设计要点：

1. **目录白名单**：只在配置好的根目录里搜（默认桌面 / 文档 / 下载），绝不全盘扫描；
   roots 里既可以写关键字（desktop / documents / downloads / pictures …），也可以写绝对路径。
2. **目录黑名单 + 深度 + 超时三重约束**：跳过 ``node_modules`` / ``AppData`` / 系统目录等噪音
   目录，限制递归深度与总耗时（默认 10 秒），避免一次语音请求把机器拖住。
3. **结果面板**：命中后给客户端下发一条 ``{"type": "files", "action": "list", ...}``，
   桌宠弹出可点击列表；不支持该特性的客户端（ESP32 等）只播报文件名。
4. **打开只能引用结果**：``open_file`` 优先接受``第一个 / 第 2 个 / 名字``这类引用，
   从最近一次搜索结果里解析；直接给绝对路径时会校验必须落在白名单根目录内，
   杜绝"任意路径打开"。
5. 所有阻塞的文件遍历都通过 ``asyncio.to_thread`` 放到线程里执行，不阻塞事件循环。
"""

import asyncio
import ctypes
import difflib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Set, Tuple

from config.logger import setup_logging
from plugins_func.functions.file_search_catalog import get_effective_config
from plugins_func.register import Action, ActionResponse, ToolType, register_function

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

IS_WINDOWS = sys.platform.startswith("win")

# ── 默认值 ───────────────────────────────────────────────────────────────────
# 默认只在"用户自己的地方"找，避免扫系统盘
DEFAULT_ROOTS: List[str] = ["desktop", "documents", "downloads"]
DEFAULT_EXCLUDE_DIRS: List[str] = [
    "node_modules",
    ".git",
    ".svn",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "AppData",
    "Application Data",
    "$Recycle.Bin",
    "System Volume Information",
    "Windows",
    "Program Files",
    "Program Files (x86)",
    "ProgramData",
    ".cache",
    ".idea",
    ".gradle",
    ".m2",
    ".npm",
    ".pyenv",
    ".conda",
    "pkgs",
    "site-packages",
    "dist-info",
]
# 备份/临时扩展名：播报时会念成"xxx（备份）"，避免和主文件重名分不清
BACKUP_EXTS: Set[str] = {
    ".bak",
    ".tmp",
    ".old",
    ".orig",
    ".copy",
    ".save",
    ".backup",
}
# 这些文件是系统产生的噪音，永远不算命中
SKIP_FILE_NAMES: Set[str] = {
    "desktop.ini",
    "thumbs.db",
    ".ds_store",
    "ehthumbs.db",
}
# 隐藏 / 系统属性的文件不算命中（桌面上的 ~$ 锁文件、desktop.ini 之类）
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
DEFAULT_MAX_DEPTH = 6
DEFAULT_MAX_RESULTS = 20
DEFAULT_TIMEOUT = 10.0
# 整台电脑模式：范围大得多，单次查找时间预算自动放宽（可用 timeout 显式覆盖）
DEFAULT_WHOLE_PC_TIMEOUT = 20.0
# "整台电脑/全盘"这类说法的识别词
WHOLE_PC_WORDS = ("整台电脑", "整个电脑", "整机", "全盘", "所有盘", "整个硬盘", "全部硬盘")
# 搜索结果可被 open_file 引用的有效期（秒）
RESULT_TTL = 1800

# Windows 已知文件夹（用 SHGetKnownFolderPath 取真实路径，能正确处理 OneDrive 重定向
# 与"文档/下载被改到 D 盘"的情况）；取不到时回退到 ~/Desktop 这类约定路径。
KNOWN_FOLDER_GUIDS: Dict[str, Tuple[str, str]] = {
    # 关键字: (FOLDERID GUID, 回退的相对路径)
    "desktop": ("{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}", "Desktop"),
    "documents": ("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}", "Documents"),
    "downloads": ("{374DE290-123F-4565-9164-39C4925E467B}", "Downloads"),
    "pictures": ("{33E28130-4E1E-4676-835A-98395C3BC3BB}", "Pictures"),
    "music": ("{4BD8D571-6D19-48D3-BE97-422220080E43}", "Music"),
    "videos": ("{18989B1D-99B5-455B-841C-AB7C74E4DDFC}", "Videos"),
}
# 语音里常见的说法 → 关键字
ROOT_ALIASES: Dict[str, str] = {
    "desktop": "desktop",
    "桌面": "desktop",
    "桌面上": "desktop",
    "documents": "documents",
    "document": "documents",
    "文档": "documents",
    "我的文档": "documents",
    "downloads": "downloads",
    "download": "downloads",
    "下载": "downloads",
    "下载目录": "downloads",
    "pictures": "pictures",
    "图片": "pictures",
    "照片": "pictures",
    "music": "music",
    "音乐": "music",
    "videos": "videos",
    "视频": "videos",
    "home": "home",
    "用户目录": "home",
    "家目录": "home",
}
# scope 的口语匹配表：长的排前面，避免"下载目录"被"下载"先截走
_SCOPE_KEYWORDS: Sequence[Tuple[str, str]] = sorted(
    ROOT_ALIASES.items(), key=lambda item: len(item[0]), reverse=True
)
# 展示用的中文标签
ROOT_LABELS: Dict[str, str] = {
    "desktop": "桌面",
    "documents": "文档",
    "downloads": "下载",
    "pictures": "图片",
    "music": "音乐",
    "videos": "视频",
    "home": "用户目录",
}

# 语音里说的类型 → 扩展名集合（ext 参数支持"pdf""图片""excel"这类口语说法）
EXT_GROUPS: Dict[str, Sequence[str]] = {
    "文档": (".doc", ".docx", ".pdf", ".txt", ".md", ".rtf", ".odt", ".wps"),
    "文本文件": (".txt", ".md", ".log", ".ini", ".csv"),
    "表格": (".xls", ".xlsx", ".csv", ".et", ".ods"),
    "excel": (".xls", ".xlsx", ".csv", ".et"),
    "幻灯片": (".ppt", ".pptx", ".dps", ".odp"),
    "ppt": (".ppt", ".pptx"),
    "pdf": (".pdf",),
    "图片": (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".heic", ".tif"),
    "照片": (".jpg", ".jpeg", ".png", ".heic", ".webp"),
    "视频": (".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".mpg"),
    "音乐": (".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".wma"),
    "音频": (".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".wma"),
    "压缩包": (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"),
    "代码": (
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".java",
        ".c",
        ".h",
        ".cpp",
        ".cs",
        ".go",
        ".rs",
        ".php",
        ".html",
        ".css",
        ".json",
        ".sh",
    ),
    "word": (".doc", ".docx", ".wps"),
}

# 播报用的中文类别（顺序即优先级）：不念 "xlsx""mp4" 这类字母组合，
# SAPI 会把它们读成"x l s x 文件"，听起来像在报错。
# 标签本身就是能直接接在"个"后面的名词（"找到 1 个表格文件"），不再拼"文件"。
_EXT_LABEL_ORDER: Sequence[Tuple[str, str]] = (
    ("pdf", "PDF 文件"),
    ("excel", "表格文件"),
    ("word", "Word 文档"),
    ("ppt", "PPT"),
    ("表格", "表格文件"),
    ("幻灯片", "PPT"),
    ("图片", "图片"),
    ("视频", "视频"),
    ("音乐", "音频"),
    ("音频", "音频"),
    ("压缩包", "压缩包"),
    ("文本文件", "文本文件"),
    ("文档", "文档"),
    ("代码", "代码文件"),
)


def _ext_label(ext: str) -> str:
    """把 ext 参数换成播报用的中文类别；认不出来就返回空串（只念"文件"）。"""
    raw = str(ext or "").strip().lower()
    if not raw:
        return ""
    for alias, label in _EXT_LABEL_ORDER:
        if raw == alias:
            return label
    dotted = f".{raw.lstrip('.')}"
    for alias, label in _EXT_LABEL_ORDER:
        if dotted in EXT_GROUPS.get(alias, ()):
            return label
    return ""

# 关键词里的噪音词（语音转文字常带的礼貌用语/动词）
NOISE_TOKENS: Set[str] = {
    "帮我",
    "请",
    "麻烦",
    "一下",
    "找一下",
    "找找",
    "查找",
    "搜索",
    "搜一下",
    "找",
    "打开",
    "看看",
    "有没有",
    "有没有叫",
    "叫",
    "那个",
    "我的",
    "里的",
    "里面",
    "的",
    "文件",
    "东西",
}
# 时间限定：说"昨天的""上周的"时转成 mtime 过滤
TIME_RULES: Sequence[Tuple[str, int]] = (
    ("今天", 1),
    ("今天的", 1),
    ("昨天", 2),
    ("昨天的", 2),
    ("前天", 3),
    ("前天的", 3),
    ("本周", 7),
    ("这周", 7),
    ("上周", 14),
    ("最近", 30),
    ("这个月", 31),
    ("上个月", 62),
)
_CN_DIGITS: Dict[str, int] = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

_TOKEN_SPLIT_RE = re.compile(r"[\s,，、;；:：]+")
_PATH_SEP_RE = re.compile(r"[\\/]")
_ORDINAL_RE = re.compile(r"第?\s*([0-9一二三四五六七八九十两]+)\s*(?:个|份|条|项)")
# 路径里凡是不可信字符（含 ..）都不接受
_UNSAFE_PATH_RE = re.compile(r"\.\.|[\x00-\x1f]|\*|\?|\"|<|>|\|")


# ── 工具描述 ─────────────────────────────────────────────────────────────────
SEARCH_FILES_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "search_files",
        "description": (
            "在这台电脑上按文件名查找文件，找到后在桌宠屏幕上列出结果。"
            "当用户说“帮我找一下某个文件”“电脑上有没有某某文件”“找一下 PDF / 图片 / 表格”"
            "“桌面上那个报告在哪”这类找文件的请求时调用。"
            "只在配置好的目录里查找（允许的目录清单由服务端注入描述），不需要用户指定路径。"
            "**用户提出找文件时必须调用本工具，只口头答应（如“这就帮你找找看”）不调用等于没做。**"
            "本工具自带模糊匹配：忽略大小写/空格/全半角差异、容忍少量错别字，"
            "多个关键词命中一部分也会返回，所以用户说得不精确时也应当调用本工具，"
            "不要因为“名字记不全”就回答找不到。"
            "本工具只负责“找”，找到后让用户说“打开第一个”来打开，不要自己编造文件内容或路径。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": (
                        "文件名关键词，填用户说的文件名或其中的关键字，如“报告”“周报”“预算”；"
                        "几个词用空格隔开即可（命中一部分也算），不必写全名；"
                        "用户要找某类文件（如“找一下PDF”“有没有图片”）时，"
                        "把类型填在 ext 参数里，keyword 可以留空或用用户提到的其它关键字。"
                    ),
                },
                "ext": {
                    "type": "string",
                    "description": (
                        "可选。文件类型，支持扩展名（pdf / .docx / xlsx）或类别说法"
                        "（文档 / 表格 / 图片 / 视频 / 音乐 / 压缩包 / 代码 / word / excel / ppt）。"
                        "用户没说类型时留空，表示不限类型。"
                    ),
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "可选。限定在哪个目录里找；填用户说的那个目录（目录名或路径原样填即可，"
                        "服务端会校验范围并在超出时明确说明）。留空表示在全部允许目录里找。"
                        "（实际允许的目录由服务端按配置注入，见运行时描述。）"
                    ),
                },
            },
            "required": [],
        },
    },
}

OPEN_FILE_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "open_file",
        "description": (
            "打开刚才查找到的文件。用户说“打开第一个 / 打开第二个 / 打开那个某某文件 / "
            "打开文件所在位置”时调用。"
            "file_ref 用用户说的引用（“第一个”“第2个”“报告.pdf”），"
            "或上一次查找结果里的文件名；也可以填允许目录内的绝对路径。"
            "找不到对应结果时如实说没有，不要编造路径。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_ref": {
                    "type": "string",
                    "description": (
                        "要打开的文件：优先填“第一个 / 第 2 个”这类序号或文件名，"
                        "也可以填桌面、文档、下载目录里的绝对路径。"
                    ),
                },
                "reveal": {
                    "type": "boolean",
                    "description": (
                        "true 表示只打开文件所在的文件夹并选中它，不打开文件本身；"
                        "用户说“打开文件位置 / 在文件夹里显示”时填 true，默认 false。"
                    ),
                },
            },
            "required": ["file_ref"],
        },
    },
}


# ── 搜索结果缓存（供 open_file 引用） ────────────────────────────────────────
# session_key -> {"items": [...], "at": 时间戳}
_RESULTS: Dict[str, Dict[str, Any]] = {}


def _session_key(conn: "ConnectionHandler") -> str:
    for attr in ("session_id", "device_id"):
        value = getattr(conn, attr, None)
        if value:
            return str(value)
    return f"conn-{id(conn)}"


def _remember_results(conn: "ConnectionHandler", items: List[Dict[str, Any]]) -> None:
    _RESULTS[_session_key(conn)] = {"items": items, "at": time.time()}
    # 顺手清理过期条目，避免长期运行后无限增长
    now = time.time()
    for key in [k for k, v in _RESULTS.items() if now - v.get("at", 0) > RESULT_TTL]:
        _RESULTS.pop(key, None)


def _last_results(conn: "ConnectionHandler") -> List[Dict[str, Any]]:
    entry = _RESULTS.get(_session_key(conn))
    if not entry:
        return []
    if time.time() - entry.get("at", 0) > RESULT_TTL:
        _RESULTS.pop(_session_key(conn), None)
        return []
    return list(entry.get("items") or [])


# ── 配置解析 ─────────────────────────────────────────────────────────────────
def _plugin_config(conn: "ConnectionHandler") -> Dict[str, Any]:
    """`.config.yaml` 的默认值 + 桌宠「查找范围设置」里用户改过的部分。"""
    config = conn.config.get("plugins", {}) or {}
    return get_effective_config(config.get("file_search", {}) or {})


def _expand_path(raw: str) -> str:
    """展开 %ENV% / ~ 并把相对路径解析为基于服务端工作目录的绝对路径。"""
    value = os.path.expandvars(os.path.expanduser(str(raw or "").strip()))
    if value and not os.path.isabs(value):
        value = os.path.abspath(value)
    return value


def _folder_guid(guid: str):
    """把 "{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}" 解析成 GUID 结构体。

    注意最后两段：第 4 段 4 位十六进制 + 第 5 段 12 位十六进制 **合起来** 才是 8 字节的
    Data4。只取第 4 段会得到错的 GUID（本机就因此拿不到真实桌面，详见 PROGRESS 23.6）。
    """
    from ctypes import wintypes

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    parts = guid.strip("{}").split("-")
    data4 = bytes.fromhex(parts[3] + parts[4])
    return _GUID(
        int(parts[0], 16),
        int(parts[1], 16),
        int(parts[2], 16),
        (ctypes.c_ubyte * 8)(*data4),
    )


def _known_folder_via_api(keyword: str) -> Optional[str]:
    """用 SHGetKnownFolderPath 取真实路径（能正确处理桌面/文档被重定向到别的盘）。"""
    info = KNOWN_FOLDER_GUIDS.get(keyword)
    if not (IS_WINDOWS and info):
        return None
    try:
        folder_id = _folder_guid(info[0])
        path_ptr = ctypes.c_wchar_p()
        # 显式声明签名：否则 64 位下指针可能被截断、返回值也不知道是不是 HRESULT
        func = ctypes.WinDLL("shell32").SHGetKnownFolderPath
        func.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        func.restype = ctypes.c_long
        hresult = func(ctypes.byref(folder_id), 0, None, ctypes.byref(path_ptr))
        address = ctypes.cast(path_ptr, ctypes.c_void_p).value
        value = str(path_ptr.value or "") if hresult == 0 else ""
        if address:  # 无论成功与否，API 分配的内存都要还回去
            ctypes.WinDLL("ole32").CoTaskMemFree(ctypes.c_void_p(address))
        if hresult != 0:
            # 不能再静默失败：错了就要能从日志里看出来（本机踩过，详见 PROGRESS 23.6）
            logger.bind(tag=TAG).warning(
                f"SHGetKnownFolderPath 失败（{keyword}）: HRESULT=0x{hresult & 0xFFFFFFFF:08X}"
            )
        return value or None
    except Exception as error:
        logger.bind(tag=TAG).warning(
            f"SHGetKnownFolderPath 异常（{keyword}）: {type(error).__name__}: {error}"
        )
        return None


def _known_folder_via_registry(keyword: str) -> Optional[str]:
    """注册表兜底：User Shell Folders 里的真值（值里可能带 %USERPROFILE% 之类的变量）。"""
    if not IS_WINDOWS:
        return None
    names = {
        "desktop": "Desktop",
        "documents": "Personal",
        "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
        "pictures": "My Pictures",
        "music": "My Music",
        "videos": "My Video",
    }
    value_name = names.get(keyword)
    if not value_name:
        return None
    try:
        import winreg

        base = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, base) as key:
            raw, _kind = winreg.QueryValueEx(key, value_name)
        path = os.path.expandvars(str(raw or "").strip())
        return path or None
    except Exception:
        return None


def _known_folder(keyword: str) -> Optional[str]:
    """取 Windows 已知文件夹的真实路径，逐级兜底：系统 API → 注册表 → ~/XXX。"""
    if keyword == "home":
        return os.path.expanduser("~")
    info = KNOWN_FOLDER_GUIDS.get(keyword)
    if not info:
        return None

    for candidate in (_known_folder_via_api(keyword), _known_folder_via_registry(keyword)):
        if candidate and os.path.isdir(candidate):
            return candidate

    fallback_path = os.path.join(os.path.expanduser("~"), info[1])
    return fallback_path if os.path.isdir(fallback_path) else None


def _whole_pc(config: Dict[str, Any]) -> bool:
    """是否开启"允许查找整台电脑"（所有可访问盘符）。"""
    return bool(config.get("whole_pc") or config.get("entire_pc"))


def _logical_drives() -> List[str]:
    """枚举本机可访问的盘符（Windows）；其它平台退化为根目录。"""
    if not IS_WINDOWS:
        return ["/"] if os.path.isdir("/") else []
    drives: List[str] = []
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception as error:
        logger.bind(tag=TAG).warning(f"枚举盘符失败: {type(error).__name__}: {error}")
        return drives
    for index in range(26):
        if not mask & (1 << index):
            continue
        root = f"{chr(ord('A') + index)}:\\"
        # 光驱/读卡器可能没介质：再确认能访问（scandir 失败也已有兜底）
        if os.path.isdir(root):
            drives.append(root)
    return drives


def _resolve_roots(config: Dict[str, Any]) -> List[Dict[str, str]]:
    """把配置解析成 [{label, path}]，只保留真实存在的目录。

    三种来源（按优先级）：
      1. ``whole_pc: true`` → 整台电脑（所有可访问盘符），此时忽略下面两项；
      2. ``roots``：常用目录，可写关键字（桌面/文档/下载/图片/音乐/视频/用户目录）或绝对路径；
      3. ``extra_dirs``：额外允许查找的目录（绝对路径，可多条）。
    """
    if _whole_pc(config):
        drives = [
            {"label": f"{drive[:2]} 盘", "path": drive} for drive in _logical_drives()
        ]
        logger.bind(tag=TAG).info(
            f"file_search | 整机模式 | 盘符={[d['path'] for d in drives]}"
        )
        return drives

    raw = config.get("roots")
    if raw is None:
        raw = list(DEFAULT_ROOTS)
    if isinstance(raw, str):
        raw = [raw]
    extra = config.get("extra_dirs") or []
    if isinstance(extra, str):
        extra = [extra]
    items = list(raw) + list(extra)

    roots: List[Dict[str, str]] = []
    seen: Set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in ROOT_ALIASES:
            folder_key = ROOT_ALIASES[key]
            path = _known_folder(folder_key)
            label = ROOT_LABELS.get(folder_key, text)
        else:
            path = _expand_path(text)
            label = os.path.basename(path.rstrip("\\/")) or path
        if not path:
            continue
        norm = os.path.normcase(os.path.abspath(path))
        if norm in seen:
            continue
        if not os.path.isdir(path):
            logger.bind(tag=TAG).debug(f"文件查找根目录不存在，已跳过: {path}")
            continue
        seen.add(norm)
        roots.append({"label": label, "path": path})
    return roots


def _exclude_names(config: Dict[str, Any]) -> Set[str]:
    raw = config.get("exclude_dirs")
    names = list(DEFAULT_EXCLUDE_DIRS)
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, (list, tuple)):
        names.extend(str(item) for item in raw)
    return {str(name).strip().lower() for name in names if str(name).strip()}


def _int_config(config: Dict[str, Any], key: str, default: int, low: int = 1) -> int:
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value >= low else default


def _default_timeout(config: Dict[str, Any]) -> float:
    """单次查找的默认时间预算：整机模式放宽（配置里显式写 timeout 会覆盖它）。"""
    return DEFAULT_WHOLE_PC_TIMEOUT if _whole_pc(config) else DEFAULT_TIMEOUT


def _float_config(config: Dict[str, Any], key: str, default: float) -> float:
    try:
        value = float(config.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _files_supported(conn: "ConnectionHandler") -> bool:
    """客户端是否声明支持文件结果面板（桌宠在 hello.features 里声明 files）。"""
    return bool((getattr(conn, "features", None) or {}).get("files"))


# ── 关键词解析 ───────────────────────────────────────────────────────────────
def _split_tokens(text: str) -> List[str]:
    return [item for item in _TOKEN_SPLIT_RE.split(str(text or "")) if item]


def _parse_keyword(keyword: str) -> Tuple[List[str], Optional[float]]:
    """把用户口语拆成"必须全部命中的关键词" + 可选的时间下限。

    中文没有空格分词，所以做法是先整段剔除时间词与口语噪音词（"帮我找一下…文件"），
    剩下的部分再按空白/标点切成多个关键词。

    例："找一下上周的预算表" -> (["预算表"], now-14天)
    """
    raw = str(keyword or "").strip()
    min_mtime: Optional[float] = None
    for word, days in TIME_RULES:
        if word in raw:
            raw = raw.replace(word, " ")
            min_mtime = time.time() - days * 86400
            break

    # 长的先删，避免"找"先被删掉后"找一下"剩下"一下"这种残留
    for noise in sorted(NOISE_TOKENS, key=len, reverse=True):
        if noise in raw:
            raw = raw.replace(noise, " ")

    tokens: List[str] = []
    for token in _split_tokens(raw):
        cleaned = token.strip()
        if not cleaned or cleaned in NOISE_TOKENS:
            continue
        # 去掉词尾的"的"（"预算表的"）
        if len(cleaned) > 1 and cleaned.endswith("的"):
            cleaned = cleaned[:-1]
        if cleaned and cleaned not in NOISE_TOKENS:
            tokens.append(cleaned.lower())
    return tokens, min_mtime


def _parse_ext(ext: str) -> Set[str]:
    """把 ext 参数解析成扩展名集合（带点、小写）；无法识别时返回空集合=不限。"""
    raw = str(ext or "").strip().lower()
    if not raw or raw in ("不限", "任意", "所有", "全部", "any", "all"):
        return set()
    if raw in EXT_GROUPS:
        return set(EXT_GROUPS[raw])
    bare = raw.lstrip(".")
    if not bare:
        return set()
    if f".{bare}" in {e for group in EXT_GROUPS.values() for e in group}:
        return {f".{bare}"}
    # 未知扩展名也允许（用户可能找 .psd 这类没列进类别的）
    if re.fullmatch(r"[a-z0-9]{1,8}", bare):
        return {f".{bare}"}
    return set()


def _normalize_scope_text(raw: str) -> str:
    """把口语里的盘符与尾巴归一成路径写法：

    ``K盘xiaozhi文件夹下的`` → ``K:\\xiaozhi``；``k盘`` → ``K:\\``。
    这样"K盘 xxx 文件夹"这类真实说法才不会被当成"不认识的目录名"。
    """
    text = str(raw or "").strip()
    # "K盘" / "k 盘" → "K:\"
    text = re.sub(r"([a-zA-Z])\s*盘", lambda m: m.group(1).upper() + ":\\", text)
    # 去掉口语尾巴（可连续出现：文件夹 / 目录 / 里面 / 下 / 的 …）
    tail = re.compile(r"(?:文件夹|目录|文件|里面|里的|下面|下|里|中|的)+$")
    for _ in range(3):  # 最多剥三层，够用且不会把正常目录名吃掉
        stripped = tail.sub("", text)
        if stripped == text:
            break
        text = stripped
    return text.strip()


def _looks_like_path(raw: str) -> bool:
    """是不是"目录路径"写法（绝对路径 / 带盘符 / 含分隔符）。

    必须先判这个再判关键字，否则 `D:\\桌面1\\子目录` 会被关键字"桌面"截走；
    反过来，若不判就往下走，`下载夹` 这类口语会被 `_expand_path` 拼成
    "工作目录\\下载夹"（一个不存在的相对路径），最后报出让人摸不着头脑的
    "不在允许查找的范围里"。
    """
    value = str(raw or "").strip()
    if not value:
        return False
    if os.path.isabs(value) or _PATH_SEP_RE.search(value):
        return True
    return bool(re.match(r"^[a-zA-Z]:", value))


def _scope_hint(roots: List[Dict[str, str]], max_labels: int = 3) -> str:
    """把可用目录列成中文清单。

    - 语音播报/错误提示用默认值（目录多时只列几个，免得念一长串）；
    - 注入给大模型的工具描述用 ``max_labels=0``（要列全，否则它不知道还有哪些目录）。
    """
    labels = [root["label"] for root in roots]
    if not labels:
        return "任何目录"
    if max_labels > 0 and len(labels) > max_labels + 1:
        return "、".join(labels[:max_labels]) + f"等 {len(labels)} 个目录"
    return "、".join(labels)


def _match_drive(roots: List[Dict[str, str]], key: str) -> List[Dict[str, str]]:
    """「C盘 / d盘 / k:」这类说法 → 对应盘根（只有该盘在允许目录里才认）。"""
    match = re.fullmatch(r"([a-zA-Z])盘?", key)
    if not match:
        return []
    prefix = os.path.normcase(f"{match.group(1)}:\\")
    return [
        root for root in roots if os.path.normcase(root["path"]).startswith(prefix)
    ]


def _match_scope(
    roots: List[Dict[str, str]], scope: str
) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """按 scope 过滤根目录；返回 (命中的根目录, 错误信息)。"""
    raw = str(scope or "").strip()
    if not raw:
        return roots, None
    available = _scope_hint(roots)

    # 1) 路径写法：必须落在已配置根目录之内（含子目录与根目录自身）
    normalized = _normalize_scope_text(raw)
    if _looks_like_path(normalized):
        # 口语里常把目录名切开（"K盘 xiaozhi" → "K:\ xiaozhi"），所以先按原样试，
        # 不中再把空格压掉试一次；官方带空格的路径（Program Files）仍然走得通
        candidates = [normalized]
        squeezed = re.sub(r"\s+", "", normalized)
        if squeezed and squeezed != normalized:
            candidates.append(squeezed)
        for candidate in candidates:
            path = _expand_path(candidate)
            if _UNSAFE_PATH_RE.search(path):
                return [], "这个目录不安全"
            norm = os.path.normcase(os.path.abspath(path))
            for root in roots:
                root_norm = os.path.normcase(os.path.abspath(root["path"]))
                if norm == root_norm or norm.startswith(root_norm.rstrip("\\/") + os.sep):
                    return [{"label": root["label"], "path": path}], None
        return [], f"这个目录不在允许查找的范围里，目前只能找{available}。"

    # 2) 口语说法：包含匹配（"下载夹""我桌面上的""文档里面"都算）
    key = _compact(raw)

    # 2a) "整台电脑 / 全盘 / 所有盘" → 全部允许目录
    if any(word in key for word in WHOLE_PC_WORDS):
        return roots, None

    # 2b) "C盘 / D盘" → 对应盘根（不在允许目录里就落到下面的通用提示）
    drive_hits = _match_drive(roots, key)
    if drive_hits:
        return drive_hits, None

    for alias, folder_key in _SCOPE_KEYWORDS:
        if alias and _compact(alias) in key:
            wanted = _known_folder(folder_key)
            if wanted:
                norm = os.path.normcase(os.path.abspath(wanted))
                hits = [
                    r
                    for r in roots
                    if os.path.normcase(os.path.abspath(r["path"])) == norm
                ]
                if hits:
                    return hits, None
                # 已知文件夹本身不在清单里、但落在某个允许根目录之内（整机模式下
                # roots 就是各盘根）→ 直接落到该文件夹，否则"看看桌面上有哪些文件"
                # 会被误回"桌面这个目录还没配置"（2026-10-15 踩过，见台账 42 节）。
                if os.path.isdir(wanted):
                    for root in roots:
                        root_norm = os.path.normcase(os.path.abspath(root["path"]))
                        if norm.startswith(root_norm.rstrip("\\/") + os.sep):
                            label = ROOT_LABELS.get(folder_key, root["label"])
                            return [{"label": label, "path": wanted}], None
            return [], f"{raw}这个目录还没配置，目前只能找{available}。"

    # 3) 既不像路径、也不是已知目录名：别硬当路径校验，直接说清可用范围
    return [], f"只能在{available}里找文件。"


# ── 匹配与打分（精确 → 模糊分级） ────────────────────────────────────────────
# 命中等级：数值越大越可信。只要存在"字面命中"（≥ MATCH_COMPACT）的结果，
# 模糊结果就不掺进来，避免把噪音塞给用户。
MATCH_EXACT = 3  # 关键词按原样全部命中
MATCH_COMPACT = 2  # 忽略空格 / 全半角 / 大小写后全部命中（"工作簿 1" ↔ "工作簿1"）
MATCH_PARTIAL = 1  # 多个关键词时命中一半以上（"报告 预算" → 只命中"报告"）
MATCH_SIMILAR = 0  # 与某个关键词足够像（容忍错别字："工作薄1" → "工作簿1"）
MATCH_NONE = -1
# 模糊门槛：部分命中要覆盖一半以上关键词；相似命中分两档：
#   - 首字相同时 0.7（"工作薄1" → "工作簿1"）
#   - 首字不同时 0.8，且关键词至少 3 个字（"项目计划书" → "小智AI桌宠-项目计划表"，
#     文件名前缀常带项目/品牌名，不能因为首字不同就一刀切掉）
PARTIAL_COVERAGE = 0.5
SIMILARITY_THRESHOLD = 0.7
SIMILAR_WINDOW_THRESHOLD = 0.8

_STRIP_RE = re.compile(r"[\s\-_·．.,，。、;；:：/\\()（）\[\]【】<>《》]+")


def _compact(text: Any) -> str:
    """归一化紧凑串：NFKC（全角→半角）+ 小写 + 去掉空格与常见分隔符。

    用于"忽略了写法差异"的模糊匹配：``工作簿 1`` 与 ``工作簿-1`` 都归一成 ``工作簿1``。
    """
    value = unicodedata.normalize("NFKC", str(text or "")).lower()
    return _STRIP_RE.sub("", value)


def _similarity(left: str, right: str) -> float:
    return difflib.SequenceMatcher(None, left, right).ratio()


def _best_window_similarity(token: str, stem: str) -> float:
    """关键词与"文件名里等长的一小段"的最高相似度。

    整串比会把长文件名摊薄：``项目计划书`` 对 ``小智AI桌宠项目计划表`` 只有 0.5，
    但滑窗比到 ``项目计划表`` 那 5 个字就是 0.8 —— 这才是用户要的"差不多就是它"。
    """
    if not token or not stem:
        return 0.0
    if len(token) > len(stem):
        return _similarity(token, stem)
    return max(
        _similarity(token, stem[index : index + len(token)])
        for index in range(len(stem) - len(token) + 1)
    )


def _match_file(
    name_lower: str, tokens: List[str], compact_tokens: List[str]
) -> Tuple[int, int]:
    """判断文件名与关键词的匹配程度；返回 (等级, 位置加分)，不匹配是 (MATCH_NONE, 0)。

    等级越高越可信，位置加分用于同级排序（名字以关键词开头 > 出现在中间）。
    """
    if not tokens:
        return MATCH_COMPACT, 0

    stem = os.path.splitext(name_lower)[0]
    joined = "".join(tokens)
    # 注意：紧凑串要"先拆扩展名再归一化"——归一化会把点去掉，
    # 反过来做的话 stem 里会混进 "xlsx"，相似度就被摊薄了
    compact_name = _compact(name_lower)
    compact_stem = _compact(stem)

    # 1) 原样全命中（最可信）
    if all(token in name_lower for token in tokens):
        if stem == joined:
            return MATCH_EXACT, 30
        if stem.startswith(joined) or name_lower.startswith(joined):
            return MATCH_EXACT, 20
        if any(stem.startswith(token) for token in tokens):
            return MATCH_EXACT, 10
        return MATCH_EXACT, 0

    # 2) 紧凑串全命中：容忍空格 / 下划线 / 连字符 / 全半角差异
    compact_joined = "".join(compact_tokens)
    if compact_name and all(token in compact_name for token in compact_tokens):
        bonus = 20 if compact_stem.startswith(compact_joined) else 0
        return MATCH_COMPACT, bonus

    # 3) 多关键词时允许"部分命中"（半数以上，且至少有一个 ≥2 字的词命中）
    if len(tokens) >= 2:
        hits = [
            token
            for token, compact in zip(tokens, compact_tokens)
            if token in name_lower or (compact and compact in compact_name)
        ]
        if (
            hits
            and len(hits) / len(tokens) >= PARTIAL_COVERAGE
            and max(len(token) for token in hits) >= 2
        ):
            return MATCH_PARTIAL, 0

    # 4) 错别字/近义容忍：与文件名里的某一段足够像
    #    首字相同用 0.7 档（"工作薄1" → "工作簿1"）；
    #    首字不同要更严（0.8 档 + 至少 3 个字），避免"前缀不同"的名词互相误伤
    if compact_stem:
        for token in compact_tokens:
            if len(token) < 2:
                continue
            score = _best_window_similarity(token, compact_stem)
            if score < SIMILARITY_THRESHOLD:
                continue
            if token[0] == compact_stem[0] or (
                score >= SIMILAR_WINDOW_THRESHOLD and len(token) >= 3
            ):
                return MATCH_SIMILAR, 0

    return MATCH_NONE, 0


def _score(level: int, bonus: int) -> int:
    """把 (等级, 位置加分) 合成排序分：等级优先，同级看名字位置。"""
    return level * 1000 + bonus * 10


def _walk(roots, max_depth, exclude_names, deadline):
    """广度/深度混合遍历（用栈做 DFS），产出 (path, name, mtime, size)。"""
    stack: List[Tuple[str, int]] = [(root["path"], 0) for root in reversed(roots)]
    while stack:
        if time.monotonic() > deadline:
            return
        dirpath, depth = stack.pop()
        try:
            entries = list(os.scandir(dirpath))
        except (PermissionError, FileNotFoundError, NotADirectoryError, OSError):
            continue
        for entry in entries:
            if time.monotonic() > deadline:
                return
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth + 1 > max_depth:
                        continue
                    name = entry.name
                    if name.startswith(".") or name.lower() in exclude_names:
                        continue
                    stack.append((entry.path, depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    name = entry.name
                    if name.lower() in SKIP_FILE_NAMES:
                        continue
                    # Office 的临时锁文件（~$XXX.xlsx / ~WRL*.tmp）不是用户要找的文件
                    if name.startswith("~$"):
                        continue
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    attrs = getattr(stat, "st_file_attributes", 0)
                    if attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM):
                        continue
                    yield entry.path, name, stat.st_mtime, stat.st_size
            except (PermissionError, OSError):
                continue


def _search_sync(
    roots: List[Dict[str, str]],
    tokens: List[str],
    exts: Set[str],
    min_mtime: Optional[float],
    max_depth: int,
    max_results: int,
    timeout: float,
    exclude_names: Set[str],
) -> Tuple[List[Dict[str, Any]], bool, bool]:
    """在线程里跑的文件遍历；返回 (结果列表, 是否超时, 是否只靠模糊命中)。"""
    deadline = time.monotonic() + timeout
    compact_tokens = [_compact(token) for token in tokens]
    results: List[Tuple[int, float, int, Dict[str, Any]]] = []
    truncated = False
    # 命中数够了就收工：既保证排序样本够用，也不会把大目录整个走完
    collect_limit = max(max_results * 20, 200)

    for path, name, mtime, size in _walk(roots, max_depth, exclude_names, deadline):
        if min_mtime is not None and mtime < min_mtime:
            continue
        lower = name.lower()
        _stem, dot_ext = os.path.splitext(lower)
        if exts and dot_ext not in exts:
            continue
        level, bonus = _match_file(lower, tokens, compact_tokens)
        if level == MATCH_NONE:
            continue
        item = {
            "name": name,
            "path": path,
            "dir": os.path.dirname(path),
            "ext": dot_ext,
            "size": size,
            "mtime": mtime,
        }
        results.append((_score(level, bonus), mtime, level, item))
        if len(results) >= collect_limit:
            break

    truncated = time.monotonic() > deadline

    results.sort(key=lambda row: (-row[0], -row[1]))
    # 有字面命中时就不掺模糊结果：模糊只用来"找不到字面结果时救急"
    best_level = results[0][2] if results else MATCH_NONE
    fuzzy = bool(results) and best_level < MATCH_COMPACT
    if not fuzzy:
        results = [row for row in results if row[2] >= MATCH_COMPACT]

    items: List[Dict[str, Any]] = []
    for score, _mtime, level, item in results[:max_results]:
        item["score"] = score
        item["match"] = level
        items.append(item)
    return items, truncated, fuzzy


# ── 结果展示 ─────────────────────────────────────────────────────────────────
def _human_size(size: int) -> str:
    try:
        value = float(size)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def _format_time(mtime: float) -> str:
    try:
        return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


def _root_label_for(roots: List[Dict[str, str]], path: str) -> str:
    """结果属于哪个根目录（用于播报"桌面上的 xxx"）。"""
    norm = os.path.normcase(os.path.abspath(path))
    best = ""
    label = ""
    for root in roots:
        root_norm = os.path.normcase(os.path.abspath(root["path"]))
        if norm.startswith(root_norm.rstrip("\\/") + os.sep) or norm == root_norm:
            if len(root_norm) > len(best):
                best = root_norm
                label = root["label"]
    return label


def _panel_items(items: List[Dict[str, Any]], roots) -> List[Dict[str, Any]]:
    """下发给客户端的结果（去掉内部字段，补上展示用的中文信息）。"""
    payload = []
    for item in items:
        payload.append(
            {
                "name": item["name"],
                "path": item["path"],
                "dir": item["dir"],
                "where": _root_label_for(roots, item["path"]),
                "size": _human_size(item.get("size", 0)),
                "mtime": _format_time(item.get("mtime", 0)),
            }
        )
    return payload


def _display_stem(name: str) -> str:
    """播报用的文件名：去掉扩展名（避免 TTS 念"点 p d f"），并把备份文件区分开。

    ``小智AI桌宠-项目计划表.xlsx.bak`` 若只去掉一层会念成"…项目计划表.xlsx"，
    和主文件几乎重名，用户分不清"第一个/第二个" → 念成"…项目计划表（备份）"。
    """
    stem, ext = os.path.splitext(str(name or ""))
    if ext.lower() in BACKUP_EXTS:
        base = os.path.splitext(stem)[0] or stem
        return f"{base}（备份）"
    return stem or str(name or "")


def _summary(
    items: List[Dict[str, Any]],
    roots,
    keyword: str,
    ext: str,
    fuzzy: bool = False,
    truncated: bool = False,
) -> str:
    """播报用的中文摘要：只念文件名，最多念 3 个。模糊命中时如实说明是"接近的名字"。"""
    parts = []
    for item in items[:3]:
        stem = _display_stem(item["name"])
        where = _root_label_for(roots, item["path"])
        parts.append(f"{where}的{stem}" if where else stem)
    names = "、".join(parts)
    more = len(items) - len(parts)
    tail = f"，还有 {more} 个" if more > 0 else ""
    kind = _ext_label(ext) or "文件"
    if fuzzy:
        verb = f"没有完全一致的，名字接近的有 {len(items)} 个{kind}：{names}{tail}"
    else:
        verb = f"找到 {len(items)} 个{kind}：{names}{tail}"
    if len(items) > 1:
        verb += "。说“打开第一个”就能打开"
    # 整机模式走不完所有目录时如实说明，免得用户以为"就这些"
    if truncated:
        verb += "；范围较大，可能还没找全"
    return verb + "。"


async def _push_results(
    conn: "ConnectionHandler",
    query: str,
    items: List[Dict[str, Any]],
    roots,
    fuzzy: bool = False,
) -> None:
    """把结果下发给桌宠，让它在屏幕上列出可点击的列表。"""
    websocket = getattr(conn, "websocket", None)
    if not websocket:
        return
    payload = {
        "type": "files",
        "action": "list",
        "query": query,
        "count": len(items),
        "fuzzy": bool(fuzzy),
        "items": _panel_items(items, roots),
    }
    try:
        await websocket.send(json.dumps(payload, ensure_ascii=False))
    except Exception as error:  # 客户端断开/不支持时不影响语音播报
        logger.bind(tag=TAG).warning(
            f"下发文件结果失败 | {type(error).__name__}: {error}"
        )


# ── 打开文件 ─────────────────────────────────────────────────────────────────
def _is_within(path: str, roots: List[Dict[str, str]]) -> bool:
    norm = os.path.normcase(os.path.abspath(path))
    for root in roots:
        root_norm = os.path.normcase(os.path.abspath(root["path"]))
        if norm == root_norm or norm.startswith(root_norm.rstrip("\\/") + os.sep):
            return True
    return False


def _ordinal_to_int(text: str) -> Optional[int]:
    text = text.strip()
    if text.isdigit():
        return int(text)
    if len(text) == 1:
        return _CN_DIGITS.get(text)
    if text.startswith("十"):
        return 10 + (_CN_DIGITS.get(text[1:], 0) if len(text) > 1 else 0)
    if "十" in text:
        tens, ones = text.split("十", 1)
        return _CN_DIGITS.get(tens, 0) * 10 + _CN_DIGITS.get(ones, 0)
    return None


def _resolve_ref(
    file_ref: str, items: List[Dict[str, Any]], roots
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """把"第一个 / 报告.pdf / 绝对路径"解析成一条结果。"""
    raw = str(file_ref or "").strip()
    if not raw:
        return None, "没说是哪个文件"

    # 1) 序号引用：第一个 / 第 2 个 / 3
    match = _ORDINAL_RE.search(raw)
    ordinal = None
    if match:
        ordinal = _ordinal_to_int(match.group(1))
    elif raw.isdigit() or (len(raw) == 1 and raw in _CN_DIGITS):
        ordinal = _ordinal_to_int(raw)
    if ordinal is not None:
        if not items:
            return None, "还没有查找结果，先说要找什么文件"
        if not 1 <= ordinal <= len(items):
            return None, f"只有 {len(items)} 个结果，没有第 {ordinal} 个"
        return items[ordinal - 1], None

    # 2) 绝对路径：必须在允许目录内
    if len(raw) > 2 and (os.path.isabs(raw) or _PATH_SEP_RE.search(raw)):
        path = _expand_path(raw)
        if _UNSAFE_PATH_RE.search(path):
            return None, "这个路径不安全"
        if not _is_within(path, roots):
            return None, "这个文件不在允许打开的目录里"
        if not os.path.exists(path):
            return None, "这个文件不存在"
        return {"name": os.path.basename(path), "path": path}, None

    # 3) 名字引用：在最近结果里模糊匹配
    if not items:
        return None, "还没有查找结果，先说要找什么文件"
    target = raw.lower()
    stem = os.path.splitext(target)[0]
    for item in items:
        if item["name"].lower() == target:
            return item, None
    for item in items:
        lowered = item["name"].lower()
        if stem and stem in lowered:
            return item, None
    for item in items:
        if any(token and token in item["name"].lower() for token in _split_tokens(target)):
            return item, None
    return None, f"刚才的结果里没有“{raw}”"


def _open_path(path: str, reveal: bool) -> None:
    """用系统默认方式打开文件，或在资源管理器里选中它。"""
    if reveal:
        if IS_WINDOWS:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            return
        folder = os.path.dirname(path)
        subprocess.Popen(["xdg-open", folder])
        return
    if IS_WINDOWS:
        os.startfile(path)  # noqa: S606 - 路径已做过白名单校验
        return
    subprocess.Popen(["xdg-open", path])


# ── 工具实现 ─────────────────────────────────────────────────────────────────
@register_function("search_files", SEARCH_FILES_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def search_files(
    conn: "ConnectionHandler",
    keyword: str = "",
    ext: str = "",
    scope: str = "",
) -> ActionResponse:
    """按文件名在允许目录里查找，命中后下发结果面板并播报摘要。"""
    started_at = time.time()
    log = conn.logger.bind(tag=TAG)
    config = _plugin_config(conn)

    whole_pc = _whole_pc(config)
    roots = _resolve_roots(config)
    if not roots:
        return ActionResponse(
            Action.RESPONSE, None, "还没配置可以查找的目录，先在配置里加上桌面或文档。"
        )

    scoped, error = _match_scope(roots, scope)
    if error:
        # 被拒也要留痕：否则"为什么说找不到这个目录"在日志里查不到（踩过）
        log.warning(
            f"search_files | scope 未通过 | scope={scope!r} | "
            f"可用目录={[r['label'] for r in roots]} | 提示={error!r}"
        )
        return ActionResponse(Action.RESPONSE, None, error)
    if not scoped:
        return ActionResponse(Action.RESPONSE, None, "没有可以查找的目录。")

    tokens, min_mtime = _parse_keyword(keyword)
    exts = _parse_ext(ext)
    if not tokens and not exts:
        return ActionResponse(
            Action.RESPONSE, None, "告诉我文件名或者要找什么类型的文件就好。"
        )

    # low=0：max_depth=0 是合法值（"不查子文件夹"，只在选中目录的第一层找）
    max_depth = _int_config(config, "max_depth", DEFAULT_MAX_DEPTH, low=0)
    max_results = _int_config(config, "max_results", DEFAULT_MAX_RESULTS)
    # 整机模式范围大得多，时间预算默认放宽（配置里显式写 timeout 则以配置为准）
    timeout = _float_config(config, "timeout", _default_timeout(config))
    exclude_names = _exclude_names(config)

    try:
        items, truncated, fuzzy = await asyncio.wait_for(
            asyncio.to_thread(
                _search_sync,
                scoped,
                tokens,
                exts,
                min_mtime,
                max_depth,
                max_results,
                timeout,
                exclude_names,
            ),
            timeout=timeout + 5,
        )
    except asyncio.TimeoutError:
        items, truncated, fuzzy = [], True, False
    except Exception as error:
        log.error(f"文件查找失败 | keyword={keyword!r} | {type(error).__name__}: {error}")
        return ActionResponse(Action.RESPONSE, None, "查找文件时出错了，稍后再试试。")

    query = " ".join(part for part in (keyword, ext, scope) if part).strip()
    if not items:
        # 无结果时必须说明"只在哪些目录里找了"：用户说"xx 目录下明明有"时，
        # 才能一眼看出是"范围不含那里"，而不是"文件不存在"（踩过的坑）
        where = f"在{_scope_hint(scoped)}里" if scoped else ""
        what = keyword.strip() or (ext.strip() or "")
        log.info(f"search_files | 无结果 | query={query!r} | 整机={whole_pc}")
        tail = "（整机范围较大，可能还没找全，可以换个更具体的名字再找）" if truncated else ""
        return ActionResponse(
            Action.RESPONSE,
            None,
            f"{where}没找到名字带“{what}”的文件。{tail}",
        )

    _remember_results(conn, items)
    if _files_supported(conn):
        await _push_results(conn, query, items, scoped, fuzzy)

    text = _summary(items, scoped, keyword, ext, fuzzy, truncated)
    log.info(
        f"search_files | 命中 {len(items)} 个 | query={query!r} | "
        f"耗时={time.time() - started_at:.2f}s | "
        f"面板={'已下发' if _files_supported(conn) else '未下发'}"
        + (" | 模糊匹配" if fuzzy else "")
        + (" | 目录未走完（已超时/达上限）" if truncated else "")
    )
    return ActionResponse(
        Action.RESPONSE, f"找到 {len(items)} 个文件" + ("（模糊）" if fuzzy else ""), text
    )


@register_function("open_file", OPEN_FILE_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def open_file(
    conn: "ConnectionHandler", file_ref: str = "", reveal: bool = False
) -> ActionResponse:
    """打开最近一次查找结果里的文件（或允许目录内的绝对路径）。"""
    log = conn.logger.bind(tag=TAG)
    config = _plugin_config(conn)
    if not config.get("allow_open", True):
        return ActionResponse(Action.RESPONSE, None, "打开文件的功能现在是关着的。")

    roots = _resolve_roots(config)
    items = _last_results(conn)
    item, error = _resolve_ref(file_ref, items, roots)
    if error or not item:
        log.info(f"open_file | 未解析到文件 | file_ref={file_ref!r} | {error}")
        return ActionResponse(Action.RESPONSE, None, error or "没找到要打开的文件。")

    path = item["path"]
    if not os.path.exists(path):
        return ActionResponse(Action.RESPONSE, None, f"{item['name']} 已经不在原来位置了。")

    try:
        await asyncio.to_thread(_open_path, path, bool(reveal))
    except Exception as error:
        log.error(f"打开文件失败 | path={path} | {type(error).__name__}: {error}")
        return ActionResponse(Action.RESPONSE, None, f"打开{item['name']}时出错了。")

    text = (
        f"已经打开{item['name']}所在的文件夹" if reveal else f"已经帮你打开{item['name']}"
    )
    log.info(f"open_file | path={path} | reveal={bool(reveal)}")
    return ActionResponse(Action.RESPONSE, f"打开 {path}", text)
