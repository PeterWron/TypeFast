"""硅基流动接口探测：用 Windows 自带 TTS 合成一段语音，再调 /v1/audio/transcriptions 看返回。

用法：python tools/sf_probe.py sk-xxxx（Key 只走命令行，不落盘）"""

from __future__ import annotations

import io
import os
import sys
import time
import wave

import numpy as np
import requests

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from typefast.platform.tts import demo_speech_path

API = "https://api.siliconflow.cn/v1/audio/transcriptions"
CHAT = "https://api.siliconflow.cn/v1/chat/completions"
MODEL = "XingChenAGI/XingChenASR-V3.2-Ultra"
DEMO_TEXT = "今天下午三点开会，讨论新版本的排期问题。"


def synth_speech(path: str, text: str) -> bool:
    try:
        import win32com.client
    except Exception as exc:
        print("SAPI 不可用：" + str(exc))
        return False
    try:
        stream = win32com.client.Dispatch("SAPI.SpFileStream")
        stream.Open(os.path.abspath(path), 3, False)
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        voice.AudioOutputStream = stream
        voice.Speak(text)
        stream.Close()
        return True
    except Exception as exc:
        print("SAPI 合成失败：" + str(exc))
        return False


def load_wav(path: str):
    with wave.open(path, "rb") as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())
    if width != 2:
        raise RuntimeError("只支持 16bit PCM wav")
    data = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, float(rate)


def resample(x, src: float, dst: float):
    if abs(src - dst) < 1.0 or len(x) < 2:
        return x
    n = int(round(len(x) * dst / src))
    return np.interp(np.linspace(0.0, len(x) - 1, n), np.linspace(0.0, len(x) - 1, len(x)), x).astype(np.float32)


def to_wav_bytes(samples, sample_rate: int = 16000) -> bytes:
    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def main() -> int:
    key = sys.argv[1] if len(sys.argv) > 1 else ""
    if not key:
        print("缺少 Key：python tools/sf_probe.py sk-xxxx")
        return 2
    demo = str(demo_speech_path())
    if not synth_speech(demo, DEMO_TEXT):
        return 1
    raw, rate = load_wav(demo)
    samples = resample(raw, rate, 16000.0)
    print("synth: %.2fs @%.0fHz -> 16k %.2fs" % (len(raw) / rate, rate, len(samples) / 16000.0))
    wav = to_wav_bytes(samples, 16000)
    print("wav bytes: %d" % len(wav))
    headers = {"Authorization": "Bearer " + key}
    started = time.monotonic()
    try:
        resp = requests.post(API, headers=headers, files={"file": ("speech.wav", wav, "audio/wav")},
                            data={"model": MODEL}, timeout=60)
    except Exception as exc:
        print("请求异常：" + repr(exc))
        return 1
    cost = (time.monotonic() - started) * 1000
    print("ASR status %s  %.0fms" % (resp.status_code, cost))
    print("ASR body: " + (resp.text or "")[:600])
    try:
        print("ASR text: " + str(resp.json().get("text")))
    except Exception:
        pass
    started = time.monotonic()
    try:
        chat = requests.post(CHAT, headers={**headers, "Content-Type": "application/json"},
                            json={"model": "Qwen/Qwen2.5-7B-Instruct",
                                  "messages": [{"role": "user", "content": "回复两个字：可以"}],
                                  "max_tokens": 16}, timeout=60)
        print("CHAT status %s  %.0fms" % (chat.status_code, (time.monotonic() - started) * 1000))
        print("CHAT body: " + (chat.text or "")[:400])
    except Exception as exc:
        print("CHAT 异常：" + repr(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
