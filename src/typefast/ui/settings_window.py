"""TypeFast 图形操作界面：模型选择、API Key、按键、音频、投递与隐私、服务控制。

用 tkinter（标准库），不引入额外依赖。
两个注意点：
- 启动/停止服务用子进程，界面关了服务照常跑；
- 密钥只进 Windows 凭据管理器，界面里只显示 已配置/未配置，不回显明文。"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Dict, List, Optional

from typefast import __version__
from typefast.settings import Config, data_dir, get_secret, set_secret

KEY_FIELDS = [
    ("volc_app_key", "火山引擎 App Key"),
    ("volc_access_key", "火山引擎 Access Key"),
    ("asr_api_key", "兼容接口 识别 Key"),
    ("siliconflow_api_key", "硅基流动 API Key"),
    ("polish_api_key", "兼容接口 润色 Key"),
]
ASR_PROVIDERS = ["siliconflow", "mock", "volcengine_flash", "openai_compat"]
ASR_MODELS = ["XingChenAGI/XingChenASR-V3.2-Ultra", "FunAudioLLM/SenseVoiceSmall"]
POLISH_PROVIDERS = ["mock", "openai_compat"]
MODEL_PRESETS = ["auto", "auto-speed", "qwen-flash", "qwen-plus", "qwen3-max", "glm-4-flash", "gpt-4o-mini", "Qwen/Qwen2.5-7B-Instruct"]
LANGUAGES = ["zh-en", "zh", "en", "ja", "auto"]
ROUTER_MODES = ["quality", "speed"]
HOLD_KEYS = ["right_ctrl", "right_alt", "right_shift", "left_ctrl", "f8", "f9", "f10", "caps_lock", "space"]
CANCEL_KEYS = ["esc", "tab", "f10"]
SAMPLE_TEXT = "然后那个我们今天讨论一下吧，关于新版本的排期和人力安排"


def src_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def service_pid() -> Optional[int]:
    """找后台服务进程：只认 python -m typefast run 这种形态；早先按「命令行含 typefast + run + -m」"""
    """模糊匹配，会把恰好带这些字样的无关进程误判成服务。"""
    try:
        import psutil
    except Exception:
        return None
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = list(proc.info.get("cmdline") or [])
        except Exception:
            continue
        if len(cmd) >= 4 and cmd[1] == "-m" and cmd[2] == "typefast" and cmd[3] == "run":
            return int(proc.info["pid"])
        if len(cmd) >= 2 and cmd[0].lower().endswith("typefast.exe") and cmd[1] == "run":
            return int(proc.info["pid"])
    return None


def start_service(demo: bool = False) -> Optional[int]:
    args = [sys.executable, "-m", "typefast", "run"]
    if demo:
        args.append("--demo")
    flags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags = subprocess.CREATE_NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0)
    try:
        proc = subprocess.Popen(args, cwd=src_dir(), creationflags=flags,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return int(proc.pid)
    except Exception:
        return None


def stop_service(pid: int) -> bool:
    try:
        import psutil
        proc = psutil.Process(pid)
        proc.terminate()
        proc.wait(timeout=5)
        return True
    except Exception:
        return False


class SettingsWindow:
    def __init__(self) -> None:
        self.config = Config.load()
        self.root = tk.Tk()
        self.root.title("TypeFast " + __version__ + " · 语音输入设置")
        self.root.geometry("820x640")
        self.root.minsize(760, 580)
        self._apply_fonts()
        self._vars: Dict[str, tk.Variable] = {}
        self._badges: Dict[str, tk.Label] = {}
        self._status = tk.StringVar(value="服务状态：检查中…")
        self._test_result = tk.StringVar(value="未测试")
        self._build()
        self._refresh_keys()
        self._tick_status()

    def _apply_fonts(self) -> None:
        """设置窗口沿用 HUD 选出的那套优雅字体（思源黑体 / 微软雅黑 UI），失败就保持 Tk 默认。"""
        try:
            import tkinter.font as tkfont

            from typefast.platform.overlay import hud_font_family

            family = hud_font_family()
            for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                try:
                    tkfont.nametofont(name).configure(family=family)
                except Exception:
                    continue
        except Exception:
            pass

    def _entry(self, parent, row: int, label: str, key: str, value: str, width: int = 34) -> tk.StringVar:
        tk.Label(parent, text=label, anchor="w").grid(row=row, column=0, sticky="w", padx=8, pady=4)
        var = tk.StringVar(value=str(value))
        tk.Entry(parent, textvariable=var, width=width).grid(row=row, column=1, sticky="we", padx=8, pady=4)
        self._vars[key] = var
        return var

    def _combo(self, parent, row: int, label: str, key: str, value: str, values: List[str],
               width: int = 32, editable: bool = False) -> tk.StringVar:
        tk.Label(parent, text=label, anchor="w").grid(row=row, column=0, sticky="w", padx=8, pady=4)
        var = tk.StringVar(value=str(value))
        box = ttk.Combobox(parent, textvariable=var, values=values, width=width)
        if not editable:
            box.state(["readonly"])
        box.grid(row=row, column=1, sticky="we", padx=8, pady=4)
        self._vars[key] = var
        return var

    def _check(self, parent, row: int, label: str, key: str, value: bool) -> tk.BooleanVar:
        var = tk.BooleanVar(value=bool(value))
        tk.Checkbutton(parent, text=label, variable=var).grid(row=row, column=0, columnspan=2, sticky="w", padx=8, pady=4)
        self._vars[key] = var
        return var

    def _build(self) -> None:
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=12, pady=(12, 6))
        self._build_models(notebook)
        self._build_keys(notebook)
        self._build_input(notebook)
        self._build_delivery(notebook)
        self._build_about(notebook)
        bar = tk.Frame(self.root)
        bar.pack(fill="x", padx=12, pady=(0, 12))
        tk.Button(bar, text="保存配置", width=12, command=self._save).pack(side="left")
        tk.Button(bar, text="重新载入", width=12, command=self._reload).pack(side="left", padx=8)
        tk.Label(bar, textvariable=self._status, anchor="e").pack(side="right")

    def _build_models(self, nb) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="模型")
        tab.columnconfigure(1, weight=1)
        asr = ttk.LabelFrame(tab, text="语音转写")
        asr.grid(row=0, column=0, sticky="we", padx=8, pady=8)
        asr.columnconfigure(1, weight=1)
        self._combo(asr, 0, "转写服务", "asr.provider", self.config.asr.provider, ASR_PROVIDERS)
        self._combo(asr, 1, "模型名", "asr.model", self.config.asr.model, ASR_MODELS, editable=True)
        self._entry(asr, 2, "接口地址", "asr.endpoint", self.config.asr.endpoint)
        self._combo(asr, 3, "识别语言", "asr.language", self.config.asr.language, LANGUAGES)
        pol = ttk.LabelFrame(tab, text="文字润色")
        pol.grid(row=1, column=0, sticky="we", padx=8, pady=8)
        pol.columnconfigure(1, weight=1)
        self._combo(pol, 0, "润色服务", "polish.provider", self.config.polish.provider, POLISH_PROVIDERS)
        self._combo(pol, 1, "模型（auto=自动路由）", "polish.model", self.config.polish.model, MODEL_PRESETS, editable=True)
        self._entry(pol, 2, "接口地址", "polish.endpoint", self.config.polish.endpoint)
        self._entry(pol, 3, "润色截止（毫秒）", "polish.deadline_ms", self.config.polish.deadline_ms)
        self._entry(pol, 4, "短句跳过阈值（字）", "polish.short_text_chars", self.config.polish.short_text_chars)
        self._entry(pol, 5, "temperature", "polish.temperature", self.config.polish.temperature)
        self._entry(pol, 6, "max_tokens", "polish.max_tokens", self.config.polish.max_tokens)
        self._check(pol, 7, "启用润色", "polish.enabled", self.config.polish.enabled)
        chain = ttk.LabelFrame(tab, text="自动路由候选链（逗号分隔，留空用内置默认）")
        chain.grid(row=2, column=0, sticky="we", padx=8, pady=8)
        chain.columnconfigure(1, weight=1)
        self._combo(chain, 0, "路由模式", "router.mode", self.config.router.mode, ROUTER_MODES)
        self._entry(chain, 1, "质量优先链", "router.quality_chain", ",".join(self.config.router.quality_chain), 48)
        self._entry(chain, 2, "速度优先链", "router.speed_chain", ",".join(self.config.router.speed_chain), 48)
        test = ttk.LabelFrame(tab, text="连通性测试")
        test.grid(row=3, column=0, sticky="we", padx=8, pady=8)
        test.columnconfigure(1, weight=1)
        tk.Button(test, text="测试润色", width=14, command=lambda: self._test_polish()).grid(row=0, column=0, padx=8, pady=6)
        tk.Button(test, text="测试识别（合成音频）", width=22, command=lambda: self._test_asr(False)).grid(row=1, column=0, padx=8, pady=6)
        tk.Button(test, text="测试识别（麦克风 3 秒）", width=22, command=lambda: self._test_asr(True)).grid(row=2, column=0, padx=8, pady=6)
        tk.Label(test, textvariable=self._test_result, anchor="w", justify="left", wraplength=430).grid(row=0, column=1, rowspan=3, sticky="we", padx=8)

    def _build_keys(self, nb) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="API Key")
        tab.columnconfigure(0, weight=1)
        tk.Label(tab, text="###替换成你的api —— 把 Key 填到下面任意一栏即可（只在本地 Windows 凭据管理器保存，不写进配置文件，也不回显明文）。留空表示不修改，点「清除」删除。", anchor="w", justify="left", wraplength=740).grid(row=0, column=0, sticky="we", padx=8, pady=(10, 6))
        frame = ttk.LabelFrame(tab, text="凭据")
        frame.grid(row=1, column=0, sticky="we", padx=8, pady=6)
        frame.columnconfigure(1, weight=1)
        for i, (name, label) in enumerate(KEY_FIELDS):
            tk.Label(frame, text=label, anchor="w").grid(row=i, column=0, sticky="w", padx=8, pady=5)
            var = tk.StringVar(value="")
            tk.Entry(frame, textvariable=var, width=44, show="*").grid(row=i, column=1, sticky="we", padx=8, pady=5)
            self._vars["key." + name] = var
            badge = tk.Label(frame, text="未配置", width=8, fg="#b04040")
            badge.grid(row=i, column=2, padx=6)
            self._badges[name] = badge
            tk.Button(frame, text="清除", width=6, command=lambda n=name: self._clear_key(n)).grid(row=i, column=3, padx=6)
        tk.Label(tab, text="提示：火山引擎用 volc_app_key + volc_access_key；百炼、方舟等兼容接口用 asr_api_key 与 polish_api_key。", anchor="w", justify="left", wraplength=740).grid(row=2, column=0, sticky="we", padx=8, pady=10)

    def _build_input(self, nb) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="按键与音频")
        tab.columnconfigure(1, weight=1)
        hk = ttk.LabelFrame(tab, text="按住说话")
        hk.grid(row=0, column=0, columnspan=2, sticky="we", padx=8, pady=8)
        hk.columnconfigure(1, weight=1)
        self._combo(hk, 0, "按住键", "hotkey.hold", self.config.hotkey.hold, HOLD_KEYS)
        self._combo(hk, 1, "取消键", "hotkey.cancel", self.config.hotkey.cancel, CANCEL_KEYS)
        self._check(hk, 2, "鼠标中键也能按住说话", "hotkey.mouse_middle", self.config.hotkey.mouse_middle)
        au = ttk.LabelFrame(tab, text="音频")
        au.grid(row=1, column=0, columnspan=2, sticky="we", padx=8, pady=8)
        au.columnconfigure(1, weight=1)
        self._entry(au, 0, "输入设备（留空自动挑）", "audio.input_device", self.config.audio.input_device)
        self._entry(au, 1, "采样率", "audio.sample_rate", self.config.audio.sample_rate)
        tk.Button(au, text="列出可用设备", command=self._list_devices).grid(row=2, column=0, padx=8, pady=6)
        self._device_text = tk.StringVar(value="（点左侧按钮查看）")
        tk.Label(au, textvariable=self._device_text, anchor="w", justify="left", wraplength=560).grid(row=2, column=1, sticky="we", padx=8)
        self._check(au, 3, "常开麦克风流（消除偶发空流；麦克风会常驻占用）", "audio.keep_warm", self.config.audio.keep_warm)
        self._check(au, 4, "送识别前按峰值归一（低电平麦克风补音量）", "audio.normalize", getattr(self.config.audio, "normalize", True))
        self._entry(au, 5, "按下前预缓冲（毫秒）", "audio.preroll_ms", self.config.audio.preroll_ms)
        seg = ttk.LabelFrame(tab, text="切段与并发（延迟调优主旋钮）")
        seg.grid(row=2, column=0, columnspan=2, sticky="we", padx=8, pady=8)
        seg.columnconfigure(1, weight=1)
        self._entry(seg, 0, "切段目标（秒）", "segment.target_commit_s", self.config.segment.target_commit_s)
        self._entry(seg, 1, "并发上限（段）", "segment.max_inflight", getattr(self.config.segment, "max_inflight", 3))
        tk.Label(seg, text="切段越小延迟越低，但实测短于 5 秒的片段会被识别模型整句丢掉内容；关掉边录边切段可换取准确率。", anchor="w", justify="left", wraplength=640).grid(row=2, column=0, columnspan=2, sticky="we", padx=8, pady=(0, 6))
        self._check(seg, 3, "边录边切段（关掉 = 松手后整段识别，准确率优先）", "segment.live_commit", getattr(self.config.segment, "live_commit", True))

        svc = ttk.LabelFrame(tab, text="后台服务")
        svc.grid(row=3, column=0, columnspan=2, sticky="we", padx=8, pady=8)
        self._demo_var = tk.BooleanVar(value=False)
        tk.Checkbutton(svc, text="演示模式（不用麦克风，用合成音频演示交互）", variable=self._demo_var).grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=6)
        tk.Button(svc, text="启动服务", width=12, command=self._start).grid(row=1, column=0, padx=8, pady=6)
        tk.Button(svc, text="停止服务", width=12, command=self._stop).grid(row=1, column=1, padx=8, pady=6)
        tk.Button(svc, text="打开日志", width=12, command=self._open_logs).grid(row=1, column=2, padx=8, pady=6)

    def _build_delivery(self, nb) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="投递与隐私")
        tab.columnconfigure(1, weight=1)
        dl = ttk.LabelFrame(tab, text="文字投递")
        dl.grid(row=0, column=0, columnspan=2, sticky="we", padx=8, pady=8)
        dl.columnconfigure(1, weight=1)
        self._entry(dl, 0, "短文本直接键入的长度上限", "delivery.paste_threshold_chars", self.config.delivery.paste_threshold_chars)
        self._check(dl, 1, "用完后还原用户剪贴板", "delivery.restore_clipboard", self.config.delivery.restore_clipboard)
        self._check(dl, 2, "聊天类应用去掉句末句号", "delivery.strip_chat_trailing_period", self.config.delivery.strip_chat_trailing_period)
        pv = ttk.LabelFrame(tab, text="隐私")
        pv.grid(row=1, column=0, columnspan=2, sticky="we", padx=8, pady=8)
        pv.columnconfigure(1, weight=1)
        self._check(pv, 0, "保存录音音频", "privacy.keep_audio", self.config.privacy.keep_audio)
        tk.Label(pv, text="排除的应用（每行一个进程名，在这些应用里不录音不投递）", anchor="w", justify="left").grid(row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(6, 2))
        self._exclude_text = tk.Text(pv, height=6, width=52)
        self._exclude_text.grid(row=2, column=0, columnspan=2, sticky="we", padx=8, pady=6)
        self._exclude_text.insert("1.0", chr(10).join(self.config.privacy.exclude_apps))

    def _build_about(self, nb) -> None:
        tab = ttk.Frame(nb)
        nb.add(tab, text="关于")
        lines = [
            "TypeFast " + __version__ + " — Windows AI 语音输入法（云优先）",
            "",
            "配置文件：" + str(Config.path()),
            "数据目录：" + str(data_dir()),
            "",
            "按住热键说话，松开后文字自动整理并落到光标处。",
            "转写可用火山引擎极速版，或任意 OpenAI 兼容接口；润色模型填 auto 会按候选链自动选择，额度用尽自动熔断切换。",
            "",
            "详细说明见 README 与 docs/ARCHITECTURE.md。",
        ]
        tk.Label(tab, text=chr(10).join(lines), anchor="w", justify="left", wraplength=760).pack(padx=12, pady=14, anchor="w")

    def _refresh_keys(self) -> None:
        for name, _label in KEY_FIELDS:
            configured = bool(get_secret(name))
            badge = self._badges.get(name)
            if badge is not None:
                badge.config(text="已配置" if configured else "未配置", fg=("#2f7d32" if configured else "#b04040"))

    def _clear_key(self, name: str) -> None:
        try:
            import keyring
            keyring.delete_password("typefast", name)
        except Exception:
            pass
        self._refresh_keys()
        self._test_result.set("已清除 " + name)

    def _list_devices(self) -> None:
        from typefast.platform.audio import has_sounddevice, input_devices
        if not has_sounddevice():
            self._device_text.set("sounddevice 未安装：python -m pip install sounddevice")
            return
        items = input_devices()
        self._device_text.set(chr(10).join(items[:12]) if items else "没有枚举到输入设备")

    def _open_logs(self) -> None:
        path = str(data_dir() / "logs")
        try:
            os.startfile(path)
        except Exception:
            self._test_result.set("日志目录：" + path)

    def _tick_status(self) -> None:
        pid = service_pid()
        self._status.set(("服务状态：运行中（PID %d）" % pid) if pid else "服务状态：未运行")
        self.root.after(2000, self._tick_status)

    def _start(self) -> None:
        if service_pid():
            self._test_result.set("服务已在运行")
            return
        self._save(silent=True)
        pid = start_service(demo=self._demo_var.get())
        self._test_result.set(("已启动服务 PID %d" % pid) if pid else "启动失败，请看日志")
        self._tick_status()

    def _stop(self) -> None:
        pid = service_pid()
        if not pid:
            self._test_result.set("服务未在运行")
            return
        ok = stop_service(pid)
        self._test_result.set("服务已停止" if ok else "停止失败")
        self._tick_status()

    def _test_polish(self) -> None:
        self._test_result.set("测试中…")
        threading.Thread(target=self._run_polish_test, daemon=True).start()

    def _run_polish_test(self) -> None:
        try:
            from typefast.cli import build_polisher
            from typefast.providers.base import PolishRequest
            cfg = self._collect()
            polisher = build_polisher(cfg)
            started = time.monotonic()
            out = polisher.polish(PolishRequest(text=SAMPLE_TEXT, deadline_ms=cfg.polish.deadline_ms,
                                              model=cfg.polish.model))
            cost = (time.monotonic() - started) * 1000
            self._test_result.set("润色 %.0fms：" % cost + (out or "（空）"))
        except Exception as exc:
            self._test_result.set("润色失败：" + str(exc))

    def _test_asr(self, use_mic: bool) -> None:
        self._test_result.set("测试中…")
        threading.Thread(target=self._run_asr_test, args=(use_mic,), daemon=True).start()

    def _run_asr_test(self, use_mic: bool) -> None:
        try:
            from typefast.cli import build_asr
            from typefast.platform.audio import DemoRecorder, Recorder
            from typefast.providers.base import AsrRequest
            cfg = self._collect()
            if use_mic:
                rec = Recorder(sample_rate=cfg.audio.sample_rate, device=cfg.audio.input_device)
            else:
                rec = DemoRecorder(sample_rate=cfg.audio.sample_rate)
            rec.start()
            time.sleep(3.0 if use_mic else 2.5)
            samples = rec.stop()
            asr = build_asr(cfg)
            started = time.monotonic()
            res = asr.transcribe(AsrRequest(samples=samples, sample_rate=cfg.audio.sample_rate,
                                          language=cfg.asr.language))
            cost = (time.monotonic() - started) * 1000
            seconds = samples.size / float(cfg.audio.sample_rate)
            self._test_result.set("识别 %.0fms（%.1fs 音频）：" % (cost, seconds) + (res.text or "（空）"))
        except Exception as exc:
            self._test_result.set("识别失败：" + str(exc))

    def _collect(self) -> Config:
        cfg = Config.load()

        def text(key: str, default: str = "") -> str:
            var = self._vars.get(key)
            return str(var.get()).strip() if var is not None else default

        def num(key: str, default, cast):
            raw = text(key)
            try:
                return int(float(raw)) if cast is int else float(raw)
            except Exception:
                return default

        def flag(key: str, default: bool) -> bool:
            var = self._vars.get(key)
            return bool(var.get()) if var is not None else default

        cfg.asr.provider = text("asr.provider", cfg.asr.provider)
        cfg.asr.model = text("asr.model")
        cfg.asr.endpoint = text("asr.endpoint")
        cfg.asr.language = text("asr.language", cfg.asr.language)
        cfg.polish.provider = text("polish.provider", cfg.polish.provider)
        cfg.polish.model = text("polish.model")
        cfg.polish.endpoint = text("polish.endpoint")
        cfg.polish.deadline_ms = num("polish.deadline_ms", cfg.polish.deadline_ms, int)
        cfg.polish.short_text_chars = num("polish.short_text_chars", cfg.polish.short_text_chars, int)
        cfg.polish.temperature = num("polish.temperature", cfg.polish.temperature, float)
        cfg.polish.max_tokens = num("polish.max_tokens", cfg.polish.max_tokens, int)
        cfg.polish.enabled = flag("polish.enabled", cfg.polish.enabled)
        cfg.router.mode = text("router.mode", cfg.router.mode)
        cfg.router.quality_chain = [x.strip() for x in text("router.quality_chain").split(",") if x.strip()]
        cfg.router.speed_chain = [x.strip() for x in text("router.speed_chain").split(",") if x.strip()]
        cfg.hotkey.hold = text("hotkey.hold", cfg.hotkey.hold)
        cfg.hotkey.cancel = text("hotkey.cancel", cfg.hotkey.cancel)
        cfg.hotkey.mouse_middle = flag("hotkey.mouse_middle", cfg.hotkey.mouse_middle)
        cfg.audio.input_device = text("audio.input_device")
        cfg.audio.sample_rate = num("audio.sample_rate", cfg.audio.sample_rate, int)
        cfg.audio.keep_warm = flag("audio.keep_warm", cfg.audio.keep_warm)
        cfg.audio.normalize = flag("audio.normalize", getattr(cfg.audio, "normalize", True))
        cfg.audio.preroll_ms = num("audio.preroll_ms", cfg.audio.preroll_ms, int)
        cfg.segment.target_commit_s = num("segment.target_commit_s", cfg.segment.target_commit_s, float)
        cfg.segment.max_inflight = num("segment.max_inflight", getattr(cfg.segment, "max_inflight", 3), int)
        cfg.segment.live_commit = flag("segment.live_commit", getattr(cfg.segment, "live_commit", True))
        cfg.delivery.paste_threshold_chars = num("delivery.paste_threshold_chars", cfg.delivery.paste_threshold_chars, int)
        cfg.delivery.restore_clipboard = flag("delivery.restore_clipboard", cfg.delivery.restore_clipboard)
        cfg.delivery.strip_chat_trailing_period = flag("delivery.strip_chat_trailing_period", cfg.delivery.strip_chat_trailing_period)
        cfg.privacy.keep_audio = flag("privacy.keep_audio", cfg.privacy.keep_audio)
        cfg.privacy.exclude_apps = [x.strip() for x in self._exclude_text.get("1.0", "end").splitlines() if x.strip()]
        return cfg

    def _save(self, silent: bool = False) -> None:
        cfg = self._collect()
        path = cfg.save()
        saved = []
        for name, label in KEY_FIELDS:
            var = self._vars.get("key." + name)
            raw = str(var.get()) if var is not None else ""
            if raw.strip():
                if set_secret(name, raw.strip()):
                    saved.append(label)
                if var is not None:
                    var.set("")
        self.config = cfg
        self._refresh_keys()
        msg = "已保存配置：" + str(path)
        if saved:
            msg += "；已更新密钥：" + "、".join(saved)
        if not silent:
            self._test_result.set(msg)

    def _reload(self) -> None:
        self.config = Config.load()
        for key, var in list(self._vars.items()):
            if key.startswith("key."):
                continue
            value = self._lookup(key)
            if value is not None:
                var.set(value)
        self._exclude_text.delete("1.0", "end")
        self._exclude_text.insert("1.0", chr(10).join(self.config.privacy.exclude_apps))
        self._refresh_keys()
        self._test_result.set("已重新载入配置")

    def _lookup(self, key: str):
        node = self.config
        for part in key.split("."):
            node = getattr(node, part, None)
            if node is None:
                return None
        if isinstance(node, list):
            return ",".join(node)
        return node

    def run(self) -> int:
        self.root.mainloop()
        return 0


def main() -> int:
    return SettingsWindow().run()
