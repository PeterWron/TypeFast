"""流式分段：录音中在停顿处切段并提交识别，让松手后的等待与录音时长无关。

参数见 config.example.json：目标每段 6s、最短 3.5s、活边缘留 1.5s、目标点附近 4.5s 内找停顿。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from typefast.core.vad import pause_candidates


@dataclass
class CommitPlan:
    cut_sample: int
    target_sample: int
    pause_center: int


class CommitPlanner:
    """纯规划器：不碰 IO，便于单测。"""

    def __init__(self, sample_rate: int = 16000, target_commit_s: float = 6.0,
                 min_commit_s: float = 3.5, live_margin_s: float = 1.5,
                 search_radius_s: float = 4.5, min_pause_s: float = 0.25) -> None:
        self.sample_rate = sample_rate
        self.target_commit_s = target_commit_s
        self.min_commit_s = min_commit_s
        self.live_margin_s = live_margin_s
        self.search_radius_s = search_radius_s
        self.min_pause_s = min_pause_s

    def find_cut(self, samples, committed_index: int, last_empty_cut: int = -1) -> Optional[CommitPlan]:
        sr = float(self.sample_rate)
        live_end = len(samples) - int(self.live_margin_s * sr)
        min_chunk = int(self.min_commit_s * sr)
        if live_end - committed_index < min_chunk:
            return None
        target = committed_index + int(self.target_commit_s * sr)
        radius = int(self.search_radius_s * sr)
        best = None
        for pause in pause_candidates(samples, self.sample_rate, self.min_pause_s):
            c = pause.center_sample
            if c <= last_empty_cut or c <= committed_index:
                continue
            if c - committed_index < min_chunk or c > live_end:
                continue
            if abs(c - target) > radius:
                continue
            if best is None or abs(c - target) < abs(best.center_sample - target):
                best = pause
        if best is None:
            return None
        return CommitPlan(cut_sample=best.center_sample, target_sample=target, pause_center=best.center_sample)

    def tail_range(self, total_samples: int, committed_index: int) -> Tuple[int, int]:
        start = min(committed_index, total_samples)
        return (start, total_samples)
