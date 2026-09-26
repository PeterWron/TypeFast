"""引擎内部数据契约（core 与 platform 的边界，保持平台无关、可序列化）。

将来引擎拆成独立进程时，这些结构可以原样换成 protobuf 消息。"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import List


class SessionState(str, Enum):
    IDLE = "idle"
    ARMING = "arming"
    RECORDING = "recording"
    FINALIZING = "finalizing"
    DELIVERING = "delivering"
    DONE = "done"
    EMPTY = "empty"
    ERROR = "error"


class ErrorKind(str, Enum):
    NOT_CONFIGURED = "not_configured"
    AUTH = "auth"          # 401/403：Key 无效。不可重试，也不应触发路由熔断（换模型救不了坏 Key）
    NETWORK = "network"
    TIMEOUT = "timeout"
    SERVER_BUSY = "server_busy"
    SERVER_FAILED = "server_failed"
    NO_SPEECH = "no_speech"
    AUDIO = "audio"
    BLOCKED = "blocked"

    def retriable_in_place(self) -> bool:
        return self in (ErrorKind.SERVER_BUSY, ErrorKind.TIMEOUT)


class ProviderError(Exception):
    """Provider 层统一错误，带错误分类供上层决定重试/降级。"""

    def __init__(self, kind: ErrorKind, message: str = "", detail: str = "") -> None:
        super().__init__(message or kind.value)
        self.kind = kind
        self.message = message or kind.value
        self.detail = detail


@dataclass
class Segment:
    index: int
    start_sample: int
    end_sample: int
    text: str = ""
    polished: str = ""
    polished_ok: bool = False


@dataclass
class Utterance:
    utterance_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: float = field(default_factory=time.monotonic)
    released_at: float = 0.0
    raw_text: str = ""
    final_text: str = ""
    segments: List[Segment] = field(default_factory=list)
    polish_applied: bool = False
    polish_reason: str = ""
    delivered: bool = False

    def add_segment(self, start: int, end: int) -> Segment:
        seg = Segment(index=len(self.segments), start_sample=start, end_sample=end)
        self.segments.append(seg)
        return seg

    def joined_raw(self) -> str:
        return clean_join([s.text or "" for s in self.segments])

    def joined_final(self) -> str:
        parts = []
        for s in self.segments:
            parts.append(s.polished if s.polished_ok and s.polished else s.text)
        return "".join(p or "" for p in parts).strip()


@dataclass
class Timings:
    released_to_text_ms: float = 0.0
    asr_ms: float = 0.0
    asr_encode_ms: float = 0.0
    asr_ttfb_ms: float = 0.0
    asr_download_ms: float = 0.0
    polish_ms: float = 0.0
    delivery_ms: float = 0.0

    def as_row(self) -> str:
        return "release2text=%.0fms asr=%.0fms polish=%.0fms delivery=%.0fms" % (
            self.released_to_text_ms, self.asr_ms, self.polish_ms, self.delivery_ms)


SENTENCE_TAIL = "。！？!?；;、,.，"


def clean_join(parts) -> str:
    # 按停顿切段后把识别结果拼回一段话：每段都会被模型补上句末标点，直接相接会把
    # 一个长句拆成一串短句（实测 12 段产生 11 个多余句号）。非末段去掉句末标点并接逗号，
    # 最终只保留一个句末标点；润色阶段还会再整理一次。
    out = []
    last = len(parts) - 1
    for i, part in enumerate(parts):
        text = (part or "").strip()
        if i < last:
            text = text.rstrip(SENTENCE_TAIL)
            if text:
                text += "，"
        out.append(text)
    joined = "".join(out).strip()
    if not joined:
        return ""
    return joined.rstrip(SENTENCE_TAIL) + "。"
