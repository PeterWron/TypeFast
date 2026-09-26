"""麦克风自检：录 2 秒，打印设备实际采样率与重采样后的样本数。

音频只留在内存，不落盘、不上传。"""

from __future__ import annotations

import os
import sys
import time

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from typefast.platform.audio import Recorder, device_list


def main() -> int:
    print("input devices:")
    for line in device_list():
        print("  " + line)
    rec = Recorder(sample_rate=16000, log=print)
    print("recording 2.0s ...")
    rec.start()
    time.sleep(2.0)
    samples = rec.stop()
    print("device rate: %.0f Hz" % rec._active_rate)
    print("samples: %d (expect about %d)" % (len(samples), 32000))
    if samples.size:
        peak = float(abs(samples).max())
        rms = float((samples * samples).mean() ** 0.5)
        print("peak %.4f  rms %.4f" % (peak, rms))
    ok = len(samples) > 16000
    print("RESULT: " + ("OK" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
