# -*- coding: utf-8 -*-
"""服务端冒烟测试：不用麦克风、不用客户端，直接把一句话"投"进语音链路。

流程：连 ws://127.0.0.1:8000/xiaozhi/v1/ → hello → listen(detect, text)
      → 服务端跑 LLM + TTS → 统计回推的 opus 音频帧。

用法（在 main/xiaozhi-server 目录下，服务端已启动）：
    python tools\\smoke_ws.py
    python tools\\smoke_ws.py "帮我找一下桌面上的文件"
    $env:XIAOZHI_WS_URL="ws://192.168.1.10:8000/xiaozhi/v1/"; python tools\\smoke_ws.py

判据：识别/回复文本拿到 + 至少 1 帧音频 → 退出码 0。
注意：这一轮会真实调用 LLM 与 TTS（火山豆包按量计费，一句话几分钱）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

try:  # Windows 控制台默认 GBK，中文与 emoji 会抛 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import websockets

WS_URL = os.environ.get("XIAOZHI_WS_URL", "ws://127.0.0.1:8000/xiaozhi/v1/")
DEVICE_ID = os.environ.get("XIAOZHI_DEVICE_ID", "smoke-test-device")
CLIENT_ID = os.environ.get("XIAOZHI_CLIENT_ID", "smoke-test-client")
TEXT = sys.argv[1] if len(sys.argv) > 1 else "你好，请用一句话介绍你自己"
FRAME_MS = 60          # 服务端按 60ms/帧推送 opus
RECV_TIMEOUT = 30.0    # 单条消息最长等待
TOTAL_TIMEOUT = 90.0   # 整轮上限


async def run() -> int:
    t0 = time.time()
    print(f"[1/3] 连接 {WS_URL}")
    async with websockets.connect(
        WS_URL,
        additional_headers={"device-id": DEVICE_ID, "client-id": CLIENT_ID},
        open_timeout=10,
        max_size=None,
    ) as ws:
        await ws.send(json.dumps({
            "type": "hello",
            "version": 1,
            "transport": "websocket",
            "audio_params": {
                "format": "opus",
                "sample_rate": 24000,
                "channels": 1,
                "frame_duration": FRAME_MS,
            },
        }))
        hello = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)
        print(f"      服务端 hello: {str(hello)[:160]}")

        print(f"[2/3] 投递文本：{TEXT}")
        await ws.send(json.dumps({
            "type": "listen",
            "state": "detect",
            "text": TEXT,
            "source": "text",
        }))

        frames = 0
        stt_text = llm_text = ""
        tts_stop = False
        while time.time() - t0 < TOTAL_TIMEOUT:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)
            except asyncio.TimeoutError:
                print("      (等待超时，结束接收)")
                break

            if isinstance(msg, (bytes, bytearray)):
                frames += 1
                continue

            try:
                data = json.loads(msg)
            except Exception:
                print(f"      非 JSON 消息: {str(msg)[:120]}")
                continue

            kind = data.get("type")
            if kind == "stt":
                stt_text = data.get("text") or ""
                print(f"      [ASR] {stt_text}")
            elif kind == "llm":
                llm_text = data.get("text") or ""
                print(f"      [LLM] {llm_text[:160]}")
            elif kind == "tts":
                state = data.get("state")
                if state == "sentence_start":
                    print(f"      [TTS] {str(data.get('text'))[:160]}")
                elif state == "stop":
                    tts_stop = True
                    if frames:
                        break
            elif kind in ("tts", "iot"):
                print(f"      [{kind}] {str(data)[:160]}")

        print("[3/3] 结果")
        print(f"      音频帧 : {frames} 帧（约 {frames * FRAME_MS / 1000:.1f} s）")
        print(f"      TTS 结束: {'是' if tts_stop else '否'}")

        ok = frames > 0
        print("\n==== 冒烟结论: " + ("通过（收到服务端音频）" if ok else "未通过（没有音频）") + " ====")
        return 0 if ok else 1


def main() -> int:
    try:
        return asyncio.run(run())
    except Exception as exc:  # 连接被拒、hello 超时等
        print(f"\n[FAIL] {type(exc).__name__}: {exc}")
        print("       先确认服务端已启动（python app.py），且 8000 没有被占用。")
        return 2


if __name__ == "__main__":
    sys.exit(main())
