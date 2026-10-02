"""Foreground / top-level window information and best-effort browser URL.
Owner: capture builder (package A).

Privacy: this module never logs or persists window titles or URLs; it only returns them.
"""
from __future__ import annotations

import ctypes
import ipaddress
import os
import re
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from ..geometry import IntRect
from ..models import MonitorMeta, WindowMeta, monitor_for_rect
from . import winapi

# ---- constants -------------------------------------------------------------------------
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
GA_ROOT = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

MENU_CLASS = "#32768"  # native popup menus are pickable even though they are tool windows
_SHELL_CLASSES = frozenset({"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW"})
_UWP_HOST = "applicationframehost.exe"

_BROWSER_PROCESSES = {"chrome": "chrome", "msedge": "edge", "firefox": "firefox"}


@dataclass(frozen=True)
class WindowSnapshot:
    """A top-level window as it was when the screen was frozen (used for click-a-window)."""

    hwnd: int
    rect: IntRect  # visible frame in virtual-screen physical px (DWMWA_EXTENDED_FRAME_BOUNDS)
    title: str
    pid: int


@dataclass(frozen=True)
class RawWindow:
    """Unfiltered facts about one top-level window (input of `filter_windows`)."""

    hwnd: int
    visible: bool
    iconic: bool
    cloaked: bool
    ex_style: int
    class_name: str
    title: str
    pid: int
    rect: IntRect


# ---- Win32 plumbing --------------------------------------------------------------------
_user32 = winapi.user32
_dwm = winapi.dwmapi
_kernel32 = winapi.kernel32

_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

if _user32 is not None:
    _user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.EnumChildWindows.argtypes = [wintypes.HWND, _WNDENUMPROC, wintypes.LPARAM]
    _user32.EnumChildWindows.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.IsIconic.restype = wintypes.BOOL
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetAncestor.restype = wintypes.HWND
    _user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    _user32.MonitorFromWindow.restype = wintypes.HANDLE
if _dwm is not None:
    _dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    _dwm.DwmGetWindowAttribute.restype = ctypes.c_long
if _kernel32 is not None:
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL


def _text(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    n = _user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value if n > 0 else ""


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    n = _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value if n > 0 else ""


def _pid_of(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _is_cloaked(hwnd: int) -> bool:
    if _dwm is None:
        return False
    val = wintypes.DWORD(0)
    try:
        if _dwm.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val)) == 0:
            return val.value != 0
    except (AttributeError, OSError):
        pass
    return False


def frame_rect(hwnd: int) -> Optional[IntRect]:
    """Visible frame of a window in physical virtual-screen pixels: DWMWA_EXTENDED_FRAME_BOUNDS
    (excludes the invisible resize border), falling back to GetWindowRect."""
    if _dwm is not None:
        r = wintypes.RECT()
        try:
            if _dwm.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) == 0:
                if r.right > r.left and r.bottom > r.top:
                    return IntRect(r.left, r.top, r.right - r.left, r.bottom - r.top)
        except (AttributeError, OSError):
            pass
    wr = winapi.get_window_rect(hwnd)
    if wr and wr[2] > 0 and wr[3] > 0:
        return IntRect(*wr)
    return None


def _enum_raw_windows() -> list[RawWindow]:
    if _user32 is None:
        return []
    out: list[RawWindow] = []

    def _cb(hwnd: int, _lp: int) -> int:
        try:
            visible = bool(_user32.IsWindowVisible(hwnd))
            if not visible:
                return 1
            iconic = bool(_user32.IsIconic(hwnd))
            cloaked = _is_cloaked(hwnd) if not iconic else False
            ex = winapi.get_ex_style(hwnd)
            cls = _class_name(hwnd)
            rect = frame_rect(hwnd) or IntRect()
            out.append(
                RawWindow(
                    hwnd=int(hwnd), visible=True, iconic=iconic, cloaked=cloaked, ex_style=ex,
                    class_name=cls, title=_text(hwnd), pid=_pid_of(hwnd), rect=rect,
                )
            )
        except Exception:  # noqa: BLE001 - one odd window must not break the whole snapshot
            pass
        return 1

    cb = _WNDENUMPROC(_cb)
    _user32.EnumWindows(cb, 0)
    return out


def filter_windows(raw: Sequence[RawWindow], exclude_pid: Optional[int] = None) -> list[WindowSnapshot]:
    """Pure filter used by snapshot_top_level_windows (keeps the given z-order).

    Keeps visible, non-minimised, non-cloaked windows with a non-empty rect and a title.
    Tool windows are dropped, except native popup menus (class #32768) so a click can pick
    an open menu. Click-through layered overlays of other programs are dropped too."""
    result: list[WindowSnapshot] = []
    for w in raw:
        if not w.visible or w.iconic or w.cloaked:
            continue
        if exclude_pid is not None and w.pid == exclude_pid:
            continue
        if w.rect.is_empty:
            continue
        is_menu = w.class_name == MENU_CLASS
        if not is_menu:
            if w.ex_style & winapi.WS_EX_TOOLWINDOW:
                continue
            if not w.title.strip():
                continue
        if (w.ex_style & winapi.WS_EX_TRANSPARENT) and (w.ex_style & winapi.WS_EX_LAYERED):
            continue  # click-through overlay (e.g. a game-overlay layer)
        result.append(WindowSnapshot(hwnd=w.hwnd, rect=w.rect, title=w.title if not is_menu else (w.title or "(menu)"), pid=w.pid))
    return result


def snapshot_top_level_windows(exclude_pid: Optional[int] = None) -> list[WindowSnapshot]:
    """Visible, non-minimised, non-cloaked, non-tool top-level windows with a non-empty
    rect and a title, in z-order (TOPMOST FIRST). Windows of `exclude_pid` (our own
    process: overlays, editor) are omitted."""
    return filter_windows(_enum_raw_windows(), exclude_pid)


def window_at_point(windows: list[WindowSnapshot], x: int, y: int) -> Optional[WindowSnapshot]:
    """First (= topmost) snapshot whose rect contains the virtual-screen point."""
    for w in windows:
        if w.rect.contains_point(x, y):
            return w
    return None


def dominant_window(windows: list[WindowSnapshot], rect: IntRect, grid: int = 24) -> Optional[WindowSnapshot]:
    """The window that shows the most of `rect` (virtual-screen px), honouring z-order.

    Hit-tests a grid x grid lattice of points inside `rect` against the snapshot (topmost
    first) and returns the window hit most often; ties go to the higher window. None when
    no window covers any sampled point or `rect` is empty."""
    if rect.is_empty or not windows:
        return None
    counts: dict[int, int] = {}
    for j in range(grid):
        y = rect.y + int((j + 0.5) * rect.h / grid)
        for i in range(grid):
            x = rect.x + int((i + 0.5) * rect.w / grid)
            w = window_at_point(windows, x, y)
            if w is not None:
                k = windows.index(w)
                counts[k] = counts.get(k, 0) + 1
    if not counts:
        return None
    best = min(counts, key=lambda k: (-counts[k], k))
    return windows[best]


def foreground_hwnd(exclude_pid: Optional[int] = None) -> Optional[int]:
    """Handle of the foreground window; if it belongs to `exclude_pid` (our own editor)
    return the topmost visible window of another process instead."""
    if _user32 is None:
        return None
    try:
        fg = _user32.GetForegroundWindow()
        if fg:
            root = _user32.GetAncestor(fg, GA_ROOT) or fg
            root = int(root)
            usable = (
                bool(_user32.IsWindowVisible(root))
                and not _user32.IsIconic(root)
                and (exclude_pid is None or _pid_of(root) != exclude_pid)
                and _class_name(root) not in _SHELL_CLASSES
            )
            if usable:
                return root
        for w in snapshot_top_level_windows(exclude_pid):
            if _class_name(w.hwnd) not in _SHELL_CLASSES:
                return w.hwnd
    except Exception:  # noqa: BLE001
        pass
    return None


def browser_kind(process_name: str) -> Optional[str]:
    """'chrome' | 'edge' | 'firefox' from a process name such as 'msedge.exe', else None."""
    name = (process_name or "").strip().lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return _BROWSER_PROCESSES.get(name)


# ---- process facts ------------------------------------------------------------------------
def _exe_path_of_pid(pid: int) -> str:
    if _kernel32 is not None and pid:
        try:
            h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if h:
                try:
                    size = wintypes.DWORD(1024)
                    buf = ctypes.create_unicode_buffer(1024)
                    if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                        return buf.value
                finally:
                    _kernel32.CloseHandle(h)
        except (AttributeError, OSError):
            pass
    try:
        import psutil

        return psutil.Process(pid).exe()
    except Exception:  # noqa: BLE001 - AccessDenied, NoSuchProcess, ImportError
        return ""


def _process_name_of_pid(pid: int, exe_path: str) -> str:
    if exe_path:
        return os.path.basename(exe_path)
    try:
        import psutil

        return psutil.Process(pid).name()
    except Exception:  # noqa: BLE001
        return ""


def _resolve_uwp_pid(hwnd: int, host_pid: int) -> int:
    """UWP apps run inside ApplicationFrameHost.exe; the real app owns a child window of a
    different process. Returns that pid, or host_pid when nothing better is found."""
    found: list[int] = []

    def _cb(child: int, _lp: int) -> int:
        try:
            p = _pid_of(child)
            if p and p != host_pid:
                found.append(p)
                return 0
        except Exception:  # noqa: BLE001
            pass
        return 1

    try:
        cb = _WNDENUMPROC(_cb)
        _user32.EnumChildWindows(hwnd, cb, 0)
    except (AttributeError, OSError):
        return host_pid
    return found[0] if found else host_pid


def choose_window_dpi(awareness: int, window_dpi: int, monitor_dpi: int) -> int:
    """DPI used to derive a window's logical size (see CONTRACT section 4.3).

    Per-monitor-aware windows report their own DPI (GetDpiForWindow). System-aware and
    DPI-unaware windows are bitmap-stretched by Windows to the monitor they sit on, so their
    logical (DIP) size is physical / monitor scale -> use the monitor DPI."""
    if awareness == winapi.DPI_AWARENESS_PER_MONITOR_AWARE and window_dpi and window_dpi > 0:
        return int(window_dpi)
    return int(monitor_dpi) if monitor_dpi and monitor_dpi > 0 else 96


def window_meta_from_hwnd(hwnd: int, monitors: list[MonitorMeta]) -> Optional[WindowMeta]:
    """Title, process name, exe path, pid, visible-frame rect (physical px), effective dpi
    and browser kind for `hwnd`. `url` is left None (see read_browser_url). Returns None
    if the window vanished. DPI rule: dpi = GetDpiForWindow(hwnd) when the window is
    DPI-aware; for a DPI-UNAWARE window use the DPI of the monitor holding most of it
    (Windows bitmap-stretches those, so their own logical size is physical / monitor scale)."""
    if _user32 is None or not hwnd or not winapi.is_window(hwnd):
        return None
    try:
        rect = frame_rect(hwnd)
        if rect is None:
            return None
        pid = _pid_of(hwnd)
        real_pid = pid
        exe = _exe_path_of_pid(pid)
        if os.path.basename(exe).lower() == _UWP_HOST:
            real_pid = _resolve_uwp_pid(hwnd, pid)
            if real_pid != pid:
                exe = _exe_path_of_pid(real_pid) or exe
        name = _process_name_of_pid(real_pid, exe)
        mon = monitor_for_rect(monitors, rect) if monitors else None
        mon_dpi = mon.dpi if mon else 96
        dpi = choose_window_dpi(winapi.window_dpi_awareness(hwnd), winapi.get_dpi_for_window(hwnd), mon_dpi)
        return WindowMeta(
            title=_text(hwnd),
            process_name=name,
            exe_path=exe,
            pid=real_pid,
            rect=rect,
            dpi=dpi,
            browser=browser_kind(name),
            url=None,
        )
    except Exception:  # noqa: BLE001
        return None


# ---- browser URL (UI Automation) -----------------------------------------------------------
_UIA_NAME_PROP = 30005
_UIA_CONTROLTYPE_PROP = 30003
_UIA_AUTOMATIONID_PROP = 30011
_UIA_CLASSNAME_PROP = 30012
_UIA_VALUE_PROP = 30045  # UIA_ValueValuePropertyId
_UIA_EDIT_TYPE = 50004
_UIA_SCOPE_DESCENDANTS = 4
_MATCH_SUBSTRING_IGNORE_CASE = 0x3  # PropertyConditionFlags_MatchSubstring | PropertyConditionFlags_IgnoreCase

# The address bar is an Edit control. Chrome/Edge: class 'OmniboxViewViews' (locale independent),
# name 'Address and search bar'; Firefox: automation id 'urlbar-input', name '... or enter address'.
_CHROMIUM_CLASS = "OmniboxViewViews"
_FIREFOX_AUTOMATION_ID = "urlbar-input"
_NAME_HINTS = ("address and search bar", "or enter address")

_LOCAL_HOST_RE = re.compile(r"^(localhost|[^/]*\.localhost|[^/]*\.local|[^/]*\.test)(:\d+)?(/|$|\?|#)", re.I)
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:(//|[^\d\s/]|$)")
_INVISIBLE_MARKS = ("\u200e", "\u200f", "\u202a", "\u202c")


def normalize_browser_url(value: Optional[str]) -> Optional[str]:
    """Turn an address-bar string into a URL, or None when it is not one.

    Browsers hide the scheme (Chrome/Edge hide 'https://'), so a bare 'example.com/x' gets
    'https://'. Loopback / LAN / .localhost hosts get 'http://' because dev servers rarely
    use TLS. Text with spaces (a search being typed), a lone word or an e-mail address is
    not a URL; an empty bar gives None."""
    if not value:
        return None
    v = value.strip()
    for mark in _INVISIBLE_MARKS:
        v = v.replace(mark, "")
    if not v or len(v) > 4096 or any(c.isspace() for c in v):
        return None
    if _SCHEME_RE.match(v) and not re.match(r"^[^/:]+:\d+(/|$)", v):  # 'localhost:3000' is not a scheme
        return v
    host = re.split(r"[/?#]", v, maxsplit=1)[0]
    if "@" in host:
        return None  # 'user@example.com' typed into a field, not a page address
    if host.startswith("["):  # [::1]:3000
        hostname = host[1:].split("]", 1)[0]
    else:
        hostname = host.rsplit(":", 1)[0] if re.search(r":\d+$", host) else host
    if "." not in hostname and hostname.lower() != "localhost" and ":" not in hostname:
        return None  # a single word is a search term, not a host
    scheme = "https://"
    if hostname.lower() == "localhost" or _LOCAL_HOST_RE.match(host + "/"):
        scheme = "http://"
    else:
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_loopback or ip.is_private or ip.is_link_local:
                scheme = "http://"
        except ValueError:
            pass
    return scheme + v


def _read_url_com(hwnd: int, browser: str, deadline: float) -> Optional[str]:
    """Runs on a thread that owns its COM apartment. Raw IUIAutomation: ONE FindFirst with an
    OR condition (a failed search costs one tree walk, a hit returns as soon as the omnibox is
    reached - the toolbar comes before the web content in the tree). Reads the ValueValue
    property; sends no input and takes no focus."""
    import comtypes
    import comtypes.client
    import uiautomation as auto  # noqa: F401 - provides UIAutomationInitializerInThread + the generated COM wrapper

    with auto.UIAutomationInitializerInThread():
        core = comtypes.client.GetModule("UIAutomationCore.dll")
        uia = comtypes.client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}", interface=core.IUIAutomation
        )
        if time.monotonic() > deadline:
            return None
        root = uia.ElementFromHandle(hwnd)
        if not root:
            return None
        cond = uia.CreatePropertyCondition(_UIA_CLASSNAME_PROP, _CHROMIUM_CLASS)
        for extra in (
            uia.CreatePropertyCondition(_UIA_AUTOMATIONID_PROP, _FIREFOX_AUTOMATION_ID),
            *(uia.CreatePropertyConditionEx(_UIA_NAME_PROP, hint, _MATCH_SUBSTRING_IGNORE_CASE) for hint in _NAME_HINTS),
        ):
            cond = uia.CreateOrCondition(cond, extra)
        cond = uia.CreateAndCondition(uia.CreatePropertyCondition(_UIA_CONTROLTYPE_PROP, _UIA_EDIT_TYPE), cond)
        if time.monotonic() > deadline:
            return None
        el = root.FindFirst(_UIA_SCOPE_DESCENDANTS, cond)
        if not el:
            return None
        try:
            value = el.GetCurrentPropertyValue(_UIA_VALUE_PROP)
        except Exception:  # noqa: BLE001
            return None
        return normalize_browser_url(value if isinstance(value, str) else None)


def read_browser_url(hwnd: int, browser: str, timeout_s: float = 1.5) -> Optional[str]:
    """Best-effort current-tab URL through UI Automation (address-bar Edit control).
    Blocking; call it from a worker thread. Returns None silently on ANY failure or when
    `timeout_s` elapses. Must not steal focus or send input.

    The COM work runs on its own short-lived daemon thread (own COM apartment) so the
    timeout is a HARD limit even if the browser's accessibility provider hangs."""
    if browser not in ("chrome", "edge", "firefox") or not hwnd:
        return None
    box: dict[str, Optional[str]] = {"url": None}
    deadline = time.monotonic() + max(0.05, float(timeout_s))

    def _work() -> None:
        try:
            box["url"] = _read_url_com(int(hwnd), browser, deadline)
        except Exception:  # noqa: BLE001 - silent by design
            box["url"] = None

    t = threading.Thread(target=_work, name="uireport-url", daemon=True)
    t.start()
    t.join(max(0.05, float(timeout_s)))
    if t.is_alive():
        return None
    return box["url"]
