"""本机软件通用操作层（app_act / app_task / list_running_apps）。

和 ``local_app.launch_local_app`` 的分工：
- ``launch_local_app``：启动软件、切前台、可选把一段话粘贴进去；
- ``app_act``：对已在运行的软件做**一个**动作（输入、按键、快捷键、窗口、读取）；
- ``app_task``：按配置里的“宏”或显式 steps 连续做几步，失败即停。

安全边界
--------
1. 只操作白名单里的软件；不接受任意路径/命令行；
2. 输入仍受 ``input`` / ``allow_input`` 控制，微信这类输入即发送的软件默认关掉；
3. 屏蔽 alt+f4、win+r、ctrl+alt+del 等系统/危险组合键；
4. 键盘动作前必须确认目标窗口在前台，否则不敲；
5. 不自动点“支付/删除/发送”类控件，也不往密码框里写（UIA ``IsPassword`` 直接拒绝）；
6. 宏支持 ``when`` / ``params`` / ``expect`` 元数据：``mode=auto`` 时先按用户原话挑宏，
   宏失败或 ``expect`` 不满足会把 trace + 当前窗口回报给模型，由模型改用 steps 续做；
7. 步骤里的 ``save_document`` 走 ``desktop_save``（txt/word/excel），不弹“另存为”。
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import re
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from config.logger import setup_logging
from plugins_func.functions import desktop_save, local_app
from plugins_func.register import Action, ActionResponse, ToolType, register_function

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

# 可选：装了 uiautomation 时 read 能带回更多界面文字；没装也不影响主体功能
try:
    import uiautomation as auto  # type: ignore

    try:
        auto.SetGlobalSearchTimeout(3)
    except Exception:
        pass
except Exception:  # pragma: no cover - 依赖缺失时走窗口标题兜底
    auto = None

_UIA_INIT = threading.local()
_UIA_STATE: Dict[str, List[Dict[str, str]]] = {}
_DANGEROUS_PATTERN = re.compile(
    r"支付|付款|购买|下单|提交订单|发送|删除|发布|注销|转账|确认支付|立即支付"
)

DEFAULT_ACTIONS = [
    "focus",
    "type",
    "press",
    "hotkey",
    "click",
    "set_text",
    "inspect",
    "read",
    "minimize",
    "maximize",
    "restore",
    "wait",
]
_WINDOW_ACTIONS = {"minimize", "maximize", "restore"}

_ACTION_ALIASES = {
    "打开": "focus",
    "切到": "focus",
    "前台": "focus",
    "输入": "type",
    "填写": "type",
    "打字": "type",
    "按": "press",
    "按键": "press",
    "回车": "press",
    "快捷键": "hotkey",
    "组合键": "hotkey",
    "点击": "click",
    "单击": "click",
    "点": "click",
    "设置文字": "set_text",
    "填入": "set_text",
    "控件": "inspect",
    "查看控件": "inspect",
    "读": "read",
    "读取": "read",
    "看看": "read",
    "最小化": "minimize",
    "最大化": "maximize",
    "还原": "restore",
    "恢复": "restore",
    "等待": "wait",
}

_KEY_ALIASES = {
    "回车": "enter",
    "确认": "enter",
    "退格": "backspace",
    "空格": "space",
    "删除": "delete",
    "制表": "tab",
    "退出": "esc",
    "上": "up",
    "下": "down",
    "左": "left",
    "右": "right",
    "退出键": "esc",
}
_VK_NAMES = {
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
}
_MODIFIERS = {
    "ctrl": 0x11,
    "control": 0x11,
    "shift": 0x10,
    "alt": 0x12,
    "win": 0x5B,
    "meta": 0x5B,
}
_BLOCKED_HOTKEYS = {
    "alt+f4",
    "ctrl+alt+del",
    "ctrl+shift+esc",
    "ctrl+esc",
    "win+r",
    "win+x",
    "win+l",
    "alt+tab",
}
KEYEVENTF_KEYUP = 0x0002
SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_RESTORE = 9


# ── 工具描述 ─────────────────────────────────────────────────────────────────
APP_ACT_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "app_act",
        "description": (
            "对已登记的本机软件执行一个动作。打开软件仍用 launch_local_app；"
            "本工具负责“打开之后的操作”：切前台、输入文字、按一次键、按快捷键、"
            "点击控件、按控件写入、查看控件、最小化/最大化/还原、读取当前窗口。"
            "用户说“在记事本里输入…”“在 WorkBuddy 里按 Ctrl+N”“把微信最小化”时直接调用，"
            "不要先调用 list_running_apps；本工具会自动拉起并切到前台。"
            "用户说“点某个按钮”时直接用 action=click、target='按钮名'，不要用 focus 代替；"
            "不知道某个软件里有哪些按钮时，先用 action=inspect 读控件列表，"
            "再用 action=click、target='按钮名' 点击。"
            "用户说“输入…然后回车/发送”时用 action=type 且 submit=true，一次完成。"
            "输入仍受软件白名单的 input 开关限制；微信这类输入即发送的软件不要自动输入。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "app_name": {"type": "string", "description": "要操作的本机软件名称。"},
                "action": {
                    "type": "string",
                    "enum": DEFAULT_ACTIONS,
                    "description": (
                        "focus=切前台；type=输入文字；press=按一次键；hotkey=快捷键；"
                        "click=点击控件；set_text=按控件写入；inspect=查看控件列表；"
                        "read=读窗口；minimize/maximize/restore=窗口状态；wait=等待。"
                    ),
                },
                "target": {
                    "type": "string",
                    "description": (
                        "click/set_text 时填控件名（可先用 inspect 得到）；"
                        "press/hotkey 时可填按键组合，例如 'enter'、'ctrl+n'。"
                    ),
                },
                "value": {
                    "type": "string",
                    "description": (
                        "type/set_text 时填要写入的文字；press 时填键名（回车/退格/空格）；"
                        "hotkey 时填组合键（ctrl+n）；wait 时填秒数。"
                    ),
                },
                "submit": {
                    "type": "boolean",
                    "description": "type 时是否输入后回车，默认沿用该软件的 submit 配置。",
                },
            },
            "required": ["app_name", "action"],
        },
    },
}

APP_TASK_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "app_task",
        "description": (
            "按配置里的宏或显式步骤，连续操作一个本机软件。"
            "当用户一句话里要求多个动作（例如“打开 WorkBuddy 新建对话并把这段话发出去”）时使用；"
            "只是输入一段文字并回车，用 app_act（type + submit=true）即可，不要用本工具。"
            "**宏优先、steps 兜底**：先看该软件登记过哪些 macro（描述里会带上宏名与适用场景），"
            "命中就用 mode=auto + macro；没有合适的宏再用 mode=steps + steps。"
            "宏自带 expect 校验，失败时会告诉你卡在第几步、当前窗口是什么，"
            "这时改用 mode=steps 只补剩下的动作，不要重复已经完成的步骤。"
            "步骤支持 save_document（把文字存成 txt/docx/xlsx，target 填格式），"
            "用于“记到记事本/存成 Word/存成 Excel”。"
            "步骤可以使用 click / set_text / inspect 做控件级操作；不知道控件名时先放一步 inspect。"
            "某一步失败会停在那一步并如实说明；中间不播报，最后统一总结。"
            "点击“支付/删除/发送”类控件、密码框、input=false 的软件都不会自动执行。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "app_name": {"type": "string", "description": "要操作的本机软件名称。"},
                "mode": {
                    "type": "string",
                    "enum": ["auto", "macro", "steps"],
                    "description": (
                        "auto=有 macro 用宏、没宏按 goal 自动挑宏、再不行用 steps（默认）；"
                        "macro=必须用宏；steps=只用 steps。"
                    ),
                },
                "macro": {
                    "type": "string",
                    "description": "可选。使用配置 app_macros 里为该软件登记的宏名。",
                },
                "text": {
                    "type": "string",
                    "description": "宏里的 {text} 占位符会替换成这段文字。",
                },
                "steps": {
                    "type": "array",
                    "description": "可选。没有宏时按这些步骤执行，最多 5 步。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "description": (
                                    "动作名：focus/type/press/hotkey/click/set_text/"
                                    "inspect/read/minimize/maximize/restore/wait，"
                                    "或 save_document（保存文档）。"
                                ),
                            },
                            "target": {
                                "type": "string",
                                "description": "控件名/按键；save_document 时填 txt / word / excel。",
                            },
                            "value": {"type": "string"},
                            "path": {
                                "type": "string",
                                "description": "save_document 可选。用户明确说了路径时照抄。",
                            },
                            "submit": {"type": "boolean"},
                            "expect": {
                                "type": "string",
                                "description": "可选。执行后窗口标题/内容应包含的文字。",
                            },
                        },
                        "required": ["action"],
                    },
                },
                "goal": {"type": "string", "description": "用户原话，用于失败说明。"},
                "max_steps": {"type": "integer", "description": "最多几步，默认 5。"},
            },
            "required": ["app_name"],
        },
    },
}

LIST_RUNNING_APPS_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "list_running_apps",
        "description": (
            "列出当前正在运行的、已登记本机软件及其窗口标题。"
            "只在用户明确问“现在开着哪些软件/微信开了吗”时调用；"
            "不要用它来确认某个软件能不能操作——app_act / app_task 会自动拉起并切前台。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


# ── Windows 基础 ────────────────────────────────────────────────────────────
if local_app.IS_WINDOWS:
    _user32 = local_app._user32
    _user32.GetForegroundWindow.restype = ctypes.c_void_p
    _user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.IsIconic.argtypes = [ctypes.c_void_p]
    _user32.IsIconic.restype = ctypes.c_bool
    _user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _user32.ShowWindow.restype = ctypes.c_bool
else:  # pragma: no cover - 项目只跑 Windows
    _user32 = None


def _window_title(hwnd: int) -> str:
    if not _user32 or not hwnd:
        return ""
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _foreground_hwnd() -> int:
    if not _user32:
        return 0
    return int(_user32.GetForegroundWindow() or 0)


def _app_state(app: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    names = local_app._process_names(app)
    processes = local_app._find_processes(names)
    if not processes:
        return None

    # 多窗口软件（Windows 11 记事本、Chrome 等）优先用当前前台窗口，
    # 避免把“另一个标签页/另一份文档”当成本次操作目标。
    foreground = _foreground_hwnd()
    for proc in processes:
        try:
            handles = local_app._windows_of_pid(proc.pid)
        except Exception:
            handles = []
        if foreground and foreground in handles:
            return {
                "hwnd": int(foreground),
                "pid": proc.pid,
                "title": _window_title(foreground),
            }

    hwnd, proc = local_app._wait_for_window(names, 0.6)
    if hwnd is None:
        return None
    return {
        "hwnd": int(hwnd),
        "pid": proc.pid if proc else 0,
        "title": _window_title(hwnd),
    }


def _state_text(state: Optional[Dict[str, Any]]) -> str:
    if not state:
        return "窗口不可用"
    return f"窗口：{state.get('title') or '（无标题）'} | pid={state.get('pid') or '-'}"


def _ensure_foreground(hwnd: int) -> bool:
    if not hwnd or not _user32:
        return False
    if _foreground_hwnd() == int(hwnd):
        return True
    if local_app._focus_window(hwnd):
        time.sleep(0.15)
        return _foreground_hwnd() == int(hwnd)
    return False


def _show_window(hwnd: int, command: int) -> bool:
    if not _user32 or not hwnd:
        return False
    return bool(_user32.ShowWindow(hwnd, command))


def _uia_ready() -> bool:
    if auto is None:
        return False
    if getattr(_UIA_INIT, "ok", False):
        return True
    try:
        import comtypes

        comtypes.CoInitialize()
    except Exception:
        pass
    _UIA_INIT.ok = True
    return True


def _uia_root(hwnd: int):
    if not _uia_ready() or not hwnd:
        return None
    control_from_handle = getattr(auto, "ControlFromHandle", None)
    if control_from_handle is None:
        return None
    try:
        return control_from_handle(hwnd)
    except Exception:
        return None


def _uia_nodes(root: Any, max_depth: int = 4, max_nodes: int = 300):
    """广度优先遍历控件；限制深度/节点数，避免在复杂应用上卡死。"""
    queue: List[tuple] = [(root, 0)]
    visited = 0
    while queue and visited < max_nodes:
        node, depth = queue.pop(0)
        yield node, depth
        visited += 1
        if depth >= max_depth:
            continue
        try:
            children = node.GetChildren()
        except Exception:
            children = []
        for child in list(children)[:40]:
            queue.append((child, depth + 1))


def _control_name(node: Any) -> str:
    try:
        return str(node.Name or "").strip()
    except Exception:
        return ""


def _control_type(node: Any) -> str:
    try:
        return str(node.ControlTypeName or "").strip()
    except Exception:
        return ""


def _control_id(node: Any) -> str:
    try:
        return str(node.AutomationId or "").strip()
    except Exception:
        return ""


def _control_text(node: Any) -> str:
    """优先取 ValuePattern，其次 TextPattern；失败返回空串。"""
    try:
        pattern = node.GetValuePattern()
        value = str(pattern.Value or "")
        if value:
            return value
    except Exception:
        pass
    try:
        pattern = node.GetTextPattern()
        # 限制 2000 字，避免大文档把整篇内容拉进来
        value = str(pattern.DocumentRange.GetText(2000) or "")
        if value:
            return value
    except Exception:
        pass
    return ""


def _uia_text(hwnd: int, limit: int = 1200) -> str:
    """用 UIA 读窗口里的可见文字；没装库/不支持时返回空串。"""
    root = _uia_root(hwnd)
    if root is None:
        return ""

    texts: List[str] = []
    seen = set()
    total = 0
    for node, _depth in _uia_nodes(root, max_depth=4, max_nodes=200):
        text = _control_text(node)
        if not text:
            name = _control_name(node)
            if name and _control_type(node) in (
                "DocumentControl",
                "EditControl",
                "TextControl",
            ):
                text = name
        text = text.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        texts.append(text)
        total += len(text)
        if total >= limit:
            break
    return "\n".join(texts)[: max(0, limit)]


def _device_key(conn: "ConnectionHandler") -> str:
    return str(getattr(conn, "device_id", "") or "default")


def _uia_state(conn: "ConnectionHandler") -> List[Dict[str, str]]:
    return _UIA_STATE.setdefault(_device_key(conn), [])


def _uia_inspect(
    conn: "ConnectionHandler", hwnd: int, depth: int = 3, limit: int = 60
) -> tuple[List[Dict[str, str]], str]:
    root = _uia_root(hwnd)
    if root is None:
        return [], ""
    entries: List[Dict[str, str]] = []
    for node, _level in _uia_nodes(root, max_depth=depth + 1, max_nodes=400):
        if node is root:
            continue
        name = _control_name(node)
        automation_id = _control_id(node)
        if not name and not automation_id:
            continue
        try:
            if bool(node.IsOffscreen):
                continue
        except Exception:
            pass
        control_type = _control_type(node)
        try:
            enabled = bool(node.IsEnabled)
        except Exception:
            enabled = True
        entries.append(
            {
                "name": name[:80],
                "id": automation_id[:80],
                "type": control_type[:40],
                "enabled": "1" if enabled else "0",
            }
        )
        if len(entries) >= limit:
            break
    _UIA_STATE[_device_key(conn)] = entries
    lines = []
    for index, item in enumerate(entries, 1):
        suffix = f" id={item['id']!r}" if item.get("id") else ""
        disabled = "" if item.get("enabled") == "1" else " (不可用)"
        lines.append(
            f"{index}. {item.get('type') or 'Control'} {item.get('name')!r}{suffix}{disabled}"
        )
    return entries, "\n".join(lines)


def _find_uia_control(
    conn: "ConnectionHandler", hwnd: int, target: str
):
    root = _uia_root(hwnd)
    if root is None:
        return None, 0, ""

    text = str(target or "").strip()
    if text.isdigit():
        entries = _uia_state(conn)
        index = int(text)
        if 1 <= index <= len(entries):
            text = entries[index - 1].get("name") or entries[index - 1].get("id") or ""

    if not text:
        getter = getattr(auto, "GetFocusedControl", None)
        if getter is None:
            return None, 0, ""
        try:
            focused = getter()
        except Exception:
            return None, 0, ""
        return focused, 1, _control_name(focused)

    exact = []
    partial = []
    for node, _depth in _uia_nodes(root, max_depth=4, max_nodes=400):
        if node is root:
            continue
        name = _control_name(node)
        automation_id = _control_id(node)
        if not name and not automation_id:
            continue
        if text == name or text == automation_id:
            exact.append(node)
        elif text in name or text in automation_id:
            partial.append(node)

    pool = exact or partial
    if not pool:
        return None, 0, ""
    for node in pool:
        try:
            if bool(node.IsEnabled) and not bool(node.IsOffscreen):
                return node, len(pool), _control_name(node)
        except Exception:
            continue
    return pool[0], len(pool), _control_name(pool[0])


def _invoke_control(control: Any) -> bool:
    try:
        pattern = control.GetInvokePattern()
        if pattern is not None:
            pattern.Invoke()
            return True
    except Exception:
        pass
    try:
        control.Click(simulateMove=False)
        return True
    except Exception:
        pass
    return False


def _is_password_control(control: Any) -> bool:
    """UIA 的输入控件如果是密码框（IsPassword=True），一律不写入。"""
    try:
        return bool(control.IsPassword)
    except Exception:
        return False


def _set_control_text(control: Any, text: str, submit: bool) -> str:
    """返回 'value' / 'paste' / ''，便于日志区分。"""
    try:
        pattern = control.GetValuePattern()
        pattern.SetValue(text)
        return "value"
    except Exception:
        pass
    try:
        control.SetFocus()
    except Exception:
        pass
    if local_app._paste_and_submit(text, submit):
        return "paste"
    return ""


# ── 快捷键 ──────────────────────────────────────────────────────────────────
def _vk_for_key(raw: str) -> Optional[int]:
    name = _KEY_ALIASES.get(str(raw or "").strip().lower(), str(raw or "").strip().lower())
    if not name:
        return None
    if name in _VK_NAMES:
        return _VK_NAMES[name]
    if len(name) == 1 and name.isalnum():
        return ord(name.upper())
    match = re.fullmatch(r"f(\d{1,2})", name)
    if match and 1 <= int(match.group(1)) <= 12:
        return 0x70 + int(match.group(1)) - 1
    return None


def _parse_hotkey(combo: str) -> tuple[List[int], int]:
    text = str(combo or "").strip().lower()
    if not text:
        raise ValueError("快捷键不能为空")
    if text.replace(" ", "") in _BLOCKED_HOTKEYS:
        raise ValueError(f"“{combo}”是系统/危险快捷键，我不执行")
    parts = [part for part in re.split(r"\s*\+\s*", text) if part]
    if len(parts) < 1:
        raise ValueError("快捷键格式不对")
    modifiers = [part for part in parts if part in _MODIFIERS]
    keys = [part for part in parts if part not in _MODIFIERS]
    if "win" in modifiers or "meta" in modifiers:
        raise ValueError("Win 组合键会影响系统，我不执行")
    if len(keys) != 1:
        raise ValueError("一次只能按一个主键，例如 ctrl+n")
    virtual_key = _vk_for_key(keys[0])
    if virtual_key is None:
        raise ValueError(f"不认识的按键：{keys[0]}")
    return [ _MODIFIERS[item] for item in modifiers ], virtual_key


def _press_key(vk: int) -> None:
    local_app._press_key(vk)


def _send_hotkey(combo: str) -> None:
    modifiers, virtual_key = _parse_hotkey(combo)
    for modifier in modifiers:
        local_app._user32.keybd_event(modifier, 0, 0, 0)
    try:
        _press_key(virtual_key)
    finally:
        for modifier in reversed(modifiers):
            local_app._user32.keybd_event(modifier, 0, KEYEVENTF_KEYUP, 0)


# ── 动作执行 ────────────────────────────────────────────────────────────────
def _action_name(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    return _ACTION_ALIASES.get(text, text)


def _allowed_actions(app: Dict[str, Any], config: Dict[str, Any]) -> set:
    raw = app.get("actions") if "actions" in app else config.get("actions")
    if not raw:
        return set(DEFAULT_ACTIONS)
    if isinstance(raw, str):
        raw = re.split(r"[,，\s]+", raw)
    return {str(item).strip().lower() for item in raw if str(item).strip()}


def _input_allowed(app: Dict[str, Any], config: Dict[str, Any]) -> bool:
    return bool(app.get("input", config.get("allow_input", True)))


async def _ensure_running(
    conn: "ConnectionHandler", app_name: str, app: Dict[str, Any]
) -> None:
    names = local_app._process_names(app)
    if local_app._find_processes(names):
        return
    # 白名单内自动拉起；失败时把插件返回的话术原样带出去
    response = await asyncio.to_thread(
        local_app._launch_local_app_sync, conn, app_name, ""
    )
    if local_app._find_processes(names):
        return
    message = str(getattr(response, "response", "") or "没能启动这个软件。")
    raise RuntimeError(message)


def _fail(message: str, observation: str = "") -> Dict[str, Any]:
    return {"ok": False, "message": message, "observation": observation, "content": ""}


async def _perform(
    conn: "ConnectionHandler",
    config: Dict[str, Any],
    app: Dict[str, Any],
    action: str,
    target: str = "",
    value: str = "",
    submit: Optional[bool] = None,
) -> Dict[str, Any]:
    action = _action_name(action)
    display = local_app._app_display_name(app, "这个软件")

    if action not in DEFAULT_ACTIONS:
        return _fail(f"我还不会“{action}”这个软件动作。")
    if action not in _allowed_actions(app, config):
        return _fail(f"“{display}”没有被允许执行“{action}”。")

    try:
        await _ensure_running(conn, display, app)
    except RuntimeError as error:
        return _fail(str(error))
    state = _app_state(app)
    if state is None:
        return _fail(
            f"“{display}”在运行，但没找到它的窗口；可能只驻留在托盘，请在任务栏点一下。"
        )

    hwnd = int(state["hwnd"])
    if action == "wait":
        try:
            seconds = max(0.0, min(5.0, float(value or 0.5)))
        except (TypeError, ValueError):
            seconds = 0.5
        await asyncio.sleep(seconds)
        return {
            "ok": True,
            "message": f"等了 {seconds:.1f} 秒。",
            "observation": _state_text(_app_state(app)),
            "content": "",
        }

    if action in _WINDOW_ACTIONS:
        command = {
            "minimize": SW_MINIMIZE,
            "maximize": SW_MAXIMIZE,
            "restore": SW_RESTORE,
        }[action]
        if not _show_window(hwnd, command):
            return _fail(f"没能把“{display}”{action}。", _state_text(state))
        time.sleep(0.2)
        wording = {"minimize": "最小化", "maximize": "最大化", "restore": "还原"}[action]
        return {
            "ok": True,
            "message": f"已经把“{display}”{wording}。",
            "observation": _state_text(_app_state(app)),
            "content": "",
        }

    if action == "focus":
        focused = local_app._focus_window(hwnd)
        time.sleep(0.15)
        return {
            "ok": bool(focused),
            "message": (
                f"已经把“{display}”切到前台。"
                if focused
                else f"没能把“{display}”切到前台。"
            ),
            "observation": _state_text(_app_state(app)),
            "content": "",
        }

    if action == "inspect":
        if auto is None:
            return _fail("还没安装 uiautomation，读不了控件；先安装后再试。")
        entries, content = _uia_inspect(conn, hwnd)
        if not entries:
            return _fail(
                f"没从“{display}”读到可识别的控件；可能这个软件没有开放 UIA。",
                _state_text(state),
            )
        return {
            "ok": True,
            "message": f"读到“{display}”的 {len(entries)} 个控件。",
            "observation": _state_text(state),
            "content": f"窗口：{state.get('title') or '（无标题）'}\n控件：\n{content}",
        }

    if action == "click":
        if auto is None:
            return _fail("还没安装 uiautomation，点不了控件；先安装后再试。")
        control, matched, control_name = _find_uia_control(conn, hwnd, target or value)
        if control is None:
            return _fail(
                f"在“{display}”里没找到“{target or value}”这个控件；先说“看看有哪些控件”让我读一遍。",
                _state_text(state),
            )
        if not _invoke_control(control):
            if not _ensure_foreground(hwnd) or not _invoke_control(control):
                return _fail(
                    f"找到了“{control_name or target or value}”，但点不动。",
                    _state_text(state),
                )
        time.sleep(0.25)
        label = control_name or str(target or value)
        extra = f"（有 {matched} 个同名控件，点了第一个）" if matched > 1 else ""
        return {
            "ok": True,
            "message": f"已经点击“{label}”{extra}。",
            "observation": _state_text(_app_state(app)),
            "content": "",
        }

    if action == "set_text":
        if auto is None:
            return _fail("还没安装 uiautomation，没法按控件写入。")
        content = str(value or "").strip()
        if not content:
            return _fail("要写入的内容是空的。")
        if not _input_allowed(app, config):
            return _fail(f"“{display}”被设置为不允许语音输入。")
        control, matched, control_name = _find_uia_control(conn, hwnd, target)
        if control is None:
            return _fail(
                f"在“{display}”里没找到“{target or '当前焦点'}”这个输入控件。",
                _state_text(state),
            )
        if _is_password_control(control):
            return _fail(
                "这是密码框，为了安全我不往里写，请你手动输入。",
                _state_text(state),
            )
        if not _ensure_foreground(hwnd):
            return _fail(f"“{display}”不在前台，我没有写入。", _state_text(state))
        submit_flag = bool(app.get("submit", True)) if submit is None else bool(submit)
        mode = _set_control_text(control, content, submit_flag)
        if not mode:
            return _fail(
                f"找到了“{control_name or target}”，但写不进去。",
                _state_text(state),
            )
        time.sleep(0.2)
        return {
            "ok": True,
            "message": f"已经写入“{control_name or target or '当前输入框'}”。",
            "observation": _state_text(_app_state(app)),
            "content": "",
        }

    # 键盘动作：必须先确认目标窗口在前台
    if not _ensure_foreground(hwnd):
        return _fail(
            f"“{display}”不在前台，我没有输入/按键，避免敲到别的窗口。",
            _state_text(state),
        )

    if action == "type":
        content = str(value or target or "").strip()
        if not content:
            return _fail("要输入的内容是空的。")
        if not _input_allowed(app, config):
            return _fail(f"“{display}”被设置为不允许语音输入。")
        submit_flag = bool(app.get("submit", True)) if submit is None else bool(submit)
        if not local_app._paste_and_submit(content, submit_flag):
            return _fail(f"往“{display}”输入失败，剪贴板可能被占用。")
        time.sleep(0.25)
        return {
            "ok": True,
            "message": f"已经在“{display}”里输入内容。",
            "observation": _state_text(_app_state(app)),
            "content": "",
        }

    if action == "press":
        key = str(value or target or "enter").strip()
        virtual_key = _vk_for_key(key)
        if virtual_key is None:
            return _fail(f"不认识的按键：{key}")
        _press_key(virtual_key)
        time.sleep(0.2)
        return {
            "ok": True,
            "message": f"已经在“{display}”里按下“{key}”。",
            "observation": _state_text(_app_state(app)),
            "content": "",
        }

    if action == "hotkey":
        combo = str(value or target or "").strip()
        if not _input_allowed(app, config) and combo.lower().replace(" ", "") in {
            "ctrl+v",
            "shift+insert",
        }:
            return _fail(f"“{display}”不允许语音输入，粘贴类快捷键也不执行。")
        try:
            _send_hotkey(combo)
        except ValueError as error:
            return _fail(str(error), _state_text(state))
        time.sleep(0.25)
        return {
            "ok": True,
            "message": f"已经在“{display}”里按下“{combo}”。",
            "observation": _state_text(_app_state(app)),
            "content": "",
        }

    # read
    text = _uia_text(hwnd)
    title = state.get("title") or "（无标题）"
    content = ""
    if text:
        content = (
            f"窗口：{title}\n进程 PID：{state.get('pid') or '-'}\n界面文字：\n{text}"
        )
    return {
        "ok": True,
        "message": f"当前窗口是《{title}》。",
        "observation": _state_text(state),
        "content": content,
    }


# ── app_task 宏/步骤 ────────────────────────────────────────────────────────
def _parse_steps(raw: Any) -> List[Dict[str, Any]]:
    data = raw
    if isinstance(data, str):
        text = data.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
        try:
            data = json.loads(text)
        except ValueError:
            return []
    if not isinstance(data, list):
        return []
    return [
        item
        for item in data
        if isinstance(item, dict) and str(item.get("action") or "").strip()
    ]


def _as_list(raw: Any) -> List[str]:
    """把 when 之类写成字符串或列表的配置统一成列表。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [item for item in re.split(r"[,，、\s]+", raw) if item.strip()]
    if isinstance(raw, (list, tuple, set)):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw).strip()
    return [text] if text else []


def _macro_table(
    config: Dict[str, Any], app: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """取这个软件的宏表：软件自带的 macros 优先，其次全局 app_macros。

    软件名按「显示名 + 别名 + 进程名」一起匹配：`app_macros` 里写 "Word"，
    而清单里显示 "Microsoft Word" / 进程 "winword.exe" 时也能命中。
    """
    raw = app.get("macros")
    if isinstance(raw, dict):
        return raw
    all_macros = config.get("app_macros") or {}
    if not isinstance(all_macros, dict):
        return None
    candidates = [
        local_app._normalize(item) for item in local_app._alias_candidates(app)
    ]
    candidates = [item for item in candidates if item]
    if not candidates:
        return None
    # 先精确匹配，避免 "记事本" 被更短的别名抢走
    for key, value in all_macros.items():
        if isinstance(value, dict) and local_app._normalize(key) in candidates:
            return value
    for key, value in all_macros.items():
        if not isinstance(value, dict):
            continue
        macro_app = local_app._normalize(key)
        if not macro_app:
            continue
        for candidate in candidates:
            if macro_app in candidate or candidate in macro_app:
                return value
    return None


def _macro_payload(name: Any, raw: Any) -> Optional[Dict[str, Any]]:
    """统一宏的两种写法：

    - 旧写法：直接给步骤列表 ``- {action: focus}``；
    - 新写法：``{when: [...], params: {...}, expect: "...", steps: [...]}``。
    """
    if isinstance(raw, list):
        return {
            "name": str(name),
            "steps": raw,
            "when": [],
            "params": {},
            "expect": "",
        }
    if not isinstance(raw, dict):
        return None
    steps = raw.get("steps")
    if steps is None:
        steps = raw.get("actions")
    if not isinstance(steps, list):
        return None
    params = raw.get("params")
    return {
        "name": str(name),
        "steps": [item for item in steps if isinstance(item, dict)],
        "when": _as_list(raw.get("when")),
        "params": dict(params) if isinstance(params, dict) else {},
        "expect": str(raw.get("expect") or "").strip(),
    }


def _macro_definition(
    config: Dict[str, Any], app: Dict[str, Any], macro: str
) -> Optional[Dict[str, Any]]:
    """查一个宏的完整定义（步骤 + when/params/expect）。"""
    table = _macro_table(config, app)
    if not isinstance(table, dict):
        return None
    target = local_app._normalize(macro)
    if not target:
        return None
    for key, value in table.items():
        if local_app._normalize(key) == target:
            return _macro_payload(key, value)
    return None


def _macro_names(config: Dict[str, Any], app: Dict[str, Any]) -> List[str]:
    table = _macro_table(config, app)
    if not isinstance(table, dict):
        return []
    return [str(key) for key in table.keys()]


def _pick_macro(
    config: Dict[str, Any], app: Dict[str, Any], goal: str
) -> Optional[Dict[str, Any]]:
    """按宏里的 when 关键词给用户原话挑一个宏；一个都没命中返回 None。

    这就是 mode=auto 的“宏优先”：命中越多越优先，没登记 when 的宏不参与自动挑选
    （宁可让模型给 steps，也不猜一个可能改坏文档的宏）。
    """
    table = _macro_table(config, app)
    if not isinstance(table, dict):
        return None
    text = local_app._normalize(goal)
    if not text:
        return None
    best: Optional[Dict[str, Any]] = None
    best_score = 0
    for key, value in table.items():
        payload = _macro_payload(key, value)
        if not payload or not payload["when"]:
            continue
        score = 0
        for word in payload["when"]:
            word_key = local_app._normalize(word)
            if word_key and word_key in text:
                score += 1
        if score > best_score:
            best, best_score = payload, score
    return best


def _macro_params(
    payload: Dict[str, Any], provided: Dict[str, Any]
) -> Dict[str, str]:
    """把宏声明的 params 解析成值：``{text}`` 取调用参数，其余当字面量。"""
    values: Dict[str, str] = {}
    for key, spec in (payload.get("params") or {}).items():
        key_text = str(key).strip()
        if not key_text:
            continue
        if isinstance(spec, str) and spec.startswith("{") and spec.endswith("}"):
            values[key_text] = str(provided.get(spec[1:-1], "") or "")
        else:
            values[key_text] = "" if spec is None else str(spec)
    return values


def _substitute_steps(
    steps: List[Dict[str, Any]],
    text: str,
    params: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for raw in steps:
        item = dict(raw)
        for field in ("value", "target", "expect", "path"):
            if not isinstance(item.get(field), str):
                continue
            value = item[field]
            for key, replacement in (params or {}).items():
                if key == "text":
                    continue
                value = value.replace("{" + key + "}", str(replacement))
            item[field] = value.replace("{text}", str(text or ""))
        result.append(item)
    return result


def _task_failure(
    goal: str,
    index: int,
    total: int,
    trace: List[str],
    reason: str,
    observation: str,
    hint: str = "",
) -> ActionResponse:
    lines = [f"任务“{goal}”执行到第 {index}/{total} 步失败。"]
    if trace:
        lines.append("已完成：\n" + "\n".join(trace))
    lines.append(f"失败原因：{reason}")
    if observation:
        lines.append(f"当前窗口：{observation}")
    lines.append("请如实告诉用户卡在哪一步，不要假装完成。")
    if hint:
        lines.append(hint)
    return ActionResponse(Action.REQLLM, "\n".join(lines), None)


_SAVE_ACTIONS = {"save_document", "save_text"}
_SAVE_HINT = (
    "可以改用 steps 继续：把剩下的动作用 mode=steps 传给我，"
    "已经完成的步骤不要重复（避免重复输入或重复保存）。"
)


def _is_save_action(raw: Dict[str, Any]) -> bool:
    text = str(raw.get("action") or "").strip()
    return text.lower() in _SAVE_ACTIONS or _action_name(text) in _SAVE_ACTIONS


def _save_target(raw: Dict[str, Any]) -> str:
    return str(raw.get("target") or raw.get("kind") or "txt")


async def _run_app_task(
    conn: "ConnectionHandler",
    config: Dict[str, Any],
    app: Dict[str, Any],
    goal: str,
    macro: str,
    text: str,
    raw_steps: Any,
    max_steps: Any,
    mode: str = "auto",
) -> ActionResponse:
    display = local_app._app_display_name(app, "这个软件")
    mode = str(mode or "auto").strip().lower()
    if mode not in ("auto", "macro", "steps"):
        mode = "auto"

    provided_steps = _parse_steps(raw_steps)
    payload: Optional[Dict[str, Any]] = None

    if macro:
        payload = _macro_definition(config, app, macro)
        if payload is None and not provided_steps:
            names = "、".join(_macro_names(config, app)) or "（这台软件还没有登记任何宏）"
            return ActionResponse(
                Action.RESPONSE,
                None,
                f"“{display}”没有登记“{macro}”这个操作；已登记的有：{names}。"
                "可以改用 steps 一步一步做。",
            )
    elif mode in ("auto", "macro"):
        # 宏优先：用户原话命中了某个宏的 when 就直接走宏
        payload = _pick_macro(config, app, goal or text)

    steps: List[Dict[str, Any]]
    if payload is not None:
        steps = _substitute_steps(
            payload["steps"],
            text,
            _macro_params(payload, {"text": text, "goal": goal}),
        )
        if not steps:
            return ActionResponse(
                Action.RESPONSE, None, f"宏“{payload['name']}”里没有可执行的步骤。"
            )
    else:
        steps = _substitute_steps(provided_steps, text)

    if not steps:
        if mode == "steps":
            return ActionResponse(Action.RESPONSE, None, "没有可执行的步骤；steps 是空的。")
        names = "、".join(_macro_names(config, app)) or "（这台软件还没有登记任何宏）"
        return ActionResponse(
            Action.RESPONSE,
            None,
            f"没有可执行的步骤；“{display}”已登记的宏有：{names}。也可以直接给 steps。",
        )

    try:
        limit = int(max_steps or 5)
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(5, limit))
    if len(steps) > limit:
        return ActionResponse(
            Action.RESPONSE, None, f"一次最多执行 {limit} 步，这次给了 {len(steps)} 步。"
        )

    try:
        timeout = float(config.get("task_timeout") or 30)
    except (TypeError, ValueError):
        timeout = 30.0

    goal_text = str(goal or "这个任务").strip()[:80] or "这个任务"
    # 全是“保存文档”的宏（例如记事本新建并写入）不需要事先拉起软件：
    # 记事本由 desktop_save 自己打开目标文件，Word/Excel 由 COM 拉起。
    needs_app = any(not _is_save_action(item) for item in steps)
    if needs_app:
        try:
            await _ensure_running(conn, display, app)
        except RuntimeError as error:
            return ActionResponse(Action.RESPONSE, None, str(error))

    trace: List[str] = []
    last_content = ""
    last_message = ""
    last_observation = ""
    started = asyncio.get_running_loop().time()

    def fail(index: int, reason: str, observation: str = "") -> ActionResponse:
        return _task_failure(
            goal_text,
            index,
            len(steps),
            trace,
            reason,
            observation or _state_text(_app_state(app)),
            _SAVE_HINT if payload is not None else "",
        )

    for index, raw in enumerate(steps, 1):
        if bool(getattr(conn, "client_abort", False)):
            return ActionResponse(
                Action.RESPONSE, "\n".join(trace), "好的，已经停下来了。"
            )
        if asyncio.get_running_loop().time() - started > timeout:
            return fail(index, "整个任务超时")

        action = _action_name(raw.get("action"))
        target = str(raw.get("target") or "")
        value = str(raw.get("value") or "")
        expect = str(raw.get("expect") or "").strip()
        submit = raw.get("submit")
        is_save = _is_save_action(raw)

        if not is_save and action == "click":
            check_text = f"{target} {value}"
            if target.isdigit():
                entries = _uia_state(conn)
                item_index = int(target)
                if 1 <= item_index <= len(entries):
                    check_text += (
                        " "
                        + entries[item_index - 1].get("name", "")
                        + " "
                        + entries[item_index - 1].get("id", "")
                    )
            dangerous = _DANGEROUS_PATTERN.search(check_text)
            if dangerous:
                conn.logger.bind(tag=TAG).warning(
                    f"app_task 拦截敏感控件 | {display} | {dangerous.group(0)}"
                )
                return ActionResponse(
                    Action.RESPONSE,
                    "\n".join(trace),
                    f"第 {index} 步要点击“{dangerous.group(0)}”相关控件，"
                    "为了安全我不自动执行；你确认后我可以一步一步来。",
                )

        try:
            if is_save:
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        desktop_save.save_text,
                        _save_target(raw),
                        value,
                        str(raw.get("path") or ""),
                    ),
                    timeout=15.0,
                )
            else:
                result = await asyncio.wait_for(
                    _perform(
                        conn,
                        config,
                        app,
                        action,
                        target,
                        value,
                        None if submit is None else bool(submit),
                    ),
                    timeout=10.0,
                )
        except asyncio.TimeoutError:
            return fail(index, f"第 {index} 步（{action}）超时")
        except Exception as error:
            return fail(index, f"{type(error).__name__}: {error}")

        if not result.get("ok"):
            return fail(
                index,
                str(result.get("message") or "这一步没有成功"),
                str(result.get("observation") or ""),
            )

        label = f"{action}({_save_target(raw)})" if is_save else action
        trace.append(f"{index}. {label} -> {result.get('message') or '完成'}")
        last_message = str(result.get("message") or "")
        if result.get("content"):
            last_content = str(result.get("content") or "")
        last_observation = str(result.get("observation") or "")

        if expect:
            haystack = " ".join(
                [
                    last_observation,
                    str(result.get("content") or ""),
                    last_message,
                ]
            )
            if expect not in haystack:
                return fail(
                    index,
                    f"执行后窗口里没有出现“{expect}”",
                    last_observation,
                )

    # 宏自带的 expect：整段跑完后校验一次（例如“窗口标题里要出现 小智记录”）
    if payload is not None and payload["expect"]:
        haystack = " ".join([last_message, last_content, last_observation])
        if payload["expect"] not in haystack:
            return _task_failure(
                goal_text,
                len(steps),
                len(steps),
                trace,
                f"宏“{payload['name']}”执行完，但窗口里没有出现“{payload['expect']}”",
                last_observation or _state_text(_app_state(app)),
                _SAVE_HINT,
            )

    macro_tag = f"（宏 {payload['name']}）" if payload is not None else ""
    if last_content:
        content = (
            f"任务“{goal_text}”{macro_tag}已执行完 {len(trace)} 步。\n"
            "执行记录：\n"
            + "\n".join(trace)
            + "\n\n"
            + last_content
            + "\n\n请用一两句话总结并告诉用户已完成。"
        )
        return ActionResponse(Action.REQLLM, content, None)

    detail = "执行记录：\n" + "\n".join(trace)
    spoken = f"已经完成 {len(trace)} 步"
    if last_message:
        spoken += f"：{last_message}"
    return ActionResponse(Action.RESPONSE, detail, spoken)


# ── 工具入口 ────────────────────────────────────────────────────────────────
def _resolve_app(
    conn: "ConnectionHandler", app_name: str
) -> tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    config = local_app._plugin_config(conn)
    apps = [item for item in (config.get("apps") or []) if isinstance(item, dict)]
    return local_app._find_app(apps, app_name), config


@register_function("app_act", APP_ACT_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def app_act(
    conn: "ConnectionHandler",
    app_name: str,
    action: str,
    target: str = "",
    value: str = "",
    submit: Optional[bool] = None,
):
    """对已运行的本机软件执行一个动作。"""
    app, config = _resolve_app(conn, app_name)
    if not app:
        return ActionResponse(
            Action.RESPONSE, None, f"清单里没有“{app_name}”这个软件。"
        )
    try:
        result = await asyncio.wait_for(
            _perform(conn, config, app, action, target, value, submit),
            timeout=float(config.get("action_timeout") or 30),
        )
    except asyncio.TimeoutError:
        return ActionResponse(Action.RESPONSE, None, "这个软件动作超时了，请稍后再试。")
    except Exception as error:
        conn.logger.bind(tag=TAG).error(
            f"app_act 失败 | {app_name} | {action} | {type(error).__name__}: {error}"
        )
        return ActionResponse(Action.RESPONSE, None, "操作软件时出了点问题，再试一次。")

    if result.get("content"):
        return ActionResponse(Action.REQLLM, result["content"], None)
    return ActionResponse(
        Action.RESPONSE, result.get("observation") or None, result.get("message") or ""
    )


@register_function("app_task", APP_TASK_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def app_task(
    conn: "ConnectionHandler",
    app_name: str,
    macro: str = "",
    text: str = "",
    steps: Any = None,
    goal: str = "",
    max_steps: int = 5,
    mode: str = "auto",
):
    """按宏或步骤连续操作一个本机软件。"""
    app, config = _resolve_app(conn, app_name)
    if not app:
        return ActionResponse(
            Action.RESPONSE, None, f"清单里没有“{app_name}”这个软件。"
        )
    try:
        return await _run_app_task(
            conn, config, app, goal, macro, text, steps, max_steps, mode
        )
    except Exception as error:
        conn.logger.bind(tag=TAG).error(
            f"app_task 失败 | {app_name} | {type(error).__name__}: {error}"
        )
        return ActionResponse(Action.RESPONSE, None, "连续操作软件时出了点问题。")


@register_function(
    "list_running_apps", LIST_RUNNING_APPS_FUNCTION_DESC, ToolType.SYSTEM_CTL
)
async def list_running_apps(conn: "ConnectionHandler"):
    """列出已登记且正在运行的软件。"""
    config = local_app._plugin_config(conn)
    apps = [item for item in (config.get("apps") or []) if isinstance(item, dict)]

    def collect() -> List[str]:
        lines: List[str] = []
        for app in apps:
            names = local_app._process_names(app)
            processes = local_app._find_processes(names)
            if not processes:
                continue
            hwnd, _proc = local_app._wait_for_window(names, 0.15)
            title = _window_title(hwnd) if hwnd else ""
            suffix = f"：{title}" if title else ""
            lines.append(f"{app.get('name')}（{len(processes)} 个进程）{suffix}")
        return lines

    lines = await asyncio.to_thread(collect)
    if not lines:
        return ActionResponse(Action.RESPONSE, None, "当前没有登记软件在运行。")
    preview = "、".join(line.split("（")[0] for line in lines[:6])
    return ActionResponse(
        Action.RESPONSE,
        "正在运行：\n" + "\n".join(lines[:10]),
        f"现在开着：{preview}。",
    )
