"""延时拆解探针：把一次语音转写拆成 本地编码 / DNS / TCP / TLS / 上传 / 服务端 / 下载 / 润色。

用法：python tools/latency_probe.py [音频文件]
Key 从 Windows 凭据管理器读（siliconflow_api_key），不走命令行。"""

from __future__ import annotations

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
from typefast.settings import data_dir, get_secret

HOST = "api.siliconflow.cn"
ASR_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
CHAT_URL = "https://api.siliconflow.cn/v1/chat/completions"
ASR_MODEL = "XingChenAGI/XingChenASR-V3.2-Ultra"
POLISH_MODELS = ["Qwen/Qwen2.5-7B-Instruct", "Qwen/Qwen2.5-3B-Instruct"]
POLISH_TEXT = "然后那个我觉得这个方案吧，其实整体还行，就是细节上还得再打磨一下，尤其是延迟这块"


def ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000.0


def stats(values) -> str:
    if not values:
        return "-"
    return "中位 %.0f / 最小 %.0f / 最大 %.0f ms" % (statistics.median(values), min(values), max(values))


def net_probe(rounds: int = 3):
    dns, tcp, tls = [], [], []
    alpn = ""
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
        alpn = ssock.selected_alpn_protocol() or ""
        ssock.close()
    return dns, tcp, tls, alpn


def encode_mp3(samples, rate: int = 16000, bitrate: str = "32k") -> bytes:
    exe = ffmpeg_exe()
    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
    cmd = [exe, "-v", "error", "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "-",
           "-c:a", "libmp3lame", "-b:a", bitrate, "-f", "mp3", "-"]
    proc = subprocess.run(cmd, input=pcm, capture_output=True, timeout=180)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError("mp3 编码失败")
    return proc.stdout


def asr_call(session, payload: bytes, name: str, mime: str):
    key = get_secret("siliconflow_api_key")
    if not key:
        raise RuntimeError("没有配置 siliconflow_api_key")
    t0 = time.perf_counter()
    resp = session.post(ASR_URL, headers={"Authorization": "Bearer " + key},
                        files={"file": (name, payload, mime)},
                        data={"model": ASR_MODEL}, timeout=120)
    ttfb = ms(t0)
    t1 = time.perf_counter()
    body = resp.content
    download = ms(t1)
    return resp, ttfb, download, ms(t0)


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else str(Path("E:/py/typefast/voice_test/testvoice.mp3"))
    print("=== 目标 ===")
    print("host     : %s" % HOST)
    print("asr      : %s" % ASR_MODEL)
    print("file     : %s" % path)

    print()
    print("=== 1. 网络基础（3 次） ===")
    dns, tcp, tls, alpn = net_probe()
    print("DNS 解析   : " + stats(dns))
    print("TCP 握手   : " + stats(tcp))
    print("TLS 握手   : " + stats(tls) + "   ALPN=" + alpn)
    base = statistics.median(dns) + statistics.median(tcp) + statistics.median(tls)
    print("新建连接小计: %.0f ms" % base)

    print()
    print("=== 2. 本地处理 ===")
    t0 = time.perf_counter()
    samples = decode_to_16k_mono(path, 16000)
    decode_ms = ms(t0)
    seconds = samples.size / 16000.0
    t0 = time.perf_counter()
    wav = pcm16_wav_bytes(samples, 16000)
    wav_ms = ms(t0)
    t0 = time.perf_counter()
    mp3 = encode_mp3(samples)
    mp3_ms = ms(t0)
    print("ffmpeg 解码: %.0f ms（%.2fs 音频）" % (decode_ms, seconds))
    print("WAV 编码   : %.1f ms  %d KB" % (wav_ms, len(wav) // 1024))
    print("MP3 编码   : %.0f ms  %d KB" % (mp3_ms, len(mp3) // 1024)) if False else print("MP3 编码   : %.0f ms  %d KB" % (mp3_ms, len(mp3) // 1024))

    print()
    print("=== 3. ASR 调用拆解 ===")
    rows = []
    fresh = requests.Session()
    resp, ttfb, dl, total = asr_call(fresh, wav, "speech.wav", "audio/wav")
    rows.append(("冷连接 WAV（新建 session）", len(wav), ttfb, dl, total, resp.status_code, len(resp.text)))
    resp, ttfb, dl, total = asr_call(fresh, wav, "speech.wav", "audio/wav")
    rows.append(("复用连接 WAV（第 2 次）", len(wav), ttfb, dl, total, resp.status_code, len(resp.text)))
    resp, ttfb, dl, total = asr_call(fresh, wav, "speech.wav", "audio/wav")
    rows.append(("复用连接 WAV（第 3 次）", len(wav), ttfb, dl, total, resp.status_code, len(resp.text)))
    resp, ttfb, dl, total = asr_call(fresh, mp3, "speech.mp3", "audio/mpeg")
    rows.append(("复用连接 MP3（同样音频）", len(mp3), ttfb, dl, total, resp.status_code, len(resp.text)))
    head = wav[:max(1, int(len(wav) * 0.3))]
    resp, ttfb, dl, total = asr_call(fresh, head, "speech.wav", "audio/wav")
    rows.append(("复用连接 WAV（30% 长度）", len(head), ttfb, dl, total, resp.status_code, len(resp.text)))
    print("%-26s %8s %9s %9s %9s %6s" % ("场景", "载荷KB", "TTFB ms", "下载ms", "合计ms", "状态"))
    for name, size, ttfb, dl, total, code, chars in rows:
        print("%-26s %8d %9.0f %9.0f %9.0f %6d" % (name, size // 1024, ttfb, dl, total, code))
    print()
    print("识别文本长度（字符）：" + " / ".join(str(r[6]) for r in rows))
    print()
    print("=== 4. 润色调用拆解 ===")
    print("%-28s %9s %9s" % ("模型", "TTFB ms", "合计ms"))
    for model in POLISH_MODELS:
        sess = requests.Session()
        for attempt in (1, 2):
            payload = {"model": model,
                       "messages": [{"role": "system", "content": "把口述整理成书面文字，去掉口头禅，补标点。"},
                                    {"role": "user", "content": POLISH_TEXT}],
                       "temperature": 0.1, "max_tokens": 500}
            t0 = time.perf_counter()
            try:
                resp = sess.post(CHAT_URL, headers={"Authorization": "Bearer " + get_secret("polish_api_key"),
                                              "Content-Type": "application/json"},
                                  json=payload, timeout=120)
                ttfb = ms(t0)
                body = resp.content
                print("%-28s %9.0f %9.0f   (%d)%s" % (model + (" 冷" if attempt == 1 else " 热"), ttfb, ms(t0), resp.status_code, " " + str(len(body)) + "B"))
            except Exception as exc:
                print(model + " 失败：" + repr(exc))
    print()
    print("=== 5. 本地汇总 ===")
    print("mp3 解码 + wav 编码 = %.0f ms（与网络无关，可优化）" % (decode_ms + wav_ms))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
