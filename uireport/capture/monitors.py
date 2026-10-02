"""Monitor enumeration + Qt screen matching. Owner: capture builder (package A)."""
from __future__ import annotations

import ctypes
import re
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from PySide6.QtGui import QGuiApplication, QScreen

from ..geometry import IntRect
from ..models import MonitorMeta
from . import winapi

MONITORINFOF_PRIMARY = 0x1
_TOLERANCE_PX = 2  # rounding slack between Qt's logical size * dpr and the physical size


# ---------------------------------------------------------------------------------------
# pure helpers (unit-tested with synthetic data)
# ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class RawMonitor:
    """What the OS reports for one monitor before we turn it into a MonitorMeta."""

    name: str  # \\.\DISPLAY1
    rect: IntRect  # virtual-screen physical px
    dpi: int
    is_primary: bool
    label: str = ""


def natural_key(name: str) -> list[Any]:
    """Sort key so that \\\\.\\DISPLAY2 < \\\\.\\DISPLAY10."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def build_monitor_list(raw: Sequence[RawMonitor]) -> list[MonitorMeta]:
    """Sort by device name (natural order) and assign 1-based indexes."""
    ordered = sorted(raw, key=lambda r: natural_key(r.name))
    return [
        MonitorMeta(
            index=i,
            name=r.name,
            is_primary=r.is_primary,
            rect=r.rect,
            dpi=r.dpi if r.dpi and r.dpi > 0 else 96,
            label=r.label,
        )
        for i, r in enumerate(ordered, start=1)
    ]


def screen_physical_rect(screen: Any) -> IntRect:
    """Physical-pixel rectangle of a QScreen (or a stub with geometry()/devicePixelRatio()).

    Qt 6 mixed-DPI convention: geometry().topLeft() is the NATIVE (physical) origin of the
    screen, geometry().size() is LOGICAL, so physical size = size * devicePixelRatio()."""
    g = screen.geometry()
    dpr = float(screen.devicePixelRatio()) or 1.0
    return IntRect(int(g.x()), int(g.y()), int(round(g.width() * dpr)), int(round(g.height() * dpr)))


def _rect_distance(a: IntRect, b: IntRect) -> int:
    return abs(a.x - b.x) + abs(a.y - b.y) + abs(a.w - b.w) + abs(a.h - b.h)


def match_screen(rect: IntRect, screens: Sequence[Any], primary: Any = None) -> Optional[Any]:
    """Pick the screen whose physical rect equals `rect` (within rounding slack); else the
    one with the closest top-left; else `primary`; else the first screen; else None."""
    if not screens:
        return primary
    scored = [(_rect_distance(screen_physical_rect(s), rect), s) for s in screens]
    best_dist, best = min(scored, key=lambda t: t[0])
    if best_dist <= 4 * _TOLERANCE_PX:
        return best
    # closest top-left
    def tl(s: Any) -> int:
        r = screen_physical_rect(s)
        return abs(r.x - rect.x) + abs(r.y - rect.y)

    closest = min(screens, key=tl)
    if tl(closest) <= _TOLERANCE_PX * 2:
        return closest
    return primary if primary is not None else closest


# ---------------------------------------------------------------------------------------
# Win32 enumeration
# ---------------------------------------------------------------------------------------
class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
)


def _enum_raw_monitors() -> list[RawMonitor]:
    user32 = winapi.user32
    if user32 is None:
        return []
    user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, _MONITORENUMPROC, wintypes.LPARAM]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MONITORINFOEXW)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    found: list[RawMonitor] = []

    def _cb(hmon: int, _hdc: int, _prc: Any, _lp: int) -> int:
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            found.append(
                RawMonitor(
                    name=str(info.szDevice),
                    rect=IntRect(r.left, r.top, r.right - r.left, r.bottom - r.top),
                    dpi=winapi.get_dpi_for_monitor(hmon),
                    is_primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
                )
            )
        return 1

    cb = _MONITORENUMPROC(_cb)  # keep a reference for the duration of the call
    user32.EnumDisplayMonitors(None, None, cb, 0)
    return found


def _fallback_primary() -> RawMonitor:
    x, y, w, h = winapi.screen_size_metrics()
    return RawMonitor(r"\\.\DISPLAY1", IntRect(0, 0, max(w, 1), max(h, 1)), 96, True)


def _apply_qt_labels(mons: list[MonitorMeta]) -> list[MonitorMeta]:
    """Fill `label` with the matching QScreen.name() (the friendly monitor name on Windows)
    when a Qt application exists. Never fails."""
    try:
        if QGuiApplication.instance() is None:
            return mons
        screens = QGuiApplication.screens()
        for m in mons:
            if m.label:
                continue
            s = match_screen(m.rect, screens, primary=None)
            if s is not None and _rect_distance(screen_physical_rect(s), m.rect) <= 4 * _TOLERANCE_PX:
                m.label = str(s.name())
    except Exception:  # noqa: BLE001 - labels are cosmetic
        pass
    return mons


def enumerate_monitors() -> list[MonitorMeta]:
    """All attached monitors, sorted by device name (\\\\.\\DISPLAY1, \\\\.\\DISPLAY2, ...);
    `index` is the 1-based position in that list.

    rect = the monitor rectangle in virtual-screen PHYSICAL pixels (EnumDisplayMonitors /
    GetMonitorInfo under PMv2). dpi = effective DPI from GetDpiForMonitor(MDT_EFFECTIVE_DPI)
    (96/120/144/...). name = device name, label = friendly name if obtainable (else the
    matching QScreen.name(), else ""). Never returns an empty list: falls back to one
    primary monitor built from GetSystemMetrics."""
    try:
        raw = _enum_raw_monitors()
    except Exception:  # noqa: BLE001
        raw = []
    if not raw:
        raw = [_fallback_primary()]
    return _apply_qt_labels(build_monitor_list(raw))


def virtual_screen_rect() -> IntRect:
    """Bounding rectangle of all monitors in virtual-screen physical pixels."""
    x, y, w, h = winapi.screen_size_metrics()
    if w > 0 and h > 0:
        return IntRect(x, y, w, h)
    rect = IntRect()
    for m in enumerate_monitors():
        rect = rect.union(m.rect)
    return rect


def find_qscreen(monitor: MonitorMeta) -> Optional[QScreen]:
    """The QScreen showing `monitor`. NOTE: on Windows QScreen.name() is the FRIENDLY name
    (e.g. '49C1R'), NOT '\\\\.\\DISPLAY1', so match by geometry: Qt 6 keeps the native
    physical top-left in QScreen.geometry().topLeft() and reports the size in logical
    pixels (physical size == geometry.size() * devicePixelRatio). Fall back to the closest
    top-left, then to QGuiApplication.primaryScreen()."""
    if QGuiApplication.instance() is None:
        return None
    return match_screen(monitor.rect, QGuiApplication.screens(), primary=QGuiApplication.primaryScreen())
