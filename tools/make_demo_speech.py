"""重新合成更长的演示语音（三句话，约 15-18 秒），让演示模式能触发多次切段与并发提交。"""

from __future__ import annotations

import os
import sys

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from typefast.platform import tts

TEXT = ("今天下午三点开会，讨论新版本的排期和人力安排。"
        "另外把上次遗留的两个问题也过一遍，特别是延迟这块。"
        "最后确认下周的发布计划，看看要不要往后推两天。")


def main() -> int:
    path = tts.demo_speech_path()
    if path.exists():
        path.unlink()
    result = tts.synthesize(TEXT, path)
    if result is None:
        print("合成失败")
        return 1
    samples = tts.load_16k(path, 16000)
    print("已生成 %s" % result)
    print("时长 %.1f 秒，采样 %d" % (len(samples) / 16000.0, len(samples)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
