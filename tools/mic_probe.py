"""麦克风诊断：默认设备是谁，每个输入设备能否以 16k / 设备默认率打开。"""

from __future__ import annotations

import os
import sys

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import sounddevice as sd


def try_open(device, rate):
    try:
        stream = sd.InputStream(device=device, channels=1, samplerate=rate, dtype="float32", blocksize=0)
        stream.start()
        stream.stop()
        stream.close()
        return True, ""
    except Exception as exc:
        return False, str(exc)


def main() -> int:
    print("sd.default.device: %s" % (sd.default.device,))
    try:
        info = sd.query_devices(kind="input")
        print("default input: %s @ %.0f Hz" % (info.get("name"), float(info.get("default_samplerate") or 0)))
    except Exception as exc:
        print("default input: NONE (%s)" % exc)
    for idx, dev in enumerate(sd.query_devices()):
        if int(dev.get("max_input_channels", 0)) < 1:
            continue
        rate = float(dev.get("default_samplerate") or 0)
        ok16, err16 = try_open(idx, 16000)
        okdef, errdef = try_open(idx, rate)
        print("dev %d | %s | 16k=%s | %.0fHz=%s" % (idx, dev.get("name"), ok16, rate, okdef))
        if not ok16 and not okdef:
            print("    err16 : %s" % err16[:140])
            print("    errdef: %s" % errdef[:140])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
