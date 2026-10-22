"""保存路径解析 + 记录文件落盘（txt / docx / xlsx）。

为什么单独一个模块
------------------
语音说「把刚才查到的记下来」时，真正难的不是写文件，而是**存到哪儿**：

1. 用户没说路径 → 存到 Windows 的「桌面」已知文件夹。不能拼 ``%USERPROFILE%\\Desktop``：
   本机桌面被重定向到了 ``D:\\桌面1``，``SHGetKnownFolderPath`` 才能拿到真实路径；
2. 用户明确说了路径 → ``resolve_target`` 只用用户给的路径，**不回退默认目录**
   （回退会让人找不到文件，比直接报错更糟）；
3. 默认文件名 ``小智记录-YYYYMMDD-HHMMSS.txt/.docx/.xlsx``，同一秒内不重名靠时间戳；
4. 记事本保存不能走「新建空白文档再 Ctrl+S」——那会弹「另存为」对话框，
   语音场景没人去点它。所以：**先按目标路径建空文件 → notepad.exe 打开它 →
   粘贴正文 → Ctrl+S**，这样保存的是已有路径，不会弹框；
5. Word / Excel 走 COM（``office_com``），只新建、不关闭用户文档。

设置
----
「默认保存位置」存在 ``data/desktop_save.json``（**不重写 .config.yaml**，同
``file_search_catalog`` / ``local_app_catalog`` 的约定）。文件不存在 = 用系统桌面。
设置窗口保存后立刻生效（每次落盘都重新读文件），不需要重启服务端。
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional

from config.logger import setup_logging
from plugins_func.functions import local_app, office_com

TAG = __name__
logger = setup_logging()

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SETTINGS_FILE = os.path.join(SERVER_ROOT, "data", "desktop_save.json")
_LOCK = threading.RLock()

DEFAULT_STEM = "小智记录"
KIND_EXT: Dict[str, str] = {"txt": ".txt", "word": ".docx", "excel": ".xlsx"}
# 用户口语里的目录名 → 已知文件夹关键字（“存到桌面”这类说法不该落到服务端工作目录）
_FOLDER_ALIASES = {
    "桌面": "desktop",
    "桌面上": "desktop",
    "desktop": "desktop",
    "文档": "documents",
    "我的文档": "documents",
    "documents": "documents",
    "下载": "downloads",
    "下载目录": "downloads",
    "downloads": "downloads",
    "图片": "pictures",
    "音乐": "music",
    "视频": "videos",
}
_KIND_ALIASES = {
    "txt": "txt",
    "text": "txt",
    "note": "txt",
    "notepad": "txt",
    "txt文件": "txt",
    "记事本": "txt",
    "文本": "txt",
    "word": "word",
    "doc": "word",
    "docx": "word",
    "文档": "word",
    "excel": "excel",
    "xls": "excel",
    "xlsx": "excel",
    "表格": "excel",
    "工作表": "excel",
}

# 记事本保存：等窗口 / 等文件真正落盘的时间预算（秒）
NOTEPAD_WINDOW_TIMEOUT = 8.0
NOTEPAD_SAVE_TIMEOUT = 1.8
# 粘贴重试次数：窗口出现 ≠ 编辑区就绪，第一次粘贴可能被丢掉
NOTEPAD_PASTE_RETRY = 2
VK_CONTROL = 0x11
VK_A = 0x41
VK_S = 0x53


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_kind(raw: Any) -> str:
    """把 ``txt / word / docx / 表格`` 之类归一化成三种格式之一。"""
    text = str(raw or "").strip().lower()
    if not text:
        return "txt"
    if text in _KIND_ALIASES:
        return _KIND_ALIASES[text]
    raise ValueError(f"不认识的保存格式：{raw}（只支持 txt / word / excel）")


def _ext(kind: str) -> str:
    return KIND_EXT[normalize_kind(kind)]


# ── 设置 ────────────────────────────────────────────────────────────────────
def _empty_settings() -> Dict[str, Any]:
    return {"version": 1, "updated_at": "", "save_dir": None}


def load_settings() -> Dict[str, Any]:
    """读设置文件；没设置过 / 文件损坏都返回 ``save_dir=None``（= 用系统桌面）。"""
    with _LOCK:
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8-sig") as handle:
                raw = json.load(handle)
        except (OSError, ValueError, TypeError):
            return _empty_settings()

    data = _empty_settings()
    if not isinstance(raw, dict):
        return data
    save_dir = raw.get("save_dir")
    if isinstance(save_dir, str) and save_dir.strip():
        data["save_dir"] = save_dir.strip()
    data["updated_at"] = str(raw.get("updated_at") or "")
    return data


def _write_settings(data: Dict[str, Any]) -> None:
    payload = {
        "version": 1,
        "updated_at": data.get("updated_at") or _now(),
        "save_dir": data.get("save_dir"),
    }
    with _LOCK:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        temporary = f"{SETTINGS_FILE}.{os.getpid()}.tmp"
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, SETTINGS_FILE)


def default_save_dir() -> str:
    """系统「桌面」的真实路径（跟随本机把桌面重定向到 D:\\桌面1 这类设置）。"""
    try:
        from plugins_func.functions.file_search import _known_folder

        path = _known_folder("desktop")
    except Exception as error:  # pragma: no cover - 只在极端环境失败
        logger.bind(tag=TAG).warning(f"取桌面路径失败，改用用户目录: {error}")
        path = None
    if path and os.path.isdir(path):
        return path
    return os.path.join(os.path.expanduser("~"), "Desktop")


def effective_save_dir() -> str:
    """当前生效的默认保存目录：设置窗口选过就用它，没选过用系统桌面。"""
    configured = load_settings().get("save_dir")
    if configured:
        return str(configured)
    return default_save_dir()


def default_filename(kind: str = "txt", when: Optional[float] = None) -> str:
    stamp = datetime.fromtimestamp(when if when is not None else time.time())
    return f"{DEFAULT_STEM}-{stamp.strftime('%Y%m%d-%H%M%S')}{_ext(kind)}"


def _known_folder_path(keyword: str) -> str:
    try:
        from plugins_func.functions.file_search import _known_folder as resolve

        path = resolve(keyword)
    except Exception:
        return ""
    return path if path and os.path.isdir(path) else ""


def _expand_folder_alias(raw: str) -> str:
    """把“桌面 / 桌面\\记录.txt / 文档\\”这类口语目录换成真实已知文件夹路径。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    match = re.match(r"^([^\\/]+)(?:[\\/](.*))?$", text)
    if not match:
        return ""
    keyword = _FOLDER_ALIASES.get(match.group(1).strip().lower())
    if not keyword:
        return ""
    base = _known_folder_path(keyword)
    if not base:
        return ""
    rest = (match.group(2) or "").strip().strip("\\/")
    if not rest:
        return base + os.sep
    return os.path.join(base, rest)


def resolve_target(path: str = "", kind: str = "txt", when: Optional[float] = None) -> str:
    """算出最终落盘路径。

    - ``path`` 为空 → 默认目录 + 默认文件名；
    - ``path`` 是目录（以分隔符结尾或本来就存在）→ 目录 + 默认文件名；
    - ``path`` 没有后缀 → 补上该格式的后缀；
    - 其它情况 → **原样使用**用户给的路径，绝不替换成默认目录。
    """
    kind = normalize_kind(kind)
    raw = os.path.expandvars(os.path.expanduser(str(path or "").strip()))
    if raw:
        # “存到桌面/文档”这类口语先换成真实已知文件夹
        raw = _expand_folder_alias(raw) or raw
        if raw.endswith(("\\", "/")) or os.path.isdir(raw):
            target = os.path.join(raw, default_filename(kind, when))
        elif not os.path.dirname(raw.rstrip("\\/")):
            # 只给了文件名（可能没后缀）：放进默认保存目录，
            # 否则会落到服务端工作目录，用户根本找不到
            name = raw if os.path.splitext(raw)[1] else raw + KIND_EXT[kind]
            target = os.path.join(effective_save_dir(), name)
        elif not os.path.splitext(os.path.basename(raw))[1]:
            target = raw + KIND_EXT[kind]
        else:
            target = raw
    else:
        target = os.path.join(effective_save_dir(), default_filename(kind, when))
    # 统一分隔符（用户可能写 "D:\资料\"，join 后会留下双反斜杠）
    return os.path.normpath(target)


def save_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    """保存「默认保存位置」；空值表示恢复默认（系统桌面）。"""
    if not isinstance(payload, dict):
        raise ValueError("请求内容不是有效设置")
    raw = payload.get("save_dir")
    value: Optional[str]
    if raw is None or not str(raw).strip():
        value = None
    else:
        value = os.path.expandvars(os.path.expanduser(str(raw).strip()))
        if not os.path.isabs(value):
            raise ValueError("请给一个完整的文件夹路径，例如 D:\\资料")
        try:
            os.makedirs(value, exist_ok=True)
        except OSError as error:
            raise ValueError(f"这个文件夹不可用：{error}") from error
        if not os.path.isdir(value):
            raise ValueError("这个路径不是文件夹")
    data = load_settings()
    data["save_dir"] = value
    data["updated_at"] = _now()
    _write_settings(data)
    logger.bind(tag=TAG).info(
        f"默认保存位置已更新 | save_dir={value or '（默认桌面）'}"
    )
    return describe_settings()


def describe_settings() -> Dict[str, Any]:
    """给设置窗口 / HTTP 接口的完整状态。"""
    settings = load_settings()
    configured = settings.get("save_dir")
    effective = str(configured) if configured else default_save_dir()
    fallback = default_save_dir()
    return {
        "ok": True,
        "save_dir": str(configured or ""),
        "configured": bool(configured),
        "default_dir": fallback,
        "effective_dir": effective,
        "effective_dir_exists": os.path.isdir(effective),
        "settings_file": SETTINGS_FILE,
        "updated_at": settings.get("updated_at") or "",
        "examples": {kind: default_filename(kind) for kind in KIND_EXT},
        "hint": (
            "用户明确说路径时以用户为准；这里只决定没说路径时存到哪个文件夹。"
        ),
    }


# ── 文本读写 ────────────────────────────────────────────────────────────────
def write_text_file(path: str, text: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # utf-8-sig：记事本打开中文不会乱码（无 BOM 的 UTF-8 在旧版本记事本里会显示成 ANSI）
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        handle.write(str(text or ""))


def read_text_file(path: str, limit: int = 4000) -> str:
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def _touch_empty_file(path: str) -> None:
    with open(path, "w", encoding="utf-8-sig"):
        pass


# ── 记事本保存 ──────────────────────────────────────────────────────────────
def _user32():
    return getattr(local_app, "_user32", None)


def _foreground_hwnd() -> int:
    user32 = _user32()
    if not user32:
        return 0
    try:
        function = user32.GetForegroundWindow
        function.restype = ctypes.c_void_p
        return int(function() or 0)
    except Exception:
        return 0


def _window_title(hwnd: int) -> str:
    user32 = _user32()
    if not user32 or not hwnd:
        return ""
    try:
        user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        length = int(user32.GetWindowTextLengthW(hwnd))
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value
    except Exception:
        return ""


def _notepad_windows() -> list:
    handles = []
    for proc in local_app._find_processes(["notepad.exe"]):
        try:
            handles.extend(local_app._windows_of_pid(proc.pid))
        except Exception:
            continue
    return handles


def _wait_notepad_window(before: list, filename: str, timeout: float) -> int:
    """等记事本窗口出现，优先挑标题里带本次文件名的那个（Win11 会在同一窗口开新标签）。"""
    stem = os.path.splitext(filename)[0]
    filename_key = filename.lower()
    stem_key = stem.lower()
    deadline = time.time() + max(0.5, timeout)
    while True:
        windows = _notepad_windows()
        if windows:
            new_windows = [hwnd for hwnd in windows if hwnd not in before]
            for hwnd in new_windows + windows:
                title = _window_title(hwnd).lower()
                if filename_key in title or (stem_key and stem_key in title):
                    return int(hwnd)
            if new_windows:
                return int(new_windows[0])
            if time.time() >= deadline:
                return int(windows[0])
        if time.time() >= deadline:
            return 0
        time.sleep(0.25)


def _focus_window(hwnd: int, timeout: float = 2.0) -> bool:
    if not hwnd:
        return False
    deadline = time.time() + timeout
    while True:
        if _foreground_hwnd() == int(hwnd):
            return True
        try:
            local_app._focus_window(hwnd)
        except Exception:
            pass
        time.sleep(0.15)
        if _foreground_hwnd() == int(hwnd):
            return True
        if time.time() >= deadline:
            return False


def _ctrl_key(virtual_key: int) -> None:
    user32 = _user32()
    if not user32:
        return
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    try:
        local_app._press_key(virtual_key)
    finally:
        user32.keybd_event(VK_CONTROL, 0, local_app.KEYEVENTF_KEYUP, 0)


def _ctrl_s() -> None:
    _ctrl_key(VK_S)


def _wait_file_text(path: str, text: str, timeout: float) -> bool:
    probe = ""
    for line in str(text or "").splitlines():
        if line.strip():
            probe = line.strip()[:24]
            break
    deadline = time.time() + max(0.3, timeout)
    while True:
        content = read_text_file(path, limit=20000)
        if not probe or probe in content:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.25)


def _save_via_notepad(target: str, text: str) -> Dict[str, Any]:
    """先建空文件 → 记事本打开 → 粘贴 → Ctrl+S；保存失败时兜底直接写盘。"""
    try:
        _touch_empty_file(target)
    except OSError as error:
        return _failure(target, "txt", f"建文件失败：{error}")

    before = _notepad_windows()
    opened = local_app._open_in_notepad(target)
    typed = False
    saved = False
    hwnd = 0
    if opened:
        hwnd = _wait_notepad_window(before, os.path.basename(target), NOTEPAD_WINDOW_TIMEOUT)
        if hwnd and _focus_window(hwnd):
            # 窗口出现 ≠ 编辑区就绪：Win11 记事本先出窗口、再挂载编辑区，
            # 这期间粘贴会被丢掉（踩过：Ctrl+S 存下空文件），所以稍等并允许重试。
            time.sleep(0.45)
            for _attempt in range(NOTEPAD_PASTE_RETRY):
                # 先全选再粘贴：重试时直接覆盖，不会出现两遍正文
                _ctrl_key(VK_A)
                time.sleep(0.12)
                if not local_app._paste_and_submit(text, False):
                    time.sleep(0.4)
                    continue
                typed = True
                time.sleep(0.5)
                # Ctrl+S 也可能被窗口初始化吞掉，所以同一份内容再补一次
                for _save_try in range(2):
                    _ctrl_s()
                    if _wait_file_text(target, text, NOTEPAD_SAVE_TIMEOUT):
                        saved = True
                        break
                    _focus_window(hwnd)
                    time.sleep(0.15)
                if saved:
                    break

    if typed and saved:
        logger.bind(tag=TAG).info(
            f"记事本保存成功 | file={target} | chars={len(text)} | hwnd={hwnd}"
        )
        return {
            "ok": True,
            "kind": "txt",
            "path": target,
            "via": "notepad",
            "typed": True,
            "saved": True,
            "message": f"已经在记事本里新建并保存到 {target}。",
        }

    # 兜底：记事本那条链路（焦点/剪贴板/保存）任何一环失败，都不能让用户白说一遍
    try:
        write_text_file(target, text)
    except OSError as error:
        return _failure(target, "txt", f"写文件失败：{error}")

    if typed:
        logger.bind(tag=TAG).warning(
            f"记事本 Ctrl+S 未生效，已直接落盘 | file={target} | hwnd={hwnd}"
        )
        message = f"内容已经存到 {target}（记事本里也打开了这个文件）。"
    else:
        logger.bind(tag=TAG).warning(
            f"记事本没打开或没输入成功，已直接落盘 | file={target} | opened={opened} | hwnd={hwnd}"
        )
        message = f"内容已经存到 {target}；记事本窗口里没输入成功，你可以在文件里查看。"
    return {
        "ok": True,
        "kind": "txt",
        "path": target,
        "via": "direct",
        "typed": typed,
        "saved": saved,
        "message": message,
    }


# ── 对外入口 ────────────────────────────────────────────────────────────────
def _failure(path: str, kind: str, message: str) -> Dict[str, Any]:
    return {"ok": False, "kind": kind, "path": path, "via": "", "message": message}


def save_text(
    kind: str = "txt",
    text: str = "",
    path: str = "",
    when: Optional[float] = None,
) -> Dict[str, Any]:
    """把一段文字存成 txt / docx / xlsx 并（尽量）在对应软件里打开。

    阻塞调用（记事本要等窗口、Office 走 COM），异步侧请用 ``asyncio.to_thread``。
    返回值统一为 ``{"ok", "kind", "path", "via", "message"}``，不抛业务异常。
    """
    try:
        kind = normalize_kind(kind)
    except ValueError as error:
        return _failure(str(path or ""), "txt", str(error))

    content = str(text or "")
    if not content.strip():
        return _failure(str(path or ""), kind, "要保存的内容是空的。")

    try:
        target = resolve_target(path, kind, when)
    except ValueError as error:
        return _failure(str(path or ""), kind, str(error))

    try:
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
    except OSError as error:
        return _failure(target, kind, f"创建文件夹失败：{error}")

    if kind == "txt":
        return _save_via_notepad(target, content)

    try:
        if kind == "word":
            result = office_com.save_word(target, content)
        else:
            result = office_com.save_excel(target, content)
    except office_com.OfficeError as error:
        return _failure(target, kind, str(error))

    result.setdefault("ok", True)
    result["kind"] = kind
    result["typed"] = True
    result["saved"] = bool(result.get("ok"))
    logger.bind(tag=TAG).info(
        f"Office 保存 | kind={kind} | file={result.get('path') or target}"
    )
    return result


__all__ = [
    "DEFAULT_STEM",
    "KIND_EXT",
    "SETTINGS_FILE",
    "default_filename",
    "default_save_dir",
    "describe_settings",
    "effective_save_dir",
    "load_settings",
    "normalize_kind",
    "read_text_file",
    "resolve_target",
    "save_settings",
    "save_text",
    "write_text_file",
]
