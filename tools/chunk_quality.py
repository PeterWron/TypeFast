"""切段对识别质量的影响：同一段音频分别用「整段」「6s 切段」「2.5s 切段」识别，对比文本差异。

A 整段（重复两次，用于估计服务端自身抖动）；B 6s 切段（当前默认）；C 2.5s 切段（拟改为默认）。
指标：去标点空白后的相似度、标点数、差异片段、逐段文本。"""

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
EMPTY = str()


def transcribe(session, samples, rate: int) -> str:
    key = get_secret("siliconflow_api_key") or EMPTY
    wav = pcm16_wav_bytes(samples, rate)
    resp = session.post(ASR_URL, headers={"Authorization": "Bearer " + key},
                        files={"file": ("c.wav", wav, "audio/wav")},
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
        parts = [f.result() for f in futures]
    return EMPTY.join(p for p in parts if p), parts


def diff_report(ref: str, other: str, label: str) -> None:
    a, b = normalise(ref), normalise(other)
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    sm = difflib.SequenceMatcher(None, a, b)
    ops = [op for op in sm.get_opcodes() if op[0] != "equal"]
    print("%-22s 字数 %3d  相似度 %.4f  差异段 %d" % (label, len(b), ratio, len(ops)))
    for tag, i1, i2, j1, j2 in ops[:6]:
        print("    %-8s 参考[%s] -> 本段[%s]" % (tag, a[i1:i2][:24], b[j1:j2][:24]))


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "E:/py/typefast/voice_test/testvoice.mp3"
    rate = 16000
    samples = decode_to_16k_mono(path, rate)
    seconds = samples.size / rate
    print("音频 %.2fs" % seconds)
    session = requests.Session()
    t0 = time.perf_counter()
    a1 = transcribe(session, samples, rate)
    t1 = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    a2 = transcribe(session, samples, rate)
    t2 = (time.perf_counter() - t0) * 1000
    print("整段识别耗时：%.0fms / %.0fms" % (t1, t2))
    print("参考文本：" + a1)
    print()
    b_ranges = cut(samples, rate, 6.0, 3.5, 1.5, 4.5, 0.25)
    t0 = time.perf_counter()
    b_text, _b = run_chunked(samples, rate, b_ranges)
    tb = (time.perf_counter() - t0) * 1000
    c_ranges = cut(samples, rate, 2.5, 1.2, 0.8, 1.5, 0.22)
    t0 = time.perf_counter()
    c_text, c_parts = run_chunked(samples, rate, c_ranges)
    tc = (time.perf_counter() - t0) * 1000
    print("B 6.0s 切段：%d 段  墙钟 %.0fms" % (len(b_ranges), tb))
    print("C 2.5s 切段：%d 段  墙钟 %.0fms  切点：%s"
          % (len(c_ranges), tc, [(round(a / rate, 1), round(b / rate, 1)) for a, b in c_ranges]))
    print()
    print("=== 相似度对比（参考 = 整段第一次结果） ===")
    diff_report(a1, a2, "A 整段（重复跑）")
    diff_report(a1, b_text, "B 6s 切段")
    diff_report(a1, c_text, "C 2.5s 切段")
    print()
    print("标点：整段 %d 句号 / %d 逗号；6s %d / %d；2.5s %d / %d"
          % (a1.count("。"), a1.count("，"), b_text.count("。"), b_text.count("，"),
             c_text.count("。"), c_text.count("，")))
    print()
    print("=== C 2.5s 每段文本（检查切点是否把词切开） ===")
    for i, part in enumerate(c_parts):
        a, b = c_ranges[i]
        print("  #%02d %.1f-%.1fs  %s" % (i + 1, a / rate, b / rate, part))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
