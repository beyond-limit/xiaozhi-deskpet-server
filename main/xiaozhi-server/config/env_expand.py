"""配置里的 ${VAR} 环境变量展开 + 极简 .env 加载。

为什么需要：交付规范要求"不硬编码 API Key"，而 xiaozhi 的配置体系只读 yaml，
没有提供环境变量注入。这里在 read_config 之后补一层展开，让 data/.config.yaml 里
写成 ${DEEPSEEK_API_KEY} 即可，真实值放在仓库根目录的 .env（已进 .gitignore）。

支持写法：
    ${VAR}              # 必填，缺失时原样保留并打一条 WARNING（不会静默变成空串）
    ${VAR:-default}     # 缺失时用 default

只处理字符串值；数字/布尔/结构不动。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

_warned: set[str] = set()
_loaded_env_files: set[str] = set()


def load_dotenv(path: str) -> bool:
    """把 .env 读进 os.environ（不覆盖已存在的进程变量）。返回是否读到了文件。"""
    path = os.path.abspath(path)
    if path in _loaded_env_files or not os.path.isfile(path):
        return False
    _loaded_env_files.add(path)
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:]
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = value
        return True
    except OSError:
        return False


def load_env_near(project_dir: str) -> None:
    """在多个候选位置找 .env：项目目录 → 上溯到仓库根目录（最多 5 层）。"""
    cur = os.path.abspath(project_dir)
    for _ in range(6):
        if load_dotenv(os.path.join(cur, ".env")):
            return
        parent = os.path.dirname(cur)
        if parent == cur:
            return
        cur = parent


def _expand_str(text: str) -> str:
    def repl(m: re.Match) -> str:
        name, default = m.group(1), m.group(2)
        val = os.environ.get(name)
        if val:
            return val
        if default is not None:
            return default
        if name not in _warned:
            _warned.add(name)
            print(f"[WARN] 配置里的 ${{{name}}} 没有对应的环境变量，请在 .env 里补上（模板见 .env.example）")
        return m.group(0)

    return _PATTERN.sub(repl, text)


def expand_env(obj):
    """递归展开 dict / list / str 里的 ${VAR}。"""
    if isinstance(obj, str):
        return _expand_str(obj) if "${" in obj else obj
    if isinstance(obj, Mapping):
        return {k: expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env(v) for v in obj]
    return obj
