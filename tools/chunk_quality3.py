"""最终参数验证：4.0s 目标 + 0.30s 停顿门限 + 拼接清理标点，与整段参考对比。"""

from __future__ import annotations

import difflib
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from typefast.core.segmenter import CommitPlanner
from typefast.platform.audiofile import decode_to_16k_mono
from typefast.providers.openai_compat import pcm16_wav_bytes
from typefast.settings import get_secret

ASR_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
MODEL = "XingChenAGI/XingChenASR-V3.2-Ultra"
PUNCT = re.compile("[\\s，。！？,.!?、；;：:~～…—\\-]+")
TRAIL = "。！？!?；;、,.，"
EMPTY = str()


def transcribe(session, samples, rate: int) -> str:
    key = get_secret("siliconflow_api_key") or EMPTY
    resp = session.post(ASR_URL, headers={"Authorization": "Bearer " + key},
                        files={"file": ("c.wav", pcm16_wav_bytes(samples, rate), "audio/wav")},
                        data={"model": MODEL}, timeout=180)
    return str(resp.json().get("text") or EMPTY).strip()


def normalise(text: str) -> str:
    return PUNCT.sub(EMPTY, text)


def cut(samples, rate: int, target: float, min_commit: float, margin: float, radius: float, pause: float):
    planner = CommitPlanner(sample_rate=rate, target_commit_s=target, min_commit_s=min_commit,
                             live_margin_s=margin, search_radius_s=radius, min_pause_s=pause)
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


def run_chunked(samples, rate: int, ranges, workers: int = 3):
    session = requests.Session()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(transcribe, session, samples[a:b], rate) for a, b in ranges]
        return [f.result() for f in futures]


def join_clean(parts) -> str:
    out = []
    last = len(parts) - 1
    for i, part in enumerate(parts):
        text = part.strip()
        if i < last:
            text = text.rstrip(TRAIL)
            if text:
                text = text + "，"
        out.append(text)
    return EMPTY.join(out).rstrip(TRAIL) + "。"


def report(ref: str, other: str, label: str) -> None:
    a, b = normalise(ref), normalise(other)
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    ops = [op for op in difflib.SequenceMatcher(None, a, b).get_opcodes() if op[0] != "equal"]
    detail = EMPTY
    for tag, i1, i2, j1, j2 in ops[:4]:
        detail += " [%s: %s->%s]" % (tag, a[i1:i2][:16], b[j1:j2][:16])
    print("%-30s 相似度 %.4f  差异 %d  句号 %2d  逗号 %2d%s"
          % (label, ratio, len(ops), other.count("。"), other.count("，"), detail))


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "E:/py/typefast/voice_test/testvoice.mp3"
    rate = 16000
    samples = decode_to_16k_mono(path, rate)
    session = requests.Session()
    ref = transcribe(session, samples, rate)
    print("参考（整段）：" + ref)
    print()
    for label, target, minc, margin, radius, pause in [
        ("G 4.0s / 门限 0.30s", 4.0, 2.5, 1.0, 1.5, 0.30),
        ("H 5.0s / 门限 0.30s", 5.0, 3.0, 1.2, 2.0, 0.30),
    ]:
        ranges = cut(samples, rate, target, minc, margin, radius, pause)
        t0 = time.perf_counter()
        parts = run_chunked(samples, rate, ranges)
        cost = (time.perf_counter() - t0) * 1000
        clean = join_clean(parts)
        print("%s：%d 段  墙钟 %.0fms  切点 %s"
              % (label, len(ranges), cost, [(round(a / rate, 1), round(b / rate, 1)) for a, b in ranges]))
        report(ref, EMPTY.join(p for p in parts if p), "  原样拼接")
        report(ref, clean, "  清理标点后")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
