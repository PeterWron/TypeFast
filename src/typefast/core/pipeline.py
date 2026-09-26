"""识别 → 术语纠正 → 润色 → 拼装。

润色的三条硬约束：
1. 有硬截止时间（默认 1500ms），超时或失败一律回落 ASR 原文，绝不阻塞出字；
2. 短文本（不超过 short_text_chars 个有效字符）直接跳过模型润色（本地词库纠正照跑）；
3. polish.model 填 auto / auto-speed 时按候选链依次尝试，额度类失败自动熔断换下一个。"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Callable, List, Optional, Tuple

import numpy as np

from typefast.core.contracts import Timings
from typefast.core.router import ModelRouter
from typefast.core.vocabulary import Vocabulary
from typefast.providers.base import AsrProvider, AsrRequest, PolishProvider, PolishRequest
from typefast.settings import Config

SYSTEM_PROMPT = """你是语音输入法的润色引擎。把用户口述的原始识别文本整理成可直接发送的书面文字。
要求：
1. 去掉口头禅、重复词、无意义的语气词；
2. 补全标点，长句按语义分段；
3. 保持原意与语言，不解释、不回答、不加评论、不编造内容；
4. 只输出整理后的正文。"""

LANGUAGE_COMMANDS = [
    ("翻译成英文", "en"), ("用英文", "en"), ("用英语", "en"),
    ("翻译成日文", "ja"), ("用日文", "ja"),
    ("翻译成中文", "zh"), ("用中文", "zh"),
]

BILINGUAL_LANGUAGES = {"zh-en", "zh_en", "zh+en", "mixed", "auto"}


def bilingual_language(code: str) -> bool:
    """语言配置是否代表中英混说；auto（识别端自行判语种）也按混说处理。"""
    return (code or "").strip().lower() in BILINGUAL_LANGUAGES


# 送识别前的电平归一：本机实测 RMS −29 dBFS 的录音会被整句漏字，提到接近满量程后恢复正常。
TARGET_PEAK = 0.85   # 归一目标峰值
MIN_PEAK = 0.02      # 低于此峰值当作静音/噪声，不放大，避免把噪声抬起来诱发幻觉
MAX_GAIN = 4.0       # 最大增益，避免把底噪放得过大

CHAT_APPS = {"wechat.exe", "weixin.exe", "qq.exe", "telegram.exe", "dingtalk.exe", "lark.exe"}
PUNCT = "[\\s，。！？,.!?、；;：:~～…—\\-]+"


def meaningful_char_count(text: str) -> int:
    return len(re.sub(PUNCT, "", text or ""))


def detect_language_command(text: str) -> Tuple[str, str]:
    t = (text or "").strip()
    for phrase, code in LANGUAGE_COMMANDS:
        if t.startswith(phrase):
            rest = t[len(phrase):].lstrip("，。,:： ")
            if rest:
                return code, rest
    return "", t


class VoicePipeline:
    def __init__(self, asr: AsrProvider, polisher: PolishProvider, config: Config,
                 router: Optional[ModelRouter] = None, vocab: Optional[Vocabulary] = None,
                 executor: Optional[ThreadPoolExecutor] = None,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.asr = asr
        self.polisher = polisher
        self.config = config
        self.router = router or ModelRouter()
        self.vocab = vocab or Vocabulary().load()
        self.executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="typefast")
        self.log = log or (lambda msg: None)

    def _prepare_audio(self, samples):
        """按峰值做一次保守归一，只提升不衰减；静音与已经够响的录音原样返回。"""
        if not getattr(self.config.audio, "normalize", True):
            return samples
        x = np.asarray(samples, dtype=np.float32)
        if x.size == 0:
            return x
        peak = float(np.abs(x).max())
        if peak < MIN_PEAK or peak >= TARGET_PEAK:
            return x
        gain = min(MAX_GAIN, TARGET_PEAK / peak)
        if gain <= 1.02:
            return x
        self.log("audio normalize x%.2f (peak %.3f)" % (gain, peak))
        return np.clip(x * gain, -1.0, 1.0).astype(np.float32)


    def transcribe(self, samples, timings: Optional[Timings] = None) -> str:
        req = AsrRequest(samples=self._prepare_audio(samples), sample_rate=self.config.audio.sample_rate,
                         language=self.config.asr.language, hotwords=list(self.vocab.hotwords))
        started = time.perf_counter()
        result = self.asr.transcribe(req)
        if timings is not None:
            timings.asr_ms = (time.perf_counter() - started) * 1000
            timings.asr_encode_ms = float(getattr(result, "encode_ms", 0.0) or 0.0)
            timings.asr_ttfb_ms = float(getattr(result, "ttfb_ms", 0.0) or 0.0)
            timings.asr_download_ms = float(getattr(result, "download_ms", 0.0) or 0.0)
        return (result.text or "").strip()

    def candidate_models(self) -> List[str]:
        """polish.model 为 auto / auto-speed 时返回候选链，否则返回单元素列表。"""
        configured = (self.config.polish.model or "").strip()
        low = configured.lower()
        if low in ("auto", "auto-speed"):
            mode = "speed" if low == "auto-speed" else (self.config.router.mode or "quality")
            return list(self.router.candidates(mode))
        return [configured]

    def _attempt_polish(self, req: PolishRequest, deadline_ms: int) -> Tuple[str, bool, str]:
        future = self.executor.submit(self.polisher.polish, req)
        try:
            polished = future.result(timeout=max(0.05, deadline_ms / 1000.0))
        except FutureTimeout:
            self.log("polish deadline exceeded, fallback to raw")
            return "", False, "deadline"
        except Exception as exc:
            kind = getattr(exc, "kind", None)
            reason = getattr(kind, "value", None) or ("error:" + type(exc).__name__)
            self.log("polish failed: " + reason)
            return "", False, reason
        text = (polished or "").strip()
        if not text:
            return "", False, "empty"
        return text, True, "ok"

    def polish_text(self, raw: str, timings: Optional[Timings] = None) -> Tuple[str, bool, str]:
        cfg = self.config.polish
        if not cfg.enabled or meaningful_char_count(raw) <= cfg.short_text_chars:
            # 短句（或关掉润色）不走模型，但本地词库纠正要照跑：否则「交互眼 → 交互框」
            # 这类确定性替换在短句里永远不生效，而短句恰恰是最常用的一档（实测占投递 38%）。
            corrected, applied = self.vocab.apply_terms(raw)
            return corrected, False, ("terms:" + str(applied) if applied else "skip")
        lang, body = detect_language_command(raw)
        if not lang and bilingual_language(self.config.asr.language):
            lang = "mix"
        # 术语先纠正一遍再喂给模型（词库设计是「润色前后各跑一次」）：模型看到的就是正确的词，
        # 不会顺着错词改写，润色后的那一遍也才命中得了。
        body, pre_applied = self.vocab.apply_terms(body)
        models = self.candidate_models()
        started = time.perf_counter()
        last_reason = "error"
        for index, model in enumerate(models):
            req = PolishRequest(text=body, deadline_ms=cfg.deadline_ms,
                                hotwords=list(self.vocab.hotwords),
                                output_language=lang, model=model)
            text, ok, reason = self._attempt_polish(req, cfg.deadline_ms)
            last_reason = reason
            if ok:
                if timings is not None:
                    timings.polish_ms = (time.perf_counter() - started) * 1000
                corrected, applied = self.vocab.apply_terms(text)
                total = applied + pre_applied
                return corrected, True, ("terms:" + str(total) if total else "ok")
            if reason == "server_failed" and model:
                self.router.mark_exhausted(model)
                if index + 1 < len(models):
                    self.log("model " + model + " exhausted, trying next candidate")
                continue
            break
        if timings is not None:
            timings.polish_ms = (time.perf_counter() - started) * 1000
        # 润色失败回落原文时，用户自己的词库规则照样生效（确定性替换与模型可用性无关）
        fallback, _terms = self.vocab.apply_terms(raw)
        return fallback, False, last_reason

    def shape_for_delivery(self, text: str, app_name: str = "") -> str:
        out = (text or "").strip()
        if self.config.delivery.strip_chat_trailing_period and (app_name or "").lower() in CHAT_APPS:
            while out.endswith("。"):
                out = out[:-1]
        return out
