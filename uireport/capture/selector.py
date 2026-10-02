"""Frozen-screen, multi-monitor region selector. Owner: capture builder (package A).

Coordinate rules (CONTRACT section 4):
  * everything stored or emitted (Selection.rect, hit-testing, hover rects) is in virtual-screen
    PHYSICAL pixels;
  * a widget works in Qt LOGICAL pixels; the conversion is done per monitor with the ratio
    monitor.rect.w / widget.width() (never a global scale), see `widget_point_to_physical`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPen,
    QRegion,
)
from PySide6.QtWidgets import QWidget

from ..geometry import IntRect
from ..models import MonitorMeta, monitor_at_point
from . import winapi
from .monitors import find_qscreen
from .windowinfo import WindowSnapshot, window_at_point

CLICK_THRESHOLD_PX = 4  # movement (widget/logical px) below which a press+release is a click
EDGE_SNAP_PX = 1.0  # a drag within this distance of a monitor's far edge snaps to the edge
HINT_TEXT = "Drag: region   ·   Click: window   ·   F / Enter: whole screen   ·   Esc: cancel"

_DIM = QColor(0, 0, 0, 120)
_ACCENT = QColor("#3E9BFF")
_PILL_BG = QColor(20, 20, 24, 220)
_PILL_FG = QColor(255, 255, 255)


# ---------------------------------------------------------------------------------------
# Pure geometry (unit-tested with synthetic monitors)
# ---------------------------------------------------------------------------------------
def widget_point_to_physical(
    x: float, y: float, widget_w: float, widget_h: float, monitor: MonitorMeta
) -> tuple[float, float]:
    """Widget-local logical point -> virtual-screen physical point.

    The ratio is per monitor: monitor.rect.w / widget.width() (and the same for height), so
    it is exact for 100/125/150/200 % and mixed-DPI layouts, and independent of Qt's
    rounding of the widget size. Then the monitor origin (may be negative) is added."""
    rx = monitor.rect.w / widget_w if widget_w > 0 else 1.0
    ry = monitor.rect.h / widget_h if widget_h > 0 else 1.0
    return (monitor.rect.x + x * rx, monitor.rect.y + y * ry)


def physical_to_widget_point(
    px: float, py: float, widget_w: float, widget_h: float, monitor: MonitorMeta
) -> tuple[float, float]:
    """Inverse of widget_point_to_physical."""
    rx = monitor.rect.w / widget_w if widget_w > 0 else 1.0
    ry = monitor.rect.h / widget_h if widget_h > 0 else 1.0
    return ((px - monitor.rect.x) / rx, (py - monitor.rect.y) / ry)


def physical_rect_to_widget(rect: IntRect, widget_w: float, widget_h: float, monitor: MonitorMeta) -> QRectF:
    x0, y0 = physical_to_widget_point(rect.x, rect.y, widget_w, widget_h, monitor)
    x1, y1 = physical_to_widget_point(rect.right, rect.bottom, widget_w, widget_h, monitor)
    return QRectF(x0, y0, x1 - x0, y1 - y0)


def clamp_point_to_monitor(px: float, py: float, monitor: MonitorMeta, snap: float = EDGE_SNAP_PX) -> tuple[float, float]:
    """Clamp a physical point to the monitor rectangle (edges inclusive, as rectangle edge
    coordinates). A point on the last pixel row/column snaps to the far edge so a full-screen
    drag really covers the full monitor."""
    r = monitor.rect
    x = min(max(px, r.x), r.x + r.w)
    y = min(max(py, r.y), r.y + r.h)
    if x >= r.x + r.w - snap:
        x = r.x + r.w
    if y >= r.y + r.h - snap:
        y = r.y + r.h
    if x <= r.x + 0.5:
        x = r.x
    if y <= r.y + 0.5:
        y = r.y
    return (x, y)


def drag_rect(p0: tuple[float, float], p1: tuple[float, float], monitor: MonitorMeta) -> IntRect:
    """Selection rectangle (physical) for a drag between two physical points, clamped to
    `monitor` (the monitor where the drag started, so Shot.monitor is unambiguous)."""
    a = clamp_point_to_monitor(p0[0], p0[1], monitor)
    b = clamp_point_to_monitor(p1[0], p1[1], monitor)
    return IntRect.from_points(a[0], a[1], b[0], b[1]).clamp_to(monitor.rect)


def is_click(press: tuple[float, float], release: tuple[float, float], threshold: float = CLICK_THRESHOLD_PX) -> bool:
    """True when press and release (widget-local logical px) are closer than `threshold`
    on both axes -> treat as a click (capture the hovered window), not a drag."""
    return abs(release[0] - press[0]) < threshold and abs(release[1] - press[1]) < threshold


def window_selection_rect(window_rect: IntRect, monitor: MonitorMeta) -> Optional[IntRect]:
    """A window's frame clamped to the monitor under the cursor (None when it does not
    overlap that monitor)."""
    inter = window_rect.intersection(monitor.rect)
    return inter if inter is not None and not inter.is_empty else None


def readout_text(rect: IntRect) -> str:
    return f"{rect.w} x {rect.h} px"


@dataclass(frozen=True)
class Selection:
    kind: str  # "region" | "window" | "monitor"   (== Shot.selection)
    rect: IntRect  # virtual-screen PHYSICAL pixels, non-empty, inside ONE monitor
    monitor: MonitorMeta  # the monitor the selection lies on
    window: Optional[WindowSnapshot] = None  # set when kind == "window"


# ---------------------------------------------------------------------------------------
# One overlay per monitor
# ---------------------------------------------------------------------------------------
class _Overlay(QWidget):
    """Frameless, always-on-top, tool window painting one monitor's slice of the frozen grab."""

    def __init__(self, selector: "RegionSelector", monitor: MonitorMeta, frozen: QImage, frozen_origin: IntRect) -> None:
        flags = (
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        super().__init__(None, flags)
        # NB: no WA_ShowWithoutActivating - the selector needs the keyboard focus.
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setWindowTitle("UI Report selector")
        self._selector = selector
        self.monitor = monitor
        self._frozen = frozen
        self._src = QRectF(
            monitor.rect.x - frozen_origin.x, monitor.rect.y - frozen_origin.y, monitor.rect.w, monitor.rect.h
        )
        self._bright: Optional[IntRect] = None  # physical rect punched out of the dimming
        self._bright_is_drag = False
        self._readout = ""
        self._show_hint = False

    # ---- state pushed by the selector ---------------------------------------------------
    def to_physical(self, pos: QPointF) -> tuple[float, float]:
        return widget_point_to_physical(pos.x(), pos.y(), self.width(), self.height(), self.monitor)

    def _bright_widget(self) -> Optional[QRectF]:
        if self._bright is None:
            return None
        return physical_rect_to_widget(self._bright, self.width(), self.height(), self.monitor)

    def _dirty_rect(self) -> QRect:
        b = self._bright_widget()
        if b is None:
            return QRect()
        r = b.toAlignedRect()
        wide = QRect(r.left() - 8, r.top() - 56, max(r.width(), 260) + 16, r.height() + 112)
        return r.adjusted(-8, -56, 8, 56).united(wide)

    def set_shapes(self, bright: Optional[IntRect], is_drag: bool, readout: str, show_hint: bool) -> None:
        if bright == self._bright and readout == self._readout and show_hint == self._show_hint and is_drag == self._bright_is_drag:
            return
        old = self._dirty_rect()
        hint_changed = show_hint != self._show_hint
        self._bright, self._bright_is_drag, self._readout, self._show_hint = bright, is_drag, readout, show_hint
        if hint_changed or (old.isNull() and bright is None):
            self.update()
            return
        region = old.united(self._dirty_rect())
        if region.isNull():
            self.update()
        else:
            self.update(region)

    # ---- painting --------------------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        try:
            w, h = self.width(), self.height()
            p.drawImage(QRectF(0, 0, w, h), self._frozen, self._src)
            bright = self._bright_widget()
            whole = QRegion(0, 0, w, h)
            dim_region = whole if bright is None else whole.subtracted(QRegion(bright.toAlignedRect()))
            p.save()
            p.setClipRegion(dim_region, Qt.ClipOperation.IntersectClip)
            p.fillRect(0, 0, w, h, _DIM)
            p.restore()
            if bright is not None:
                pen = QPen(_ACCENT)
                pen.setCosmetic(True)
                pen.setWidth(2)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(bright.adjusted(-1, -1, 1, 1))
                if self._readout:
                    self._paint_pill(p, self._readout, self._readout_anchor(p, bright), bold=True)
            if self._show_hint:
                self._paint_pill(p, HINT_TEXT, None, bold=False)
        finally:
            p.end()

    def _pill_font(self, bold: bool) -> QFont:
        f = QFont(self.font())
        f.setPointSizeF(max(9.0, f.pointSizeF()))
        f.setBold(bold)
        return f

    def _pill_rect(self, text: str, bold: bool) -> QRect:
        fm = QFontMetrics(self._pill_font(bold))
        return QRect(0, 0, fm.horizontalAdvance(text) + 20, fm.height() + 10)

    def _readout_anchor(self, p: QPainter, bright: QRectF) -> QPoint:
        pill = self._pill_rect(self._readout, True)
        x = int(bright.left())
        y = int(bright.bottom()) + 8
        if y + pill.height() > self.height() - 4:
            y = int(bright.top()) - pill.height() - 8
        if y < 4:
            y = int(bright.top()) + 8
        x = max(4, min(x, self.width() - pill.width() - 4))
        return QPoint(x, y)

    def _paint_pill(self, p: QPainter, text: str, at: Optional[QPoint], bold: bool) -> None:
        rect = self._pill_rect(text, bold)
        if at is None:  # hint: top centre
            rect.moveTo((self.width() - rect.width()) // 2, 16)
        else:
            rect.moveTo(at)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_PILL_BG)
        p.drawRoundedRect(rect, 6, 6)
        p.setPen(_PILL_FG)
        p.setFont(self._pill_font(bold))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        p.restore()

    # ---- input -> selector ----------------------------------------------------------------------
    def mouseMoveEvent(self, ev: QMouseEvent) -> None:  # noqa: N802
        self._selector._on_mouse_move(self, ev.position())

    def mousePressEvent(self, ev: QMouseEvent) -> None:  # noqa: N802
        self._selector._on_mouse_press(self, ev.position(), ev.button())

    def mouseReleaseEvent(self, ev: QMouseEvent) -> None:  # noqa: N802
        self._selector._on_mouse_release(self, ev.position(), ev.button())

    def keyPressEvent(self, ev: QKeyEvent) -> None:  # noqa: N802
        if not self._selector._on_key(self, ev.key()):
            super().keyPressEvent(ev)

    def contextMenuEvent(self, ev) -> None:  # noqa: N802
        ev.accept()  # right-click cancels; never pop a menu

    def enterEvent(self, ev: QEvent) -> None:  # noqa: N802
        self._selector._on_enter(self)


# ---------------------------------------------------------------------------------------
class RegionSelector(QObject):
    """Shows one frameless, always-on-top, dimmed overlay per monitor, each painting that
    monitor's slice of the FROZEN grab (so open menus / hover states stay visible).

    Interaction: drag = region (clamped to the monitor where the drag started);
    click without dragging = the window under the cursor (hover shows its outline;
    hit-tested against the pre-taken `windows` snapshot); F or Enter = whole monitor under
    the cursor; Esc or right-click = cancel. Shows a small hint bar and a live
    'W x H px' readout.

    Signals (emitted exactly once, on the GUI thread; the overlays are already closed):
        selected(Selection)
        cancelled()

    Optional keyword arguments are test seams: `cursor_pos` (physical cursor position),
    `key_down` (GetAsyncKeyState replacement for the Esc safety poll), `place` (False = do
    not position overlays on their QScreen; tests size them themselves).
    """

    selected = Signal(object)
    cancelled = Signal()

    def __init__(
        self,
        frozen: QImage,
        frozen_origin: IntRect,
        monitors: list[MonitorMeta],
        windows: list[WindowSnapshot],
        parent: Optional[QObject] = None,
        *,
        cursor_pos: Optional[Callable[[], tuple[int, int]]] = None,
        key_down: Optional[Callable[[int], bool]] = None,
        place: bool = True,
    ) -> None:
        super().__init__(parent)
        self._frozen = frozen
        self._origin = frozen_origin
        self._monitors = list(monitors)
        self._windows = list(windows)
        self._cursor_pos = cursor_pos or winapi.cursor_pos
        self._key_down = key_down or winapi.is_key_down
        self._place = place
        self._overlays: dict[int, _Overlay] = {}
        self._started = False
        self._done = False
        # interaction state
        self._press_overlay: Optional[_Overlay] = None
        self._press_widget: tuple[float, float] = (0.0, 0.0)
        self._press_phys: tuple[float, float] = (0.0, 0.0)
        self._dragging = False
        self._right_down = False
        self._drag_rect: Optional[IntRect] = None
        self._hover_window: Optional[WindowSnapshot] = None
        self._hover_rect: Optional[IntRect] = None
        self._hover_monitor: Optional[MonitorMeta] = None
        self._esc_timer: Optional[QTimer] = None
        self._esc_was_down = False

    # ---- lifecycle -----------------------------------------------------------------------
    @property
    def overlays(self) -> list[QWidget]:
        return list(self._overlays.values())

    def overlay_for(self, monitor: MonitorMeta) -> Optional[QWidget]:
        return self._overlays.get(monitor.index)

    def start(self) -> None:
        """Create + show the overlays and grab keyboard focus on the one under the cursor."""
        if self._started or self._done:
            return
        self._started = True
        for mon in self._monitors:
            ov = _Overlay(self, mon, self._frozen, self._origin)
            self._overlays[mon.index] = ov
            if self._place:
                self._place_overlay(ov, mon)
        if not self._overlays:
            QTimer.singleShot(0, self._finish_cancelled)
            return
        cur = self._cursor_monitor()
        for ov in self._overlays.values():
            ov.show()
        for ov in self._overlays.values():
            if self._place:
                self._fix_native_geometry(ov)
        self._hover_monitor = cur
        self._update_hover_from_cursor()
        self._focus_overlay(self._overlays.get(cur.index) if cur else next(iter(self._overlays.values())))
        self._push_shapes()
        # Safety net: if Windows refuses to give an overlay the keyboard focus, Esc must still cancel.
        self._esc_was_down = bool(self._key_down(winapi.VK_ESCAPE))
        self._esc_timer = QTimer(self)
        self._esc_timer.setInterval(30)
        self._esc_timer.timeout.connect(self._poll_escape)
        self._esc_timer.start()

    def close(self) -> None:
        """Close all overlays (idempotent). Does not emit a signal."""
        if self._esc_timer is not None:
            self._esc_timer.stop()
            self._esc_timer.deleteLater()
            self._esc_timer = None
        overlays = list(self._overlays.values())
        self._overlays.clear()
        for ov in overlays:
            try:
                ov.hide()
                ov.close()
                ov.deleteLater()
            except RuntimeError:
                pass  # already deleted
        self._press_overlay = None

    # ---- placement --------------------------------------------------------------------------
    def _place_overlay(self, ov: _Overlay, mon: MonitorMeta) -> None:
        screen = find_qscreen(mon)
        if screen is not None:
            ov.setScreen(screen)
            ov.setGeometry(screen.geometry())
        else:
            ov.setGeometry(mon.rect.x, mon.rect.y, mon.rect.w, mon.rect.h)

    def _fix_native_geometry(self, ov: _Overlay) -> None:
        """Guarantee a pixel-exact cover of the monitor on real Windows (Qt rounds logical
        sizes on 125%/150% screens). Skipped on offscreen/other Qt platforms."""
        if QGuiApplication.platformName() != "windows":
            return
        hwnd = winapi.widget_hwnd(ov)
        cur = winapi.get_window_rect(hwnd)
        r = ov.monitor.rect
        if cur is None or abs(cur[0] - r.x) > 1 or abs(cur[1] - r.y) > 1 or abs(cur[2] - r.w) > 1 or abs(cur[3] - r.h) > 1:
            winapi.set_window_rect_physical(hwnd, r.x, r.y, r.w, r.h, topmost=True)

    def _focus_overlay(self, ov: Optional[_Overlay]) -> None:
        if ov is None:
            return
        ov.raise_()
        ov.activateWindow()
        ov.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        if QGuiApplication.platformName() == "windows":
            winapi.force_foreground(winapi.widget_hwnd(ov))

    # ---- state helpers ---------------------------------------------------------------------------
    def _cursor_monitor(self) -> Optional[MonitorMeta]:
        try:
            x, y = self._cursor_pos()
        except Exception:  # noqa: BLE001
            x, y = 0, 0
        return monitor_at_point(self._monitors, x, y) or (self._monitors[0] if self._monitors else None)

    def _update_hover_from_cursor(self) -> None:
        try:
            x, y = self._cursor_pos()
        except Exception:  # noqa: BLE001
            return
        mon = monitor_at_point(self._monitors, x, y)
        if mon is not None:
            self._hover_monitor = mon
            self._set_hover(x, y, mon)

    def _set_hover(self, px: float, py: float, mon: MonitorMeta) -> None:
        w = window_at_point(self._windows, int(px), int(py))
        rect = window_selection_rect(w.rect, mon) if w is not None else None
        if rect is None:
            w = None
        self._hover_window, self._hover_rect = w, rect

    def _push_shapes(self) -> None:
        """Send the current drag/hover shapes to the overlays."""
        for idx, ov in list(self._overlays.items()):
            show_hint = self._hover_monitor is not None and self._hover_monitor.index == idx
            if self._drag_rect is not None and self._press_overlay is not None:
                if ov is self._press_overlay:
                    ov.set_shapes(self._drag_rect, True, readout_text(self._drag_rect), show_hint)
                else:
                    ov.set_shapes(None, False, "", show_hint)
            elif self._hover_rect is not None and self._hover_monitor is not None and self._hover_monitor.index == idx:
                ov.set_shapes(self._hover_rect, False, readout_text(self._hover_rect), show_hint)
            else:
                ov.set_shapes(None, False, "", show_hint)

    # ---- events from the overlays ---------------------------------------------------------------------
    def _on_enter(self, ov: _Overlay) -> None:
        if self._done or self._press_overlay is not None:
            return
        if self._hover_monitor is None or self._hover_monitor.index != ov.monitor.index:
            self._hover_monitor = ov.monitor
            self._hover_window, self._hover_rect = None, None
            self._push_shapes()

    def _on_mouse_move(self, ov: _Overlay, pos: QPointF) -> None:
        if self._done:
            return
        if self._press_overlay is not None:
            if ov is not self._press_overlay:
                return  # implicit grab keeps moves on the pressed overlay; ignore strays
            if not self._dragging and not is_click(self._press_widget, (pos.x(), pos.y())):
                self._dragging = True
            if self._dragging:
                cur = ov.to_physical(pos)
                self._drag_rect = drag_rect(self._press_phys, cur, ov.monitor)
                self._hover_window, self._hover_rect = None, None
            self._push_shapes()
            return
        px, py = ov.to_physical(pos)
        self._hover_monitor = ov.monitor
        self._set_hover(px, py, ov.monitor)
        self._push_shapes()

    def _on_mouse_press(self, ov: _Overlay, pos: QPointF, button: Qt.MouseButton) -> None:
        if self._done:
            return
        if button == Qt.MouseButton.RightButton:
            self._right_down = True  # cancel on release so the click does not reach the app below
            return
        if button != Qt.MouseButton.LeftButton or self._right_down:
            return
        self._press_overlay = ov
        self._hover_monitor = ov.monitor
        self._press_widget = (pos.x(), pos.y())
        self._press_phys = ov.to_physical(pos)
        self._dragging = False
        self._drag_rect = None

    def _on_mouse_release(self, ov: _Overlay, pos: QPointF, button: Qt.MouseButton) -> None:
        if self._done:
            return
        if button == Qt.MouseButton.RightButton:
            if self._right_down:
                self._finish_cancelled()
            return
        if button != Qt.MouseButton.LeftButton or self._press_overlay is None:
            return
        press_ov = self._press_overlay
        dragging = self._dragging or not is_click(self._press_widget, (pos.x(), pos.y()))
        press_phys = self._press_phys
        self._press_overlay = None
        self._dragging = False
        self._drag_rect = None
        if dragging:
            cur = press_ov.to_physical(pos)
            rect = drag_rect(press_phys, cur, press_ov.monitor)
            if rect.w >= 1 and rect.h >= 1:
                self._finish_selected(Selection("region", rect, press_ov.monitor, None))
                return
            self._push_shapes()  # degenerate (zero-width) drag: keep selecting
            return
        # a click: capture the window under the cursor
        px, py = press_ov.to_physical(pos)
        w = window_at_point(self._windows, int(px), int(py))
        rect = window_selection_rect(w.rect, press_ov.monitor) if w is not None else None
        if w is not None and rect is not None:
            self._finish_selected(Selection("window", rect, press_ov.monitor, w))
        else:
            self._set_hover(px, py, press_ov.monitor)
            self._push_shapes()

    def _on_key(self, ov: _Overlay, key: int) -> bool:
        if self._done:
            return True
        if key == Qt.Key.Key_Escape:
            self._finish_cancelled()
            return True
        if key in (Qt.Key.Key_F, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            mon = self._cursor_monitor() or ov.monitor
            self._finish_selected(Selection("monitor", mon.rect, mon, None))
            return True
        return False

    def _poll_escape(self) -> None:
        down = bool(self._key_down(winapi.VK_ESCAPE))
        if down and not self._esc_was_down:
            self._finish_cancelled()
        self._esc_was_down = down

    # ---- terminal transitions ----------------------------------------------------------------------------
    def _finish_selected(self, sel: Selection) -> None:
        if self._done:
            return
        self._done = True
        self.close()
        self.selected.emit(sel)

    def _finish_cancelled(self) -> None:
        if self._done:
            return
        self._done = True
        self.close()
        self.cancelled.emit()
