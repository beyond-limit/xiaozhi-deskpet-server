"""独立音乐播放进程（方案B：音乐不走设备音频流，从服务器本机扬声器发声）。

由 `core/utils/music_player.py` 的控制器拉起，进程与语音会话完全解耦：
桌宠退出、对话打断、ESP 断线都不会影响播放；只有收到 stop/quit 才停。

进程协议（stdin / stdout 均为 UTF-8，一行一个 JSON）：

    {"id":1,"cmd":"play","file":"...","volume":0.35,"fade":0.8,"title":"..."}
    {"id":2,"cmd":"stop"}
    {"id":3,"cmd":"pause"}
    {"id":4,"cmd":"resume"}
    {"id":5,"cmd":"volume","value":0.5}
    {"id":6,"cmd":"status"}
    {"id":7,"cmd":"ping"}
    {"id":8,"cmd":"quit"}

响应：

    {"id":1,"ok":true,"result":{...status...}}
    {"id":1,"ok":false,"error":"FileNotFoundError: ..."}

主动事件（没有 id）：

    {"event":"ended","file":"..."}
    {"event":"error","error":"..."}

依赖：pydub（ffmpeg 解码）+ sounddevice（默认输出设备）。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any, Dict, Optional

# 子进程没有父进程的启动脚本环境，自己兜底一次编码设置
for _stream in (sys.stdout, sys.stderr, sys.stdin):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

try:
    import numpy as np
    import sounddevice as sd
    from pydub import AudioSegment
except Exception as _import_error:  # pragma: no cover - 环境缺依赖时给出明确提示
    print(
        "music_player_daemon dependency error: "
        f"{type(_import_error).__name__}: {_import_error}",
        file=sys.stderr,
        flush=True,
    )
    raise

TAG = "music_player"


def _log(message: str) -> None:
    print(f"[{TAG}] {message}", file=sys.stderr, flush=True)


def _emit(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


class Player:
    """单曲播放器：整首歌解码进内存（int16），回调里做音量/淡入。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: Optional[np.ndarray] = None
        self._samplerate = 44100
        self._channels = 1
        self._pos = 0
        self._volume = 0.35
        self._fade = 0.8
        self._paused = False
        self._finished = True
        self._file = ""
        self._title = ""
        self._started_at = 0.0
        self._stream: Optional[sd.OutputStream] = None

        self._event_thread = threading.Thread(
            target=self._watch_end, name="music-player-events", daemon=True
        )
        self._event_thread.start()

    # ── 对外命令 ────────────────────────────────────────────────────────────
    @staticmethod
    def _fit_output_device(segment: "AudioSegment") -> "AudioSegment":
        """把音频调整成默认输出设备支持的采样率/声道，避免 PortAudio 报错。"""
        rate = int(segment.frame_rate)
        channels = int(segment.channels)
        if channels > 2:
            segment = segment.set_channels(2)
            channels = 2
        try:
            sd.check_output_settings(
                samplerate=rate, channels=channels, dtype="float32"
            )
            return segment
        except Exception:
            pass

        try:
            default_rate = int(
                round(float(sd.query_devices(None, "output")["default_samplerate"]))
            )
        except Exception:
            default_rate = 44100
        segment = segment.set_frame_rate(default_rate)

        for candidate_channels in (channels, 1):
            try:
                sd.check_output_settings(
                    samplerate=default_rate,
                    channels=candidate_channels,
                    dtype="float32",
                )
                if candidate_channels != channels:
                    segment = segment.set_channels(candidate_channels)
                _log(
                    f"output adjusted | {rate}Hz/{channels}ch -> "
                    f"{default_rate}Hz/{candidate_channels}ch"
                )
                return segment
            except Exception:
                continue
        raise RuntimeError("默认输出设备不支持该音频格式（采样率/声道）")

    def play(
        self,
        path: str,
        volume: float = 0.35,
        fade: float = 0.8,
        title: str = "",
    ) -> Dict[str, Any]:
        path = os.path.abspath(str(path or ""))
        if not path or not os.path.isfile(path):
            raise FileNotFoundError(path or "(empty)")

        self.stop()

        segment = AudioSegment.from_file(path)
        segment = self._fit_output_device(segment)
        samples = np.array(segment.get_array_of_samples())
        if segment.channels > 1:
            samples = samples.reshape((-1, segment.channels))
        else:
            samples = samples.reshape((-1, 1))
        data = samples.astype(np.int16, copy=False)

        volume = min(1.0, max(0.0, float(volume)))
        fade = max(0.0, float(fade))

        with self._lock:
            self._data = data
            self._channels = int(segment.channels)
            self._samplerate = int(segment.frame_rate)
            self._pos = 0
            self._volume = volume
            self._fade = fade
            self._paused = False
            self._finished = False
            self._file = path
            self._title = str(title or os.path.splitext(os.path.basename(path))[0])
            self._started_at = time.time()

        stream = sd.OutputStream(
            samplerate=self._samplerate,
            channels=self._channels,
            dtype="float32",
            callback=self._callback,
        )
        with self._lock:
            self._stream = stream
        stream.start()
        _log(
            f"play | file={self._file} | {self._samplerate}Hz "
            f"{self._channels}ch | volume={volume:.2f} fade={fade:.2f}"
        )
        return self.status()

    def stop(self) -> Dict[str, Any]:
        with self._lock:
            stream = self._stream
            self._stream = None
            self._data = None
            self._pos = 0
            self._finished = True
            self._paused = False
            file = self._file
            self._file = ""
            self._title = ""
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        if file:
            _log(f"stop | file={file}")
        return self.status()

    def pause(self) -> Dict[str, Any]:
        with self._lock:
            if self._data is not None and not self._finished:
                self._paused = True
        return self.status()

    def resume(self) -> Dict[str, Any]:
        with self._lock:
            self._paused = False
        return self.status()

    def set_volume(self, value: float) -> Dict[str, Any]:
        with self._lock:
            self._volume = min(1.0, max(0.0, float(value)))
            volume = self._volume
        _log(f"volume | {volume:.2f}")
        return self.status()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            duration = (
                len(self._data) / float(self._samplerate)
                if self._data is not None and self._samplerate
                else 0.0
            )
            return {
                "playing": bool(
                    self._data is not None and not self._finished and not self._paused
                ),
                "paused": self._paused,
                "finished": self._finished,
                "file": self._file,
                "title": self._title,
                "position": round(self._pos / float(self._samplerate or 1), 3),
                "duration": round(duration, 3),
                "volume": round(self._volume, 3),
            }

    # ── 音频回调 ────────────────────────────────────────────────────────────
    def _callback(self, outdata, frames, _time_info, _status) -> None:
        with self._lock:
            data = self._data
            if data is None or self._finished or self._paused:
                outdata.fill(0)
                return

            pos = self._pos
            end = min(pos + frames, len(data))
            chunk = data[pos:end].astype(np.float32) / 32768.0
            if len(chunk) < frames:
                outdata[: len(chunk)] = chunk
                outdata[len(chunk):] = 0
            else:
                outdata[:] = chunk

            gain = self._volume
            if self._fade > 0 and self._samplerate:
                fade_frames = max(1, int(self._fade * self._samplerate))
                if pos < fade_frames:
                    gain *= pos / float(fade_frames)

            outdata *= gain
            self._pos = end
            if self._pos >= len(data):
                self._finished = True

    def _watch_end(self) -> None:
        """播放自然结束时停掉流并主动通知控制器。"""
        was_playing = False
        while True:
            time.sleep(0.2)
            with self._lock:
                playing = self._data is not None and not self._finished
                finished = self._finished and self._data is not None
                file = self._file
            if playing:
                was_playing = True
                continue
            if was_playing and finished:
                was_playing = False
                self.stop()
                _emit({"event": "ended", "file": file})


def _handle(player: Player, request: Dict[str, Any]) -> Any:
    cmd = str(request.get("cmd") or "").strip().lower()
    if cmd == "play":
        return player.play(
            request.get("file", ""),
            request.get("volume", 0.35),
            request.get("fade", 0.8),
            request.get("title", ""),
        )
    if cmd == "stop":
        return player.stop()
    if cmd == "pause":
        return player.pause()
    if cmd == "resume":
        return player.resume()
    if cmd == "volume":
        return player.set_volume(request.get("value", 0.35))
    if cmd == "status":
        return player.status()
    if cmd == "ping":
        return {"pong": True, **player.status()}
    if cmd == "quit":
        player.stop()
        raise SystemExit(0)
    raise ValueError(f"unknown cmd: {cmd!r}")


def main() -> None:
    player = Player()
    _emit({"event": "ready", "pid": os.getpid()})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as error:
            _emit({"ok": False, "error": f"bad json: {error}"})
            continue

        request_id = request.get("id")
        try:
            result = _handle(player, request)
        except SystemExit:
            raise
        except Exception as error:  # 单条命令失败不能带崩整个进程
            _log(f"cmd failed | {request.get('cmd')!r} | {type(error).__name__}: {error}")
            _emit(
                {
                    "id": request_id,
                    "ok": False,
                    "error": f"{type(error).__name__}: {error}",
                }
            )
            continue

        _emit({"id": request_id, "ok": True, "result": result})


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
