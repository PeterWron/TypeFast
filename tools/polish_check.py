"""润色验证：拿一句带口头禅的话打一次真实润色接口，看改写效果与耗时。"""

from __future__ import annotations

import os
import sys
import time

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from typefast.cli import build_polisher
from typefast.providers.base import PolishRequest
from typefast.settings import Config

SAMPLES = [
    "然后那个我们今天讨论一下吧，关于新版本的排期和人力安排，就那个先把时间定下来",
    "嗯这个方案我觉得还行吧就是细节还得再想想",
]


def main() -> int:
    cfg = Config.load()
    print("polish : %s %s" % (cfg.polish.provider, cfg.polish.model))
    polisher = build_polisher(cfg)
    for text in SAMPLES:
        started = time.monotonic()
        try:
            out = polisher.polish(PolishRequest(text=text, deadline_ms=cfg.polish.deadline_ms,
                                         model=cfg.polish.model))
        except Exception as exc:
            print("失败：" + str(exc))
            return 1
        cost = (time.monotonic() - started) * 1000
        print("--- %.0fms" % cost)
        print("in : " + text)
        print("out: " + out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
