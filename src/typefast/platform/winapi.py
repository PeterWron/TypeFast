"""集中声明用到的 Win32 函数原型。

ctypes 默认把返回值当 32 位 int，64 位下句柄（HWND / HMODULE）会被截断：
GetModuleHandleW 返回的模块句柄一旦被截断，SetWindowsHookEx 就会报 126（ERROR_MOD_NOT_FOUND）。
所有涉及句柄的调用都在这里声明 restype / argtypes，其它模块 import 后调用 apply_prototypes()。"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

LRESULT = ctypes.c_ssize_t
APPLIED = False


def apply_prototypes() -> None:
    global APPLIED
    if APPLIED:
        return
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]

    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, wt.DWORD]
    user32.UnhookWindowsHookEx.restype = wt.BOOL
    user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    user32.CallNextHookEx.restype = LRESULT
    user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM]
    user32.GetMessageW.restype = wt.BOOL
    user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), ctypes.c_void_p, wt.UINT, wt.UINT]

    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetForegroundWindow.argtypes = []
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]

    user32.GetWindowLongW.restype = ctypes.c_long
    user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.SetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]

    user32.SendInput.restype = wt.UINT
    user32.SendInput.argtypes = [wt.UINT, ctypes.c_void_p, ctypes.c_int]

    APPLIED = True
