"""列出 PortAudio 各 host API 与设备，找可用的输入设备。"""

from __future__ import annotations

import os
import sys

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import sounddevice as sd


def main() -> int:
    print("== host APIs ==")
    for i, api in enumerate(sd.query_hostapis()):
        print("%d %-18s devices=%s in=%s" % (i, api.get("name"), api.get("device_count"), api.get("default_input_device")))
    print("== devices ==")
    for idx, dev in enumerate(sd.query_devices()):
        if int(dev.get("max_input_channels", 0)) < 1:
            continue
        api = sd.query_hostapis(dev.get("hostapi", 0))
        print("%d [%s] %s @ %.0f Hz in=%d" % (idx, api.get("name"), dev.get("name"), float(dev.get("default_samplerate") or 0), int(dev.get("max_input_channels", 0))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
