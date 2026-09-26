"""能量 VAD：帧能量、底噪估计、停顿候选点。

底噪用低分位（默认 P2）而不是 P10：录音里有效语音往往占 90% 以上，
P10 会把语音本身当底噪，导致整段都判成静音。阈值同时用峰值的一个比例兜底。
纯 numpy、无额外依赖；以后换 WebRTC VAD 或 Silero 只替换本模块。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

import numpy as np

FRAME_MS = 25
HOP_MS = 10
FLOOR_PERCENTILE = 2.0
FLOOR_FACTOR = 3.0
PEAK_RATIO = 0.06
FLOOR_MIN = 0.004


def frame_energies(samples, sample_rate: int = 16000, frame_ms: int = FRAME_MS, hop_ms: int = HOP_MS) -> np.ndarray:
    frame = max(1, int(sample_rate * frame_ms / 1000))
    hop = max(1, int(sample_rate * hop_ms / 1000))
    x = np.asarray(samples, dtype=np.float32)
    if x.size < frame:
        return np.zeros(0, dtype=np.float32)
    n = 1 + (x.size - frame) // hop
    idx = np.arange(frame)[None, :] + hop * np.arange(n)[:, None]
    frames = x[idx]
    return np.sqrt((frames * frames).mean(axis=1) + 1e-12)


def noise_floor(energies) -> float:
    if energies is None or len(energies) == 0:
        return 0.0
    return float(np.percentile(energies, FLOOR_PERCENTILE))


def speech_threshold(energies, factor: float = FLOOR_FACTOR, peak_ratio: float = PEAK_RATIO) -> float:
    if energies is None or len(energies) == 0:
        return FLOOR_MIN
    peak = float(np.max(energies))
    return max(noise_floor(energies) * factor, peak * peak_ratio, FLOOR_MIN)


def speech_ratio(samples, sample_rate: int = 16000) -> float:
    e = frame_energies(samples, sample_rate)
    if e.size == 0:
        return 0.0
    return float((e > speech_threshold(e)).mean())


@dataclass
class Pause:
    start_sample: int
    end_sample: int

    @property
    def center_sample(self) -> int:
        return (self.start_sample + self.end_sample) // 2

    @property
    def duration_samples(self) -> int:
        return self.end_sample - self.start_sample


def pause_candidates(samples, sample_rate: int = 16000, min_pause_s: float = 0.25,
                     hop_ms: int = HOP_MS) -> List[Pause]:
    e = frame_energies(samples, sample_rate, hop_ms=hop_ms)
    if e.size == 0:
        return []
    threshold = speech_threshold(e)
    quiet = e <= threshold
    hop = max(1, int(sample_rate * hop_ms / 1000))
    min_frames = max(1, int(min_pause_s * 1000 / hop_ms))
    out: List[Pause] = []
    run_start = None
    for i, is_quiet in enumerate(quiet):
        if is_quiet and run_start is None:
            run_start = i
        elif not is_quiet and run_start is not None:
            if i - run_start >= min_frames:
                out.append(Pause(run_start * hop, i * hop))
            run_start = None
    return out
