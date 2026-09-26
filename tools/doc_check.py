"""文档体检：围栏是否成对、是否有残留占位符与控制字符。"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICK = chr(96)
FENCE = TICK * 3
AT = chr(64)
PLACEHOLDERS = [AT + chr(78) + AT, AT + chr(81) + AT]
DOCS = ["README.md", "DEVELOPMENT.md", "CHANGELOG.md", "docs/ARCHITECTURE.md", "docs/ROADMAP.md", "docs/LATENCY.md"]


def main() -> int:
    bad = 0
    for path in DOCS:
        full = os.path.join(ROOT, path)
        if not os.path.exists(full):
            continue
        text = open(full, encoding="utf-8").read()
        lines = text.split(chr(10))
        fences = [line for line in lines if line.startswith(FENCE)]
        has_placeholder = any(p in text for p in PLACEHOLDERS)
        has_ctrl = any(ch in text for ch in (chr(9), chr(11), chr(12), chr(7)))
        odd_start = any(line.startswith(FENCE) and line.strip() != FENCE
                        and not line[3:].strip().isalpha() for line in lines)
        ok = (len(fences) % 2 == 0) and not has_placeholder and not has_ctrl and not odd_start
        if not ok:
            bad += 1
        print("%-24s 行 %4d  围栏 %2d 项  占位符 %s  控制字符 %s  %s"
              % (path, len(lines), len(fences), has_placeholder, has_ctrl, "OK" if ok else "需检查"))
    print("问题文件数：%d" % bad)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
