"""Mock Provider：不需要任何 Key，用于离线自检、单测与压测。"""

from __future__ import annotations

import time

from typefast.core.contracts import ErrorKind, ProviderError
from typefast.providers.base import AsrRequest, AsrResult, PolishRequest


class MockAsrProvider:
    name = "mock"

    def __init__(self, text: str = "这是一段离线自检用的识别结果") -> None:
        self.text = text

    def transcribe(self, req: AsrRequest) -> AsrResult:
        started = time.perf_counter()
        seconds = len(req.samples) / float(req.sample_rate or 16000) if req.samples is not None else 0.0
        if seconds < 0.2:
            raise ProviderError(ErrorKind.NO_SPEECH, "音频过短")
        time.sleep(min(0.35, seconds * 0.05))
        return AsrResult(text=self.text, provider=self.name, latency_ms=(time.perf_counter() - started) * 1000)


class MockPolishProvider:
    name = "mock"

    def polish(self, req: PolishRequest) -> str:
        text = (req.text or "").strip()
        if not text:
            return ""
        time.sleep(0.12)
        polished = text.replace("然后", "").replace("那个", "")
        if polished and not polished.endswith(("。", "！", "？", ".", "!", "?")):
            polished += "。"
        return polished
