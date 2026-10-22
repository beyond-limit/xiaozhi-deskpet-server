"""Edge / Chromium 的 CDP（DevTools 协议）客户端与独立实例启动辅助。

为什么单独用一个 profile
------------------------
Chromium 的 ``--remote-debugging-port`` 只在“拥有该 user-data-dir 的那个进程”
上生效。日常 Edge 已经运行时，再执行 ``msedge.exe --remote-debugging-port=9222``
只会把参数转交给已有实例然后退出，端口根本不会打开。

因此本模块用 ``data/edge-profile`` 另起一个 Edge 实例：
  - 不打扰用户日常的 Edge 窗口；
  - 9222 只暴露演示用的登录态，安全边界更小；
  - 崩溃后可以直接删目录重建。

注意：本文件也会被 ``plugins_func.loadplugins`` 当作插件模块导入，但这里
没有注册任何工具函数，导入失败不应影响服务启动（依赖都是已有包）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import aiohttp
import psutil
import websockets

from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

DEFAULT_PORT = 9222
DEFAULT_PROFILE_DIR = "data/edge-profile"
MAX_WS_SIZE = 32 * 1024 * 1024


class CDPError(RuntimeError):
    """CDP 调用失败（页面异常、方法报错等）。"""


class CDPUnavailable(CDPError):
    """调试端口不可用（Edge 没启动、没带调试参数、端口被占用等）。"""


# ── Edge 定位与启动 ─────────────────────────────────────────────────────────
def find_edge_executable() -> str:
    """按常见安装位置 / 注册表找 msedge.exe，找不到返回空串。"""
    candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return os.path.abspath(path)

    try:
        import winreg

        key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    value = winreg.QueryValue(key, None)
            except OSError:
                continue
            if value and os.path.exists(value):
                return os.path.abspath(value)
    except (ImportError, OSError):
        pass
    return ""


def resolve_profile_dir(raw: str) -> str:
    """把配置里的相对路径解析成绝对路径（相对服务端工作目录）。"""
    text = str(raw or "").strip() or DEFAULT_PROFILE_DIR
    path = os.path.expandvars(os.path.expanduser(text))
    if not os.path.isabs(path):
        path = os.path.abspath(path)
    return path


def profile_launch_args(
    port: int, profile_dir: str, extra_args: Optional[List[Any]] = None
) -> List[str]:
    """构造独立实例的启动参数；不在这里启动进程。"""
    args = [
        f"--remote-debugging-port={int(port)}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        # 让 Chromium 把网页内容也暴露到可访问性树；截图/兜底 UIA 都用得上
        "--force-renderer-accessibility",
    ]
    for item in extra_args or []:
        text = str(item).strip()
        if text and text not in args:
            args.append(text)
    return args


def launch_edge(
    edge_path: str, args: List[str], log=None
) -> subprocess.Popen:
    """启动 Edge。DETACHED_PROCESS 保证服务端退出时不会带走浏览器。"""
    log = log or logger.bind(tag=TAG)
    log.info(f"启动 Edge | path={edge_path} | args={args}")
    return subprocess.Popen(
        [edge_path, *args],
        close_fds=True,
        creationflags=(
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        ),
    )


def _is_edge(browser_info: Dict[str, Any]) -> bool:
    name = str((browser_info or {}).get("Browser") or "").lower()
    return "edg" in name or "chrome" in name


# ── HTTP 端点（/json/*）─────────────────────────────────────────────────────
async def version_info(port: int, timeout: float = 1.0) -> Dict[str, Any]:
    url = f"http://127.0.0.1:{int(port)}/json/version"
    timeout_cfg = aiohttp.ClientTimeout(total=max(0.2, float(timeout)))
    try:
        async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    raise CDPUnavailable(
                        f"调试端口 {port} 返回 HTTP {response.status}"
                    )
                data = await response.json(content_type=None)
                return data if isinstance(data, dict) else {}
    except CDPUnavailable:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as error:
        raise CDPUnavailable(f"调试端口 {port} 不可用：{error}") from error


async def debug_ready(port: int, timeout: float = 0.8) -> bool:
    try:
        info = await version_info(port, timeout)
        return _is_edge(info)
    except CDPUnavailable:
        return False


async def list_targets(port: int, timeout: float = 2.0) -> List[Dict[str, Any]]:
    url = f"http://127.0.0.1:{int(port)}/json/list"
    timeout_cfg = aiohttp.ClientTimeout(total=max(0.3, float(timeout)))
    try:
        async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    raise CDPUnavailable(
                        f"调试端口 {port} 返回 HTTP {response.status}"
                    )
                data = await response.json(content_type=None)
    except CDPUnavailable:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as error:
        raise CDPUnavailable(f"读取 Edge 标签页失败：{error}") from error
    return [item for item in (data or []) if isinstance(item, dict)]


async def list_pages(port: int) -> List[Dict[str, Any]]:
    targets = await list_targets(port)
    return [
        item
        for item in targets
        if item.get("type") == "page" and item.get("webSocketDebuggerUrl")
    ]


async def pick_page(
    port: int, index: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """取一个页面目标。

    Chromium 的 ``/json/list`` 通常按“最近活跃”排序，第一项就是当前标签页；
    显式传 index 时按用户看到的标签页顺序取。
    """
    pages = await list_pages(port)
    if not pages:
        return None
    if index is not None:
        try:
            idx = int(index)
        except (TypeError, ValueError):
            idx = 0
        if 1 <= idx <= len(pages):
            return pages[idx - 1]
    return pages[0]


async def _json_endpoint(
    port: int, path: str, method: str = "GET", timeout: float = 2.0
) -> Any:
    url = f"http://127.0.0.1:{int(port)}{path}"
    timeout_cfg = aiohttp.ClientTimeout(total=max(0.3, float(timeout)))
    try:
        async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
            async with session.request(method, url) as response:
                if response.status >= 400:
                    raise CDPError(f"{path} 返回 HTTP {response.status}")
                text = await response.text()
                try:
                    return json.loads(text)
                except ValueError:
                    return text
    except CDPError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
        raise CDPError(f"{path} 请求失败：{error}") from error


async def activate_page(port: int, target_id: str) -> bool:
    if not target_id:
        return False
    await _json_endpoint(port, f"/json/activate/{quote(target_id)}")
    return True


async def close_page(port: int, target_id: str) -> bool:
    if not target_id:
        return False
    await _json_endpoint(port, f"/json/close/{quote(target_id)}")
    return True


async def new_page(port: int, url: str = "about:blank") -> Optional[Dict[str, Any]]:
    """新开标签页。新版 Chromium 要求 PUT /json/new，GET 只作为旧版兜底。"""
    safe_url = url or "about:blank"
    try:
        result = await _json_endpoint(
            port, f"/json/new?{quote(safe_url, safe=':/?&=%#')}", method="PUT"
        )
    except CDPError:
        result = await _json_endpoint(
            port, f"/json/new?{quote(safe_url, safe=':/?&=%#')}", method="GET"
        )
    return result if isinstance(result, dict) else None


async def ensure_edge(
    edge_path: str,
    port: int,
    profile: str,
    extra_args: Optional[List[Any]] = None,
    start_timeout: float = 15.0,
    log=None,
) -> Dict[str, Any]:
    """确保独立实例的调试端口可用；不可用时启动它。"""
    log = log or logger.bind(tag=TAG)

    try:
        info = await version_info(port, 1.0)
        if _is_edge(info):
            return info
        raise CDPUnavailable(
            f"端口 {port} 已被其他程序占用：{info.get('Browser') or '未知程序'}"
        )
    except CDPUnavailable as error:
        # 端口没开（最常见），继续走启动流程；若是明确的占用错误则直接抛
        if "已被其他程序占用" in str(error):
            raise

    executable = str(edge_path or "").strip() or find_edge_executable()
    if not executable:
        raise CDPUnavailable(
            "没找到 Microsoft Edge 的安装路径，请在配置里填写 browser_control.edge_path。"
        )
    if not os.path.exists(executable):
        raise CDPUnavailable(f"Edge 程序不存在：{executable}")

    profile_dir = resolve_profile_dir(profile)
    try:
        os.makedirs(profile_dir, exist_ok=True)
    except OSError as error:
        raise CDPUnavailable(f"创建 Edge 独立配置目录失败：{error}") from error

    args = profile_launch_args(port, profile_dir, extra_args)
    try:
        launch_edge(executable, args, log=log)
    except OSError as error:
        raise CDPUnavailable(f"启动 Edge 失败：{error}") from error

    deadline = asyncio.get_running_loop().time() + max(3.0, float(start_timeout))
    last_error: Optional[Exception] = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            info = await version_info(port, 0.8)
            if _is_edge(info):
                log.info(
                    f"Edge 调试端口已就绪 | port={port} | profile={profile_dir}"
                )
                return info
        except CDPUnavailable as error:
            last_error = error
        await asyncio.sleep(0.4)

    raise CDPUnavailable(
        f"Edge 已启动，但调试端口 {port} 在 {int(start_timeout)} 秒内没有就绪"
        f"（{last_error}）。如果一直失败，可以关闭 Edge 后删除 {profile_dir} 再试。"
    )


# ── CDP WebSocket 会话 ──────────────────────────────────────────────────────
class CDPSession:
    """一个页面目标的 CDP 会话；用完即关，避免长连接状态污染。"""

    def __init__(self, ws_url: str, timeout: float = 10.0) -> None:
        self.ws_url = ws_url
        self.timeout = float(timeout)
        self._ws = None
        self._seq = 0
        self._lock = asyncio.Lock()
        self._enabled = False

    async def __aenter__(self) -> "CDPSession":
        try:
            self._ws = await websockets.connect(
                self.ws_url,
                max_size=MAX_WS_SIZE,
                open_timeout=min(5.0, self.timeout),
                # 不发 Origin：Chromium 只接受没有 Origin 的本地调试连接
                origin=None,
            )
        except Exception as error:
            raise CDPUnavailable(f"连接 Edge 调试通道失败：{error}") from error
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def call(self, method: str, timeout: Optional[float] = None, **params):
        if self._ws is None:
            raise CDPError("CDP 会话尚未建立")
        wait = float(timeout or self.timeout)
        async with self._lock:
            self._seq += 1
            message_id = self._seq
            payload = {
                "id": message_id,
                "method": method,
                "params": params,
            }
            try:
                await self._ws.send(json.dumps(payload, ensure_ascii=False))
                deadline = asyncio.get_running_loop().time() + wait
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise CDPError(f"{method} 超时（{wait:.1f}s）")
                    raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
                    data = json.loads(raw)
                    if data.get("id") != message_id:
                        continue  # 跳过 Page.loadEventFired 之类的事件帧
                    if "error" in data:
                        error = data.get("error") or {}
                        raise CDPError(
                            f"{method} 失败：{error.get('message') or error}"
                        )
                    return data.get("result", {})
            except asyncio.TimeoutError as error:
                raise CDPError(f"{method} 超时（{wait:.1f}s）") from error
            except (websockets.WebSocketException, OSError) as error:
                raise CDPError(f"{method} 通信失败：{error}") from error

    async def enable_page(self) -> None:
        if self._enabled:
            return
        for method in ("Page.enable", "Runtime.enable"):
            try:
                await self.call(method, timeout=3.0)
            except CDPError:
                pass
        self._enabled = True

    async def evaluate(self, expression: str, timeout: float = 10.0):
        result = await self.call(
            "Runtime.evaluate",
            timeout=timeout,
            expression=expression,
            returnByValue=True,
            awaitPromise=True,
        )
        if result.get("exceptionDetails"):
            details = result.get("exceptionDetails") or {}
            text = (
                (details.get("exception") or {}).get("description")
                or details.get("text")
                or "页面脚本执行失败"
            )
            raise CDPError(str(text)[:300])
        return (result.get("result") or {}).get("value")


# ── 页面操作 ────────────────────────────────────────────────────────────────
JS_OBSERVE = r"""
(() => {
  const MAX_ITEMS = __MAX_ITEMS__;
  const TEXT_LIMIT = __TEXT_LIMIT__;
  document.querySelectorAll('[data-xz-idx]').forEach((el) => el.removeAttribute('data-xz-idx'));
  const selector = 'a[href], button, [role="button"], input:not([type="hidden"]), textarea, select, [contenteditable="true"]';
  // 搜索结果经常排在几十个导航项之后（GitHub 实测约第 68 个才开始），
  // 所以不能只扫前 MAX_ITEMS 个就停；先收集结果项，再补其它项。
  const results = [];
  const others = [];
  const seen = new Set();
  const scanLimit = Math.max(MAX_ITEMS * 8, 200);
  let index = 0;
  let scanned = 0;
  for (const el of document.querySelectorAll(selector)) {
    if (seen.has(el)) continue;
    seen.add(el);
    if (scanned >= scanLimit) break;
    const rect = el.getBoundingClientRect();
    if (rect.width < 8 || rect.height < 8) continue;
    if (rect.bottom < -50 || rect.top > window.innerHeight * 4) continue;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue;
    let text = (
      el.innerText || el.value || el.getAttribute('aria-label') ||
      el.getAttribute('placeholder') || el.getAttribute('title') || ''
    ).replace(/\s+/g, ' ').trim();
    if (!text) text = String(el.getAttribute('name') || el.id || '').trim();
    if (!text) continue;
    if (text.length > 80) text = text.slice(0, 80);
    index += 1;
    scanned += 1;
    const entry = {
      idx: index,
      tag: el.tagName.toLowerCase(),
      type: String(el.getAttribute('type') || '').toLowerCase(),
      text: text,
      href: el.href || '',
      heading: !!(el.closest('h1,h2,h3,h4,[role="heading"]') ||
                  (el.querySelector && el.querySelector('h1,h2,h3,h4'))),
      result: !!(el.closest(
        '[data-testid="results-list"], #search-results, div.search-title, ' +
        'li.b_algo, .g, [data-testid="result"]'
      ))
    };
    el.setAttribute('data-xz-idx', String(index));
    if (entry.result) results.push(entry); else others.push(entry);
  }
  const resultLimit = Math.min(20, Math.max(10, MAX_ITEMS));
  const strongResults = results.filter((item) => item.heading);
  const weakResults = results.filter((item) => !item.heading);
  const pickedResults = strongResults
    .slice(0, resultLimit)
    .concat(weakResults.slice(0, Math.max(0, resultLimit - strongResults.length)));
  const items = pickedResults.concat(
    others.slice(0, Math.max(0, MAX_ITEMS - pickedResults.length))
  );
  const body = document.body ? document.body.innerText : '';
  return {
    title: document.title || '',
    url: location.href,
    ready: document.readyState,
    items: items,
    text: String(body || '').replace(/\n{3,}/g, '\n\n').slice(0, TEXT_LIMIT)
  };
})()
"""

JS_CLICK_INDEX = r"""
(() => {
  const el = document.querySelector('[data-xz-idx="__IDX__"]');
  if (!el) return {ok: false, reason: 'index-gone'};
  el.scrollIntoView({block: 'center', inline: 'center'});
  const rect = el.getBoundingClientRect();
  if (rect.width < 4 || rect.height < 4) return {ok: false, reason: 'invisible'};
  try { if (el.focus) el.focus({preventScroll: true}); } catch (e) {}
  el.click();
  return {
    ok: true,
    tag: el.tagName.toLowerCase(),
    text: String(el.innerText || el.value || '').replace(/\s+/g, ' ').trim().slice(0, 80)
  };
})()
"""

JS_CLICK_TEXT = r"""
(() => {
  const needle = __TEXT_JSON__;
  const selector = 'a[href], button, [role="button"], [data-xz-idx]';
  const matches = [];
  for (const el of document.querySelectorAll(selector)) {
    const text = String(
      el.innerText || el.value || el.getAttribute('aria-label') || ''
    ).replace(/\s+/g, ' ').trim();
    if (!text.includes(needle)) continue;
    const rect = el.getBoundingClientRect();
    if (rect.width < 8 || rect.height < 8) continue;
    matches.push({el: el, text: text});
  }
  if (!matches.length) return {ok: false, reason: 'not-found'};
  matches.sort((a, b) => a.text.length - b.text.length);
  const chosen = matches[0];
  chosen.el.scrollIntoView({block: 'center', inline: 'center'});
  try { if (chosen.el.focus) chosen.el.focus({preventScroll: true}); } catch (e) {}
  chosen.el.click();
  return {ok: true, text: chosen.text.slice(0, 80), matched: matches.length};
})()
"""

JS_FILL = r"""
(() => {
  const idx = __IDX_JSON__;
  const needle = __TEXT_JSON__;
  const value = __VALUE_JSON__;
  let el = null;
  if (idx) el = document.querySelector('[data-xz-idx="' + idx + '"]');
  if (!el && !idx && !needle) el = document.activeElement;
  if (!el && needle) {
    const selector = 'input:not([type="hidden"]), textarea, select, [contenteditable="true"]';
    const matches = [];
    for (const node of document.querySelectorAll(selector)) {
      const label = node.labels
        ? Array.from(node.labels).map((item) => item.innerText).join(' ')
        : '';
      const text = (
        label + ' ' + String(node.getAttribute('aria-label') || '') + ' ' +
        String(node.getAttribute('placeholder') || '') + ' ' +
        String(node.getAttribute('name') || '') + ' ' + String(node.id || '')
      ).replace(/\s+/g, ' ').trim();
      if (!text.includes(needle)) continue;
      const rect = node.getBoundingClientRect();
      if (rect.width < 4 || rect.height < 4) continue;
      matches.push({node: node, text: text});
    }
    if (matches.length) {
      matches.sort((a, b) => a.text.length - b.text.length);
      el = matches[0].node;
    }
  }
  if (!el) return {ok: false, reason: 'not-found'};
  if (String(el.type || '').toLowerCase() === 'password') {
    return {ok: false, reason: 'password'};
  }
  const editable = el.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName);
  if (!editable) return {ok: false, reason: 'not-editable'};

  el.scrollIntoView({block: 'center'});
  try { if (el.focus) el.focus({preventScroll: true}); } catch (e) {}
  if (el.isContentEditable) {
    el.innerText = value;
    el.dispatchEvent(new InputEvent('input', {
      bubbles: true, data: value, inputType: 'insertText'
    }));
  } else if (el.tagName === 'SELECT') {
    el.value = value;
    el.dispatchEvent(new Event('change', {bubbles: true}));
  } else {
    const proto = el instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (setter) setter.call(el, value); else el.value = value;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  }
  return {
    ok: true,
    tag: el.tagName.toLowerCase(),
    value: el.value !== undefined ? el.value : el.innerText
  };
})()
"""

JS_READ_TEXT = r"""
(() => {
  const body = document.body ? document.body.innerText : '';
  return {
    title: document.title || '',
    url: location.href,
    ready: document.readyState,
    text: String(body || '').replace(/\n{3,}/g, '\n\n').slice(0, __TEXT_LIMIT__)
  };
})()
"""

JS_PAGE_STATE = r"""
({title: document.title || '', url: location.href,
  ready: document.readyState, visibility: document.visibilityState})
"""

JS_BACK = "(() => { history.back(); return {ok: true}; })()"
JS_FORWARD = "(() => { history.forward(); return {ok: true}; })()"
JS_RELOAD = "(() => { location.reload(); return {ok: true}; })()"
JS_SCROLL = r"""
(() => { window.scrollBy(0, __DY__); return {ok: true, y: window.scrollY}; })()
"""


def _js_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def read_page(
    cdp: CDPSession,
    max_items: int = 40,
    text_limit: int = 3000,
) -> Dict[str, Any]:
    expression = JS_OBSERVE.replace("__MAX_ITEMS__", str(max(1, max_items))).replace(
        "__TEXT_LIMIT__", str(max(0, text_limit))
    )
    data = await cdp.evaluate(expression)
    return data if isinstance(data, dict) else {}


async def read_text(cdp: CDPSession, text_limit: int = 3000) -> Dict[str, Any]:
    expression = JS_READ_TEXT.replace("__TEXT_LIMIT__", str(max(0, text_limit)))
    data = await cdp.evaluate(expression)
    return data if isinstance(data, dict) else {}


async def page_state(cdp: CDPSession) -> Dict[str, Any]:
    data = await cdp.evaluate(JS_PAGE_STATE)
    return data if isinstance(data, dict) else {}


async def click_index(cdp: CDPSession, index: int) -> Dict[str, Any]:
    result = await cdp.evaluate(
        JS_CLICK_INDEX.replace("__IDX__", str(_js_int(index, 0)))
    )
    return result if isinstance(result, dict) else {"ok": False}


async def click_text(cdp: CDPSession, text: str) -> Dict[str, Any]:
    result = await cdp.evaluate(
        JS_CLICK_TEXT.replace("__TEXT_JSON__", json.dumps(text, ensure_ascii=False))
    )
    return result if isinstance(result, dict) else {"ok": False}


async def fill(
    cdp: CDPSession,
    value: str,
    index: Optional[int] = None,
    text: str = "",
) -> Dict[str, Any]:
    expression = (
        JS_FILL.replace(
            "__IDX_JSON__",
            json.dumps(str(_js_int(index, 0)) if index else "", ensure_ascii=False),
        )
        .replace("__TEXT_JSON__", json.dumps(text or "", ensure_ascii=False))
        .replace("__VALUE_JSON__", json.dumps(value or "", ensure_ascii=False))
    )
    result = await cdp.evaluate(expression)
    return result if isinstance(result, dict) else {"ok": False}


async def wait_document(cdp: CDPSession, timeout: float = 12.0) -> str:
    """等页面进入 interactive/complete；SPA 只等 complete 会太早，再补一点时间。"""
    deadline = asyncio.get_running_loop().time() + max(1.0, float(timeout))
    last = ""
    while asyncio.get_running_loop().time() < deadline:
        try:
            state = await cdp.evaluate("document.readyState", timeout=3.0)
        except CDPError:
            state = None
        if isinstance(state, str):
            last = state
            if state in ("interactive", "complete"):
                await asyncio.sleep(0.35)
                return state
        await asyncio.sleep(0.2)
    return last


async def navigate(cdp: CDPSession, url: str, timeout: float = 12.0) -> None:
    result = await cdp.call("Page.navigate", timeout=min(10.0, timeout), url=url)
    error_text = str((result or {}).get("errorText") or "").strip()
    if error_text:
        raise CDPError(f"打开网址失败：{error_text}")
    await wait_document(cdp, timeout)


async def capture_screenshot(cdp: CDPSession, timeout: float = 15.0) -> str:
    """截当前标签页（不是整块桌面），返回 base64 JPEG。"""
    try:
        await cdp.call("Page.bringToFront", timeout=3.0)
    except CDPError:
        pass
    result = await cdp.call(
        "Page.captureScreenshot",
        timeout=timeout,
        format="jpeg",
        quality=60,
        fromSurface=True,
    )
    data = str((result or {}).get("data") or "")
    if not data:
        raise CDPError("截图返回为空")
    return data


_KEY_SPECS: Dict[str, tuple] = {
    "enter": (13, "Enter", "Enter", "\r"),
    "return": (13, "Enter", "Enter", "\r"),
    "回车": (13, "Enter", "Enter", "\r"),
    "确认": (13, "Enter", "Enter", "\r"),
    "escape": (27, "Escape", "Escape", ""),
    "esc": (27, "Escape", "Escape", ""),
    "退出": (27, "Escape", "Escape", ""),
    "tab": (9, "Tab", "Tab", "\t"),
    "backspace": (8, "Backspace", "Backspace", ""),
    "退格": (8, "Backspace", "Backspace", ""),
    "arrowdown": (40, "ArrowDown", "ArrowDown", ""),
    "down": (40, "ArrowDown", "ArrowDown", ""),
    "下": (40, "ArrowDown", "ArrowDown", ""),
    "arrowup": (38, "ArrowUp", "ArrowUp", ""),
    "up": (38, "ArrowUp", "ArrowUp", ""),
    "上": (38, "ArrowUp", "ArrowUp", ""),
    "space": (32, " ", "Space", " "),
    "空格": (32, " ", "Space", " "),
}


async def press_key(cdp: CDPSession, key: str, timeout: float = 5.0) -> str:
    """向当前焦点发送一次按键（Enter/Escape/Tab/方向键等）。

    比在页面里 ``new KeyboardEvent`` 更可靠：GitHub 这类站点的搜索框
    只认真实按键，合成事件不会触发提交。
    """
    name = str(key or "").strip()
    spec = _KEY_SPECS.get(name.lower()) or _KEY_SPECS.get(name)
    if not spec:
        raise CDPError(f"不支持的按键：{key}")
    virtual_key, key_name, code, text = spec

    down = {
        "type": "keyDown",
        "key": key_name,
        "code": code,
        "windowsVirtualKeyCode": virtual_key,
        "nativeVirtualKeyCode": virtual_key,
    }
    if text:
        down["text"] = text
        down["unmodifiedText"] = text
    await cdp.call("Input.dispatchKeyEvent", timeout=timeout, **down)
    await cdp.call(
        "Input.dispatchKeyEvent",
        timeout=timeout,
        type="keyUp",
        key=key_name,
        code=code,
        windowsVirtualKeyCode=virtual_key,
        nativeVirtualKeyCode=virtual_key,
    )
    return key_name


async def scroll(cdp: CDPSession, delta_y: int = 600) -> Dict[str, Any]:
    result = await cdp.evaluate(JS_SCROLL.replace("__DY__", str(_js_int(delta_y, 600))))
    return result if isinstance(result, dict) else {}


async def go_back(cdp: CDPSession) -> None:
    await cdp.evaluate(JS_BACK)
    await wait_document(cdp, 8.0)


async def go_forward(cdp: CDPSession) -> None:
    await cdp.evaluate(JS_FORWARD)
    await wait_document(cdp, 8.0)


async def reload(cdp: CDPSession) -> None:
    await cdp.evaluate(JS_RELOAD)
    await wait_document(cdp, 12.0)


# ── 清理 ────────────────────────────────────────────────────────────────────
def kill_profile_processes(profile_dir: str, wait: float = 3.0) -> int:
    """结束使用指定 user-data-dir 的 Edge 进程（只用于自检/退出时清理）。"""
    target = os.path.normcase(os.path.abspath(resolve_profile_dir(profile_dir)))
    if not target:
        return 0
    victims: List[psutil.Process] = []
    for proc in psutil.process_iter(["name"]):
        try:
            name = str(proc.info.get("name") or "").lower()
            if name not in ("msedge.exe", "msedgewebview2.exe"):
                continue
            cmdline = " ".join(proc.cmdline())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if target.lower() in os.path.normcase(cmdline).lower():
            victims.append(proc)

    for proc in victims:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    deadline = time.time() + max(0.5, float(wait))
    while time.time() < deadline:
        if not any(proc.is_running() for proc in victims):
            return len(victims)
        time.sleep(0.2)

    for proc in victims:
        if proc.is_running():
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    return len(victims)


# ── 兼容性辅助 ──────────────────────────────────────────────────────────────
def normalize_url(raw: str) -> str:
    """只允许 http(s) / about:blank；把“example.com”补成 https。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith(("http://", "https://")):
        return text
    if lowered == "about:blank":
        return text
    if re.match(r"^(?:localhost|127\.0\.0\.1)(?::\d+)?(?:/|$)", text, re.I):
        return "http://" + text
    if re.match(r"^[\w.-]+\.[a-zA-Z]{2,}(?::\d+)?(?:/|$)", text):
        return "https://" + text
    return ""


def is_windows() -> bool:
    return sys.platform.startswith("win")
