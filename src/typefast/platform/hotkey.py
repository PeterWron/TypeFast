"""全局按住说话（PTT）：低级键盘/鼠标钩子。

为什么不用 RegisterHotKey：它拿不到成对的抬起事件，实现不了按住说话。
钩子回调里只做最小工作，必须尽快返回，否则 Windows 会因超时把钩子摘掉。"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
from typing import Callable, Optional

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208

VK = {
    "esc": 0x1B, "tab": 0x09, "space": 0x20, "enter": 0x0D,
    "left_ctrl": 0xA2, "right_ctrl": 0xA3, "left_alt": 0xA4, "right_alt": 0xA5,
    "left_shift": 0xA0, "right_shift": 0xA1, "left_win": 0x5B, "right_win": 0x5C,
    "caps_lock": 0x14, "f8": 0x77, "f9": 0x78, "f10": 0x79,
    "middle_mouse": 0x04,
}


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class PushToTalkHook:
    """按住 hold_key 说话：按下触发 on_press，抬起触发 on_release，按住时按 cancel_key 触发 on_cancel。"""

    def __init__(self, hold_key: str = "right_ctrl", cancel_key: str = "esc", mouse_middle: bool = True,
                 on_press: Optional[Callable[[], None]] = None,
                 on_release: Optional[Callable[[], None]] = None,
                 on_cancel: Optional[Callable[[], None]] = None,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self.hold_vk = VK.get(hold_key, 0xA3)
        self.cancel_vk = VK.get(cancel_key, 0x1B)
        self.mouse_middle = mouse_middle
        self.on_press = on_press or (lambda: None)
        self.on_release = on_release or (lambda: None)
        self.on_cancel = on_cancel or (lambda: None)
        self.log = log or (lambda m: None)
        self._user32 = ctypes.windll.user32
        self._holding = False
        self._thread: Optional[threading.Thread] = None
        self._kb_hook = None
        self._ms_hook = None
        self._kb_cb = None
        self._ms_cb = None
        self._hmod = None

    def start(self) -> bool:
        self._thread = threading.Thread(target=self._run, name="typefast-hooks", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        try:
            if self._kb_hook:
                self._user32.UnhookWindowsHookEx(self._kb_hook)
            if self._ms_hook:
                self._user32.UnhookWindowsHookEx(self._ms_hook)
        except Exception:
            pass

    def _run(self) -> None:
        proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)
        self._kb_cb = proc_type(self._kb_proc)
        self._ms_cb = proc_type(self._ms_proc)
        self._hmod = None  # 低位钩子不需要模块句柄，传 NULL 更安全
        self._kb_hook = self._user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_cb, None, 0)
        if not self._kb_hook:
            self.log("keyboard hook install failed (err=%d)" % ctypes.get_last_error())
            return
        if self.mouse_middle:
            self._ms_hook = self._user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms_cb, None, 0)
        self.log("hooks installed")
        msg = wt.MSG()
        while self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            self._user32.TranslateMessage(ctypes.byref(msg))
            self._user32.DispatchMessageW(ctypes.byref(msg))

    def _begin(self) -> None:
        if self._holding:
            return
        self._holding = True
        try:
            self.on_press()
        except Exception as exc:
            self.log("on_press error: " + repr(exc))

    def _end(self) -> None:
        if not self._holding:
            return
        self._holding = False
        try:
            self.on_release()
        except Exception as exc:
            self.log("on_release error: " + repr(exc))

    def _cancel(self) -> None:
        if not self._holding:
            return
        self._holding = False
        try:
            self.on_cancel()
        except Exception as exc:
            self.log("on_cancel error: " + repr(exc))

    def _kb_proc(self, n_code, w_param, l_param):
        try:
            if n_code >= 0:
                info = ctypes.cast(l_param, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                vk = int(info.vkCode)
                if vk == self.hold_vk and w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    self._begin()
                elif vk == self.hold_vk and w_param in (WM_KEYUP, WM_SYSKEYUP):
                    self._end()
                elif vk == self.cancel_vk and w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    self._cancel()
        except Exception:
            pass
        return self._user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _ms_proc(self, n_code, w_param, l_param):
        try:
            if n_code >= 0:
                if w_param == WM_MBUTTONDOWN:
                    self._begin()
                elif w_param == WM_MBUTTONUP:
                    self._end()
        except Exception:
            pass
        return self._user32.CallNextHookEx(None, n_code, w_param, l_param)


from typefast.platform.winapi import apply_prototypes
apply_prototypes()
