"""Windows 自带语音合成（SAPI）：把文本合成成 wav，供演示模式与接口自测使用。

只依赖 pywin32（Anaconda 自带）。合成结果默认落在 数据目录/audio/demo_speech.wav。
这样演示模式可以喂真实语音给真实模型，而不是合成音调——
既能演示交互，又能顺带验证云端识别效果。"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

from typefast.settings import data_subdir

DEMO_TEXT = "今天下午三点开会，讨论新版本的排期和人力安排。"
DEMO_FILE = "demo_speech.wav"


def demo_speech_path() -> Path:
    return data_subdir("audio") / DEMO_FILE


def synthesize(text: str, path: Optional[Path] = None) -> Optional[Path]:
    """用系统 TTS 合成到 wav；失败返回 None（不抛异常）。"""
    target = Path(path) if path else demo_speech_path()
    try:
        import win32com.client
    except Exception:
        return None
    try:
        stream = win32com.client.Dispatch("SAPI.SpFileStream")
        stream.Open(str(target), 3, False)
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        voice.AudioOutputStream = stream
        voice.Speak(text)
        stream.Close()
        return target
    except Exception:
        return None


def load_wav(path) -> Tuple[np.ndarray, float]:
    with wave.open(str(path), "rb") as wf:
        rate = float(wf.getframerate())
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())
    if width != 2:
        raise ValueError("只支持 16bit PCM wav")
    data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, rate


def resample(x: np.ndarray, src_rate: float, dst_rate: float) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    if x.size < 2 or abs(src_rate - dst_rate) < 1.0:
        return x
    n = int(round(x.size * dst_rate / src_rate))
    src = np.linspace(0.0, x.size - 1, x.size)
    dst = np.linspace(0.0, x.size - 1, n)
    return np.interp(dst, src, x).astype(np.float32)


def load_16k(path, sample_rate: int = 16000) -> np.ndarray:
    data, rate = load_wav(path)
    return resample(data, rate, float(sample_rate))


def ensure_demo_speech(text: str = DEMO_TEXT, path=None, sample_rate: int = 16000) -> Optional[np.ndarray]:
    """确保有一段演示语音：没有就现场合成；合成或读取失败返回 None。"""
    target = Path(path) if path else demo_speech_path()
    if not target.exists():
        if synthesize(text, target) is None:
            return None
    try:
        return load_16k(target, sample_rate)
    except Exception:
        return None
