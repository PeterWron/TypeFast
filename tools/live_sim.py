"""模拟「边录边发」：同一段音频按不同切段粒度并发提交，看松手后还要等多久。

松手等待 ≈ 最后一段自己的耗时（前几段在说话过程中就已经发完）。"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Tuple

import numpy as np
import requests

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from typefast.core.segmenter import CommitPlanner
from typefast.platform.audiofile import decode_to_16k_mono
from typefast.providers.openai_compat import pcm16_wav_bytes
from typefast.settings import get_secret

ASR_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
ASR_MODEL = "XingChenAGI/XingChenASR-V3.2-Ultra"
CHUNK_TARGETS = [2.0, 3.5, 6.0]
WORKERS = 6


def cut(samples, rate: int, target: float, min_commit: float = 1.2) -> List[Tuple[int, int]]:
    planner = CommitPlanner(sample_rate=rate, target_commit_s=target, min_commit_s=min_commit,
                             live_margin_s=0.8, search_radius_s=max(1.0, target * 0.6),
                             min_pause_s=0.22)
    ranges = []
    committed = 0
    while True:
        plan = planner.find_cut(samples, committed, -1)
        if plan is None or plan.cut_sample <= committed:
            break
        ranges.append((committed, int(plan.cut_sample)))
        committed = int(plan.cut_sample)
    if committed < samples.size:
        ranges.append((committed, int(samples.size)))
    return ranges or [(0, int(samples.size))]


def transcribe(session, payload: bytes) -> Tuple[float, str]:
    key = get_secret("siliconflow_api_key") or ""
    t0 = time.perf_counter()
    resp = session.post(ASR_URL, headers={"Authorization": "Bearer " + key},
                        files={"file": ("chunk.wav", payload, "audio/wav")},
                        data={"model": ASR_MODEL}, timeout=180)
    cost = (time.perf_counter() - t0) * 1000.0
    text = ""
    try:
        text = str(resp.json().get("text") or "")
    except Exception:
        text = "HTTP " + str(resp.status_code)
    return cost, text


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "E:/py/typefast/voice_test/testvoice.mp3"
    rate = 16000
    samples = decode_to_16k_mono(path, rate)
    total_s = samples.size / rate
    print("音频 %.1fs，并发上限 %d" % (total_s, WORKERS))
    for target in CHUNK_TARGETS:
        ranges = cut(samples, rate, target)
        payloads = [pcm16_wav_bytes(samples[a:b], rate) for a, b in ranges]
        seconds = [(b - a) / rate for a, b in ranges]
        session = requests.Session()
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = [(i, sec, pool.submit(transcribe, session, payloads[i]))
                       for i, sec in enumerate(seconds)]
            results = []
            for i, sec, fut in futures:
                cost, text = fut.result()
                results.append((i, sec, cost, len(text)))
        wall = (time.perf_counter() - started) * 1000.0
        last = max(r[2] for r in results)
        med = statistics.median(r[2] for r in results)
        print()
        print("切段目标 %.1fs -> %d 段" % (target, len(ranges)))
        print("  每段: " + "  ".join("%.1fs/%.0fms" % (r[1], r[2]) for r in results))
        print("  段耗时 中位 %.0fms / 最大 %.0fms；并发墙钟 %.0fms" % (med, last, wall))
        print("  估算「松手等待」= 最后一段耗时 %.0fms（其余段在说话时已完成）" % last)
    print()
    print("注：这里不含润色；润色需再叠加 0.6-2.0s（取决于文本长度与模型）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
