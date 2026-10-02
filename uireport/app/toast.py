"""In-app toast notification with an action button (tray balloons cannot have buttons).
Owner: output/app builder (package C)."""
from __future__ import annotations

import logging
from typing import Callable, Optional

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QToolButton, QWidget

log = logging.getLogger("uireport.toast")

DEFAULT_TIMEOUT_MS = 8000
MARGIN = 16  # logical px between the card and the edge of the available area
WIDTH = 380  # logical px

_NORMAL = {"bg": "#111827", "border": "#374151", "text": "#F9FAFB", "accent": "#3E63DD"}
_ERROR = {"bg": "#3B0D11", "border": "#E5484D", "text": "#FFE5E7", "accent": "#E5484D"}


def _has_real_hwnd() -> bool:
    """False under Qt's offscreen platform, where winId() is not a Win32 window handle."""
    return QGuiApplication.platformName() != "offscreen"


class Toast(QWidget):
    """Frameless, always-on-top, non-activating card at the bottom-right of the primary
    monitor's available area (DPI-correct), auto-hides after `timeout_ms`. Excluded from screen
    capture (capture.winapi.exclude_from_capture) so it never appears in the next shot."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setObjectName("uireportToast")

        self._action: Optional[Callable[[], None]] = None
        self._error = False
        self._timeout_ms = DEFAULT_TIMEOUT_MS
        self._excluded = False
        self._hovered = False
        self._palette = _NORMAL

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)

        self.message_label = QLabel(self)
        self.message_label.setWordWrap(True)
        self.message_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.message_label.setMinimumWidth(WIDTH - 130)

        self.action_button = QPushButton(self)
        self.action_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.action_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.action_button.clicked.connect(self._on_action_clicked)
        self.action_button.hide()

        self.close_button = QToolButton(self)
        self.close_button.setText("×")  # multiplication sign used as the close "x"
        self.close_button.setToolTip("Dismiss")
        self.close_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.clicked.connect(self.dismiss)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 12, 10, 12)
        lay.setSpacing(10)
        lay.addWidget(self.message_label, 1)
        lay.addWidget(self.action_button, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)
        self._apply_style()

    # ------------------------------------------------------------------ public API
    @property
    def is_error(self) -> bool:
        return self._error

    def show_toast(
        self,
        message: str,
        *,
        action_text: Optional[str] = None,
        action: Optional[Callable[[], None]] = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        error: bool = False,
    ) -> None:
        """Show (replacing any current toast). Clicking the action button calls `action()`
        and dismisses; a close 'x' dismisses. Finish uses message='Copied — paste into
        Claude', action_text='Open folder'. timeout_ms <= 0 keeps the toast until it is
        dismissed or replaced (used for 'Saving...')."""
        self._timer.stop()
        self._error = bool(error)
        self._palette = _ERROR if error else _NORMAL
        self._action = action if (action_text and action is not None) else None
        self._timeout_ms = int(timeout_ms)
        self.message_label.setText(message)
        if action_text and action is not None:
            self.action_button.setText(action_text)
            self.action_button.show()
        else:
            self.action_button.hide()
        self._apply_style()
        self.layout().activate()
        self.adjustSize()
        self._position()
        if not self.isVisible():
            self.show()
        self._exclude_from_capture_once()
        self.raise_()
        self.update()
        if self._timeout_ms > 0:
            self._timer.start(self._timeout_ms)

    def dismiss(self) -> None:
        self._timer.stop()
        self._action = None
        self._hovered = False
        if self.isVisible():
            self.hide()

    # ------------------------------------------------------------------ internals
    def _on_action_clicked(self) -> None:
        action, self._action = self._action, None
        self.dismiss()
        if action is not None:
            try:
                action()
            except Exception:  # an action failing must never crash the tray app
                log.exception("toast action failed")

    def _apply_style(self) -> None:
        p = self._palette
        self.message_label.setStyleSheet(
            f"color: {p['text']}; font-size: 13px; background: transparent;"
        )
        self.action_button.setStyleSheet(
            f"QPushButton {{ background: {p['accent']}; color: #FFFFFF; border: none; border-radius: 6px;"
            f" padding: 6px 12px; font-size: 12px; font-weight: 600; }}"
            f"QPushButton:hover {{ background: #FFFFFF; color: #111827; }}"
        )
        self.close_button.setStyleSheet(
            f"QToolButton {{ background: transparent; color: {p['text']}; border: none; font-size: 18px;"
            f" padding: 0 4px; }}"
            f"QToolButton:hover {{ color: #FFFFFF; background: rgba(255,255,255,40); border-radius: 4px; }}"
        )

    def _position(self) -> None:
        """Bottom-right corner of the primary screen's available area (excludes the taskbar).
        Qt places top-level widgets in the same logical coordinate space QScreen reports, so
        this is right on scaled and mixed-DPI setups."""
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        x = avail.x() + avail.width() - self.width() - MARGIN
        y = avail.y() + avail.height() - self.height() - MARGIN
        self.move(max(avail.x(), x), max(avail.y(), y))

    def target_geometry(self):
        """Where the toast sits (for tests): the current widget geometry."""
        return self.geometry()

    def _exclude_from_capture_once(self) -> None:
        if self._excluded:
            return
        self._excluded = True
        try:
            if not _has_real_hwnd():
                return  # no real HWND to protect
            from ..capture.winapi import exclude_from_capture

            exclude_from_capture(int(self.winId()), True)
        except Exception as exc:  # stubs / unsupported OS: never break the toast
            log.debug("could not exclude the toast from screen capture: %s", exc)

    # hover keeps the toast alive while the mouse is over it
    def enterEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        self._hovered = True
        self._timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hovered = False
        if self.isVisible() and self._timeout_ms > 0:
            self._timer.start(min(self._timeout_ms, 3000))
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, 10, 10)
        p.fillPath(path, QColor(self._palette["bg"]))
        p.setPen(QPen(QColor(self._palette["border"]), 1.0))
        p.drawPath(path)
        p.end()
