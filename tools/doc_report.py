"""诊断：列出文档里被转义破坏的正文行（控制字符 / 多余反引号），供修复参考。"""

from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICK = chr(96)
FENCE = TICK * 3
DOCS = ["README.md", "DEVELOPMENT.md", "CHANGELOG.md", "docs/ARCHITECTURE.md", "docs/ROADMAP.md", "docs/LATENCY.md"]


def main() -> int:
    for path in DOCS:
        full = os.path.join(ROOT, path)
        if not os.path.exists(full):
            continue
        lines = open(full, encoding="utf-8").read().split(chr(10))
        inside = False
        bad = []
        for i, line in enumerate(lines, 1):
            if line.strip().startswith(FENCE):
                inside = not inside
                continue
            if inside:
                continue
            body = line[:-1] if line.endswith(chr(13)) else line
            has_ctrl = any(ch in body for ch in (chr(13), chr(9), chr(11), chr(12), chr(7)))
            if has_ctrl or TICK in body:
                bad.append((i, repr(body)))
        print("== %s  可疑行 %d 条 ==" % (path, len(bad)))
        for i, text in bad[:40]:
            print("  %4d %s" % (i, text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
