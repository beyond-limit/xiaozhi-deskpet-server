"""本机软件目录：按需扫描、持久化与手动登记。"""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

IS_WINDOWS = sys.platform.startswith("win")
SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CATALOG_FILE = os.path.join(SERVER_ROOT, "data", "local_apps.json")
_CATALOG_LOCK = threading.RLock()

_BLOCKED_NAME_PARTS = (
    "uninstall",
    "uninstaller",
    "setup",
    "installer",
    "updater",
    "update helper",
    "crash handler",
    " help",
    "manual",
    "documentation",
    "readme",
    "release notes",
    "website",
    "homepage",
    "powershell prompt",
    "remove ",
    "卸载",
    "安装程序",
    "更新程序",
    "修复工具",
    "官网",
    "帮助",
    "用户手册",
    "使用说明",
)
_BLOCKED_EXECUTABLES = {
    "cmd.exe",
    "cscript.exe",
    "msiexec.exe",
    "powershell.exe",
    "pwsh.exe",
    "regsvr32.exe",
    "rundll32.exe",
    "wscript.exe",
    # 以下是系统/共享宿主进程：商店应用清单偶尔把它们当 Executable，
    # 按进程名去关等于杀掉一片无关窗口，宁可不允许关闭
    "applicationframehost.exe",
    "backgroundtaskhost.exe",
    "explorer.exe",
    "runtimebroker.exe",
    "searchapp.exe",
    "shellexperiencehost.exe",
    "startmenuexperiencehost.exe",
    "textinputhost.exe",
}
_ALLOWED_SUFFIXES = (".exe", ".com", ".bat", ".cmd", ".lnk", ".url")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalize(value: Any) -> str:
    text = str(value or "").strip().lower()
    for source, target in (("（", "("), ("）", ")"), ("　", "")):
        text = text.replace(source, target)
    return "".join(text.split())


def _stable_id(prefix: str, name: str, path: str) -> str:
    digest = hashlib.sha1(f"{name}\0{path}".encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _empty_catalog() -> Dict[str, Any]:
    return {
        "version": 1,
        "updated_at": "",
        "scanned_at": "",
        "manual": [],
        "scanned": [],
        # 用户在软件管理里改的「是否允许语音关闭」，按软件名归一化键保存。
        # 单独存一份而不是写回 manual/scanned 条目：重新扫描会整体替换 scanned，
        # 手工/内置条目的 close 也可能来自配置文件，覆盖表能让设置跨扫描保留。
        "overrides": {"close": {}},
    }


def load_catalog() -> Dict[str, Any]:
    with _CATALOG_LOCK:
        try:
            with open(CATALOG_FILE, "r", encoding="utf-8-sig") as handle:
                raw = json.load(handle)
        except (OSError, ValueError, TypeError):
            return _empty_catalog()

    if not isinstance(raw, dict):
        return _empty_catalog()
    result = _empty_catalog()
    result.update(raw)
    result["manual"] = [item for item in (raw.get("manual") or []) if isinstance(item, dict)]
    result["scanned"] = [item for item in (raw.get("scanned") or []) if isinstance(item, dict)]
    close_map: Dict[str, str] = {}
    overrides = raw.get("overrides")
    if isinstance(overrides, dict):
        for key, value in (overrides.get("close") or {}).items():
            mode = str(value or "").strip().lower()
            if str(key).strip() and mode in ("graceful", "off"):
                close_map[str(key).strip()] = mode
    result["overrides"] = {"close": close_map}
    return result


def _write_catalog(catalog: Dict[str, Any]) -> None:
    with _CATALOG_LOCK:
        os.makedirs(os.path.dirname(CATALOG_FILE), exist_ok=True)
        temporary = f"{CATALOG_FILE}.{os.getpid()}.tmp"
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(catalog, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, CATALOG_FILE)


def _path_key(path: Any) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    if text.lower().startswith("shell:"):
        return text.lower()
    return os.path.normcase(os.path.abspath(os.path.expandvars(os.path.expanduser(text))))


def _path_available(path: Any) -> bool:
    text = str(path or "").strip()
    if not text:
        return False
    if IS_WINDOWS and text.lower().startswith("shell:appsfolder\\"):
        return True
    expanded = os.path.expandvars(os.path.expanduser(text))
    if os.path.dirname(expanded):
        return os.path.exists(expanded)
    return bool(shutil.which(expanded))


def _clean_text(value: Any, limit: int) -> str:
    return re.sub(r"[\x00-\x1f]", "", str(value or "")).strip()[:limit]


def _clean_process(value: Any) -> Any:
    values = value if isinstance(value, (list, tuple)) else [value]
    result = []
    for item in values:
        name = os.path.basename(_clean_text(item, 160))
        if name and name.lower().endswith((".exe", ".com")) and name.lower() not in _BLOCKED_EXECUTABLES:
            if name.lower() not in {entry.lower() for entry in result}:
                result.append(name)
    if not result:
        return ""
    return result[0] if len(result) == 1 else result


def _has_close_target(entry: Dict[str, Any]) -> bool:
    """该条目有没有可用来关闭进程的映像名（没有就只能禁止语音关闭）。"""
    raw = entry.get("process")
    if isinstance(raw, str) and raw.strip():
        return True
    if isinstance(raw, (list, tuple)) and any(str(item).strip() for item in raw):
        return True
    path = str(entry.get("path") or "").strip()
    return path.lower().endswith((".exe", ".com"))


def _clean_aliases(value: Any, name: str) -> List[str]:
    if isinstance(value, str):
        values = re.split(r"[,，;；\n]", value)
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        values = []
    result = []
    seen = {_normalize(name)}
    for item in values:
        alias = _clean_text(item, 80)
        key = _normalize(alias)
        if alias and key and key not in seen:
            seen.add(key)
            result.append(alias)
        if len(result) >= 16:
            break
    return result


def _candidate_allowed(name: str, path: str, target: str = "") -> bool:
    lowered_name = name.lower()
    if not name or any(part in lowered_name for part in _BLOCKED_NAME_PARTS):
        return False
    check_path = target or path
    if str(check_path).lower().startswith("shell:appsfolder\\"):
        return True
    expanded = os.path.expandvars(os.path.expanduser(str(check_path or "").strip().strip('"')))
    suffix = os.path.splitext(expanded)[1].lower()
    if suffix not in _ALLOWED_SUFFIXES or not os.path.exists(expanded):
        return False
    return os.path.basename(expanded).lower() not in _BLOCKED_EXECUTABLES


def _clean_entry(entry: Dict[str, Any], source: str, require_existing: bool = True) -> Dict[str, Any]:
    name = _clean_text(entry.get("name"), 100)
    path = _clean_text(entry.get("path"), 1000)
    if not name:
        raise ValueError("软件名称不能为空")
    if not path:
        raise ValueError("程序路径不能为空")

    if not path.lower().startswith("shell:"):
        path = os.path.abspath(os.path.expandvars(os.path.expanduser(path)))
    if require_existing and not _path_available(path):
        raise ValueError("程序文件不存在，请重新选择")
    if not path.lower().startswith("shell:") and os.path.splitext(path)[1].lower() not in _ALLOWED_SUFFIXES:
        raise ValueError("仅支持 exe、com、bat、cmd、lnk 或 url 程序")

    process = _clean_process(entry.get("process"))
    if not process and path.lower().endswith((".exe", ".com")):
        process = os.path.basename(path)

    item: Dict[str, Any] = {
        "id": _clean_text(entry.get("id"), 80) or _stable_id(source, name, path),
        "name": name,
        "path": path,
        "aliases": _clean_aliases(entry.get("aliases"), name),
        "input": bool(entry.get("input", False)),
        "submit": bool(entry.get("submit", False)),
        "close": "graceful" if process and str(entry.get("close", "graceful")).lower() != "off" else "off",
    }
    if process:
        item["process"] = process
    return item


def _configured_entries(local_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = []
    for raw in local_config.get("apps", []) or []:
        if not isinstance(raw, dict) or not raw.get("name") or not raw.get("path"):
            continue
        item = dict(raw)
        item["id"] = _stable_id("configured", str(raw["name"]), str(raw["path"]))
        item["source"] = "configured"
        result.append(item)
    return result


def _with_source(entries: Iterable[Dict[str, Any]], source: str) -> List[Dict[str, Any]]:
    result = []
    for raw in entries:
        if not isinstance(raw, dict) or not raw.get("name") or not raw.get("path"):
            continue
        item = dict(raw)
        item["source"] = source
        item["id"] = item.get("id") or _stable_id(source, str(item["name"]), str(item["path"]))
        result.append(item)
    return result


def _merge_entries(local_config: Dict[str, Any], catalog: Dict[str, Any]) -> List[Dict[str, Any]]:
    groups = [
        _with_source(catalog.get("manual", []), "manual"),
        _configured_entries(local_config),
        _with_source(catalog.get("scanned", []), "scanned"),
    ]
    merged: List[Dict[str, Any]] = []
    by_name: Dict[str, Dict[str, Any]] = {}
    by_path: Dict[str, Dict[str, Any]] = {}

    for group in groups:
        for item in group:
            name_key = _normalize(item.get("name"))
            path_key = _path_key(item.get("path"))
            existing = by_name.get(name_key) or (by_path.get(path_key) if path_key else None)
            if existing is not None:
                aliases = list(existing.get("aliases") or [])
                aliases.extend(item.get("aliases") or [])
                if _normalize(item.get("name")) != _normalize(existing.get("name")):
                    aliases.append(str(item.get("name")))
                existing["aliases"] = _clean_aliases(aliases, str(existing.get("name")))
                if not _path_available(existing.get("path")) and _path_available(item.get("path")):
                    existing["path"] = item.get("path")
                    if item.get("process"):
                        existing["process"] = item.get("process")
                    by_path[_path_key(existing.get("path"))] = existing
                continue
            merged.append(item)
            if name_key:
                by_name[name_key] = item
            if path_key:
                by_path[path_key] = item

    # 用户在软件管理里改过的「是否允许语音关闭」优先于条目自带的 close；
    # 没有进程信息的条目一律 off（真去关也只会回"本来就没在运行"）。
    close_map = (catalog.get("overrides") or {}).get("close") or {}
    for item in merged:
        mode = str(close_map.get(_normalize(item.get("name"))) or "").strip().lower()
        if mode not in ("graceful", "off"):
            mode = "off" if str(item.get("close") or "").strip().lower() == "off" else "graceful"
        item["close"] = mode if _has_close_target(item) else "off"
    return merged


def get_effective_apps(local_config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    apps = _merge_entries(local_config or {}, load_catalog())
    return [{key: value for key, value in item.items() if key != "source"} for item in apps]


def get_catalog_state(local_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    catalog = load_catalog()
    apps = _merge_entries(local_config or {}, catalog)
    counts = {"manual": 0, "configured": 0, "scanned": 0}
    closable = 0
    for app in apps:
        source = str(app.get("source") or "scanned")
        if source in counts:
            counts[source] += 1
        # 给设置窗口判断"这一行的开关能不能点"（没有进程名就没法关）
        app["close_capable"] = _has_close_target(app)
        if app["close_capable"] and app.get("close") != "off":
            closable += 1
    return {
        "apps": apps,
        "counts": counts,
        "total": len(apps),
        "closable": closable,
        "scanned_at": catalog.get("scanned_at") or "",
        "updated_at": catalog.get("updated_at") or "",
    }


def replace_scanned_apps(entries: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    cleaned = []
    for raw in entries:
        try:
            item = _clean_entry(raw, "scanned", require_existing=True)
        except ValueError:
            continue
        item["id"] = _stable_id("scanned", item["name"], item["path"])
        cleaned.append(item)

    with _CATALOG_LOCK:
        catalog = load_catalog()
        catalog["scanned"] = cleaned
        catalog["scanned_at"] = _now()
        catalog["updated_at"] = catalog["scanned_at"]
        _write_catalog(catalog)
    return catalog


def add_manual_app(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("软件信息格式不正确")
    item = _clean_entry(payload, "manual", require_existing=True)
    item["id"] = f"manual-{uuid.uuid4().hex[:16]}"

    with _CATALOG_LOCK:
        catalog = load_catalog()
        manual = list(catalog.get("manual") or [])
        name_key = _normalize(item["name"])
        path_key = _path_key(item["path"])
        replaced = False
        for index, old in enumerate(manual):
            if _normalize(old.get("name")) == name_key or _path_key(old.get("path")) == path_key:
                item["id"] = old.get("id") or item["id"]
                manual[index] = item
                replaced = True
                break
        if not replaced:
            manual.append(item)
        catalog["manual"] = manual
        catalog["updated_at"] = _now()
        _write_catalog(catalog)
    return item


def remove_manual_app(app_id: str) -> bool:
    target = _clean_text(app_id, 80)
    with _CATALOG_LOCK:
        catalog = load_catalog()
        before = list(catalog.get("manual") or [])
        after = [item for item in before if str(item.get("id") or "") != target]
        if len(after) == len(before):
            return False
        catalog["manual"] = after
        catalog["updated_at"] = _now()
        _write_catalog(catalog)
    return True


def set_apps_close(
    app_ids: Iterable[Any], allow: bool, local_config: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """批量设置「是否允许语音关闭」。

    入参是合并后清单里的 id（设置窗口列表用的就是它），落盘到
    ``overrides.close``（键为软件名归一化值）：这样重新扫描覆盖 scanned、
    或条目来自 .config.yaml 时，用户的选择都不会丢。
    """
    wanted: List[str] = []
    for raw in app_ids or []:
        key = _clean_text(raw, 80)
        if key and key not in wanted:
            wanted.append(key)
    if not wanted:
        raise ValueError("请先选择要设置的软件")

    mode = "graceful" if allow else "off"
    updated: List[str] = []
    unsupported: List[str] = []
    missing: List[str] = []

    with _CATALOG_LOCK:
        catalog = load_catalog()
        merged = _merge_entries(local_config or {}, catalog)
        by_id = {str(item.get("id") or ""): item for item in merged}
        close_map = dict((catalog.get("overrides") or {}).get("close") or {})
        for app_id in wanted:
            item = by_id.get(app_id)
            name_key = _normalize(item.get("name")) if item else ""
            if not item or not name_key:
                missing.append(app_id)
                continue
            if not _has_close_target(item):
                # 没有进程名，允许了也关不掉，别写进覆盖表骗用户
                unsupported.append(str(item.get("name")))
                continue
            close_map[name_key] = mode
            updated.append(str(item.get("name")))
        if updated:
            overrides = dict(catalog.get("overrides") or {})
            overrides["close"] = close_map
            catalog["overrides"] = overrides
            catalog["updated_at"] = _now()
            _write_catalog(catalog)

    return {
        "updated": len(updated),
        "names": updated,
        "unsupported": unsupported,
        "missing": missing,
        "allow": bool(allow),
    }


def _unquote_executable(raw: Any) -> str:
    text = os.path.expandvars(str(raw or "").strip())
    if not text:
        return ""
    match = re.search(r'"([^"\r\n]+\.(?:exe|com))"|([^,\r\n]+\.(?:exe|com))', text, re.IGNORECASE)
    if not match:
        return ""
    return os.path.abspath((match.group(1) or match.group(2) or "").strip())


def _scan_registry() -> List[Dict[str, Any]]:
    if not IS_WINDOWS:
        return []
    import winreg

    result: List[Dict[str, Any]] = []
    uninstall_keys = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    )
    views = [0, getattr(winreg, "KEY_WOW64_64KEY", 0), getattr(winreg, "KEY_WOW64_32KEY", 0)]

    def read_value(key, name, default=""):
        try:
            return winreg.QueryValueEx(key, name)[0]
        except OSError:
            return default

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for key_path in uninstall_keys:
            for view in views:
                try:
                    parent = winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ | view)
                except OSError:
                    continue
                with parent:
                    index = 0
                    while True:
                        try:
                            child_name = winreg.EnumKey(parent, index)
                        except OSError:
                            break
                        index += 1
                        try:
                            child = winreg.OpenKey(parent, child_name)
                        except OSError:
                            continue
                        with child:
                            name = _clean_text(read_value(child, "DisplayName"), 100)
                            if not name or read_value(child, "SystemComponent", 0) == 1:
                                continue
                            path = _unquote_executable(read_value(child, "DisplayIcon"))
                            if not _candidate_allowed(name, path):
                                continue
                            result.append(
                                {
                                    "name": name,
                                    "path": path,
                                    "process": os.path.basename(path),
                                    "aliases": [],
                                    "input": False,
                                    "submit": False,
                                }
                            )

        app_paths = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
        for view in views:
            try:
                parent = winreg.OpenKey(hive, app_paths, 0, winreg.KEY_READ | view)
            except OSError:
                continue
            with parent:
                index = 0
                while True:
                    try:
                        executable_name = winreg.EnumKey(parent, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        child = winreg.OpenKey(parent, executable_name)
                        path = _unquote_executable(winreg.QueryValue(child, None))
                        child.Close()
                    except OSError:
                        continue
                    name = os.path.splitext(executable_name)[0]
                    if not _candidate_allowed(name, path):
                        continue
                    result.append(
                        {
                            "name": name,
                            "path": path,
                            "process": os.path.basename(path),
                            "aliases": [],
                            "input": False,
                            "submit": False,
                        }
                    )
    return result


def _run_powershell_json(script: str, extra_env: Optional[Dict[str, str]] = None) -> Any:
    powershell = shutil.which("powershell.exe") or os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
    )
    if not os.path.exists(powershell):
        return []
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    env = os.environ.copy()
    env.update(extra_env or {})
    try:
        completed = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True,
            timeout=25,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    output = completed.stdout.decode("utf-8-sig", errors="replace").strip()
    if not output:
        return []
    try:
        return json.loads(output)
    except ValueError:
        return []


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    return [value] if value else []


def _start_menu_roots() -> List[str]:
    roots = [
        os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Microsoft", "Windows", "Start Menu", "Programs"),
        os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
    ]
    return [root for root in roots if root and os.path.isdir(root)]


def _scan_start_menu() -> List[Dict[str, Any]]:
    roots = _start_menu_roots()
    if not roots:
        return []
    script = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$roots = @($env:XIAOZHI_START_MENU_ROOTS | ConvertFrom-Json)
$shell = New-Object -ComObject WScript.Shell
$items = @()
foreach ($root in $roots) {
  Get-ChildItem -LiteralPath $root -Recurse -File -Filter '*.lnk' | ForEach-Object {
    $target = $shell.CreateShortcut($_.FullName).TargetPath
    $items += [PSCustomObject]@{ Name = $_.BaseName; Path = $_.FullName; Target = $target }
  }
}
@($items) | ConvertTo-Json -Compress -Depth 3
"""
    raw_items = _run_powershell_json(
        script, {"XIAOZHI_START_MENU_ROOTS": json.dumps(roots, ensure_ascii=False)}
    )
    result = []
    for raw in _as_list(raw_items):
        if not isinstance(raw, dict):
            continue
        name = _clean_text(raw.get("Name"), 100)
        link = _clean_text(raw.get("Path"), 1000)
        target = _clean_text(raw.get("Target"), 1000)
        if not _candidate_allowed(name, link, target or link):
            continue
        item: Dict[str, Any] = {
            "name": name,
            "path": link,
            "aliases": [],
            "input": False,
            "submit": False,
        }
        if target.lower().endswith((".exe", ".com")):
            item["process"] = os.path.basename(target)
        result.append(item)
    return result


def _scan_start_apps() -> List[Dict[str, Any]]:
    """扫描「开始菜单应用」。商店应用的 AppID 会解析出真实可执行文件。

    AppID 形如 ``<PackageFamilyName>!<ApplicationId>``，只是包的标识，不是 exe；
    以前只拿到 AppID，条目没有进程名，于是"记事本"这类商店应用被判成不可关闭。
    这里顺带读包清单的 ``Application.Executable``，让它也能按进程名关掉。
    """
    script = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$apps = @(Get-StartApps | Select-Object Name, AppID)
# 只解析开始菜单里出现的包，别把 178 个包的清单全读一遍
$needed = @{}
foreach ($a in $apps) {
  if ($a.AppID -match '^(?<pfn>[^!]+)!(?<app>.+)$') { $needed[$matches['pfn']] = $true }
}
$map = @{}
foreach ($pkg in Get-AppxPackage) {
  if (-not $needed.ContainsKey($pkg.PackageFamilyName)) { continue }
  $manifest = Join-Path $pkg.InstallLocation 'AppxManifest.xml'
  if (-not (Test-Path -LiteralPath $manifest)) { continue }
  try { [xml]$xml = Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 } catch { continue }
  foreach ($app in @($xml.Package.Applications.Application)) {
    if (-not $app.Executable) { continue }
    $map[$pkg.PackageFamilyName + '!' + $app.Id] = $app.Executable
  }
}
$items = @()
foreach ($a in $apps) {
  $target = ''
  if ($a.AppID -and $map.ContainsKey($a.AppID)) { $target = $map[$a.AppID] }
  $items += [PSCustomObject]@{ Name = $a.Name; AppID = $a.AppID; Target = $target }
}
@($items) | ConvertTo-Json -Compress -Depth 3
"""
    result = []
    for raw in _as_list(_run_powershell_json(script)):
        if not isinstance(raw, dict):
            continue
        name = _clean_text(raw.get("Name"), 100)
        app_id = _clean_text(raw.get("AppID"), 500)
        path = f"shell:AppsFolder\\{app_id}" if app_id else ""
        if not _candidate_allowed(name, path):
            continue
        item: Dict[str, Any] = {
            "name": name,
            "path": path,
            "aliases": [],
            "input": False,
            "submit": False,
        }
        # 清单里写的是相对路径（如 Notepad\Notepad.exe），只取文件名给 _find_processes 用；
        # 真正的白名单校验仍在 _clean_entry → _clean_process（会挡掉渲染宿主这类共享进程）
        executable = os.path.basename(str(raw.get("Target") or "").strip().replace("/", "\\"))
        if executable.lower().endswith((".exe", ".com")):
            item["process"] = executable
        result.append(item)
    return result


def _dedupe_scanned(entries: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    by_name: Dict[str, Dict[str, Any]] = {}
    by_path: Dict[str, Dict[str, Any]] = {}
    for raw in entries:
        name = _clean_text(raw.get("name"), 100)
        path = _clean_text(raw.get("path"), 1000)
        if not name or not path:
            continue
        name_key = _normalize(name)
        path_key = _path_key(path)
        existing = by_path.get(path_key) or by_name.get(name_key)
        if existing is not None:
            aliases = list(existing.get("aliases") or [])
            if _normalize(existing.get("name")) != name_key:
                aliases.append(name)
            aliases.extend(raw.get("aliases") or [])
            existing["aliases"] = _clean_aliases(aliases, str(existing.get("name")))
            if not existing.get("process") and raw.get("process"):
                existing["process"] = raw.get("process")
                existing["close"] = "graceful"
            continue
        item = dict(raw)
        item["aliases"] = _clean_aliases(item.get("aliases"), name)
        item["close"] = "graceful" if item.get("process") else "off"
        result.append(item)
        by_name[name_key] = item
        by_path[path_key] = item
    return sorted(result, key=lambda item: str(item.get("name") or "").casefold())


def scan_installed_apps() -> List[Dict[str, Any]]:
    """执行一次全新扫描；调用方决定是否持久化，不在后台自动运行。"""
    if not IS_WINDOWS:
        return []
    return _dedupe_scanned([*_scan_registry(), *_scan_start_menu(), *_scan_start_apps()])


def refresh_connections(connections: Iterable[Any]) -> int:
    """让现有连接立刻重建工具描述与意图提示词。"""
    refreshed = 0
    seen_intents = set()
    for conn in list(connections or []):
        handler = getattr(conn, "func_handler", None)
        if handler is not None:
            try:
                handler.upload_functions_desc()
                refreshed += 1
            except Exception:
                pass
        intent = getattr(conn, "intent", None)
        if intent is None or id(intent) in seen_intents:
            continue
        seen_intents.add(id(intent))
        try:
            if hasattr(intent, "promot"):
                intent.promot = ""
            cache_manager = getattr(intent, "cache_manager", None)
            cache_type = getattr(intent, "CacheType", None)
            if cache_manager is not None and cache_type is not None:
                cache_manager.clear(cache_type.INTENT)
        except Exception:
            pass
    return refreshed
