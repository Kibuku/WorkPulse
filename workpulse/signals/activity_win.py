"""
activity_win.py — Windows backend for the activity tracker.

Provides foreground_window() + idle_seconds() against the Win32 API via
ctypes. No third-party libraries; depends only on user32.dll and
kernel32.dll (always present on Windows).

This module is imported only when sys.platform == "win32"; see activity.py
for the platform switch.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes


user32   = ctypes.WinDLL("user32",   use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


user32.GetForegroundWindow.restype       = wintypes.HWND
user32.GetWindowTextLengthW.argtypes     = [wintypes.HWND]
user32.GetWindowTextLengthW.restype      = ctypes.c_int
user32.GetWindowTextW.argtypes           = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype            = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype  = wintypes.DWORD
user32.GetLastInputInfo.argtypes         = [ctypes.POINTER(_LASTINPUTINFO)]
user32.GetLastInputInfo.restype          = wintypes.BOOL

kernel32.GetTickCount.restype            = wintypes.DWORD


def foreground_window() -> tuple[str, int, str] | None:
    """Return (title, pid, app_name) of the foreground window, or None.

    Windows doesn't expose a clean app-name primitive at this level, so
    app_name is returned as an empty string and activity.py's psutil
    fallback path remains primary. Kept for API parity with activity_mac.
    """
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return buf.value, pid.value, ""


def idle_seconds() -> float:
    """Seconds since the last mouse or keyboard input. 0.0 on failure."""
    lii = _LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(lii)
    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    return (kernel32.GetTickCount() - lii.dwTime) / 1000.0
