"""文本投递：短文本走 SendInput Unicode，长文本走剪贴板粘贴。

剪贴板保护：
- 粘贴前后快照/还原用户剪贴板，避免占用；
- 写入时同时标记 CanIncludeInClipboardHistory=0 与 ExcludeClipboardContentFromMonitorProcessing，
  防止听写内容进入 Windows 剪贴板历史或第三方剪贴板管理器。
0.0.1 缺口：UIA 密码框识别、TSF 内联合成（见 docs/ROADMAP.md）。"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time
from typing import Callable, List, Optional

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_CONTROL = 0x11
VK_RETURN = 0x0D
VK_V = 0x56


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _U)]


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> _INPUT:
    item = _INPUT()
    item.type = INPUT_KEYBOARD
    item.ki.wVk = vk
    item.ki.wScan = scan
    item.ki.dwFlags = flags
    return item


def _send(items: List[_INPUT]) -> None:
    size = ctypes.sizeof(_INPUT)
    array = (_INPUT * len(items))(*items)
    ctypes.windll.user32.SendInput(len(items), ctypes.byref(array), size)


def send_unicode_text(text: str) -> None:
    """逐字注入。只用于短文本：长文本走剪贴板更快，也避免部分应用丢字。"""
    for ch in text:
        if ch == chr(10):
            _send([_key(vk=VK_RETURN), _key(vk=VK_RETURN, flags=KEYEVENTF_KEYUP)])
            continue
        code = ord(ch)
        _send([_key(scan=code, flags=KEYEVENTF_UNICODE)])
        _send([_key(scan=code, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)])
        time.sleep(0.001)


def send_paste() -> None:
    _send([_key(vk=VK_CONTROL), _key(vk=VK_V),
           _key(vk=VK_V, flags=KEYEVENTF_KEYUP), _key(vk=VK_CONTROL, flags=KEYEVENTF_KEYUP)])


def foreground_app_exe() -> str:
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        import psutil
        return (psutil.Process(pid.value).name() or "").lower()
    except Exception:
        return ""


class Clipboard:
    """只在必要时短暂占用剪贴板；两个排除标记让听写内容不进剪贴板历史。"""
    EXCLUDE_FORMATS = ("CanIncludeInClipboardHistory", "ExcludeClipboardContentFromMonitorProcessing")

    @staticmethod
    def read_text() -> str:
        try:
            import win32clipboard as cb
        except Exception:
            return ""
        for _ in range(5):
            try:
                cb.OpenClipboard()
                try:
                    if cb.IsClipboardFormatAvailable(cb.CF_UNICODETEXT):
                        return cb.GetClipboardData(cb.CF_UNICODETEXT) or ""
                    return ""
                finally:
                    cb.CloseClipboard()
            except Exception:
                time.sleep(0.02)
        return ""

    @staticmethod
    def write_text(text: str, exclude_from_history: bool = True) -> bool:
        try:
            import win32clipboard as cb
        except Exception:
            return False
        marker = (chr(0) * 4).encode("ascii")
        for _ in range(5):
            try:
                cb.OpenClipboard()
                try:
                    cb.EmptyClipboard()
                    cb.SetClipboardData(cb.CF_UNICODETEXT, text)
                    if exclude_from_history:
                        for name in Clipboard.EXCLUDE_FORMATS:
                            try:
                                cb.SetClipboardData(cb.RegisterClipboardFormat(name), marker)
                            except Exception:
                                pass
                    return True
                finally:
                    cb.CloseClipboard()
            except Exception:
                time.sleep(0.02)
        return False


class TextDelivery:
    def __init__(self, paste_threshold_chars: int = 30, restore_clipboard: bool = True,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.paste_threshold_chars = int(paste_threshold_chars)
        self.restore_clipboard = bool(restore_clipboard)
        self.log = log or (lambda m: None)

    def deliver(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return "empty"
        if len(text) <= self.paste_threshold_chars:
            send_unicode_text(text)
            return "sendinput"
        previous = Clipboard.read_text() if self.restore_clipboard else ""
        if not Clipboard.write_text(text, exclude_from_history=True):
            send_unicode_text(text)
            return "sendinput-fallback"
        time.sleep(0.02)
        send_paste()
        if self.restore_clipboard and previous:
            time.sleep(0.15)
            Clipboard.write_text(previous, exclude_from_history=True)
        return "clipboard"


from typefast.platform.winapi import apply_prototypes
apply_prototypes()
