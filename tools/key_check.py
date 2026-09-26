"""核对两个凭据槽里放的是不是同一把 Key（只打印指纹，不打印完整 Key）。"""

from __future__ import annotations

import os
import sys

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from typefast.settings import Config, get_secret


def fp(value) -> str:
    if not value:
        return "缺失"
    return "%s...%s (长度 %d)" % (value[:6], value[-4:], len(value))


def main() -> int:
    asr_key = get_secret("siliconflow_api_key")
    polish_key = get_secret("polish_api_key")
    print("凭据槽 siliconflow_api_key : " + fp(asr_key))
    print("凭据槽 polish_api_key      : " + fp(polish_key))
    print("两者是否同一把 Key        : " + str(bool(asr_key) and asr_key == polish_key))
    cfg = Config.load()
    print()
    print("识别 provider : " + cfg.asr.provider + "  model=" + cfg.asr.model)
    print("识别 endpoint : " + (cfg.asr.endpoint or "(内置默认) https://api.siliconflow.cn/v1/audio/transcriptions"))
    print("润色 provider : " + cfg.polish.provider + "  model=" + cfg.polish.model)
    print("润色 endpoint : " + (cfg.polish.endpoint or "(内置默认) https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
