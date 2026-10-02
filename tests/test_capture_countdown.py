"""CountdownOverlay: timing, Esc handling and cleanup with fakes (no real hotkey, no real key state)."""
from __future__ import annotations

import time

import pytest
from PySide6.QtCore import QEventLoop, QObject, Qt, QTimer, Signal

from uireport.capture.countdown import CountdownOverlay


class FakeEsc(QObject):
    """Stands in for TemporaryHotkey."""

    pressed = Signal()

    def __init__(self, ok=True):
        super().__init__()
        self.ok = ok
        self.acquired = 0
        self.released = 0

    def acquire(self):
        self.acquired += 1
        return self.ok

    def release(self):
        self.released += 1


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make(seconds=3, esc_ok=True, key_down=None):
    clock = Clock()
    esc = FakeEsc(esc_ok)
    cd = CountdownOverlay(
        seconds, None, None, esc_factory=lambda: esc, clock=clock, use_native=False, place=False,
        key_down=key_down or (lambda vk: False),
    )
    log = {"finished": 0, "cancelled": 0}
    cd.finished.connect(lambda: log.__setitem__("finished", log["finished"] + 1))
    cd.cancelled.connect(lambda: log.__setitem__("cancelled", log["cancelled"] + 1))
    return cd, clock, esc, log


def pump(qapp, ms=30):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def test_counts_down_and_finishes_once(qapp):
    cd, clock, esc, log = make(3)
    try:
        cd.start()
        assert cd.is_visible and cd.widget.number == 3 and esc.acquired == 1 and cd.uses_global_esc
        clock.t += 0.4
        cd._on_tick()
        assert cd.widget.number == 3
        clock.t += 0.7  # 1.1 s elapsed
        cd._on_tick()
        assert cd.widget.number == 2
        clock.t += 1.0  # 2.1 s
        cd._on_tick()
        assert cd.widget.number == 1
        assert log["finished"] == 0
        clock.t += 0.95  # 3.05 s
        cd._on_tick()
        assert log["finished"] == 1 and not cd.is_visible  # hidden immediately when the count hits zero
        cd._on_tick()
        assert log["finished"] == 1
        assert log["cancelled"] == 0
    finally:
        cd.close()
    assert esc.released == 1


def test_badge_never_takes_focus_or_input(qapp):
    cd, clock, esc, log = make(3)
    try:
        cd.start()
        f = cd.widget.windowFlags()
        for flag in (
            Qt.WindowType.Tool, Qt.WindowType.WindowStaysOnTopHint, Qt.WindowType.WindowDoesNotAcceptFocus,
            Qt.WindowType.WindowTransparentForInput, Qt.WindowType.FramelessWindowHint,
        ):
            assert f & flag, flag
        assert cd.widget.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        assert cd.widget.focusPolicy() == Qt.FocusPolicy.NoFocus
    finally:
        cd.close()


def test_escape_cancels_once_and_hides(qapp):
    cd, clock, esc, log = make(3)
    try:
        cd.start()
        esc.pressed.emit()
        pump(qapp)  # queued connection
        assert log == {"finished": 0, "cancelled": 1}
        assert not cd.is_visible
        esc.pressed.emit()
        pump(qapp)
        clock.t += 10
        cd._on_tick()
        assert log == {"finished": 0, "cancelled": 1}
    finally:
        cd.close()
    assert esc.released == 1


def test_escape_after_finish_but_before_close_still_cancels(qapp):
    """The Esc grab stays armed during the short settle time before the grab."""
    cd, clock, esc, log = make(1)
    try:
        cd.start()
        clock.t += 1.5
        cd._on_tick()
        assert log["finished"] == 1
        assert esc.released == 0  # still armed
        cd._on_escape()
        assert log == {"finished": 1, "cancelled": 1}
    finally:
        cd.close()
    assert esc.released == 1


def test_close_is_idempotent_and_releases_the_grab(qapp):
    cd, clock, esc, log = make(3)
    cd.start()
    cd.close()
    cd.close()
    cd.hide()
    assert esc.released == 1
    assert cd.widget is None
    clock.t += 10
    cd._on_tick()
    assert log == {"finished": 0, "cancelled": 0}


def test_close_before_start_is_safe(qapp):
    cd, clock, esc, log = make(3)
    cd.close()
    cd.start()  # a closed overlay never starts
    assert cd.widget is None and esc.acquired == 0


def test_fallback_to_key_polling_when_the_hotkey_cannot_be_registered(qapp):
    state = {"down": False}
    cd, clock, esc, log = make(3, esc_ok=False, key_down=lambda vk: state["down"])
    try:
        cd.start()
        assert not cd.uses_global_esc
        cd._on_poll()
        assert log["cancelled"] == 0
        state["down"] = True
        cd._on_poll()
        cd._on_poll()
        assert log["cancelled"] == 1
    finally:
        cd.close()
    assert cd._poll is None


def test_polling_ignores_an_esc_that_was_already_held(qapp):
    state = {"down": True}
    cd, clock, esc, log = make(3, esc_ok=False, key_down=lambda vk: state["down"])
    try:
        cd.start()
        cd._on_poll()
        assert log["cancelled"] == 0
    finally:
        cd.close()


def test_real_timer_runs_for_about_one_second(qapp):
    cd = CountdownOverlay(1, None, None, esc_factory=lambda: FakeEsc(), use_native=False, place=False)
    done = []
    cd.finished.connect(lambda: done.append(time.monotonic()))
    t0 = time.monotonic()
    cd.start()
    loop = QEventLoop()
    cd.finished.connect(loop.quit)
    QTimer.singleShot(3000, loop.quit)
    try:
        loop.exec()
        assert done, "countdown never finished"
        assert 0.9 <= done[0] - t0 <= 1.6
        assert not cd.is_visible
    finally:
        cd.close()


def test_zero_seconds_finishes_immediately(qapp):
    cd, clock, esc, log = make(0)
    try:
        cd.start()
        assert log["finished"] == 1
    finally:
        cd.close()
