"""延时基准：同一个音频文件跑多轮，按阶段给出 P50/P95。

场景 1 批量转写：当前 transcribe 的真实行为（按配置切段 + 并行识别 + 润色）。
场景 2 模拟边录边发：不同切段粒度下「松手后还要等多久」= 最慢那一段的耗时。

用法：python tools/bench_latency.py [音频] [轮数]"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from typefast.cli import build_pipeline, plan_file_segments
from typefast.core.contracts import Timings
from typefast.core.segmenter import CommitPlanner
from typefast.platform.audiofile import decode_to_16k_mono
from typefast.settings import Config, data_subdir


def ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000.0


def pct(values, p: float) -> float:
    if not values:
        return 0.0
    data = sorted(values)
    idx = min(len(data) - 1, max(0, int(round(len(data) * p / 100.0)) - 1))
    return data[idx]


def cut_ranges(samples, rate: int, target: float):
    planner = CommitPlanner(sample_rate=rate, target_commit_s=target, min_commit_s=1.2,
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


def run_batch(pipe, samples, config, rate: int):
    ranges = plan_file_segments(samples, config)
    chunks = []
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = []
        for a, b in ranges:
            tm = Timings()
            futures.append(((b - a) / rate, tm, pool.submit(pipe.transcribe, samples[a:b], tm)))
        texts = []
        for sec, tm, fut in futures:
            texts.append(fut.result())
            chunks.append((sec, tm.asr_ms, tm.asr_ttfb_ms, tm.asr_encode_ms, tm.asr_download_ms))
    asr_wall = ms(t0)
    raw = "".join(t.strip() for t in texts if t and t.strip())
    tm2 = Timings()
    t1 = time.perf_counter()
    final, ok, reason = pipe.polish_text(raw, tm2)
    polish_ms = ms(t1)
    return asr_wall, polish_ms, len(raw), len(final), ok, reason, chunks


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "E:/py/typefast/voice_test/testvoice.mp3"
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    config = Config.load()
    rate = int(config.audio.sample_rate)
    t0 = time.perf_counter()
    samples = decode_to_16k_mono(path, rate)
    decode_ms = ms(t0)
    seconds = samples.size / rate
    print("文件 : %s" % path)
    print("音频 : %.2fs  解码 %.0fms  ASR %s 润色 %s" % (seconds, decode_ms, config.asr.model, config.polish.model))
    pipe = build_pipeline(config, lambda m: None)
    print()
    print("=== 场景 1：批量转写（%d 轮） ===" % rounds)
    print("%-6s %10s %10s %10s %8s %8s" % ("轮次", "识别墙钟ms", "润色ms", "合计ms", "原文字数", "润色"))
    asr_wall, polish, total = [], [], []
    chunk_rows = []
    for i in range(rounds):
        a, p, raw_len, fin_len, ok, reason, chunks = run_batch(pipe, samples, config, rate)
        chunk_rows.extend(chunks)
        asr_wall.append(a)
        polish.append(p)
        total.append(a + p)
        print("%-6d %10.0f %10.0f %10.0f %8d %8s" % (i + 1, a, p, a + p, raw_len, "ok" if ok else reason))
    print("P50    %10.0f %10.0f %10.0f" % (pct(asr_wall, 50), pct(polish, 50), pct(total, 50)))
    print("P95    %10.0f %10.0f %10.0f" % (pct(asr_wall, 95), pct(polish, 95), pct(total, 95)))
    print("平均   %10.0f %10.0f %10.0f" % (statistics.mean(asr_wall), statistics.mean(polish), statistics.mean(total)))
    print()
    print("=== 单段拆解（%d 个样本） ===" % len(chunk_rows))
    for label, idx in (("单段总耗时", 1), ("其中 首字节TTFB", 2), ("其中 本地编码", 3), ("其中 回程", 4)):
        vals = [r[idx] for r in chunk_rows]
        print("%-14s P50 %8.0f  P95 %8.0f  最大 %8.0f" % (label, pct(vals, 50), pct(vals, 95), max(vals)))
    print("单段音频长度 ： %.1f - %.1f s" % (min(r[0] for r in chunk_rows), max(r[0] for r in chunk_rows)))
    print()
    print("=== 场景 2：模拟边录边发（各 1 轮，看松手后还要等多久） ===")
    for target in (2.0, 3.5, 6.0):
        ranges = cut_ranges(samples, rate, target)
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = []
            for a, b in ranges:
                tm = Timings()
                futures.append(((b - a) / rate, tm, pool.submit(pipe.transcribe, samples[a:b], tm)))
            lat = []
            for sec, tm, fut in futures:
                fut.result()
                lat.append(tm.asr_ms)
        wall = ms(t0)
        print("切段 %.1fs -> %2d 段  单段 P50 %6.0fms  最大 %6.0fms  (松手等待≈最大段耗时)  并发墙钟 %6.0fms"
              % (target, len(ranges), pct(lat, 50), max(lat), wall))
    out = data_subdir("records") / "bench_latency.json"
    try:
        out.write_text(json.dumps({"file": path, "seconds": seconds, "rounds": rounds,
                                 "asr_wall": asr_wall, "polish": polish, "total": total}, ensure_ascii=False, indent=2), encoding="utf-8")
        print()
        print("结果已存：" + str(out))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
