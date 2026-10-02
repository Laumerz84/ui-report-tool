"""RegionSelector behaviour with synthetic monitors, offscreen overlays and injected input."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from uireport.capture.selector import RegionSelector, Selection
from uireport.capture.windowinfo import WindowSnapshot
from uireport.geometry import IntRect, dpi_from_scale_percent
from uireport.models import MonitorMeta

LEFT = Qt.MouseButton.LeftButton
RIGHT = Qt.MouseButton.RightButton


def M(index, x, y, w, h, pct=100, primary=False):
    return MonitorMeta(index=index, name=rf"\\.\DISPLAY{index}", is_primary=primary,
                       rect=IntRect(x, y, w, h), dpi=dpi_from_scale_percent(pct))


def frozen_for(monitors, color="#C86432"):
    r = IntRect(0, 0, 0, 0)
    for m in monitors:
        r = r.union(m.rect)
    img = QImage(r.w, r.h, QImage.Format.Format_RGB32)
    img.fill(QColor(color))
    return img, r


class Harness:
    def __init__(self, monitors, windows=(), cursor=(10, 10), esc_down=lambda: False, color="#C86432"):
        img, origin = frozen_for(monitors, color)
        self.monitors = monitors
        self.cursor = list(cursor)
        self.esc = {"down": False}
        self.sel = RegionSelector(
            img, origin, monitors, list(windows), place=False,
            cursor_pos=lambda: tuple(self.cursor),
            key_down=lambda vk: self.esc["down"],
        )
        self.selected: list[Selection] = []
        self.cancelled = 0
        self.sel.selected.connect(self.selected.append)
        self.sel.cancelled.connect(lambda: setattr(self, "cancelled", self.cancelled + 1))

    def start(self, sizes=None):
        self.sel.start()
        for m in self.monitors:
            ov = self.sel.overlay_for(m)
            w, h = (sizes or {}).get(m.index, (round(m.rect.w / m.scale_factor), round(m.rect.h / m.scale_factor)))
            ov.resize(w, h)
        return self

    def ov(self, index):
        return self.sel.overlay_for(next(m for m in self.monitors if m.index == index))

    @staticmethod
    def _mouse(kind, ov, x, y, button, buttons):
        ev = QMouseEvent(kind, QPointF(x, y), QPointF(x, y), button, buttons, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(ov, ev)

    def press(self, ov, x, y, button=LEFT):
        self._mouse(QEvent.Type.MouseButtonPress, ov, x, y, button, button)

    def move(self, ov, x, y, buttons=Qt.MouseButton.NoButton):
        self._mouse(QEvent.Type.MouseMove, ov, x, y, Qt.MouseButton.NoButton, buttons)

    def release(self, ov, x, y, button=LEFT):
        self._mouse(QEvent.Type.MouseButtonRelease, ov, x, y, button, Qt.MouseButton.NoButton)

    def key(self, ov, key):
        QApplication.sendEvent(ov, QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier))

    def drag(self, ov, x0, y0, x1, y1):
        self.press(ov, x0, y0)
        self.move(ov, (x0 + x1) / 2, (y0 + y1) / 2, LEFT)
        self.move(ov, x1, y1, LEFT)
        self.release(ov, x1, y1)


@pytest.fixture
def cleanup(qapp):
    made: list[Harness] = []
    yield made
    for h in made:
        h.sel.close()
    qapp.processEvents()


def test_drag_100_percent(qapp, cleanup):
    h = Harness([M(1, 0, 0, 800, 600, 100, True)])
    cleanup.append(h)
    h.start()
    h.drag(h.ov(1), 100, 50, 300, 200)
    assert h.cancelled == 0 and len(h.selected) == 1
    s = h.selected[0]
    assert s.kind == "region" and s.rect == IntRect(100, 50, 200, 150) and s.window is None
    assert s.monitor.index == 1
    assert h.sel.overlays == []  # overlays are already closed when the signal fires


def test_drag_on_a_150_percent_monitor_maps_to_physical_pixels(qapp, cleanup):
    m = M(1, 0, 0, 3840, 2160, 150, True)
    h = Harness([m])
    cleanup.append(h)
    h.start()  # overlay sized 2560 x 1440 (logical)
    ov = h.ov(1)
    assert (ov.width(), ov.height()) == (2560, 1440)
    h.drag(ov, 100, 100, 300, 250)
    assert h.selected[0].rect == IntRect(150, 150, 300, 225)


def test_drag_on_secondary_monitor_with_negative_origin(qapp, cleanup):
    left = M(1, -2560, -180, 2560, 1440, 125)
    primary = M(2, 0, 0, 1920, 1080, 100, True)
    h = Harness([left, primary])
    cleanup.append(h)
    h.start()
    ov = h.ov(1)
    assert (ov.width(), ov.height()) == (2048, 1152)
    h.drag(ov, 80, 40, 480, 240)  # 1.25 physical px per logical px
    s = h.selected[0]
    assert s.rect == IntRect(-2560 + 100, -180 + 50, 500, 250)
    assert s.monitor.index == 1 and s.monitor.rect.contains_rect(s.rect)


def test_drag_is_clamped_to_the_monitor_where_it_started(qapp, cleanup):
    a = M(1, 0, 0, 3840, 2160, 150, True)
    b = M(2, 3840, 0, 1920, 1080, 100)
    h = Harness([a, b])
    cleanup.append(h)
    h.start()
    ov_b = h.ov(2)
    # press on B, drag out to the left (into A) and up: the events still arrive at B's overlay
    h.press(ov_b, 500, 300)
    h.move(ov_b, 100, 100, LEFT)
    h.move(ov_b, -900, -50, LEFT)
    h.release(ov_b, -900, -50)
    s = h.selected[0]
    assert s.monitor.index == 2
    assert s.rect == IntRect(3840, 0, 500, 300)
    assert s.monitor.rect.contains_rect(s.rect)


def test_tiny_drag_below_threshold_is_a_click_on_a_window(qapp, cleanup):
    m = M(1, 0, 0, 800, 600, 100, True)
    win = WindowSnapshot(hwnd=111, rect=IntRect(50, 60, 400, 300), title="w", pid=5)
    h = Harness([m], [win])
    cleanup.append(h)
    h.start()
    ov = h.ov(1)
    h.press(ov, 200, 200)
    h.move(ov, 202, 201, LEFT)
    h.release(ov, 202, 201)
    assert len(h.selected) == 1
    s = h.selected[0]
    assert s.kind == "window" and s.window is win and s.rect == IntRect(50, 60, 400, 300)


def test_click_picks_topmost_window_and_clamps_to_the_monitor(qapp, cleanup):
    m = M(1, 0, 0, 800, 600, 100, True)
    top = WindowSnapshot(1, IntRect(100, 100, 300, 200), "top", 1)
    big = WindowSnapshot(2, IntRect(-200, -100, 2000, 900), "big", 2)  # extends past the monitor
    h = Harness([m], [top, big])  # z-order: topmost first
    cleanup.append(h)
    h.start()
    ov = h.ov(1)
    h.press(ov, 150, 150)
    h.release(ov, 150, 150)
    assert h.selected[0].window is top
    h2 = Harness([m], [top, big])
    cleanup.append(h2)
    h2.start()
    h2.press(h2.ov(1), 700, 500)
    h2.release(h2.ov(1), 700, 500)
    s = h2.selected[0]
    assert s.window is big and s.rect == IntRect(0, 0, 800, 600)  # clamped to the monitor


def test_hover_shows_the_window_outline_and_size_readout(qapp, cleanup):
    m = M(1, 0, 0, 800, 600, 100, True)
    win = WindowSnapshot(1, IntRect(100, 100, 300, 200), "w", 1)
    h = Harness([m], [win], cursor=(5, 5))
    cleanup.append(h)
    h.start()
    ov = h.ov(1)
    assert ov._bright is None
    h.move(ov, 150, 150)
    assert ov._bright == IntRect(100, 100, 300, 200) and ov._readout == "300 x 200 px"
    h.move(ov, 700, 550)
    assert ov._bright is None and ov._readout == ""


def test_initial_hover_uses_the_cursor_position(qapp, cleanup):
    m = M(1, 0, 0, 800, 600, 100, True)
    win = WindowSnapshot(1, IntRect(100, 100, 300, 200), "w", 1)
    h = Harness([m], [win], cursor=(120, 130))
    cleanup.append(h)
    h.start()
    assert h.ov(1)._bright == IntRect(100, 100, 300, 200)


def test_live_readout_while_dragging(qapp, cleanup):
    m = M(1, 0, 0, 800, 600, 100, True)
    h = Harness([m])
    cleanup.append(h)
    h.start()
    ov = h.ov(1)
    h.press(ov, 10, 10)
    h.move(ov, 110, 60, LEFT)
    assert ov._bright == IntRect(10, 10, 100, 50) and ov._readout == "100 x 50 px"
    assert h.selected == []
    h.release(ov, 110, 60)
    assert h.selected[0].rect == IntRect(10, 10, 100, 50)


def test_click_on_empty_space_does_nothing(qapp, cleanup):
    m = M(1, 0, 0, 800, 600, 100, True)
    h = Harness([m], [])
    cleanup.append(h)
    h.start()
    h.press(h.ov(1), 100, 100)
    h.release(h.ov(1), 100, 100)
    assert h.selected == [] and h.cancelled == 0
    assert len(h.sel.overlays) == 1  # still selecting


def test_f_and_enter_capture_the_monitor_under_the_cursor(qapp, cleanup):
    a = M(1, 0, 0, 1920, 1080, 100, True)
    b = M(2, 1920, 0, 1920, 1080, 100)
    for key in (Qt.Key.Key_F, Qt.Key.Key_Return, Qt.Key.Key_Enter):
        h = Harness([a, b], cursor=(2500, 400))
        cleanup.append(h)
        h.start()
        h.key(h.ov(1), key)  # even if the key event lands on the other overlay
        assert len(h.selected) == 1, key
        s = h.selected[0]
        assert s.kind == "monitor" and s.monitor.index == 2 and s.rect == b.rect and s.window is None
        assert h.cancelled == 0


def test_escape_cancels(qapp, cleanup):
    h = Harness([M(1, 0, 0, 800, 600, 100, True)])
    cleanup.append(h)
    h.start()
    h.key(h.ov(1), Qt.Key.Key_Escape)
    assert h.cancelled == 1 and h.selected == [] and h.sel.overlays == []
    h.sel.close()
    h.sel.close()  # idempotent
    assert h.cancelled == 1


def test_right_click_cancels_on_release_only(qapp, cleanup):
    h = Harness([M(1, 0, 0, 800, 600, 100, True)])
    cleanup.append(h)
    h.start()
    ov = h.ov(1)
    h.press(ov, 100, 100, RIGHT)
    assert h.cancelled == 0  # the press is swallowed; the release must not reach the app below either
    h.release(ov, 100, 100, RIGHT)
    assert h.cancelled == 1 and h.selected == []


def test_esc_poll_safety_net_cancels_when_the_overlay_has_no_focus(qapp, cleanup):
    h = Harness([M(1, 0, 0, 800, 600, 100, True)])
    cleanup.append(h)
    h.start()
    h.sel._poll_escape()
    assert h.cancelled == 0
    h.esc["down"] = True
    h.sel._poll_escape()
    h.sel._poll_escape()  # held key: still one cancel
    assert h.cancelled == 1


def test_esc_already_down_at_start_does_not_cancel(qapp, cleanup):
    m = M(1, 0, 0, 800, 600, 100, True)
    img, origin = frozen_for([m])
    state = {"down": True}
    sel = RegionSelector(img, origin, [m], [], place=False, cursor_pos=lambda: (1, 1), key_down=lambda vk: state["down"])
    got = []
    sel.cancelled.connect(lambda: got.append(1))
    sel.start()
    try:
        sel._poll_escape()
        assert got == []
        state["down"] = False
        sel._poll_escape()
        state["down"] = True
        sel._poll_escape()
        assert got == [1]
    finally:
        sel.close()


def test_exactly_one_terminal_signal(qapp, cleanup):
    h = Harness([M(1, 0, 0, 800, 600, 100, True)])
    cleanup.append(h)
    h.start()
    ov = h.ov(1)
    h.drag(ov, 10, 10, 200, 100)
    h.key(ov, Qt.Key.Key_Escape)  # late events after completion are ignored
    h.key(ov, Qt.Key.Key_F)
    assert len(h.selected) == 1 and h.cancelled == 0


def test_zero_width_drag_keeps_selecting(qapp, cleanup):
    h = Harness([M(1, 0, 0, 800, 600, 100, True)])
    cleanup.append(h)
    h.start()
    ov = h.ov(1)
    h.drag(ov, 100, 100, 100, 300)  # a vertical line: no area
    assert h.selected == [] and h.cancelled == 0 and len(h.sel.overlays) == 1


def test_overlays_paint_dimmed_background_with_the_selection_punched_out(qapp, cleanup):
    m = M(1, 0, 0, 400, 300, 100, True)
    h = Harness([m], color="#C86432")
    cleanup.append(h)
    h.start(sizes={1: (400, 300)})
    ov = h.ov(1)
    h.press(ov, 100, 80)
    h.move(ov, 220, 160, LEFT)  # drag in progress: shapes visible
    qapp.processEvents()
    img = ov.grab().toImage()
    inside = QColor(img.pixel(150, 120))
    outside = QColor(img.pixel(20, 20))
    assert (inside.red(), inside.green(), inside.blue()) == (0xC8, 0x64, 0x32)  # untouched frozen pixels
    assert outside.red() < 0xC8 and outside.green() < 0x64  # dimmed
    h.release(ov, 220, 160)


def test_overlay_paints_the_right_slice_of_the_frozen_grab(qapp, cleanup):
    """Two monitors, distinct colours: each overlay must show its own monitor's pixels."""
    a = M(1, 0, 0, 200, 100, 100, True)
    b = M(2, 200, 0, 200, 100, 100)
    img = QImage(400, 100, QImage.Format.Format_RGB32)
    img.fill(QColor("#FF0000"))
    from PySide6.QtGui import QPainter

    p = QPainter(img)
    p.fillRect(200, 0, 200, 100, QColor("#0000FF"))
    p.end()
    sel = RegionSelector(img, IntRect(0, 0, 400, 100), [a, b], [], place=False, cursor_pos=lambda: (1, 1), key_down=lambda v: False)
    sel.start()
    try:
        for m, expect_blue in ((a, False), (b, True)):
            ov = sel.overlay_for(m)
            ov.resize(200, 100)
            qapp.processEvents()
            c = QColor(ov.grab().toImage().pixel(100, 60))  # dimmed but hue preserved
            assert (c.blue() > c.red()) == expect_blue
    finally:
        sel.close()


def test_negative_frozen_origin_slices_correctly(qapp, cleanup):
    left = M(1, -200, 0, 200, 100, 100)
    right = M(2, 0, 0, 200, 100, 100, True)
    img = QImage(400, 100, QImage.Format.Format_RGB32)
    img.fill(QColor("#00FF00"))
    from PySide6.QtGui import QPainter

    p = QPainter(img)
    p.fillRect(0, 0, 200, 100, QColor("#FF0000"))  # the LEFT monitor's pixels are red
    p.end()
    sel = RegionSelector(img, IntRect(-200, 0, 400, 100), [left, right], [], place=False, cursor_pos=lambda: (1, 1), key_down=lambda v: False)
    sel.start()
    try:
        ov = sel.overlay_for(left)
        ov.resize(200, 100)
        qapp.processEvents()
        c = QColor(ov.grab().toImage().pixel(50, 50))
        assert c.red() > c.green()
    finally:
        sel.close()


def test_closing_overlays_never_triggers_quit_on_last_window_closed(qapp, cleanup):
    """A tray app has no other visible window; closing the overlays must not quit it."""
    fired = []
    qapp.lastWindowClosed.connect(lambda: fired.append(1))
    old = qapp.quitOnLastWindowClosed()
    qapp.setQuitOnLastWindowClosed(True)
    try:
        h = Harness([M(1, 0, 0, 800, 600, 100, True)])
        cleanup.append(h)
        h.start()
        assert all(not ov.testAttribute(Qt.WidgetAttribute.WA_QuitOnClose) for ov in h.sel.overlays)
        h.key(h.ov(1), Qt.Key.Key_Escape)
        qapp.processEvents()
        assert h.cancelled == 1 and fired == []
    finally:
        qapp.setQuitOnLastWindowClosed(old)


def test_overlays_are_frameless_topmost_tool_windows_that_may_take_focus(qapp, cleanup):
    h = Harness([M(1, 0, 0, 800, 600, 100, True)])
    cleanup.append(h)
    h.start()
    ov = h.ov(1)
    f = ov.windowFlags()
    assert f & Qt.WindowType.FramelessWindowHint and f & Qt.WindowType.WindowStaysOnTopHint and f & Qt.WindowType.Tool
    assert not ov.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)  # the selector needs the keyboard
    assert not (f & Qt.WindowType.WindowDoesNotAcceptFocus)
    assert ov.hasMouseTracking() and ov.cursor().shape() == Qt.CursorShape.CrossCursor


def test_overlays_are_destroyed_on_every_exit_path(qapp, cleanup):
    import gc

    from shiboken6 import isValid

    for how in ("select", "escape", "close"):
        h = Harness([M(1, 0, 0, 800, 600, 100, True)])
        cleanup.append(h)
        h.start()
        ov = h.ov(1)
        if how == "select":
            h.drag(ov, 10, 10, 100, 100)
        elif how == "escape":
            h.key(ov, Qt.Key.Key_Escape)
        else:
            h.sel.close()
        assert h.sel.overlays == []
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qapp.processEvents()
        assert not isValid(ov) or not ov.isVisible()
