"""去程上传探针：手工构造 multipart 请求，把「发送耗时」与「服务端耗时」分开。

sendall 返回 ≈ 数据已被内核接受（受 TCP 窗口与链路限制），可作为上传耗时近似。"""

from __future__ import annotations

import os
import socket
import ssl
import subprocess
import sys
import time

import numpy as np

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from typefast.platform.audiofile import decode_to_16k_mono, ffmpeg_exe
from typefast.providers.openai_compat import pcm16_wav_bytes
from typefast.settings import get_secret

HOST = "api.siliconflow.cn"
PATH = "/v1/audio/transcriptions"
MODEL = "XingChenAGI/XingChenASR-V3.2-Ultra"
CRLF = chr(13) + chr(10)
Q = chr(34)
SP = bytes([32])


def ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000.0


def encode_mp3(samples, rate: int = 16000, bitrate: str = "32k") -> bytes:
    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
    cmd = [ffmpeg_exe(), "-v", "error", "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "-",
           "-c:a", "libmp3lame", "-b:a", bitrate, "-f", "mp3", "-"]
    return subprocess.run(cmd, input=pcm, capture_output=True, timeout=180).stdout


def build_request(payload: bytes, filename: str, mime: str, key: str) -> bytes:
    boundary = "----typefastboundary7f3a"
    parts = [
        "--" + boundary,
        "Content-Disposition: form-data; name=" + Q + "model" + Q,
        "",
        MODEL,
        "--" + boundary,
        "Content-Disposition: form-data; name=" + Q + "file" + Q + "; filename=" + Q + filename + Q,
        "Content-Type: " + mime,
        "",
    ]
    head = (CRLF.join(parts) + CRLF).encode("utf-8")
    tail = (CRLF + "--" + boundary + "--" + CRLF).encode("utf-8")
    body = head + payload + tail
    headers = ("POST " + PATH + " HTTP/1.1" + CRLF
               + "Host: " + HOST + CRLF
               + "Authorization: Bearer " + key + CRLF
               + "Content-Type: multipart/form-data; boundary=" + boundary + CRLF
               + "Content-Length: " + str(len(body)) + CRLF
               + "Connection: close" + CRLF + CRLF).encode("utf-8")
    return headers + body


def probe(label: str, payload: bytes, filename: str, mime: str, key: str):
    request = build_request(payload, filename, mime, key)
    ctx = ssl.create_default_context()
    raw = socket.create_connection((HOST, 443), timeout=30)
    sock = ctx.wrap_socket(raw, server_hostname=HOST)
    t0 = time.perf_counter()
    sock.sendall(request)
    upload = ms(t0)
    t1 = time.perf_counter()
    data = bytes()
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    server = ms(t1)
    sock.close()
    try:
        status = data.split(SP)[1][:3].decode("ascii", "ignore")
    except Exception:
        status = "?"
    kb = len(payload) / 1024.0
    rate = (kb / (upload / 1000.0)) if upload > 1 else 0.0
    print("%-10s 载荷 %6.0f KB  发送 %7.0f ms (%7.0f KB/s)  服务端+回程 %7.0f ms  合计 %7.0f ms  HTTP %s"
          % (label, kb, upload, rate, server, upload + server, status))
    return upload, server


def main() -> int:
    key = get_secret("siliconflow_api_key") or ""
    if not key:
        print("缺少 siliconflow_api_key")
        return 2
    samples = decode_to_16k_mono("E:/py/typefast/voice_test/testvoice.mp3", 16000)
    wav = pcm16_wav_bytes(samples, 16000)
    mp3 = encode_mp3(samples)
    print("音频 25.03s")
    probe("WAV", wav, "speech.wav", "audio/wav", key)
    probe("MP3", mp3, "speech.mp3", "audio/mpeg", key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
