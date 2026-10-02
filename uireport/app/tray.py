"""System tray icon and menu. Owner: output/app builder (package C)."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .icons import make_app_icon

LABEL_CAPTURE = "Capture"
LABEL_DELAYED = "Delayed capture"
LABEL_SETTINGS = "Settings"
LABEL_OPEN_FOLDER = "Open reports folder"
LABEL_QUIT = "Quit"

# Choosing "Capture" in the tray MENU closes a popup window right before the screen is frozen. Give the
# compositor a moment to remove it, or it could end up in the screenshot. (A left click on the icon and the
# hotkey have no popup and stay instant.)
MENU_CLOSE_SETTLE_MS = 90


def _flush_compositor() -> None:
    try:
        from ..capture.winapi import dwm_flush

        dwm_flush()
    except Exception:  # best effort: not Windows, or the API is unavailable
        pass


def show_session_label(n: int) -> str:
    return f"Show session ({int(n)})"


class TrayIcon(QSystemTrayIcon):
    """Menu (exact labels): 'Capture', 'Delayed capture', 'Settings', 'Open reports folder',
    'Quit' - plus one conditional extra 'Show session (N)' (enabled only when N > 0) placed
    right after 'Delayed capture'. Left click on the icon = Capture. Tooltip shows the hotkeys
    and the shot count. Icon drawn in code (app.icons.make_app_icon), no image files.

    Signals: capture_requested, delayed_requested, settings_requested,
             open_folder_requested, show_session_requested, quit_requested
    """

    capture_requested = Signal()
    delayed_requested = Signal()
    settings_requested = Signal()
    open_folder_requested = Signal()
    show_session_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._count = 0
        self._hint_capture = ""
        self._hint_delayed = ""
        self.setIcon(make_app_icon())

        self._menu = QMenu()  # no parent: a tray menu is a top-level popup; we keep it alive
        self._menu.setToolTipsVisible(True)
        self.action_capture = self._add(LABEL_CAPTURE, self.capture_requested, after_menu_closes=True)
        self.action_delayed = self._add(LABEL_DELAYED, self.delayed_requested)
        self.action_show_session = self._add(show_session_label(0), self.show_session_requested)
        self.action_show_session.setEnabled(False)
        self._menu.addSeparator()
        self.action_settings = self._add(LABEL_SETTINGS, self.settings_requested)
        self.action_open_folder = self._add(LABEL_OPEN_FOLDER, self.open_folder_requested)
        self._menu.addSeparator()
        self.action_quit = self._add(LABEL_QUIT, self.quit_requested)
        self.setContextMenu(self._menu)

        self.activated.connect(self._on_activated)
        self._refresh_tooltip()

    # ------------------------------------------------------------------ helpers
    def _add(self, text: str, signal: Signal, after_menu_closes: bool = False) -> QAction:
        action = self._menu.addAction(text)
        if after_menu_closes:
            action.triggered.connect(lambda _checked=False, s=signal: self._emit_after_menu_closed(s))
        else:
            action.triggered.connect(lambda _checked=False, s=signal: s.emit())
        return action

    def _emit_after_menu_closed(self, signal: Signal) -> None:
        _flush_compositor()
        QTimer.singleShot(MENU_CLOSE_SETTLE_MS, lambda s=signal: s.emit())

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # Left click = Capture. (A double click already delivered a Trigger for the first
        # click; the controller ignores requests while a capture is active.)
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.capture_requested.emit()

    def menu_labels(self) -> list[str]:
        """The visible action labels in order (separators omitted)."""
        return [a.text() for a in self._menu.actions() if not a.isSeparator()]

    def _refresh_tooltip(self) -> None:
        lines = ["UI Report Tool"]
        if self._hint_capture:
            lines.append(f"Capture: {self._hint_capture}")
        if self._hint_delayed:
            lines.append(f"Delayed capture: {self._hint_delayed}")
        lines.append("Click the icon to capture")
        lines.append(f"Shots in this session: {self._count}")
        self.setToolTip("\n".join(lines))

    # ------------------------------------------------------------------ API
    def set_session_count(self, n: int) -> None:
        self._count = max(0, int(n))
        self.action_show_session.setText(show_session_label(self._count))
        self.action_show_session.setEnabled(self._count > 0)
        self._refresh_tooltip()

    def set_hotkey_hint(self, capture: str, delayed: str) -> None:
        """Refresh the tooltip / menu hints, e.g. 'Capture  (Ctrl+Alt+S)'."""
        self._hint_capture = capture or ""
        self._hint_delayed = delayed or ""
        # The menu labels stay EXACT (contract); the hotkeys are shown as hover tips.
        self.action_capture.setToolTip(f"Capture  ({capture})" if capture else LABEL_CAPTURE)
        self.action_delayed.setToolTip(f"Delayed capture  ({delayed})" if delayed else LABEL_DELAYED)
        self._refresh_tooltip()
