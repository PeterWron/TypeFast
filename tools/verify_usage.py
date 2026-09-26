"""核对：请求是否真的打到了 XingChenASR，以及账户侧能看到什么。

1) 打印本机配置里的模型名（含不可见字符检查）
2) 查 /v1/models 确认模型 ID 存在
3) 查账户信息接口（余额/用量，如果有）
4) 发一次 2 秒的真实识别请求，打印完整响应头与 body（含 request id）"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import wave

import numpy as np
import requests

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from typefast.platform.tts import ensure_demo_speech
from typefast.providers.openai_compat import pcm16_wav_bytes
from typefast.settings import Config, get_secret

BASE = "https://api.siliconflow.cn/v1"
MODEL = "XingChenAGI/XingChenASR-V3.2-Ultra"


def show(label: str, value) -> None:
    print("%-22s %s" % (label, value))


def main() -> int:
    cfg = Config.load()
    key = get_secret("siliconflow_api_key") or ""
    show("key", (key[:8] + "..." + key[-4:]) if key else "缺失")
    show("config.asr.provider", cfg.asr.provider)
    show("config.asr.model", repr(cfg.asr.model))
    show("config.asr.endpoint", repr(cfg.asr.endpoint))
    show("代码默认模型", MODEL)
    headers = {"Authorization": "Bearer " + key}
    print()
    print("=== 1. 模型列表（筛 ASR 相关） ===")
    try:
        r = requests.get(BASE + "/models", headers=headers, timeout=30)
        print("GET /v1/models -> HTTP %d" % r.status_code)
        data = r.json().get("data", []) if r.status_code == 200 else []
        ids = [str(m.get("id")) for m in data]
        show("模型总数", len(ids))
        hits = [i for i in ids if any(k in i for k in ("ASR", "asr", "XingChen", "SenseVoice", "Audio", "audio"))]
        show("ASR/音频相关", len(hits))
        for i in hits[:20]:
            print("    " + i)
        show("目标模型在列表里", MODEL in ids)
        if r.status_code != 200:
            print((r.text or "")[:300])
    except Exception as exc:
        print("失败：" + repr(exc))
    print()
    print("=== 2. 账户信息（余额/用量，若接口存在） ===")
    for path in ("/user/info", "/user/account", "/user/usage"):
        try:
            r = requests.get(BASE + path, headers=headers, timeout=30)
            body = (r.text or "")[:500]
            print("%-16s HTTP %d  %s" % (path, r.status_code, body))
        except Exception as exc:
            print("%-16s 失败 %s" % (path, repr(exc)))
    print()
    print("=== 3. 单次识别请求（2 秒音频，完整响应头） ===")
    samples = ensure_demo_speech()
    if samples is None:
        print("演示语音不可用")
        return 1
    clip = samples[: 2 * 16000]
    wav = pcm16_wav_bytes(clip, 16000)
    data = {"model": cfg.asr.model or MODEL}
    show("请求 data 字段", data)
    show("form 里的 model", repr(data["model"]))
    show("WAV 字节", len(wav))
    show("音频时长", "%.2f s" % (clip.size / 16000.0))
    t0 = time.perf_counter()
    r = requests.post(BASE + "/audio/transcriptions", headers=headers,
                      files={"file": ("speech.wav", wav, "audio/wav")}, data=data,
                      timeout=120)
    cost = (time.perf_counter() - t0) * 1000
    print("HTTP %d  %.0f ms" % (r.status_code, cost))
    print("响应头：")
    for k, v in r.headers.items():
        print("    %s: %s" % (k, v))
    print("响应体：" + (r.text or "")[:500])
    try:
        print("识别文本：" + str(r.json().get("text")))
        print("计费信息：" + json.dumps(r.json().get("usage"), ensure_ascii=False))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
