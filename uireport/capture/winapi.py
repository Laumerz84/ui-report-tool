"""Thin Win32 helpers (ctypes). Owner: capture builder (package A).

Everything here is import-safe on any platform (the Win32 handles are only created on
Windows) and every helper degrades to a harmless result instead of raising, because the
capture flow must never crash the tray app.

Extra (non-contract) helpers used by the other capture modules:
    set_noactivate_clickthrough, force_foreground, set_window_rect_physical,
    get_window_rect, get_dpi_for_window, window_dpi_awareness, get_dpi_for_monitor,
    screen_size_metrics
"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from typing import Optional

IS_WINDOWS = sys.platform == "win32"

# ---- constants ---------------------------------------------------------------------
WDA_NONE = 0x0
WDA_EXCLUDEFROMCAPTURE = 0x11

WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
GWL_EXSTYLE = -20

HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_NOOWNERZORDER = 0x0200

VK_ESCAPE = 0x1B

DPI_AWARENESS_UNAWARE = 0
DPI_AWARENESS_SYSTEM_AWARE = 1
DPI_AWARENESS_PER_MONITOR_AWARE = 2

_DPI_CONTEXT_PER_MONITOR_AWARE_V2 = -4
_DPI_CONTEXT_PER_MONITOR_AWARE = -3
_E_ACCESSDENIED = -2147024891  # 0x80070005 as a signed HRESULT

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_CXSCREEN = 0
SM_CYSCREEN = 1


def _load(name: str):
    try:
        return ctypes.WinDLL(name, use_last_error=True)
    except (OSError, AttributeError):  # not Windows / DLL missing
        return None


user32 = _load("user32") if IS_WINDOWS else None
shcore = _load("shcore") if IS_WINDOWS else None
dwmapi = _load("dwmapi") if IS_WINDOWS else None
kernel32 = _load("kernel32") if IS_WINDOWS else None

_HWND = ctypes.c_void_p


def _sig(lib, name: str, restype, argtypes) -> None:
    """Attach a prototype to lib.<name> if the export exists (older Windows lacks some)."""
    if lib is None:
        return
    try:
        fn = getattr(lib, name)
    except AttributeError:
        return
    fn.restype = restype
    fn.argtypes = argtypes


if user32 is not None:
    _sig(user32, "SetProcessDpiAwarenessContext", wintypes.BOOL, [ctypes.c_void_p])
    _sig(user32, "GetThreadDpiAwarenessContext", ctypes.c_void_p, [])
    _sig(user32, "AreDpiAwarenessContextsEqual", wintypes.BOOL, [ctypes.c_void_p, ctypes.c_void_p])
    _sig(user32, "GetAwarenessFromDpiAwarenessContext", ctypes.c_int, [ctypes.c_void_p])
    _sig(user32, "GetWindowDpiAwarenessContext", ctypes.c_void_p, [_HWND])
    _sig(user32, "GetDpiForWindow", wintypes.UINT, [_HWND])
    _sig(user32, "SetWindowDisplayAffinity", wintypes.BOOL, [_HWND, wintypes.DWORD])
    _sig(user32, "GetCursorPos", wintypes.BOOL, [ctypes.POINTER(wintypes.POINT)])
    _sig(user32, "GetAsyncKeyState", ctypes.c_short, [ctypes.c_int])
    _sig(user32, "GetWindowLongPtrW", ctypes.c_ssize_t, [_HWND, ctypes.c_int])
    _sig(user32, "SetWindowLongPtrW", ctypes.c_ssize_t, [_HWND, ctypes.c_int, ctypes.c_ssize_t])
    _sig(user32, "GetForegroundWindow", _HWND, [])
    _sig(user32, "SetForegroundWindow", wintypes.BOOL, [_HWND])
    _sig(user32, "BringWindowToTop", wintypes.BOOL, [_HWND])
    _sig(user32, "SetFocus", _HWND, [_HWND])
    _sig(user32, "GetWindowThreadProcessId", wintypes.DWORD, [_HWND, ctypes.POINTER(wintypes.DWORD)])
    _sig(user32, "AttachThreadInput", wintypes.BOOL, [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL])
    _sig(user32, "SetWindowPos", wintypes.BOOL,
         [_HWND, _HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT])
    _sig(user32, "GetWindowRect", wintypes.BOOL, [_HWND, ctypes.POINTER(wintypes.RECT)])
    _sig(user32, "IsWindow", wintypes.BOOL, [_HWND])
    _sig(user32, "GetSystemMetrics", ctypes.c_int, [ctypes.c_int])
if kernel32 is not None:
    _sig(kernel32, "GetCurrentThreadId", wintypes.DWORD, [])
if dwmapi is not None:
    _sig(dwmapi, "DwmFlush", ctypes.c_long, [])
if shcore is not None:
    _sig(shcore, "SetProcessDpiAwareness", ctypes.c_long, [ctypes.c_int])
    _sig(shcore, "GetProcessDpiAwareness", ctypes.c_long, [wintypes.HANDLE, ctypes.POINTER(ctypes.c_int)])
    _sig(shcore, "GetDpiForMonitor", ctypes.c_long,
         [wintypes.HANDLE, ctypes.c_int, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)])


def _ctx(value: int) -> ctypes.c_void_p:
    return ctypes.c_void_p(value)


# ---- DPI awareness -----------------------------------------------------------------
def _current_awareness() -> Optional[int]:
    """DPI_AWARENESS_* of the current thread/process, or None if it cannot be determined."""
    if user32 is not None:
        try:
            ctx = user32.GetThreadDpiAwarenessContext()
            val = int(user32.GetAwarenessFromDpiAwarenessContext(ctx))
            if val >= 0:
                return val
        except (AttributeError, OSError):
            pass
    if shcore is not None:
        try:
            out = ctypes.c_int(-1)
            if shcore.GetProcessDpiAwareness(None, ctypes.byref(out)) == 0 and out.value >= 0:
                return int(out.value)
        except (AttributeError, OSError):
            pass
    return None


def enable_per_monitor_dpi_awareness() -> bool:
    """Make the process Per-Monitor-DPI-Aware V2 (fallback: V1 via shcore, then system).

    Must run BEFORE any window / QApplication exists. Idempotent: if the process is
    already PMv2 (Qt 6 sets that itself) or PMv1 it returns True without touching anything.
    Returns True when the process ends up per-monitor aware, else False."""
    if not IS_WINDOWS or user32 is None:
        return False
    # 1) already PMv2 (Qt 6 / manifest)?  -> success
    try:
        cur = user32.GetThreadDpiAwarenessContext()
        if user32.AreDpiAwarenessContextsEqual(cur, _ctx(_DPI_CONTEXT_PER_MONITOR_AWARE_V2)):
            return True
    except (AttributeError, OSError):
        pass
    # 2) set PMv2 (Windows 10 1703+)
    try:
        if user32.SetProcessDpiAwarenessContext(_ctx(_DPI_CONTEXT_PER_MONITOR_AWARE_V2)):
            return True
    except (AttributeError, OSError):
        pass
    if _current_awareness() == DPI_AWARENESS_PER_MONITOR_AWARE:
        return True  # "access denied" because it was already set (PMv1 or PMv2)
    # 3) PMv1 through shcore (Windows 8.1+)
    if shcore is not None:
        try:
            hr = int(shcore.SetProcessDpiAwareness(DPI_AWARENESS_PER_MONITOR_AWARE))
            if hr == 0:
                return True
            if hr == _E_ACCESSDENIED and _current_awareness() == DPI_AWARENESS_PER_MONITOR_AWARE:
                return True
        except (AttributeError, OSError):
            pass
    # 4) last resort: system aware (not per-monitor -> report False)
    try:
        user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass
    return _current_awareness() == DPI_AWARENESS_PER_MONITOR_AWARE


def window_dpi_awareness(hwnd: int) -> int:
    """DPI_AWARENESS_* of the process owning `hwnd` (0 unaware, 1 system, 2 per-monitor).
    Returns 2 when it cannot be determined (Windows < 10 1607) so callers use GetDpiForWindow."""
    if user32 is None:
        return DPI_AWARENESS_PER_MONITOR_AWARE
    try:
        ctx = user32.GetWindowDpiAwarenessContext(hwnd)
        val = int(user32.GetAwarenessFromDpiAwarenessContext(ctx))
        return val if val >= 0 else DPI_AWARENESS_PER_MONITOR_AWARE
    except (AttributeError, OSError):
        return DPI_AWARENESS_PER_MONITOR_AWARE


def get_dpi_for_window(hwnd: int) -> int:
    """GetDpiForWindow(hwnd); 0 if unavailable."""
    if user32 is None:
        return 0
    try:
        return int(user32.GetDpiForWindow(hwnd))
    except (AttributeError, OSError):
        return 0


def get_dpi_for_monitor(hmonitor: int) -> int:
    """Effective DPI (MDT_EFFECTIVE_DPI) of a monitor handle; 96 if it cannot be read."""
    if shcore is None:
        return 96
    try:
        dx, dy = wintypes.UINT(0), wintypes.UINT(0)
        if shcore.GetDpiForMonitor(hmonitor, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0 and dx.value > 0:
            return int(dx.value)
    except (AttributeError, OSError):
        pass
    return 96


# ---- capture exclusion / window styles ----------------------------------------------
def exclude_from_capture(hwnd: int, exclude: bool = True) -> bool:
    """SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE=0x11) (or WDA_NONE when
    exclude=False). Keeps that window out of screen grabs (Win10 2004+). Returns False if
    unsupported or the call failed; never raises. Callers pass int(widget.winId())."""
    if user32 is None or not hwnd:
        return False
    try:
        return bool(user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE if exclude else WDA_NONE))
    except (AttributeError, OSError, ctypes.ArgumentError, TypeError):
        return False


def widget_hwnd(widget: object) -> int:
    """int(widget.winId()) - creates the native window if needed."""
    return int(widget.winId())  # type: ignore[attr-defined]


def get_ex_style(hwnd: int) -> int:
    if user32 is None or not hwnd:
        return 0
    try:
        return int(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)) & 0xFFFFFFFF
    except (AttributeError, OSError):
        return 0


def set_noactivate_clickthrough(hwnd: int) -> bool:
    """OR WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT into the extended style
    (keeps every existing bit, e.g. WS_EX_LAYERED). The window can then neither take
    activation/focus nor receive mouse input. Returns True if the bits are set afterwards."""
    if user32 is None or not hwnd:
        return False
    want = WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT
    try:
        cur = get_ex_style(hwnd)
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, cur | want)
        return (get_ex_style(hwnd) & want) == want
    except (AttributeError, OSError):
        return False


def get_window_rect(hwnd: int) -> Optional[tuple[int, int, int, int]]:
    """(x, y, w, h) of GetWindowRect (physical pixels under PMv2) or None."""
    if user32 is None or not hwnd:
        return None
    try:
        r = wintypes.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    except (AttributeError, OSError):
        pass
    return None


def set_window_rect_physical(hwnd: int, x: int, y: int, w: int, h: int, topmost: bool = True) -> bool:
    """Place a top-level window at an exact PHYSICAL rectangle (PMv2 process), without
    activating it. Used to guarantee pixel-exact selector overlays regardless of Qt's
    logical-pixel rounding on scaled monitors."""
    if user32 is None or not hwnd:
        return False
    try:
        return bool(
            user32.SetWindowPos(
                hwnd, _HWND(HWND_TOPMOST if topmost else 0), int(x), int(y), int(w), int(h),
                SWP_NOACTIVATE | SWP_NOOWNERZORDER,
            )
        )
    except (AttributeError, OSError, ctypes.ArgumentError, TypeError):
        return False


def force_foreground(hwnd: int) -> bool:
    """Give keyboard focus to our own window even when Windows' foreground lock would
    refuse: attach our input queue to the current foreground thread (no synthetic input is
    sent), raise and activate, then detach. Best effort; returns True if `hwnd` is now the
    foreground window."""
    if user32 is None or not hwnd:
        return False
    try:
        fg = user32.GetForegroundWindow()
        if fg == hwnd:
            return True
        cur_tid = int(kernel32.GetCurrentThreadId())
        fg_tid = int(user32.GetWindowThreadProcessId(fg, None)) if fg else 0
        attached = False
        if fg_tid and fg_tid != cur_tid:
            attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(cur_tid, fg_tid, False)
        return user32.GetForegroundWindow() == hwnd
    except (AttributeError, OSError, ctypes.ArgumentError, TypeError):
        return False


def is_window(hwnd: int) -> bool:
    if user32 is None or not hwnd:
        return False
    try:
        return bool(user32.IsWindow(hwnd))
    except (AttributeError, OSError):
        return False


# ---- compositor / input state ---------------------------------------------------------
def dwm_flush() -> None:
    """DwmFlush(): block until the compositor presented a frame (so a just-hidden window
    is really gone before we grab). Never raises."""
    if dwmapi is None:
        return
    try:
        dwmapi.DwmFlush()
    except (AttributeError, OSError):
        pass


def cursor_pos() -> tuple[int, int]:
    """Cursor position in virtual-screen PHYSICAL pixels (GetCursorPos under PMv2)."""
    if user32 is None:
        return (0, 0)
    try:
        pt = wintypes.POINT()
        if user32.GetCursorPos(ctypes.byref(pt)):
            return (int(pt.x), int(pt.y))
    except (AttributeError, OSError):
        pass
    return (0, 0)


def is_key_down(vk: int) -> bool:
    """GetAsyncKeyState(vk) & 0x8000."""
    if user32 is None:
        return False
    try:
        return bool(user32.GetAsyncKeyState(int(vk)) & 0x8000)
    except (AttributeError, OSError):
        return False


def screen_size_metrics() -> tuple[int, int, int, int]:
    """(x, y, w, h) of the virtual screen from GetSystemMetrics (physical px under PMv2);
    falls back to the primary screen size."""
    if user32 is None:
        return (0, 0, 0, 0)
    try:
        x = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
        y = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
        w = int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
        h = int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
        if w > 0 and h > 0:
            return (x, y, w, h)
        return (0, 0, int(user32.GetSystemMetrics(SM_CXSCREEN)), int(user32.GetSystemMetrics(SM_CYSCREEN)))
    except (AttributeError, OSError):
        return (0, 0, 0, 0)


def own_pid() -> int:
    return os.getpid()
