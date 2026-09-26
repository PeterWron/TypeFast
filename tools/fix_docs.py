"""修复 Markdown 反引号破坏（v6，最终版）。

相比 v5：先合并被错误换行的表格行（上一行以竖线与空格结尾、本行以行内代码开头），再归一化围栏。

本脚本自身不使用生反引号，全部用 chr(96) 拼接。"""

from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICK = chr(96)
FENCE = TICK * 3
NL = chr(10)
CR = chr(13)

DOCS = ["README.md", "DEVELOPMENT.md", "CHANGELOG.md", "docs/ARCHITECTURE.md", "docs/ROADMAP.md", "docs/LATENCY.md"]

PHRASES = [
    "python -m pytest -q", "python tools/bench_latency.py", "tools/bench_latency.py",
    "run --demo --selftest", "run --demo", "transcribe <音频>",
    "typefast key status", "run_typefast.bat start", "run_typefast.bat <子命令>",
    "verify_usage.py", "key set", "key status",
    "doctor", "pytest -q", "ui", "config", "start", "transcribe",
]

LETTERS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_/")

DAMAGE = [
    ("un_typefast.bat", "run_typefast.bat"),
    ("un --demo", "run --demo"),
    ("ranscribe", "transcribe"),
    ("ypefast", "typefast"),
    ("ools/bench_latency", "tools/bench_latency"),
    ("erify_usage", "verify_usage"),
]


def merge_wrapped_table(lines):
    out = []
    merges = 0
    for line in lines:
        if (out and line.startswith(TICK)
                and "|" in out[-1] and out[-1].endswith(" ")):
            out[-1] = out[-1] + line
            merges += 1
            continue
        out.append(line)
    return out, merges


def fix_damage(text: str) -> str:
    for bad, good in DAMAGE:
        text = re.sub("(?<![A-Za-z])" + re.escape(bad), good, text)
    return text


def strip_controls(body: str) -> str:
    for ch in (chr(9), chr(11), chr(12), chr(7)):
        body = body.replace(ch, "")
    return body


def collapse(text: str) -> str:
    return re.sub(TICK + "{2,}", TICK, text)


def wrap(text: str, phrase: str) -> str:
    out = []
    i = 0
    while True:
        j = text.find(phrase, i)
        if j < 0:
            out.append(text[i:])
            return "".join(out)
        before = text[j - 1] if j > 0 else ""
        after = text[j + len(phrase)] if j + len(phrase) < len(text) else ""
        inside_code = text[:j].count(TICK) % 2 == 1
        ok = ((before not in LETTERS) and (before != TICK) and (after not in LETTERS)
              and (after != ".") and (not inside_code))
        out.append(text[i:j])
        out.append(TICK + phrase + TICK if ok else phrase)
        i = j + len(phrase)


def fix_file(path: str):
    full = os.path.join(ROOT, path)
    if not os.path.exists(full):
        return (path, 0, 0, 0)
    original = open(full, encoding="utf-8").read()
    lines, merges = merge_wrapped_table(original.split(NL))
    fixed = []
    for line in lines:
        stripped = line.strip()
        if stripped == TICK:
            fixed.append(FENCE)
        elif re.fullmatch(TICK + "[A-Za-z]+" + TICK, stripped):
            fixed.append(FENCE + stripped[1:])
        else:
            fixed.append(line)
    out = []
    inside = False
    fences = 0
    changed = 0
    for line in fixed:
        if line.strip().startswith(FENCE):
            fences += 1
            inside = not inside
            out.append(line)
            continue
        if inside:
            out.append(line.replace(TICK, ""))
            continue
        body = line[:-1] if line.endswith(CR) else line
        tail = CR if line.endswith(CR) else ""
        new = collapse(strip_controls(fix_damage(body)).replace(TICK, ""))
        for phrase in PHRASES:
            new = wrap(new, phrase)
        if new != body:
            changed += 1
        out.append(new + tail)
    text = NL.join(out)
    if text != original:
        open(full, "w", encoding="utf-8").write(text)
    return (path, fences, changed, merges)


def main() -> int:
    for path in DOCS:
        name, fences, changed, merges = fix_file(path)
        print("%-24s 围栏 %2d  合并断行 %d  正文规整 %2d" % (name, fences, merges, changed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
