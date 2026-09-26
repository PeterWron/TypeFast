"""延时拆解 v2：分离 首字节(TTFB) 与 回程(读 body)，并拟合「固定开销 + 每秒音频」模型。"""

from __future__ import annotations

import json
import os
import socket
import ssl
import statistics
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import requests

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from typefast.platform.audiofile import decode_to_16k_mono, ffmpeg_exe
from typefast.providers.openai_compat import pcm16_wav_bytes
from typefast.settings import get_secret

HOST = "api.siliconflow.cn"
ASR_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
CHAT_URL = "https://api.siliconflow.cn/v1/chat/completions"
ASR_MODEL = "XingChenAGI/XingChenASR-V3.2-Ultra"
POLISH_CANDIDATES = ["Qwen/Qwen2.5-7B-Instruct", "Qwen/Qwen3-8B", "THUDM/glm-4-9b-chat", "Qwen/Qwen2.5-3B-Instruct"]
POLISH_TEXT = "然后那个我觉得这个方案吧，其实整体还行，就是细节上还得再打磨一下，尤其是延迟这块，我们得想办法把松手到出字压到一秒以内"


def ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000.0


def net_probe(rounds: int = 3):
    dns, tcp, tls = [], [], []
    for _ in range(rounds):
        t0 = time.perf_counter()
        info = socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)
        dns.append(ms(t0))
        ip = info[0][4][0]
        t0 = time.perf_counter()
        sock = socket.create_connection((ip, 443), timeout=10)
        tcp.append(ms(t0))
        ctx = ssl.create_default_context()
        t0 = time.perf_counter()
        ssock = ctx.wrap_socket(sock, server_hostname=HOST)
        tls.append(ms(t0))
        ssock.close()
    return dns, tcp, tls


def encode_mp3(samples, rate: int = 16000, bitrate: str = "32k") -> bytes:
    exe = ffmpeg_exe()
    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
    cmd = [exe, "-v", "error", "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "-",
           "-c:a", "libmp3lame", "-b:a", bitrate, "-f", "mp3", "-"]
    proc = subprocess.run(cmd, input=pcm, capture_output=True, timeout=180)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError("mp3 编码失败")
    return proc.stdout


def asr_stream(session, payload: bytes, name: str, mime: str):
    key = get_secret("siliconflow_api_key")
    t0 = time.perf_counter()
    resp = session.post(ASR_URL, headers={"Authorization": "Bearer " + key},
                        files={"file": (name, payload, mime)}, data={"model": ASR_MODEL},
                        timeout=180, stream=True)
    ttfb = ms(t0)
    t1 = time.perf_counter()
    body = resp.content
    back = ms(t1)
    text = ""
    try:
        text = str(json.loads(body.decode("utf-8", "ignore")).get("text") or "")
    except Exception:
        text = body.decode("utf-8", "ignore")[:80]
    return resp.status_code, ttfb, back, ms(t0), len(payload), len(text)


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "E:/py/typefast/voice_test/testvoice.mp3"
    samples = decode_to_16k_mono(path, 16000)
    seconds = samples.size / 16000.0
    wav = pcm16_wav_bytes(samples, 16000)
    try:
        mp3 = encode_mp3(samples)
    except Exception:
        mp3 = b''
    print("音频 %.2fs / WAV %dKB / MP3 %dKB" % (seconds, len(wav) // 1024, len(mp3) // 1024))
    dns, tcp, tls = net_probe()
    print("DNS %.0f  TCP %.0f  TLS %.0f  -> 新建连接 %.0f ms" % (statistics.median(dns), statistics.median(tcp), statistics.median(tls),
                                                          statistics.median(dns) + statistics.median(tcp) + statistics.median(tls)))
    print()
    print("%-24s %7s %8s %8s %8s %6s" % ("场景", "KB", "首字节ms", "回程ms", "合计ms", "状态"))
    session = requests.Session()
    cases = [(wav, "speech.wav", "audio/wav", "冷连接 WAV 100%"),
             (wav, "speech.wav", "audio/wav", "热连接 WAV 100%"),
             (wav[: len(wav) // 2], "speech.wav", "audio/wav", "热连接 WAV 50%"),
             (wav[: len(wav) // 4], "speech.wav", "audio/wav", "热连接 WAV 25%")]
    if mp3:
        cases.append((mp3, "speech.mp3", "audio/mpeg", "热连接 MP3 100%"))
    points = []
    for i, (payload, name, mime, label) in enumerate(cases):
        sess = requests.Session() if i == 0 else session
        try:
            code, ttfb, back, total, size, chars = asr_stream(sess, payload, name, mime)
        except Exception as exc:
            print("%-24s 失败 %s" % (label, repr(exc)))
            continue
        print("%-24s %7d %8.0f %8.0f %8.0f %6d" % (label, size // 1024, ttfb, back, total, code))
        points.append((size, chars, ttfb, total, label))
    print()
    print("识别字符数：" + "、".join("%s:%d" % (p[4], p[1]) for p in points))
    dur = [(p[0] / (len(wav) or 1) * seconds, p[3]) for p in points]
    dur = sorted(d for d in dur if d[0] > 0.5)
    if len(dur) >= 2:
        (d1, t1), (d2, t2) = dur[0], dur[-1]
        k = (t2 - t1) / (d2 - d1) if d2 != d1 else 0.0
        fixed = t1 - k * d1
        print("拟合：合计 ≈ %.0f ms 固定开销 + %.0f ms × 音频秒数（%.1fs=%.0fms, %.1fs=%.0fms）"
              % (fixed, k, d1, t1, d2, t2))
    print()
    print("%-28s %8s %8s %6s  %s" % ("润色模型", "首字节ms", "合计ms", "状态", "备注"))
    for model in POLISH_CANDIDATES:
        sess = requests.Session()
        for attempt in (1, 2):
            payload = {"model": model,
                       "messages": [{"role": "system", "content": "把口述整理成书面文字，去掉口头禅，补标点，只输出正文。"},
                                    {"role": "user", "content": POLISH_TEXT}],
                       "temperature": 0.1, "max_tokens": 400}
            t0 = time.perf_counter()
            try:
                resp = sess.post(CHAT_URL, headers={"Authorization": "Bearer " + (get_secret("polish_api_key") or ""),
                                              "Content-Type": "application/json"},
                                  json=payload, timeout=180, stream=True)
                ttfb = ms(t0)
                body = resp.content
                note = ""
                if resp.status_code >= 400:
                    note = body.decode("utf-8", "ignore")[:110]
                else:
                    try:
                        note = str(json.loads(body.decode("utf-8", "ignore"))["choices"][0]["message"]["content"])[:60]
                    except Exception:
                        note = "(解析失败)"
                print("%-28s %8.0f %8.0f %6d  %s" % (model + (" 冷" if attempt == 1 else " 热"), ttfb, ms(t0), resp.status_code, note))
            except Exception as exc:
                print("%-28s 失败 %s" % (model, repr(exc)))
                break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
