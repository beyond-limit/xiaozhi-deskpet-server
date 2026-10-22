"""Windows 内置语音合成（SAPI）本地 TTS provider。

设计目的：给项目补一个**完全离线、零 Key、不依赖外部服务**的 TTS。
适用场景：
  1. EdgeTTS 之类的免费云 TTS 被限流/封禁时（实测会出现持续 403）作为兜底；
  2. 追求更低首字延迟——SAPI 是本机进程内合成，实测 130~400ms，
     而 EdgeTTS 每次都有约 1.2s 的"建连 + 服务端往返"固定开销（见 PROGRESS 5.1）。

依赖与限制：
  - 仅 Windows；需要 pywin32（`pip install pywin32`），用于 win32com 调 COM。
  - 只能使用系统已安装的音色。中文需要系统装有 zh-CN 语音，
    例如 "Microsoft Huihui Desktop"（Windows 中文版自带，
    英文系统可在「设置 → 时间和语言 → 语言 → 中文 → 语音」中补装）。
  - 音质不如豆包/EdgeTTS 的自然，适合做兜底而非最终方案。

配置示例（`config.yaml` 的 TTS 段）：
    SapiLocal:
      type: sapi_local
      voice: "Microsoft Huihui Desktop"   # 可写音色名片段，大小写不敏感
      sample_rate: 24000                  # 16000 / 22050 / 24000
      rate: 0                             # 语速 -10 ~ 10（SAPI 原生档位）
      volume: 100                         # 音量 0 ~ 100
      output_dir: tmp/
"""

import asyncio
import gc
import os
import uuid
from datetime import datetime
from typing import Optional

from config.logger import setup_logging
from core.providers.tts.base import TTSProviderBase

TAG = __name__
logger = setup_logging()

DEFAULT_VOICE = "Microsoft Huihui Desktop"

# SpeechAudioFormatType 取值（SAPI.SpFileStream 的 Format.Type）
# 注意：枚举里 8bit 与 16bit 是相邻项，别写错——写成 24 会得到 24kHz **8bit**，
# 基类再按 16bit 处理就会变噪。24000 对应的是 26（SAFT24kHz16BitMono）。
FORMAT_TYPES = {
    16000: 18,  # SAFT16kHz16BitMono
    22050: 22,  # SAFT22kHz16BitMono
    24000: 26,  # SAFT24kHz16BitMono
}

SSFM_CREATE_FOR_WRITE = 3

# SpeechVoiceSpeakFlags（务必按枚举写，别记错）：
#   SVSFlagsAsync = 1      ← 异步：Speak 立即返回，朗读在后台线程进行
#   SVSFIsXML     = 8      ← 把文本按 XML 解析
#   SVSFIsNotXML  = 16     ← 按纯文本朗读（我们要的就是它）
# 2026-10-16 修：这里原来写的是 1（记错成 notXML），实际是"异步"——
# Speak 返回后紧接着 stream.Close()，后台朗读还没写文件就被关掉，
# 实测每句都得到 46 字节的空 wav（只有文件头）。
# 表现为"服务端日志一路正常（语音生成成功→发送第一段语音→FIRST→LAST），
# 客户端却一个字都听不到、口型也不动"（客户端诊断：srcCount=0、analyserPeak=0、
# TTS 帧从未到达）。必须用 16（同步）并在写完后再 Close。
SVSF_IS_NOT_XML = 16

# 小于这个字节数的 wav 视为"空产物"（46 字节 = 只有 WAV 头的空文件）
MIN_WAV_BYTES = 1024


def list_installed_voices() -> list:
    """列出系统已安装的 SAPI 音色描述，供自检/排错使用。"""
    pythoncom, win32com = _load_com()

    def _read() -> list:
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        tokens = voice.GetVoices()
        return [tokens.Item(i).GetDescription() for i in range(tokens.Count)]

    return _with_com(pythoncom, _read)


def _load_com():
    """延迟导入 pywin32，未安装时给出可操作的提示。"""
    try:
        import pythoncom
        import win32com.client
    except ImportError as error:
        raise RuntimeError(
            "SAPI 本地语音合成依赖 pywin32，请在当前环境执行：pip install pywin32"
        ) from error
    return pythoncom, win32com


def _with_com(pythoncom, work):
    """在当前线程完成一次 COM 调用。

    COM 对象必须在 CoUninitialize **之前**释放，否则 pywin32 会在退出时报
    "Win32 exception occurred releasing IUnknown"，所以把调用放进独立函数，
    返回后先 gc 再反初始化。
    """
    pythoncom.CoInitialize()
    try:
        return work()
    finally:
        gc.collect()
        pythoncom.CoUninitialize()



class TTSProvider(TTSProviderBase):
    TTS_PARAM_CONFIG = [
        ("ttsVolume", "volume", 0, 100, 50, int),
        ("ttsRate", "rate", -10, 10, 0, int),
    ]

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.pythoncom, self.win32com = _load_com()

        self.voice = (
            config.get("private_voice")
            or config.get("voice")
            or DEFAULT_VOICE
        )
        self.audio_file_type = "wav"

        sample_rate = int(config.get("sample_rate", 24000))
        if sample_rate not in FORMAT_TYPES:
            raise ValueError(
                f"SAPI 不支持 sample_rate={sample_rate}，可选：{sorted(FORMAT_TYPES)}"
            )
        self.format_type = FORMAT_TYPES[sample_rate]
        self.sample_rate = sample_rate

        volume = config.get("volume", 100)
        self.volume = max(0, min(100, int(volume))) if volume not in (None, "") else 100

        rate = config.get("rate", 0)
        self.rate = max(-10, min(10, int(rate))) if rate not in (None, "") else 0

        # 支持通过 ttsVolume / ttsRate 做运行时百分比微调
        self._apply_percentage_params(config)

        logger.bind(tag=TAG).info(
            f"SAPI 本地 TTS 已就绪 | voice={self.voice} | "
            f"sample_rate={self.sample_rate} | rate={self.rate} | volume={self.volume}"
        )

    def generate_filename(self, extension=".wav"):
        return os.path.join(
            self.output_file,
            f"tts-{datetime.now().date()}@{uuid.uuid4().hex}{extension}",
        )

    async def text_to_speak(self, text, output_file):
        """合成语音。

        output_file 为空时返回 wav 二进制（基类会自行转 opus），否则写入该文件并返回 None。
        """
        return await asyncio.to_thread(self._speak_sync, text, output_file)

    # ── 内部实现 ────────────────────────────────────────────────────────────
    def _speak_sync(self, text: str, output_file: Optional[str]):
        target = output_file or self.generate_filename(".wav")
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)

        def _work():
            voice = self.win32com.client.Dispatch("SAPI.SpVoice")
            stream = self.win32com.client.Dispatch("SAPI.SpFileStream")
            stream.Format.Type = self.format_type
            stream.Open(target, SSFM_CREATE_FOR_WRITE)

            voice.AudioOutputStream = stream
            voice.Volume = self.volume
            voice.Rate = self.rate
            self._select_voice(voice)

            # 必须带 SVSFIsNotXML(=16)：SAPI 默认会把以 "<" 开头的文本当 SSML(XML) 解析，
            # 一句里混进 "<"（模型偶发的工具标记碎片等）就会失败：
            #   "SAPI 本地语音合成失败: ... XML parser error"
            # 带上这个标志后按纯文本朗读（同步），文本里出现任何符号都不会再炸掉整句。
            voice.Speak(text, SVSF_IS_NOT_XML)
            stream.Close()

        try:
            _with_com(self.pythoncom, _work)
        except Exception as error:
            raise Exception(f"SAPI 本地语音合成失败: {error}") from error

        # 空音频护栏：SAPI 偶发只写出文件头（音色丢失/引擎异常/误用异步标志等），
        # 必须当失败抛出去让基类重试，不能让"空 wav"冒充"生成成功"一路发到客户端
        # （否则客户端表现为完全无声，而日志一片正常，极难排查）。
        size = os.path.getsize(target) if os.path.exists(target) else 0
        if size < MIN_WAV_BYTES:
            raise Exception(f"SAPI 生成的音频文件为空（{size} bytes）: {target}")

        if output_file:
            return None

        # 基类要的是音频二进制；文件路径模式下基类会自己处理文件
        try:
            with open(target, "rb") as fp:
                return fp.read()
        finally:
            if os.path.exists(target):
                os.remove(target)

    def _select_voice(self, voice) -> None:
        """按音色名片段匹配系统音色；匹配不到就退回系统默认并告警。"""
        wanted = str(self.voice or "").strip().lower()
        if not wanted:
            return

        try:
            tokens = voice.GetVoices()
            for index in range(tokens.Count):
                token = tokens.Item(index)
                description = token.GetDescription()
                if wanted in description.lower():
                    voice.Voice = token
                    return
            logger.bind(tag=TAG).warning(
                f"未找到音色“{self.voice}”，将使用系统默认音色。"
                f"当前可用：{list_installed_voices()}"
            )
        except Exception as error:
            logger.bind(tag=TAG).warning(f"选择音色失败，使用系统默认音色: {error}")
