"""Window placement and (legitimate) foreground promotion for the editor.
Owner: editor builder (package B). No dependency on uireport.capture.

Coordinates: Qt's `QScreen.geometry().topLeft()` is the NATIVE (physical) top-left of the
screen while its size is LOGICAL (physical size = size * devicePixelRatio), see CONTRACT.md 4.5.
Window geometry passed to Qt widgets lives in the same hybrid space, so everything below
computes in that space using `availableGeometry()`.

Bringing the window to the front uses only documented calls: ShowWindow, SetForegroundWindow,
optionally AttachThreadInput and a temporary topmost toggle. It never synthesises keyboard or
mouse input.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from ..models import MonitorMeta

COMPACT_MAX_W = 1100
COMPACT_MAX_H = 760
COMPACT_FRACTION = 0.8

Rect4 = tuple[int, int, int, int]  # x, y, w, h


# ---------------------------------------------------------------------------
# screens
# ---------------------------------------------------------------------------
def screen_physical_rect(screen: Any) -> Rect4:
    """Native virtual-screen rectangle (physical px) of a QScreen (or a duck-typed stand-in
    with geometry() and devicePixelRatio())."""
    g = screen.geometry()
    dpr = float(screen.devicePixelRatio() or 1.0)
    return (int(g.x()), int(g.y()), int(round(g.width() * dpr)), int(round(g.height() * dpr)))


def _overlap(a: Rect4, b: Rect4) -> int:
    w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0


def find_screen_for_monitor(monitor: Optional[MonitorMeta], screens: Optional[Sequence[Any]] = None) -> Any:
    """The QScreen showing `monitor` (matched by native geometry, never by name: on Windows
    QScreen.name() is the friendly monitor name, not \\\\.\\DISPLAYn). Falls back to the screen with
    the largest overlap, then to a name match, then None."""
    if monitor is None:
        return None
    if screens is None:
        from PySide6.QtGui import QGuiApplication

        screens = QGuiApplication.screens()
    screens = list(screens)
    if not screens:
        return None
    target: Rect4 = (monitor.rect.x, monitor.rect.y, monitor.rect.w, monitor.rect.h)
    for s in screens:
        if screen_physical_rect(s) == target:
            return s
    best, best_area = None, 0
    for s in screens:
        a = _overlap(screen_physical_rect(s), target)
        if a > best_area:
            best, best_area = s, a
    if best is not None:
        return best
    for s in screens:
        try:
            if s.name() and s.name() in (monitor.label, monitor.name):
                return s
        except Exception:  # pragma: no cover - defensive
            pass
    return None


# ---------------------------------------------------------------------------
# geometry math (pure)
# ---------------------------------------------------------------------------
def compact_size(avail: Rect4) -> tuple[int, int]:
    """~min(1100x760, 80% of the available area)."""
    _, _, aw, ah = avail
    return (min(COMPACT_MAX_W, int(aw * COMPACT_FRACTION)), min(COMPACT_MAX_H, int(ah * COMPACT_FRACTION)))


def place_centered(avail: Rect4, size: tuple[int, int], min_size: tuple[int, int] = (0, 0)) -> Rect4:
    """A rectangle of `size` (raised to `min_size`, but never larger than `avail`) centred in `avail`."""
    ax, ay, aw, ah = avail
    w = min(aw, max(size[0], min_size[0]))
    h = min(ah, max(size[1], min_size[1]))
    return (ax + (aw - w) // 2, ay + (ah - h) // 2, w, h)


def clamp_rect_into(rect: Rect4, avail: Rect4) -> Rect4:
    """Shift (and, if necessary, shrink) `rect` so it lies inside `avail`."""
    x, y, w, h = rect
    ax, ay, aw, ah = avail
    w, h = min(w, aw), min(h, ah)
    x = max(ax, min(x, ax + aw - w))
    y = max(ay, min(y, ay + ah - h))
    return (x, y, w, h)


def rect_inside(rect: Rect4, avail: Rect4) -> bool:
    return (
        rect[0] >= avail[0]
        and rect[1] >= avail[1]
        and rect[0] + rect[2] <= avail[0] + avail[2]
        and rect[1] + rect[3] <= avail[1] + avail[3]
    )


# ---------------------------------------------------------------------------
# foreground promotion (Windows only)
# ---------------------------------------------------------------------------
class Win32Api:
    """Thin wrapper over pywin32 so the promotion sequence can be tested with a fake."""

    def __init__(self) -> None:
        import pywintypes
        import win32api
        import win32con
        import win32gui
        import win32process

        self.pywintypes, self.win32api, self.con = pywintypes, win32api, win32con
        self.gui, self.process = win32gui, win32process

    def is_iconic(self, hwnd: int) -> bool:
        return bool(self.gui.IsIconic(hwnd))

    def show_window(self, hwnd: int, cmd: int) -> None:
        self.gui.ShowWindow(hwnd, cmd)

    def foreground(self) -> int:
        return int(self.gui.GetForegroundWindow() or 0)

    def set_foreground(self, hwnd: int) -> bool:
        try:
            self.gui.SetForegroundWindow(hwnd)
            return True
        except self.pywintypes.error:
            return False

    def bring_to_top(self, hwnd: int) -> None:
        try:
            self.gui.BringWindowToTop(hwnd)
        except self.pywintypes.error:
            pass

    def window_thread(self, hwnd: int) -> int:
        try:
            return int(self.process.GetWindowThreadProcessId(hwnd)[0])
        except self.pywintypes.error:
            return 0

    def current_thread(self) -> int:
        return int(self.win32api.GetCurrentThreadId())

    def attach_input(self, a: int, b: int, attach: bool) -> bool:
        try:
            return bool(self.process.AttachThreadInput(a, b, attach))
        except self.pywintypes.error:
            return False

    def set_topmost(self, hwnd: int, topmost: bool) -> None:
        after = self.con.HWND_TOPMOST if topmost else self.con.HWND_NOTOPMOST
        flags = self.con.SWP_NOMOVE | self.con.SWP_NOSIZE | self.con.SWP_NOACTIVATE
        try:
            self.gui.SetWindowPos(hwnd, after, 0, 0, 0, 0, flags)
        except self.pywintypes.error:
            pass

    SW_RESTORE = 9
    SW_SHOW = 5


def bring_to_front(hwnd: int, api: Any = None) -> bool:
    """Make `hwnd` the foreground window as far as Windows allows. Returns True when it ends
    up foreground. Steps, stopping at the first that works: restore/show + SetForegroundWindow;
    the same while attached to the current foreground thread's input queue; a brief topmost
    toggle. Never sends synthetic input."""
    try:
        w = api or Win32Api()
    except Exception:
        return False
    try:
        if w.is_iconic(hwnd):
            w.show_window(hwnd, w.SW_RESTORE)
        else:
            w.show_window(hwnd, w.SW_SHOW)
        w.set_foreground(hwnd)
        if w.foreground() == hwnd:
            return True
        fg = w.foreground()
        cur = w.current_thread()
        fg_thread = w.window_thread(fg) if fg else 0
        attached = False
        if fg_thread and fg_thread != cur:
            attached = w.attach_input(cur, fg_thread, True)
        try:
            w.bring_to_top(hwnd)
            w.set_foreground(hwnd)
        finally:
            if attached:
                w.attach_input(cur, fg_thread, False)
        if w.foreground() == hwnd:
            return True
        w.set_topmost(hwnd, True)
        w.set_topmost(hwnd, False)
        w.set_foreground(hwnd)
        return w.foreground() == hwnd
    except Exception:
        return False
