"""HUD 浮窗单测：动效曲线、逐字淡入、折行、逐帧渲染与分层窗口（不弹窗、不碰屏幕）。

渲染部分全部在内存里做（_Renderer 不依赖窗口），所以可以直接进 CI。
"""

from __future__ import annotations

import pytest

from typefast.core.contracts import SessionState
from typefast.platform import overlay as hud

pytestmark = pytest.mark.skipif(not hud.HAS_PIL, reason="HUD 需要 Pillow")


def test_style_and_glow_cover_all_states():
    """每个会话状态都必须有状态色/文案与光晕配色（HUD 才不会显示成默认待机）。"""
    for state in SessionState:
        assert state.value in hud.STYLE, "缺少状态样式: " + state.value
        assert hud.GLOW.get(state.value), "缺少光晕配色: " + state.value


def test_spring_curve_overshoots():
    """出现动效必须是带过冲的弹簧曲线，不能是线性或纯缓出。"""
    ease = hud.EASE_SPRING
    assert ease(0.0) == 0.0 and ease(1.0) == 1.0
    peak = max(ease(i / 100.0) for i in range(101))
    assert peak > 1.02, "弹簧曲线没有过冲，峰值 %.3f" % peak
    assert ease(0.15) < 0.6, "起步应当先慢（蓄力）"
    assert ease(0.6) > 1.05, "中段应当冲过 1.0 再回落"


def test_disappear_curve_is_monotone():
    ease = hud.EASE_OUT
    values = [ease(i / 50.0) for i in range(51)]
    assert values[0] == 0.0 and abs(values[-1] - 1.0) < 1e-9
    assert all(b >= a - 1e-9 for a, b in zip(values, values[1:]))


def test_char_fade_reuses_prefix():
    """逐字淡入：已经出现的字不能重播，新字从 0 淡到 255。"""
    overlay = hud.Overlay()
    overlay._resync_chars("你好", 1.0)
    births = [ch[1] for ch in overlay._chars]
    overlay._resync_chars("你好呀", 2.0)
    assert [ch[1] for ch in overlay._chars][:2] == births
    assert overlay._chars[2][1] == 2.0
    alphas = dict(overlay._alphas(2.0))
    assert alphas["好"] == 255 and alphas["呀"] == 0
    half = dict(overlay._alphas(2.0 + hud.CHAR_FADE_MS / 2000.0))
    assert 0 < half["呀"] < 255, "新字应当正在淡入"
    assert dict(overlay._alphas(4.0))["呀"] == 255


def test_wrap_keeps_newest_text_and_marks_truncation():
    """正文超出两行时保留最新的尾巴，并在最前面加省略号（由 truncated 标记）。"""
    renderer = hud._Renderer()
    text = "一二三四五六七八九十" * 6
    chars = [(ch, 255) for ch in text]
    width = hud.PANEL_W - (hud.LEFT_W + hud.PAD_X) - hud.PAD_X
    lines, truncated = renderer._wrap_tail(chars, 16, width, hud.TEXT_MAX_LINES)
    assert len(lines) <= hud.TEXT_MAX_LINES
    assert truncated, "超长文本应当标记为被截断"
    assert sum(len(line) for line in lines) < len(chars)
    assert lines[-1][-1] == len(chars) - 1, "必须保留最新的字符"


def test_frame_renders_glass_pill_with_shadow():
    renderer = hud._Renderer()
    chars = [(ch, 255) for ch in "正在把语音整理成文字"]
    bars = [0.3, 0.6, 0.9, 0.6, 0.4, 0.7, 0.45]
    image = renderer.frame(state="recording", label="聆听中", hint="按住 Right Ctrl 说话",
                           chars=chars, level=0.6, bars=bars, now=1.0)
    assert image.size == (renderer.win_w, renderer.win_h)
    assert image.mode == "RGBA"
    center = image.getpixel((renderer.pad + renderer.panel_w // 2, renderer.pad + renderer.panel_h // 2))
    assert center[3] >= 200, "胶囊中心必须是可见的玻璃面板，alpha=%.0f" % center[3]
    assert image.getpixel((0, 0))[3] < 40, "胶囊四角之外不该有实心内容"
    # 阴影：胶囊正下方（胶囊外）应当有淡淡的暗色
    below = image.getpixel((renderer.pad + renderer.panel_w // 2, renderer.pad + renderer.panel_h + 10))
    assert below[3] > 0, "下方应当有环境光阴影"


def test_glow_follows_state_palette():
    """光晕配色要跟着状态走：已输入=绿、出错=红。"""
    renderer = hud._Renderer()
    bars = [0.5] * hud.BARS

    def glow_mean(state: str):
        image = renderer.frame(state=state, label="x", hint="h", chars=[], level=0.75, bars=bars, now=2.0)
        region = image.crop((renderer.pad, renderer.pad,
                             renderer.pad + hud.LEFT_W, renderer.pad + renderer.panel_h))
        return region.convert("RGB").resize((1, 1)).getpixel((0, 0))

    done = glow_mean("done")
    error = glow_mean("error")
    assert done[1] > done[0], "已输入应当偏绿，实际 %r" % (done,)
    assert error[0] > error[1], "出错应当偏红，实际 %r" % (error,)


def test_premultiplied_bgra_buffer_for_layered_window():
    """UpdateLayeredWindow 需要预乘 BGRA；这里锁住转换契约（长度与预乘规则）。"""
    renderer = hud._Renderer()
    image = renderer.frame(state="done", label="已输入", hint="h", chars=[], level=0.2,
                           bars=[0.4] * hud.BARS, now=1.0)
    data = image.convert("RGBa").tobytes("raw", "BGRa")
    assert len(data) == image.width * image.height * 4
    px = image.getpixel((renderer.pad + 5, renderer.pad + 5))
    idx = ((renderer.pad + 5) * image.width + (renderer.pad + 5)) * 4
    assert data[idx + 3] == px[3]
    assert data[idx + 2] == int(px[0] * px[3] / 255) or px[3] == 0


def test_layered_window_blit_smoke():
    """真窗口冒烟：建一个不可见的分层窗口并贴一帧（失败则跳过，不阻塞 CI）。"""
    renderer = hud._Renderer()
    try:
        window = hud._LayeredWindow()
        window.create(renderer.win_w, renderer.win_h)
    except Exception as exc:  # pragma: no cover - 无 GUI 会话时跳过
        pytest.skip("分层窗口不可用: %r" % (exc,))
    try:
        image = renderer.frame(state="recording", label="聆听中", hint="h", chars=[],
                               level=0.5, bars=[0.5] * hud.BARS, now=1.0)
        window.blit(image.convert("RGBa").tobytes("raw", "BGRa"),
                    image.width, image.height, 0, 0)
        window.pump()
    finally:
        window.destroy()

def test_dismissed_latch_blocks_reentry():
    """回归（0.0.9 首版 bug）：终态自动收起后不能又弹回来，否则会一直弹入弹出。"""
    import time as _time

    overlay = hud.Overlay()
    overlay.update("整理好的文本", "done")
    overlay._drain(_time.monotonic())
    assert overlay._should_show() is True, "新一轮输入应当弹出"
    overlay._dismissed_seq = overlay._update_seq     # 相当于收起动画刚播完
    assert overlay._should_show() is False, "收起之后不该再自动弹出"
    overlay.update("下一句新的内容", "done")           # 有新内容 = 新一轮
    overlay._drain(_time.monotonic())
    assert overlay._should_show() is True, "有新内容应当允许再次弹出"
    overlay._dismissed_seq = overlay._update_seq
    overlay.update("下一句新的内容", "done")           # 状态和文本都没变
    overlay._drain(_time.monotonic())
    assert overlay._should_show() is False, "没有新内容时不该弹出"


def test_new_utterance_during_out_animation_reopens():
    """收起动画还没播完就来了新一轮（用户又按下说话），收起结束后必须能再弹出来。"""
    import time as _time

    overlay = hud.Overlay()
    overlay.update("上一条的文本", "done")
    overlay._drain(_time.monotonic())
    overlay._hide_seq = overlay._update_seq           # 开始收起
    overlay._phase = "out"
    overlay.update("正在听…", "recording")             # 收起期间用户又按下了
    overlay._drain(_time.monotonic())
    overlay._dismissed_seq = overlay._hide_seq         # 收起动画播完，记录的是开始收起那一刻
    assert overlay._should_show() is True, "收起期间来了新一轮，收起结束后必须再弹"


def test_hud_stays_hidden_after_auto_hide():
    """端到端回归：真窗口跑完显示→收起，终态必须一直保持隐藏。"""
    import time as _time

    overlay = hud.Overlay()
    overlay.start()
    try:
        deadline = _time.time() + 5.0
        while overlay._renderer is None and _time.time() < deadline:
            _time.sleep(0.05)
        if overlay._renderer is None:
            pytest.skip("HUD 无法初始化（没有可用桌面会话）")
        overlay.update("整理好的文本", "done")        # done 终态：显示 1.4s 后自动收起
        _time.sleep(0.8)
        assert overlay._phase in ("in", "shown"), "终态应当先弹出，实际 %s" % overlay._phase
        _time.sleep(2.4)                             # 1.4s 显示 + 0.22s 收起 + 余量
        assert overlay._phase == "hidden", "终态应当已经收起，实际 %s" % overlay._phase
        seen = []
        for _ in range(12):                          # 再盯 1.8 秒：不允许再弹出来
            seen.append(overlay._phase)
            _time.sleep(0.15)
        assert set(seen) == {"hidden"}, "收起后不该再弹出，实际 %r" % (seen,)
    finally:
        overlay.stop()
        _time.sleep(0.2)
        window = getattr(overlay, "_win", None)
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass

def test_font_stack_prefers_noto_sans_sc():
    """字体栈按优雅程度排序：有思源黑体（Noto Sans SC）就用它，否则回落系统字体。"""
    import os

    family = hud.hud_font_family()
    assert family in ("Noto Sans SC", "Microsoft YaHei UI", "Microsoft JhengHei UI", "DengXian")
    if os.path.exists(hud._font_path("NotoSansSC-VF.ttf")):
        assert family == "Noto Sans SC", "装了 Noto Sans SC 就应当优先用它，实际 " + family


def test_latin_glyphs_use_segoe_ui():
    """拉丁字符单独走 Segoe UI：中英混排时比中文自带的拉丁字形精致。"""
    import os

    if not os.path.exists(hud._font_path("segoeui.ttf")):
        pytest.skip("系统没有 Segoe UI")
    types = hud._Type(1.0)
    latin = types.font(hud.SIZE_TEXT, False, latin=True)
    cjk = types.font(hud.SIZE_TEXT, False, latin=False)
    assert latin.getname()[0].startswith("Segoe UI")
    assert not cjk.getname()[0].startswith("Segoe UI")


def test_mixed_scripts_share_one_baseline():
    """中英混排共用同一条基线：同一字号下 ASCII 与中日韩字形的位图高度必须一致。"""
    types = hud._Type(1.0)
    _, _, _, h_latin = types.tile("A", hud.SIZE_TEXT, False, 255)
    _, _, _, h_cjk = types.tile("中", hud.SIZE_TEXT, False, 255)
    _, _, _, h_punct = types.tile("，", hud.SIZE_TEXT, False, 255)
    assert h_latin == h_cjk == h_punct, \
        "字形高度不一致会让同一行文字上下跳动：%d / %d / %d" % (h_latin, h_cjk, h_punct)


def test_interaction_text_is_one_size_up_and_still_fits():
    """交互正文（转录文本）在规范 16px 上调大一档，并且两行仍装得进胶囊。"""
    assert hud.SIZE_TEXT >= 18, "交互正文字号应当调大一档"
    assert hud.SIZE_TEXT > hud.SIZE_HINT, "正文必须比右侧提示更醒目"
    assert hud.SIZE_STATUS >= hud.SIZE_HINT
    bottom = hud.TEXT_TOP + hud.TEXT_MAX_LINES * hud.TEXT_LINE_H
    assert bottom <= hud.PANEL_H - 12, "两行正文会顶到胶囊底边：%d > %d" % (bottom, hud.PANEL_H - 12)

def test_wrap_line_width_never_exceeds_limit():
    """折行后每行宽度都不能超过正文可用宽度（否则文字会顶到胶囊边上）。"""
    renderer = hud._Renderer()
    text = "然后那个我们今天讨论一下吧，关于新版本的排期和人力安排都要再确认一遍"
    chars = [(ch, 255) for ch in text]
    width = hud.PANEL_W - (hud.LEFT_W + hud.PAD_X) - hud.PAD_X
    lines, _truncated = renderer._wrap_tail(chars, hud.SIZE_TEXT, width, hud.TEXT_MAX_LINES)
    for index, line in enumerate(lines):
        line_w = sum(renderer.types.advance(chars[i][0], hud.SIZE_TEXT) for i in line)
        assert line_w <= width, "第 %d 行宽 %.1f 超过可用宽度 %d" % (index + 1, line_w, width)
