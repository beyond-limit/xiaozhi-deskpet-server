"""跨软件多步指令（desktop_task）：一次工具调用串起「本机软件 + Edge + 保存文件」。

为什么要有这个工具
------------------
真实需求经常横跨几个软件：「查一下小智 AI 是什么，整理到记事本」「把网页上的内容
存成 Word」。让模型连续调用 app_task / browser_task / write_note 会带来两个问题：

1. 每多调一个工具就多一轮 LLM，``core/connection.py`` 的 ``MAX_DEPTH``（=5）很快见底，
   用户听到的就是「已达到最大工具调用次数限制」；
2. 中间每一步都会播报，听起来啰嗦，而且模型容易在第二轮就忘记前面做过什么。

本工具把 app / browser / 保存文档放进**同一次工具调用**里执行，因此：
**不提高全局 MAX_DEPTH，也不增加对话轮次**。

硬性边界
--------
- 最多 **8 步**；总超时 **60 秒**；单步 **15 秒**；
- **中间静默**（执行期间不逐条播报），失败即停，最后统一总结；
- ``capture`` 把某一步读到的正文存成变量（**单次最多 2000 字**），后续步骤用
  ``{变量名}`` 引用，例如 ``{"action": "save_document", "value": "{page}"}``；
- 危险点击（支付/付款/发送/删除/转账…）、密码框、``input=false`` 的软件、
  未登记软件一律拒绝执行；
- 宏优先：``plugins.desktop_task.macros`` 里登记过、且 ``when`` 命中用户原话的宏直接跑，
  没有宏再用模型给的 steps。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from config.logger import setup_logging
from plugins_func.functions import (
    browser_cdp,
    browser_control,
    desktop_save,
    local_app,
    local_app_control,
)
from plugins_func.register import Action, ActionResponse, ToolType, register_function

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

# 一次调用最多 8 步：再多就该拆成两次说，否则用户等太久、出错也难定位
MAX_STEPS = 8
# 单步 15 秒（启动软件/等页面/COM 另存都够）
STEP_TIMEOUT = 15.0
# 整个任务 60 秒（8 步 × 15 秒是上限，实际远小于这个值）
TOTAL_TIMEOUT = 60.0
# capture 变量单次最多 2000 字
CAPTURE_LIMIT = 2000
# 回给模型时最多展示多少字资料（避免把 2000 字全塞进上下文）
REPLY_PREVIEW = 1200

BROWSER_ACTIONS = {
    "open",
    "navigate",
    "search",
    "click",
    "click_result",
    "fill",
    "press",
    "read",
    "screenshot",
    "back",
    "forward",
    "reload",
    "scroll",
    "tabs",
    "switch",
    "new_tab",
    "close_tab",
    "status",
}
APP_ACTIONS = set(local_app_control.DEFAULT_ACTIONS)
BROWSER_ONLY = BROWSER_ACTIONS - APP_ACTIONS
APP_ONLY = APP_ACTIONS - BROWSER_ACTIONS
_SAVE_ALIASES = {
    "save_document",
    "save_text",
    "save",
    "保存文档",
    "保存文件",
    "存成文档",
    "写成文档",
}


DESKTOP_TASK_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "desktop_task",
        "description": (
            "一句话里跨软件、跨步骤地做事：查资料并整理到记事本/Word/Excel、"
            "把网页内容写进某个软件、先读窗口再保存成文件。"
            "**只有当用户一句话里同时包含多个动作、且动作跨软件时**才用；"
            "只做一个动作时用 app_act / browser_act，同一软件内的连续动作优先用 app_task 的宏。"
            "把动作拆成 steps（最多 8 步），工具会顺序执行：中间不播报，失败即停，"
            "最后统一说明卡在哪一步、当前窗口/页面是什么。"
            "步骤的 action 可以是本机软件动作（focus/type/press/hotkey/click/set_text/"
            "inspect/read/minimize/maximize/restore/wait），也可以是 Edge 动作"
            "（open/navigate/search/click/click_result/fill/press/read/back/forward/reload/scroll），"
            "还可以是 save_document（把文字存成 txt/word/excel）。"
            "用 app 指定这一步操作哪个软件；浏览器步骤默认路由到 Edge。"
            "capture 会把这一步读到的正文存成变量（最多 2000 字），后续步骤用 {变量名} 引用，"
            "例如先 read 且 capture=page，再 save_document 且 value={page}。"
            "不要为了完成一件事连续调用多个工具——那会多耗轮次，优先用本工具一次做完。"
            "支付/发送/删除类点击、密码框、input=false 的软件不会执行。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "用户原话或一句话目标，用于失败说明。",
                },
                "app_name": {
                    "type": "string",
                    "description": "可选。没写 app 的软件步骤默认操作这个软件。",
                },
                "macro": {
                    "type": "string",
                    "description": (
                        "可选。使用配置 desktop_task.macros 里登记的跨软件宏，"
                        "例如 research_to_notepad（查资料→记事本）。"
                    ),
                },
                "text": {
                    "type": "string",
                    "description": "宏里的 {text} 占位符会替换成这段文字（例如搜索关键词）。",
                },
                "steps": {
                    "type": "array",
                    "description": "按顺序执行的步骤，最多 8 步。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "description": "软件动作 / Edge 动作 / save_document。",
                            },
                            "app": {
                                "type": "string",
                                "description": "可选。这一步操作的本机软件名。",
                            },
                            "via": {
                                "type": "string",
                                "enum": ["app", "browser"],
                                "description": "可选。同名动作（click/press/read）走哪一边，默认按上下文。",
                            },
                            "target": {
                                "type": "string",
                                "description": (
                                    "控件名/按键/搜索结果序号；save_document 时填 txt/word/excel。"
                                ),
                            },
                            "value": {
                                "type": "string",
                                "description": "网址、搜索词、要输入或保存的文字，可引用 {变量名}。",
                            },
                            "path": {
                                "type": "string",
                                "description": "save_document 可选。用户明确说了路径时照抄。",
                            },
                            "submit": {"type": "boolean"},
                            "expect": {
                                "type": "string",
                                "description": "可选。执行后窗口/页面应包含的文字，不满足就停。",
                            },
                            "capture": {
                                "type": "string",
                                "description": (
                                    "可选。把这一步读到的正文存成变量名（最多 2000 字），"
                                    "后续步骤用 {变量名} 引用。"
                                ),
                            },
                        },
                        "required": ["action"],
                    },
                },
                "max_steps": {
                    "type": "integer",
                    "description": "最多执行几步，默认 8；硬上限就是 8。",
                },
            },
            "required": [],
        },
    },
}


# ── 配置与宏 ────────────────────────────────────────────────────────────────
def _plugin_config(conn: "ConnectionHandler") -> Dict[str, Any]:
    plugins = conn.config.get("plugins", {}) or {}
    raw = plugins.get("desktop_task", {}) or {}
    return dict(raw) if isinstance(raw, dict) else {}


def _macro_table(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("macros")
    return raw if isinstance(raw, dict) else {}


def _macro_definition(config: Dict[str, Any], macro: str) -> Optional[Dict[str, Any]]:
    table = _macro_table(config)
    target = local_app._normalize(macro)
    if not target:
        return None
    for key, value in table.items():
        if local_app._normalize(key) == target:
            return local_app_control._macro_payload(key, value)
    return None


def _macro_names(config: Dict[str, Any]) -> List[str]:
    return [str(key) for key in _macro_table(config).keys()]


def _pick_macro(config: Dict[str, Any], goal: str) -> Optional[Dict[str, Any]]:
    table = _macro_table(config)
    text = local_app._normalize(goal)
    if not text:
        return None
    best: Optional[Dict[str, Any]] = None
    best_score = 0
    for key, value in table.items():
        payload = local_app_control._macro_payload(key, value)
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


# ── 步骤解析 / 变量 ─────────────────────────────────────────────────────────
def _parse_steps(raw: Any) -> List[Dict[str, Any]]:
    return local_app_control._parse_steps(raw)


def _substitute_step(
    step: Dict[str, Any], variables: Dict[str, str]
) -> Dict[str, Any]:
    item = dict(step)
    for field in ("value", "target", "path", "expect", "app"):
        raw = item.get(field)
        if not isinstance(raw, str) or "{" not in raw:
            continue
        text = raw
        for key, replacement in variables.items():
            if key:
                text = text.replace("{" + key + "}", str(replacement))
        item[field] = text
    return item


def _capture_text(info: Dict[str, Any], limit: int = CAPTURE_LIMIT) -> str:
    """把一步的产出整理成变量值；空行折叠、最长 limit 字。"""
    raw = (
        str(info.get("content") or "")
        or str(info.get("observation") or "")
        or str(info.get("message") or "")
    )
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines: List[str] = []
    for line in text.split("\n"):
        line = line.rstrip()
        if not line and lines and not lines[-1]:
            continue
        lines.append(line)
    text = "\n".join(lines).strip()
    if len(text) > limit:
        # 含省略号在内也不超过 limit 字
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text


def _action_key(raw: Any) -> str:
    return str(raw or "").strip().lower().replace(" ", "")


def _is_save_step(step: Dict[str, Any]) -> bool:
    return _action_key(step.get("action")) in _SAVE_ALIASES


def _resolve_route(
    step: Dict[str, Any], state: Dict[str, Any]
) -> Tuple[str, str]:
    """决定这一步走 app / browser / save，并归一化动作名。

    规则（先显式、后推断）：
    1. ``via`` 明说 app/browser 就用它；
    2. 写了 ``app`` 就走本机软件；
    3. 动作只在浏览器里有（navigate/search/fill…）→ 浏览器；
       只在软件里有（focus/set_text/hotkey…）→ 软件；
    4. click / press / read 两边都有 → 沿用上一步的通道（没有就按默认目标）。
    """
    raw_action = str(step.get("action") or "").strip()
    if _is_save_step(step):
        return "save", "save_document"

    via = str(step.get("via") or "").strip().lower()
    if via in ("browser", "浏览器", "edge", "网页"):
        return "browser", browser_control._action_name(raw_action)
    if via in ("app", "软件", "客户端"):
        return "app", local_app_control._action_name(raw_action)
    if str(step.get("app") or "").strip():
        return "app", local_app_control._action_name(raw_action)

    app_action = local_app_control._action_name(raw_action)
    browser_action = browser_control._action_name(raw_action)
    app_known = app_action in APP_ACTIONS
    browser_known = browser_action in BROWSER_ACTIONS

    if browser_known and not app_known:
        return "browser", browser_action
    if app_known and not browser_known:
        return "app", app_action
    if app_known and browser_known:
        route = str(state.get("last_route") or "")
        if route not in ("app", "browser"):
            route = "app" if str(state.get("app_name") or "").strip() else "browser"
        return route, app_action if route == "app" else browser_action
    return "", raw_action


def _dangerous_hit(
    conn: "ConnectionHandler", route: str, action: str, target: str, value: str
) -> Optional[str]:
    """危险点击拦截：解析"点第几个"对应的真实文案，避免用序号绕过关键词。"""
    if action not in ("click", "click_result"):
        return None
    check_text = f"{target} {value}"
    if str(target).strip().isdigit():
        index = int(str(target).strip())
        if route == "app":
            entries = local_app_control._uia_state(conn)
            if 1 <= index <= len(entries):
                check_text += f" {entries[index - 1].get('name', '')}"
        elif route == "browser":
            item = (browser_control._state(conn).get("items") or {}).get(index)
            if isinstance(item, dict):
                check_text += f" {item.get('text', '')}"
    hit = local_app_control._DANGEROUS_PATTERN.search(check_text)
    return hit.group(0) if hit else None


# ── 单步执行 ────────────────────────────────────────────────────────────────
def _step_fail(message: str, observation: str = "", content: str = "") -> Dict[str, Any]:
    return {
        "ok": False,
        "message": message,
        "observation": observation,
        "content": content,
    }


async def _run_app_step(
    conn: "ConnectionHandler",
    step: Dict[str, Any],
    app_name: str,
    action: str,
    target: str,
    value: str,
) -> Dict[str, Any]:
    app, config = local_app_control._resolve_app(conn, app_name)
    if not app:
        return _step_fail(f"清单里没有“{app_name}”这个软件")
    display = local_app._app_display_name(app, app_name)

    if action in ("type", "set_text") and not local_app_control._input_allowed(
        app, config
    ):
        return _step_fail(f"“{display}”被设置为不允许语音输入，这一步我没有执行")

    submit = step.get("submit")
    result = await local_app_control._perform(
        conn,
        config,
        app,
        action,
        target,
        value,
        None if submit is None else bool(submit),
    )
    return {
        "ok": bool(result.get("ok")),
        "message": str(result.get("message") or ""),
        "observation": str(result.get("observation") or ""),
        "content": str(result.get("content") or ""),
    }


async def _run_browser_step(
    conn: "ConnectionHandler",
    action: str,
    target: str,
    value: str,
) -> Dict[str, Any]:
    config = browser_control._plugin_config(conn)
    async with browser_control._lock_for(conn):
        response = await browser_control._execute(conn, config, action, target, value)

    result_text = str(response.result or "")
    message = str(response.response or "")
    failed = response.action in (Action.ERROR, Action.NOTFOUND) or (
        response.action == Action.RESPONSE and result_text.startswith("[STEP_FAILED]")
    )
    if failed:
        clean = result_text.replace("[STEP_FAILED]", "").strip()
        return _step_fail(message or "这一步没有成功", clean, clean)
    if response.action == Action.REQLLM:
        return {"ok": True, "message": message or "已读取页面", "observation": "", "content": result_text}
    return {"ok": True, "message": message or "完成", "observation": result_text, "content": ""}


async def _run_save_step(conn: "ConnectionHandler", step: Dict[str, Any], value: str) -> Dict[str, Any]:
    kind = str(step.get("target") or step.get("kind") or "txt").strip()
    path = str(step.get("path") or "").strip()
    result = await asyncio.to_thread(desktop_save.save_text, kind, value, path)
    return {
        "ok": bool(result.get("ok")),
        "message": str(result.get("message") or ""),
        "observation": str(result.get("path") or path),
        "content": "",
    }


# ── 主流程 ──────────────────────────────────────────────────────────────────
def _failure(
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
        lines.append(f"当前窗口/页面：\n{observation}")
    lines.append("请如实告诉用户卡在哪一步，不要假装完成。")
    if hint:
        lines.append(hint)
    return ActionResponse(Action.REQLLM, "\n".join(lines), None)


async def _run_desktop_task(
    conn: "ConnectionHandler",
    config: Dict[str, Any],
    goal: str,
    app_name: str,
    macro: str,
    text: str,
    raw_steps: Any,
    max_steps: Any,
) -> ActionResponse:
    log = conn.logger.bind(tag=TAG)
    provided = _parse_steps(raw_steps)
    variables: Dict[str, str] = {"text": str(text or ""), "goal": str(goal or "")}

    payload: Optional[Dict[str, Any]] = None
    if macro:
        payload = _macro_definition(config, macro)
        if payload is None and not provided:
            names = "、".join(_macro_names(config)) or "（还没有登记跨软件宏）"
            return ActionResponse(
                Action.RESPONSE,
                None,
                f"没有登记“{macro}”这个跨软件宏；已登记的有：{names}。可以改用 steps。",
            )
    elif not provided:
        payload = _pick_macro(config, goal or text)

    if payload is not None:
        params = local_app_control._macro_params(payload, variables)
        steps = local_app_control._substitute_steps(payload["steps"], text, params)
        if not steps:
            return ActionResponse(
                Action.RESPONSE, None, f"宏“{payload['name']}”里没有可执行的步骤。"
            )
    else:
        steps = [_substitute_step(step, variables) for step in provided]

    if not steps:
        if payload is None and not provided:
            names = "、".join(_macro_names(config)) or "（还没有登记跨软件宏）"
            return ActionResponse(
                Action.RESPONSE,
                None,
                f"没有可执行的步骤；已登记的跨软件宏有：{names}。也可以直接给 steps。",
            )
        return ActionResponse(
            Action.RESPONSE, None, "没有可执行的步骤；steps 里没有带 action 的项。"
        )

    if len(steps) > MAX_STEPS:
        return ActionResponse(
            Action.RESPONSE,
            None,
            f"一次最多执行 {MAX_STEPS} 步，这次给了 {len(steps)} 步；请拆成两次说。",
        )
    try:
        wanted = int(max_steps or MAX_STEPS)
    except (TypeError, ValueError):
        wanted = MAX_STEPS
    if wanted < len(steps):
        log.info(f"desktop_task max_steps={wanted} 小于实际步数 {len(steps)}，按实际步数执行")

    goal_text = str(goal or "这个任务").strip()[:80] or "这个任务"

    # 显式写了 app 的步骤先校验清单，避免跑了一半才发现软件名不对
    for raw in steps:
        name = str(raw.get("app") or "").strip()
        if not name:
            continue
        app, _cfg = local_app_control._resolve_app(conn, name)
        if not app:
            return ActionResponse(
                Action.RESPONSE, None, f"清单里没有“{name}”这个软件。"
            )

    state: Dict[str, Any] = {
        "last_route": "",
        "app_name": str(app_name or ""),
    }
    trace: List[str] = []
    captures: Dict[str, str] = {}
    last_content = ""
    last_message = ""
    last_observation = ""
    edge_ready = False
    started = asyncio.get_running_loop().time()

    for index, raw in enumerate(steps, 1):
        if bool(getattr(conn, "client_abort", False)):
            log.info(f"desktop_task 被用户打断 | step={index}")
            return ActionResponse(
                Action.RESPONSE, "\n".join(trace), "好的，已经停下来了。"
            )
        if asyncio.get_running_loop().time() - started > TOTAL_TIMEOUT:
            return _failure(
                goal_text,
                index,
                len(steps),
                trace,
                f"整个任务超过 {int(TOTAL_TIMEOUT)} 秒",
                last_observation,
            )

        step = _substitute_step(raw, {**variables, **captures})

        # wait 两边通用、也不需要指定软件：直接睡，避免为了等一下就要求 app_name
        wait_action = local_app_control._action_name(str(step.get("action") or ""))
        if wait_action == "wait" and not str(step.get("app") or "").strip():
            try:
                seconds = max(0.0, min(5.0, float(step.get("value") or 0.5)))
            except (TypeError, ValueError):
                seconds = 0.5
            await asyncio.sleep(seconds)
            last_message = f"等了 {seconds:.1f} 秒。"
            trace.append(f"{index}. wait -> {last_message}")
            continue

        route, action = _resolve_route(step, state)
        known = {"app": APP_ACTIONS, "browser": BROWSER_ACTIONS}
        if not route or (route in known and action not in known[route]):
            return _failure(
                goal_text,
                index,
                len(steps),
                trace,
                f"不认识的动作“{action}”",
                last_observation,
                "请改用标准动作名（app 动作或 Edge 动作），或 save_document。",
            )

        target = str(step.get("target") or "")
        value = str(step.get("value") or "")
        expect = str(step.get("expect") or "").strip()

        hit = _dangerous_hit(conn, route, action, target, value)
        if hit:
            log.warning(f"desktop_task 拦截危险步骤 | step={index} | {hit}")
            return ActionResponse(
                Action.RESPONSE,
                "\n".join(trace),
                f"第 {index} 步涉及“{hit}”，为了安全我不自动执行；"
                "你确认之后我可以一步一步来。",
            )

        if route == "app":
            app_label = str(step.get("app") or state.get("app_name") or "").strip()
            if not app_label:
                return _failure(
                    goal_text,
                    index,
                    len(steps),
                    trace,
                    "这一步没说是哪个软件",
                    last_observation,
                    "请在这类步骤里填 app，或在工具参数里给 app_name。",
                )
            state["app_name"] = app_label
        state["last_route"] = route

        log.info(
            f"desktop_task step {index}/{len(steps)} | route={route} | "
            f"action={action} | target={target[:40]!r} | value={value[:40]!r}"
        )

        try:
            if route == "save":
                info = await asyncio.wait_for(
                    _run_save_step(conn, step, value), timeout=STEP_TIMEOUT
                )
            elif route == "browser":
                if not edge_ready:
                    await browser_control._ensure_edge(
                        conn, browser_control._plugin_config(conn), log
                    )
                    edge_ready = True
                info = await asyncio.wait_for(
                    _run_browser_step(conn, action, target, value),
                    timeout=STEP_TIMEOUT,
                )
            else:
                info = await asyncio.wait_for(
                    _run_app_step(
                        conn,
                        step,
                        str(state.get("app_name") or ""),
                        action,
                        target,
                        value,
                    ),
                    timeout=STEP_TIMEOUT,
                )
        except asyncio.TimeoutError:
            return _failure(
                goal_text,
                index,
                len(steps),
                trace,
                f"第 {index} 步（{action}）超过 {int(STEP_TIMEOUT)} 秒",
                last_observation,
            )
        except browser_cdp.CDPUnavailable as error:
            return _failure(
                goal_text,
                index,
                len(steps),
                trace,
                f"Edge 调试端口不可用：{error}",
                last_observation,
                "可以让用户先手动打开 Edge 再重试，或改用 app_task 只操作本机软件。",
            )
        except browser_cdp.CDPError as error:
            return _failure(
                goal_text,
                index,
                len(steps),
                trace,
                f"{type(error).__name__}: {error}",
                last_observation,
            )
        except Exception as error:
            return _failure(
                goal_text,
                index,
                len(steps),
                trace,
                f"{type(error).__name__}: {error}",
                last_observation,
            )

        if not info.get("ok"):
            return _failure(
                goal_text,
                index,
                len(steps),
                trace,
                str(info.get("message") or "这一步没有成功"),
                str(info.get("observation") or "") or last_observation,
                "可以把剩下的步骤拆成 steps 再试，已完成的步骤不要重复。",
            )

        capture = str(step.get("capture") or "").strip()
        if capture:
            captured = _capture_text(info)
            if captured:
                captures[capture] = captured
                variables[capture] = captured
                variables["last"] = captured

        label = f"{route}:{action}"
        if route == "save":
            label = f"save_document({str(step.get('target') or 'txt')})"
        trace.append(f"{index}. {label} -> {info.get('message') or '完成'}")
        if capture and capture in captures:
            trace[-1] += f"（已存入变量 {capture}，{len(captures[capture])} 字）"

        last_message = str(info.get("message") or "")
        if info.get("content"):
            last_content = str(info.get("content") or "")
        if info.get("observation"):
            last_observation = str(info.get("observation") or "")

        if expect:
            haystack = " ".join(
                [last_observation, last_content, last_message, captures.get(capture, "") if capture else ""]
            )
            if expect not in haystack:
                return _failure(
                    goal_text,
                    index,
                    len(steps),
                    trace,
                    f"执行后窗口/页面没有出现“{expect}”",
                    last_observation,
                    "可以用 expect 校准步骤顺序，或改用 app_act / browser_act 逐步确认。",
                )

    macro_tag = f"（宏 {payload['name']}）" if payload is not None else ""
    source = last_content or variables.get("last") or ""
    if captures or source:
        preview = (source or "")[:REPLY_PREVIEW]
        content = (
            f"任务“{goal_text}”{macro_tag}已执行完 {len(trace)} 步。\n"
            "执行记录：\n"
            + "\n".join(trace)
            + (
                f"\n\n资料（{len(source)} 字，最多显示 {REPLY_PREVIEW} 字）：\n{preview}"
                if preview
                else ""
            )
            + "\n\n请先用一两句话向用户概括资料要点，再说明结果保存到了哪里；"
            "没有做过的步骤不要声称做过。"
        )
        return ActionResponse(Action.REQLLM, content, None)

    detail = "执行记录：\n" + "\n".join(trace)
    spoken = f"已经完成 {len(trace)} 步"
    if last_message:
        spoken += f"：{last_message}"
    return ActionResponse(Action.RESPONSE, detail, spoken)


@register_function("desktop_task", DESKTOP_TASK_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def desktop_task(
    conn: "ConnectionHandler",
    steps: Any = None,
    goal: str = "",
    app_name: str = "",
    macro: str = "",
    text: str = "",
    max_steps: int = MAX_STEPS,
):
    """把本机软件 / Edge / 保存文件串成一次多步执行。"""
    config = _plugin_config(conn)
    try:
        return await _run_desktop_task(
            conn, config, goal, app_name, macro, text, steps, max_steps
        )
    except browser_cdp.CDPUnavailable as error:
        conn.logger.bind(tag=TAG).warning(f"desktop_task 调试端口不可用 | {error}")
        return ActionResponse(
            Action.RESPONSE, None, f"Edge 的调试窗口没准备好：{error}"
        )
    except Exception as error:
        conn.logger.bind(tag=TAG).error(
            f"desktop_task 失败 | {type(error).__name__}: {error}"
        )
        return ActionResponse(
            Action.RESPONSE, None, "跨软件操作时出了点问题，你再说一次我重试。"
        )
