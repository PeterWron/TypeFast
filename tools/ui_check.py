"""UI 自检：构建设置窗口与全部控件，读取一次配置后关闭（不进消息循环）。"""

from __future__ import annotations

import os
import sys

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from typefast.ui.settings_window import SettingsWindow, service_pid


def main() -> int:
    win = SettingsWindow()
    cfg = win._collect()
    print("widgets: %d" % len(win._vars))
    print("asr    : %s %s" % (cfg.asr.provider, cfg.asr.model or "(默认)"))
    print("polish : %s %s" % (cfg.polish.provider, cfg.polish.model or "(默认)"))
    print("hotkey : %s" % cfg.hotkey.hold)
    print("router : %s %s" % (cfg.router.mode, cfg.router.quality_chain))
    print("service: %s" % (service_pid() or "未运行"))
    win.root.destroy()
    print("UI CHECK OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
