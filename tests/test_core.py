"""核心逻辑离线单测：VAD / 分段 / 路由 / 词库 / 流水线。

不联网、不碰音频设备、不弹窗，可直接进 CI。"""

from __future__ import annotations

import time

import numpy as np

from typefast.core.contracts import ErrorKind, ProviderError
from typefast.core.pipeline import VoicePipeline, detect_language_command, meaningful_char_count
from typefast.core.router import ModelRouter
from typefast.core.segmenter import CommitPlanner
from typefast.core.vad import pause_candidates, speech_ratio
from typefast.core.vocabulary import TermRule, Vocabulary
from typefast.providers.mock import MockAsrProvider, MockPolishProvider
from typefast.settings import Config

RATE = 16000


def tone(seconds, freq=220.0, amp=0.3):
    t = np.linspace(0.0, seconds, int(RATE * seconds), endpoint=False)
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def silence(seconds):
    return np.zeros(int(RATE * seconds), dtype=np.float32)


def make_pipeline(config=None, asr=None, polisher=None, path=None):
    config = config or Config()
    asr = asr or MockAsrProvider(text="然后我们今天讨论一下那个新方案")
    polisher = polisher or MockPolishProvider()
    vocab = Vocabulary(path=path or (Config.path().parent / "test_vocab.json"))
    return VoicePipeline(asr=asr, polisher=polisher, config=config, vocab=vocab)


def test_vad_detects_speech_and_gap():
    samples = np.concatenate([tone(1.0), silence(0.5), tone(1.0)])
    assert speech_ratio(samples) > 0.4
    pauses = pause_candidates(samples)
    assert pauses, "应当找到中间的停顿"
    assert abs(pauses[0].center_sample - int(RATE * 1.25)) < int(RATE * 0.2)


def test_segmenter_cuts_near_pause():
    samples = np.concatenate([tone(5.0), silence(0.6), tone(5.0)])
    planner = CommitPlanner(sample_rate=RATE)
    plan = planner.find_cut(samples, 0)
    assert plan is not None
    assert abs(plan.cut_sample - int(RATE * 5.3)) < int(RATE * 0.8)


def test_segmenter_respects_min_commit():
    planner = CommitPlanner(sample_rate=RATE)
    assert planner.find_cut(tone(2.0), 0) is None


def test_segmenter_skips_last_empty_cut():
    samples = np.concatenate([tone(5.0), silence(0.6), tone(3.0)])
    planner = CommitPlanner(sample_rate=RATE)
    first = planner.find_cut(samples, 0)
    assert first is not None
    again = planner.find_cut(samples, 0, last_empty_cut=first.cut_sample)
    assert again is None or again.cut_sample > first.cut_sample


def test_router_exhaustion_and_fallback(tmp_path):
    router = ModelRouter(state_path=tmp_path / "router.json", quality=["a", "b"], speed=["b", "a"], cooldown_s=3600.0)
    assert router.candidates("quality") == ["a", "b"]
    router.mark_exhausted("a")
    assert router.candidates("quality") == ["b"]
    router.mark_exhausted("b")
    assert router.candidates("quality") == ["b"]


def test_vocabulary_terms_and_learning(tmp_path):
    vocab = Vocabulary(path=tmp_path / "v.json")
    vocab.add_term(TermRule(wrong="typefst", right="TypeFast", whole_latin_word=True))
    text, applied = vocab.apply_terms("我用 typefst 写代码，typefastx 不算")
    assert applied == 1
    assert "TypeFast" in text and "typefastx" in text
    learned = vocab.learn_from_edit("我用飞输开会", "我用飞书开会")
    assert learned and learned[0].right == "书"


def test_pipeline_skips_polish_for_short_text(tmp_path):
    config = Config()
    config.polish.short_text_chars = 10
    pipeline = make_pipeline(config, asr=MockAsrProvider(text="好的"), path=tmp_path / "v.json")
    raw = pipeline.transcribe(tone(1.0))
    final, polished, reason = pipeline.polish_text(raw)
    assert raw == "好的"
    assert polished is False and reason == "skip" and final == "好的"


def test_pipeline_polishes_long_text(tmp_path):
    config = Config()
    config.polish.short_text_chars = 3
    pipeline = make_pipeline(config, path=tmp_path / "v.json")
    raw = pipeline.transcribe(tone(2.0))
    final, polished, reason = pipeline.polish_text(raw)
    assert polished is True
    assert "然后" not in final
    assert final.endswith("。")


class _Sloth:
    name = "sloth"

    def polish(self, req):
        time.sleep(0.6)
        return "永远不该被采用"


def test_pipeline_polish_deadline_falls_back_to_raw(tmp_path):
    config = Config()
    config.polish.short_text_chars = 0
    config.polish.deadline_ms = 120
    pipeline = make_pipeline(config, polisher=_Sloth(), path=tmp_path / "v.json")
    raw = pipeline.transcribe(tone(2.0))
    started = time.monotonic()
    final, polished, reason = pipeline.polish_text(raw)
    elapsed = time.monotonic() - started
    assert polished is False and reason == "deadline"
    assert final == raw
    assert elapsed < 0.55


class _Boom:
    name = "boom"

    def transcribe(self, req):
        raise ProviderError(ErrorKind.NOT_CONFIGURED, "没有配置 Key")



def test_asr_error_propagates(tmp_path):
    pipeline = make_pipeline(asr=_Boom(), path=tmp_path / "v.json")
    try:
        pipeline.transcribe(tone(1.0))
    except ProviderError as exc:
        assert exc.kind == ErrorKind.NOT_CONFIGURED
    else:
        raise AssertionError("应当抛出 ProviderError")


def test_language_command_and_char_count():
    assert meaningful_char_count("你好，世界！") == 4
    assert detect_language_command("用英文 今天的会议取消了") == ("en", "今天的会议取消了")
    assert detect_language_command("普通一句话")[0] == ""


class _RecordingPolisher:
    name = "recorder"

    def __init__(self):
        self.requests = []

    def polish(self, req):
        self.requests.append(req)
        return req.text


def test_bilingual_config_sends_mix_hint(tmp_path):
    config = Config()
    config.asr.language = "zh-en"
    config.polish.short_text_chars = 0
    polisher = _RecordingPolisher()
    pipeline = make_pipeline(config, asr=MockAsrProvider(text="把 API Key 填进 config.json"),
                             polisher=polisher, path=tmp_path / "v.json")
    raw = pipeline.transcribe(tone(2.0))
    final, polished, reason = pipeline.polish_text(raw)
    assert polished is True
    assert len(polisher.requests) == 1
    assert polisher.requests[0].output_language == "mix"


def test_explicit_language_command_beats_bilingual_default(tmp_path):
    config = Config()
    config.asr.language = "zh-en"
    config.polish.short_text_chars = 0
    polisher = _RecordingPolisher()
    pipeline = make_pipeline(config, asr=MockAsrProvider(text="用英文 今天的会议取消了"),
                             polisher=polisher, path=tmp_path / "v.json")
    raw = pipeline.transcribe(tone(2.0))
    pipeline.polish_text(raw)
    assert polisher.requests[0].output_language == "en"
    assert polisher.requests[0].text == "今天的会议取消了"


def test_polish_prompt_carries_bilingual_rule(monkeypatch):
    from typefast.providers import openai_compat
    from typefast.providers.base import PolishRequest

    seen = {}

    def fake_post(url, headers, payload, timeout_s):
        seen["payload"] = payload
        return {"choices": [{"message": {"content": "把 API Key 填好。"}}]}

    monkeypatch.setattr(openai_compat, "_post_json", fake_post)
    provider = openai_compat.OpenAICompatPolishProvider(api_key="test-key")
    out = provider.polish(PolishRequest(text="把 API Key 填好", output_language="mix"))
    system = seen["payload"]["messages"][0]["content"]
    assert "中英混说" in system
    assert out == "把 API Key 填好。"


class _PeakAsr:
    name = "peak"

    def __init__(self):
        self.peaks = []

    def transcribe(self, req):
        import numpy as np
        from typefast.providers.base import AsrResult

        self.peaks.append(float(np.abs(np.asarray(req.samples)).max()))
        return AsrResult(text="没问题", provider=self.name)


def test_quiet_audio_is_normalized_before_asr(tmp_path):
    """回归：本机录音峰值只有 -8.8 dBFS / RMS -29 dBFS 时识别会整句漏字。"""
    import numpy as np

    asr = _PeakAsr()
    pipeline = make_pipeline(asr=asr, path=tmp_path / "v.json")
    pipeline.transcribe((0.30 * np.sin(np.linspace(0.0, 200.0, 16000))).astype(np.float32))
    assert asr.peaks[0] > 0.7, "低电平录音应当被提升到接近目标峰值"
    pipeline.transcribe((0.9 * np.sin(np.linspace(0.0, 200.0, 16000))).astype(np.float32))
    assert abs(asr.peaks[1] - 0.9) < 0.02, "已经够响的录音不应被改动"


def test_normalize_can_be_disabled(tmp_path):
    import numpy as np

    config = Config()
    config.audio.normalize = False
    asr = _PeakAsr()
    pipeline = make_pipeline(config, asr=asr, path=tmp_path / "v.json")
    pipeline.transcribe((0.05 * np.sin(np.linspace(0.0, 200.0, 16000))).astype(np.float32))
    assert abs(asr.peaks[0] - 0.05) < 0.005

# ---------- 词库：短句与前置纠正（2026-09-26 复查补） ----------

def test_short_text_still_applies_vocabulary_terms(tmp_path):
    """回归：短句跳过润色时，本地词库纠正也必须照跑（否则短句里的错词永远修不好）。"""
    config = Config()
    config.polish.short_text_chars = 10
    vocab = Vocabulary(path=tmp_path / "v.json")
    vocab.add_term(TermRule(wrong="交互眼", right="交互框"))
    pipeline = VoicePipeline(asr=MockAsrProvider(text="弹出一个交互眼。"),
                             polisher=MockPolishProvider(), config=config, vocab=vocab)
    final, polished, reason = pipeline.polish_text("弹出一个交互眼。")
    assert final == "弹出一个交互框。"
    assert polished is False, "短句仍然不调模型"
    assert reason == "terms:1"


def test_terms_are_applied_before_polish(tmp_path):
    """词库设计是「润色前后各跑一次」：模型必须看到已纠正的词，否则会顺着错词改写。"""
    seen = {}

    class _EchoPolish:
        name = "echo"

        def polish(self, req):
            seen["text"] = req.text
            return req.text

    config = Config()
    config.polish.short_text_chars = 0
    vocab = Vocabulary(path=tmp_path / "v.json")
    vocab.add_term(TermRule(wrong="codex", right="Codex"))
    pipeline = VoicePipeline(asr=MockAsrProvider(text="x"), polisher=_EchoPolish(),
                             config=config, vocab=vocab)
    final, ok, reason = pipeline.polish_text("这个交互框 codex 设计的很丑")
    assert ok is True
    assert seen["text"].startswith("这个交互框 Codex"), \
        "模型收到的应当是已纠正的文本，实际 %r" % seen["text"]
    assert final == "这个交互框 Codex 设计的很丑"
    assert reason == "terms:1", "前置纠正要计进命中数且不重复计数，实际 %r" % reason


def test_terms_survive_polish_failure(tmp_path):
    """润色失败回落原文时，用户自己的词库规则照样生效。"""

    class _BoomPolish:
        name = "boom"

        def polish(self, req):
            raise ProviderError(ErrorKind.SERVER_FAILED, "炸了")

    config = Config()
    config.polish.short_text_chars = 0
    config.polish.model = "solo-model"
    vocab = Vocabulary(path=tmp_path / "v.json")
    vocab.add_term(TermRule(wrong="交互眼", right="交互框"))
    router = ModelRouter(state_path=tmp_path / "router.json")
    pipeline = VoicePipeline(asr=MockAsrProvider(text="x"), polisher=_BoomPolish(),
                             config=config, vocab=vocab, router=router)
    final, ok, reason = pipeline.polish_text("弹出一个交互眼，你看看效果怎么样")
    assert ok is False
    assert final == "弹出一个交互框，你看看效果怎么样"
    assert reason == "server_failed"


def test_apply_terms_does_not_count_noop_matches(tmp_path):
    """已经正确的词被「替换成自己」时不该计入命中数（否则日志里的 terms:N 会虚高）。"""
    vocab = Vocabulary(path=tmp_path / "v.json")
    vocab.add_term(TermRule(wrong="codex", right="Codex"))
    text, applied = vocab.apply_terms("Codex 已经是对的")
    assert text == "Codex 已经是对的"
    assert applied == 0, "已经正确的词不该算命中，实际 %d" % applied
    text2, applied2 = vocab.apply_terms("codex 这里该改")
    assert text2 == "Codex 这里该改"
    assert applied2 == 1
