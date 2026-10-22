"""文件查找范围设置：桌宠右键菜单「查找范围设置」的可写数据。

与 `local_app_catalog` 同一个思路：**不重写 `data/.config.yaml`**
（那个文件里有大量注释，程序化写回会把注释和排版全丢掉），
用户在设置窗口里的选择存到 `data/file_search.json`，运行期再与配置合并：

- 文件里**出现过**的字段（`roots` / `extra_dirs` / `whole_pc`）以文件为准；
- 其它字段（`timeout` / `max_depth` / `max_results` / `allow_open` / `exclude_dirs`）仍用 `.config.yaml`；
- 文件不存在时（用户没动过设置窗口）一切照旧走配置，行为与从前完全一致。

字段用 `None` 表示"没设置过"，所以"用户主动清空所有目录"（`[]`）和"没设置过"能区分开。
"""

import json
import os
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

SERVER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SETTINGS_FILE = os.path.join(SERVER_ROOT, "data", "file_search.json")
_LOCK = threading.RLock()

# 设置窗口里可勾选的常用目录（关键字 → 中文名），顺序即界面顺序
OPTIONAL_ROOTS: List[Dict[str, str]] = [
    {"key": "desktop", "label": "桌面"},
    {"key": "documents", "label": "文档"},
    {"key": "downloads", "label": "下载"},
    {"key": "pictures", "label": "图片"},
    {"key": "music", "label": "音乐"},
    {"key": "videos", "label": "视频"},
    {"key": "home", "label": "用户目录"},
]
OPTIONAL_ROOT_KEYS = {item["key"] for item in OPTIONAL_ROOTS}

# 子文件夹层数：0 = 不进子目录（只在选中的目录第一层找），上限做个护栏
MAX_DEPTH_LIMIT = 12


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _empty() -> Dict[str, Any]:
    return {
        "version": 1,
        "updated_at": "",
        "roots": None,
        "extra_dirs": None,
        "whole_pc": None,
        # None = 没设置过（用 .config.yaml 的 max_depth）；0 = 明确表示"不查子文件夹"
        "max_depth": None,
    }


def load_settings() -> Dict[str, Any]:
    """读设置文件；文件不存在或损坏时返回"什么都没设置"。"""
    with _LOCK:
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8-sig") as handle:
                raw = json.load(handle)
        except (OSError, ValueError, TypeError):
            return _empty()

    data = _empty()
    if not isinstance(raw, dict):
        return data
    if isinstance(raw.get("roots"), list):
        data["roots"] = [str(item) for item in raw["roots"] if str(item or "").strip()]
    if isinstance(raw.get("extra_dirs"), list):
        data["extra_dirs"] = [
            str(item) for item in raw["extra_dirs"] if str(item or "").strip()
        ]
    if isinstance(raw.get("whole_pc"), bool):
        data["whole_pc"] = raw["whole_pc"]
    if isinstance(raw.get("max_depth"), int) and not isinstance(raw.get("max_depth"), bool):
        data["max_depth"] = max(0, min(MAX_DEPTH_LIMIT, int(raw["max_depth"])))
    data["updated_at"] = str(raw.get("updated_at") or "")
    return data


def _write_settings(data: Dict[str, Any]) -> None:
    payload = {
        "version": 1,
        "updated_at": data.get("updated_at") or _now(),
        "roots": data.get("roots"),
        "extra_dirs": data.get("extra_dirs"),
        "whole_pc": data.get("whole_pc"),
        "max_depth": data.get("max_depth"),
    }
    with _LOCK:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        temporary = f"{SETTINGS_FILE}.{os.getpid()}.tmp"
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, SETTINGS_FILE)


def get_effective_config(base: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把 `.config.yaml` 的 `plugins.file_search` 与用户设置合并成"当前生效配置"。"""
    config = dict(base or {})
    settings = load_settings()
    for key in ("roots", "extra_dirs", "whole_pc", "max_depth"):
        if settings.get(key) is not None:
            config[key] = settings[key]
    if settings.get("whole_pc") is True:
        # 整机模式：roots/extra_dirs 不参与，明确置空，免得下游误用
        config["roots"] = []
        config["extra_dirs"] = []
    return config


# ── 界面数据 ────────────────────────────────────────────────────────────────
def _resolve_keyword(keyword: str) -> Optional[str]:
    """关键字（desktop/documents/…）解析成真实路径；解析不到返回 None。"""
    from plugins_func.functions.file_search import _known_folder

    return _known_folder(keyword)


def candidate_roots() -> List[Dict[str, Any]]:
    """设置窗口的"常用目录"候选：每个都给解析后的真实路径与是否存在。"""
    result = []
    for item in OPTIONAL_ROOTS:
        path = _resolve_keyword(item["key"])
        result.append(
            {
                "key": item["key"],
                "label": item["label"],
                "path": path or "",
                "exists": bool(path and os.path.isdir(path)),
            }
        )
    return result


def _entries(paths: List[str]) -> List[Dict[str, Any]]:
    from plugins_func.functions.file_search import _expand_path

    items = []
    for raw in paths:
        path = _expand_path(raw)
        items.append(
            {
                "path": path,
                "label": os.path.basename(path.rstrip("\\/")) or path,
                "exists": os.path.isdir(path),
            }
        )
    return items


def describe_scope(base: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """给设置窗口/接口的完整状态：用户设置 + 生效结果。"""
    from plugins_func.functions.file_search import (
        DEFAULT_MAX_DEPTH,
        _resolve_roots,
        _scope_hint,
        _whole_pc,
    )

    config = get_effective_config(base)
    settings = load_settings()
    roots = _resolve_roots(config)
    raw_base = base or {}
    return {
        "ok": True,
        "whole_pc": _whole_pc(config),
        "roots": list(settings.get("roots") or []),
        # 用户是否在设置窗口里改过 roots：没改过时界面要按 .config.yaml 的默认值预勾选，
        # 否则会出现"界面全没勾、实际却在生效"的错觉
        "roots_configured": settings.get("roots") is not None,
        "default_roots": list(raw_base.get("roots") or []),
        # 额外目录同理：没改过设置时，界面要显示 .config.yaml 里已经挂上的目录
        "extra_dirs": list(config.get("extra_dirs") or []),
        "extra_dirs_configured": settings.get("extra_dirs") is not None,
        # 子文件夹层数：0 = 不查子文件夹；未设置过时给出配置里的默认值供界面回显
        "max_depth": int(config.get("max_depth", DEFAULT_MAX_DEPTH) or 0),
        "max_depth_configured": settings.get("max_depth") is not None,
        "default_max_depth": int(raw_base.get("max_depth", DEFAULT_MAX_DEPTH) or 0),
        "max_depth_limit": MAX_DEPTH_LIMIT,
        # 当前生效（可能是配置默认值）：给界面显示"实际查这些地方"
        "resolved": [{"label": item["label"], "path": item["path"]} for item in roots],
        "effective_hint": _scope_hint(roots, max_labels=0),
        "available_roots": candidate_roots(),
        "extra_dirs_info": _entries(list(config.get("extra_dirs") or [])),
        "settings_file": os.path.relpath(SETTINGS_FILE, SERVER_ROOT).replace("\\", "/"),
        "updated_at": settings.get("updated_at") or "",
        "configured_roots": list(config.get("roots") or []),
    }


# ── 保存 ────────────────────────────────────────────────────────────────────
def _validate_roots(values: Any) -> List[str]:
    from plugins_func.functions.file_search import _expand_path

    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError("常用目录格式不对")
    cleaned: List[str] = []
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        if text.lower() in OPTIONAL_ROOT_KEYS:
            key = text.lower()
            if key not in cleaned:
                cleaned.append(key)
            continue
        # 也允许直接在设置里写绝对路径（界面上的"额外目录"更常用，这里做个兜底）
        path = _expand_path(text)
        if not os.path.isabs(path):
            raise ValueError(f"不认识的目录：{text}")
        if not os.path.isdir(path):
            raise ValueError(f"目录不存在：{path}")
        if path not in cleaned:
            cleaned.append(path)
    return cleaned


def _validate_extra_dirs(values: Any) -> List[str]:
    from plugins_func.functions.file_search import _expand_path

    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError("额外目录格式不对")
    cleaned: List[str] = []
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        path = _expand_path(text)
        if not os.path.isabs(path):
            raise ValueError(f"请填写完整路径：{text}")
        if not os.path.isdir(path):
            raise ValueError(f"目录不存在：{path}")
        key = os.path.normcase(path)
        if all(os.path.normcase(existing) != key for existing in cleaned):
            cleaned.append(os.path.abspath(path))
    return cleaned


def _validate_max_depth(value: Any) -> int:
    """子文件夹层数：0 = 不查子目录（只在选中的目录第一层找），上限 MAX_DEPTH_LIMIT。"""
    if value is None or value == "":
        return 0
    try:
        depth = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("子文件夹层数必须是整数") from error
    if depth < 0 or depth > MAX_DEPTH_LIMIT:
        raise ValueError(f"子文件夹层数要在 0~{MAX_DEPTH_LIMIT} 之间")
    return depth


def save_settings(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """保存设置（设置窗口点"保存并生效"）。返回保存后的设置。"""
    body = payload or {}
    if not isinstance(body, dict):
        raise ValueError("请求格式不对")

    data = load_settings()
    if "roots" in body:
        data["roots"] = _validate_roots(body.get("roots"))
    if "extra_dirs" in body:
        data["extra_dirs"] = _validate_extra_dirs(body.get("extra_dirs"))
    if "whole_pc" in body:
        data["whole_pc"] = bool(body.get("whole_pc"))
    if "max_depth" in body:
        data["max_depth"] = _validate_max_depth(body.get("max_depth"))
    data["updated_at"] = _now()
    _write_settings(data)
    return data


def reset_settings() -> None:
    """恢复成"完全跟随 .config.yaml"（测试与排障用）。"""
    with _LOCK:
        try:
            os.remove(SETTINGS_FILE)
        except FileNotFoundError:
            pass
