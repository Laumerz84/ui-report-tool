"""Real Win32 behaviour of the capture helpers.

Always-on tests only touch windows THIS test creates and never shows (hidden), plus read-only
queries. The two tests that briefly show something on the real desktop (a 40x40 non-activating
probe, and the real countdown badge, both < 1 s in a far corner) are opt-in:

    set UIREPORT_REAL_DESKTOP=1
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import pytest

from uireport.capture import grab, monitors, windowinfo, winapi
from uireport.geometry import IntRect

ROOT = Path(__file__).resolve().parent.parent
REAL_DESKTOP = os.environ.get("UIREPORT_REAL_DESKTOP") == "1"

user32 = winapi.user32
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", _WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
_gdi32.CreateSolidBrush.restype = wintypes.HANDLE
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE

WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
SW_SHOWNOACTIVATE = 4
PM_REMOVE = 1
_classes: dict[int, str] = {}


def _class_for(colorref: int) -> str:
    if colorref in _classes:
        return _classes[colorref]
    name = f"UIReportTestProbe{colorref:06X}"
    wc = _WNDCLASSW()
    wc.lpfnWndProc = _WNDPROC(ctypes.cast(user32.DefWindowProcW, ctypes.c_void_p).value)
    wc.hInstance = _kernel32.GetModuleHandleW(None)
    wc.hbrBackground = _gdi32.CreateSolidBrush(colorref)
    wc.lpszClassName = name
    if not user32.RegisterClassW(ctypes.byref(wc)):
        raise OSError(f"RegisterClassW failed: {ctypes.get_last_error()}")
    _classes[colorref] = name
    return name


def pump(seconds: float) -> None:
    end = time.monotonic() + seconds
    msg = wintypes.MSG()
    while time.monotonic() < end:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        winapi.dwm_flush()
        time.sleep(0.005)


class Win32Window:
    """A raw Win32 window created (and destroyed) by the test."""

    def __init__(self, title="uireport-test-window", rect=(100, 100, 300, 200), style=WS_POPUP, ex_style=0, colorref=0x00FF00FF):
        x, y, w, h = rect
        self.hwnd = user32.CreateWindowExW(
            ex_style, _class_for(colorref), title, style, x, y, w, h, None, None, _kernel32.GetModuleHandleW(None), None
        )
        if not self.hwnd:
            raise OSError(f"CreateWindowExW failed: {ctypes.get_last_error()}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.destroy()

    def destroy(self):
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None


# ---- DPI awareness (fresh interpreters: the setting is process wide and one-shot) ---------------------------------------------
def _run_py(code: str) -> str:
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_enable_per_monitor_dpi_awareness_from_unaware_and_idempotent():
    code = (
        "from uireport.capture import winapi as w\n"
        "a = w.enable_per_monitor_dpi_awareness(); mid = w._current_awareness(); b = w.enable_per_monitor_dpi_awareness()\n"
        "print(a, mid, b)"
    )
    assert _run_py(code) == "True 2 True"


def test_already_pmv1_counts_as_success():
    code = (
        "import ctypes\n"
        "ctypes.windll.shcore.SetProcessDpiAwareness(2)\n"  # per-monitor V1, like an older manifest
        "from uireport.capture import winapi as w\n"
        "print(w.enable_per_monitor_dpi_awareness(), w._current_awareness())"
    )
    assert _run_py(code) == "True 2"


def test_pmv2_set_by_the_framework_counts_as_success():
    code = (
        "import ctypes\n"
        "ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))\n"  # what Qt 6 does
        "from uireport.capture import winapi as w\n"
        "print(w.enable_per_monitor_dpi_awareness(), w._current_awareness())"
    )
    assert _run_py(code) == "True 2"


def test_system_aware_process_reports_false_without_raising():
    code = (
        "import ctypes\n"
        "ctypes.windll.user32.SetProcessDPIAware()\n"
        "from uireport.capture import winapi as w\n"
        "print(w.enable_per_monitor_dpi_awareness())"
    )
    assert _run_py(code) == "False"


# ---- helpers on windows we own ---------------------------------------------------------------------------------------------------------
def test_simple_queries_and_defensive_behaviour():
    x, y = winapi.cursor_pos()
    assert isinstance(x, int) and isinstance(y, int)
    assert winapi.is_key_down(0x7E) in (True, False)  # F15: harmless
    winapi.dwm_flush()
    assert winapi.exclude_from_capture(0) is False
    assert winapi.exclude_from_capture(0x7FFFFF0) is False  # not a window
    assert winapi.set_noactivate_clickthrough(0) is False
    assert winapi.force_foreground(0) is False
    assert winapi.get_window_rect(0x7FFFFF0) is None
    assert winapi.widget_hwnd(type("W", (), {"winId": lambda self: 77})()) == 77


def test_exclude_from_capture_sets_and_clears_the_display_affinity():
    with Win32Window() as w:
        aff = wintypes.DWORD(0xFFFF)
        assert winapi.exclude_from_capture(w.hwnd, True) is True
        assert user32.GetWindowDisplayAffinity(w.hwnd, ctypes.byref(aff)) and aff.value == winapi.WDA_EXCLUDEFROMCAPTURE == 0x11
        assert winapi.exclude_from_capture(w.hwnd, False) is True
        assert user32.GetWindowDisplayAffinity(w.hwnd, ctypes.byref(aff)) and aff.value == winapi.WDA_NONE


def test_noactivate_clickthrough_adds_bits_and_keeps_existing_ones():
    with Win32Window(ex_style=winapi.WS_EX_LAYERED) as w:
        before = winapi.get_ex_style(w.hwnd)
        assert before & winapi.WS_EX_LAYERED
        assert winapi.set_noactivate_clickthrough(w.hwnd) is True
        after = winapi.get_ex_style(w.hwnd)
        for bit in (winapi.WS_EX_NOACTIVATE, winapi.WS_EX_TOOLWINDOW, winapi.WS_EX_TRANSPARENT, winapi.WS_EX_LAYERED):
            assert after & bit, hex(bit)


def test_set_window_rect_physical_places_exactly():
    with Win32Window(rect=(200, 200, 100, 100)) as w:
        assert winapi.set_window_rect_physical(w.hwnd, 321, 123, 456, 234, topmost=False)
        assert winapi.get_window_rect(w.hwnd) == (321, 123, 456, 234)


def test_window_meta_of_a_window_we_own():
    mons = monitors.enumerate_monitors()
    with Win32Window(title="uireport-meta-probe", rect=(120, 130, 320, 240)) as w:
        meta = windowinfo.window_meta_from_hwnd(w.hwnd, mons)
        assert meta is not None
        assert meta.title == "uireport-meta-probe"
        assert meta.pid == os.getpid()
        assert meta.process_name.lower().startswith("python") and meta.process_name.lower().endswith(".exe")
        assert meta.exe_path.lower().endswith(meta.process_name.lower()) and os.path.isfile(meta.exe_path)
        assert (meta.rect.w, meta.rect.h) == (320, 240)
        assert meta.browser is None and meta.url is None
        holder = next(m for m in mons if m.rect.intersects(meta.rect))
        assert meta.dpi in (holder.dpi, winapi.get_dpi_for_window(w.hwnd))
        assert meta.size_logical == (round(320 / meta.scale_factor), round(240 / meta.scale_factor))
    assert windowinfo.window_meta_from_hwnd(w.hwnd or 0x7FFFFF0, mons) is None  # destroyed / bogus handle
    assert windowinfo.window_meta_from_hwnd(0, mons) is None


def test_frame_rect_of_a_window_we_own_uses_physical_pixels():
    with Win32Window(rect=(50, 60, 111, 77)) as w:
        r = windowinfo.frame_rect(w.hwnd)
        assert r is not None and (r.w, r.h) == (111, 77)


# ---- the real UI Automation path against a synthetic "address bar" we own -------------------------------------------------------
user32.GetClassInfoW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, ctypes.POINTER(_WNDCLASSW)]
user32.GetClassInfoW.restype = wintypes.BOOL
ES_AUTOHSCROLL = 0x80
WS_CHILD = 0x40000000
WS_OVERLAPPEDWINDOW = 0x00CF0000
_edit_classes: set[str] = set()


def _register_edit_superclass(name: str) -> None:
    """A window class with the system EDIT behaviour but a chosen class name (Chrome's omnibox is
    the Views class 'OmniboxViewViews', which is what windowinfo looks for)."""
    if name in _edit_classes:
        return
    wc = _WNDCLASSW()
    assert user32.GetClassInfoW(None, "EDIT", ctypes.byref(wc))
    wc.lpszClassName = name
    wc.hInstance = _kernel32.GetModuleHandleW(None)
    assert user32.RegisterClassW(ctypes.byref(wc))
    _edit_classes.add(name)


class AddressBarHost:
    """A top-level window (created far outside every monitor at -32000,-32000, never activated, alive
    for a fraction of a second) hosting an EDIT-like child with the given class and text, on its own
    message-pumping thread - UI Automation needs the window's thread to answer. Nothing is drawn on
    any monitor. (UIA does not expose the children of windows that are not WS_VISIBLE.)"""

    def __init__(self, text: str, child_class: str = "OmniboxViewViews"):
        self.text, self.child_class = text, child_class
        self.parent = self.child = None
        self._ready, self._stop = threading.Event(), threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="uireport-test-host")

    def _run(self):
        hinst = _kernel32.GetModuleHandleW(None)
        if self.child_class != "EDIT":
            _register_edit_superclass(self.child_class)
        self.parent = user32.CreateWindowExW(
            winapi.WS_EX_NOACTIVATE | winapi.WS_EX_TOOLWINDOW, _class_for(0x00FFFFFF), "uireport-url-host",
            WS_OVERLAPPEDWINDOW | WS_VISIBLE, -32000, -32000, 400, 200, None, None, hinst, None,
        )
        self.child = user32.CreateWindowExW(
            0, self.child_class, self.text, WS_CHILD | WS_VISIBLE | ES_AUTOHSCROLL, 5, 5, 300, 24, self.parent, None, hinst, None
        )
        self._ready.set()
        msg = wintypes.MSG()
        while not self._stop.is_set():
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.005)
        user32.DestroyWindow(self.parent)

    def __enter__(self):
        self._thread.start()
        assert self._ready.wait(3) and self.parent and self.child
        # UI Automation intermittently (~1 read in 10) blocks for its internal 3 s timeout when it is asked
        # about a window created a few milliseconds ago; a settled window never does (0 of 70 reads, median
        # 0.13 s). A real browser window has been open for a while, so this is a property of the test host.
        time.sleep(0.5)
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(2)


@pytest.mark.parametrize(
    "typed,expected",
    [
        ("example.com/path?q=1", "https://example.com/path?q=1"),  # Chrome hides 'https://'
        ("http://localhost:3000/settings", "http://localhost:3000/settings"),
        ("localhost:3000/settings", "http://localhost:3000/settings"),
        ("how to centre a div", None),  # a search being typed
        ("", None),
    ],
)
def test_real_ui_automation_reads_the_omnibox_of_a_window_we_own(typed, expected):
    with AddressBarHost(typed) as host:
        t0 = time.monotonic()
        url = windowinfo.read_browser_url(host.parent, "chrome", 3.0)
        assert url == expected
        assert time.monotonic() - t0 < 3.0
        assert user32.GetForegroundWindow() != host.parent  # reading never activates the window


def test_real_ui_automation_returns_none_when_there_is_no_address_bar():
    with AddressBarHost("example.com", child_class="EDIT") as host:  # a plain edit box is not the omnibox
        assert windowinfo.read_browser_url(host.parent, "edge", 3.0) is None
        assert windowinfo.read_browser_url(host.parent, "notepad", 3.0) is None  # not a browser: not even tried


# ---- opt-in: real desktop probes (< 1 s, far corner, non-activating) ----------------------------------------------------------------------
def _center_pixel(img, rect: IntRect, origin: IntRect):
    c = rect.center
    return img.pixelColor(int(c[0]) - origin.x, int(c[1]) - origin.y)


@pytest.mark.skipif(not REAL_DESKTOP, reason="set UIREPORT_REAL_DESKTOP=1 to flash a 40x40 probe for < 1 s")
def test_exclude_from_capture_removes_a_window_from_a_real_grab(qapp):
    winapi.enable_per_monitor_dpi_awareness()
    primary = next(m for m in monitors.enumerate_monitors() if m.is_primary)
    size = 40
    x = primary.rect.right - size - 4
    y = primary.rect.bottom - size - 4
    fg_before = user32.GetForegroundWindow()
    ex = winapi.WS_EX_NOACTIVATE | winapi.WS_EX_TOOLWINDOW | winapi.WS_EX_TRANSPARENT | 0x8  # + WS_EX_TOPMOST
    with Win32Window(title="uireport-probe", rect=(x, y, size, size), style=WS_POPUP | WS_VISIBLE, ex_style=ex, colorref=0x00FF00FF) as w:
        user32.ShowWindow(w.hwnd, SW_SHOWNOACTIVATE)
        pump(0.2)
        rect = IntRect(x, y, size, size)
        img, origin = grab.grab_virtual_screen()
        seen = _center_pixel(img, rect, origin)
        if (seen.red(), seen.green(), seen.blue()) != (255, 0, 255):
            pytest.skip("the probe window is not visible to BitBlt in this session (cannot test exclusion)")
        assert winapi.exclude_from_capture(w.hwnd, True)
        pump(0.2)
        img2, _ = grab.grab_virtual_screen()
        hidden = _center_pixel(img2, rect, origin)
        assert (hidden.red(), hidden.green(), hidden.blue()) != (255, 0, 255), "excluded window still appears in the grab"
        assert winapi.exclude_from_capture(w.hwnd, False)
        pump(0.2)
        img3, _ = grab.grab_virtual_screen()
        again = _center_pixel(img3, rect, origin)
        assert (again.red(), again.green(), again.blue()) == (255, 0, 255)  # control: it was the affinity, not luck
        assert user32.GetForegroundWindow() != w.hwnd  # never took the foreground


_COUNTDOWN_PROBE = r"""
import os, sys, json, time
os.environ["QT_QPA_PLATFORM"] = "windows"
sys.path.insert(0, os.getcwd())
from uireport.capture import winapi
winapi.enable_per_monitor_dpi_awareness()
from PySide6.QtCore import QObject, Signal, QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
app = QApplication([])
app.setQuitOnLastWindowClosed(False)
from uireport.capture.countdown import CountdownOverlay
from uireport.capture.grab import grab_virtual_screen
from uireport.capture.monitors import enumerate_monitors

class NoEsc(QObject):
    pressed = Signal()
    def acquire(self): return True      # pretend the hotkey is held: this probe must not grab Esc
    def release(self): pass

def pump(sec):
    loop = QEventLoop(); QTimer.singleShot(int(sec * 1000), loop.quit); loop.exec()

def region_diff(a, b, r):
    total = n = 0
    step = 3
    for y in range(r[1], r[1] + r[3], step):
        for x in range(r[0], r[0] + r[2], step):
            pa, pb = a.pixelColor(x, y), b.pixelColor(x, y)
            total += abs(pa.red() - pb.red()) + abs(pa.green() - pb.green()) + abs(pa.blue() - pb.blue())
            n += 1
    return total / max(1, n)

mons = enumerate_monitors()
primary = next(m for m in mons if m.is_primary)
user32 = winapi.user32
def fg_pid():
    import ctypes
    from ctypes import wintypes
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
    return int(pid.value)

out = {}
base, origin = grab_virtual_screen()
cd = CountdownOverlay(6, primary, None, esc_factory=lambda: NoEsc())
try:
    cd.start()
    pump(0.25)
    hwnd = winapi.widget_hwnd(cd.widget)
    rect = winapi.get_window_rect(hwnd)
    out["rect"] = rect
    out["ex_style_ok"] = (winapi.get_ex_style(hwnd) & (winapi.WS_EX_NOACTIVATE | winapi.WS_EX_TOOLWINDOW | winapi.WS_EX_TRANSPARENT)) == (winapi.WS_EX_NOACTIVATE | winapi.WS_EX_TOOLWINDOW | winapi.WS_EX_TRANSPARENT)
    out["foreground_is_us"] = fg_pid() == os.getpid()
    out["visible"] = cd.is_visible
    rel = (rect[0] - origin.x, rect[1] - origin.y, rect[2], rect[3])
    # control: with the exclusion switched OFF the badge must be visible in a grab
    winapi.exclude_from_capture(hwnd, False); pump(0.15)
    g_visible, _ = grab_virtual_screen()
    out["diff_when_not_excluded"] = region_diff(base, g_visible, rel)
    winapi.exclude_from_capture(hwnd, True); pump(0.15)
    g_excluded, _ = grab_virtual_screen()
    out["diff_when_excluded"] = region_diff(base, g_excluded, rel)
    out["foreground_is_us_after"] = fg_pid() == os.getpid()
finally:
    cd.close()
print("RESULT " + json.dumps(out))
"""


@pytest.mark.skipif(not REAL_DESKTOP, reason="set UIREPORT_REAL_DESKTOP=1 to flash the real countdown badge for < 1 s")
def test_real_countdown_badge_is_non_activating_and_excluded_from_capture():
    proc = subprocess.run(
        [sys.executable, "-c", _COUNTDOWN_PROBE], cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        env={k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"},
    )
    assert proc.returncode == 0, proc.stderr
    line = next(l for l in proc.stdout.splitlines() if l.startswith("RESULT "))
    res = json.loads(line[len("RESULT "):])
    assert res["visible"] and res["ex_style_ok"], res
    assert not res["foreground_is_us"] and not res["foreground_is_us_after"], "the countdown stole the foreground"
    if res["diff_when_not_excluded"] < 4:
        pytest.skip(f"the badge is not visible to BitBlt in this session, cannot test exclusion: {res}")
    assert res["diff_when_excluded"] < 2.0, res  # the excluded badge leaves the grab unchanged
