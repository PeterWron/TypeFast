"""词库三层：ASR 热词、术语后纠正、个人词库学习。

- 热词：喂给 ASR 做偏置（provider 支持时）
- 术语后纠正：确定性替换，润色前后各跑一次（专有名词识别错的兜底）
- 个人词库：用户改过一次的词，自动生成纠正规则并进热词"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from typefast.settings import data_subdir


@dataclass
class TermRule:
    wrong: str = ""
    right: str = ""
    whole_latin_word: bool = False


class Vocabulary:
    def __init__(self, hotwords: Optional[List[str]] = None, terms: Optional[List[TermRule]] = None,
                 path: Optional[Path] = None) -> None:
        self.hotwords: List[str] = list(hotwords or [])
        self.terms: List[TermRule] = list(terms or [])
        self.path = path or (data_subdir("state") / "vocabulary.json")

    def load(self) -> "Vocabulary":
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return self
        self.hotwords = [str(h) for h in raw.get("hotwords", []) if str(h).strip()]
        self.terms = [TermRule(**t) for t in raw.get("terms", []) if t.get("wrong")]
        return self

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"hotwords": self.hotwords, "terms": [asdict(t) for t in self.terms]}
            self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def add_hotword(self, word: str) -> None:
        word = (word or "").strip()
        if word and word not in self.hotwords:
            self.hotwords.append(word)
            self.save()

    def add_term(self, rule: TermRule) -> None:
        if not rule.wrong or not rule.right or rule.wrong == rule.right:
            return
        for existing in self.terms:
            if existing.wrong == rule.wrong:
                existing.right = rule.right
                self.save()
                return
        self.terms.append(rule)
        self.add_hotword(rule.right)
        self.save()

    def apply_terms(self, text: str) -> Tuple[str, int]:
        out = text or ""
        applied = 0
        for rule in self.terms:
            if not rule.wrong or not rule.right:
                continue
            if rule.whole_latin_word or rule.wrong.isascii():
                pattern = re.compile("(?<![0-9A-Za-z])" + re.escape(rule.wrong) + "(?![0-9A-Za-z])", re.IGNORECASE)
                replaced, n = pattern.subn(rule.right, out)
                if replaced == out:
                    n = 0      # 大小写不敏感：已经正确的词会被「替换成自己」，不能算命中
                out = replaced
            else:
                n = out.count(rule.wrong)
                if n:
                    out = out.replace(rule.wrong, rule.right)
            applied += n
        return out, applied

    def learn_from_edit(self, raw: str, edited: str) -> List[TermRule]:
        raw = raw or ""
        edited = edited or ""
        learned: List[TermRule] = []
        matcher = difflib.SequenceMatcher(a=raw, b=edited)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag != "replace":
                continue
            wrong = raw[i1:i2].strip()
            right = edited[j1:j2].strip()
            if not wrong or not right or wrong == right:
                continue
            if len(wrong) > 12 or len(right) > 12:
                continue
            if " " in wrong or " " in right:
                continue
            rule = TermRule(wrong=wrong, right=right, whole_latin_word=wrong.isascii())
            self.add_term(rule)
            learned.append(rule)
        return learned

    def prompt_block(self) -> str:
        if not self.hotwords:
            return ""
        return "用户常用词（优先保持原样）：" + "、".join(self.hotwords[:40])
