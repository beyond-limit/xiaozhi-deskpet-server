"""服务端音乐播放控制器（与 `music_player_daemon.py` 子进程通信）。

设计要点：
- **常驻子进程**：避免每首歌都付一次 Python 启动 + 解码的冷启动开销；
- **进程与会话解耦**：播放状态只属于服务端，桌宠/ESP 断开不影响；
- **同步 API**：插件里用 `asyncio.to_thread(...)` 调用，避免阻塞事件循环；
- **失败可恢复**：子进程退出后下一次调用自动重启，并在错误里带上 stderr 末尾几行。

对外入口：`get_music_player()` 返回进程级单例。
"""

from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from typing import Any, Dict, Optional

from config.config_loader import get_project_dir
from config.logger import setup_logging

TAG = __name__
logger = setup_logging()

DAEMON_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "music_player_daemon.py")
START_TIMEOUT = 10.0
DEFAULT_TIMEOUT = 8.0
PLAY_TIMEOUT = 60.0  # 在线歌曲可能是 webm/m4a，解码稍慢


class MusicPlayerError(RuntimeError):
    """播放器进程不可用或命令执行失败。"""


class MusicPlayerController:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: Optional[subprocess.Popen] = None
        self._pending: Dict[int, Future] = {}
        self._next_id = 0
        self._ready = threading.Event()
        self._stderr_tail: list[str] = []
        self._closed = False

    # ── 进程管理 ────────────────────────────────────────────────────────────
    def _spawn_locked(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return

        self._ready.clear()
        self._stderr_tail = []
        self._pending.clear()

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

        env = os.environ.copy()
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        # 与 start_server.ps1 一致：保证子进程能找到 ffmpeg / opus.dll
        env_dir = os.path.dirname(sys.executable)
        for sub in ("Library\\bin", "Scripts"):
            candidate = os.path.join(env_dir, sub)
            if os.path.isdir(candidate):
                env["PATH"] = candidate + os.pathsep + env.get("PATH", "")

        try:
            self._process = subprocess.Popen(
                [sys.executable, "-u", DAEMON_FILE],
                cwd=get_project_dir(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
                env=env,
            )
        except Exception as error:
            self._process = None
            raise MusicPlayerError(
                f"播放器进程启动失败：{type(error).__name__}: {error}"
            ) from error

        threading.Thread(
            target=self._read_stdout, name="music-player-stdout", daemon=True
        ).start()
        threading.Thread(
            target=self._read_stderr, name="music-player-stderr", daemon=True
        ).start()

    def _ensure_process(self) -> None:
        with self._lock:
            if self._closed:
                raise MusicPlayerError("播放器已经关闭（服务端正在退出）")
            if self._process is not None and self._process.poll() is None:
                return
            self._spawn_locked()
            ready = self._ready
            process = self._process

        if not ready.wait(START_TIMEOUT):
            tail = "\n".join(self._stderr_tail[-8:])
            with self._lock:
                self._kill_locked()
            raise MusicPlayerError(
                "播放器进程未就绪（超时）。"
                + ("\n" + tail if tail else "")
                + "\n请确认已安装 sounddevice 与 ffmpeg。"
            )
        if process is None or process.poll() is not None:
            tail = "\n".join(self._stderr_tail[-8:])
            with self._lock:
                self._kill_locked()
            raise MusicPlayerError(
                "播放器进程启动后立即退出。"
                + ("\n" + tail if tail else "")
                + "\n请确认已安装 sounddevice 与 ffmpeg。"
            )

    def _kill_locked(self) -> None:
        process = self._process
        self._process = None
        self._ready.clear()
        if process is None:
            return
        try:
            process.kill()
        except Exception:
            pass
        try:
            process.wait(timeout=2)
        except Exception:
            pass

    # ── 管道读取 ────────────────────────────────────────────────────────────
    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                logger.bind(tag=TAG).warning(f"播放器输出无法解析: {line[:200]}")
                continue

            if payload.get("event") == "ready":
                self._ready.set()
                logger.bind(tag=TAG).info(
                    f"播放器就绪 | pid={payload.get('pid')} | daemon={DAEMON_FILE}"
                )
                continue
            if payload.get("event"):
                logger.bind(tag=TAG).info(
                    f"播放器事件 | {payload.get('event')} | {payload.get('file') or ''}"
                )
                continue

            request_id = payload.get("id")
            if request_id is None:
                continue
            with self._lock:
                future = self._pending.pop(int(request_id), None)
            if future is None or future.done():
                continue
            if payload.get("ok"):
                future.set_result(payload.get("result"))
            else:
                future.set_exception(
                    MusicPlayerError(str(payload.get("error") or "播放器命令失败"))
                )

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            line = line.rstrip()
            if not line:
                continue
            self._stderr_tail.append(line)
            if len(self._stderr_tail) > 40:
                self._stderr_tail = self._stderr_tail[-40:]
            logger.bind(tag=TAG).info(f"daemon | {line}")

    # ── 命令通道 ────────────────────────────────────────────────────────────
    def _request(self, cmd: str, timeout: float, **payload: Any) -> Any:
        self._ensure_process()

        with self._lock:
            process = self._process
            if process is None or process.stdin is None:
                raise MusicPlayerError("播放器进程不可用")
            self._next_id += 1
            request_id = self._next_id
            future: Future = Future()
            self._pending[request_id] = future
            message = {"id": request_id, "cmd": cmd}
            message.update(payload)
            try:
                process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                process.stdin.flush()
            except Exception as error:
                self._pending.pop(request_id, None)
                self._kill_locked()
                raise MusicPlayerError(
                    f"向播放器发送命令失败：{type(error).__name__}: {error}"
                ) from error

        try:
            return future.result(timeout=timeout)
        except Exception as error:
            with self._lock:
                self._pending.pop(request_id, None)
            if isinstance(error, MusicPlayerError):
                raise
            if isinstance(error, (TimeoutError, FutureTimeoutError)):
                with self._lock:
                    self._kill_locked()
                raise MusicPlayerError(f"播放器命令超时：{cmd}") from error
            raise MusicPlayerError(
                f"播放器命令失败：{type(error).__name__}: {error}"
            ) from error

    # ── 对外 API ────────────────────────────────────────────────────────────
    def play(
        self,
        path: str,
        volume: float = 0.35,
        fade: float = 0.8,
        title: str = "",
    ) -> Dict[str, Any]:
        return self._request(
            "play",
            PLAY_TIMEOUT,
            file=path,
            volume=volume,
            fade=fade,
            title=title,
        )

    def stop(self) -> Dict[str, Any]:
        return self._request("stop", DEFAULT_TIMEOUT)

    def pause(self) -> Dict[str, Any]:
        return self._request("pause", DEFAULT_TIMEOUT)

    def resume(self) -> Dict[str, Any]:
        return self._request("resume", DEFAULT_TIMEOUT)

    def set_volume(self, value: float) -> Dict[str, Any]:
        return self._request("volume", DEFAULT_TIMEOUT, value=value)

    def status(self) -> Dict[str, Any]:
        return self._request("status", DEFAULT_TIMEOUT)

    def is_alive(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            process = self._process
            self._process = None
            self._ready.clear()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(MusicPlayerError("播放器已关闭"))
            self._pending.clear()

        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.write('{"id":0,"cmd":"quit"}\n')
                process.stdin.flush()
            process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


_PLAYER: Optional[MusicPlayerController] = None
_PLAYER_LOCK = threading.Lock()


def get_music_player() -> MusicPlayerController:
    """进程级单例（第一次调用时创建，不启动子进程）。"""
    global _PLAYER
    with _PLAYER_LOCK:
        if _PLAYER is None:
            _PLAYER = MusicPlayerController()
            atexit.register(_PLAYER.shutdown)
        return _PLAYER
