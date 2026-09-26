"""交互链路单测：演示录音器、HUD 状态映射、配置往返、模型自动路由与熔断。

不联网、不碰麦克风、不弹窗。"""

from __future__ import annotations

import time

from typefast.core.contracts import ErrorKind, ProviderError, SessionState, Utterance
from typefast.core.session import DictationSession
from typefast.core.pipeline import VoicePipeline
from typefast.core.router import ModelRouter
from typefast.core.vocabulary import Vocabulary
from typefast.platform.audio import DemoRecorder, rms_level
from typefast.platform.overlay import STYLE
from typefast.providers.mock import MockAsrProvider
from typefast.settings import Config


def test_demo_recorder_produces_audio_and_levels():
    levels = []
    rec = DemoRecorder(on_level=levels.append)
    rec.start()
    time.sleep(0.5)
    mid = rec.snapshot()
    samples = rec.stop()
    assert samples.size >= int(0.4 * 16000)
    assert float(abs(samples).max()) > 0.01
    assert levels, "电平回调应当被触发"
    assert all(0.0 <= v <= 1.0 for v in levels)
    assert mid.size <= samples.size


def test_rms_level_scaling():
    import numpy as np
    assert rms_level([]) == 0.0
    assert rms_level(np.zeros(100, dtype=np.float32)) < 0.001
    assert rms_level(np.full(100, 0.5, dtype=np.float32)) == 1.0


def test_hud_style_covers_all_states():
    for state in SessionState:
        assert state.value in STYLE, "HUD 缺少状态样式: " + state.value


def test_config_round_trip_with_router_chains():
    raw = {
        "polish": {"model": "auto", "provider": "openai_compat"},
        "router": {"mode": "speed", "quality_chain": ["a", "b"], "speed_chain": ["c"]},
    }
    cfg = Config.from_dict(raw)
    assert cfg.polish.model == "auto"
    assert cfg.router.quality_chain == ["a", "b"]
    assert cfg.router.speed_chain == ["c"]
    again = Config.from_dict(cfg.to_dict())
    assert again.router.quality_chain == ["a", "b"]
    assert again.router.mode == "speed"


class _PickOne:
    """只对不在 fail_models 里的模型成功的假润色器，用来验证自动路由与熔断。"""
    name = "pick-one"

    def __init__(self, fail_models, ok_text: str) -> None:
        self.fail_models = set(fail_models)
        self.ok_text = ok_text
        self.calls = []

    def polish(self, req):
        self.calls.append(req.model)
        if req.model in self.fail_models:
            raise ProviderError(ErrorKind.SERVER_FAILED, "额度不足")
        return self.ok_text


def test_pipeline_auto_routing_switches_model(tmp_path):
    cfg = Config()
    cfg.polish.model = "auto"
    cfg.polish.short_text_chars = 0
    cfg.router.mode = "quality"
    router = ModelRouter(quality=["bad-model", "good-model"], speed=["good-model"],
                        state_path=tmp_path / "router.json")
    polisher = _PickOne({"bad-model"}, "整理好的文本")
    pipe = VoicePipeline(asr=MockAsrProvider(text="这是一段足够长的口述内容"),
                         polisher=polisher, config=cfg, router=router,
                         vocab=Vocabulary(path=tmp_path / "v.json"))
    final, ok, reason = pipe.polish_text("这是一段足够长的口述内容")
    assert ok is True
    assert final == "整理好的文本"
    assert polisher.calls[0] == "bad-model"
    assert router.is_exhausted("bad-model")


def test_pipeline_single_model_does_not_retry(tmp_path):
    cfg = Config()
    cfg.polish.model = "solo-model"
    cfg.polish.short_text_chars = 0
    router = ModelRouter(state_path=tmp_path / "router.json")
    polisher = _PickOne({"solo-model"}, "不该成功")
    pipe = VoicePipeline(asr=MockAsrProvider(text="这是一段足够长的口述内容"),
                         polisher=polisher, config=cfg, router=router,
                         vocab=Vocabulary(path=tmp_path / "v.json"))
    final, ok, reason = pipe.polish_text("这是一段足够长的口述内容")
    assert ok is False
    assert final == "这是一段足够长的口述内容"
    assert len(polisher.calls) == 1


class _ScriptedPipeline:
    # 按调用顺序返回预设结果（延迟 + 文本，或异常），用来验证并发提交与顺序回收
    def __init__(self, script) -> None:
        from concurrent.futures import ThreadPoolExecutor
        self.script = list(script)
        self.calls = 0
        self.executor = ThreadPoolExecutor(max_workers=4)

    def transcribe(self, samples, timings=None) -> str:
        idx = self.calls
        self.calls += 1
        item = self.script[idx] if idx < len(self.script) else (0.0, "多余调用")
        if isinstance(item, Exception):
            raise item
        delay, text = item
        if delay:
            time.sleep(delay)
        return text

    def polish_text(self, raw, timings=None):
        return raw, False, "skip"

    def shape_for_delivery(self, text, app_name=""):
        return (text or "").strip()


class _FakeRecorder:
    def __init__(self, samples) -> None:
        self.samples = samples

    def start(self) -> None:
        pass

    def snapshot(self):
        return self.samples

    def stop(self):
        return self.samples

    def cancel(self) -> None:
        pass


def _make_session(script, max_inflight: int = 3):
    cfg = Config()
    cfg.segment.max_inflight = max_inflight
    cfg.segment.min_commit_s = 0.5
    cfg.segment.live_margin_s = 0.2
    cfg.segment.search_radius_s = 2.0
    cfg.segment.min_pause_s = 0.25
    cfg.polish.enabled = False
    pipe = _ScriptedPipeline(script)
    session = DictationSession(config=cfg, pipeline=pipe, dry_run=True)
    return session, pipe


def test_pending_segments_drain_in_order():
    import numpy as np
    session, pipe = _make_session([(0.45, "第一段"), (0.05, "第二段")])
    session._utterance = Utterance()
    session._submit(0, 16000, np.zeros(16000, dtype=np.float32))
    session._submit(16000, 32000, np.zeros(16000, dtype=np.float32))
    time.sleep(0.2)
    session._collect_pending()
    assert session._committed_index == 0, "队首未完成时不得推进，否则会丢音频"
    assert pipe.calls == 2
    time.sleep(0.45)
    session._collect_pending()
    assert session._committed_index == 32000
    assert session._utterance.joined_raw() == "第一段，第二段。"
    session.stop()


def test_pending_segment_retries_once():
    import numpy as np
    busy = ProviderError(ErrorKind.SERVER_BUSY, "繁忙")
    session, pipe = _make_session([busy, (0.0, "重试成功")])
    session._utterance = Utterance()
    session._submit(0, 16000, np.zeros(16000, dtype=np.float32))
    time.sleep(0.1)
    session._collect_pending()
    time.sleep(0.2)  # 重试是提交到线程池的，等它真正跑起来再断言
    assert pipe.calls == 2, "可重试错误应原地重试一次"
    session._collect_pending()
    assert session._utterance.joined_raw() == "重试成功。"
    session.stop()


def test_max_inflight_blocks_new_submissions():
    import numpy as np
    session, pipe = _make_session([(0.3, "占位")], max_inflight=1)
    rate = 16000
    tone = np.full(int(5.0 * rate), 0.2, dtype=np.float32)
    samples = np.concatenate([tone, np.zeros(int(0.4 * rate), dtype=np.float32), tone])
    session.recorder = _FakeRecorder(samples)
    session._utterance = Utterance()
    session._submit(0, 1000, np.zeros(1000, dtype=np.float32))
    before = pipe.calls
    session._maybe_commit()
    assert pipe.calls == before, "在途已满时不应再提交"
    session._pending = []
    session._maybe_commit()
    assert pipe.calls == before + 1
    assert session._pending and session._pending[0]["end"] > 0
    session.stop()


def test_recorder_snapshot_returns_16k_for_48k_device():
    """回归：48k 设备的数据曾按 16k 记账，导致分段被截短、尾巴被整段丢掉。"""
    import numpy as np
    from typefast.platform.audio import Recorder

    rec = Recorder(sample_rate=16000)
    rec._active_rate = 48000.0  # 模拟只支持 48k 的 WASAPI 设备
    t = np.arange(48000 * 2, dtype=np.float32) / 48000.0
    rec._frames = [(0.3 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)]
    rec._mark = 0

    mid = rec.snapshot()
    assert abs(mid.size - 32000) <= 4, "48k 的 2 秒应当给出 16k 的 2 秒"
    assert rec.snapshot().size == mid.size, "重复调用应命中缓存且长度稳定"
    assert abs(rec.stop().size - 32000) <= 4, "stop() 不能对已重采样的数据再采一次"


def test_recorder_keeps_16k_device_untouched():
    import numpy as np
    from typefast.platform.audio import Recorder

    rec = Recorder(sample_rate=16000)
    rec._active_rate = 16000.0
    rec._frames = [np.full(16000, 0.1, dtype=np.float32)]
    rec._mark = 0
    assert rec.snapshot().size == 16000
    assert rec.stop().size == 16000


def test_recorder_uses_nominal_rate_even_if_delivery_is_slow():
    """回归：本机 USB 麦克风声明 48k，实际只交付约 36.2k 样本/秒。
    实测按标称率解释时 ASR 才准确（按墙钟拉伸会把语音拖慢 1.33 倍），所以输出按标称率换算。"""
    import numpy as np
    import time
    from typefast.platform.audio import Recorder

    rec = Recorder(sample_rate=16000)
    rec._active_rate = 48000.0
    rec._record_started_at = time.monotonic() - 3.0
    rec._first_cb_at = time.monotonic() - 3.0
    rec._frames = [np.full(108000, 0.05, dtype=np.float32)]  # 3 秒墙钟 × 36k 的实际交付量
    rec._mark = 0

    out = rec.stop()
    assert abs(out.size - 36000) <= 2000, "应按标称 48k 换算成约 2.25 秒 16k 样本，实际 %d" % out.size


def test_recorder_uses_nominal_rate_when_clock_is_honest():
    import numpy as np
    from typefast.platform.audio import Recorder

    rec = Recorder(sample_rate=16000)
    rec._active_rate = 16000.0
    rec._frames = [np.full(16000, 0.05, dtype=np.float32)]
    rec._mark = 0
    assert rec.stop().size == 16000, "设备说 16k 且交付 16k 时不校正、不重采样"


def test_write_wav_roundtrip(tmp_path):
    import numpy as np
    import wave
    from typefast.platform.audio import write_wav

    target = tmp_path / "kept.wav"
    saved = write_wav(target, np.zeros(16000, dtype=np.float32))
    assert saved and target.exists()
    with wave.open(str(target), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getframerate() == 16000
        assert wf.getnframes() == 16000


def test_live_commit_disabled_submits_nothing():
    """关闭边录边切段后，tick 不应提交任何分段（整段留给松手后的尾巴识别）。"""
    import numpy as np

    session, pipe = _make_session([(0.3, "占位")])
    session.config.segment.live_commit = False
    rate = 16000
    tone = np.full(int(6.0 * rate), 0.2, dtype=np.float32)
    samples = np.concatenate([tone, np.zeros(int(0.4 * rate), dtype=np.float32), tone])
    session.recorder = _FakeRecorder(samples)
    session._utterance = Utterance()
    before = pipe.calls
    session._maybe_commit()
    assert pipe.calls == before, "关闭后不应提交分段"
    assert not session._pending
    session.stop()


def test_warm_recorder_marks_preroll_and_bounds_buffer():
    """常开流：空闲缓冲被裁到预缓冲附近，按下时的标记点包含 preroll 音频。"""
    import numpy as np
    from typefast.platform.audio import Recorder

    rec = Recorder(sample_rate=16000, keep_warm=True, preroll_ms=300)
    rec._active_rate = 16000.0
    rec._stream = object()  # 假装流已经打开（离线单测不碰设备）
    for _ in range(10):      # 空闲 1 秒，每块 0.1 秒
        rec._on_audio(np.full(1600, 0.1, dtype=np.float32), 1600, None, None)
    kept = rec._total - rec._base
    assert kept <= 4800 + 8000 + 1600, "空闲缓冲应被裁到「预缓冲 + 0.5 秒」附近，实际 %d" % kept

    rec.start()  # 按下：只打标记
    preroll = rec._total - rec._mark
    assert 0 < preroll <= 4800 + 1600, "标记点应含约 300ms 预缓冲，实际 %d" % preroll

    rec._on_audio(np.full(1600, 0.2, dtype=np.float32), 1600, None, None)
    out = rec.stop()
    assert out.size == preroll + 1600, "输出应是「预缓冲 + 本次说话」，实际 %d" % out.size
    assert rec._mark is None, "停止后应回到空闲（标记清空）"


# ---------- 0.0.7 P0 回归：错误分类与「绝不丢音频」 ----------

def test_http_401_403_are_auth_errors():
    """401/403 是 Key 无效（AUTH），不是模型额度耗尽——后者才会触发路由熔断换候选。"""
    from typefast.providers.openai_compat import classify_http_error
    assert classify_http_error(401, "{}").kind == ErrorKind.AUTH
    assert classify_http_error(403, "{}").kind == ErrorKind.AUTH
    assert classify_http_error(500, "{}").kind == ErrorKind.SERVER_BUSY


class _AuthFail:
    name = "auth-fail"

    def __init__(self):
        self.calls = []

    def polish(self, req):
        self.calls.append(req.model)
        raise ProviderError(ErrorKind.AUTH, "Key 无效")


def test_auth_failure_does_not_trip_router(tmp_path):
    """Key 无效时应立即停下报错：不熔断模型、不拿同一个坏 Key 去试下一个候选。"""
    cfg = Config()
    cfg.polish.model = "auto"
    cfg.polish.short_text_chars = 0
    router = ModelRouter(quality=["m1", "m2"], speed=["m2"], state_path=tmp_path / "r.json")
    polisher = _AuthFail()
    pipe = VoicePipeline(asr=MockAsrProvider(text="这是一段足够长的口述内容"),
                         polisher=polisher, config=cfg, router=router,
                         vocab=Vocabulary(path=tmp_path / "v.json"))
    final, ok, reason = pipe.polish_text("这是一段足够长的口述内容")
    assert ok is False and reason == "auth"
    assert final == "这是一段足够长的口述内容"
    assert polisher.calls == ["m1"], "坏 Key 不该顺延到下一个候选模型"
    assert not router.is_exhausted("m1"), "坏 Key 不该触发额度熔断"


def test_tail_failure_falls_back_to_committed_segments():
    """尾巴识别失败（如断网）不再丢整句：已识别成功的分段照常投递。"""
    import numpy as np
    session, pipe = _make_session([(0.0, "已识别的部分"),
                                   ProviderError(ErrorKind.NETWORK, "断网")])
    session.recorder = _FakeRecorder(np.full(16000 * 2, 0.1, dtype=np.float32))
    session._utterance = Utterance()
    session._submit(0, 16000, np.zeros(16000, dtype=np.float32))
    time.sleep(0.2)
    session._collect_pending()
    assert session._committed_index == 16000
    session._finalize()
    assert session.last_delivered_text == "已识别的部分。", \
        "尾巴失败时应回落到已提交分段，实际: " + repr(session.last_delivered_text)
    session.stop()


def test_failed_segment_is_redone_at_finalize():
    """录音中识别失败的分段不再永久丢失：松手时用完整音频重发，且与尾巴按序拼接。"""
    import numpy as np
    boom = ProviderError(ErrorKind.SERVER_FAILED, "服务端炸了")
    session, pipe = _make_session([boom, (0.0, "补识别成功"), (0.0, "尾巴")])
    session.recorder = _FakeRecorder(np.full(16000 * 4, 0.1, dtype=np.float32))
    session._utterance = Utterance()
    session._submit(0, 16000, np.zeros(16000, dtype=np.float32))
    time.sleep(0.2)
    session._collect_pending()
    assert session._failed_ranges == [(0, 16000)], "失败分段的音频区间应当被记下"
    session._finalize()
    assert "补识别成功" in session.last_delivered_text
    assert "尾巴" in session.last_delivered_text
    assert session.last_delivered_text.index("补识别成功") < session.last_delivered_text.index("尾巴"), \
        "补发的分段应按音频顺序排在尾巴之前"
    session.stop()


# ---------- 0.0.8：麦克风不出数据时的收尾 ----------

def test_stall_backoff_grows_after_fast_retries():
    """常开流连续「打开成功但一个回调都不来」：前 3 次立即重开，之后按 5→10→20→30 秒退避。"""
    from typefast.platform.audio import Recorder
    rec = Recorder(keep_warm=True)
    waits = []
    for streak in range(0, 9):
        rec._stall_streak = streak
        waits.append(rec._stall_wait_s())
    assert waits[:3] == [0.0, 0.0, 0.0], "前 3 次应当立即重开，实际 %r" % (waits,)
    assert waits[3:8] == [5.0, 10.0, 20.0, 30.0, 30.0], "之后应按 5→10→20→30 退避，实际 %r" % (waits,)


def test_audio_callback_resets_stall_streak():
    """一旦真的收到音频，退避计数立刻清零（设备恢复后马上回到正常节奏）。"""
    import numpy as np
    from typefast.platform.audio import Recorder
    rec = Recorder(sample_rate=16000, keep_warm=True)
    rec._active_rate = 16000.0
    rec._stall_streak = 7
    rec._on_audio(np.zeros(1600, dtype=np.float32), 1600, None, None)
    assert rec._stall_streak == 0
    assert rec._last_cb_at > 0.0


def test_failed_stream_start_is_closed(monkeypatch):
    """回归：start() 失败的流必须 close()，否则会留下一个占着音频设备的打开句柄。"""
    from typefast.platform.audio import Recorder

    closed = []

    class _FakeStream:
        def start(self):
            raise RuntimeError("Error starting stream: Unanticipated host error [PaErrorCode -9999]")

        def close(self):
            closed.append(True)

    class _FakeSd:
        @staticmethod
        def query_hostapis():
            return [{"name": "Windows WASAPI"}]

        @staticmethod
        def query_devices(device=None, kind=None):
            info = {"name": "fake mic", "max_input_channels": 1,
                    "default_samplerate": 48000.0, "hostapi": 0}
            if device is None and kind is None:
                return [info]
            return info

        @staticmethod
        def InputStream(**kwargs):
            return _FakeStream()

    rec = Recorder(sample_rate=16000, keep_warm=True)
    monkeypatch.setattr(rec, "_portaudio_restart", lambda: None)
    assert rec._try_open(_FakeSd) is False
    assert closed, "启动失败的流必须被 close()，实际一个都没关"
    assert rec._stream is None


def test_press_reopens_stale_warm_stream(monkeypatch):
    """按下时若常开流已经不出数据，先重开再打标记（否则这次必然录到 0 秒）。"""
    from typefast.platform.audio import Recorder, STALE_AFTER_S

    rec = Recorder(sample_rate=16000, keep_warm=True, preroll_ms=300)
    rec._active_rate = 16000.0
    rec._stream = object()                                  # 假装常开流是开着的
    rec._last_cb_at = time.monotonic() - (STALE_AFTER_S + 3.0)
    calls = []

    def fake_close():
        calls.append("close")
        rec._stream = None
        rec._frames = []
        rec._base = 0
        rec._total = 0

    monkeypatch.setattr(rec, "_close_stream", fake_close)
    monkeypatch.setattr(rec, "_try_open", lambda sd: (calls.append("open"), True)[1])

    rec.start()
    assert calls == ["close", "open"], "应当先关掉僵死的流再重开，实际 %r" % (calls,)
    assert rec._recording is True
    assert rec._mark is not None
    rec.cancel()


def test_press_with_dead_mic_reports_error():
    """麦克风不出数据时按下必须报错给 HUD，不能静默录到 0 秒。"""

    class _DeadRecorder:
        def start(self):
            raise RuntimeError("设备不交付音频（USB 麦克风可能被拔掉、断电、掉线或驱动卡死）。")

        def snapshot(self):
            import numpy as np
            return np.zeros(0, dtype=np.float32)

        def stop(self):
            return self.snapshot()

        def cancel(self):
            pass

    session, _pipe = _make_session([])
    session.recorder = _DeadRecorder()
    seen = []
    session.on_state = lambda state, text: seen.append((state.value, text))
    session._on_press()
    assert seen, "按下应当产生状态回调"
    assert seen[-1][0] == "error", "麦克风没数据时应当报错，实际 %r" % (seen[-1],)
    assert "麦克风打不开" in seen[-1][1]
    session.stop()


def test_failed_segment_ranges_are_deduped():
    """同一段音频重复记失败时只补发一次，避免把同一句话投递两遍。"""
    session, _pipe = _make_session([])
    session._note_failed(0, 16000)
    session._note_failed(0, 16000)
    session._note_failed(16000, 32000)
    assert session._failed_ranges == [(0, 16000), (16000, 32000)]
    session.stop()


def test_empty_result_says_mic_has_no_data(monkeypatch):
    """麦克风整段没交付音频时，空结果必须说清楚是设备问题，而不是「没听到内容」。"""
    import numpy as np
    session, _pipe = _make_session([])
    session.recorder = _FakeRecorder(np.zeros(0, dtype=np.float32))
    monkeypatch.setattr(session.recorder, "stalled_for_s", lambda: 9.0, raising=False)
    session._utterance = Utterance()
    seen = []
    session.on_state = lambda state, text: seen.append((state.value, text))
    session._finalize()
    assert seen and seen[-1][0] == "error", "应当报设备无数据，实际 %r" % (seen[-1:],)
    assert "麦克风没有数据" in seen[-1][1]
    session.stop()


def test_empty_result_keeps_soft_message_for_healthy_mic(monkeypatch):
    """设备正常（一直在交付音频）但确实没识别出内容时，仍然是温和的「没听到内容」。"""
    import numpy as np
    session, _pipe = _make_session([])
    session.recorder = _FakeRecorder(np.zeros(0, dtype=np.float32))
    monkeypatch.setattr(session.recorder, "stalled_for_s", lambda: 0.1, raising=False)
    session._utterance = Utterance()
    seen = []
    session.on_state = lambda state, text: seen.append((state.value, text))
    session._finalize()
    assert seen and seen[-1][0] == "empty", "应当仍是 empty，实际 %r" % (seen[-1:],)
    assert seen[-1][1] == "没听到内容"
    session.stop()


def test_stalled_for_s_counts_from_silence_start(monkeypatch):
    """静音时长从「最后一次真回调」算起，且要跨越看门狗重开与 stop() 的重开。"""
    from typefast.platform.audio import Recorder

    rec = Recorder(sample_rate=16000, keep_warm=True, preroll_ms=300)
    rec._active_rate = 16000.0
    rec._stream = object()
    rec._mark = 0                                    # 已经按下过
    rec._silent_since = time.monotonic() - 6.0        # 看门狗已判定：6 秒没有音频
    rec._last_cb_at = time.monotonic() - 0.2          # 但流刚被重开过

    def fake_open():                                  # stop() 里的重开会刷新回调时间
        rec._stream = object()
        rec._last_cb_at = time.monotonic()
    monkeypatch.setattr(rec, "open", fake_open)

    assert rec.stalled_for_s() >= 5.0, "应当按静音起点算，实际 %.1fs" % rec.stalled_for_s()
    samples = rec.stop()
    assert samples.size == 0
    assert rec.stalled_for_s() >= 5.0, "重开流之后静音时长不能被清零，实际 %.1fs" % rec.stalled_for_s()


def test_audio_arrival_clears_silence(monkeypatch):
    """真收到音频后，静音起点清零（否则设备恢复了还会一直说没数据）。"""
    import numpy as np
    from typefast.platform.audio import Recorder

    rec = Recorder(sample_rate=16000, keep_warm=True)
    rec._active_rate = 16000.0
    rec._silent_since = time.monotonic() - 6.0
    rec._on_audio(np.zeros(1600, dtype=np.float32), 1600, None, None)
    assert rec._silent_since == 0.0
    assert rec.stalled_for_s() == 0.0
