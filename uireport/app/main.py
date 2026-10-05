"""Application entry point. Owner: output/app builder (package C)."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import shutil
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from typing import Callable, Optional, Sequence

from .. import APP_ID, APP_NAME, __version__
from ..settings import SettingsStore, resolve_settings_path
from .autostart import BLOCK_ENV

log = logging.getLogger("uireport")

LOG_FILE_NAME = "uireport.log"
_HANDLER_TAG = "_uireport_handler"


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Flags (all optional):
        --settings-path PATH   settings JSON to use (default: %APPDATA%\\UIReportTool\\settings.json,
                               or $UIREPORT_SETTINGS)
        --output-root PATH     override the reports folder for this run (not saved to settings)
        --smoke-test           start the real app wiring, run a synthetic end-to-end session
                               through the controller (fake shots -> write_session into a TEMP
                               output root -> prompt on the clipboard is checked), then exit 0
                               by itself after ~3 s (non-zero + message on failure)
        --offscreen            set QT_QPA_PLATFORM=offscreen before the QApplication exists
        --no-hotkeys           do not register global hotkeys
        --instance-name NAME   single-instance key (default 'UIReportTool'); smoke tests use
                               a unique name so they never collide with a running copy
        --quit                 ask the running copy to quit exactly like its tray Quit (it may
                               ask about unsaved shots), then exit: 0 = request delivered,
                               3 = no copy running, 1 = a copy is running but did not listen
    """
    parser = argparse.ArgumentParser(
        prog="uireport",
        description=f"{APP_NAME} v{__version__}: capture UI screenshots, annotate them and hand the set to Claude Code.",
    )
    parser.add_argument("--settings-path", metavar="PATH", default=None,
                        help="settings JSON file (default: %%APPDATA%%\\UIReportTool\\settings.json)")
    parser.add_argument("--output-root", metavar="PATH", default=None,
                        help="reports folder for this run only (not saved to the settings)")
    parser.add_argument("--smoke-test", action="store_true",
                        help="run a synthetic end-to-end session, then exit (0 = OK)")
    parser.add_argument("--offscreen", action="store_true",
                        help="use Qt's offscreen platform (no windows on the desktop)")
    parser.add_argument("--no-hotkeys", action="store_true", help="do not register global hotkeys")
    parser.add_argument("--instance-name", metavar="NAME", default=APP_ID,
                        help=f"single-instance key (default {APP_ID})")
    parser.add_argument("--quit", action="store_true",
                        help="ask the running copy to quit like its tray Quit (0 = asked, 3 = none running)")
    parser.add_argument("--verbose", action="store_true", help="write debug messages to the log")
    return parser.parse_args(list(argv) if argv is not None else None)


# ---------------------------------------------------------------------------
# logging and crash reporting (pythonw has no console)
# ---------------------------------------------------------------------------
def setup_logging(log_dir: Path, verbose: bool = False) -> Optional[Path]:
    """Rotating file log in `log_dir` (+ stderr when there is one). Returns the log path."""
    logger = logging.getLogger("uireport")
    teardown_logging()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    log_path: Optional[Path] = None
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / LOG_FILE_NAME
        fh = logging.handlers.RotatingFileHandler(log_path, maxBytes=512 * 1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        setattr(fh, _HANDLER_TAG, True)
        logger.addHandler(fh)
    except OSError:
        log_path = None
    if sys.stderr is not None:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        setattr(sh, _HANDLER_TAG, True)
        logger.addHandler(sh)
    logger.propagate = False
    return log_path


def teardown_logging() -> None:
    logger = logging.getLogger("uireport")
    for h in list(logger.handlers):
        if getattr(h, _HANDLER_TAG, False):
            logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass


def install_excepthook(
    log_path: Optional[Path], *, show_dialog: bool, on_error: Optional[Callable[[str], None]] = None
) -> None:
    """Log every uncaught exception; show a message box for GUI-thread ones (pythonw has no console)."""
    busy = {"active": False}

    def report(exc_type, exc, tb, thread_name: str) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        log.error("Uncaught exception in %s:\n%s", thread_name, text)
        if on_error is not None:
            try:
                on_error(text)
            except Exception:
                pass
            return
        if not show_dialog or busy["active"] or threading.current_thread() is not threading.main_thread():
            return
        busy["active"] = True
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is not None:
                where = f"\n\nDetails were written to:\n{log_path}" if log_path else ""
                QMessageBox.critical(None, APP_NAME, f"Unexpected error: {exc}{where}")
        except Exception:
            pass
        finally:
            busy["active"] = False

    def hook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        report(exc_type, exc, tb, threading.current_thread().name)

    sys.excepthook = hook
    threading.excepthook = lambda a: report(a.exc_type, a.exc_value, a.exc_traceback, getattr(a.thread, "name", "thread"))


# ---------------------------------------------------------------------------
# single instance
# ---------------------------------------------------------------------------
class SingleInstance:
    """Named-mutex guard (falls back to a QLockFile when pywin32 is unavailable)."""

    def __init__(self, name: str) -> None:
        self.name = "".join(c if c.isalnum() or c in "._-" else "_" for c in (name or APP_ID)) or APP_ID
        self._handle = None
        self._lock = None

    def acquire(self) -> bool:
        """True if we are the only instance now; False if another one already holds the name."""
        try:
            import win32api
            import win32event
            import winerror

            handle = win32event.CreateMutex(None, False, f"Local\\{self.name}")
            if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
                win32api.CloseHandle(handle)
                return False
            self._handle = handle
            return True
        except ImportError:
            pass
        from PySide6.QtCore import QLockFile

        lock = QLockFile(str(Path(tempfile.gettempdir()) / f"{self.name}.lock"))
        if not lock.tryLock(0):
            return False
        self._lock = lock
        return True

    def release(self) -> None:
        if self._handle is not None:
            try:
                import win32api

                win32api.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None
        if self._lock is not None:
            self._lock.unlock()
            self._lock = None


def _say(message: str) -> None:
    if sys.stderr is not None:
        try:
            print(message, file=sys.stderr)
        except Exception:
            pass


EXIT_QUIT_NOT_RUNNING = 3


def _send_quit(instance_name: str) -> int:
    """--quit: 0 = the running copy was asked, 3 = none is running, 1 = one runs but does not listen."""
    from .control import request_quit

    name = SingleInstance(instance_name).name
    if request_quit(name):
        log.info("--quit: asked the running copy to quit")
        return 0
    probe = SingleInstance(instance_name)
    if probe.acquire():
        probe.release()
        log.info("--quit: no copy is running")
        _say(f"{APP_NAME} is not running.")
        return EXIT_QUIT_NOT_RUNNING
    log.warning("--quit: a copy is running but does not accept --quit (an older version?)")
    _say(f"{APP_NAME} is running but did not accept the quit request; use its tray icon's Quit.")
    return 1


def _message_box(title: str, text: str, *, offscreen: bool, warn: bool = False) -> None:
    """A blocking message box - never under the offscreen platform (nobody could click it)."""
    if offscreen:
        return
    try:
        from PySide6.QtWidgets import QMessageBox

        (QMessageBox.warning if warn else QMessageBox.information)(None, title, text)
    except Exception:
        log.exception("could not show a message box")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    """Order matters: (1) parse args, set QT_QPA_PLATFORM if --offscreen; (2)
    capture.enable_per_monitor_dpi_awareness(); (3) create QApplication with
    setQuitOnLastWindowClosed(False); (4) single-instance guard (named mutex / QLockFile);
    a second instance shows a message and exits 0; (5) SettingsStore(args.settings_path);
    (6) AppController(...).start(); (7) app.exec(); on exit make sure hotkeys are
    unregistered. Returns the process exit code."""
    # (1) arguments and platform
    args = parse_args(argv)
    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    smoke_dir: Optional[Path] = None
    if args.smoke_test:
        os.environ[BLOCK_ENV] = "1"  # the smoke test must never write the real Run key
        os.environ["UIREPORT_NO_PASTE"] = "1"  # ... nor paste its fake report into the user's Claude
        if not args.settings_path:  # never fall back to the real settings folder
            smoke_dir = Path(tempfile.mkdtemp(prefix="uireport-smoke-"))
            args.settings_path = str(smoke_dir / "settings.json")
        if not args.output_root:
            base = Path(os.path.abspath(args.settings_path)).parent
            args.output_root = str(base / "reports")

    settings_path = resolve_settings_path(args.settings_path)
    log_path = setup_logging(settings_path.parent, args.verbose)
    log.info("%s v%s starting (pid %s, smoke=%s)", APP_NAME, __version__, os.getpid(), args.smoke_test)
    smoke_errors: list[str] = []
    install_excepthook(
        log_path,
        show_dialog=not (args.offscreen or args.smoke_test),
        on_error=smoke_errors.append if args.smoke_test else None,
    )

    try:
        return _run(args, settings_path, log_path, smoke_errors)
    finally:
        teardown_logging()
        if smoke_dir is not None:
            shutil.rmtree(smoke_dir, ignore_errors=True)


def _run(args: argparse.Namespace, settings_path: Path, log_path: Optional[Path], smoke_errors: list[str]) -> int:
    offscreen = os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen"

    # (2) per-monitor DPI awareness, before any window exists
    try:
        from ..capture.winapi import enable_per_monitor_dpi_awareness

        enable_per_monitor_dpi_awareness()
    except Exception as exc:  # stub / non-Windows: Qt 6 sets PMv2 by itself anyway
        log.warning("could not set per-monitor DPI awareness explicitly: %s", exc)

    # (3) the application object
    from PySide6.QtWidgets import QApplication

    from .icons import make_app_icon

    app = QApplication.instance() or QApplication([sys.argv[0] if sys.argv else "uireport"])
    app.setApplicationName(APP_ID)
    app.setApplicationDisplayName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)  # a tray app lives on when the editor is closed
    app.setWindowIcon(make_app_icon())

    # (3b) --quit talks to the running copy and never becomes an instance itself
    if args.quit:
        return _send_quit(args.instance_name)

    # (4) single instance
    instance = SingleInstance(args.instance_name)
    if not instance.acquire():
        msg = f"{APP_NAME} is already running - look for its icon in the system tray (bottom right, maybe under the ^ arrow)."
        log.info("second instance: exiting")
        _say(msg)
        _message_box(APP_NAME, msg, offscreen=offscreen or args.smoke_test)
        return 0

    try:
        # (5) settings
        first_run = not settings_path.exists()
        store = SettingsStore(settings_path)
        output_root = Path(os.path.abspath(args.output_root)) if args.output_root else None

        if args.smoke_test:
            from .smoke import run_smoke_test

            return run_smoke_test(app, args, store, output_root or Path(store.settings.output_root),
                                  settings_path.parent, smoke_errors)

        # (6) controller
        from .controller import AppController

        try:
            controller = AppController(store, enable_hotkeys=not args.no_hotkeys, output_root=output_root)
        except Exception as exc:
            log.exception("could not build the application")
            _say(f"{APP_NAME} could not start: {exc}")
            _message_box(APP_NAME, f"{APP_NAME} could not start:\n{exc}\n\nDetails: {log_path}",
                         offscreen=offscreen, warn=True)
            return 1
        # app.exit(), not app.quit(): quit() sends close events to visible windows and the editor
        # ignores them (its X only hides it), which would silently cancel the quit.
        controller.quit_requested.connect(lambda: app.exit(0))
        app.aboutToQuit.connect(controller.shutdown)
        controller.start(welcome=first_run)

        # `--quit` from another process runs the same path as the tray's Quit
        from PySide6.QtCore import Qt

        from .control import QuitListener

        quit_listener = QuitListener(instance.name)
        quit_listener.quit_requested.connect(controller.quit, Qt.ConnectionType.QueuedConnection)
        quit_listener.start()

        # (7) run; whatever happens, leave no hotkey registered
        try:
            return int(app.exec())
        finally:
            quit_listener.stop()
            controller.shutdown()
    finally:
        instance.release()
