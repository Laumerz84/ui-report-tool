"""On-screen countdown for delayed capture. Owner: capture builder (package A)."""
from __future__ import annotations

import math
import time
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ..models import MonitorMeta
from . import winapi
from .hotkeys import TemporaryHotkey
from .monitors import find_qscreen

BADGE_W = 132
BADGE_H = 104
MARGIN_RIGHT = 28
MARGIN_BOTTOM = 96  # keeps clear of the taskbar
TICK_MS = 50
ESC_POLL_MS = 25


class _Badge(QWidget):
    """The visible countdown badge: never activates, never takes focus or mouse input."""

    def __init__(self) -> None:
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.NoDropShadowWindowHint
        )
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setWindowTitle("UI Report countdown")
        self.resize(BADGE_W, BADGE_H)
        self.number = 0

    def set_number(self, n: int) -> None:
        if n != self.number:
            self.number = n
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(20, 20, 24, 225))
            p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 16, 16)
            p.setPen(QPen(QColor(255, 255, 255)))
            f = QFont(self.font())
            f.setPixelSize(52)
            f.setBold(True)
            p.setFont(f)
            p.drawText(QRect(0, 6, self.width(), 62), Qt.AlignmentFlag.AlignCenter, str(self.number))
            f2 = QFont(self.font())
            f2.setPixelSize(13)
            p.setFont(f2)
            p.setPen(QColor(200, 200, 208))
            p.drawText(QRect(0, 68, self.width(), 26), Qt.AlignmentFlag.AlignCenter, "Esc to cancel")
        finally:
            p.end()


class CountdownOverlay(QObject):
    """A small badge in a screen corner counting down N..1.

    It must NOT disturb what the user is doing (an open menu must stay open):
      * never takes focus / activation (WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW |
        WS_EX_TRANSPARENT, Qt.WindowDoesNotAcceptFocus, Qt.WindowTransparentForInput);
      * is excluded from screen capture (winapi.exclude_from_capture) as belt and braces;
      * Esc cancels it WITHOUT reaching the target app (e.g. register Esc as a temporary
        global hotkey for the duration of the countdown, fall back to polling
        GetAsyncKeyState(VK_ESCAPE); release it on finish/cancel/close).

    Signals (each at most once; after either, the badge is hidden):
        finished()   the count reached zero
        cancelled()  Esc was pressed

    Detail: after `finished` the Esc grab stays armed until close() so the short settle
    time before the grab is still covered (an Esc then emits `cancelled` instead of
    leaking into the target app); the service always calls close() right after the grab.

    Keyword-only test seams: `esc_factory` (returns an object with pressed/acquire/release),
    `clock` (monotonic seconds), `use_native` (False = skip the Win32 style/exclusion calls),
    `key_down` (GetAsyncKeyState replacement for the polling fallback), `place` (False = do
    not position the badge).
    """

    finished = Signal()
    cancelled = Signal()

    def __init__(
        self,
        seconds: int,
        monitor: Optional[MonitorMeta] = None,
        parent: Optional[QObject] = None,
        *,
        esc_factory: Optional[Callable[[], Any]] = None,
        clock: Optional[Callable[[], float]] = None,
        use_native: bool = True,
        key_down: Optional[Callable[[int], bool]] = None,
        place: bool = True,
    ) -> None:
        super().__init__(parent)
        self._seconds = max(0, int(seconds))
        self._monitor = monitor
        self._esc_factory = esc_factory or (lambda: TemporaryHotkey(0, winapi.VK_ESCAPE))
        self._clock = clock or time.monotonic
        self._use_native = use_native
        self._key_down = key_down or winapi.is_key_down
        self._place = place
        self._badge: Optional[_Badge] = None
        self._tick: Optional[QTimer] = None
        self._poll: Optional[QTimer] = None
        self._esc: Any = None
        self._deadline = 0.0
        self._started = False
        self._closed = False
        self._finished_emitted = False
        self._cancelled_emitted = False
        self._esc_was_down = False
        self.uses_global_esc = False  # True when the Esc hotkey registration succeeded

    # ---- properties for tests / service ------------------------------------------------------
    @property
    def remaining(self) -> int:
        """Whole seconds left (what the badge shows)."""
        if not self._started:
            return self._seconds
        return max(0, math.ceil(self._deadline - self._clock() - 1e-9))

    @property
    def widget(self) -> Optional[QWidget]:
        return self._badge

    @property
    def is_visible(self) -> bool:
        return bool(self._badge is not None and self._badge.isVisible())

    # ---- lifecycle ---------------------------------------------------------------------------
    def start(self) -> None:
        if self._started or self._closed:
            return
        self._started = True
        self._deadline = self._clock() + self._seconds
        self._badge = _Badge()
        self._badge.set_number(self._seconds)
        if self._place:
            self._position(self._badge)
        self._badge.show()  # WA_ShowWithoutActivating -> SW_SHOWNOACTIVATE
        if self._use_native and QGuiApplication.platformName() == "windows":
            hwnd = winapi.widget_hwnd(self._badge)
            winapi.set_noactivate_clickthrough(hwnd)
            winapi.exclude_from_capture(hwnd, True)
        self._arm_escape()
        self._tick = QTimer(self)
        self._tick.setTimerType(Qt.TimerType.PreciseTimer)
        self._tick.setInterval(TICK_MS)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start()
        if self._seconds == 0:
            self._on_tick()

    def hide(self) -> None:
        """Hide immediately (called right before the grab)."""
        if self._badge is not None:
            self._badge.hide()

    def close(self) -> None:
        """Hide, stop timers, release the Esc grab. Idempotent."""
        self._closed = True
        for t in (self._tick, self._poll):
            if t is not None:
                t.stop()
                t.deleteLater()
        self._tick = None
        self._poll = None
        self._release_escape()
        badge, self._badge = self._badge, None
        if badge is not None:
            try:
                badge.hide()
                badge.close()
                badge.deleteLater()
            except RuntimeError:
                pass

    # ---- internals -----------------------------------------------------------------------------
    def _position(self, badge: _Badge) -> None:
        screen = find_qscreen(self._monitor) if self._monitor is not None else QGuiApplication.primaryScreen()
        if screen is None:
            return
        badge.setScreen(screen)
        g = screen.geometry()  # top-left native, size logical (Qt 6 mixed-DPI convention)
        badge.move(QPoint(g.x() + g.width() - BADGE_W - MARGIN_RIGHT, g.y() + g.height() - BADGE_H - MARGIN_BOTTOM))

    def _arm_escape(self) -> None:
        try:
            self._esc = self._esc_factory()
            self._esc.pressed.connect(self._on_escape, Qt.ConnectionType.QueuedConnection)
            self.uses_global_esc = bool(self._esc.acquire())
        except Exception:  # noqa: BLE001
            self.uses_global_esc = False
        if not self.uses_global_esc:
            # fallback: watch the key state (Esc will still reach the target app)
            self._esc_was_down = bool(self._key_down(winapi.VK_ESCAPE))
            self._poll = QTimer(self)
            self._poll.setInterval(ESC_POLL_MS)
            self._poll.timeout.connect(self._on_poll)
            self._poll.start()

    def _release_escape(self) -> None:
        esc, self._esc = self._esc, None
        if esc is not None:
            try:
                esc.release()
            except Exception:  # noqa: BLE001
                pass
            try:
                esc.deleteLater()
            except Exception:  # noqa: BLE001
                pass

    def _on_tick(self) -> None:
        if self._closed or self._finished_emitted or self._cancelled_emitted or self._badge is None:
            return
        left = max(0, math.ceil(self._deadline - self._clock() - 1e-9))
        if left > 0:
            self._badge.set_number(left)
            return
        self._finished_emitted = True
        if self._tick is not None:
            self._tick.stop()
        self.hide()
        self.finished.emit()

    def _on_poll(self) -> None:
        down = bool(self._key_down(winapi.VK_ESCAPE))
        if down and not self._esc_was_down:
            self._on_escape()
        self._esc_was_down = down

    def _on_escape(self) -> None:
        if self._closed or self._cancelled_emitted:
            return
        self._cancelled_emitted = True
        if self._tick is not None:
            self._tick.stop()
        self.hide()
        self.cancelled.emit()
