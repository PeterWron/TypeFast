"""Provider 抽象：ASR 与润色。

core 只依赖这里的协议，不依赖任何厂商 SDK；换厂商只需新增一个实现并在 cli 里登记。
PolishRequest.model 允许按请求指定模型：留空用 provider 默认，填了就用它（配合 models=auto 的路由）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Protocol, runtime_checkable


@dataclass
class AsrRequest:
    samples: object
    sample_rate: int = 16000
    language: str = "zh-en"
    hotwords: List[str] = field(default_factory=list)


@dataclass
class AsrResult:
    text: str = ""
    provider: str = ""
    latency_ms: float = 0.0
    encode_ms: float = 0.0
    ttfb_ms: float = 0.0
    download_ms: float = 0.0


@dataclass
class PolishRequest:
    text: str
    deadline_ms: int = 1500
    hotwords: List[str] = field(default_factory=list)
    output_language: str = ""
    model: str = ""


@runtime_checkable
class AsrProvider(Protocol):
    name: str

    def transcribe(self, req: AsrRequest) -> AsrResult:
        ...


@runtime_checkable
class PolishProvider(Protocol):
    name: str

    def polish(self, req: PolishRequest) -> str:
        ...
