"""CaptureService state machine with fake countdown / selector / grab / window layers."""
from __future__ import annotations

import threading
import time

import pytest
from PySide6.QtCore import QEventLoop, QObject, QTimer, Signal
from PySide6.QtGui import QColor, QImage

from uireport.capture import service as service_mod
from uireport.capture.selector import Selection
from uireport.capture.service import CaptureDeps, CaptureService
from uireport.capture.windowinfo import WindowSnapshot
from uireport.geometry import IntRect, dpi_from_scale_percent
from uireport.models import CaptureMode, MonitorMeta, Role, WindowMeta
from uireport.settings import Settings

MON1 = MonitorMeta(index=1, name=r"\\.\DISPLAY1", is_primary=True, rect=IntRect(0, 0, 1000, 600), dpi=96)
MON2 = MonitorMeta(index=2, name=r"\\.\DISPLAY2", is_primary=False, rect=IntRect(1000, 0, 1500, 900), dpi=144)
ORIGIN = IntRect(0, 0, 2500, 900)
FG_HWND = 0xF00
OWN_PID = 4242


class FakeCountdown(QObject):
    finished = Signal()
    cancelled = Signal()

    def __init__(self, seconds, monitor, parent, log):
        super().__init__(parent)
        self.seconds = seconds
        self.monitor = monitor
        self.log = log
        self.started = False
        self.closed = 0
        log.append(("countdown.new", seconds, monitor.index if monitor else None))

    def start(self):
        self.started = True
        self.log.append("countdown.start")

    def hide(self):
        self.log.append("countdown.hide")

    def close(self):
        self.closed += 1
        self.log.append("countdown.close")


class FakeSelector(QObject):
    selected = Signal(object)
    cancelled = Signal()

    def __init__(self, image, origin, monitors, windows, parent, log):
        super().__init__(parent)
        self.image, self.origin, self.monitors, self.windows = image, origin, monitors, windows
        self.log = log
        self.started = False
        self.closed = 0

    def start(self):
        self.started = True
        self.log.append("selector.start")

    def close(self):
        self.closed += 1


class Rig:
    def __init__(self, qapp, **overrides):
        self.log: list = []
        self.countdowns: list[FakeCountdown] = []
        self.selectors: list[FakeSelector] = []
        self.grab_calls = 0
        self.grab_error: Exception | None = None
        self.url_calls: list[tuple] = []
        self.url_thread_names: list[str] = []
        self.url_result: str | None = "https://example.com/page"
        self.url_delay = 0.0
        self.url_release = threading.Event()
        self.fg_meta = WindowMeta(title="App", process_name="app.exe", exe_path=r"C:\app.exe", pid=77,
                                  rect=IntRect(10, 20, 600, 400), dpi=96)
        self.metas = {FG_HWND: self.fg_meta}
        self.settings = Settings(delay_seconds=3)
        self.image = QImage(2500, 900, QImage.Format.Format_RGB32)
        self.image.fill(QColor("#123456"))
        self.events: list = []

        def enum():
            return [MON1, MON2]

        def grab():
            self.grab_calls += 1
            self.log.append("grab")
            if self.grab_error:
                raise self.grab_error
            return self.image, ORIGIN

        def snapshot(pid):
            self.log.append(("snapshot", pid))
            return [WindowSnapshot(FG_HWND, IntRect(10, 20, 600, 400), "App", 77)]

        def fg(pid):
            self.log.append(("foreground", pid))
            return FG_HWND

        def wmeta(hwnd, mons):
            self.log.append(("window_meta", hwnd))
            return self.metas.get(hwnd)

        def read_url(hwnd, browser, timeout):
            self.url_calls.append((hwnd, browser, timeout))
            self.url_thread_names.append(threading.current_thread().name)
            if self.url_delay:
                self.url_release.wait(self.url_delay)
            return self.url_result

        def cd_factory(seconds, monitor, parent):
            cd = FakeCountdown(seconds, monitor, parent, self.log)
            self.countdowns.append(cd)
            return cd

        def sel_factory(image, origin, monitors, windows, parent):
            s = FakeSelector(image, origin, monitors, windows, parent, self.log)
            self.selectors.append(s)
            return s

        self.deps = CaptureDeps(
            enumerate_monitors=enum, grab=grab, snapshot_windows=snapshot, foreground_hwnd=fg,
            window_meta=wmeta, read_url=read_url, cursor_pos=lambda: (1200, 300),
            dwm_flush=lambda: self.log.append("dwm_flush"), countdown_factory=cd_factory,
            selector_factory=sel_factory, own_pid=lambda: OWN_PID, settle_ms=20,
        )
        for k, v in overrides.items():
            setattr(self.deps, k, v)
        self.svc = CaptureService(lambda: self.settings, None, deps=self.deps)
        self.svc.captured.connect(lambda s: self.events.append(("captured", s)))
        self.svc.cancelled.connect(lambda m: self.events.append(("cancelled", m)))
        self.svc.failed.connect(lambda m: self.events.append(("failed", m)))

    def wait_for(self, predicate, timeout=3.0):
        loop = QEventLoop()
        deadline = time.monotonic() + timeout
        t = QTimer()
        t.setInterval(10)

        def check():
            if predicate() or time.monotonic() > deadline:
                loop.quit()

        t.timeout.connect(check)
        t.start()
        check()
        if not (predicate() or time.monotonic() > deadline):
            loop.exec()
        t.stop()
        return predicate()

    def terminal(self):
        return [e for e in self.events]


@pytest.fixture
def rig(qapp):
    r = Rig(qapp)
    yield r
    r.url_release.set()
    r.svc.cancel()


def region(rect=IntRect(20, 30, 50, 40), mon=MON1):
    return Selection("region", rect, mon, None)


# ---- normal capture ------------------------------------------------------------------------------------
def test_normal_capture_opens_the_selector_synchronously_without_any_timer(rig):
    assert rig.svc.start(CaptureMode.NORMAL) is True
    # everything happened inside the start() call: no event loop has run yet
    assert rig.grab_calls == 1
    assert len(rig.selectors) == 1 and rig.selectors[0].started
    assert rig.countdowns == []
    assert rig.svc.is_active and rig.svc.state == "selecting"
    assert rig.svc._settle_timer is None and rig.svc._url_timer is None
    # order: identify the foreground window and snapshot BEFORE freezing and BEFORE the overlay
    names = [e if isinstance(e, str) else e[0] for e in rig.log]
    assert names.index("foreground") < names.index("grab") < names.index("selector.start")
    assert names.index("snapshot") < names.index("grab")
    assert ("foreground", OWN_PID) in rig.log and ("snapshot", OWN_PID) in rig.log  # own windows excluded


def test_selector_receives_the_frozen_grab_monitors_and_windows(rig):
    rig.svc.start(CaptureMode.NORMAL)
    s = rig.selectors[0]
    assert s.image is rig.image and s.origin == ORIGIN
    assert [m.index for m in s.monitors] == [1, 2]
    assert [w.hwnd for w in s.windows] == [FG_HWND]


def test_region_selection_emits_captured_with_full_metadata(rig):
    rig.svc.start(CaptureMode.NORMAL)
    rig.selectors[0].selected.emit(region(IntRect(1100, 100, 300, 200), MON2))
    assert len(rig.events) == 1 and rig.events[0][0] == "captured"
    shot = rig.events[0][1]
    assert shot.capture_mode == CaptureMode.NORMAL and shot.selection == "region"
    assert shot.capture_rect == IntRect(1100, 100, 300, 200)
    assert (shot.image_width, shot.image_height) == (300, 200)
    assert (shot.image.width(), shot.image.height()) == (300, 200) and shot.image.devicePixelRatio() == 1.0
    assert shot.monitor == MON2 and shot.monitor is not MON2 and shot.monitor.scale_percent == 150
    assert shot.window == rig.fg_meta and shot.window is not rig.fg_meta
    assert shot.role == Role.SHOT and shot.caption == "" and shot.annotations == [] and shot.index == 0
    assert shot.timestamp.count("-") >= 2 and "T" in shot.timestamp
    assert QColor(shot.image.pixel(5, 5)).name() == "#123456"
    assert not rig.svc.is_active and rig.svc.state == "idle"
    assert rig.selectors[0].closed >= 1


def test_pixels_are_cropped_from_the_right_place_with_a_negative_origin(qapp):
    r = Rig(qapp)
    # virtual desktop origin at (-500, -100): pixel value encodes its own local position
    img = QImage(800, 300, QImage.Format.Format_RGB32)
    for y in range(300):
        for x in range(0, 800):
            img.setPixel(x, y, QColor(x % 256, y % 256, (x // 256) * 10).rgb())
    r.deps.grab = lambda: (img, IntRect(-500, -100, 800, 300))
    r.svc.start(CaptureMode.NORMAL)
    r.selectors[0].selected.emit(Selection("region", IntRect(-450, -60, 40, 30), MON1, None))
    shot = r.events[0][1]
    c = QColor(shot.image.pixel(0, 0))
    assert (c.red(), c.green()) == ((-450 + 500) % 256, (-60 + 100) % 256)
    c = QColor(shot.image.pixel(39, 29))
    assert (c.red(), c.green()) == ((-450 + 500 + 39) % 256, (-60 + 100 + 29) % 256)
    assert shot.capture_rect == IntRect(-450, -60, 40, 30)


@pytest.mark.parametrize("kind", ["window", "monitor", "region"])
def test_selection_kind_is_recorded(rig, kind):
    rig.svc.start(CaptureMode.NORMAL)
    win = WindowSnapshot(FG_HWND, IntRect(0, 0, 100, 100), "App", 77) if kind == "window" else None
    rig.selectors[0].selected.emit(Selection(kind, IntRect(0, 0, 100, 100), MON1, win))
    assert rig.events[0][1].selection == kind


def test_window_pick_of_another_window_uses_the_picked_windows_info(rig):
    other = WindowMeta(title="Other", process_name="other.exe", exe_path=r"C:\other.exe", pid=99,
                       rect=IntRect(300, 200, 400, 300), dpi=96)
    rig.metas[0xBEEF] = other
    rig.svc.start(CaptureMode.NORMAL)
    rig.selectors[0].selected.emit(
        Selection("window", IntRect(300, 200, 400, 300), MON1, WindowSnapshot(0xBEEF, IntRect(300, 200, 400, 300), "Other", 99))
    )
    assert rig.events[0][1].window.title == "Other" and rig.events[0][1].window.pid == 99


def test_window_pick_of_the_foreground_window_reuses_its_meta(rig):
    rig.svc.start(CaptureMode.NORMAL)
    rig.selectors[0].selected.emit(
        Selection("window", IntRect(10, 20, 600, 400), MON1, WindowSnapshot(FG_HWND, IntRect(10, 20, 600, 400), "App", 77))
    )
    assert rig.events[0][1].window.title == "App"
    assert [e for e in rig.log if isinstance(e, tuple) and e[0] == "window_meta"] == [("window_meta", FG_HWND)]


def test_no_window_under_the_selection_and_no_foreground_gives_a_shot_without_window_meta(qapp):
    r = Rig(qapp)
    r.deps.foreground_hwnd = lambda pid: None
    r.deps.snapshot_windows = lambda pid: []
    r.svc.start(CaptureMode.NORMAL)
    r.selectors[0].selected.emit(region())
    assert r.events[0][1].window is None


# ---- region/monitor selections describe the window UNDER the selection, not the focused one ----------------
TERM_HWND = 0x7E7


def _rig_with_terminal_over_region(qapp, term_meta=None):
    """Focused window (FG_HWND, 'App') sits far right; a terminal fills the top-left area."""
    r = Rig(qapp)
    r.fg_meta.rect = IntRect(700, 300, 250, 200)
    term = term_meta or WindowMeta(title="Terminal", process_name="WindowsTerminal.exe",
                                   exe_path=r"C:\wt.exe", pid=55, rect=IntRect(0, 0, 600, 500), dpi=96)
    r.metas[TERM_HWND] = term
    r.deps.snapshot_windows = lambda pid: [
        WindowSnapshot(FG_HWND, IntRect(700, 300, 250, 200), "App", 77),
        WindowSnapshot(TERM_HWND, IntRect(0, 0, 600, 500), "Terminal", 55),
    ]
    return r


def test_region_selection_describes_the_window_under_it_not_the_focused_window(qapp):
    r = _rig_with_terminal_over_region(qapp)
    r.svc.start(CaptureMode.NORMAL)
    r.selectors[0].selected.emit(region(IntRect(50, 50, 300, 200), MON1))
    shot = r.events[0][1]
    assert shot.window.title == "Terminal" and shot.window.process_name == "WindowsTerminal.exe"
    assert shot.window is not r.metas[TERM_HWND]


def test_monitor_selection_describes_the_window_filling_most_of_it(qapp):
    r = _rig_with_terminal_over_region(qapp)
    r.svc.start(CaptureMode.NORMAL)
    r.selectors[0].selected.emit(Selection("monitor", MON1.rect, MON1, None))
    assert r.events[0][1].window.title == "Terminal"


def test_region_over_the_focused_window_still_uses_it_without_a_second_lookup(qapp):
    r = _rig_with_terminal_over_region(qapp)
    r.svc.start(CaptureMode.NORMAL)
    r.selectors[0].selected.emit(region(IntRect(710, 310, 100, 100), MON1))
    assert r.events[0][1].window.title == "App"
    assert [e for e in r.log if isinstance(e, tuple) and e[0] == "window_meta"] == [("window_meta", FG_HWND)]


def test_region_not_over_any_known_window_falls_back_to_the_focused_window(qapp):
    r = _rig_with_terminal_over_region(qapp)
    r.svc.start(CaptureMode.NORMAL)
    r.selectors[0].selected.emit(region(IntRect(1100, 600, 100, 100), MON2))
    assert r.events[0][1].window.title == "App"


def test_browser_under_the_selection_gets_its_own_url_read(qapp):
    browser = WindowMeta(title="Docs - Chrome", process_name="chrome.exe", exe_path=r"C:\chrome.exe", pid=55,
                         rect=IntRect(0, 0, 600, 500), dpi=96, browser="chrome")
    r = _rig_with_terminal_over_region(qapp, term_meta=browser)
    r.svc.start(CaptureMode.NORMAL)
    r.selectors[0].selected.emit(region(IntRect(50, 50, 300, 200), MON1))
    assert r.wait_for(lambda: r.events)
    shot = r.events[0][1]
    assert shot.window.title == "Docs - Chrome" and shot.window.url == "https://example.com/page"
    assert r.url_calls and r.url_calls[-1][:2] == (TERM_HWND, "chrome")


# ---- cancel paths ------------------------------------------------------------------------------------------
def test_escape_in_the_selector_cancels_and_a_new_capture_works(rig):
    rig.svc.start(CaptureMode.NORMAL)
    rig.selectors[0].cancelled.emit()
    assert rig.events == [("cancelled", "normal")]
    assert not rig.svc.is_active
    assert rig.selectors[0].closed >= 1
    # the session is not lost: the very next start works and completes
    assert rig.svc.start(CaptureMode.NORMAL) is True
    rig.selectors[1].selected.emit(region())
    assert [e[0] for e in rig.events] == ["cancelled", "captured"]


def test_start_while_active_returns_false_and_does_nothing(rig):
    assert rig.svc.start(CaptureMode.NORMAL) is True
    n_grab, n_sel = rig.grab_calls, len(rig.selectors)
    assert rig.svc.start(CaptureMode.NORMAL) is False
    assert rig.svc.start(CaptureMode.DELAYED) is False
    assert rig.grab_calls == n_grab and len(rig.selectors) == n_sel and rig.countdowns == []
    assert rig.events == []
    rig.selectors[0].selected.emit(region())
    assert [e[0] for e in rig.events] == ["captured"]  # still exactly one terminal signal


def test_start_during_countdown_returns_false(rig):
    assert rig.svc.start(CaptureMode.DELAYED) is True
    assert rig.svc.start(CaptureMode.DELAYED) is False
    assert rig.svc.start(CaptureMode.NORMAL) is False
    assert len(rig.countdowns) == 1 and rig.grab_calls == 0


def test_programmatic_cancel(rig):
    rig.svc.cancel()  # idle: no-op
    assert rig.events == []
    rig.svc.start(CaptureMode.NORMAL)
    rig.svc.cancel()
    assert rig.events == [("cancelled", "normal")] and not rig.svc.is_active
    rig.events.clear()
    rig.svc.start(CaptureMode.DELAYED)
    rig.svc.cancel()
    assert rig.events == [("cancelled", "delayed")] and rig.countdowns[0].closed >= 1


def test_a_slot_can_start_again_from_inside_the_cancelled_signal(rig):
    again = []
    rig.svc.cancelled.connect(lambda m: again.append(rig.svc.start(CaptureMode.NORMAL)))
    rig.svc.start(CaptureMode.NORMAL)
    rig.selectors[0].cancelled.emit()
    assert again == [True] and rig.svc.is_active and len(rig.selectors) == 2


def test_stale_selector_signals_after_cancel_are_ignored(rig):
    rig.svc.start(CaptureMode.NORMAL)
    old = rig.selectors[0]
    rig.svc.cancel()
    old.selected.emit(region())
    old.cancelled.emit()
    assert rig.events == [("cancelled", "normal")]


def test_grab_failure_emits_failed_and_goes_idle(rig):
    rig.grab_error = RuntimeError("BitBlt failed")
    assert rig.svc.start(CaptureMode.NORMAL) is True
    assert len(rig.events) == 1 and rig.events[0][0] == "failed" and "BitBlt failed" in rig.events[0][1]
    assert not rig.svc.is_active and rig.selectors == []
    rig.grab_error = None
    assert rig.svc.start(CaptureMode.NORMAL) is True


def test_crop_failure_emits_failed(rig):
    rig.svc.start(CaptureMode.NORMAL)
    rig.selectors[0].selected.emit(Selection("region", IntRect(9000, 9000, 10, 10), MON1, None))
    assert [e[0] for e in rig.events] == ["failed"] and not rig.svc.is_active


# ---- delayed capture ----------------------------------------------------------------------------------------
def test_delayed_capture_sequence_countdown_hide_flush_settle_grab_selector(rig):
    rig.settings.delay_seconds = 5
    assert rig.svc.start(CaptureMode.DELAYED) is True
    assert rig.svc.is_active and rig.svc.state == "countdown"
    cd = rig.countdowns[0]
    assert cd.started and cd.seconds == 5 and cd.monitor.index == 2  # monitor under the cursor (1200, 300)
    assert rig.grab_calls == 0 and rig.selectors == []  # nothing is frozen while counting down
    cd.finished.emit()
    # right after `finished`: badge hidden and compositor flushed, but NO grab yet (settle wait)
    assert rig.log.index("countdown.hide") < rig.log.index("dwm_flush")
    assert rig.grab_calls == 0 and rig.svc.state == "settling"
    assert rig.wait_for(lambda: len(rig.selectors) == 1)
    assert rig.grab_calls == 1 and rig.selectors[0].started
    names = [e if isinstance(e, str) else e[0] for e in rig.log]
    order = ["countdown.start", "countdown.hide", "dwm_flush", "foreground", "grab", "countdown.close", "selector.start"]
    idx = [names.index(n) for n in order]
    assert idx == sorted(idx), names
    rig.selectors[0].selected.emit(region())
    assert rig.events[0][0] == "captured" and rig.events[0][1].capture_mode == CaptureMode.DELAYED


def test_delayed_settle_is_short(qapp):
    assert CaptureDeps().settle_ms <= 100


def test_foreground_is_identified_after_the_countdown_not_before(rig):
    rig.svc.start(CaptureMode.DELAYED)
    assert not any(isinstance(e, tuple) and e[0] == "foreground" for e in rig.log)
    rig.countdowns[0].finished.emit()
    rig.wait_for(lambda: len(rig.selectors) == 1)
    assert any(isinstance(e, tuple) and e[0] == "foreground" for e in rig.log)


@pytest.mark.parametrize("configured,expected", [(3, 3), (0, 1), (-4, 1), (100, 30), (12, 12)])
def test_delay_seconds_come_from_settings_and_are_clamped(rig, configured, expected):
    rig.settings.delay_seconds = configured
    rig.svc.start(CaptureMode.DELAYED)
    assert rig.countdowns[0].seconds == expected


def test_settings_are_read_on_every_start(rig):
    rig.settings.delay_seconds = 2
    rig.svc.start(CaptureMode.DELAYED)
    rig.countdowns[0].cancelled.emit()
    rig.settings.delay_seconds = 7
    rig.svc.start(CaptureMode.DELAYED)
    assert [c.seconds for c in rig.countdowns] == [2, 7]


def test_escape_during_the_countdown_cancels_without_grabbing(rig):
    rig.svc.start(CaptureMode.DELAYED)
    rig.countdowns[0].cancelled.emit()
    assert rig.events == [("cancelled", "delayed")]
    assert not rig.svc.is_active and rig.grab_calls == 0 and rig.selectors == []
    assert rig.countdowns[0].closed >= 1  # the Esc grab is released
    # and the session continues: a new capture (either kind) starts right away
    assert rig.svc.start(CaptureMode.NORMAL) is True and rig.selectors[0].started


def test_escape_between_hide_and_grab_still_cancels(rig):
    rig.svc.start(CaptureMode.DELAYED)
    cd = rig.countdowns[0]
    cd.finished.emit()
    cd.cancelled.emit()  # Esc during the settle wait
    rig.wait_for(lambda: False, timeout=0.15)  # let the (cancelled) settle timer's time pass
    assert rig.events == [("cancelled", "delayed")]
    assert rig.grab_calls == 0 and rig.selectors == []


def test_stale_countdown_signals_are_ignored(rig):
    rig.svc.start(CaptureMode.DELAYED)
    old = rig.countdowns[0]
    old.cancelled.emit()
    rig.svc.start(CaptureMode.DELAYED)
    old.finished.emit()  # from the previous, cancelled countdown
    rig.wait_for(lambda: False, timeout=0.1)
    assert rig.grab_calls == 0 and rig.svc.state == "countdown"
    assert [e[0] for e in rig.events] == ["cancelled"]


def test_escape_in_the_selector_after_a_delayed_capture(rig):
    rig.svc.start(CaptureMode.DELAYED)
    rig.countdowns[0].finished.emit()
    rig.wait_for(lambda: len(rig.selectors) == 1)
    rig.selectors[0].cancelled.emit()
    assert rig.events == [("cancelled", "delayed")] and not rig.svc.is_active


def test_exactly_one_terminal_signal_per_start(rig):
    for step in range(6):
        rig.events.clear()
        mode = CaptureMode.NORMAL if step % 2 == 0 else CaptureMode.DELAYED
        assert rig.svc.start(mode)
        if mode == CaptureMode.DELAYED:
            rig.countdowns[-1].finished.emit()
            rig.wait_for(lambda: rig.svc.state == "selecting")
        sel = rig.selectors[-1]
        if step % 3 == 0:
            sel.cancelled.emit()
        else:
            sel.selected.emit(region())
        sel.cancelled.emit()
        sel.selected.emit(region())
        assert len(rig.events) == 1, (step, rig.events)
        assert not rig.svc.is_active


# ---- browser URL -----------------------------------------------------------------------------------------------------
def make_browser(rig):
    rig.fg_meta.process_name, rig.fg_meta.browser = "chrome.exe", "chrome"


def test_no_url_read_for_non_browsers(rig):
    rig.svc.start(CaptureMode.NORMAL)
    rig.selectors[0].selected.emit(region())
    assert rig.url_calls == [] and rig.events[0][1].window.url is None


def test_url_is_read_on_a_worker_thread_and_attached(rig):
    make_browser(rig)
    rig.svc.start(CaptureMode.NORMAL)
    # the read starts right away (while the user is still selecting), off the GUI thread
    assert rig.wait_for(lambda: len(rig.url_calls) == 1, timeout=1.0)
    assert rig.url_calls[0][:2] == (FG_HWND, "chrome") and rig.url_calls[0][2] <= 1.5
    assert rig.url_thread_names[0] != threading.current_thread().name
    rig.selectors[0].selected.emit(region())
    rig.wait_for(lambda: bool(rig.events))
    assert rig.events[0][1].window.url == "https://example.com/page"
    assert rig.events[0][1].window.browser == "chrome"


def test_slow_url_read_never_blocks_the_gui_thread(rig):
    make_browser(rig)
    rig.url_delay = 5.0
    rig.svc.start(CaptureMode.NORMAL)
    rig.wait_for(lambda: len(rig.url_calls) == 1, timeout=1.0)
    ticks = []
    t = QTimer()
    t.setInterval(20)
    t.timeout.connect(lambda: ticks.append(time.monotonic()))
    t.start()
    t0 = time.monotonic()
    rig.selectors[0].selected.emit(region())  # returns immediately, capture is pending on the URL
    assert time.monotonic() - t0 < 0.3
    assert rig.events == [] and rig.svc.is_active and rig.svc.state == "resolving"
    QTimer.singleShot(250, rig.url_release.set)  # the worker finishes a quarter second later
    assert rig.wait_for(lambda: bool(rig.events), timeout=2.0)
    t.stop()
    assert len(ticks) >= 5  # the event loop kept running meanwhile (a blocked GUI thread would tick 0-1 times)
    assert rig.events[0][1].window.url == "https://example.com/page"


def test_url_wait_gives_up_after_the_limit(rig, monkeypatch):
    make_browser(rig)
    monkeypatch.setattr(service_mod, "URL_WORKER_TIMEOUT_S", 0.2)
    monkeypatch.setattr(service_mod, "URL_WAIT_MAX_S", 0.4)
    rig.url_delay = 10.0  # the reader ignores its own timeout
    rig.svc.start(CaptureMode.NORMAL)
    rig.wait_for(lambda: len(rig.url_calls) == 1, timeout=1.0)
    t0 = time.monotonic()
    rig.selectors[0].selected.emit(region())
    assert rig.wait_for(lambda: bool(rig.events), timeout=2.0)
    assert time.monotonic() - t0 < 1.0
    assert rig.events[0][0] == "captured" and rig.events[0][1].window.url is None


def test_url_read_failure_is_silent(rig):
    make_browser(rig)

    def boom(hwnd, browser, timeout):
        raise RuntimeError("UIA exploded")

    rig.deps.read_url = boom
    rig.svc.start(CaptureMode.NORMAL)
    rig.selectors[0].selected.emit(region())
    rig.wait_for(lambda: bool(rig.events))
    assert rig.events[0][0] == "captured" and rig.events[0][1].window.url is None


def test_url_result_of_none_leaves_url_unset(rig):
    make_browser(rig)
    rig.url_result = None
    rig.svc.start(CaptureMode.NORMAL)
    rig.selectors[0].selected.emit(region())
    rig.wait_for(lambda: bool(rig.events))
    assert rig.events[0][1].window.url is None


def test_picking_another_browser_window_reads_that_windows_url(rig):
    other = WindowMeta(title="B", process_name="msedge.exe", exe_path=r"C:\edge.exe", pid=99,
                       rect=IntRect(300, 200, 400, 300), dpi=96, browser="edge")
    rig.metas[0xBEEF] = other
    rig.svc.start(CaptureMode.NORMAL)
    rig.selectors[0].selected.emit(
        Selection("window", IntRect(300, 200, 400, 300), MON1, WindowSnapshot(0xBEEF, IntRect(300, 200, 400, 300), "B", 99))
    )
    rig.wait_for(lambda: bool(rig.events))
    assert rig.url_calls[-1][:2] == (0xBEEF, "edge")
    assert rig.events[0][1].window.browser == "edge" and rig.events[0][1].window.url == "https://example.com/page"


def test_cancel_while_waiting_for_the_url(rig):
    make_browser(rig)
    rig.url_delay = 5.0
    rig.svc.start(CaptureMode.NORMAL)
    rig.wait_for(lambda: len(rig.url_calls) == 1, timeout=1.0)
    rig.selectors[0].selected.emit(region())
    assert rig.svc.state == "resolving"
    rig.svc.cancel()
    rig.url_release.set()
    rig.wait_for(lambda: False, timeout=0.15)
    assert rig.events == [("cancelled", "normal")]


# ---- real wiring (real grab / monitors / windows / selector / countdown class), offscreen overlays ----------------------------
class _NoEsc(QObject):
    pressed = Signal()

    def acquire(self):
        return True

    def release(self):
        pass


def _drive_drag(sel, x0, y0, x1, y1):
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    ov = sel.overlays[0]

    def send(kind, x, y, button, buttons):
        QApplication.sendEvent(ov, QMouseEvent(kind, QPointF(x, y), QPointF(x, y), button, buttons, Qt.KeyboardModifier.NoModifier))

    L = Qt.MouseButton.LeftButton
    send(QEvent.Type.MouseButtonPress, x0, y0, L, L)
    send(QEvent.Type.MouseMove, x1, y1, Qt.MouseButton.NoButton, L)
    send(QEvent.Type.MouseButtonRelease, x1, y1, L, Qt.MouseButton.NoButton)
    return ov


def test_real_wiring_normal_capture_end_to_end(qapp):
    import os

    svc = CaptureService(lambda: Settings())
    got = []
    svc.captured.connect(lambda s: got.append(("captured", s)))
    svc.cancelled.connect(lambda m: got.append(("cancelled", m)))
    svc.failed.connect(lambda m: got.append(("failed", m)))
    t0 = time.perf_counter()
    assert svc.start(CaptureMode.NORMAL) is True
    opened_in = time.perf_counter() - t0
    try:
        assert svc.is_active and svc._selector is not None and len(svc._selector.overlays) >= 1
        assert opened_in < 1.5  # synchronous: snapshot + one real grab + overlays
        ov = svc._selector.overlays[0]
        w, h = ov.width(), ov.height()
        _drive_drag(svc._selector, w * 0.25, h * 0.25, w * 0.5, h * 0.75)
        qapp.processEvents()
        assert [g[0] for g in got] == ["captured"], got
        shot = got[0][1]
        assert shot.selection == "region" and shot.capture_mode == CaptureMode.NORMAL
        assert (shot.image.width(), shot.image.height()) == (shot.capture_rect.w, shot.capture_rect.h) == (shot.image_width, shot.image_height)
        assert shot.image.devicePixelRatio() == 1.0 and shot.capture_rect.w > 0
        assert shot.monitor is not None and shot.monitor.rect.contains_rect(shot.capture_rect)
        if shot.window is not None:
            assert shot.window.pid != os.getpid() and shot.window.rect.w > 0
        assert not svc.is_active
    finally:
        svc.cancel()


def test_real_wiring_delayed_capture_counts_down_then_freezes_then_esc_cancels(qapp):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    from uireport.capture.countdown import CountdownOverlay

    deps = CaptureDeps(
        countdown_factory=lambda s, m, p: CountdownOverlay(s, m, p, esc_factory=lambda: _NoEsc(), use_native=False, place=False),
    )
    svc = CaptureService(lambda: Settings(delay_seconds=1), None, deps=deps)
    got = []
    svc.captured.connect(lambda s: got.append(("captured", s)))
    svc.cancelled.connect(lambda m: got.append(("cancelled", m)))
    svc.failed.connect(lambda m: got.append(("failed", m)))
    rig = Rig(qapp)  # only for its event-loop helper
    t0 = time.monotonic()
    assert svc.start(CaptureMode.DELAYED)
    try:
        assert svc.state == "countdown" and svc._selector is None  # nothing frozen while counting down
        assert rig.wait_for(lambda: svc.state == "selecting", timeout=4.0)
        elapsed = time.monotonic() - t0
        assert 0.95 <= elapsed <= 2.5, elapsed  # 1 s countdown + short settle
        assert svc._countdown is None  # closed right after the grab
        sel = svc._selector
        QApplication.sendEvent(sel.overlays[0], QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
        assert got == [("cancelled", "delayed")]
        assert not svc.is_active
    finally:
        svc.cancel()
