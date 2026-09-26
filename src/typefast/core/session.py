"""会话编排：热键 → 采集 → 流式分段 → 识别/润色 → 投递，并把过程实时喂给 HUD。

线程模型：
- 调用线程：run_forever() 阻塞；
- 钩子线程：只做回调（见 platform/hotkey.py），必须快；
- tick 线程：每 300ms 做两件事——① 可能的话提交新分段 ② 回收已完成的分段；
- 电平回调：音频回调里直接算 RMS（演示录音器用自己的线程），驱动 HUD 动画；
- 工作线程池：分段识别与润色（pipeline.executor）。

0.0.5 关键改动：分段从「同一时刻只允许一段在途」改成「最多 max_inflight 段并发」。
- 切段目标 5.0s、只在 0.30s 以上真停顿处切（实测保证与整段识别一致的质量）；
- 结果按音频顺序回收：只有队首完成才推进 committed_index，避免乱序丢音频；
- 分段失败：可原地重试的错误重试一次；仍失败把该段音频区间记下，松手时与尾巴一起重发（绝不丢）；
- 松手时仍有分段在途：把它之后的音频并回尾巴重发（保守，绝不丢）；
- 尾巴识别失败：已识别成功的分段照常投递，不因最后一次网络抖动丢掉整句。"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from typefast.core.contracts import ErrorKind, ProviderError, SessionState, Timings, Utterance
from typefast.core.pipeline import VoicePipeline
from typefast.core.segmenter import CommitPlanner
from typefast.platform.audio import DemoRecorder, Recorder
from typefast.platform.delivery import TextDelivery, foreground_app_exe
from typefast.platform.hotkey import PushToTalkHook
from typefast.settings import Config, data_subdir

TICK_S = 0.3


class DictationSession:
    def __init__(self, config: Config, pipeline: VoicePipeline, overlay=None,
                 log: Optional[Callable[[str], None]] = None,
                 on_state: Optional[Callable[[SessionState, str], None]] = None,
                 demo: bool = False, dry_run: bool = False) -> None:
        self.config = config
        self.pipeline = pipeline
        self.overlay = overlay
        self.log = log or (lambda m: None)
        self.on_state = on_state or (lambda s, t: None)
        self.demo = bool(demo)
        self.dry_run = bool(dry_run)
        seg = config.segment
        self.planner = CommitPlanner(sample_rate=config.audio.sample_rate,
                                     target_commit_s=seg.target_commit_s,
                                     min_commit_s=seg.min_commit_s,
                                     live_margin_s=seg.live_margin_s,
                                     search_radius_s=seg.search_radius_s,
                                     min_pause_s=seg.min_pause_s)
        self.max_inflight = max(1, int(getattr(seg, "max_inflight", 3) or 3))
        recorder_cls = DemoRecorder if self.demo else Recorder
        self.recorder = recorder_cls(sample_rate=config.audio.sample_rate,
                                     device=config.audio.input_device, log=self.log,
                                     on_level=self._on_level,
                                     keep_warm=bool(getattr(config.audio, "keep_warm", False)),
                                     preroll_ms=int(getattr(config.audio, "preroll_ms", 0) or 0))
        self.delivery = TextDelivery(paste_threshold_chars=config.delivery.paste_threshold_chars,
                                     restore_clipboard=config.delivery.restore_clipboard,
                                     log=self.log)
        self.hook = PushToTalkHook(hold_key=config.hotkey.hold,
                                   cancel_key=config.hotkey.cancel,
                                   mouse_middle=config.hotkey.mouse_middle,
                                   on_press=self._on_press,
                                   on_release=self._on_release,
                                   on_cancel=self._on_cancel,
                                   log=self.log)
        self._lock = threading.RLock()
        self._state = SessionState.IDLE
        self._utterance: Optional[Utterance] = None
        self._committed_index = 0
        self._last_empty_cut = -1
        self._pending: List[Dict] = []
        self._failed_ranges: List[Tuple[int, int]] = []  # 识别失败被丢弃的分段区间，松手时重发
        self._planning_index = 0
        self._stop = threading.Event()
        self._ticker: Optional[threading.Thread] = None
        self.last_delivered_text = ""

    def state(self) -> SessionState:
        return self._state

    def start(self, install_hooks: bool = True) -> bool:
        ok = self.hook.start() if install_hooks else False
        if getattr(self.config.audio, "keep_warm", False):
            try:
                self.recorder.open()
            except Exception as exc:
                self.log("warm open failed: " + repr(exc))
        self._ticker = threading.Thread(target=self._tick_loop, name="typefast-ticker", daemon=True)
        self._ticker.start()
        if install_hooks:
            self.log("listening; hold %s to talk (inflight<=%d)" % (self.config.hotkey.hold, self.max_inflight))
        self._emit(SessionState.IDLE, "")
        return ok

    def stop(self) -> None:
        self._stop.set()
        self.hook.stop()
        try:
            self.recorder.close()
        except Exception:
            pass
        try:
            self.recorder.cancel()
        except Exception:
            pass

    def run_forever(self) -> None:
        while not self._stop.is_set():
            time.sleep(0.2)

    def wait_until_done(self, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                idle = self._utterance is None
            if idle and self._state in (SessionState.DONE, SessionState.EMPTY, SessionState.ERROR):
                return True
            time.sleep(0.05)
        return False

    def _emit(self, state: SessionState, text: str = "") -> None:
        self._state = state
        try:
            self.on_state(state, text)
        except Exception:
            pass
        if self.overlay is not None:
            try:
                self.overlay.update(text, state.value)
            except Exception:
                pass

    def _on_level(self, level: float) -> None:
        if self.overlay is None:
            return
        try:
            self.overlay.set_level(level)
        except Exception:
            pass

    def _clear(self) -> None:
        with self._lock:
            self._utterance = None
            self._committed_index = 0
            self._last_empty_cut = -1
            self._pending = []
            self._failed_ranges = []
            self._planning_index = self._committed_index

    def _on_press(self) -> None:
        with self._lock:
            if self._utterance is not None:
                return
        try:
            self.recorder.start()
        except Exception as exc:
            self._emit(SessionState.ERROR, "麦克风打不开：" + str(exc))
            return
        with self._lock:
            self._utterance = Utterance()
            self._committed_index = 0
            self._last_empty_cut = -1
            self._pending = []
            self._failed_ranges = []
            self._planning_index = self._committed_index
        self._emit(SessionState.RECORDING, "正在听…")

    def _on_release(self) -> None:
        threading.Thread(target=self._finalize, name="typefast-finalize", daemon=True).start()

    def _on_cancel(self) -> None:
        try:
            self.recorder.cancel()
        except Exception:
            pass
        self._clear()
        self._emit(SessionState.IDLE, "")

    def _tick_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(TICK_S)
            try:
                self._collect_pending()
                self._maybe_commit()
            except Exception as exc:
                self.log("tick error: " + repr(exc))

    def _maybe_commit(self) -> None:
        # live_commit=False：不边录边切段，整段留给松手后的尾巴识别（实测短片段会整句丢内容）
        if not getattr(self.config.segment, "live_commit", True):
            return
        with self._lock:
            if self._utterance is None or len(self._pending) >= self.max_inflight:
                return
            # 规划基准用「已提交位置」而不是「已回收位置」：并发时后者要等识别完成才推进，
            # 否则同一段音频会在每个 tick 被重复提交（实测重复 3 次）。
            committed = self._planning_index
            last_empty = -1
        samples = self.recorder.snapshot()
        if samples.size == 0:
            return
        plan = self.planner.find_cut(samples, committed, last_empty)
        if plan is None or plan.cut_sample <= committed:
            return
        chunk = samples[committed:plan.cut_sample]
        if chunk.size == 0:
            return
        rate = float(self.config.audio.sample_rate)
        self.log("submit segment [%d..%d] %.1fs (inflight=%d)"
                 % (committed, plan.cut_sample, chunk.size / rate, len(self._pending) + 1))
        self._submit(committed, plan.cut_sample, chunk)

    def _submit(self, start: int, end: int, chunk) -> None:
        future = self.pipeline.executor.submit(self.pipeline.transcribe, chunk)
        record = {"future": future, "start": int(start), "end": int(end),
                  "chunk": chunk, "text": "", "handled": False, "retried": False}
        with self._lock:
            self._pending.append(record)
            self._planning_index = max(self._planning_index, int(end))

    def _note_failed(self, start: int, end: int) -> None:
        """记下识别失败的分段区间，松手后用完整音频重发（去重：同一段话只补发一次）。"""
        item = (int(start), int(end))
        with self._lock:
            if item not in self._failed_ranges:
                self._failed_ranges.append(item)

    def _collect_pending(self) -> None:
        with self._lock:
            records = list(self._pending)
        for rec in records:
            if rec["handled"] or not rec["future"].done():
                continue
            try:
                rec["text"] = (rec["future"].result() or "").strip()
                rec["handled"] = True
            except ProviderError as exc:
                if exc.kind == ErrorKind.NO_SPEECH:
                    rec["text"] = ""
                    rec["handled"] = True
                elif exc.kind.retriable_in_place() and not rec["retried"]:
                    rec["retried"] = True
                    self.log("segment [%d..%d] %s -> retry once" % (rec["start"], rec["end"], exc.kind.value))
                    rec["future"] = self.pipeline.executor.submit(self.pipeline.transcribe, rec["chunk"])
                else:
                    self.log("segment [%d..%d] dropped: %s -> will redo at release"
                             % (rec["start"], rec["end"], exc.kind.value))
                    rec["text"] = ""
                    rec["handled"] = True
                    self._note_failed(rec["start"], rec["end"])
            except Exception as exc:
                self.log("segment error: " + repr(exc) + " -> will redo at release")
                rec["text"] = ""
                rec["handled"] = True
                self._note_failed(rec["start"], rec["end"])
        partial = ""
        with self._lock:
            while self._pending and self._pending[0]["handled"]:
                rec = self._pending.pop(0)
                start = int(rec["start"])
                end = int(rec["end"])
                text = rec["text"]
                if self._utterance is not None:
                    if text:
                        seg = self._utterance.add_segment(start, end)
                        seg.text = text
                    else:
                        self.log("segment [%d..%d] empty" % (start, end))
                    self._committed_index = end
                    self._last_empty_cut = -1
                    partial = self._utterance.joined_raw()
        if partial:
            self._emit(SessionState.RECORDING, partial)

    def _drain_pending(self, deadline: float) -> None:
        while time.monotonic() < deadline:
            self._collect_pending()
            with self._lock:
                if not self._pending:
                    return
            time.sleep(0.05)

    def _finalize(self) -> None:
        with self._lock:
            utt = self._utterance
        if utt is None:
            return
        utt.released_at = time.monotonic()
        samples = self.recorder.stop()
        if self.config.privacy.keep_audio:
            from typefast.platform.audio import write_wav
            stamp = time.strftime("%Y%m%d-%H%M%S")
            kept = write_wav(data_subdir("audio") / ("utt-" + stamp + ".wav"), samples)
            if kept:
                self.log("audio kept: " + kept)
        self.log("recorded %.2fs" % (samples.size / float(self.config.audio.sample_rate or 16000)))
        self._emit(SessionState.FINALIZING, "识别中…")
        self._drain_pending(time.monotonic() + 6.0)
        with self._lock:
            if self._pending:
                first = min(int(rec["start"]) for rec in self._pending)
                self.log("%d segment(s) still running; merge their audio into the tail" % len(self._pending))
                self._committed_index = min(self._committed_index, first)
                self._pending = []
            redo = sorted(self._failed_ranges)
            self._failed_ranges = []
            self._planning_index = self._committed_index
        timings = Timings()
        rate = int(self.config.audio.sample_rate or 16000)
        # 录音中识别失败被丢弃的分段：松手后用完整音频逐段重发，避免那句话永久丢失。
        # 失败区间彼此不相交、且都在 committed_index 之前，重发不会与尾巴/成功分段重复。
        for s, e in redo:
            piece = samples[int(s):int(e)]
            if piece.size < int(0.15 * rate):
                continue
            try:
                text = (self.pipeline.transcribe(piece) or "").strip()
            except Exception as exc:
                self.log("redo segment [%d..%d] failed again: %s" % (int(s), int(e), repr(exc)))
                continue
            if text:
                seg = utt.add_segment(int(s), int(e))
                seg.text = text
        tail_start = min(self._committed_index, int(samples.size))
        tail = samples[tail_start:] if samples.size else samples
        tail_error = ""
        if tail.size > int(0.15 * rate):
            try:
                tail_text = self.pipeline.transcribe(tail, timings)
            except ProviderError as exc:
                if exc.kind == ErrorKind.NO_SPEECH:
                    tail_text = ""
                else:
                    # 尾巴失败不再丢整句：已识别成功的分段照常投递
                    tail_error = exc.message
                    tail_text = ""
                    self.log("tail transcribe failed (%s); falling back to committed segments" % exc.kind.value)
            except Exception as exc:
                tail_error = str(exc)
                tail_text = ""
                self.log("tail transcribe error: " + repr(exc))
            if tail_text:
                seg = utt.add_segment(tail_start, int(samples.size))
                seg.text = tail_text
        if utt.segments:
            utt.segments.sort(key=lambda s: s.start_sample)  # 补发的分段插在列表尾部，按音频顺序排回去
        raw = utt.joined_raw()
        if not raw:
            if tail_error:
                self._emit(SessionState.ERROR, tail_error)
            else:
                # 一个回调都没来 = 麦克风根本没在交付音频（拔了 / 断电 / 驱动卡死）。
                # 这时只说「没听到内容」用户永远不知道为什么按了没反应。
                stalled = float(getattr(self.recorder, "stalled_for_s", lambda: 0.0)() or 0.0)
                if stalled >= 2.0:
                    self._emit(SessionState.ERROR,
                               "麦克风没有数据（可能被拔掉、断电或驱动卡死），请检查设备后重试")
                else:
                    self._emit(SessionState.EMPTY, "没听到内容")
            self._clear()
            return
        app = foreground_app_exe()
        excluded = {a.lower() for a in self.config.privacy.exclude_apps}
        if app and app in excluded:
            self.log("foreground app excluded: " + app)
            self._emit(SessionState.EMPTY, "当前应用在排除列表")
            self._clear()
            return
        final, polished_ok, reason = self.pipeline.polish_text(raw, timings)
        utt.raw_text = raw
        utt.polish_applied = polished_ok
        utt.polish_reason = reason
        text = self.pipeline.shape_for_delivery(final, app)
        if self.dry_run:
            channel = "dry-run"
        else:
            self._emit(SessionState.DELIVERING, text)
            started = time.monotonic()
            try:
                channel = self.delivery.deliver(text)
            except Exception as exc:
                self._emit(SessionState.ERROR, "投递失败：" + str(exc))
                self._clear()
                return
            timings.delivery_ms = (time.monotonic() - started) * 1000
        timings.released_to_text_ms = (time.monotonic() - utt.released_at) * 1000
        utt.final_text = text
        utt.delivered = not self.dry_run
        self.last_delivered_text = text
        self.log("delivered via %s; polish=%s(%s); segments=%d; %s"
                 % (channel, polished_ok, reason, len(utt.segments), timings.as_row()))
        self.log("text: " + text)
        self._emit(SessionState.DONE, text)
        self._clear()
