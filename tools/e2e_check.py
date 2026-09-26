"""端到端验证：演示语音（系统 TTS 合成）→ 云端识别 → 润色，打印文本与各阶段耗时。"""

from __future__ import annotations

import os
import sys
import time

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from typefast.cli import build_asr, build_polisher
from typefast.core.contracts import Timings
from typefast.core.pipeline import VoicePipeline
from typefast.core.router import ModelRouter
from typefast.core.vocabulary import Vocabulary
from typefast.platform.tts import DEMO_TEXT, ensure_demo_speech
from typefast.settings import Config


def main() -> int:
    cfg = Config.load()
    print("asr    : %s %s" % (cfg.asr.provider, cfg.asr.model))
    print("polish : %s %s" % (cfg.polish.provider, cfg.polish.model))
    samples = ensure_demo_speech()
    if samples is None:
        print("演示语音不可用")
        return 1
    seconds = samples.size / float(cfg.audio.sample_rate)
    print("audio  : %.2fs" % seconds)
    print("spoken : " + DEMO_TEXT)
    pipe = VoicePipeline(asr=build_asr(cfg), polisher=build_polisher(cfg), config=cfg,
                         router=ModelRouter(), vocab=Vocabulary().load())
    timings = Timings()
    started = time.monotonic()
    raw = pipe.transcribe(samples, timings)
    print("asr    : %.0fms" % timings.asr_ms)
    print("raw    : " + raw)
    print("codepoints: " + str([hex(ord(c)) for c in raw[:6]]))
    final, ok, reason = pipe.polish_text(raw, timings)
    total = (time.monotonic() - started) * 1000
    print("polish : %s (%s) %.0fms" % (ok, reason, timings.polish_ms))
    print("final  : " + final)
    print("total  : %.0fms" % total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
