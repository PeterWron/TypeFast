"""音频采集：真实麦克风（sounddevice/WASAPI）+ 演示录音器（合成音频）。

- 懒加载 sounddevice：没装只影响真实录音，不影响 doctor / simulate / 单测；
- 自动挑输入设备：Windows 默认输入设备可能是 -1（未配置），必须自己挑，
  并按 WASAPI / DirectSound / MME 优先级选（WDM-KS 不支持阻塞 API）；
- 设备不支持 16k 时回落到设备默认采样率，并把 snapshot()/stop() 的输出统一重采样到 16k
  （否则分段规划、尾巴切片、送识别都会按错误的时间尺度计算：48k 数据当成 16k 用会用掉三倍时长）；
- 时基说明：本机 USB 麦克风报 48k，host 侧实测只交付约 36.2k 样本/秒（0.755 倍）。
  变速对照实测：按标称率解释时 ASR 才准确，按墙钟拉伸会让语音慢 1.33 倍、识别明显变差，
  所以实测交付率只作诊断记录，不用于换算；
- snapshot() 让录音中也能读到当前样本（流式分段要用）；
- on_level 回调把 RMS 电平交给浮窗做实时动画（约 25 次/秒）；
- 设备不出数据时的收尾：启动失败的流一定 close()；常开流连续「打开成功但一个回调都不来」时
  前几次立即重开，之后按 5→10→20→30 秒退避（设备整条掉线时不再每 3 秒戳一次设备）；
  按下时若常开流已超过 2 秒没有任何音频，先重开再打标记，重开失败就报错给用户。"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np

SAMPLE_RATE = 16000
HOST_PRIORITY = ("wasapi", "directsound", "mme")
LEVEL_INTERVAL_S = 0.04
STALE_AFTER_S = 2.0           # 常开流超过这么久没有任何回调，就当它「不出数据」
STALL_FAST_RETRIES = 3        # 前几次「不出数据」立即重开（覆盖本机约 20% 的整段空流）
REOPEN_BACKOFF_MIN_S = 5.0    # 之后按 5→10→20→30 秒退避，设备整条掉线时不再无脑刷
REOPEN_BACKOFF_MAX_S = 30.0


def has_sounddevice() -> bool:
    try:
        import sounddevice
    except Exception:
        return False
    return True


def rms_level(mono) -> float:
    x = np.asarray(mono, dtype=np.float32)
    if x.size == 0:
        return 0.0
    rms = float(np.sqrt((x * x).mean() + 1e-12))
    return float(min(1.0, rms * 8.0))


def write_wav(path, samples, sample_rate: int = SAMPLE_RATE) -> Optional[str]:
    """把 16k 单声道 float32 样本写成 16bit PCM wav（privacy.keep_audio 用）；失败返回 None。"""
    try:
        import wave
        x = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
        pcm = (x * 32767.0).astype(np.int16).tobytes()
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(target), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(int(sample_rate))
            wf.writeframes(pcm)
        return str(target)
    except Exception:
        return None

def _host_api_name(apis, dev) -> str:
    try:
        return str(apis[int(dev.get("hostapi", 0))].get("name", "?"))
    except Exception:
        return "?"


def input_devices() -> List[str]:
    try:
        import sounddevice as sd
    except Exception:
        return []
    out: List[str] = []
    try:
        apis = sd.query_hostapis()
        for i, dev in enumerate(sd.query_devices()):
            if int(dev.get("max_input_channels", 0)) < 1:
                continue
            rate = float(dev.get("default_samplerate") or 0)
            out.append("%d [%s] %s (%.0f Hz)" % (i, _host_api_name(apis, dev), dev.get("name", "?"), rate))
    except Exception:
        return out
    return out


def device_list() -> List[str]:
    return input_devices()


def pick_input_device(sd) -> Optional[int]:
    """挑一个可用输入设备：WASAPI 优先，其次 DirectSound / MME，最后任意。"""
    try:
        devices = sd.query_devices()
        apis = sd.query_hostapis()
    except Exception:
        return None
    best = None
    for idx, dev in enumerate(devices):
        if int(dev.get("max_input_channels", 0)) < 1:
            continue
        name = _host_api_name(apis, dev).lower()
        if "wdm-ks" in name:
            continue  # WDM-KS 在本机是「能打开但从不交付数据」的形态，比没有设备更糟
        rank = len(HOST_PRIORITY) + 1
        for i, key in enumerate(HOST_PRIORITY):
            if key in name:
                rank = i
                break
        if best is None or rank < best[0]:
            best = (rank, idx)
    return int(best[1]) if best else None


class Recorder:
    """真实麦克风录音器：start() 开始累积，snapshot()/stop() 一律返回 16k float32
    （设备原生率不是 16k 时线性重采样），cancel() 丢弃。

    keep_warm=True 时走常开流：服务启动就 open()，空闲期把缓冲裁到「预缓冲 + 0.5 秒」，
    按下只打标记（标记点含 preroll_ms 的预缓冲），不再重新打开设备；看门狗在流超过 2 秒
    没有任何数据时自动重开（本机实测约 20% 的打开会整段不出数据）。"""

    def __init__(self, sample_rate: int = SAMPLE_RATE, device: str = "",
                 log: Optional[Callable[[str], None]] = None,
                 on_level: Optional[Callable[[float], None]] = None,
                 keep_warm: bool = False, preroll_ms: int = 0) -> None:
        self.sample_rate = int(sample_rate)
        self.device = device or ""
        self.log = log or (lambda m: None)
        self.on_level = on_level
        self.keep_warm = bool(keep_warm)
        self.preroll_ms = max(0, int(preroll_ms))
        self._lock = threading.Lock()
        self._frames: List[np.ndarray] = []
        self._stream = None
        self._active_rate = float(self.sample_rate)
        self._recording = False
        self._last_level_at = 0.0
        self._snap_cache: Optional[np.ndarray] = None
        self._snap_cache_len = -1
        self._snap_cache_rate = 0.0
        self._record_started_at = 0.0
        self._first_cb_at: Optional[float] = None
        self._base = 0          # 缓冲区首个样本的绝对下标
        self._total = 0         # 已写入样本总数（绝对下标）
        self._mark: Optional[int] = None   # 本次说话的起点（含预缓冲）
        self._last_cb_at = 0.0
        self._reopen_count = 0
        self._last_reopen_try = 0.0
        self._reopen_backoff = 5.0
        self._stall_streak = 0      # 连续「打开成功但不出数据」的次数，用来决定是否退避
        self._last_stall_try = 0.0
        self._silent_since = 0.0     # 本次「设备不出数据」是什么时候开始的（收到音频即清零）
        self._press_total = 0
        self._stop_watchdog = threading.Event()
        self._watchdog: Optional[threading.Thread] = None

    def _resolve_device(self, sd):
        if self.device:
            return int(self.device) if self.device.isdigit() else self.device
        chosen = pick_input_device(sd)
        if chosen is None:
            raise RuntimeError("没有找到可用的输入设备。请到 Windows 设置 → 系统 → 声音 → 输入 选择麦克风，"
                               "并在 隐私和安全性 → 麦克风 里允许桌面应用访问。")
        try:
            name = sd.query_devices(chosen).get("name")
        except Exception:
            name = "?"
        self.log("auto-selected input device #%d (%s)" % (chosen, name))
        return chosen

    @staticmethod
    def _friendly(exc: Exception) -> str:
        text = str(exc)
        if ("Error querying device" in text) or ("-9992" in text) or ("Insufficient memory" in text):
            return ("找不到可用的录音设备：USB 麦克风可能被拔掉、断电或掉线了。"
                    "请重新插好麦克风（换一个 USB 口更稳），服务会自动重连。原始错误：" + text)
        if "Blocking API not supported" in text or "-9999" in text:
            return ("录音设备打不开：设备可能被占用、掉线或驱动卡死（WDM-KS 后端也可能不被 PortAudio 支持）。"
                    "可以拔插一次麦克风、在设备管理器里禁用再启用该设备，或重启 Windows 音频服务后重试。"
                    "原始错误：" + text)
        return text

    def _make_stream(self, sd, device):
        """建流：优先 16k，失败回落设备默认率；回调统一走 _on_audio。"""
        try:
            stream = sd.InputStream(device=device, channels=1, samplerate=self.sample_rate,
                                    dtype="float32", callback=self._on_audio, blocksize=0)
            self._active_rate = float(self.sample_rate)
            return stream
        except Exception as exc:
            self.log("16k open failed (%s), fallback to device default" % exc)
            info = sd.query_devices(device, "input")
            self._active_rate = float(info.get("default_samplerate") or 44100.0)
            return sd.InputStream(device=device, channels=1, samplerate=self._active_rate,
                                  dtype="float32", callback=self._on_audio, blocksize=0)

    def _portaudio_restart(self) -> None:
        """设备热插拔/休眠后 PortAudio 的设备表可能失效：重启一次再试。"""
        try:
            import sounddevice as sd
            sd._terminate()
            sd._initialize()
            self.log("portaudio restarted")
        except Exception as exc:
            self.log("portaudio restart failed: " + repr(exc))

    def _try_open(self, sd) -> bool:
        """打开并启动常开流；第一次失败就重启 PortAudio 再试一次。"""
        for attempt in (1, 2):
            stream = None
            try:
                device = self._resolve_device(sd)
                stream = self._make_stream(sd, device)
                stream.start()
                self._stream = stream
                self._last_cb_at = time.monotonic()
                return True
            except Exception as exc:
                self._stream = None
                if stream is not None:
                    # 启动失败的流也要关掉：sounddevice 的 Stream 没有 __del__，
                    # 不关就会留着一个占住音频设备的打开句柄（反复重开时会越积越多）
                    try:
                        stream.close()
                    except Exception:
                        pass
                self.log("open stream failed (attempt %d): %s" % (attempt, self._friendly(exc)))
                if attempt == 1:
                    self._portaudio_restart()
                    time.sleep(0.3)
        return False

    def open(self) -> None:
        """常开模式：启动时把流打开并持续缓冲，按下时只打标记、不再开关设备。"""
        self.keep_warm = True
        if self._stream is not None:
            return
        import sounddevice as sd
        if self._try_open(sd):
            self.log("warm stream opened: capture %.0f Hz -> %d Hz, preroll %d ms"
                     % (self._active_rate, self.sample_rate, self.preroll_ms))
        else:
            self.log("warm open failed; watchdog keeps retrying")
        self._last_reopen_try = time.monotonic()
        self._stop_watchdog.clear()
        if self._watchdog is None or not self._watchdog.is_alive():
            self._watchdog = threading.Thread(target=self._watchdog_loop, name="typefast-warm", daemon=True)
            self._watchdog.start()

    def close(self) -> None:
        self._stop_watchdog.set()
        self._close_stream()

    def _close_stream(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:
                self.log("stream close failed: " + repr(exc))
        with self._lock:
            self._frames = []
            self._base = 0
            self._total = 0
            self._mark = None
            self._snap_cache = None
            self._snap_cache_len = -1

    def stalled_for_s(self) -> float:
        """常开流已经多久没收到任何音频（秒）；没开常开流、或还没判定为静音时返回 0。

        会话用它区分「说了但没识别出来」和「麦克风根本没在交付音频」。
        从「静音开始时刻」算而不是从「流打开时刻」算：看门狗重开流会刷新后者，
        设备明明一个回调都没来也会看起来刚有声音。静音开始时刻只由真的收到音频来清零。"""
        if not self.keep_warm or not self._silent_since:
            return 0.0
        return max(0.0, time.monotonic() - self._silent_since)

    def _stall_wait_s(self) -> float:
        """常开流连续「打开成功但一个回调都不来」时，下一次重开前要等多久（秒）。

        前 STALL_FAST_RETRIES 次返回 0（立即重开，对应本机约 20% 的整段空流）；
        之后按 5→10→20→30 秒退避。设备真的恢复（收到音频）时 _stall_streak 归零。"""
        streak = int(self._stall_streak)
        if streak < STALL_FAST_RETRIES:
            return 0.0
        return min(REOPEN_BACKOFF_MAX_S,
                   REOPEN_BACKOFF_MIN_S * (2.0 ** (streak - STALL_FAST_RETRIES)))

    def _watchdog_loop(self) -> None:
        """看门狗：流长时间没有数据就重开（本机约 20% 的打开会整段不出数据）。"""
        while not self._stop_watchdog.wait(1.0):
            import sounddevice as sd
            if self._stream is None:
                if time.monotonic() - (self._last_reopen_try or 0.0) < self._reopen_backoff:
                    continue
                self._last_reopen_try = time.monotonic()
                self._reopen_count += 1
                self.log("warm stream missing, retry open (#%d)" % self._reopen_count)
                if self._try_open(sd):
                    self.log("warm stream reopened: capture %.0f Hz -> %d Hz"
                             % (self._active_rate, self.sample_rate))
                    self._reopen_backoff = 5.0
                else:
                    self._reopen_backoff = min(REOPEN_BACKOFF_MAX_S, self._reopen_backoff * 2.0)
                    self.log("no usable input device; next retry in %.0f s" % self._reopen_backoff)
                continue
            idle = time.monotonic() - (self._last_cb_at or 0.0)
            if idle <= STALE_AFTER_S:
                continue
            if not self._silent_since:
                self._silent_since = time.monotonic() - idle   # 本次静音从「最后一次真回调」算起
            # 前几次「没数据」立刻重开（本机实测约 20% 的打开会整段不出数据）；
            # 连续很多次还是没数据说明设备真掉线了：退避重试，别再每 3 秒戳设备一次。
            wait = self._stall_wait_s()
            now = time.monotonic()
            if wait and now - (self._last_stall_try or 0.0) < wait:
                continue
            self._stall_streak += 1
            self._last_stall_try = now
            self._reopen_count += 1
            if wait:
                self.log("warm stream stalled %.1fs (streak %d, backoff %.0fs); reopening (#%d)"
                         % (idle, self._stall_streak, wait, self._reopen_count))
            else:
                self.log("warm stream stalled %.1fs, reopening (#%d)" % (idle, self._reopen_count))
            self._close_stream()
            self._last_reopen_try = time.monotonic()
            if self._try_open(sd):
                self.log("warm stream reopened: capture %.0f Hz -> %d Hz"
                         % (self._active_rate, self.sample_rate))
                self._reopen_backoff = 5.0
            else:
                self._reopen_backoff = min(REOPEN_BACKOFF_MAX_S, self._reopen_backoff * 2.0)
                self.log("no usable input device; next retry in %.0f s" % self._reopen_backoff)
                self._stop_watchdog.wait(self._reopen_backoff)

    def _preroll_samples(self) -> int:
        return int(self.preroll_ms * (self._active_rate or self.sample_rate) / 1000.0)

    def _on_audio(self, indata, frames, time_info, status) -> None:
        now = time.monotonic()
        if self._first_cb_at is None:
            self._first_cb_at = now
        self._last_cb_at = now
        self._stall_streak = 0      # 真的收到音频了：退避计数清零
        self._silent_since = 0.0
        mono = np.asarray(indata, dtype=np.float32)
        if mono.ndim > 1:
            mono = mono.mean(axis=1)
        with self._lock:
            self._frames.append(mono.copy())
            self._total += int(mono.size)
            recording = self._recording
        if recording:
            self._push_level(mono)
        elif self.keep_warm:
            self._trim_idle()

    def _trim_idle(self) -> None:
        """空闲时只保留「预缓冲 + 0.5 秒」，避免常开流无限占内存。"""
        keep = self._preroll_samples() + int(0.5 * (self._active_rate or self.sample_rate))
        with self._lock:
            while self._frames and (self._total - self._base - int(self._frames[0].size)) >= keep:
                self._base += int(self._frames[0].size)
                self._frames.pop(0)
            self._snap_cache = None
            self._snap_cache_len = -1

    def _push_level(self, mono) -> None:
        if self.on_level is None:
            return
        now = time.monotonic()
        if now - self._last_level_at < LEVEL_INTERVAL_S:
            return
        self._last_level_at = now
        try:
            self.on_level(rms_level(mono))
        except Exception:
            pass

    def start(self) -> None:
        if self.keep_warm:
            idle = time.monotonic() - (self._last_cb_at or 0.0)
            if self._stream is not None and idle > STALE_AFTER_S:
                # 常开流已经不出数据（设备被拔掉 / 掉线 / 驱动卡死）：按下时先重开，
                # 否则这次说话必然录成 0 秒，用户只会看到「没听到内容」。
                self.log("warm stream has no audio for %.1fs, reopening before arming" % idle)
                self._close_stream()
            if self._stream is None:
                import sounddevice as sd
                if not self._try_open(sd):
                    raise RuntimeError("设备不交付音频（USB 麦克风可能被拔掉、断电、掉线或驱动卡死）。"
                                       "插好后服务会自动重连，也可以运行 tools/mic_check.py 自检。")
            with self._lock:
                self._mark = max(self._base, self._total - self._preroll_samples())
                self._snap_cache = None
                self._snap_cache_len = -1
                self._record_started_at = time.monotonic()
                self._first_cb_at = None
                self._recording = True
                self._press_total = self._total
                preroll = self._total - self._mark
            self.log("armed on warm stream (preroll %.0f ms)" % (1000.0 * preroll / max(1.0, self._active_rate)))
            return
        import sounddevice as sd
        device = self._resolve_device(sd)
        with self._lock:
            self._frames = []
            self._base = 0
            self._total = 0
            self._mark = 0
            self._snap_cache = None
            self._snap_cache_len = -1
            self._record_started_at = time.monotonic()
            self._first_cb_at = None
            self._recording = True
            self._press_total = self._total
        try:
            self._stream = self._make_stream(sd, device)
            self._stream.start()
        except Exception as exc:
            self._stream = None
            self._recording = False
            raise RuntimeError(self._friendly(exc)) from exc
        self.log("capture %.0f Hz -> %d Hz" % (self._active_rate, self.sample_rate))

    def _effective_rate(self, raw_size: int) -> float:
        """估算设备真实交付率：样本数 ÷ 墙钟。
        本机 USB 麦克风声明 48000 Hz，host 侧实际只交付约 36200 样本/秒。仅用于诊断与日志，不用于换算。
        数据不足 0.3 秒或估值离谱时回落到标称率，避免早期抖动。"""
        nominal = float(self._active_rate or self.sample_rate)
        t0 = self._first_cb_at or self._record_started_at
        if not t0 or raw_size < int(0.3 * nominal):
            return nominal
        elapsed = time.monotonic() - t0
        if elapsed <= 0.05:
            return nominal
        measured = raw_size / elapsed
        if measured < nominal * 0.5 or measured > nominal * 1.5:
            return nominal
        return measured

    def snapshot(self) -> np.ndarray:
        """录音中读取当前样本，一律返回 16k：按标称率换算（实测按墙钟拉伸会让语音变慢、识别变差）。
        常开模式下从本次标记点开始返回（含 preroll 预缓冲）。"""
        with self._lock:
            frames = list(self._frames)
            rate = float(self._active_rate)
            base = int(self._base)
            mark = self._mark
        if not frames or mark is None:
            return np.zeros(0, dtype=np.float32)
        raw = np.concatenate(frames).astype(np.float32)
        offset = max(0, int(mark) - base)
        if offset >= raw.size:
            return np.zeros(0, dtype=np.float32)
        if offset:
            raw = raw[offset:]
        if (self._snap_cache is not None and self._snap_cache_len == raw.size
                and abs(self._snap_cache_rate - rate) < 1e-6):
            return self._snap_cache
        out = self._resample(raw, rate, self.sample_rate)
        self._snap_cache = out
        self._snap_cache_len = raw.size
        self._snap_cache_rate = rate
        return out

    def stop(self) -> np.ndarray:
        stream = self._stream
        self._recording = False
        with self._lock:
            # 诊断用的交付率只算「按下之后」的样本，否则预缓冲会把数字抬高一截
            raw_size = max(0, int(self._total) - int(getattr(self, "_press_total", 0)))
        out = self.snapshot()
        eff = self._effective_rate(raw_size)
        if self._active_rate and abs(eff - self._active_rate) > 0.05 * self._active_rate:
            self.log("device delivery %.0f Hz vs nominal %.0f Hz (diagnostic; resampled at nominal)" % (eff, self._active_rate))
        with self._lock:
            self._mark = None
            self._snap_cache = None
            self._snap_cache_len = -1
        if self.keep_warm:
            self._trim_idle()
            if out.size == 0:
                self.log("no audio captured; reopening warm stream")
                self._close_stream()
                self.open()
            return out
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        with self._lock:
            self._frames = []
            self._base = 0
            self._total = 0
        return out

    def cancel(self) -> None:
        self._recording = False
        with self._lock:
            self._mark = None
            self._snap_cache = None
            self._snap_cache_len = -1
        if self.keep_warm:
            self._trim_idle()
            return
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        with self._lock:
            self._frames = []
            self._base = 0
            self._total = 0

    @staticmethod
    def _resample(x: np.ndarray, src_rate: float, dst_rate: int) -> np.ndarray:
        if x.size == 0 or abs(src_rate - float(dst_rate)) < 1.0:
            return x
        n = int(round(x.size * float(dst_rate) / float(src_rate)))
        if n <= 1:
            return x
        src_idx = np.linspace(0.0, float(x.size - 1), num=x.size)
        dst_idx = np.linspace(0.0, float(x.size - 1), num=n)
        return np.interp(dst_idx, src_idx, x).astype(np.float32)


class DemoRecorder:
    """演示录音器：不开麦克风，实时生成 像说话的合成音频。

用于没有可用麦克风的机器演示整条交互链路；也可以把它发到真实云端做连通性测试。
接口与 Recorder 完全一致。"""

    def __init__(self, sample_rate: int = SAMPLE_RATE, device: str = "",
                 log: Optional[Callable[[str], None]] = None,
                 on_level: Optional[Callable[[float], None]] = None,
                 keep_warm: bool = False, preroll_ms: int = 0,
                 speech_seconds: float = 3.2,
                 speech_path: Optional[str] = None) -> None:
        self.sample_rate = int(sample_rate)
        self.device = ""
        self.log = log or (lambda m: None)
        self.on_level = on_level
        self.speech_seconds = float(speech_seconds)
        self.keep_warm = bool(keep_warm)
        self.preroll_ms = int(preroll_ms)
        self._lock = threading.Lock()
        self._chunks: List[np.ndarray] = []
        self._rendered = 0.0
        self._started_at = 0.0
        self._recording = False
        self._stop_level = threading.Event()
        self._level_thread: Optional[threading.Thread] = None
        self._speech = _load_demo_speech(speech_path, self.sample_rate)

    def open(self) -> None:
        """演示录音器不需要常开流。"""
        return None

    def close(self) -> None:
        return None

    def start(self) -> None:
        with self._lock:
            self._chunks = []
            self._rendered = 0.0
        self._recording = True
        self._started_at = time.monotonic()
        self._stop_level.clear()
        self._level_thread = threading.Thread(target=self._level_loop, name="typefast-demo-level", daemon=True)
        self._level_thread.start()
        self.log("demo recorder started: synthetic audio, no microphone")

    @staticmethod
    def envelope(t: float) -> float:
        if t <= 0.0 or t > 3.6:
            return 0.0
        fade = min(1.0, t / 0.15)
        syllable = 0.5 + 0.5 * float(np.sin(2 * np.pi * 3.1 * t))
        return float(max(0.05, min(1.0, fade * (syllable ** 1.4) * 0.85)))

    def _level_loop(self) -> None:
        while not self._stop_level.is_set():
            if self.on_level is not None:
                try:
                    now_s = time.monotonic() - self._started_at
                    if self._speech is None:
                        self.on_level(self.envelope(now_s))
                    else:
                        self.on_level(_speech_level(self._speech, self.sample_rate, now_s))
                except Exception:
                    pass
            time.sleep(LEVEL_INTERVAL_S)

    def _render(self, seconds: float) -> np.ndarray:
        n = int(self.sample_rate * seconds)
        if n <= 0:
            return np.zeros(0, dtype=np.float32)
        t = (np.arange(n, dtype=np.float32) / float(self.sample_rate)) + self._rendered
        if self._speech is not None:
            start = int(self._rendered * self.sample_rate)
            self._rendered += float(n) / float(self.sample_rate)
            piece = self._speech[start:start + n]
            if piece.size < n:
                piece = np.concatenate([piece, np.zeros(n - piece.size, dtype=np.float32)])
            return piece.astype(np.float32)
        carrier = (0.60 * np.sin(2 * np.pi * 140 * t) + 0.25 * np.sin(2 * np.pi * 280 * t)
                   + 0.15 * np.sin(2 * np.pi * 560 * t))
        env = np.array([self.envelope(float(x)) for x in t], dtype=np.float32)
        noise = (0.01 * np.random.randn(n)).astype(np.float32)
        self._rendered += float(n) / float(self.sample_rate)
        return (0.4 * env * carrier + noise).astype(np.float32)

    def snapshot(self) -> np.ndarray:
        with self._lock:
            if self._recording:
                elapsed = time.monotonic() - self._started_at
                delta = elapsed - self._rendered
                if delta > 0.05:
                    self._chunks.append(self._render(delta))
            if not self._chunks:
                return np.zeros(0, dtype=np.float32)
            return np.concatenate(self._chunks).astype(np.float32)

    def stop(self) -> np.ndarray:
        samples = self.snapshot()
        self._recording = False
        self._stop_level.set()
        with self._lock:
            self._chunks = []
        self.log("demo recorder stopped: %.2fs synthetic audio" % (samples.size / float(self.sample_rate)))
        return samples

    def cancel(self) -> None:
        self._recording = False
        self._stop_level.set()
        with self._lock:
            self._chunks = []


def _load_demo_speech(speech_path, sample_rate: int):
    # 读演示语音（没有就现场用系统 TTS 合成）；不可用返回 None，回退合成音调
    try:
        from typefast.platform.tts import ensure_demo_speech
    except Exception:
        return None
    try:
        return ensure_demo_speech(path=speech_path, sample_rate=sample_rate)
    except Exception:
        return None


def _speech_level(samples, sample_rate: int, position_s: float, window_s: float = 0.08) -> float:
    # 取播放位置附近的真实电平，让 HUD 电平条反映实际语音
    if samples is None or samples.size == 0:
        return 0.0
    start = max(0, int(position_s * sample_rate))
    piece = samples[start:start + max(1, int(window_s * sample_rate))]
    return rms_level(piece)
