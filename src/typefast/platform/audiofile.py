"""音频文件解码：任意格式（mp3 / m4a / wav / flac / ogg）→ 16k 单声道 float32。

优先用系统 ffmpeg，找不到就用 imageio-ffmpeg 自带的二进制；wav 走标准库快路径。"""

from __future__ import annotations

import subprocess
import wave
from pathlib import Path
from shutil import which
from typing import Optional

import numpy as np

TARGET_RATE = 16000


def ffmpeg_exe() -> Optional[str]:
    exe = which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def probe_duration(path) -> float:
    exe = ffmpeg_exe()
    if not exe:
        return 0.0
    name = "ffprobe.exe" if exe.lower().endswith(".exe") else "ffprobe"
    probe = str(Path(exe).with_name(name))
    try:
        out = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", str(path)],
                             capture_output=True, text=True, timeout=30)
        return float((out.stdout or "0").strip() or 0.0)
    except Exception:
        return 0.0


def decode_to_16k_mono(path, target_rate: int = TARGET_RATE) -> np.ndarray:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    if p.suffix.lower() == ".wav":
        try:
            return _read_wav(p, target_rate)
        except Exception:
            pass
    exe = ffmpeg_exe()
    if not exe:
        raise RuntimeError("找不到 ffmpeg：请把 ffmpeg 加入 PATH，或安装 imageio-ffmpeg")
    cmd = [exe, "-v", "error", "-i", str(p), "-vn", "-f", "s16le", "-acodec", "pcm_s16le",
           "-ac", "1", "-ar", str(target_rate), "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=600)
    if proc.returncode != 0 or not proc.stdout:
        detail = (proc.stderr or b'').decode("utf-8", "ignore")[:300]
        raise RuntimeError("ffmpeg 解码失败：" + detail)
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def _read_wav(p, target_rate: int) -> np.ndarray:
    with wave.open(str(p), "rb") as wf:
        rate = float(wf.getframerate())
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())
    if width != 2:
        raise ValueError("不是 16bit PCM")
    data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if abs(rate - target_rate) > 1:
        from typefast.platform.tts import resample
        data = resample(data, rate, float(target_rate))
    return data
