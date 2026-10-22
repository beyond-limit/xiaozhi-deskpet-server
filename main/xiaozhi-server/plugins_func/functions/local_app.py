"""本机（电脑）软件调用插件。

用途：让语音助手按用户要求扫描软件，并拉起、切换、关闭已登记的软件。

设计要点：

1. **白名单**：只有基础配置、用户手动登记或用户明确要求扫描后发现的软件才允许操作；
   工具不接受任意命令行 / 任意路径，避免把整机控制权直接交给大模型。
2. **按需扫描**：不会后台扫描；只有调用 ``scan_local_apps`` 时才全量重扫并替换旧扫描结果。
3. **日志留痕**：每次调用都记录软件名、解析出的路径、进程与窗口句柄。
4. **只在 Windows 上做窗口级操作**，其它平台退化为启动 / 结束进程。
5. 所有阻塞操作都通过 ``asyncio.to_thread`` 放到线程里执行，不阻塞事件循环。
"""

import asyncio
import os
import re
import shutil
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

import psutil

from config.logger import setup_logging
from plugins_func.functions.local_app_catalog import (
    get_effective_apps,
    refresh_connections,
    replace_scanned_apps,
    scan_installed_apps,
)
from plugins_func.register import Action, ActionResponse, ToolType, register_function

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

IS_WINDOWS = sys.platform.startswith("win")

# ── Win32 常量 ───────────────────────────────────────────────────────────────
WM_CLOSE = 0x0010
VK_CONTROL = 0x11
VK_V = 0x56
VK_RETURN = 0x0D
KEYEVENTF_KEYUP = 0x0002
SW_RESTORE = 9
GW_OWNER = 4
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

# 启动后等待进程 / 窗口出现的最长时间（秒）
DEFAULT_WINDOW_TIMEOUT = 12
# 关闭时等待进程退出的最长时间（秒）
CLOSE_WAIT_TIMEOUT = 6
# 记事本记录（write_note）的默认落盘文件，相对服务端工作目录；
# 可在 data/.config.yaml 的 plugins.local_app.note_file 覆盖
DEFAULT_NOTE_FILE = "data/notes/xiaozhi-notes.txt"

if IS_WINDOWS:
    import ctypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _user32.GetWindowThreadProcessId.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    _user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    _user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    _user32.IsWindowVisible.restype = ctypes.c_bool
    _user32.IsIconic.argtypes = [ctypes.c_void_p]
    _user32.IsIconic.restype = ctypes.c_bool
    _user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    _user32.GetWindow.restype = ctypes.c_void_p
    _user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _user32.ShowWindow.restype = ctypes.c_bool
    _user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    _user32.SetForegroundWindow.restype = ctypes.c_bool
    _user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
    _user32.BringWindowToTop.restype = ctypes.c_bool
    _user32.AttachThreadInput.argtypes = [
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_bool,
    ]
    _user32.PostMessageW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    _user32.PostMessageW.restype = ctypes.c_bool
    _user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    _user32.OpenClipboard.restype = ctypes.c_bool
    _user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    _user32.SetClipboardData.restype = ctypes.c_void_p
    _user32.keybd_event.argtypes = [
        ctypes.c_ubyte,
        ctypes.c_ubyte,
        ctypes.c_uint,
        ctypes.c_void_p,
    ]
    _kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    _kernel32.GlobalAlloc.restype = ctypes.c_void_p
    _kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    _kernel32.GlobalLock.restype = ctypes.c_void_p
    _kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    _kernel32.GetCurrentThreadId.restype = ctypes.c_ulong


# ── 工具描述 ─────────────────────────────────────────────────────────────────
LAUNCH_LOCAL_APP_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "launch_local_app",
        "description": (
            "在本机电脑上启动某个软件，或把已在运行的该软件切换到前台。"
            "当用户说“打开/启动/拉起/运行某个软件”“把小智/微信/浏览器调出来”时调用本工具。"
            "如果用户还要求让该软件做点什么（例如“打开 WorkBuddy 帮我写周报”），"
            "把用户的原话放进 text 参数，本工具会把它输入到该软件窗口里。"
            "注意：本工具只负责启动软件/切换软件窗口，不负责“记录、保存文字”，"
            "用户要求记录内容时必须改用 write_note 工具，不要用本工具的 text 参数来记东西。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "要操作的本机软件名称。",
                },
                "text": {
                    "type": "string",
                    "description": (
                        "可选。启动并切换到前台之后，自动输入到该软件窗口里的内容（粘贴后回车）。"
                        "只有用户明确要求“让这个软件做什么/跟它说点什么”时才填写，"
                        "否则必须留空。"
                    ),
                },
            },
            "required": ["app_name"],
        },
    },
}

CLOSE_LOCAL_APP_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "close_local_app",
        "description": (
            "关闭本机电脑上正在运行的某个软件。"
            "当用户说“关闭/退出/关掉某个软件”时调用本工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "要关闭的本机软件名称。",
                }
            },
            "required": ["app_name"],
        },
    },
}

SCAN_LOCAL_APPS_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "scan_local_apps",
        "description": (
            "重新扫描这台电脑中可启动的软件，并立即更新桌宠可操作的软件清单。"
            "仅当用户明确说“扫描软件”“重新扫描电脑里的软件”“更新软件清单”"
            "“看看设备里有哪些软件”等要求时调用。"
            "不能在启动桌宠、普通闲聊、打开已登记软件或打开未知软件时自行调用。"
            "每次调用都会重新读取系统，不复用上一次扫描结果。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

WRITE_NOTE_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "write_note",
        "description": (
            "把一段文字真正写进本机记事本（落盘保存），并打开记事本把内容显示出来。"
            "当用户说“记录一下”“记一下”“记下来”“帮我记一笔”“写进记事本”“存到记事本”"
            "“把这段话记下来”“把我们刚才的对话记录下来”这类要求记录文字的请求时，必须调用本工具。"
            "用户说“记录我们刚才的对话/聊天内容”时，把上下文里最近的对话整理成"
            "“用户：…”“小智：…”交替的纯文本放进 content。"
            "只有本工具会真正写入记事本，绝不允许不调用工具就回答“已经记录”“记好了”。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "要记到记事本里的正文，可以多行（换行用 \\n 表示）。"
                        "用户要求记录对话时，把整理好的对话文本填在这里；"
                        "不要把“记一下”“记录到记事本”这类指令本身也写进正文。"
                    ),
                },
                "show": {
                    "type": "boolean",
                    "description": "写入后是否立刻打开记事本把内容显示给用户，默认 true。",
                },
            },
            "required": ["content"],
        },
    },
}

START_NOTE_CAPTURE_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "start_note_capture",
        "description": (
            "开启“持续记录”模式：从这一刻起，用户说的每一句话都会被自动写进记事本，直到用户说停止。"
            "当用户说“接下来我说的话都记录到记事本”“开始记录”“开启记录模式”"
            "“帮我记着我说的每一句话”“把我后面说的都记下来”时调用本工具。"
            "注意：这是持续记录，不要改用 write_note，也不要只记当前这一句。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

STOP_NOTE_CAPTURE_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "stop_note_capture",
        "description": (
            "结束“持续记录”模式，之后不再自动往记事本里写用户的话。"
            "当用户说“停止”“停止记录”“别记了”“结束记录”“不用记了”时调用本工具。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


# ── 配置解析与名称匹配 ───────────────────────────────────────────────────────
def _plugin_config(conn: "ConnectionHandler") -> Dict[str, Any]:
    plugins = conn.config.get("plugins", {}) or {}
    base = plugins.get("local_app", {}) or {}
    config = dict(base)
    config["apps"] = get_effective_apps(base)
    return config


def _normalize(text: Any) -> str:
    """归一化：小写、去空白、全角括号转半角，便于口语化名字匹配。"""
    if text is None:
        return ""
    value = str(text).strip().lower()
    for src, dst in (("（", "("), ("）", ")"), ("　", "")):
        value = value.replace(src, dst)
    return "".join(value.split())


def _process_names(app: Dict[str, Any]) -> List[str]:
    """取该软件用于查找进程的映像名列表。"""
    raw = app.get("process")
    names: List[str] = []
    if isinstance(raw, str) and raw.strip():
        names.append(raw.strip())
    elif isinstance(raw, (list, tuple)):
        names.extend(str(item).strip() for item in raw if str(item).strip())

    if not names:
        path = str(app.get("path", "") or "")
        base = os.path.basename(path)
        if base and not path.lower().startswith("shell:") and not base.lower().endswith(
            (".lnk", ".url")
        ):
            names.append(base)
    return names


def _alias_candidates(app: Dict[str, Any]) -> List[str]:
    """一个软件可用于口语匹配的所有名字（正式名 + 别名 + 可执行文件名）。"""
    candidates: List[str] = []
    if app.get("name"):
        candidates.append(str(app["name"]))
    aliases = app.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    candidates.extend(str(item) for item in aliases)
    for proc in _process_names(app):
        candidates.append(os.path.splitext(proc)[0])
    return [item for item in candidates if str(item).strip()]


def _find_app(apps: List[Dict[str, Any]], app_name: str) -> Optional[Dict[str, Any]]:
    """按名字找配置项：先精确匹配，再做包含匹配（容忍"软件""助手"等后缀）。"""
    target = _normalize(app_name)
    if not target:
        return None

    for app in apps:
        if not isinstance(app, dict):
            continue
        for candidate in _alias_candidates(app):
            if _normalize(candidate) == target:
                return app

    for app in apps:
        if not isinstance(app, dict):
            continue
        for candidate in _alias_candidates(app):
            normalized = _normalize(candidate)
            if normalized and (normalized in target or target in normalized):
                return app
    return None


def _app_display_name(app: Dict[str, Any], fallback: str) -> str:
    return str(app.get("name") or fallback)


def _registered_names(apps: List[Dict[str, Any]]) -> List[str]:
    return [
        str(app.get("name"))
        for app in apps
        if isinstance(app, dict) and app.get("name")
    ]


# ── 进程查找 ─────────────────────────────────────────────────────────────────
def _find_processes(process_names: List[str]) -> List[psutil.Process]:
    """按映像名查找正在运行的进程（大小写不敏感，兼容带/不带 .exe）。"""
    if not process_names:
        return []

    wanted = {_normalize(name) for name in process_names if name}
    wanted_stems = {os.path.splitext(name)[0] for name in wanted}

    found: List[psutil.Process] = []
    for proc in psutil.process_iter(["name"]):
        try:
            proc_name = _normalize(proc.info.get("name") or "")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if not proc_name:
            continue
        if proc_name in wanted or os.path.splitext(proc_name)[0] in wanted_stems:
            found.append(proc)
    return found


# ── Windows 窗口操作 ────────────────────────────────────────────────────────
def _windows_of_pid(pid: int) -> List[int]:
    """枚举属于指定进程、有标题的可见顶层窗口。"""
    if not IS_WINDOWS or not pid:
        return []

    handles: List[int] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _callback(hwnd, _lparam):
        window_pid = ctypes.c_ulong()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if window_pid.value != pid:
            return True
        if not _user32.IsWindowVisible(hwnd):
            return True
        # 排除无标题窗口与子窗口（如悬浮工具窗）
        if _user32.GetWindowTextLengthW(hwnd) <= 0:
            return True
        if _user32.GetWindow(hwnd, GW_OWNER):
            return True
        handles.append(hwnd)
        return True

    _user32.EnumWindows(callback_type(_callback), 0)
    return handles


def _wait_for_window(
    process_names: List[str], timeout: float
) -> Tuple[Optional[int], Optional[psutil.Process]]:
    """等待进程出现并拿到它的主窗口，返回 ``(窗口句柄, 进程)``。

    注意：有的程序（例如 Windows 11 的记事本）会先起一个启动器进程、
    再拉起真正显示窗口的应用进程，所以每轮都要重新扫描同名进程，
    不能只盯着最先出现的那个进程。
    """
    if not process_names:
        return None, None

    if not IS_WINDOWS:
        # 非 Windows 没有窗口概念，退化为"等进程起来"
        deadline = time.time() + max(timeout, 0.0)
        while True:
            processes = _find_processes(process_names)
            if processes or time.time() >= deadline:
                return None, (processes[0] if processes else None)
            time.sleep(0.4)

    deadline = time.time() + max(timeout, 0.0)
    last_process: Optional[psutil.Process] = None
    while True:
        for proc in _find_processes(process_names):
            last_process = proc
            handles = _windows_of_pid(proc.pid)
            if handles:
                return handles[0], proc
        if time.time() >= deadline:
            return None, last_process
        time.sleep(0.4)


def _focus_window(hwnd: int) -> bool:
    """把窗口切到前台（含 SetForegroundWindow 的前台权限限制绕行）。"""
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, SW_RESTORE)

        if _user32.SetForegroundWindow(hwnd):
            return True

        # 前台进程持有"设置前台窗口"的权限，通过 AttachThreadInput 借过来
        foreground = _user32.GetForegroundWindow()
        foreground_tid = _user32.GetWindowThreadProcessId(foreground, None)
        current_tid = _kernel32.GetCurrentThreadId()
        _user32.AttachThreadInput(foreground_tid, current_tid, True)
        try:
            _user32.BringWindowToTop(hwnd)
            _user32.ShowWindow(hwnd, SW_RESTORE)
            return bool(_user32.SetForegroundWindow(hwnd))
        finally:
            _user32.AttachThreadInput(foreground_tid, current_tid, False)
    except Exception as error:  # 窗口操作失败不应中断整体流程
        logger.bind(tag=TAG).warning(f"窗口切前台失败 | hwnd={hwnd} | {error}")
        return False


def _set_clipboard_text(text: str) -> bool:
    """写入系统剪贴板（CF_UNICODETEXT）。"""
    if not IS_WINDOWS:
        return False

    data = text.encode("utf-16-le") + b"\x00\x00"
    handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        return False

    pointer = _kernel32.GlobalLock(handle)
    if not pointer:
        return False
    ctypes.memmove(pointer, data, len(data))
    _kernel32.GlobalUnlock(handle)

    if not _user32.OpenClipboard(None):
        return False
    try:
        _user32.EmptyClipboard()
        # 交给系统后由系统持有该内存，不能在这里释放
        return bool(_user32.SetClipboardData(CF_UNICODETEXT, handle))
    finally:
        _user32.CloseClipboard()


def _press_key(vk: int) -> None:
    _user32.keybd_event(vk, 0, 0, 0)
    _user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def _paste_and_submit(text: str, submit: bool) -> bool:
    """把文本粘贴到当前前台窗口，可选回车提交。"""
    if not IS_WINDOWS:
        return False
    if not _set_clipboard_text(text):
        return False

    _user32.keybd_event(VK_CONTROL, 0, 0, 0)
    _press_key(VK_V)
    _user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

    if submit:
        time.sleep(0.4)
        _press_key(VK_RETURN)
    return True


# ── 启动 / 关闭 ─────────────────────────────────────────────────────────────
def _spawn(path: str, args: List[str]) -> None:
    """启动目标程序。

    经验：Windows 上对 ``notepad.exe`` 这类"光名字"调用 ``os.startfile`` 会静默失败
    （ShellExecute 不按 PATH 解析），所以先按 PATH 解析成绝对路径再交给 CreateProcess；
    只有快捷方式 / 批处理 / 文档才必须交给系统外壳解析。
    """
    if IS_WINDOWS:
        if path.lower().startswith("shell:appsfolder\\") or path.lower().endswith(
            (".lnk", ".url", ".bat", ".cmd", ".ps1")
        ):
            os.startfile(path)  # noqa: S606 - 路径来自本机白名单配置
            return
        subprocess.Popen(
            [path, *args],
            close_fds=True,
            creationflags=subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        return

    if args:
        subprocess.Popen([path, *args], close_fds=True, start_new_session=True)
        return

    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen(
        [opener, path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _resolve_program(path: str) -> Optional[str]:
    """把配置里的程序路径解析成可直接执行的形式。

    - 绝对路径 / 带目录的相对路径：只做展开与存在性检查
    - 只有文件名（如 ``notepad.exe``）：按 PATH 查找

    返回 None 表示找不到这个程序。
    """
    expanded = os.path.expandvars(os.path.expanduser(path.strip()))
    if not expanded:
        return None
    if IS_WINDOWS and expanded.lower().startswith("shell:appsfolder\\"):
        return expanded
    if os.path.dirname(expanded):
        return expanded if os.path.exists(expanded) else None
    return expanded if shutil.which(expanded) else None


def _close_processes(processes: List[psutil.Process], force: bool) -> int:
    """优雅关闭（Windows 走 WM_CLOSE），必要时强制结束。返回剩余未退出的数量。"""
    for proc in processes:
        closed = False
        if IS_WINDOWS:
            for hwnd in _windows_of_pid(proc.pid):
                if _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
                    closed = True
        if not closed:
            try:
                proc.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    deadline = time.time() + CLOSE_WAIT_TIMEOUT
    while time.time() < deadline:
        alive = [proc for proc in processes if proc.is_running()]
        if not alive:
            return 0
        time.sleep(0.4)

    alive = [proc for proc in processes if proc.is_running()]
    if alive and force:
        for proc in alive:
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        time.sleep(1.0)
        alive = [proc for proc in processes if proc.is_running()]
    return len(alive)


# ── 记事本记录 ───────────────────────────────────────────────────────────────
def _note_file_path(config: Dict[str, Any]) -> str:
    """解析记录文件的落盘路径（相对路径以服务端工作目录为基准）。"""
    raw = str(config.get("note_file") or DEFAULT_NOTE_FILE).strip()
    path = os.path.expandvars(os.path.expanduser(raw))
    if not os.path.isabs(path):
        path = os.path.abspath(path)
    return path


def _open_in_notepad(path: str) -> bool:
    """用记事本打开指定文件，把刚记下的内容显示给用户。"""
    if not IS_WINDOWS:
        return False
    exe = shutil.which("notepad.exe") or os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32", "notepad.exe"
    )
    if not os.path.exists(exe):
        return False
    try:
        subprocess.Popen(
            [exe, path],
            close_fds=True,
            creationflags=subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        return True
    except OSError as error:
        logger.bind(tag=TAG).warning(f"打开记事本失败 | file={path} | {error}")
        return False


# ── 持续记录模式 ─────────────────────────────────────────────────────────────
# 记录按 device_id 保存（而不是挂在连接对象上），这样说话间隔超过
# close_connection_no_voice_time 导致断线重连后，记录模式依然保持，
# 真正做到"直到我说停止"。
_note_capture_devices: Set[str] = set()

# "停止记录"这类指令本身不该被写进记事本
_STOP_CAPTURE_PATTERN = re.compile(r"停止记录|结束记录|别记了|不用记了|不要记了|^停止$|^停$")


def _device_key(conn: "ConnectionHandler") -> str:
    return str(getattr(conn, "device_id", "") or "default")


def note_capture_enabled(conn: "ConnectionHandler") -> bool:
    """当前设备是否处于持续记录模式。"""
    return _device_key(conn) in _note_capture_devices


def _append_note_file(path: str, block: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fp:
        fp.write(block)


def capture_user_text(conn: "ConnectionHandler", text: Any) -> bool:
    """持续记录模式下的钩子：把用户这句话追加进记事本。

    由 ``core/connection.py`` 的 ``chat()`` 在每轮用户消息时调用。
    返回是否真的写入了内容。
    """
    if not note_capture_enabled(conn):
        return False

    content = (text or "").strip()
    if not content or _STOP_CAPTURE_PATTERN.search(content):
        return False

    path = _note_file_path(_plugin_config(conn))
    stamp = time.strftime("%H:%M")
    try:
        _append_note_file(path, f"[{stamp}] {content}\n")
    except OSError as error:
        logger.bind(tag=TAG).error(f"持续记录写入失败 | file={path} | {error}")
        return False

    logger.bind(tag=TAG).info(f"note_capture | file={path} | {content[:40]!r}")
    return True


# ── 工具实现 ─────────────────────────────────────────────────────────────────
@register_function(
    "launch_local_app", LAUNCH_LOCAL_APP_FUNCTION_DESC, ToolType.SYSTEM_CTL
)
async def launch_local_app(conn: "ConnectionHandler", app_name: str, text: str = ""):
    """启动本机软件（或把它切到前台），可选向它输入一段文本。"""
    return await asyncio.to_thread(_launch_local_app_sync, conn, app_name, text or "")


@register_function(
    "close_local_app", CLOSE_LOCAL_APP_FUNCTION_DESC, ToolType.SYSTEM_CTL
)
async def close_local_app(conn: "ConnectionHandler", app_name: str):
    """关闭本机正在运行的软件。"""
    return await asyncio.to_thread(_close_local_app_sync, conn, app_name)


@register_function(
    "scan_local_apps", SCAN_LOCAL_APPS_FUNCTION_DESC, ToolType.SYSTEM_CTL
)
async def scan_local_apps(conn: "ConnectionHandler"):
    """仅在用户明确要求时全量重扫软件，并替换上一次扫描结果。"""
    scanned = await asyncio.to_thread(scan_installed_apps)
    await asyncio.to_thread(replace_scanned_apps, scanned)
    server = getattr(conn, "server", None)
    connections = getattr(server, "active_connections", None) or [conn]
    refresh_connections(connections)

    apps = _plugin_config(conn).get("apps") or []
    names = _registered_names(apps)
    preview = "、".join(names[:8])
    suffix = f"，例如{preview}" if preview else ""
    conn.logger.bind(tag=TAG).info(
        f"scan_local_apps | scanned={len(scanned)} | available={len(apps)}"
    )
    return ActionResponse(
        Action.RESPONSE,
        None,
        f"已经重新扫描并更新软件清单，共有{len(apps)}个可操作软件{suffix}。",
    )


@register_function("write_note", WRITE_NOTE_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def write_note(conn: "ConnectionHandler", content: str, show: bool = True):
    """把一段文字追加记录到本机记事本文件，并可打开记事本显示。"""
    return await asyncio.to_thread(_write_note_sync, conn, content or "", show)


@register_function(
    "start_note_capture", START_NOTE_CAPTURE_FUNCTION_DESC, ToolType.SYSTEM_CTL
)
async def start_note_capture(conn: "ConnectionHandler"):
    """开启持续记录模式：之后每句用户消息都会自动写进记事本。"""
    device = _device_key(conn)
    # 幂等：大模型在记录模式下可能重复调用本工具，重复调用不写重复的分隔标记
    if device in _note_capture_devices:
        logger.bind(tag=TAG).info(f"持续记录模式已在开启状态，忽略重复调用 | device={device}")
        return ActionResponse(Action.RESPONSE, None, "已经在记着呢，你直接说就行。")

    _note_capture_devices.add(device)
    logger.bind(tag=TAG).info(f"持续记录模式开启 | device={device}")

    config = _plugin_config(conn)
    path = _note_file_path(config)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    try:
        await asyncio.to_thread(_append_note_file, path, f"===== 开始记录 {stamp} =====\n")
    except OSError as error:
        logger.bind(tag=TAG).warning(f"写入记录起始标记失败 | {error}")

    return ActionResponse(
        Action.RESPONSE,
        None,
        "好，从现在起你说的每句话我都会记到记事本里，说完了说一声“停止记录”。",
    )


@register_function(
    "stop_note_capture", STOP_NOTE_CAPTURE_FUNCTION_DESC, ToolType.SYSTEM_CTL
)
async def stop_note_capture(conn: "ConnectionHandler"):
    """结束持续记录模式。"""
    device = _device_key(conn)
    was_on = device in _note_capture_devices
    _note_capture_devices.discard(device)

    if was_on:
        path = _note_file_path(_plugin_config(conn))
        stamp = time.strftime("%Y-%m-%d %H:%M")
        try:
            await asyncio.to_thread(
                _append_note_file, path, f"===== 停止记录 {stamp} =====\n"
            )
        except OSError as error:
            logger.bind(tag=TAG).warning(f"写入记录结束标记失败 | {error}")

    logger.bind(tag=TAG).info(f"持续记录模式关闭 | device={device} | was_on={was_on}")
    if was_on:
        return ActionResponse(Action.RESPONSE, None, "好，已经停止记录，不再往记事本里写了。")
    return ActionResponse(Action.RESPONSE, None, "现在也没有在记录呀。")


def _launch_local_app_sync(
    conn: "ConnectionHandler", app_name: str, text: str
) -> ActionResponse:
    log = conn.logger.bind(tag=TAG)
    config = _plugin_config(conn)
    apps = [app for app in (config.get("apps") or []) if isinstance(app, dict)]
    if not apps:
        return ActionResponse(
            Action.RESPONSE,
            None,
            "还没有配置可调用的本机软件，请先在配置文件里登记。",
        )

    app = _find_app(apps, app_name)
    if not app:
        return ActionResponse(
            Action.RESPONSE,
            None,
            f"清单里没有“{app_name}”这个软件。",
        )

    display = _app_display_name(app, app_name)
    raw_path = str(app.get("path", "")).strip()
    args = [str(item) for item in (app.get("args") or [])]
    process_names = _process_names(app)
    window_timeout = float(config.get("window_timeout", DEFAULT_WINDOW_TIMEOUT))

    if not raw_path:
        log.error(f"本机软件配置缺少 path | app={display}")
        return ActionResponse(Action.RESPONSE, None, f"“{display}”没有配置程序路径，没法打开。")

    allow_input = bool(app.get("input", config.get("allow_input", True)))
    submit = bool(app.get("submit", True))
    want_input = bool(text.strip()) and allow_input

    processes = _find_processes(process_names)
    already_running = bool(processes)

    # 已在运行时不必校验程序路径（用户可能装完把 exe 挪走了，但进程还在）
    path = raw_path
    if not already_running:
        resolved = _resolve_program(raw_path)
        if not resolved:
            log.error(f"程序路径不可用 | app={display} | path={raw_path}")
            return ActionResponse(
                Action.RESPONSE,
                None,
                f"没找到“{display}”的程序文件，请检查配置里的路径。",
            )
        path = resolved

    log.info(
        f"launch_local_app | app={display} | path={path} | args={args} "
        f"| text={text!r} | running={already_running}"
    )

    if not already_running:
        try:
            _spawn(path, args)
        except OSError as error:
            log.error(f"启动失败 | app={display} | path={path} | {error}")
            return ActionResponse(Action.RESPONSE, None, f"打开“{display}”失败了，{error}。")

    # 启动的用完整超时等窗口；已经运行的只快速取一次
    hwnd, process = _wait_for_window(
        process_names, window_timeout if not already_running else 1.0
    )

    # 进程活着但**没有可见窗口**：常见于"关闭到托盘/后台常驻"的软件
    # （WorkBuddy、微信、QQ 这类 Electron/常驻程序都这样）。
    # 这时单纯切前台无从下手，再拉一次可执行文件——单实例应用通常会恢复出主窗口。
    reopened = False
    if process is not None and hwnd is None:
        relaunch_path = path if not already_running else _resolve_program(raw_path)
        if relaunch_path:
            log.info(f"进程在运行但无可见窗口，再拉起一次以恢复窗口 | app={display}")
            try:
                _spawn(relaunch_path, args)
                hwnd, process = _wait_for_window(process_names, window_timeout)
                reopened = hwnd is not None
            except OSError as error:
                log.warning(f"再次拉起失败 | app={display} | {error}")
        else:
            log.warning(f"进程在运行但没有窗口，且程序路径不可用，无法恢复 | app={display}")

    focused = _focus_window(hwnd) if hwnd else False
    log.info(
        f"窗口就绪 | app={display} "
        f"| pid={process.pid if process else None} | hwnd={hwnd} "
        f"| focused={focused} | reopened={reopened}"
    )

    if process is None:
        # 程序可能是"启动器 + 真正应用"两段式，进程名与配置不一致；
        # 这里按"已发起启动"回报，避免把成功的事情说成失败
        log.warning(f"启动后未找到进程 | app={display} | names={process_names}")
        return ActionResponse(Action.RESPONSE, None, f"已经帮你启动“{display}”了。")

    input_done = False
    if want_input:
        if hwnd and focused:
            input_done = _paste_and_submit(text, submit)
            log.info(
                f"文本输入{'成功' if input_done else '失败'} | app={display} | text={text!r}"
            )
        else:
            log.warning(f"未能切换到前台，跳过文本输入 | app={display} | hwnd={hwnd}")

    if already_running:
        if want_input and input_done:
            return ActionResponse(Action.RESPONSE, None, f"已经把这段话发给“{display}”了。")
        if not want_input and focused:
            if reopened:
                return ActionResponse(Action.RESPONSE, None, f"“{display}”刚才在后台，已经帮你调出来了。")
            return ActionResponse(Action.RESPONSE, None, f"“{display}”已经在运行，已经切到前台了。")
        if not hwnd:
            # 进程在跑但始终没有可见窗口（有的软件只驻留托盘）
            return ActionResponse(
                Action.RESPONSE,
                None,
                f"“{display}”正在后台运行，但没找到它的窗口，请你在任务栏图标上点一下。",
            )
        return ActionResponse(Action.RESPONSE, None, f"“{display}”已经在运行了，帮你切了过去。")

    if want_input and input_done:
        return ActionResponse(Action.RESPONSE, None, f"已经打开“{display}”并把要求发给它了。")
    if want_input and not input_done:
        return ActionResponse(
            Action.RESPONSE, None, f"已经打开“{display}”了，但没找到它的输入框，请手动输入一下。"
        )
    return ActionResponse(Action.RESPONSE, None, f"已经帮你打开“{display}”了。")


def _close_local_app_sync(conn: "ConnectionHandler", app_name: str) -> ActionResponse:
    log = conn.logger.bind(tag=TAG)
    config = _plugin_config(conn)
    apps = [app for app in (config.get("apps") or []) if isinstance(app, dict)]
    if not apps:
        return ActionResponse(
            Action.RESPONSE, None, "还没有配置可调用的本机软件，请先在配置文件里登记。"
        )

    app = _find_app(apps, app_name)
    if not app:
        return ActionResponse(
            Action.RESPONSE,
            None,
            f"清单里没有“{app_name}”这个软件。",
        )

    display = _app_display_name(app, app_name)
    if str(app.get("close", "graceful")).lower() == "off":
        return ActionResponse(Action.RESPONSE, None, f"“{display}”被设置为不允许语音关闭。")

    processes = _find_processes(_process_names(app))
    if not processes:
        return ActionResponse(Action.RESPONSE, None, f"“{display}”本来就没在运行。")

    force = bool(app.get("force_close", config.get("force_close", False)))
    log.info(f"close_local_app | app={display} | pids={[p.pid for p in processes]} | force={force}")
    remaining = _close_processes(processes, force)

    if remaining == 0:
        return ActionResponse(Action.RESPONSE, None, f"已经帮你关闭“{display}”了。")
    log.warning(f"关闭后仍有进程存活 | app={display} | remaining={remaining}")
    return ActionResponse(
        Action.RESPONSE, None, f"“{display}”好像还有窗口没关掉，可能需要你手动确认一下。"
    )


def _write_note_sync(
    conn: "ConnectionHandler", content: str, show: bool = True
) -> ActionResponse:
    """把内容追加写入记事本文件。

    刻意不走"粘贴到窗口"那条链路：粘贴依赖前台焦点，用户说话时前台往往是浏览器，
    会出现"剪贴板里有、记事本里没有"的情况。直接落盘更可靠，记事本只负责展示。
    """
    log = conn.logger.bind(tag=TAG)

    # 持续记录模式下，chat() 的钩子已经把用户这句话写进去了，
    # 大模型如果再调 write_note 就会同一句话写两遍，这里直接跳过。
    if note_capture_enabled(conn):
        log.info("持续记录进行中，write_note 跳过以免重复")
        return ActionResponse(Action.RESPONSE, None, "这句已经在持续记录里了，放心。")

    text = (content or "").strip()
    if not text:
        log.warning("write_note 收到空内容，已忽略")
        return ActionResponse(Action.RESPONSE, None, "没听清要记什么，你再说一遍吧。")

    path = _note_file_path(_plugin_config(conn))
    stamp = time.strftime("%Y-%m-%d %H:%M")
    block = f"【{stamp}】\n{text}\n\n"

    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # 先以 UTF-8 写盘；记事本对无 BOM 的 UTF-8 也能正确显示中文
        with open(path, "a", encoding="utf-8") as fp:
            fp.write(block)
    except OSError as error:
        log.error(f"记录到记事本失败 | file={path} | {error}")
        return ActionResponse(Action.RESPONSE, None, f"记事本没记上，{error}。")

    log.info(f"write_note | file={path} | chars={len(text)} | show={show}")
    if show and not _open_in_notepad(path):
        return ActionResponse(
            Action.RESPONSE, None, "已经记到记事本文件里了，你手动打开记事本看一眼。"
        )
    return ActionResponse(Action.RESPONSE, None, "已经记到记事本里了，现在就能看到。")
