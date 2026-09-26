"""语音输入 HUD：Apple 风格「毛玻璃胶囊」浮窗（Pillow 分层窗口渲染，2026-09-26 重构）。

设计规范 → 实现对照（细节见 DEVELOPMENT 3.9）：

1) 窗口与容器
   - 无边框：WS_POPUP，没有标题栏/边框，也没有系统阴影（窗口类不注册 CS_DROPSHADOW）
   - 系统级透明：WS_EX_LAYERED + UpdateLayeredWindow，逐像素 alpha（不再是色键抠图）
   - 置顶：SetWindowPos(HWND_TOPMOST)；不抢焦点：WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW；
     完全穿透点击：WS_EX_TRANSPARENT（面板纯展示，不挡下面的窗口）
   - 胶囊/Squircle：圆角半径 34px，静态几何全部 4 倍超采样后 LANCZOS 缩小，边缘平滑
   - 布局：左侧动感麦克风 + 波形，右侧状态行 + 实时转录

2) 毛玻璃材质
   - 背景：rgba(20,20,20,0.4) 暗色半透明（浅色主题自动切 rgba(255,255,255,0.55)）
   - 模糊：弹出前抓取窗口背后那块桌面 → 高斯模糊 σ=12px（等价 CSS blur(24px)）→ 饱和度 x1.5
   - 边缘高光：1px rgba(255,255,255,0.15) 内发光边 + 顶部更亮的玻璃厚度亮边
   - 环境光阴影：两层柔光外阴影 0 8px 32px rgba(0,0,0,0.2) + 0 2px 8px rgba(0,0,0,0.1)

3) 动效
   - 出现/消失：cubic-bezier(0.34,1.56,0.64,1) 弹簧曲线，0.4s，translateY(15px→0) + scale(0.8→1) + 淡入
   - 聆听态：麦克风背后蓝→紫→品红渐变光晕，双瓣错位旋转 + 非对称呼吸 + 电平驱动的胀缩
   - 转录文本逐字淡入（180ms/字），已出现的字不重播（前缀复用）

4) 排版
   - 系统 UI 字体：微软雅黑 UI（msyh.ttc index=1）+ Segoe UI；状态 12px 600 60% 白；正文 16px 400 纯白

线程模型：HUD 独占一个线程跑「Win32 消息泵 + 渲染循环」，外部只用 update() / set_level() 投递队列。
没装 Pillow 时 HUD 自动禁用，语音输入本身不受影响。"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import math
import os
import queue
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

try:
    from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageEnhance, ImageFilter, ImageFont
    from PIL import ImageGrab

    HAS_PIL = True
except Exception:  # pragma: no cover - 没装 Pillow 时禁用 HUD
    HAS_PIL = False

# ---------------------------------------------------------------- 设计度量（逻辑 px @96dpi）
PANEL_W = 640
PANEL_H = 136               # 正文两行 + 上下留白
RADIUS = 34                 # 胶囊圆角（规范要求 >= 24px）
SHADOW_PAD = 46             # 阴影外扩：窗口比胶囊大一圈
LEFT_W = 132                # 左侧麦克风 / 波形区
PAD_X = 26
STATUS_CY = 36              # 状态行中心
TEXT_TOP = 56               # 正文首行顶部（固定不跳动）
TEXT_LINE_H = 30            # 交互正文调大一档后行高同步放大
TEXT_MAX_LINES = 2
SIZE_STATUS = 13            # 状态行字号（正文调大后同步 +1，保持层级）
SIZE_TEXT = 18              # 交互正文（转录文本）字号：比规范 16px 大一档
SIZE_HINT = 12              # 右侧提示字号
BARS = 7                    # 波形柱数量
BAR_W = 4
BAR_GAP = 4
BOTTOM_MARGIN = 150

APPEAR_MS = 400             # 弹簧入场
DISAPPEAR_MS = 220
CHAR_FADE_MS = 180          # 逐字淡入
HIDE_AFTER_MS = {"done": 1400, "empty": 900, "error": 3600, "idle": 400}

FRAME_S_ACTIVE = 1 / 50.0
FRAME_S_IDLE = 1 / 30.0

STYLE = {
    "idle": ("#6b7683", "待机"),
    "arming": ("#5ac8fa", "聆听中"),
    "recording": ("#5ac8fa", "聆听中"),
    "finalizing": ("#f2994a", "识别中"),
    "delivering": ("#9b51e0", "整理中"),
    "done": ("#27ae60", "已输入"),
    "empty": ("#8a8a8a", "没听到内容"),
    "error": ("#eb5757", "出错"),
}

# Siri 风格光晕渐变：内核 → 中层 → 外缘
GLOW = {
    "idle": ("#64748b", "#334155", "#1e293b"),
    "arming": ("#5ac8fa", "#7a5cff", "#ff4fd8"),
    "recording": ("#5ac8fa", "#7a5cff", "#ff4fd8"),
    "finalizing": ("#ffb24d", "#ff7a45", "#ff4f8b"),
    "delivering": ("#a06bff", "#7a5cff", "#4fd1ff"),
    "done": ("#4cd97b", "#27ae60", "#2fd1b0"),
    "empty": ("#8b98a5", "#5c6672", "#3f4750"),
    "error": ("#ff7b7b", "#e04a4a", "#ff9a5a"),
}

THEME = {
    "dark": {
        "fill": (20, 20, 20, 102),          # rgba(20,20,20,0.4)
        "border": (255, 255, 255, 38),      # rgba(255,255,255,0.15)
        "rim": (255, 255, 255, 76),         # 顶部玻璃厚度亮边
        "status": (255, 255, 255, 153),     # 60% 白
        "text": (255, 255, 255, 255),
        "hint": (255, 255, 255, 112),
        "fallback": (18, 20, 24),
    },
    "light": {
        "fill": (255, 255, 255, 140),       # rgba(255,255,255,0.55)
        "border": (255, 255, 255, 132),
        "rim": (255, 255, 255, 200),
        "status": (28, 28, 30, 158),
        "text": (22, 22, 24, 255),
        "hint": (60, 60, 67, 124),
        "fallback": (236, 238, 242),
    },
}


def _system_theme() -> str:
    """HUD 主题：默认暗色玻璃，可用环境变量 TYPEFAST_HUD_THEME=dark|light|auto 覆盖。

    浮窗要压在任意背景（黑色终端、白色文档、图片）上都读得清，所以默认走规范里的暗色玻璃
    rgba(20,20,20,0.4) + 纯白正文；设成 auto 才跟随 Windows 应用主题（浅色时用白玻璃）。"""
    want = (os.environ.get("TYPEFAST_HUD_THEME") or "dark").strip().lower()
    if want in ("dark", "light"):
        return want
    if want != "auto":
        return "dark"
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            light, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return "light" if int(light) else "dark"
    except Exception:
        return "dark"


# ---------------------------------------------------------------- 动效曲线
def cubic_bezier(x1: float, y1: float, x2: float, y2: float) -> Callable[[float], float]:
    """CSS cubic-bezier 的等价实现：给 x（时间进度）求 y（动画进度）。"""

    def one_axis(t: float, a: float, b: float) -> float:
        return 3.0 * (1.0 - t) ** 2 * t * a + 3.0 * (1.0 - t) * t ** 2 * b + t ** 3

    def solve(x: float) -> float:
        lo, hi = 0.0, 1.0
        for _ in range(26):
            mid = (lo + hi) / 2.0
            if one_axis(mid, x1, x2) < x:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0

    def ease(x: float) -> float:
        x = min(1.0, max(0.0, x))
        if x <= 0.0:
            return 0.0
        if x >= 1.0:
            return 1.0
        return one_axis(solve(x), y1, y2)

    return ease


EASE_SPRING = cubic_bezier(0.34, 1.56, 0.64, 1.0)   # 出现：带过冲的苹果弹簧
EASE_OUT = cubic_bezier(0.22, 1.0, 0.36, 1.0)       # 消失：快出缓停

_SPRING_PEAK = max(EASE_SPRING(i / 200.0) for i in range(201))


# ---------------------------------------------------------------- 字体与字形
def _font_path(name: str) -> str:
    return os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name)


# 字体栈（按优雅程度排序）：思源黑体家族（Noto Sans SC，可变字重）> 微软雅黑 UI
# > 微軟正黑體 UI > 等线。拉丁字符单独走 Segoe UI —— 中英混排时比雅黑自带的拉丁字形精致。
# 每项：(家族名, 文件名, ttc 索引, 是否可变字体)
_CJK_STACK = (
    ("Noto Sans SC", "NotoSansSC-VF.ttf", None, True),
    ("Microsoft YaHei UI", "msyh.ttc", 1, False),
    ("Microsoft JhengHei UI", "msjh.ttc", 1, False),
    ("DengXian", "Deng.ttf", 0, False),
)
_LATIN_FILES = {False: "segoeui.ttf", True: "seguisb.ttf"}
_FAMILY: Optional[str] = None


def hud_font_family() -> str:
    """当前可用的中文字体家族名（设置窗口共用同一套选择逻辑，保证全应用一致）。"""
    global _FAMILY
    if _FAMILY is None:
        for family, name, _index, _vf in _CJK_STACK:
            if os.path.exists(_font_path(name)):
                _FAMILY = family
                break
        else:
            _FAMILY = "Microsoft YaHei UI"
    return _FAMILY


def _load_font(size: int, bold: bool = False, latin: bool = False):
    """取字体：拉丁用 Segoe UI（Semibold），中日韩用栈里第一个可用字体。

    Noto Sans SC 是可变字体且默认实例是 Thin（太细），必须显式选字重（正文 400 / 加粗 600）。"""
    if latin:
        try:
            return ImageFont.truetype(_font_path(_LATIN_FILES[bool(bold)]), size)
        except Exception:
            pass
    weight = 600 if bold else 400
    for _family, name, index, variable in _CJK_STACK:
        path = _font_path(name)
        if not os.path.exists(path):
            continue
        try:
            font = (ImageFont.truetype(path, size, index=index) if index is not None
                    else ImageFont.truetype(path, size))
            if variable:
                try:
                    font.set_variation_by_axes([weight])
                except Exception:
                    pass
            return font
        except Exception:
            continue
    try:
        return ImageFont.truetype(_font_path(_LATIN_FILES[bool(bold)]), size)
    except Exception:
        return ImageFont.load_default()


class _Type:
    """字体 + 字形位图缓存：逐字淡入要按字贴图，所以把每个字的位图/步进宽度缓存起来。

    中英混排按字选字体（拉丁 Segoe UI / 中日韩 思源黑体），并统一到同一条基线 ——
    两种字体的 ascent 不同，不统一的话同一行文字会上下跳动。
    alpha 量化成 8 档（0..8），每档一张位图，避免每帧重算蒙版。
    """

    def __init__(self, scale: float = 1.0) -> None:
        self.scale = float(scale)
        self._fonts: Dict[Tuple[int, bool, bool], object] = {}
        self._adv: Dict[Tuple[str, int, bool], float] = {}
        self._tiles: Dict[Tuple[str, int, bool, int], Tuple[object, float, int, int]] = {}
        self._baseline: Dict[Tuple[int, bool], Tuple[int, int]] = {}

    def font(self, size: int, bold: bool = False, latin: bool = False):
        key = (size, bold, latin)
        hit = self._fonts.get(key)
        if hit is None:
            hit = _load_font(max(6, int(round(size * self.scale))), bold, latin)
            self._fonts[key] = hit
        return hit

    def font_for(self, ch: str, size: int, bold: bool = False):
        """按字符选字体：ASCII 走 Segoe UI，其余（中日韩、全角标点）走中文字体。"""
        return self.font(size, bold, latin=ch.isascii())

    def advance(self, ch: str, size: int, bold: bool = False) -> float:
        key = (ch, size, bold)
        hit = self._adv.get(key)
        if hit is None:
            hit = float(self.font_for(ch, size, bold).getlength(ch))
            self._adv[key] = hit
        return hit

    def baseline(self, size: int, bold: bool = False) -> Tuple[int, int]:
        """两种字体共用的 (ascent, descent)：字形位图按它对齐，保证整行同一条基线。"""
        key = (size, bold)
        hit = self._baseline.get(key)
        if hit is None:
            ascent = descent = 0
            for latin in (False, True):
                a, d = self.font(size, bold, latin).getmetrics()
                ascent, descent = max(ascent, a), max(descent, d)
            hit = (ascent, descent)
            self._baseline[key] = hit
        return hit

    def tile(self, ch: str, size: int, bold: bool, alpha: int):
        """返回 (RGBA 位图, 步进宽度, 位图宽, 位图高)，alpha 0..255。"""
        step = max(0, min(8, int(round(alpha / 255.0 * 8.0))))
        key = (ch, size, bold, step)
        hit = self._tiles.get(key)
        if hit is not None:
            return hit
        font = self.font_for(ch, size, bold)
        ascent, _descent = font.getmetrics()
        base_ascent, base_descent = self.baseline(size, bold)
        adv = float(font.getlength(ch))
        pad = max(1, int(self.scale))
        width = max(1, int(math.ceil(adv)) + 2 * pad)
        height = base_ascent + base_descent
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).text((pad, base_ascent - ascent), ch, font=font, fill=min(255, step * 32))
        tile = Image.new("RGBA", (width, height), (255, 255, 255, 0))
        tile.putalpha(mask)
        self._tiles[key] = (tile, adv, width, height)
        return tile, adv, width, height

    def text_width(self, text: str, size: int, bold: bool = False) -> float:
        return sum(self.advance(ch, size, bold) for ch in text)

def _mix_color(a: Sequence[int], b: Sequence[int], t: float) -> Tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return (int(a[0] + (b[0] - a[0]) * t), int(a[1] + (b[1] - a[1]) * t), int(a[2] + (b[2] - a[2]) * t))


def _clip_box(box: Sequence[int], size: Sequence[int]):
    x0 = max(0, int(box[0]))
    y0 = max(0, int(box[1]))
    x1 = min(int(size[0]), int(box[2]))
    y1 = min(int(size[1]), int(box[3]))
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def _mic_sprite(size: int, color: Tuple[int, int, int, int], ss: int = 3):
    """极简麦克风：胶囊腔体 + U 形支架 + 立柱。3 倍超采样后缩小，曲线平滑。"""
    n = max(12, int(size) * ss)
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    body_w = int(n * 0.30)
    x0 = int(n / 2.0 - body_w / 2.0)
    d.rounded_rectangle([x0, int(n * 0.10), x0 + body_w, int(n * 0.58)], radius=body_w / 2.0, fill=color)
    stroke = max(2, int(n * 0.075))
    d.arc([int(n * 0.24), int(n * 0.28), int(n * 0.76), int(n * 0.80)], start=0, end=180, fill=color, width=stroke)
    d.line([int(n / 2.0), int(n * 0.78), int(n / 2.0), int(n * 0.90)], fill=color, width=stroke)
    d.line([int(n * 0.34), int(n * 0.90), int(n * 0.66), int(n * 0.90)], fill=color, width=stroke)
    return img.resize((int(size), int(size)), Image.LANCZOS)


def _dot_sprite(size: int, color: Tuple[int, int, int], ss: int = 4):
    """状态点：实心圆 + 外圈柔光。"""
    n = max(8, int(size) * ss)
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([int(n * 0.06), int(n * 0.06), int(n * 0.94), int(n * 0.94)], fill=(color[0], color[1], color[2], 64))
    d.ellipse([int(n * 0.26), int(n * 0.26), int(n * 0.74), int(n * 0.74)], fill=(color[0], color[1], color[2], 255))
    return img.resize((int(size), int(size)), Image.LANCZOS)


def _glow_sprite(colors: Sequence[str], size: int):
    """Siri 风格光晕：径向三段渐变（内核 → 中层 → 外缘）+ 高斯模糊，作为发光流体底。"""
    import numpy as np

    size = max(24, int(size))
    half = max(16, size // 2)
    ys, xs = np.mgrid[0:half, 0:half].astype(np.float32)
    center = (half - 1) / 2.0
    dist = np.sqrt((xs - center) ** 2 + (ys - center) ** 2) / (half / 2.0)
    dist = np.clip(dist, 0.0, 1.0)
    c0, c1, c2 = [np.array(ImageColor.getrgb(col), dtype=np.float32) for col in colors]
    w1 = np.clip(dist / 0.55, 0.0, 1.0)[..., None]
    rgb = c0 * (1.0 - w1) + c1 * w1
    w2 = np.clip((dist - 0.55) / 0.45, 0.0, 1.0)[..., None]
    rgb = rgb * (1.0 - w2) + c2 * w2
    ring = np.exp(-((dist - 0.42) / 0.36) ** 2)          # 环状发光：核心不堆成白色
    edge = np.clip((1.0 - dist) / 0.25, 0.0, 1.0)        # 外缘收敛
    core = np.clip(1.0 - dist / 0.40, 0.0, 1.0) ** 1.6 * 0.35
    alpha = (np.clip(ring * 0.95 + core, 0.0, 1.0) * edge * 215.0)[..., None]
    arr = np.concatenate([rgb, alpha], axis=2).astype(np.uint8)
    small = Image.fromarray(arr, "RGBA").filter(ImageFilter.GaussianBlur(max(1.0, half * 0.055)))
    return small.resize((size, size), Image.BILINEAR)


class _Renderer:
    """纯渲染器：不碰窗口，输入状态/文本/电平，输出一帧 RGBA 图（可离线单测）。"""

    def __init__(self, panel_w: int = PANEL_W, panel_h: int = PANEL_H,
                 scale: float = 1.0, theme: str = "dark") -> None:
        self.scale = float(scale)
        self.panel_w = int(round(panel_w * self.scale))
        self.panel_h = int(round(panel_h * self.scale))
        self.pad = int(round(SHADOW_PAD * self.scale))
        self.radius = int(round(RADIUS * self.scale))
        self.theme_name = theme if theme in THEME else "dark"
        self.theme = THEME[self.theme_name]
        self.types = _Type(self.scale)
        self.win_w = self.panel_w + 2 * self.pad
        self.win_h = self.panel_h + 2 * self.pad
        self._backdrop = None
        self._glass = None
        self._glow: Dict[str, object] = {}
        self._dots: Dict[Tuple[int, str], object] = {}
        self._mics: Dict[Tuple[int, str], object] = {}
        self._status_cache: Dict[Tuple[str, str, str], object] = {}
        self._build_static()
        self._build_glass(None)          # 兜底玻璃底：抓不到屏幕也能看到胶囊

    # ---------------- 静态层（圆角 / 填色 / 1px 高光边 / 双层阴影） ----------------
    def _build_static(self) -> None:
        pw, ph, pad, r = self.panel_w, self.panel_h, self.pad, self.radius
        f = 4
        big = (pw * f, ph * f)
        mask_big = Image.new("L", big, 0)
        ImageDraw.Draw(mask_big).rounded_rectangle([0, 0, big[0] - 1, big[1] - 1], radius=r * f, fill=255)
        self.mask = mask_big.resize((pw, ph), Image.LANCZOS)

        fill_big = Image.new("RGBA", big, (0, 0, 0, 0))
        ImageDraw.Draw(fill_big).rounded_rectangle([0, 0, big[0] - 1, big[1] - 1], radius=r * f,
                                                   fill=self.theme["fill"])
        self.fill = fill_big.resize((pw, ph), Image.LANCZOS)

        edge_big = Image.new("RGBA", big, (0, 0, 0, 0))
        de = ImageDraw.Draw(edge_big)
        inset = f / 2.0
        de.rounded_rectangle([inset, inset, big[0] - 1 - inset, big[1] - 1 - inset],
                             radius=max(1.0, (r - 0.5) * f), outline=self.theme["border"], width=f)
        de.arc([inset * 3, inset * 3, big[0] - 1 - inset * 3, big[1] - 1 - inset * 3],
               start=190, end=350, fill=self.theme["rim"], width=f)
        self.border = edge_big.resize((pw, ph), Image.LANCZOS)

        shadow = Image.new("RGBA", (self.win_w, self.win_h), (0, 0, 0, 0))
        for dy, blur, alpha in ((8.0, 32.0, 0.20), (2.0, 8.0, 0.10)):
            layer = Image.new("RGBA", shadow.size, (0, 0, 0, 0))
            ImageDraw.Draw(layer).rounded_rectangle(
                [pad, pad + dy * self.scale, pad + pw - 1, pad + ph - 1 + dy * self.scale],
                radius=r, fill=(0, 0, 0, int(round(255 * alpha))))
            layer = layer.filter(ImageFilter.GaussianBlur(max(1.0, blur * self.scale / 2.0)))
            shadow = Image.alpha_composite(shadow, layer)
        ring = Image.new("L", (self.win_w, self.win_h), 0)
        ring.paste(self.mask, (pad, pad))
        shadow.putalpha(ImageChops.subtract(shadow.getchannel("A"), ring))
        self.base = shadow

    def _build_glass(self, image) -> None:
        """玻璃底 = 模糊后的桌面（贴在圆角内）+ 暗色/浅色半透明填充；抓不到屏幕时用纯色兜底。"""
        if image is None:
            base = Image.new("RGBA", (self.panel_w, self.panel_h), self.theme["fallback"] + (236,))
            base.putalpha(ImageChops.multiply(base.getchannel("A"), self.mask))
        else:
            base = image.copy()
            base.putalpha(ImageChops.multiply(base.getchannel("A"), self.mask))
        self._glass = Image.alpha_composite(base, self.fill)

    def set_backdrop(self, image) -> None:
        """设置弹出瞬间抓到的桌面模糊图（None = 抓取失败，走纯色兜底）。"""
        self._backdrop = image
        self._build_glass(image)

    # ---------------- 元素缓存 ----------------
    def glow(self, state: str):
        hit = self._glow.get(state)
        if hit is None:
            hit = _glow_sprite(GLOW.get(state, GLOW["idle"]), int(round(216 * self.scale)))
            self._glow[state] = hit
        return hit

    def glow_alt(self, state: str):
        """第二瓣光晕用错位色序（紫/品红/蓝），两瓣叠加才混得出 Siri 那种蓝紫品红。"""
        hit = self._glow.get(state + "#alt")
        if hit is None:
            colors = GLOW.get(state, GLOW["idle"])
            hit = _glow_sprite((colors[1], colors[2], colors[0]), int(round(216 * self.scale)))
            self._glow[state + "#alt"] = hit
        return hit

    def mic(self, size: int, color: Tuple[int, int, int, int]):
        key = (int(size), str(color))
        hit = self._mics.get(key)
        if hit is None:
            hit = _mic_sprite(int(size), color)
            self._mics[key] = hit
        return hit

    def dot(self, size: int, color: Tuple[int, int, int]):
        key = (int(size), str(color))
        hit = self._dots.get(key)
        if hit is None:
            hit = _dot_sprite(int(size), color)
            self._dots[key] = hit
        return hit

    # ---------------- 逐帧绘制 ----------------
    def _blend_glow(self, content, sprite, cx: int, cy: int, sx: float, sy: float, weight: float,
                    mode: str = "screen") -> None:
        w = max(10, int(sprite.width * sx))
        h = max(10, int(sprite.height * sy))
        sp = sprite if (w == sprite.width and h == sprite.height) else sprite.resize((w, h), Image.BILINEAR)
        box = (cx - w // 2, cy - h // 2, cx - w // 2 + w, cy - h // 2 + h)
        clip = _clip_box(box, content.size)
        if clip is None:
            return
        src = sp.crop((clip[0] - box[0], clip[1] - box[1], clip[2] - box[0], clip[3] - box[1]))
        alpha = src.getchannel("A")
        if weight < 0.999:
            alpha = alpha.point(lambda v: int(v * weight))
        if mode == "screen":      # 发光：提亮
            dst = content.crop(clip).convert("RGB")
            content.paste(ImageChops.screen(dst, src.convert("RGB")).convert("RGBA"), (clip[0], clip[1]), alpha)
        else:                     # 上色：正常混合，保住蓝/紫/品红的饱和度
            tinted = src.copy()
            tinted.putalpha(alpha)
            content.alpha_composite(tinted, (clip[0], clip[1]))

    def _draw_text(self, content, text: str, x: float, y_center: float, size: int, bold: bool,
                   color: Tuple[int, int, int, int]) -> float:
        """整段文字（不做逐字动画）：状态行/提示用。返回结束 x。"""
        pad = max(1, int(self.scale))
        for ch in text:
            tile, adv, tw, th = self.types.tile(ch, size, bold, color[3])
            tinted = ImageChops.multiply(tile, Image.new("RGBA", tile.size, color[:3] + (255,)))
            content.alpha_composite(tinted, (int(x - pad), int(y_center - th / 2.0)))
            x += adv
        return x

    def _status_layer(self, label: str, hint: str, accent) -> object:
        """状态点 + 状态文本 + 右侧提示：整行缓存（状态变化才重画），每帧只贴一次。"""
        key = (label, hint, str(accent))
        hit = self._status_cache.get(key)
        if hit is not None:
            return hit
        s = self.scale
        layer = Image.new("RGBA", (self.panel_w, self.panel_h), (0, 0, 0, 0))
        rx = int(round((LEFT_W + PAD_X) * s))
        cy = int(round(STATUS_CY * s))
        dot = self.dot(max(7, int(round(7 * s))), accent)
        layer.alpha_composite(dot, (rx, cy - dot.height // 2))
        self._draw_text(layer, label, rx + dot.width + max(4, int(6 * s)), cy, SIZE_STATUS, True, self.theme["status"])
        hint_w = self.types.text_width(hint, SIZE_HINT)
        self._draw_text(layer, hint, self.panel_w - int(round(PAD_X * s)) - hint_w, cy, SIZE_HINT, False,
                        self.theme["hint"])
        self._status_cache[key] = layer
        return layer

    def _wrap_tail(self, chars: Sequence[Tuple[str, int]], size: int, max_w: float, max_lines: int):
        """从尾部往前装行：正文超出时保留最新内容，并在最前面加省略号。"""
        lines: List[List[int]] = []
        cur: List[int] = []
        cur_w = 0.0
        truncated = False
        for idx in range(len(chars) - 1, -1, -1):
            ch = chars[idx][0]
            if ch == "\n":
                lines.append(cur)
                cur, cur_w = [], 0.0
                if len(lines) >= max_lines:
                    truncated = True
                    break
                continue
            w = self.types.advance(ch, size)
            if cur and cur_w + w > max_w:
                lines.append(cur)
                if len(lines) >= max_lines:
                    truncated = True
                    break
                cur, cur_w = [], 0.0
            cur.append(idx)
            cur_w += w
        if cur and len(lines) < max_lines:
            lines.append(cur)
        lines.reverse()
        for line in lines:
            line.reverse()
        # 行宽兜底：从尾部往前装时，每行第一个字是无条件塞进去的，可能略微超宽。
        # 超了就从这个行最老的字开始裁（每行至少留一个字），保证文字不会顶到胶囊边上。
        for line in lines:
            line_w = sum(self.types.advance(chars[i][0], size) for i in line)
            while len(line) > 1 and line_w > max_w:
                line_w -= self.types.advance(chars[line[0]][0], size)
                line.pop(0)
        return lines, truncated

    def frame(self, *, state: str, label: str, hint: str, chars: Sequence[Tuple[str, int]],
              level: float, bars: Sequence[float], now: float, alpha: float = 1.0):
        s = self.scale
        pw, ph, pad = self.panel_w, self.panel_h, self.pad
        theme = self.theme
        accent = ImageColor.getrgb(STYLE.get(state, STYLE["idle"])[0])

        img = self.base.copy()
        if self._glass is not None:
            img.alpha_composite(self._glass, (pad, pad))
        img.alpha_composite(self.border, (pad, pad))

        content = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))

        # 1) Siri 光晕：双瓣错位旋转 + 非对称呼吸 + 电平胀缩
        glow_cx = int(round(LEFT_W * s * 0.5))
        glow_cy = int(round(ph * 0.44))
        strength = 0.56 + 0.34 * min(1.0, level * 1.4)
        if state in ("idle", "empty"):
            strength *= 0.45
        breathe = 1.0 + 0.06 * math.sin(now * 2.1)
        sprite = self.glow(state)
        sprite_b = self.glow_alt(state)
        # 光晕在 1/2 分辨率的小图上做双瓣 screen 混合再放大：柔光看不出分辨率差别，省一半以上开销
        box_w = max(48, int(round(286 * s)))
        box_h = max(48, int(round(236 * s)))
        small = Image.new("RGBA", (max(16, box_w // 2), max(16, box_h // 2)), (0, 0, 0, 0))
        for idx, phase in ((0, 0.0), (1, 2.35)):
            ang = now * 0.9 + phase
            rad = (4.0 + 9.0 * min(1.0, level * 1.5)) * s
            ox = int(math.cos(ang) * rad)
            oy = int(math.sin(ang * 1.3) * rad * 0.65)
            sx = (1.0 + 0.18 * math.sin(now * 1.6 + phase) + 0.34 * level) * breathe * 0.5
            sy = (1.0 - 0.14 * math.sin(now * 2.1 + phase) + 0.30 * level) * breathe * 0.5
            weight = (0.92 if idx == 0 else 0.85) * strength
            self._blend_glow(small, sprite if idx == 0 else sprite_b,
                             small.width // 2 + ox, small.height // 2 + oy, sx, sy, min(1.0, weight),
                             "screen" if idx == 0 else "normal")
        content.alpha_composite(small.resize((box_w, box_h), Image.BILINEAR),
                                (glow_cx - box_w // 2, glow_cy - box_h // 2))

        # 2) 左侧：麦克风（缓存精灵）+ 波形（柱子条带 2 倍超采样，圆头平滑）
        mic = self.mic(int(round(44 * s)), (255, 255, 255, 238))
        content.alpha_composite(mic, (int(round(LEFT_W * s * 0.5)) - mic.width // 2,
                                      int(round(ph * 0.28)) - mic.height // 2))
        ss = 2
        bar_w = max(2, int(round(BAR_W * s)))
        gap = max(1, int(round(BAR_GAP * s)))
        total = len(bars) * bar_w + (len(bars) - 1) * gap
        strip_h = int(round((3.0 + 26.0) * s)) + 2
        strip = Image.new("RGBA", (total * ss, strip_h * ss), (0, 0, 0, 0))
        sd = ImageDraw.Draw(strip)
        bx = 0
        cy = strip_h * ss // 2
        for value in bars:
            v = max(0.0, min(1.0, float(value)))
            bw = bar_w * ss
            bh = max(bw, int((3.0 + 26.0 * v) * ss * s))
            col = _mix_color(accent, (255, 255, 255), 0.12 + 0.62 * v)
            sd.rounded_rectangle([bx, cy - bh // 2, bx + bw, cy - bh // 2 + bh],
                                 radius=min(bw, bh) / 2.0, fill=(col[0], col[1], col[2], 240))
            bx += bw + gap * ss
        strip = strip.resize((total, strip_h), Image.LANCZOS)
        content.alpha_composite(strip, (int(round(LEFT_W * s * 0.5)) - total // 2,
                                        int(round(ph * 0.78)) - strip_h // 2))

        # 3) 右侧：状态行 + 提示（整行缓存）+ 逐字淡入正文
        content.alpha_composite(self._status_layer(label, hint, accent))
        rx = int(round((LEFT_W + PAD_X) * s))
        text_w = pw - rx - int(round(PAD_X * s))
        lines, truncated = self._wrap_tail(chars, SIZE_TEXT, text_w, TEXT_MAX_LINES)
        pad_c = max(1, int(s))
        for li, line in enumerate(lines):
            x = float(rx - pad_c)
            y_center = (int(round(TEXT_TOP * s)) + li * int(round(TEXT_LINE_H * s))
                    + int(round(TEXT_LINE_H * s * 0.5)))
            if li == 0 and truncated:
                tile, adv, tw, th = self.types.tile("…", SIZE_TEXT, False, 200)
                content.alpha_composite(tile, (int(x), int(y_center - th / 2.0)))
                x += adv
            for idx in line:
                ch, a = chars[idx]
                tile, adv, tw, th = self.types.tile(ch, SIZE_TEXT, False, a)
                content.alpha_composite(tile, (int(x), int(y_center - th / 2.0)))
                x += adv

        content.putalpha(ImageChops.multiply(content.getchannel("A"), self.mask))
        img.alpha_composite(content, (pad, pad))
        if alpha < 0.999:
            img.putalpha(img.getchannel("A").point(lambda v: int(v * alpha)))
        return img


# ---------------------------------------------------------------- Win32 分层窗口
WM_PAINT = 0x000F
WM_ERASEBKGND = 0x0014
WM_DISPLAYCHANGE = 0x007E
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_POPUP = 0x80000000
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
HWND_TOPMOST = -1
ULW_ALPHA = 0x00000002
PM_REMOVE = 0x0001
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
MONITOR_DEFAULTTONEAREST = 2
IDC_ARROW = 32512
LOGPIXELSX = 88
DIB_RGB_COLORS = 0


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", ctypes.c_void_p), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, wt.UINT, ctypes.c_size_t, ctypes.c_size_t)

_API_READY = False


def _init_api() -> None:
    global _API_READY
    if _API_READY:
        return
    u = ctypes.windll.user32
    g = ctypes.windll.gdi32
    k = ctypes.windll.kernel32
    u.GetDC.restype = ctypes.c_void_p
    u.GetDC.argtypes = [ctypes.c_void_p]
    u.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    u.CreateWindowExW.restype = ctypes.c_void_p
    u.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_void_p, ctypes.c_void_p]
    u.DefWindowProcW.restype = ctypes.c_ssize_t
    u.DefWindowProcW.argtypes = [ctypes.c_void_p, wt.UINT, ctypes.c_size_t, ctypes.c_size_t]
    u.RegisterClassW.restype = wt.ATOM
    u.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
    u.LoadCursorW.restype = ctypes.c_void_p
    u.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    u.PeekMessageW.restype = wt.BOOL
    u.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), ctypes.c_void_p, wt.UINT, wt.UINT, wt.UINT]
    u.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
    u.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
    u.UpdateLayeredWindow.restype = wt.BOOL
    u.UpdateLayeredWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wt.POINT),
                                      ctypes.POINTER(wt.SIZE), ctypes.c_void_p, ctypes.POINTER(wt.POINT),
                                      wt.DWORD, ctypes.POINTER(_BLENDFUNCTION), wt.DWORD]
    u.SetWindowPos.restype = wt.BOOL
    u.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int, wt.UINT]
    u.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    u.DestroyWindow.argtypes = [ctypes.c_void_p]
    u.ValidateRect.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    u.MonitorFromPoint.restype = ctypes.c_void_p
    u.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
    u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MONITORINFO)]
    u.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
    g.CreateCompatibleDC.restype = ctypes.c_void_p
    g.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    g.CreateDIBSection.restype = ctypes.c_void_p
    g.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.POINTER(_BITMAPINFO), wt.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, wt.DWORD]
    g.SelectObject.restype = ctypes.c_void_p
    g.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    g.DeleteObject.argtypes = [ctypes.c_void_p]
    g.DeleteDC.argtypes = [ctypes.c_void_p]
    g.GetDeviceCaps.argtypes = [ctypes.c_void_p, ctypes.c_int]
    k.GetModuleHandleW.restype = ctypes.c_void_p
    k.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    _API_READY = True


def _set_dpi_aware() -> None:
    """让浮窗在缩放屏上也是物理像素级清晰（不会被打回模糊位图）。"""
    for call in (lambda: ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)),
                 lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2),
                 lambda: ctypes.windll.user32.SetProcessDPIAware()):
        try:
            call()
            return
        except Exception:
            continue


def _scale_factor() -> float:
    try:
        _init_api()
        dc = ctypes.windll.user32.GetDC(None)
        dpi = int(ctypes.windll.gdi32.GetDeviceCaps(ctypes.c_void_p(dc), LOGPIXELSX))
        ctypes.windll.user32.ReleaseDC(None, ctypes.c_void_p(dc))
        return max(1.0, min(3.0, dpi / 96.0))
    except Exception:
        return 1.0


def _monitor_rect():
    try:
        _init_api()
        u = ctypes.windll.user32
        pt = wt.POINT()
        u.GetCursorPos(ctypes.byref(pt))
        mon = u.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if mon and u.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(info)):
            rc = info.rcMonitor
            return (int(rc.left), int(rc.top), int(rc.right), int(rc.bottom))
    except Exception:
        pass
    u = ctypes.windll.user32
    return (0, 0, int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1)))


def _grab_blurred(box, scale: float, log):
    """抓一块屏幕 → 高斯模糊 σ=12px（等价 backdrop-filter: blur(24px)）→ 饱和度 x1.5。"""
    try:
        shot = ImageGrab.grab(bbox=box, all_screens=True)
    except Exception as exc:
        log("HUD backdrop grab failed: " + repr(exc))
        return None
    try:
        blurred = shot.convert("RGB").filter(ImageFilter.GaussianBlur(12.0 * scale))
        return ImageEnhance.Color(blurred).enhance(1.5).convert("RGBA")
    except Exception as exc:
        log("HUD backdrop blur failed: " + repr(exc))
        return None


class _LayeredWindow:
    """WS_EX_LAYERED 无边框置顶窗口：内容全部交给 UpdateLayeredWindow（逐像素 alpha）。"""

    _class_atom = None
    _proc_ref = None
    _class_name = "TypeFastHudWnd"

    def __init__(self, log: Optional[Callable[[str], None]] = None) -> None:
        _init_api()
        self.log = log or (lambda m: None)
        self.hwnd = None
        self.on_display_change = False
        self._dc_screen = None
        self._dc_mem = None
        self._bmp = None
        self._bits = None
        self._size = (0, 0)

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_PAINT:
            ctypes.windll.user32.ValidateRect(ctypes.c_void_p(hwnd), None)
            return 0
        if msg == WM_ERASEBKGND:
            return 1
        if msg == WM_DISPLAYCHANGE:
            self.on_display_change = True
            return 0
        return ctypes.windll.user32.DefWindowProcW(ctypes.c_void_p(hwnd), msg, wparam, lparam)

    def create(self, w: int, h: int) -> None:
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        if _LayeredWindow._class_atom is None:
            proc = WNDPROC(self._wnd_proc)
            _LayeredWindow._proc_ref = proc
            cls = _WNDCLASSW()
            cls.style = 0                       # 不注册 CS_DROPSHADOW：系统阴影必须没有
            cls.lpfnWndProc = ctypes.cast(proc, ctypes.c_void_p)
            cls.hInstance = k.GetModuleHandleW(None)
            cls.hCursor = u.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
            cls.lpszClassName = _LayeredWindow._class_name
            atom = u.RegisterClassW(ctypes.byref(cls))
            if not atom:
                raise RuntimeError("RegisterClassW failed")
            _LayeredWindow._class_atom = atom
        ex = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        self.hwnd = u.CreateWindowExW(ex, _LayeredWindow._class_name, _LayeredWindow._class_name, WS_POPUP,
                                      0, 0, int(w), int(h), None, None, k.GetModuleHandleW(None), None)
        if not self.hwnd:
            raise RuntimeError("CreateWindowExW failed")
        u.SetWindowPos(ctypes.c_void_p(self.hwnd), ctypes.c_void_p(HWND_TOPMOST), 0, 0, 0, 0,
                       SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        self._dc_screen = u.GetDC(None)
        self._ensure_dib(int(w), int(h))

    def _ensure_dib(self, w: int, h: int) -> None:
        g = ctypes.windll.gdi32
        if self._bmp and (w, h) == self._size:
            return
        if self._bmp:
            g.DeleteObject(ctypes.c_void_p(self._bmp))
            self._bmp = None
        if self._dc_mem is None:
            self._dc_mem = g.CreateCompatibleDC(ctypes.c_void_p(self._dc_screen))
        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h             # 负高度 = 自上而下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0          # BI_RGB
        bits = ctypes.c_void_p()
        self._bmp = g.CreateDIBSection(ctypes.c_void_p(self._dc_screen), ctypes.byref(bmi), DIB_RGB_COLORS,
                                       ctypes.byref(bits), None, 0)
        if not self._bmp:
            raise RuntimeError("CreateDIBSection failed")
        g.SelectObject(ctypes.c_void_p(self._dc_mem), ctypes.c_void_p(self._bmp))
        self._bits = bits
        self._size = (w, h)

    def blit(self, data: bytes, w: int, h: int, x: int, y: int) -> None:
        u = ctypes.windll.user32
        self._ensure_dib(w, h)
        ctypes.memmove(self._bits, data, len(data))
        pt_dst = wt.POINT(int(x), int(y))
        size = wt.SIZE(int(w), int(h))
        pt_src = wt.POINT(0, 0)
        blend = _BLENDFUNCTION(0, 0, 255, 1)     # AC_SRC_OVER + AC_SRC_ALPHA
        u.UpdateLayeredWindow(ctypes.c_void_p(self.hwnd), ctypes.c_void_p(self._dc_screen),
                              ctypes.byref(pt_dst), ctypes.byref(size), ctypes.c_void_p(self._dc_mem),
                              ctypes.byref(pt_src), 0, ctypes.byref(blend), ULW_ALPHA)

    def show(self) -> None:
        ctypes.windll.user32.ShowWindow(ctypes.c_void_p(self.hwnd), SW_SHOWNOACTIVATE)

    def hide(self) -> None:
        if self.hwnd:
            ctypes.windll.user32.ShowWindow(ctypes.c_void_p(self.hwnd), SW_HIDE)

    def pump(self) -> None:
        u = ctypes.windll.user32
        msg = wt.MSG()
        while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))

    def destroy(self) -> None:
        g = ctypes.windll.gdi32
        if self.hwnd:
            ctypes.windll.user32.DestroyWindow(ctypes.c_void_p(self.hwnd))
            self.hwnd = None
        if self._bmp:
            g.DeleteObject(ctypes.c_void_p(self._bmp))
            self._bmp = None
        if self._dc_mem:
            g.DeleteDC(ctypes.c_void_p(self._dc_mem))
            self._dc_mem = None
        if self._dc_screen:
            ctypes.windll.user32.ReleaseDC(None, ctypes.c_void_p(self._dc_screen))
            self._dc_screen = None


class Overlay:
    """按住说话时的悬浮交互框。对外接口与旧版一致：start() / update(text, state) / set_level(level)。"""

    def __init__(self, width: int = PANEL_W, height: int = PANEL_H, hotkey_text: str = "Right Ctrl",
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.panel_w = int(width)
        self.panel_h = int(height)
        self.hotkey_text = hotkey_text
        self.log = log or (lambda m: None)
        self._queue: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # —— 以下状态只在 HUD 线程内读写 ——
        self._state = "idle"
        self._text = ""
        self._chars: List[List] = []
        self._level = 0.0
        self._level_at = 0.0
        self._smooth = 0.0
        self._bars: List[float] = [0.0] * BARS
        self._phase = "hidden"
        self._phase_t0 = 0.0
        self._hide_at: Optional[float] = None
        self._update_seq = 0        # 每收到一次新的状态/文本就 +1（用来判断是不是新一轮输入）
        self._dismissed_seq = 0     # 收起动画播完时记下的 _update_seq：两者相等 = 本轮已收起
        self._hide_seq = 0          # 开始收起那一刻的 _update_seq
        self._win: Optional[_LayeredWindow] = None
        self._renderer: Optional[_Renderer] = None
        self._scale = 1.0
        self._base = (0, 0)
        self._screen = (0, 0, 1920, 1080)

    # ---------------- 外部 API ----------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="typefast-hud", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def update(self, text: str = "", state: str = "") -> None:
        self._queue.put(("status", (state or "idle", text or "")))

    def set_level(self, level: float) -> None:
        self._queue.put(("level", float(level)))

    # ---------------- 队列与文本 ----------------
    def _drain(self, now: float) -> None:
        try:
            while True:
                kind, value = self._queue.get_nowait()
                if kind == "level":
                    self._level = max(0.0, min(1.0, float(value)))
                    self._level_at = now
                elif kind == "status":
                    state, text = value
                    if state != self._state or text != self._text:
                        self._update_seq += 1      # 有新状态或新文本 = 新一轮输入
                    if state != self._state:
                        self._hide_at = None
                    if text != self._text:
                        self._text = text
                        self._resync_chars(text, now)
                    self._state = state
        except queue.Empty:
            pass

    def _resync_chars(self, text: str, now: float) -> None:
        """逐字淡入：复用与上一帧文本的最长公共前缀，只给新出现的字记出生时间。"""
        old = self._chars
        common = 0
        limit = min(len(old), len(text))
        while common < limit and old[common][0] == text[common]:
            common += 1
        chars = [[ch, old[i][1]] for i, ch in enumerate(text[:common])]
        for ch in text[common:]:
            chars.append([ch, now])
        self._chars = chars

    def _alphas(self, now: float) -> List[Tuple[str, int]]:
        fade = max(0.001, CHAR_FADE_MS / 1000.0)
        out: List[Tuple[str, int]] = []
        for ch, birth in self._chars:
            p = min(1.0, max(0.0, (now - float(birth)) / fade))
            out.append((ch, int(round(255 * p))))
        return out

    # ---------------- HUD 线程 ----------------
    def _should_show(self) -> bool:
        """收起动画播完后不再自动弹回来：终态（已输入/没听到/出错）会一直停在当前的
        state/text 上，只看状态判断会导致「收起→立刻又弹出」的死循环（0.0.9 首版的 bug）。"""
        if self._update_seq == self._dismissed_seq:
            return False      # 本轮已经收起过了，等下一次更新再弹
        return not (self._state == "idle" and not self._text)

    def _enter(self, now: float) -> None:
        r = self._renderer
        left, top, right, bottom = self._screen
        bx = left + (right - left - r.win_w) // 2
        by = bottom - int(round(BOTTOM_MARGIN * self._scale)) - r.win_h
        self._base = (bx, by)
        if self._win is not None:
            self._win.hide()                 # 抓背景前先把自己藏起来，否则会抓到上一帧
        time.sleep(0.03)
        pad = r.pad
        r.set_backdrop(_grab_blurred((bx + pad, by + pad, bx + pad + r.panel_w, by + pad + r.panel_h),
                                     self._scale, self.log))
        self._hide_at = None          # 新一轮显示，重新计时
        self._phase = "in"
        self._phase_t0 = now

    def _paint(self, now: float) -> None:
        r = self._renderer
        if self._phase == "in":
            p = min(1.0, (now - self._phase_t0) / (APPEAR_MS / 1000.0))
            e = EASE_SPRING(p)
            scale = 0.8 + 0.2 * e
            dy = (1.0 - e) * 15.0 * self._scale
            alpha = min(1.0, p / 0.35)
            if p >= 1.0:
                self._phase = "shown"
        elif self._phase == "out":
            p = min(1.0, (now - self._phase_t0) / (DISAPPEAR_MS / 1000.0))
            e = EASE_OUT(p)
            scale = 1.0 - 0.08 * e
            dy = 8.0 * self._scale * e
            alpha = 1.0 - e
            if p >= 1.0:
                self._phase = "hidden"
                # 记下「开始收起那一刻」的序号：收起动画期间若来了新一轮（用户又按下了），
                # 那时 _update_seq 已经变大，收起结束后仍然允许再弹出来。
                self._dismissed_seq = self._hide_seq
                if self._win is not None:
                    self._win.hide()
                return
        else:
            scale, dy, alpha = 1.0, 0.0, 1.0

        # 电平：0.25 秒没来新值就当作静音（收音停止后波形自己落下去，不会僵在最后一帧）
        target_level = self._level if (now - self._level_at) <= 0.25 else 0.0
        self._smooth += (target_level - self._smooth) * 0.32
        for i in range(len(self._bars)):
            shape = 0.42 + 0.58 * math.sin(math.pi * (i + 0.5) / len(self._bars))
            wobble = 1.0 + 0.18 * math.sin(now * 6.5 + i * 0.7)
            target = min(1.0, self._smooth * shape * wobble)
            if self._state in ("arming", "recording"):
                target = max(target, 0.10 + 0.05 * math.sin(now * 3.2 + i * 0.5))
            rate = 0.45 if target > self._bars[i] else 0.12
            self._bars[i] += (target - self._bars[i]) * rate

        label = STYLE.get(self._state, STYLE["idle"])[1]
        hint = "按住 %s 说话 · 松开结束 · Esc 取消" % self.hotkey_text
        frame = r.frame(state=self._state, label=label, hint=hint, chars=self._alphas(now),
                        level=self._smooth, bars=self._bars, now=now, alpha=max(0.0, min(1.0, alpha)))

        ww = max(16, int(round(r.win_w * scale)))
        wh = max(16, int(round(r.win_h * scale)))
        if (ww, wh) != (r.win_w, r.win_h):
            frame = frame.resize((ww, wh), Image.BILINEAR)
        bx, by = self._base
        x = bx + (r.win_w - ww) // 2
        y = by + (r.win_h - wh) + int(round(dy))
        if self._win is None:
            return
        self._win.blit(frame.convert("RGBa").tobytes("raw", "BGRa"), ww, wh, x, y)
        self._win.show()

    def _run(self) -> None:
        if not HAS_PIL:
            self.log("HUD disabled: Pillow 未安装（pip install pillow）；语音输入不受影响")
            return
        try:
            _set_dpi_aware()
            self._scale = _scale_factor()
            self._renderer = _Renderer(self.panel_w, self.panel_h, self._scale, _system_theme())
            self._win = _LayeredWindow(self.log)
            self._win.create(self._renderer.win_w, self._renderer.win_h)
        except Exception as exc:
            self.log("HUD init failed: " + repr(exc))
            self._win = None
            return
        self._screen = _monitor_rect()
        self.log("HUD ready: %dx%d @%.2fx %s" % (self._renderer.win_w, self._renderer.win_h,
                                                 self._scale, self._renderer.theme_name))
        last = 0.0
        while not self._stop.is_set():
            try:
                self._win.pump()
                now = time.monotonic()
                self._drain(now)
                if self._win.on_display_change:
                    self._win.on_display_change = False
                    self._screen = _monitor_rect()
                    if self._phase != "hidden":
                        self._enter(now)
                if self._phase == "hidden":
                    if self._should_show():
                        self._enter(now)
                    else:
                        time.sleep(0.02)
                        continue
                elif self._phase == "shown":
                    if self._state in HIDE_AFTER_MS and self._hide_at is None:
                        self._hide_at = now + HIDE_AFTER_MS[self._state] / 1000.0
                    if (self._hide_at is not None and now >= self._hide_at) or \
                            (self._state == "idle" and not self._text):
                        self._hide_seq = self._update_seq
                        self._phase = "out"
                        self._phase_t0 = now
                active = self._phase != "shown" or self._state in ("arming", "recording")
                if now - last >= (FRAME_S_ACTIVE if active else FRAME_S_IDLE):
                    last = now
                    self._paint(now)
                time.sleep(0.002)
            except Exception as exc:
                self.log("HUD render error: " + repr(exc))
                time.sleep(0.2)
