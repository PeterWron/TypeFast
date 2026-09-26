"""命令行入口：run / ui / simulate / doctor / config。

平台相关的 import 都放在函数内部，这样 doctor 与 simulate 在没有装 sounddevice、
没有麦克风的机器上也能跑，便于排查问题。"""

from __future__ import annotations

import argparse
import json
import sys
import time

from typefast import __version__
from typefast.core.pipeline import VoicePipeline
from typefast.core.router import ModelRouter
from typefast.core.vocabulary import Vocabulary
from typefast.settings import Config, data_dir, data_subdir, get_secret, set_secret


def build_asr(config: Config):
    name = (config.asr.provider or "mock").lower()
    if name in ("mock", ""):
        from typefast.providers.mock import MockAsrProvider
        return MockAsrProvider()
    if name.startswith("siliconflow"):
        from typefast.providers.siliconflow import SiliconFlowAsrProvider
        return SiliconFlowAsrProvider(model=config.asr.model or None,
                                      endpoint=config.asr.endpoint or None)
    if name.startswith("volcengine"):
        from typefast.providers.volcengine import VolcengineFlashAsrProvider
        return VolcengineFlashAsrProvider()
    if name.startswith("openai") or name.startswith("dashscope"):
        from typefast.providers.openai_compat import OpenAICompatAsrProvider
        return OpenAICompatAsrProvider(model=config.asr.model or "qwen3-asr-flash",
                                        endpoint=config.asr.endpoint or None)
    raise SystemExit("未知的 asr provider: " + name)


def build_polisher(config: Config):
    name = (config.polish.provider or "mock").lower()
    if name in ("mock", ""):
        from typefast.providers.mock import MockPolishProvider
        return MockPolishProvider()
    if name.startswith("openai") or name.startswith("dashscope") or name.startswith("ark"):
        from typefast.providers.openai_compat import OpenAICompatPolishProvider
        return OpenAICompatPolishProvider(model=config.polish.model or "qwen-flash",
                                                endpoint=config.polish.endpoint or None,
                                                temperature=config.polish.temperature,
                                                max_tokens=config.polish.max_tokens)
    raise SystemExit("未知的 polish provider: " + name)


def build_pipeline(config: Config, log) -> VoicePipeline:
    router = ModelRouter(quality=(config.router.quality_chain or None),
                        speed=(config.router.speed_chain or None))
    return VoicePipeline(asr=build_asr(config), polisher=build_polisher(config), config=config,
                         router=router, vocab=Vocabulary().load(), log=log)


def cmd_run(args) -> int:
    from typefast.core.contracts import SessionState
    from typefast.core.session import DictationSession
    from typefast.logging_setup import setup as setup_logging
    logger = setup_logging(verbose=getattr(args, "verbose", False))
    config = Config.load()
    pipeline = build_pipeline(config, logger.info)
    demo = bool(getattr(args, "demo", False))
    selftest = bool(getattr(args, "selftest", False))
    dry_run = bool(getattr(args, "no_deliver", False)) or selftest
    overlay = None
    if not getattr(args, "no_overlay", False):
        try:
            from typefast.platform.overlay import Overlay
            overlay = Overlay(hotkey_text=config.hotkey.hold, log=logger.info)
            overlay.start()
        except Exception as exc:
            logger.info("overlay disabled: " + repr(exc))
            overlay = None
    session = DictationSession(config=config, pipeline=pipeline, overlay=overlay,
                               log=logger.info, demo=demo, dry_run=dry_run)
    session.start(install_hooks=not selftest)
    if demo:
        logger.info("demo mode: 使用合成音频，不占用麦克风")
    if dry_run:
        logger.info("dry-run: 结果只打印，不注入到任何窗口")
    logger.info("TypeFast %s ready. 按住 %s 说话；按住时按 %s 取消；Ctrl+C 退出。",
                __version__, config.hotkey.hold, config.hotkey.cancel)
    if selftest:
        return run_selftest(session, logger, float(getattr(args, "seconds", 3.0)))
    try:
        session.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        session.stop()
        logger.info("stopped")
    return 0


def run_selftest(session, logger, seconds: float) -> int:
    """模拟一次 按下 → 说话 → 松开，打状态流转与最终文本；不装钩子、不注入。"""
    seen = []
    session.on_state = lambda state, text: seen.append((state.value, text))
    session._on_press()
    time.sleep(max(0.5, seconds))
    session._on_release()
    ok = session.wait_until_done(25.0)
    for state, text in seen:
        print("  %-10s %s" % (state, text))
    text = session.last_delivered_text
    print("selftest: %s" % ("OK" if ok else "TIMEOUT"))
    print("text: " + text)
    session.stop()
    return 0 if ok and text else 1


def cmd_ui(args) -> int:
    from typefast.ui.settings_window import main as ui_main
    return ui_main()


def cmd_simulate(args) -> int:
    import numpy as np
    from typefast.core.contracts import Timings, clean_join
    config = Config.load()
    config.asr.provider = "mock"
    config.polish.provider = "mock"
    pipeline = build_pipeline(config, print)
    rate = config.audio.sample_rate
    seconds = float(args.seconds)
    t = np.linspace(0.0, seconds, int(rate * seconds), endpoint=False)
    samples = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    timings = Timings()
    started = time.monotonic()
    raw = pipeline.transcribe(samples, timings)
    final, polished, reason = pipeline.polish_text(raw, timings)
    total = (time.monotonic() - started) * 1000
    print("asr   : " + raw)
    print("final : " + final)
    print("polish: %s (%s)" % (polished, reason))
    print("cost  : asr=%.0fms polish=%.0fms total=%.0fms" % (timings.asr_ms, timings.polish_ms, total))
    return 0


def cmd_doctor(args) -> int:
    from typefast.platform.audio import device_list, has_sounddevice
    config = Config.load()
    print("TypeFast " + __version__)
    print("python : " + sys.version.split()[0] + " (" + sys.executable + ")")
    print("config : " + str(Config.path()))
    print("data   : " + str(data_dir()))
    print("audio  : " + ("sounddevice ok" if has_sounddevice() else "sounddevice MISSING -> pip install sounddevice"))
    for line in device_list()[:8]:
        print("         " + line)
    for label, secret in (("ASR  siliconflow_key", "siliconflow_api_key"),
                         ("ASR  volc_app_key", "volc_app_key"),
                         ("ASR  asr_api_key", "asr_api_key"),
                         ("PLSH polish_api_key", "polish_api_key")):
        print("%-16s %s" % (label, "configured" if get_secret(secret) else ""))
    print("asr provider   : " + config.asr.provider + (" (" + config.asr.model + ")" if config.asr.model else ""))
    print("polish provider: " + config.polish.provider + " model=" + (config.polish.model or "(默认)") + " (deadline %dms, short<=%d)" % (config.polish.deadline_ms, config.polish.short_text_chars))
    print("hotkey         : hold " + config.hotkey.hold + ", cancel " + config.hotkey.cancel)
    return 0


def cmd_config(args) -> int:
    config = Config.load()
    print("path: " + str(Config.path()))
    print(json.dumps(config.to_dict(), ensure_ascii=False, indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="typefast", description="TypeFast - Windows AI 语音输入法（云优先）")
    parser.add_argument("--version", action="version", version="typefast " + __version__)
    sub = parser.add_subparsers(dest="command")
    p_run = sub.add_parser("run", help="开始监听：按住热键说话")
    p_run.add_argument("--no-overlay", action="store_true", help="不显示 HUD 浮窗")
    p_run.add_argument("--demo", action="store_true", help="演示模式：用合成音频代替麦克风")
    p_run.add_argument("--no-deliver", action="store_true", help="只打印结果，不注入到任何窗口")
    p_run.add_argument("--selftest", action="store_true", help="自动跑一次按下→松开，打印状态流转后退出")
    p_run.add_argument("--seconds", type=float, default=3.0, help="selftest 的模拟说话时长")
    p_run.add_argument("-v", "--verbose", action="store_true")
    p_start = sub.add_parser("start", help="一键启动：拉起后台服务并打开设置窗口")
    p_start.add_argument("--demo", action="store_true", help="强制演示模式")
    p_start.add_argument("--no-ui", action="store_true", help="只拉服务，不开界面")
    sub.add_parser("ui", help="打开图形操作界面（模型 / 密钥 / 按键 / 服务）")
    p_sim = sub.add_parser("simulate", help="不录音，用合成音频跑一遍完整流水线")
    p_sim.add_argument("--seconds", type=float, default=4.0)
    p_tr = sub.add_parser("transcribe", help="转写一个音频文件（mp3 / wav / m4a / flac），输出文字")
    p_tr.add_argument("path", help="音频文件路径")
    p_tr.add_argument("--no-polish", action="store_true", help="只输出识别原文，不润色")
    sub.add_parser("doctor", help="检查环境、设备、密钥与配置")
    sub.add_parser("config", help="打印配置路径与当前配置")
    p_key = sub.add_parser("key", help="管理 API Key（写入 Windows 凭据管理器）")
    p_key.add_argument("action", choices=["set", "status"])
    p_key.add_argument("name", nargs="?")
    p_key.add_argument("value", nargs="?")
    args = parser.parse_args(argv)
    if args.command in (None, "run"):
        return cmd_run(args)
    if args.command == "start":
        return cmd_start(args)
    if args.command == "ui":
        return cmd_ui(args)
    if args.command == "transcribe":
        return cmd_transcribe(args)
    if args.command == "simulate":
        return cmd_simulate(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "key":
        return cmd_key(args)
    if args.command == "config":
        return cmd_config(args)
    parser.print_help()
    return 2


def cmd_key(args) -> int:
    # 管理 API Key：set 写入 Windows 凭据管理器，status 列出配置情况
    # ###替换成你的api：typefast key set siliconflow_api_key "###替换成你的api"（润色：polish_api_key）
    names = ["siliconflow_api_key", "polish_api_key", "volc_app_key", "volc_access_key", "asr_api_key"]
    action = getattr(args, "action", "status")
    if action == "set":
        name = getattr(args, "name", None)
        value = getattr(args, "value", None)
        if not name or not value:
            print("用法：typefast key set <name> <value>")
            return 2
        ok = set_secret(name, value)
        print(("已写入 " + name) if ok else ("写入失败：" + name))
        return 0 if ok else 1
    for name in names:
        print("%-22s %s" % (name, "已配置" if get_secret(name) else "未配置"))
    return 0


def cmd_start(args) -> int:
    # 一键启动：没有可用输入设备时自动降级演示模式；服务已在跑就复用，然后打开设置窗口
    # 服务启停的实现与界面共用一份（typefast.ui.settings_window），避免两套逻辑跑偏
    from typefast.platform.audio import input_devices
    from typefast.ui.settings_window import service_pid, src_dir, start_service
    demo = bool(getattr(args, "demo", False))
    if not demo:
        names = " ".join(input_devices()).lower()
        if not any(k in names for k in ("wasapi", "mme", "directsound")):
            demo = True
            print("未发现可用的 WASAPI/MME 输入设备，自动使用演示模式（系统 TTS 合成语音）")
    pid = service_pid()
    if pid:
        print("后台服务已在运行（PID %d）" % pid)
    else:
        pid = start_service(demo=demo)
        if not pid:
            print("后台服务启动失败，请查看日志")
            return 1
        print("后台服务已启动（PID %d，%s）" % (pid, "演示模式" if demo else "真实麦克风"))
    if not getattr(args, "no_ui", False):
        import subprocess
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        try:
            subprocess.Popen([sys.executable, "-m", "typefast", "ui"], cwd=src_dir(),
                             creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("设置窗口已打开")
        except Exception as exc:
            print("打开界面失败：" + repr(exc))
    return 0


def plan_file_segments(samples, config, max_seconds: float = 25.0):
    # 用与实时链路同一套分段策略；尾巴过长再硬切，避免单次上传过大
    from typefast.core.segmenter import CommitPlanner
    seg = config.segment
    planner = CommitPlanner(sample_rate=config.audio.sample_rate, target_commit_s=seg.target_commit_s,
                             min_commit_s=seg.min_commit_s, live_margin_s=seg.live_margin_s,
                             search_radius_s=seg.search_radius_s, min_pause_s=seg.min_pause_s)
    ranges = []
    committed = 0
    while True:
        plan = planner.find_cut(samples, committed, -1)
        if plan is None or plan.cut_sample <= committed:
            break
        ranges.append((committed, int(plan.cut_sample)))
        committed = int(plan.cut_sample)
    rate = int(config.audio.sample_rate)
    max_chunk = int(max_seconds * rate)
    while samples.size - committed > max_chunk:
        ranges.append((committed, committed + max_chunk))
        committed += max_chunk
    if committed < samples.size:
        ranges.append((committed, int(samples.size)))
    return ranges or [(0, int(samples.size))]


def cmd_transcribe(args) -> int:
    # 把一个音频文件跑完整条流水线：解码 → 分段 → 识别 → 润色 → 存 last_transcript.txt
    from concurrent.futures import ThreadPoolExecutor
    from typefast.core.contracts import Timings, clean_join
    from typefast.platform.audiofile import decode_to_16k_mono
    config = Config.load()
    # 离线批量转写没有实时延迟压力，给润色更宽松的预算
    config.polish.deadline_ms = max(int(config.polish.deadline_ms), 8000)
    print("polish : %s %s (deadline %dms)" % (config.polish.provider, config.polish.model or "(默认)", config.polish.deadline_ms))
    samples = decode_to_16k_mono(args.path, config.audio.sample_rate)
    rate = float(config.audio.sample_rate)
    seconds = samples.size / rate if rate else 0.0
    print("file   : " + str(args.path))
    print("audio  : %.1fs (%d samples @ %.0fHz)" % (seconds, samples.size, rate))
    print("asr    : " + config.asr.provider + " " + (config.asr.model or "(默认)"))
    pipe = build_pipeline(config, lambda m: None)
    ranges = plan_file_segments(samples, config)
    print("chunks : %d" % len(ranges))
    timings = Timings()
    started = time.monotonic()
    texts = []
    if len(ranges) == 1:
        texts.append(pipe.transcribe(samples, timings))
    else:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(pipe.transcribe, samples[a:b], None) for a, b in ranges]
            texts = [f.result() for f in futures]
    asr_ms = (time.monotonic() - started) * 1000
    raw = clean_join(texts)
    if not raw:
        print("没有识别到内容")
        return 1
    print("asr ms : %.0f" % asr_ms)
    print("raw    : " + raw)
    final = raw
    if not getattr(args, "no_polish", False):
        final, ok, reason = pipe.polish_text(raw, timings)
        print("polish : %s (%s) %.0fms" % (ok, reason, timings.polish_ms))
    out = data_subdir("records") / "last_transcript.txt"
    try:
        out.write_text(final, encoding="utf-8")
    except Exception:
        pass
    print("final  : " + final)
    print("total  : %.0fms" % ((time.monotonic() - started) * 1000))
    print("saved  : " + str(out))
    return 0
