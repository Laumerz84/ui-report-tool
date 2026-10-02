"""AppController: the glue between capture (A), editor (B) and output (C).
Owner: output/app builder (package C)."""
from __future__ import annotations

import logging
import os
import platform
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox

from .. import __version__
from ..hotkeyspec import NAME_CAPTURE, NAME_DELAYED
from ..models import CaptureMode, Session, Shot, SystemMeta
from ..output.clipboard import copy_prompt_to_clipboard, render_copy_text
from ..output.writer import SessionOutput, write_session
from ..settings import SettingsStore

log = logging.getLogger("uireport.controller")

HIDE_SETTLE_MS = 120  # wait after hiding the editor so the compositor has removed it
TOAST_COPIED = "Copied — paste into Claude"
TOAST_COPIED_CUSTOM = "Copied to the clipboard"  # the user's own copy text may not be for Claude
TOAST_OPEN_FOLDER = "Open folder"
CHOICE_FINISH = "finish"
CHOICE_DISCARD = "discard"
CHOICE_CANCEL = "cancel"

_HOTKEY_LABELS = {NAME_CAPTURE: "capture", NAME_DELAYED: "delayed capture"}


# ---------------------------------------------------------------------------
# hooks that are easy to replace in tests
# ---------------------------------------------------------------------------
def _dwm_flush() -> None:
    """Best effort: wait for the compositor (package A). A failure never matters here."""
    try:
        from ..capture.winapi import dwm_flush

        dwm_flush()
    except Exception as exc:  # NotImplementedError while A is a stub, non-Windows, ...
        log.debug("dwm_flush unavailable: %s", exc)


def _collect_system_meta(previous: SystemMeta) -> SystemMeta:
    """SystemMeta from package A; a fallback built from `platform` if A cannot deliver."""
    try:
        from ..capture.sysinfo import collect_system_meta

        return collect_system_meta()
    except Exception as exc:
        log.warning("collect_system_meta failed (%s); using a basic fallback", exc)
    if previous.windows_version:
        return previous
    meta = SystemMeta(tool_version=__version__, monitors=list(previous.monitors))
    try:
        meta.windows_version = f"{platform.system()} {platform.release()} {platform.version()}".strip()
        meta.windows_build = int(platform.version().split(".")[-1])
    except Exception:
        pass
    return meta


def default_open_path(path: str) -> None:
    """Open a folder/file with the shell (Explorer)."""
    startfile = getattr(os, "startfile", None)
    if startfile is not None:
        startfile(str(path))
    else:  # pragma: no cover - non-Windows
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def default_confirm_quit(shot_count: int, parent: Any = None) -> str:
    """Finish now / Discard and quit / Cancel."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle("UI Report Tool")
    noun = "screenshot" if shot_count == 1 else "screenshots"
    box.setText(f"You have {shot_count} unsaved {noun} in this session.")
    box.setInformativeText("Finish now to save the report and copy the prompt, or discard the session and quit.")
    finish = box.addButton("Finish now", QMessageBox.ButtonRole.AcceptRole)
    discard = box.addButton("Discard and quit", QMessageBox.ButtonRole.DestructiveRole)
    cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(finish)
    box.setEscapeButton(cancel)
    box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    box.exec()
    clicked = box.clickedButton()
    if clicked is finish:
        return CHOICE_FINISH
    if clicked is discard:
        return CHOICE_DISCARD
    return CHOICE_CANCEL


# ---------------------------------------------------------------------------
# background writer
# ---------------------------------------------------------------------------
class _WriteSignals(QObject):
    progress = Signal(int, int)
    done = Signal(object, str)  # (SessionOutput | None, error message)


class _WriteTask(QRunnable):
    """Runs write_session off the GUI thread; results come back through queued signals."""

    def __init__(self, work: Callable[[Callable[[int, int], None]], SessionOutput], signals: _WriteSignals) -> None:
        super().__init__()
        self.setAutoDelete(False)  # Python owns it; the controller drops the reference when done
        self._work = work
        self._signals = signals

    def run(self) -> None:  # runs on a pool thread
        try:
            result = self._work(self._signals.progress.emit)
        except BaseException as exc:  # never let a worker die silently
            log.exception("writing the session failed")
            self._signals.done.emit(None, f"{type(exc).__name__}: {exc}")
        else:
            self._signals.done.emit(result, "")


# ---------------------------------------------------------------------------
# defaults for the real collaborators (built lazily so tests can inject fakes)
# ---------------------------------------------------------------------------
def _default_capture(store: SettingsStore) -> Any:
    from ..capture import CaptureService

    return CaptureService(lambda: store.settings)


def _default_hotkeys() -> Any:
    from ..capture import HotkeyManager

    return HotkeyManager()


def _default_editor_factory(session: Session) -> Any:
    from ..editor import EditorWindow

    return EditorWindow(session)


def _default_settings_window_factory(store: SettingsStore) -> Any:
    from .settings_window import SettingsWindow

    return SettingsWindow(store)


class AppController(QObject):
    """Owns the long-lived Session and connects everything. Every collaborator can be
    injected (tests pass fakes with the same signals); when None the real one is built:

        capture   -> uireport.capture.CaptureService(lambda: store.settings)
        hotkeys   -> uireport.capture.HotkeyManager()
        editor_factory(session) -> uireport.editor.EditorWindow(session)   (created lazily on
                                   the first captured shot, then reused)
        tray      -> uireport.app.tray.TrayIcon()
        toast     -> uireport.app.toast.Toast()
        settings_window_factory(store) -> uireport.app.settings_window.SettingsWindow
        autostart -> the uireport.app.autostart module (functions is_enabled / set_enabled)

    Full behaviour table: CONTRACT.md section 6.

    Signals (for tests / smoke test):
        session_finished(SessionOutput)   after files are written and the prompt is on the clipboard
        finish_failed(str)                writing failed; the session is kept intact
        quit_requested()                  the app should exit (main() connects this to QApplication.quit)

    Extra optional constructor arguments (appended, contract-compatible):
        output_root   override of the reports folder for this run (never saved to settings)
        confirm_quit  callable(shot_count) -> 'finish' | 'discard' | 'cancel' (default: message box)
        open_path     callable(path) used for 'Open folder' / 'Open reports folder' (default os.startfile)
    """

    session_finished = Signal(object)
    finish_failed = Signal(str)
    quit_requested = Signal()

    def __init__(
        self,
        store: SettingsStore,
        *,
        capture: Optional[Any] = None,
        hotkeys: Optional[Any] = None,
        editor_factory: Optional[Callable[[Session], Any]] = None,
        tray: Optional[Any] = None,
        toast: Optional[Any] = None,
        settings_window_factory: Optional[Callable[[SettingsStore], Any]] = None,
        autostart: Optional[Any] = None,
        enable_hotkeys: bool = True,
        parent: Optional[QObject] = None,
        output_root: "Optional[Path | str]" = None,
        confirm_quit: Optional[Callable[[int], str]] = None,
        open_path: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self._enable_hotkeys = bool(enable_hotkeys)
        self._output_root_override = Path(output_root) if output_root else None
        self._confirm_quit_fn = confirm_quit
        self._open_path_fn = open_path or default_open_path
        self._editor_factory = editor_factory or _default_editor_factory
        self._settings_window_factory = settings_window_factory or _default_settings_window_factory

        self._session: Session = store.settings.new_session()
        self._editor: Optional[Any] = None
        self._settings_window: Optional[Any] = None
        self._resume_shot_id: Optional[str] = None
        self._pending_mode: Optional[CaptureMode] = None
        self._finishing = False
        self._quit_after_finish = False
        self._shut_down = False
        self._started = False
        self._hotkeys_paused = False
        self._write_job: Optional[tuple[_WriteTask, _WriteSignals]] = None

        # collaborators
        self.capture = capture if capture is not None else _default_capture(store)
        if hotkeys is not None:
            self.hotkeys: Optional[Any] = hotkeys
        else:
            self.hotkeys = _default_hotkeys() if self._enable_hotkeys else None
        if tray is None:
            from .tray import TrayIcon

            tray = TrayIcon()
        self.tray = tray
        if toast is None:
            from .toast import Toast

            toast = Toast()
        self.toast = toast
        if autostart is None:
            from . import autostart as autostart_module

            autostart = autostart_module
        self.autostart = autostart

        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._settle_timer = QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.timeout.connect(self._after_settle)

        # capture results (GUI thread -> direct connections)
        self.capture.captured.connect(self._on_captured)
        self.capture.cancelled.connect(self._on_cancelled)
        self.capture.failed.connect(self._on_failed)
        # hotkey activations arrive from the hotkey thread -> queued onto the GUI thread
        if self.hotkeys is not None:
            self.hotkeys.activated.connect(self._on_hotkey, Qt.ConnectionType.QueuedConnection)
            reg_failed = getattr(self.hotkeys, "registration_failed", None)
            if reg_failed is not None:
                reg_failed.connect(self._on_registration_failed, Qt.ConnectionType.QueuedConnection)
        # tray
        self.tray.capture_requested.connect(self._on_tray_capture)
        self.tray.delayed_requested.connect(self._on_tray_delayed)
        self.tray.settings_requested.connect(self.show_settings)
        self.tray.open_folder_requested.connect(self.open_reports_folder)
        self.tray.show_session_requested.connect(self.show_session)
        self.tray.quit_requested.connect(self.quit)

    # ------------------------------------------------------------------ properties
    @property
    def session(self) -> Session:
        return self._session

    @property
    def editor(self) -> Optional[Any]:
        return self._editor

    @property
    def settings_window(self) -> Optional[Any]:
        return self._settings_window

    @property
    def is_finishing(self) -> bool:
        return self._finishing

    @property
    def output_root(self) -> Path:
        if self._output_root_override is not None:
            return self._output_root_override
        return Path(self.store.settings.output_root)

    # ------------------------------------------------------------------ lifecycle
    def start(self, welcome: bool = False) -> None:
        """Show the tray icon, register the hotkeys (unless enable_hotkeys=False; a failed
        registration is reported with a toast + tray message, the app keeps running)."""
        if self._started or self._shut_down:
            return
        self._started = True
        self._sync_tray()
        self._refresh_hints()
        try:
            self.tray.show()
        except Exception:
            log.exception("could not show the tray icon")
        if self._enable_hotkeys and self.hotkeys is not None:
            try:
                self.hotkeys.start()
            except Exception as exc:
                log.exception("hotkey thread failed to start")
                self._notify_error(f"Global hotkeys are not available: {exc}. The tray menu still works.")
            else:
                self._register_hotkeys(report=True)
        if welcome:
            s = self.store.settings
            self._toast(
                f"UI Report Tool is running in the tray. Press {s.hotkey_capture} to capture, "
                f"or click the tray icon.",
                timeout_ms=10000,
            )

    def shutdown(self) -> None:
        """Stop hotkeys (unregister all), hide tray/toast, close the editor. Idempotent."""
        if self._shut_down:
            return
        self._shut_down = True
        self._settle_timer.stop()
        self._pending_mode = None
        try:
            if getattr(self.capture, "is_active", False):
                self.capture.cancel()
        except Exception:
            log.exception("cancelling the active capture failed")
        if self.hotkeys is not None:
            try:
                self.hotkeys.stop()
            except Exception:
                log.exception("stopping the hotkeys failed")
        win, self._settings_window = self._settings_window, None
        if win is not None:
            try:
                win.close()
            except Exception:
                pass
        editor = self._editor
        if editor is not None:
            try:
                editor.hide()
            except Exception:
                pass
        try:
            self.toast.dismiss()
        except Exception:
            pass
        try:
            self.tray.hide()
        except Exception:
            pass
        # let a running write finish (it never touches widgets); bounded so quitting cannot hang
        self._pool.waitForDone(15000)

    # ------------------------------------------------------------------ small helpers
    def _toast(
        self,
        message: str,
        *,
        action_text: Optional[str] = None,
        action: Optional[Callable[[], None]] = None,
        timeout_ms: int = 8000,
        error: bool = False,
    ) -> None:
        try:
            self.toast.show_toast(
                message, action_text=action_text, action=action, timeout_ms=timeout_ms, error=error
            )
        except Exception:
            log.exception("could not show a toast")

    def _dismiss_toast(self) -> None:
        try:
            self.toast.dismiss()
        except Exception:
            log.exception("could not dismiss the toast")

    def _notify_error(self, message: str) -> None:
        self._toast(message, error=True, timeout_ms=12000)
        show_message = getattr(self.tray, "showMessage", None)
        if show_message is not None:
            try:
                show_message("UI Report Tool", message)
            except Exception:
                log.debug("tray message failed", exc_info=True)

    def _sync_tray(self) -> None:
        try:
            self.tray.set_session_count(len(self._session.shots))
        except Exception:
            log.exception("could not update the tray")

    def _refresh_hints(self) -> None:
        s = self.store.settings
        try:
            self.tray.set_hotkey_hint(s.hotkey_capture, s.hotkey_delayed)
        except Exception:
            log.exception("could not update the tray hints")

    def _editor_visible(self) -> bool:
        ed = self._editor
        if ed is None:
            return False
        try:
            return bool(ed.isVisible())
        except Exception:
            return False

    def _current_shot_id(self) -> Optional[str]:
        ed = self._editor
        if ed is None:
            return None
        try:
            return ed.current_shot_id()
        except Exception:
            return None

    def _ensure_editor(self) -> Any:
        if self._editor is None:
            editor = self._editor_factory(self._session)
            editor.next_requested.connect(self._on_editor_next)
            editor.next_delayed_requested.connect(self._on_editor_next_delayed)
            editor.finish_requested.connect(self.finish_session)
            editor.discard_session_requested.connect(self.discard_session)
            editor.session_changed.connect(self._on_session_changed)
            self._editor = editor
        return self._editor

    def _pick_shot(self, preferred: Optional[str]) -> Optional[Shot]:
        """The shot to show: `preferred` if it still exists, else the last shot."""
        if preferred:
            shot = self._session.get_shot(preferred)
            if shot is not None:
                return shot
        return self._session.shots[-1] if self._session.shots else None

    def _present_shot(self, shot: Optional[Shot], *, refresh: bool = False) -> None:
        """Bring the editor up on `shot` (caption box focused by the editor)."""
        try:
            editor = self._ensure_editor()
            if refresh:
                editor.refresh()
            if shot is not None:
                editor.show_shot(shot.id)
            editor.present(shot.monitor if shot is not None else None)
        except Exception as exc:
            log.exception("could not show the editor")
            self._toast(f"Could not open the editor: {exc}", error=True, timeout_ms=12000)

    def _restore_editor(self) -> None:
        """After a cancelled/failed capture (F4/F5): back to the shot that was open, or idle."""
        preferred, self._resume_shot_id = self._resume_shot_id, None
        if self._shut_down or not self._session.shots:
            return
        self._present_shot(self._pick_shot(preferred), refresh=True)

    def _hide_editor(self) -> None:
        if self._editor is not None:
            try:
                self._editor.hide()
            except Exception:
                log.exception("could not hide the editor")

    # ------------------------------------------------------------------ hotkeys
    def _hotkey_mapping(self) -> dict[str, str]:
        s = self.store.settings
        return {NAME_CAPTURE: s.hotkey_capture, NAME_DELAYED: s.hotkey_delayed}

    def _register_hotkeys(self, report: bool) -> dict[str, str]:
        """(Re-)register both hotkeys; returns {name: reason} for failures. With report=True
        each failure is announced (F16); the app keeps running either way."""
        if self.hotkeys is None or self._shut_down:
            return {}
        mapping = self._hotkey_mapping()
        try:
            results = self.hotkeys.set_hotkeys(mapping) or {}
        except Exception as exc:
            log.exception("registering the hotkeys failed")
            results = {name: str(exc) for name in mapping}
        failures = {name: str(reason) for name, reason in results.items() if reason}
        if report:
            for name, reason in failures.items():
                self._report_hotkey_failure(name, reason)
        return failures

    def _hotkey_failure_text(self, name: str, reason: str) -> str:
        combo = self._hotkey_mapping().get(name, name)
        return f"{combo} could not be registered as the {_HOTKEY_LABELS.get(name, name)} hotkey ({reason})"

    def _report_hotkey_failure(self, name: str, reason: str) -> None:
        text = self._hotkey_failure_text(name, reason)
        log.warning(text)
        self._notify_error(f"{text}. Use the tray menu, or choose another combination in Settings.")

    def _on_registration_failed(self, name: str, reason: str) -> None:
        # HotkeyManager also emits this signal for every failure that set_hotkeys() returns.
        # The returned dict is authoritative and already produced the toast / inline error,
        # so this (queued) signal is only logged; announcing it too would show every error twice.
        log.info("hotkey registration failed: %s (%s)", name, reason)

    def _on_hotkey(self, name: str) -> None:
        if name == NAME_CAPTURE:
            self.request_capture(CaptureMode.NORMAL)
        elif name == NAME_DELAYED:
            self.request_capture(CaptureMode.DELAYED)

    # ------------------------------------------------------------------ capture (F1, F2, F6, F11)
    def _on_tray_capture(self) -> None:
        self.request_capture(CaptureMode.NORMAL)

    def _on_tray_delayed(self) -> None:
        self.request_capture(CaptureMode.DELAYED)

    def _on_editor_next(self) -> None:
        self.request_capture(CaptureMode.NORMAL)

    def _on_editor_next_delayed(self) -> None:
        self.request_capture(CaptureMode.DELAYED)

    def _capture_busy(self) -> bool:
        if self._settle_timer.isActive() or self._finishing:
            return True
        try:
            return bool(self.capture.is_active)
        except Exception:
            return False

    def request_capture(self, mode: CaptureMode) -> None:
        """Hotkey / tray / editor button entry. Ignored while a capture is active. If the
        editor is visible: editor.hide_for_capture(), wait HIDE_SETTLE_MS (120 ms, QTimer),
        then capture.start(mode). Otherwise capture.start(mode) immediately (no delay)."""
        if self._shut_down or self._capture_busy():
            return
        mode = CaptureMode(mode)
        self._dismiss_toast()
        self._resume_shot_id = self._current_shot_id()
        if self._editor_visible():
            try:
                self._editor.hide_for_capture()
            except Exception:
                log.exception("hide_for_capture failed")
            _dwm_flush()
            self._pending_mode = mode
            self._settle_timer.start(HIDE_SETTLE_MS)
        else:
            self._begin_capture(mode)  # synchronous: the region selector opens with no delay

    def _after_settle(self) -> None:
        mode, self._pending_mode = self._pending_mode, None
        if mode is None or self._shut_down:
            return
        self._begin_capture(mode)

    def _begin_capture(self, mode: CaptureMode) -> None:
        self._dismiss_toast()
        try:
            started = self.capture.start(mode)
        except Exception as exc:
            log.exception("capture.start failed")
            self._toast(f"Could not start the capture: {exc}", error=True, timeout_ms=12000)
            self._restore_editor()
            return
        if started is False:
            log.debug("capture.start refused (a capture is already active)")

    def _on_captured(self, shot: Shot) -> None:
        """F3: add to the session, show it in the editor with the caption focused."""
        if self._shut_down:
            return
        self._resume_shot_id = None
        self._session.add_shot(shot)
        self._sync_tray()
        self._present_shot(shot, refresh=True)

    def _on_cancelled(self, _mode: str = "") -> None:
        """F4: nothing added; back to the shot that was open (or idle in the tray)."""
        if self._shut_down:
            return
        self._restore_editor()

    def _on_failed(self, message: str) -> None:
        """F5: error toast, then as F4."""
        if self._shut_down:
            return
        log.error("capture failed: %s", message)
        self._toast(f"Capture failed: {message}", error=True, timeout_ms=10000)
        self._restore_editor()

    # ------------------------------------------------------------------ session (F8-F12)
    def _on_session_changed(self) -> None:
        try:
            self.store.settings.remember_session(self._session)  # in memory; saved on Finish/Quit
        except Exception:
            log.exception("remember_session failed")
        self._sync_tray()

    def show_session(self) -> None:
        """Re-open the editor on the current session's last shot (tray 'Show session')."""
        if self._shut_down or self._finishing:
            return
        try:
            if self.capture.is_active or self._settle_timer.isActive():
                return  # the selector / countdown owns the screen; the editor comes back afterwards
        except Exception:
            pass
        self._present_shot(self._pick_shot(self._current_shot_id()), refresh=True)

    def discard_session(self) -> None:
        """Drop the session (the caller has already confirmed) and start a fresh one."""
        if self._finishing:
            return
        self._start_fresh_session()
        self._hide_editor()

    def _start_fresh_session(self) -> None:
        self._session = self.store.settings.new_session()
        self._resume_shot_id = None
        if self._editor is not None:
            try:
                self._editor.set_session(self._session)
            except Exception:
                log.exception("editor.set_session failed")
        self._sync_tray()

    # ------------------------------------------------------------------ finish (F7)
    def finish_session(self) -> None:
        """Finish: no-op if the session has no shots. Otherwise refresh session.system, remember
        project path/framework (store.save()), hide the editor, write the session on a worker
        thread (write_session), then on the GUI thread: copy the prompt, show the toast
        'Copied - paste into Claude' with an 'Open folder' button, start a fresh session
        (settings.new_session()), emit session_finished. On failure: emit finish_failed, show
        an error toast, keep the session and reopen the editor."""
        if self._finishing or self._shut_down:
            return
        session = self._session
        if not session.shots:
            return
        self._finishing = True
        self._settle_timer.stop()
        self._pending_mode = None
        self._resume_shot_id = self._current_shot_id()  # where to come back to if the save fails
        session.system = _collect_system_meta(session.system)
        try:
            self.store.settings.remember_session(session)
            self.store.save()
        except Exception:
            log.exception("could not save the settings")
        self._hide_editor()
        self._toast("Saving...", timeout_ms=0)

        root = self.output_root
        signals = _WriteSignals(self)
        signals.progress.connect(self._on_write_progress)
        signals.done.connect(self._on_write_done)
        task = _WriteTask(lambda progress: write_session(session, root, progress=progress), signals)
        self._write_job = (task, signals)
        try:
            self._pool.start(task)
        except Exception as exc:  # could not even start the worker
            log.exception("could not start the writer")
            self._on_write_done(None, f"{type(exc).__name__}: {exc}")

    def _on_write_progress(self, done: int, total: int) -> None:
        if self._finishing and total > 1 and done < total:
            self._toast(f"Saving... {done}/{total}", timeout_ms=0)

    def _on_write_done(self, result: Optional[SessionOutput], error: str) -> None:
        self._write_job = None
        self._finishing = False
        if error or result is None:
            self._finish_failed(error or "unknown error")
            return
        session = self._session
        template = self.store.settings.copy_text_template
        prompt = render_copy_text(template, len(session.shots), session.goal, result.report_md)
        copied = True
        copy_error = ""
        try:
            copy_prompt_to_clipboard(prompt)
        except Exception as exc:
            copied = False
            copy_error = str(exc)
            log.exception("could not copy the prompt")
        folder = result.folder
        if copied:
            self._toast(
                TOAST_COPIED if not template.strip() else TOAST_COPIED_CUSTOM,
                action_text=TOAST_OPEN_FOLDER,
                action=lambda: self._open_path(folder),
                timeout_ms=8000,
            )
        else:
            self._toast(
                f"Report saved, but the prompt could not be copied ({copy_error}). "
                f"Open the folder and read report.md.",
                action_text=TOAST_OPEN_FOLDER,
                action=lambda: self._open_path(folder),
                timeout_ms=15000,
                error=True,
            )
        self._start_fresh_session()
        self.session_finished.emit(result)
        if self._quit_after_finish:
            self._quit_after_finish = False
            self._do_quit()

    def _finish_failed(self, message: str) -> None:
        log.error("finish failed: %s", message)
        self._quit_after_finish = False
        self._toast(
            f"Could not save the report: {message}. Your screenshots are still in the session.",
            error=True,
            timeout_ms=15000,
        )
        self.finish_failed.emit(message)
        self._restore_editor()

    def _open_path(self, path: "Path | str") -> None:
        try:
            self._open_path_fn(str(path))
        except Exception as exc:
            log.exception("could not open %s", path)
            self._toast(f"Could not open {path}: {exc}", error=True, timeout_ms=10000)

    def open_reports_folder(self) -> None:
        """mkdir -p output_root, then os.startfile(output_root)."""
        root = self.output_root
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.exception("could not create %s", root)
            self._toast(f"Could not create the reports folder {root}: {exc}", error=True, timeout_ms=10000)
            return
        self._open_path(root)

    # ------------------------------------------------------------------ settings (F13)
    def _sync_autostart_flag(self) -> None:
        """The registry is the truth for 'Start with Windows' (the entry may have been removed in Task
        Manager > Startup apps); show and compare against the real state, not just the saved flag."""
        try:
            actual = bool(self.autostart.is_enabled())
        except Exception:
            log.debug("could not read the autostart state", exc_info=True)
            return
        if actual != bool(self.store.settings.start_with_windows):
            self.store.settings.start_with_windows = actual

    def show_settings(self) -> None:
        win = self._settings_window
        if win is not None:
            try:
                if win.isVisible():
                    win.raise_()
                    win.activateWindow()
                    return
            except RuntimeError:  # the C++ object is already gone
                self._settings_window = None
        self._sync_autostart_flag()
        win = self._settings_window_factory(self.store)
        win.applied.connect(self._on_settings_applied)
        recording = getattr(win, "hotkey_recording", None)
        if recording is not None:
            recording.connect(self._on_hotkey_recording)
        finished = getattr(win, "finished", None)
        if finished is not None:
            finished.connect(self._on_settings_closed)
        self._settings_window = win
        win.show()
        for name in ("raise_", "activateWindow"):
            fn = getattr(win, name, None)
            if fn is not None:
                fn()

    def _on_hotkey_recording(self, active: bool) -> None:
        """While a hotkey field records, release the global hotkeys so the user can press the
        very combination that is registered right now."""
        if self.hotkeys is None or not self._enable_hotkeys or self._shut_down:
            return
        if active and not self._hotkeys_paused:
            self._hotkeys_paused = True
            try:
                # HotkeyManager keeps names that are missing from the mapping; an empty text unbinds
                self.hotkeys.set_hotkeys({name: "" for name in self._hotkey_mapping()})
            except Exception:
                log.exception("could not pause the hotkeys")
        elif not active and self._hotkeys_paused:
            self._hotkeys_paused = False
            self._register_hotkeys(report=True)

    def _on_settings_closed(self, _result: int = 0) -> None:
        if self._hotkeys_paused:
            self._hotkeys_paused = False
            self._register_hotkeys(report=True)
        win, self._settings_window = self._settings_window, None
        if win is not None and hasattr(win, "deleteLater"):
            try:
                win.deleteLater()
            except Exception:
                pass

    def _on_settings_applied(self, new_settings: Any) -> None:
        win = self._settings_window
        old = self.store.settings
        self.store.settings = new_settings
        errors: list[str] = []

        if self.hotkeys is not None and self._enable_hotkeys:
            self._hotkeys_paused = False
            failures = self._register_hotkeys(report=False)
            if failures:
                for name, reason in failures.items():
                    errors.append(self._hotkey_failure_text(name, reason))
                # keep the last working combination for whatever failed, and put it back
                if NAME_CAPTURE in failures:
                    self.store.settings.hotkey_capture = old.hotkey_capture
                if NAME_DELAYED in failures:
                    self.store.settings.hotkey_delayed = old.hotkey_delayed
                self._register_hotkeys(report=False)

        if self.store.settings.start_with_windows != old.start_with_windows:
            flag = self.store.settings.start_with_windows
            try:
                self.autostart.set_enabled(flag)
            except Exception as exc:
                log.exception("autostart change failed")
                self.store.settings.start_with_windows = old.start_with_windows
                errors.append(f"Could not {'enable' if flag else 'disable'} 'Start with Windows': {exc}")

        try:
            self.store.save()
        except Exception as exc:
            log.exception("could not save the settings")
            errors.append(f"Could not save the settings: {exc}")

        self._refresh_hints()
        if errors:
            message = "\n".join(errors)
            if win is not None and hasattr(win, "show_error"):
                win.show_error(message)
            else:
                self._notify_error(message)

    # ------------------------------------------------------------------ quit (F15)
    def quit(self) -> None:
        """Ask before discarding a non-empty session (Finish now / Discard / Cancel), then
        shutdown() and emit quit_requested."""
        if self._shut_down or self._quit_after_finish:
            return
        if self._finishing:  # a save is running: quit as soon as it is done
            self._quit_after_finish = True
            return
        count = len(self._session.shots)
        if count > 0:
            confirm = self._confirm_quit_fn or (lambda n: default_confirm_quit(n, self._editor_widget_parent()))
            choice = confirm(count)
            if choice == CHOICE_CANCEL or choice not in (CHOICE_FINISH, CHOICE_DISCARD):
                return
            if choice == CHOICE_FINISH:
                self._quit_after_finish = True
                self.finish_session()
                if not self._finishing:  # nothing was started (cannot normally happen)
                    self._quit_after_finish = False
                    self._do_quit()
                return
        self._do_quit()

    def _editor_widget_parent(self) -> Any:
        ed = self._editor
        try:
            if ed is not None and ed.isVisible() and hasattr(ed, "windowHandle"):
                return ed
        except Exception:
            pass
        return None

    def _do_quit(self) -> None:
        try:
            self.store.settings.remember_session(self._session)
            self.store.save()
        except Exception:
            log.exception("could not save the settings on quit")
        self.shutdown()
        self.quit_requested.emit()
