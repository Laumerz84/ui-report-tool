"""Test doubles with the same signals/methods as the real collaborators of AppController.

Used by the unit tests and by ``--smoke-test`` (when package A/B classes are not usable yet, or
when the smoke test must not show real windows). None of these touch the desktop, the registry,
the network or the real hotkey table.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Signal

from ..models import CaptureMode, MonitorMeta, Session


class FakeCapture(QObject):
    """Same surface as capture.CaptureService: captured/cancelled/failed + start()/is_active."""

    captured = Signal(object)
    cancelled = Signal(str)
    failed = Signal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.active = False
        self.mode: Optional[CaptureMode] = None
        self.started: list[CaptureMode] = []
        self.cancel_calls = 0

    @property
    def is_active(self) -> bool:
        return self.active

    def start(self, mode: CaptureMode) -> bool:
        if self.active:
            return False
        self.active = True
        self.mode = CaptureMode(mode)
        self.started.append(self.mode)
        return True

    def cancel(self) -> None:
        self.cancel_calls += 1
        if self.active:
            self.finish_cancelled()

    # -- helpers that play the part of the user / of package A --------------------------
    def finish_with(self, shot: Any) -> None:
        self.active = False  # the real service is idle again before it emits
        self.captured.emit(shot)

    def finish_cancelled(self) -> None:
        mode = self.mode.value if self.mode else CaptureMode.NORMAL.value
        self.active = False
        self.cancelled.emit(mode)

    def finish_failed(self, message: str = "grab failed") -> None:
        self.active = False
        self.failed.emit(message)


class FakeEditor(QObject):
    """Same surface as editor.EditorWindow (the five signals + the methods C calls)."""

    next_requested = Signal()
    next_delayed_requested = Signal()
    finish_requested = Signal()
    discard_session_requested = Signal()
    session_changed = Signal()

    def __init__(self, session: Session, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.session = session
        self.visible = False
        self.current: Optional[str] = None
        self.calls: list[tuple] = []

    def set_session(self, session: Session) -> None:
        self.calls.append(("set_session", session))
        self.session = session
        self.current = None

    def refresh(self) -> None:
        self.calls.append(("refresh",))

    def show_shot(self, shot_id: str) -> None:
        self.calls.append(("show_shot", shot_id))
        if self.session.get_shot(shot_id) is not None:
            self.current = shot_id

    def current_shot_id(self) -> Optional[str]:
        return self.current

    def present(self, monitor_hint: Optional[MonitorMeta] = None) -> None:
        self.calls.append(("present", monitor_hint))
        self.visible = True

    def hide_for_capture(self) -> None:
        self.calls.append(("hide_for_capture",))
        self.visible = False

    # QWidget-ish bits the controller uses
    def isVisible(self) -> bool:  # noqa: N802
        return self.visible

    def hide(self) -> None:
        self.calls.append(("hide",))
        self.visible = False

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


class FakeHotkeys(QObject):
    """Same surface as capture.HotkeyManager. `fail` maps a hotkey NAME to an error text (that
    name always fails); `fail_combos` maps a combination text ('Ctrl+Alt+S') to an error text
    (only that combination fails, like a combination taken by another program)."""

    activated = Signal(str)
    registration_failed = Signal(str, str)

    def __init__(
        self,
        parent: Optional[QObject] = None,
        fail: Optional[dict[str, str]] = None,
        fail_combos: Optional[dict[str, str]] = None,
    ) -> None:
        super().__init__(parent)
        self.fail: dict[str, str] = dict(fail or {})
        self.fail_combos: dict[str, str] = dict(fail_combos or {})
        self.start_error: Optional[Exception] = None
        self._registered: dict[str, str] = {}
        self.started = False
        self.stopped = False
        self.set_calls: list[dict[str, str]] = []

    def start(self) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.started = True
        self.stopped = False

    def stop(self) -> None:
        self.started = False
        self.stopped = True
        self._registered = {}

    def set_hotkeys(self, mapping: dict[str, str]) -> dict[str, Optional[str]]:
        """Like the real manager: names missing from `mapping` keep their binding, an empty
        text unbinds that name, a failure keeps the previous binding of that name."""
        self.set_calls.append(dict(mapping))
        results: dict[str, Optional[str]] = {}
        for name, text in mapping.items():
            if not (text or "").strip():
                self._registered.pop(name, None)
                results[name] = None
                continue
            reason = self.fail.get(name) or self.fail_combos.get(text)
            if reason:
                results[name] = reason
                self.registration_failed.emit(name, reason)  # previous binding stays
            else:
                results[name] = None
                self._registered[name] = text
        return results

    @property
    def registered(self) -> dict[str, str]:
        return dict(self._registered)

    def press(self, name: str) -> None:
        self.activated.emit(name)


class FakeTray(QObject):
    """Same surface as app.tray.TrayIcon."""

    capture_requested = Signal()
    delayed_requested = Signal()
    settings_requested = Signal()
    open_folder_requested = Signal()
    show_session_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.count = 0
        self.hints: tuple[str, str] = ("", "")
        self.shown = False
        self.messages: list[tuple[str, str]] = []

    def set_session_count(self, n: int) -> None:
        self.count = n

    def set_hotkey_hint(self, capture: str, delayed: str) -> None:
        self.hints = (capture, delayed)

    def show(self) -> None:
        self.shown = True

    def hide(self) -> None:
        self.shown = False

    def showMessage(self, title: str, message: str, *args: Any, **kwargs: Any) -> None:  # noqa: N802
        self.messages.append((title, message))


class FakeToast(QObject):
    """Same surface as app.toast.Toast; records every call."""

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.shown: list[dict[str, Any]] = []
        self.dismissed = 0
        self.visible = False

    def show_toast(
        self,
        message: str,
        *,
        action_text: Optional[str] = None,
        action: Optional[Callable[[], None]] = None,
        timeout_ms: int = 8000,
        error: bool = False,
    ) -> None:
        self.visible = True
        self.shown.append(
            {"message": message, "action_text": action_text, "action": action,
             "timeout_ms": timeout_ms, "error": error}
        )

    def dismiss(self) -> None:
        self.dismissed += 1
        self.visible = False

    @property
    def last(self) -> Optional[dict[str, Any]]:
        return self.shown[-1] if self.shown else None

    def messages(self) -> list[str]:
        return [s["message"] for s in self.shown]

    def click_action(self) -> None:
        last = self.last
        assert last and last["action"], "no toast action to click"
        last["action"]()


class FakeSettingsWindow(QObject):
    """Same surface as app.settings_window.SettingsWindow."""

    applied = Signal(object)
    hotkey_recording = Signal(bool)
    finished = Signal(int)

    def __init__(self, store: Any, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.store = store
        self.errors: list[str] = []
        self.visible = False

    def show_error(self, message: str) -> None:
        self.errors.append(message)

    def show(self) -> None:
        self.visible = True

    def raise_(self) -> None:
        pass

    def activateWindow(self) -> None:  # noqa: N802
        pass

    def isVisible(self) -> bool:  # noqa: N802
        return self.visible

    def close(self) -> bool:
        self.visible = False
        self.finished.emit(0)
        return True


class MemoryAutostart:
    """Stand-in for the app.autostart module: never touches the registry."""

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled
        self.calls: list[bool] = []
        self.fail_with: Optional[Exception] = None

    def is_enabled(self, backend: Any = None) -> bool:
        return self.enabled

    def set_enabled(self, enabled: bool, backend: Any = None, command: Optional[str] = None) -> None:
        self.calls.append(bool(enabled))
        if self.fail_with is not None:
            raise self.fail_with
        self.enabled = bool(enabled)
