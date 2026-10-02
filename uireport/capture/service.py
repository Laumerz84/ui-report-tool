"""CaptureService: orchestrates one capture (normal or delayed). Owner: capture builder (A)."""
from __future__ import annotations

import dataclasses
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QImage

from ..geometry import IntRect
from ..models import CaptureMode, MonitorMeta, Shot, WindowMeta, monitor_at_point, now_iso
from ..settings import MAX_DELAY_SECONDS, MIN_DELAY_SECONDS, Settings
from . import grab as _grab
from . import monitors as _monitors
from . import windowinfo as _wi
from . import winapi as _winapi
from .countdown import CountdownOverlay
from .selector import RegionSelector, Selection

URL_WORKER_TIMEOUT_S = 1.5  # hard limit inside the worker (windowinfo.read_browser_url)
URL_WAIT_MAX_S = 2.0  # the service never waits longer than this after the selection
URL_POLL_MS = 25
SETTLE_MS = 80  # after dwm_flush: lets the compositor finish removing the countdown badge


@dataclass
class CaptureDeps:
    """Everything CaptureService touches outside itself; tests replace the fields with fakes."""

    enumerate_monitors: Callable[[], list[MonitorMeta]] = _monitors.enumerate_monitors
    grab: Callable[[], tuple[QImage, IntRect]] = _grab.grab_virtual_screen
    crop: Callable[[QImage, IntRect, IntRect], QImage] = _grab.crop_image
    snapshot_windows: Callable[[Optional[int]], list[_wi.WindowSnapshot]] = _wi.snapshot_top_level_windows
    foreground_hwnd: Callable[[Optional[int]], Optional[int]] = _wi.foreground_hwnd
    window_meta: Callable[[int, list[MonitorMeta]], Optional[WindowMeta]] = _wi.window_meta_from_hwnd
    read_url: Callable[[int, str, float], Optional[str]] = _wi.read_browser_url
    cursor_pos: Callable[[], tuple[int, int]] = _winapi.cursor_pos
    dwm_flush: Callable[[], None] = _winapi.dwm_flush
    countdown_factory: Callable[..., Any] = CountdownOverlay
    selector_factory: Callable[..., Any] = RegionSelector
    own_pid: Callable[[], int] = _winapi.own_pid
    clock: Callable[[], float] = time.monotonic
    settle_ms: int = SETTLE_MS


class _UrlJob:
    """Reads a browser URL on a daemon worker thread; the GUI thread only polls `done`."""

    def __init__(self, hwnd: int, browser: str, reader: Callable[[int, str, float], Optional[str]], clock: Callable[[], float]) -> None:
        self.hwnd = hwnd
        self.browser = browser
        self.started = clock()
        self.url: Optional[str] = None
        self.done = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(reader,), name="uireport-url-job", daemon=True)
        self._thread.start()

    def _run(self, reader: Callable[[int, str, float], Optional[str]]) -> None:
        try:
            self.url = reader(self.hwnd, self.browser, URL_WORKER_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - silent by design
            self.url = None
        finally:
            self.done.set()


class CaptureService(QObject):
    """One capture at a time. Algorithm (see CONTRACT.md section 6):

    NORMAL : start() synchronously does snapshot + grab + shows the selector (no timer, no
             countdown, no artificial delay).
    DELAYED: start() shows a CountdownOverlay for get_settings().delay_seconds, then hides
             it, dwm_flush() + a short settle wait (<= ~100 ms), and only THEN performs the
             same snapshot + grab + selector. The countdown therefore is never in the PNG.
    Both: the foreground window is identified and the windows are snapshotted BEFORE the
          selector overlay is shown (it would steal focus); the browser URL (if the
          foreground window is Chrome/Edge/Firefox) is read on a worker thread while the
          user selects and joined (<= 2 s) after the selection.
          The shot's window is the one CAPTURED: the picked window, or the window showing
          most of the region/monitor (windowinfo.dominant_window). The foreground window is
          only the fallback when no snapshotted window covers the selection.

    Signals (exactly one of the three per start() that returned True):
        captured(Shot)   Shot has .image (original QImage, dpr 1.0), image_width/height,
                         capture_rect, monitor, window, capture_mode, selection, timestamp;
                         role=PROBLEM, empty caption, no annotations, index 0.
        cancelled(str)   CaptureMode value ('normal'|'delayed'); Esc in the countdown or in
                         the selector. Nothing is lost - the session is untouched.
        failed(str)      unexpected error message (grab failed, ...)

    `deps` (keyword-only) is the test seam: a CaptureDeps whose members replace the Win32,
    countdown and selector implementations.
    """

    captured = Signal(object)
    cancelled = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        get_settings: Callable[[], Settings],
        parent: Optional[QObject] = None,
        *,
        deps: Optional[CaptureDeps] = None,
    ) -> None:
        super().__init__(parent)
        self._get_settings = get_settings
        self._d = deps or CaptureDeps()
        self._active = False
        self._mode = CaptureMode.NORMAL
        self._gen = 0  # bumped by every start() and every terminal transition: stale callbacks self-cancel
        self._state = "idle"  # idle | countdown | settling | selecting | resolving
        self._countdown: Any = None
        self._selector: Any = None
        self._settle_timer: Optional[QTimer] = None
        self._url_timer: Optional[QTimer] = None
        # data of the capture in flight
        self._frozen: Optional[QImage] = None
        self._frozen_origin = IntRect()
        self._monitors: list[MonitorMeta] = []
        self._windows: list[_wi.WindowSnapshot] = []
        self._fg_hwnd: Optional[int] = None
        self._fg_meta: Optional[WindowMeta] = None
        self._fg_job: Optional[_UrlJob] = None
        self._timestamp = ""

    # ---- public API -------------------------------------------------------------------------
    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def state(self) -> str:
        return self._state

    def start(self, mode: CaptureMode) -> bool:
        """Begin a capture. Returns False (and does nothing) if one is already active."""
        if self._active:
            return False
        self._active = True
        self._mode = CaptureMode(mode)
        self._gen += 1
        try:
            if self._mode == CaptureMode.DELAYED:
                self._begin_countdown()
            else:
                self._freeze_and_select()
        except Exception as exc:  # noqa: BLE001
            self._fail(f"Capture failed: {exc}")
        return True

    def cancel(self) -> None:
        """Programmatic cancel of an active capture (emits cancelled). No-op when idle."""
        if not self._active:
            return
        self._finish_cancelled()

    # ---- delayed: countdown -> hide -> flush -> settle ---------------------------------------------
    def _begin_countdown(self) -> None:
        settings = self._get_settings()
        seconds = int(max(MIN_DELAY_SECONDS, min(MAX_DELAY_SECONDS, int(settings.delay_seconds))))
        mons = self._d.enumerate_monitors()
        x, y = self._d.cursor_pos()
        mon = monitor_at_point(mons, x, y)
        gen = self._gen
        cd = self._d.countdown_factory(seconds, mon, self)
        cd.finished.connect(lambda: self._on_countdown_finished(gen))
        cd.cancelled.connect(lambda: self._on_countdown_cancelled(gen))
        self._countdown = cd
        self._state = "countdown"
        cd.start()

    def _on_countdown_finished(self, gen: int) -> None:
        if gen != self._gen or self._state != "countdown":
            return
        self._state = "settling"
        cd = self._countdown
        try:
            if cd is not None:
                cd.hide()  # the badge must be gone before the grab (and it is excluded from capture anyway)
            self._d.dwm_flush()
        except Exception as exc:  # noqa: BLE001
            self._fail(f"Capture failed: {exc}")
            return
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._after_settle(gen))
        self._settle_timer = timer
        timer.start(max(0, int(self._d.settle_ms)))

    def _after_settle(self, gen: int) -> None:
        if gen != self._gen or self._state != "settling":
            return
        try:
            self._freeze_and_select()
        except Exception as exc:  # noqa: BLE001
            self._fail(f"Capture failed: {exc}")

    def _on_countdown_cancelled(self, gen: int) -> None:
        if gen != self._gen or self._state not in ("countdown", "settling"):
            return
        self._finish_cancelled()

    # ---- freeze: foreground -> snapshot -> grab -> selector ----------------------------------------------
    def _freeze_and_select(self) -> None:
        d = self._d
        own = d.own_pid()
        monitors = d.enumerate_monitors()
        # 1) who is in front, and which windows exist - BEFORE any overlay of ours appears
        fg_hwnd = d.foreground_hwnd(own)
        windows = d.snapshot_windows(own)
        # 2) freeze
        self._timestamp = now_iso()
        image, origin = d.grab()
        # 3) the delayed-capture badge is not needed any more (its Esc grab ends here)
        if self._countdown is not None:
            self._countdown.close()
            self._countdown = None
        # 4) metadata of the foreground window + start the URL read while the user selects
        fg_meta = d.window_meta(fg_hwnd, monitors) if fg_hwnd else None
        self._fg_job = None
        if fg_meta is not None and fg_meta.browser and fg_hwnd:
            self._fg_job = _UrlJob(fg_hwnd, fg_meta.browser, d.read_url, d.clock)
        self._frozen, self._frozen_origin, self._monitors = image, origin, monitors
        self._windows = windows
        self._fg_hwnd, self._fg_meta = fg_hwnd, fg_meta
        # 5) open the selector (this is what takes focus)
        gen = self._gen
        sel = d.selector_factory(image, origin, monitors, windows, self)
        sel.selected.connect(lambda s: self._on_selected(gen, s))
        sel.cancelled.connect(lambda: self._on_selector_cancelled(gen))
        self._selector = sel
        self._state = "selecting"
        sel.start()

    def _on_selector_cancelled(self, gen: int) -> None:
        if gen != self._gen or self._state != "selecting":
            return
        self._finish_cancelled()

    def _on_selected(self, gen: int, selection: Selection) -> None:
        if gen != self._gen or self._state != "selecting":
            return
        self._state = "resolving"
        try:
            self._resolve(gen, selection)
        except Exception as exc:  # noqa: BLE001
            self._fail(f"Capture failed: {exc}")

    # ---- resolve: window meta + URL, then emit ----------------------------------------------------------------
    def _resolve(self, gen: int, sel: Selection) -> None:
        d = self._d
        assert self._frozen is not None
        meta: Optional[WindowMeta] = self._fg_meta
        job = self._fg_job
        # describe the window that was CAPTURED: the picked one in window mode, otherwise the one
        # showing most of the selection. The focused window is only the fallback.
        if sel.kind == "window" and sel.window is not None:
            target: Optional[_wi.WindowSnapshot] = sel.window
        else:
            target = _wi.dominant_window(self._windows, sel.rect)
        if target is not None and target.hwnd != self._fg_hwnd:
            target_meta = d.window_meta(target.hwnd, self._monitors)
            if target_meta is not None:
                meta, job = target_meta, None
                if meta.browser:
                    job = _UrlJob(target.hwnd, meta.browser, d.read_url, d.clock)
        image = d.crop(self._frozen, self._frozen_origin, sel.rect)
        shot = Shot(
            capture_mode=self._mode,
            selection=sel.kind,
            timestamp=self._timestamp or now_iso(),
            capture_rect=sel.rect,
            monitor=dataclasses.replace(sel.monitor),
            window=dataclasses.replace(meta) if meta is not None else None,
        )
        shot.set_image(image)
        # the frozen full-desktop grab is no longer needed
        self._frozen = None
        self._release_selector()
        if job is None or job.done.is_set():
            self._emit_captured(gen, shot, job)
            return
        # never block the GUI thread: poll the worker, give up after URL_WAIT_MAX_S
        deadline = min(job.started + URL_WORKER_TIMEOUT_S + 0.25, d.clock() + URL_WAIT_MAX_S)
        timer = QTimer(self)
        timer.setInterval(URL_POLL_MS)

        def _poll() -> None:
            if gen != self._gen:
                return
            if job.done.is_set() or d.clock() >= deadline:
                self._emit_captured(gen, shot, job if job.done.is_set() else None)

        timer.timeout.connect(_poll)
        self._url_timer = timer
        timer.start()

    def _emit_captured(self, gen: int, shot: Shot, job: Optional[_UrlJob]) -> None:
        if gen != self._gen or not self._active:
            return
        if job is not None and shot.window is not None and job.url:
            shot.window.url = job.url
        self._reset()
        self.captured.emit(shot)

    # ---- terminal transitions ------------------------------------------------------------------------------------
    def _finish_cancelled(self) -> None:
        mode = self._mode
        self._reset()
        self.cancelled.emit(mode.value)

    def _fail(self, message: str) -> None:
        self._reset()
        self.failed.emit(message)

    def _release_selector(self) -> None:
        sel, self._selector = self._selector, None
        if sel is not None:
            try:
                sel.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                sel.deleteLater()
            except Exception:  # noqa: BLE001
                pass

    def _reset(self) -> None:
        """Back to idle (before any terminal signal is emitted, so a slot may start() again)."""
        self._gen += 1  # invalidates every pending callback of this capture
        for attr in ("_settle_timer", "_url_timer"):
            t = getattr(self, attr)
            if t is not None:
                t.stop()
                t.deleteLater()
                setattr(self, attr, None)
        cd, self._countdown = self._countdown, None
        if cd is not None:
            try:
                cd.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                cd.deleteLater()
            except Exception:  # noqa: BLE001
                pass
        self._release_selector()
        self._frozen = None
        self._fg_job = None
        self._fg_hwnd = None
        self._fg_meta = None
        self._monitors = []
        self._windows = []
        self._state = "idle"
        self._active = False
