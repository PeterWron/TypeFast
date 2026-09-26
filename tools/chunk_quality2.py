"""验证折中方案：3.5s 切段 + 只在强停顿处切 + 拼接时清理句末标点；并测试接口是否支持 prompt 上下文参数。"""

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


def transcribe(session, samples, rate: int, prompt: str = EMPTY) -> str:
    key = get_secret("siliconflow_api_key") or EMPTY
    data = {"model": MODEL}
    if prompt:
        data["prompt"] = prompt[-200:]
    resp = session.post(ASR_URL, headers={"Authorization": "Bearer " + key},
                        files={"file": ("c.wav", pcm16_wav_bytes(samples, rate), "audio/wav")},
                        data=data, timeout=180)
    if resp.status_code >= 400:
        return "HTTP " + str(resp.status_code) + " " + (resp.text or EMPTY)[:120]
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
        parts = [f.result() for f in futures]
    return parts


def join_clean(parts) -> str:
    out = []
    last = len(parts) - 1
    for i, part in enumerate(parts):
        text = part.strip()
        if i < last:
            text = text.rstrip(TRAIL)
            if text:
                text = text + "，"
            elif out:
                continue
        out.append(text)
    joined = EMPTY.join(out)
    return joined.rstrip(TRAIL) + "。"


def report(ref: str, other: str, label: str) -> None:
    a, b = normalise(ref), normalise(other)
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    ops = [op for op in difflib.SequenceMatcher(None, a, b).get_opcodes() if op[0] != "equal"]
    print("%-26s 字数 %3d  相似度 %.4f  差异段 %2d  句号 %2d  逗号 %2d"
          % (label, len(b), ratio, len(ops), other.count("。"), other.count("，")))


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "E:/py/typefast/voice_test/testvoice.mp3"
    rate = 16000
    samples = decode_to_16k_mono(path, rate)
    session = requests.Session()
    ref = transcribe(session, samples, rate)
    print("参考（整段）：" + ref)
    print()
    variants = [
        ("D 3.5s + 强停顿 0.35s", 3.5, 1.8, 1.0, 2.0, 0.35),
        ("E 2.5s + 强停顿 0.35s", 2.5, 1.5, 0.9, 1.5, 0.35),
        ("F 2.5s + 停顿 0.22s", 2.5, 1.2, 0.8, 1.5, 0.22),
    ]
    for label, target, minc, margin, radius, pause in variants:
        ranges = cut(samples, rate, target, minc, margin, radius, pause)
        t0 = time.perf_counter()
        parts = run_chunked(samples, rate, ranges)
        cost = (time.perf_counter() - t0) * 1000
        raw_join = EMPTY.join(p for p in parts if p)
        clean = join_clean(parts)
        print("%s：%d 段  墙钟 %.0fms" % (label, len(ranges), cost))
        report(ref, raw_join, "  直接用原标点")
        report(ref, clean, "  拼接时清理标点")
        print()
    print("=== prompt 上下文参数支持测试 ===")
    probe = transcribe(session, samples[: 3 * rate], rate, prompt="搬家那天雨一直没停，我蹲在客厅地上")
    print("带 prompt 的返回：" + probe)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
