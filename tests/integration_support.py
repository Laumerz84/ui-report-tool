"""Rig for the integration tests (contains no tests).

A SYNTHETIC desktop replaces the real screen: no screen grab, no OS hotkeys, no desktop input.
Everything else is the real thing: CaptureService, RegionSelector, CountdownOverlay (offscreen
overlays driven by injected mouse/key events), AppController, EditorWindow + AnnotationCanvas
(driven with QTest on our own widgets), the writer, the clipboard, the toast and the tray icon.

The synthetic desktop encodes its own coordinates in its pixels: the colour of the pixel at
virtual-screen point (gx, gy) is `encode(gx - origin.x, gy - origin.y)`, so any pixel found in a
saved PNG can be traced back to the exact screen point it came from
(`SyntheticDesktop.rgb_matches`). That is what proves that region selection, cropping, pins
and the report agree with each other on scaled and mixed-DPI layouts.
"""
from __future__ import annotations

import dataclasses
import os
import time
from typing import Any, Callable, Optional

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter
from PySide6.QtWidgets import QApplication

from uireport.capture.countdown import CountdownOverlay
from uireport.capture.selector import RegionSelector
from uireport.capture.service import CaptureDeps, CaptureService
from uireport.capture.windowinfo import WindowSnapshot
from uireport.geometry import IntRect, dpi_from_scale_percent
from uireport.models import MonitorMeta, SystemMeta, WindowMeta

LEFT = Qt.MouseButton.LeftButton
RIGHT = Qt.MouseButton.RightButton
NOBTN = Qt.MouseButton.NoButton


# ---------------------------------------------------------------------------------------------
# layouts
# ---------------------------------------------------------------------------------------------
def mon(index: int, x: int, y: int, w: int, h: int, pct: int = 100, primary: bool = False, label: str = "") -> MonitorMeta:
    return MonitorMeta(
        index=index,
        name=rf"\\.\DISPLAY{index}",
        is_primary=primary,
        rect=IntRect(x, y, w, h),
        dpi=dpi_from_scale_percent(pct),
        label=label,
    )


def layout_single_150() -> list[MonitorMeta]:
    """One 3840x2160 monitor at 150 % (Qt sees 2560x1440 logical pixels)."""
    return [mon(1, 0, 0, 3840, 2160, 150, True, "UHD 150")]


def layout_mixed() -> list[MonitorMeta]:
    """Primary 1080p at 100 % + a 150 % monitor to its left, higher up (negative x AND y)."""
    return [
        mon(1, 0, 0, 1920, 1080, 100, True, "Primary 100"),
        mon(2, -2880, -300, 2880, 1620, 150, False, "Side 150"),
    ]


def layout_three() -> list[MonitorMeta]:
    """125 % | primary 100 % | 200 %, all with different origins (one negative x, one negative y)."""
    return [
        mon(1, -2560, -180, 2560, 1440, 125, False, "Left 125"),
        mon(2, 0, 0, 1920, 1080, 100, True, "Primary 100"),
        mon(3, 1920, -540, 3840, 2160, 200, False, "Right 200"),
    ]


def layout_odd_rounding() -> list[MonitorMeta]:
    """1366x768 at 125 %: the logical size (1092.8 x 614.4) is not a whole number, so Qt rounds it."""
    return [mon(1, 0, 0, 1366, 768, 125, True, "Laptop 125")]


LAYOUTS: dict[str, Callable[[], list[MonitorMeta]]] = {
    "single_150": layout_single_150,
    "mixed_100_150": layout_mixed,
    "three_125_100_200": layout_three,
    "odd_rounding_125": layout_odd_rounding,
}


def union_rect(monitors: list[MonitorMeta]) -> IntRect:
    r = monitors[0].rect
    for m in monitors[1:]:
        r = r.union(m.rect)
    return r


def logical_size(m: MonitorMeta) -> tuple[int, int]:
    """What Qt reports for the monitor's widget: physical / scale, rounded to whole logical px."""
    return (round(m.rect.w / m.scale_factor), round(m.rect.h / m.scale_factor))


# ---------------------------------------------------------------------------------------------
# a desktop whose pixels say where they are
# ---------------------------------------------------------------------------------------------
def encode(lx: int, ly: int) -> tuple[int, int, int]:
    """Local desktop coordinates -> (r, g, b). 13 bits for x and 11 bits for y fit into 24 bits, so
    the code wraps at 8192 x 2048 (a 4K-tall desktop wraps its last rows; every check compares
    modulo that wrap, which is still unambiguous for the offsets the tests use)."""
    lx &= 8191
    ly &= 2047
    return (lx & 255, ly & 255, ((lx >> 8) & 31) | ((ly >> 8) << 5))


def hex_of(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


class SyntheticDesktop:
    def __init__(self, monitors: list[MonitorMeta]) -> None:
        self.origin = union_rect(monitors)
        w, h = self.origin.w, self.origin.h
        red = bytes(x & 255 for x in range(w))
        blue_lo = [(x >> 8) & 31 for x in range(w)]
        blue_rows = {yh: bytes(b | (yh << 5) for b in blue_lo) for yh in range(8)}
        alpha = b"\xff" * w
        buf = bytearray(w * h * 4)
        stride = w * 4
        for y in range(h):
            row = bytearray(stride)
            row[0::4] = blue_rows[((y & 2047) >> 8)]
            row[1::4] = bytes([y & 255]) * w
            row[2::4] = red
            row[3::4] = alpha
            buf[y * stride:(y + 1) * stride] = row
        self.image = QImage(bytes(buf), w, h, stride, QImage.Format.Format_RGB32).copy()

    def color_at(self, gx: int, gy: int) -> str:
        """The colour the desktop has at virtual-screen point (gx, gy), as '#RRGGBB'."""
        return hex_of(encode(gx - self.origin.x, gy - self.origin.y))

    def rgb_matches(self, rgb: tuple[int, int, int], gx: int, gy: int) -> bool:
        """True when the colour is the one the desktop has at virtual-screen point (gx, gy)."""
        return rgb == encode(gx - self.origin.x, gy - self.origin.y)


def pixel_rgb(img: QImage, x: int, y: int) -> tuple[int, int, int]:
    c = img.pixelColor(x, y)
    return (c.red(), c.green(), c.blue())


def as_rgb32(img: QImage) -> QImage:
    return img if img.format() == QImage.Format.Format_RGB32 else img.convertToFormat(QImage.Format.Format_RGB32)


def blank_rect(img: QImage, r: IntRect) -> QImage:
    """A copy of `img` (RGB32) with `r` painted black (to compare everything but a redacted area)."""
    out = as_rgb32(img).copy()
    p = QPainter(out)
    p.fillRect(r.x, r.y, r.w, r.h, QColor("black"))
    p.end()
    return out


def same_pixels(a: QImage, b: QImage) -> bool:
    a, b = as_rgb32(a), as_rgb32(b)
    if a.size() != b.size():
        return False
    return bytes(a.constBits()) == bytes(b.constBits())


def fraction_differing(a: QImage, b: QImage, r: IntRect) -> float:
    """Share of pixels inside `r` whose colour differs between the two images."""
    a, b = as_rgb32(a), as_rgb32(b)
    bad = 0
    for y in range(r.y, r.y + r.h):
        for x in range(r.x, r.x + r.w):
            if a.pixel(x, y) != b.pixel(x, y):
                bad += 1
    return bad / max(1, r.w * r.h)


def diff_bbox(a: QImage, b: QImage, window: IntRect) -> Optional[IntRect]:
    """Bounding box of the pixels that differ between two images inside `window` (None: identical)."""
    a, b = as_rgb32(a), as_rgb32(b)
    x0 = y0 = 10**9
    x1 = y1 = -1
    for y in range(max(0, window.y), min(a.height(), window.y + window.h)):
        for x in range(max(0, window.x), min(a.width(), window.x + window.w)):
            if a.pixel(x, y) != b.pixel(x, y):
                x0, y0, x1, y1 = min(x0, x), min(y0, y), max(x1, x), max(y1, y)
    if x1 < 0:
        return None
    return IntRect(x0, y0, x1 - x0 + 1, y1 - y0 + 1)


# ---------------------------------------------------------------------------------------------
# event loop helpers
# ---------------------------------------------------------------------------------------------
def wait_until(cond: Callable[[], Any], timeout: float = 20.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.004)
    QCoreApplication.processEvents()
    return bool(cond())


def settle(ms: int = 60) -> None:
    """Let queued events (and short single-shot timers) run."""
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.004)


# ---------------------------------------------------------------------------------------------
# the capture side: synthetic screen + real CaptureService / RegionSelector / CountdownOverlay
# ---------------------------------------------------------------------------------------------
class FakeEsc(QObject):
    """Stands in for the temporary global Esc hotkey of the countdown."""

    pressed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.acquired = False
        self.released = False

    def acquire(self) -> bool:
        self.acquired = True
        return True

    def release(self) -> None:
        self.released = True


class SizedSelector(RegionSelector):
    """RegionSelector whose overlays get the size Qt would give them on a scaled monitor:
    logical = physical / scale, rounded (place=False keeps them off the real screens)."""

    def start(self) -> None:
        super().start()
        for ov in self.overlays:
            lw, lh = logical_size(ov.monitor)
            ov.resize(lw, lh)


class CaptureRig:
    """A real CaptureService running against a synthetic desktop.

    `screen_layers` is a list of callables `(image) -> None` that paint whatever else would be on
    the screen at the moment of the grab (an open menu, ...). The countdown badge is one of them
    when `simulate_badge` is on: if the real badge widget is still visible when the grab happens,
    it is painted into the grabbed image, exactly as a compositor would show it."""

    BADGE_COLOR = QColor(255, 0, 255)
    BADGE_W, BADGE_H = 132, 104

    def __init__(
        self,
        monitors: list[MonitorMeta],
        get_settings: Callable[[], Any],
        *,
        clock_speed: float = 60.0,
        parent: Optional[QObject] = None,
    ) -> None:
        self.monitors = monitors
        self.desktop = SyntheticDesktop(monitors)
        primary = next((m for m in monitors if m.is_primary), monitors[0])
        self.cursor = [primary.rect.x + primary.rect.w // 2, primary.rect.y + primary.rect.h // 2]
        self.selectors: list[SizedSelector] = []
        self.countdowns: list[CountdownOverlay] = []
        self.esc_guards: list[FakeEsc] = []
        self.grab_count = 0
        self.screen_layers: list[Callable[[QImage], None]] = []
        self.badge_rects: list[IntRect] = []  # where a visible badge was painted into a grab
        self.badge_visible_at_grab = 0
        self.simulate_badge = True
        self._speed = clock_speed  # countdown clock runs this many times faster than real time
        self._virtual = 0.0
        self._last = time.monotonic()
        self.windows = [
            WindowSnapshot(hwnd=0x1000 + m.index, rect=IntRect(m.rect.x + 90, m.rect.y + 60, 1200, 700),
                           title=f"Demo window on {m.label or m.index}", pid=5000 + m.index)
            for m in monitors
        ]
        self.foreground = self.windows[0]
        self.deps = CaptureDeps(
            enumerate_monitors=lambda: [dataclasses.replace(m) for m in self.monitors],
            grab=self._grab,
            snapshot_windows=lambda pid: list(self.windows),
            foreground_hwnd=lambda pid: self.foreground.hwnd,
            window_meta=self._window_meta,
            read_url=lambda hwnd, browser, timeout: None,
            cursor_pos=lambda: (int(self.cursor[0]), int(self.cursor[1])),
            dwm_flush=lambda: None,
            countdown_factory=self._countdown_factory,
            selector_factory=self._selector_factory,
            own_pid=os.getpid,
            settle_ms=5,
        )
        self.service = CaptureService(get_settings, parent, deps=self.deps)

    # ---- synthetic screen -------------------------------------------------------------------------
    def fast_clock(self) -> float:
        now = time.monotonic()
        self._virtual += (now - self._last) * self._speed
        self._last = now
        return self._virtual

    def set_clock_speed(self, speed: float) -> None:
        """1.0 = a countdown of N seconds really takes N seconds (use it before pressing Esc in it)."""
        self.fast_clock()
        self._speed = speed

    def _grab(self) -> tuple[QImage, IntRect]:
        self.grab_count += 1
        img = self.desktop.image.copy()
        for layer in list(self.screen_layers):
            layer(img)
        if self.simulate_badge:
            for cd in self.countdowns:
                if cd.is_visible:  # the real badge is still on screen at the moment of the grab
                    self.badge_visible_at_grab += 1
                    r = self._badge_rect(cd)
                    self.badge_rects.append(r)
                    p = QPainter(img)
                    p.fillRect(r.x - self.desktop.origin.x, r.y - self.desktop.origin.y, r.w, r.h, self.BADGE_COLOR)
                    p.end()
        return img, self.desktop.origin

    def badge_rect_for(self, m: MonitorMeta) -> IntRect:
        """Where the badge sits on monitor `m` (bottom-right, like countdown._position)."""
        lw, lh = logical_size(m)
        s = m.scale_factor
        w, h = round(self.BADGE_W * s), round(self.BADGE_H * s)
        return IntRect(m.rect.x + m.rect.w - w - round(28 * s), m.rect.y + m.rect.h - h - round(96 * s), w, h)

    def _badge_rect(self, cd: CountdownOverlay) -> IntRect:
        return self.badge_rect_for(cd.rig_monitor)  # the monitor the service put the badge on

    def _window_meta(self, hwnd: int, mons: list[MonitorMeta]) -> Optional[WindowMeta]:
        w = next((s for s in self.windows if s.hwnd == hwnd), None)
        if w is None:
            return None
        holder = max(mons, key=lambda m: (w.rect.intersection(m.rect).w * w.rect.intersection(m.rect).h) if w.rect.intersection(m.rect) else 0)
        return WindowMeta(title=w.title, process_name="demo.exe", exe_path=r"C:\Demo\demo.exe", pid=w.pid,
                          rect=w.rect, dpi=holder.dpi)

    def _countdown_factory(self, seconds: int, monitor: Optional[MonitorMeta], parent: QObject) -> CountdownOverlay:
        esc = FakeEsc()
        self.esc_guards.append(esc)
        cd = CountdownOverlay(seconds, monitor, parent, esc_factory=lambda: esc, clock=self.fast_clock,
                              use_native=False, place=False)
        cd.rig_monitor = monitor if monitor is not None else self.monitors[0]
        self.countdowns.append(cd)
        return cd

    def _selector_factory(self, image: QImage, origin: IntRect, monitors: list[MonitorMeta],
                          windows: list[WindowSnapshot], parent: QObject) -> SizedSelector:
        sel = SizedSelector(image, origin, monitors, windows, parent, place=False,
                            cursor_pos=lambda: (int(self.cursor[0]), int(self.cursor[1])),
                            key_down=lambda vk: False)
        self.selectors.append(sel)
        return sel

    # ---- the user's hands -----------------------------------------------------------------------------
    def monitor_for(self, x: float, y: float) -> MonitorMeta:
        return next(m for m in self.monitors if m.rect.contains_point(x, y))

    def put_cursor_on(self, m: MonitorMeta) -> None:
        self.cursor = [m.rect.x + m.rect.w // 2, m.rect.y + m.rect.h // 2]

    @property
    def selector(self) -> SizedSelector:
        assert self.selectors, "no selector was ever opened"
        sel = self.selectors[-1]
        assert sel.overlays, "the selector is closed"
        return sel

    def _widget_point(self, m: MonitorMeta, ov: Any, px: float, py: float) -> tuple[float, float]:
        # independent of the code under test: plain proportion of the physical monitor rectangle
        return ((px - m.rect.x) * ov.width() / m.rect.w, (py - m.rect.y) * ov.height() / m.rect.h)

    def _send_mouse(self, ov: Any, kind: QEvent.Type, x: float, y: float, button: Any, buttons: Any) -> None:
        ev = QMouseEvent(kind, QPointF(x, y), QPointF(x, y), button, buttons, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(ov, ev)

    def overlay_at(self, px: float, py: float) -> tuple[MonitorMeta, Any]:
        m = self.monitor_for(px, py)
        return m, self.selector.overlay_for(m)

    def drag_region(self, rect: IntRect) -> None:
        """Press at the rectangle's top-left, move, release at its bottom-right (physical screen px)."""
        m, ov = self.overlay_at(rect.x, rect.y)
        x0, y0 = self._widget_point(m, ov, rect.x, rect.y)
        x1, y1 = self._widget_point(m, ov, rect.x + rect.w, rect.y + rect.h)
        self._send_mouse(ov, QEvent.Type.MouseButtonPress, x0, y0, LEFT, LEFT)
        self._send_mouse(ov, QEvent.Type.MouseMove, (x0 + x1) / 2, (y0 + y1) / 2, NOBTN, LEFT)
        self._send_mouse(ov, QEvent.Type.MouseMove, x1, y1, NOBTN, LEFT)
        self._send_mouse(ov, QEvent.Type.MouseButtonRelease, x1, y1, LEFT, NOBTN)

    def click_at(self, px: float, py: float) -> None:
        m, ov = self.overlay_at(px, py)
        x, y = self._widget_point(m, ov, px, py)
        self._send_mouse(ov, QEvent.Type.MouseButtonPress, x, y, LEFT, LEFT)
        self._send_mouse(ov, QEvent.Type.MouseButtonRelease, x, y, LEFT, NOBTN)

    def press_key(self, key: Qt.Key) -> None:
        from PySide6.QtGui import QKeyEvent

        ov = self.selector.overlays[0]
        QApplication.sendEvent(ov, QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier))

    def esc_in_selector(self) -> None:
        self.press_key(Qt.Key.Key_Escape)

    def esc_in_countdown(self) -> None:
        """The user presses Esc while the badge counts down (the temporary global hotkey fires)."""
        assert self.esc_guards, "no countdown was ever started"
        self.esc_guards[-1].pressed.emit()
        settle(30)  # the guard's connection is queued

    def close(self) -> None:
        try:
            self.service.cancel()
        except Exception:
            pass
        for sel in self.selectors:
            try:
                sel.close()
            except Exception:
                pass
        for cd in self.countdowns:
            try:
                cd.close()
            except Exception:
                pass


def synthetic_system_meta(monitors: list[MonitorMeta]) -> SystemMeta:
    return SystemMeta(
        windows_version="Windows 11 Pro 10.0.26200 (synthetic)",
        windows_build=26200,
        theme_apps="dark",
        theme_system="dark",
        monitors=[dataclasses.replace(m) for m in monitors],
    )


# ---------------------------------------------------------------------------------------------
# the whole application, minus the OS
# ---------------------------------------------------------------------------------------------
class Stack:
    """Real AppController + CaptureService + selector + countdown + EditorWindow + writer +
    clipboard + Toast + TrayIcon (all offscreen) on top of a synthetic desktop. Only the OS
    hotkeys (FakeHotkeys), the autostart registry (MemoryAutostart), 'open folder' (a list) and the
    system-meta probe are replaced. Output and settings live under `test_out`."""

    def __init__(self, test_out: Any, monkeypatch: Any, monitors: list[MonitorMeta], *, delay_seconds: int = 3) -> None:
        from uireport.app import controller as controller_mod
        from uireport.app.controller import AppController
        from uireport.app.fakes import FakeHotkeys, MemoryAutostart
        from uireport.app.toast import Toast
        from uireport.app.tray import TrayIcon
        from uireport.settings import SettingsStore

        monkeypatch.setattr(controller_mod, "_dwm_flush", lambda: None)
        monkeypatch.setattr(controller_mod, "_collect_system_meta", lambda prev: synthetic_system_meta(monitors))
        self.monitors = monitors
        self.test_out = test_out
        self.store = SettingsStore(test_out / "settings.json")
        self.store.settings.output_root = str(test_out / "reports")
        self.store.settings.delay_seconds = delay_seconds
        self.rig = CaptureRig(monitors, lambda: self.store.settings)
        self.hotkeys = FakeHotkeys()
        self.tray = TrayIcon()
        self.toast = Toast()
        self.autostart = MemoryAutostart()
        self.opened: list[str] = []
        self.finished: list[Any] = []
        self.failed: list[str] = []
        self.captured: list[Any] = []
        self.cancelled: list[str] = []
        self.controller = AppController(
            self.store,
            capture=self.rig.service,
            hotkeys=self.hotkeys,
            tray=self.tray,
            toast=self.toast,
            autostart=self.autostart,
            confirm_quit=lambda n: "cancel",
            open_path=self.opened.append,
        )
        self.controller.session_finished.connect(self.finished.append)
        self.controller.finish_failed.connect(self.failed.append)
        self.rig.service.captured.connect(self.captured.append)
        self.rig.service.cancelled.connect(self.cancelled.append)
        self.controller.start()

    # ---- shortcuts to the pieces --------------------------------------------------------------------
    @property
    def session(self) -> Any:
        return self.controller.session

    @property
    def editor(self) -> Any:
        return self.controller.editor

    @property
    def service(self) -> CaptureService:
        return self.rig.service

    def shutdown(self) -> None:
        try:
            self.controller.shutdown()
        finally:
            self.rig.close()
            ed = self.controller.editor
            if ed is not None:
                ed.deleteLater()
            self.toast.deleteLater()
            settle(10)
