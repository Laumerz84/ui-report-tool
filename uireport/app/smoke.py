"""`--smoke-test`: a synthetic end-to-end session through the real controller wiring.

What it proves (and what it never does):

* builds the real AppController; collaborators from packages A/B are used when they work and are
  replaced by fakes (uireport.app.fakes) when they raise (e.g. still a stub). Under a real desktop
  platform the editor and the toast are ALWAYS fakes, so no window flashes up - only the tray icon.
* feeds three synthetic shots (images with pins, a rectangle, an arrow, a ruler and a redaction)
  into the controller through ``capture.captured`` (no screen grab, no synthetic input),
  then runs Finish -> files on disk -> prompt on the clipboard -> fresh session.
* hotkeys: none with ``--no-hotkeys``; otherwise only the unusual combos Ctrl+Alt+Shift+F23/F24,
  unregistered again before the process exits.
* "Start with Windows" is backed by an in-memory fake: the registry Run key is never touched.
* exits by itself (0 = OK, 1 = failure with a message on stderr and in smoke-result.json).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter

from ..geometry import IntRect
from ..imageops import sample_hex
from ..models import (
    ArrowAnn,
    CaptureMode,
    MonitorMeta,
    PinAnn,
    RectAnn,
    RedactAnn,
    Role,
    RulerAnn,
    Session,
    Shot,
    WindowMeta,
)
from ..output.clipboard import build_prompt
from ..settings import SettingsStore
from .fakes import FakeCapture, FakeEditor, FakeHotkeys, FakeToast, MemoryAutostart

log = logging.getLogger("uireport.smoke")

SMOKE_HOTKEY_CAPTURE = "Ctrl+Alt+Shift+F23"
SMOKE_HOTKEY_DELAYED = "Ctrl+Alt+Shift+F24"
SMOKE_GOAL = "Smoke test: check the report pipeline"
SHOT_SIZES = [(320, 200), (400, 240), (360, 220)]
WATCHDOG_MS = 30000
LINGER_MS = 400


def make_synthetic_shot(index: int, scale_percent: int = 100) -> Shot:
    """A Shot with a gradient image and one of each annotation kind (pure Qt, no screen)."""
    w, h = SHOT_SIZES[index % len(SHOT_SIZES)]
    image = QImage(w, h, QImage.Format.Format_ARGB32)
    grad = QLinearGradient(0, 0, w, h)
    grad.setColorAt(0.0, QColor("#F4F1DE"))
    grad.setColorAt(1.0, QColor("#3D405B"))
    p = QPainter(image)
    p.fillRect(0, 0, w, h, grad)
    p.end()
    image.setPixelColor(40, 30, QColor("#1F2937"))

    dpi = round(96 * scale_percent / 100)
    monitor = MonitorMeta(index=1, name=r"\\.\DISPLAY1", is_primary=True, rect=IntRect(0, 0, 1920, 1080), dpi=dpi)
    shot = Shot(
        role=(Role.PROBLEM, Role.WANT, Role.AFTER)[index % 3],
        caption=f"Synthetic shot {index + 1}",
        capture_mode=CaptureMode.DELAYED if index % 2 else CaptureMode.NORMAL,
        capture_rect=IntRect(100 + index, 50, w, h),
        monitor=monitor,
        window=WindowMeta(
            title="Smoke test window", process_name="smoke.exe", exe_path=r"C:\smoke\smoke.exe",
            rect=IntRect(80, 40, 1200, 800), dpi=dpi,
        ),
    )
    shot.set_image(image)
    shot.add_annotation(PinAnn(x=40, y=30, note="pin note", color=sample_hex(image, 40, 30)))
    shot.add_annotation(RectAnn(x=10, y=10, w=60, h=40))
    shot.add_annotation(ArrowAnn(x1=100, y1=100, x2=160, y2=140))
    shot.add_annotation(RulerAnn(x1=20, y1=150, x2=140, y2=150))
    shot.add_annotation(RedactAnn(x=200, y=20, w=80, h=30))
    return shot


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


class SmokeRunner(QObject):
    """Drives the steps on the GUI thread; the writer runs on its worker like in production."""

    def __init__(self, app: Any, args: Any, store: SettingsStore, output_root: Path, work_dir: Path,
                 errors: list[str]) -> None:
        super().__init__()
        self.app = app
        self.args = args
        self.store = store
        self.output_root = Path(output_root)
        self.work_dir = Path(work_dir)
        self.errors = errors  # uncaught exceptions collected by the excepthook
        self.parts: dict[str, str] = {}
        self.details: dict[str, Any] = {}  # evidence written to smoke-result.json
        self.warnings: list[str] = []
        self.exit_code = 1
        self.message = "did not finish"
        self.expected_sizes: list[tuple[int, int]] = []
        self.opened: list[str] = []
        self.controller: Any = None
        self.autostart = MemoryAutostart()
        self._done = False
        self._tray_missing = False
        self._t0 = time.monotonic()
        self._offscreen = QGuiApplication.platformName() == "offscreen"

    # ------------------------------------------------------------------ building
    def _build_controller(self) -> Any:
        from .controller import AppController
        from .toast import Toast
        from .tray import TrayIcon

        store = self.store
        # unusual hotkeys only, and only in memory (never saved: the temp settings file is disposable)
        store.settings.hotkey_capture = SMOKE_HOTKEY_CAPTURE
        store.settings.hotkey_delayed = SMOKE_HOTKEY_DELAYED
        store.settings.output_root = str(self.output_root)  # nothing in the temp settings points at real folders

        try:
            from ..capture import CaptureService

            capture: Any = CaptureService(lambda: store.settings)
            self.parts["capture"] = "real"
        except Exception as exc:
            log.info("CaptureService unavailable (%s): using a fake", exc)
            capture = FakeCapture()
            self.parts["capture"] = "fake"

        hotkeys: Optional[Any] = None
        if not self.args.no_hotkeys:
            try:
                from ..capture import HotkeyManager

                hotkeys = HotkeyManager()
                self.parts["hotkeys"] = "real"
            except Exception as exc:
                log.info("HotkeyManager unavailable (%s): using a fake", exc)
                hotkeys = FakeHotkeys()
                self.parts["hotkeys"] = "fake"
        else:
            self.parts["hotkeys"] = "off"

        def editor_factory(session: Session) -> Any:
            if self._offscreen:
                try:
                    from ..editor import EditorWindow

                    editor = EditorWindow(session)
                    self.parts["editor"] = "real"
                    return editor
                except Exception as exc:
                    log.info("EditorWindow unavailable (%s): using a fake", exc)
            self.parts["editor"] = "fake"
            return FakeEditor(session)

        toast: Any = Toast() if self._offscreen else FakeToast()
        self.parts["toast"] = "real" if self._offscreen else "fake"
        self.parts["tray"] = "real"

        return AppController(
            store,
            capture=capture,
            hotkeys=hotkeys,
            editor_factory=editor_factory,
            tray=TrayIcon(),
            toast=toast,
            autostart=self.autostart,
            enable_hotkeys=not self.args.no_hotkeys,
            output_root=self.output_root,
            confirm_quit=lambda _n: "discard",
            open_path=self.opened.append,
        )

    # ------------------------------------------------------------------ run
    def run(self) -> int:
        try:
            self.controller = self._build_controller()
            self.controller.session_finished.connect(self._on_finished)
            self.controller.finish_failed.connect(lambda msg: self._fail(f"finish failed: {msg}"))
            self.controller.start()
            self._record_startup_evidence()
        except Exception as exc:
            log.exception("smoke setup failed")
            self._fail(f"setup failed: {type(exc).__name__}: {exc}")
            self._cleanup()
            return self.exit_code
        QTimer.singleShot(50, self._populate)
        QTimer.singleShot(WATCHDOG_MS, lambda: self._fail(f"timed out after {WATCHDOG_MS / 1000:.0f} s"))
        self.app.exec()
        self._cleanup()
        return self.exit_code

    def _record_startup_evidence(self) -> None:
        """Right after controller.start(): is the tray icon really up, which hotkeys are really
        registered? (Recorded in smoke-result.json; a missing tray on a desktop that has one
        is a failure, a taken hotkey combination is only a warning.)"""
        from PySide6.QtWidgets import QSystemTrayIcon

        available = bool(QSystemTrayIcon.isSystemTrayAvailable())
        visible = bool(self.controller.tray.isVisible())
        self.details["platform"] = QGuiApplication.platformName()
        self.details["tray_available"] = available
        self.details["tray_visible"] = visible
        self.details["tray_menu"] = list(getattr(self.controller.tray, "menu_labels", lambda: [])())
        hk = self.controller.hotkeys
        self.details["hotkeys_registered_during_run"] = dict(getattr(hk, "registered", {}) or {}) if hk else {}
        if available and not visible:
            self.warnings.append("the tray icon was created but is not visible")
            self._tray_missing = True
        elif not available:
            self.warnings.append("this session reports no system tray; the tray icon could not be shown")

    def _populate(self) -> None:
        if self._done:
            return
        try:
            c = self.controller
            c.session.goal = SMOKE_GOAL
            c.session.project_path = r"C:\smoke\project"
            c.session.framework_hint = "Smoke + Qt"
            for i in range(3):
                shot = make_synthetic_shot(i, scale_percent=150 if i == 1 else 100)
                self.expected_sizes.append((shot.image.width(), shot.image.height()))
                c.capture.captured.emit(shot)  # F3 through the real controller
            self._check(len(c.session.shots) == 3, f"expected 3 shots in the session, got {len(c.session.shots)}")
            self._check([s.index for s in c.session.shots] == [1, 2, 3], "shot indices are not 1..3")
            c.finish_session()  # F7: worker thread -> _on_finished / finish_failed
        except Exception as exc:
            log.exception("smoke populate failed")
            self._fail(f"populate failed: {type(exc).__name__}: {exc}")

    def _on_finished(self, out: Any) -> None:
        if self._done:
            return
        try:
            self._verify(out)
        except Exception as exc:
            log.exception("smoke verification failed")
            self._fail(f"{type(exc).__name__}: {exc}")
            return
        self._succeed()

    # ------------------------------------------------------------------ verification
    @staticmethod
    def _check(condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)

    def _verify(self, out: Any) -> None:
        c = self.controller
        folder = Path(out.folder)
        self._check(folder.is_dir(), f"session folder missing: {folder}")
        self._check(_same_path(folder.parent, self.output_root),
                    f"session folder {folder} is not inside the output root {self.output_root}")
        names = {p.name for p in folder.iterdir()}
        expected = {"report.md", "report.json"}
        for i in range(1, 4):
            expected |= {f"{i:02d}.png", f"{i:02d}_annotated.png"}
        self._check(names == expected, f"unexpected files in the session folder: {sorted(names ^ expected)}")
        self._check((self.output_root / "latest.json").is_file(), "latest.json missing")
        for i, (w, h) in enumerate(self.expected_sizes, start=1):
            for name in (f"{i:02d}.png", f"{i:02d}_annotated.png"):
                img = QImage(str(folder / name))
                self._check(not img.isNull() and (img.width(), img.height()) == (w, h),
                            f"{name} has the wrong size (expected {w}x{h})")
        doc = json.loads(Path(out.report_json).read_text(encoding="utf-8"))
        self._check(len(doc["shots"]) == 3 and doc["session"]["goal"] == SMOKE_GOAL, "report.json content is wrong")
        pin = doc["shots"][0]["annotations"][0]
        self._check((pin["x"], pin["y"], pin["color"]) == (40, 30, "#1F2937"), f"pin data wrong: {pin}")
        md = Path(out.report_md).read_text(encoding="utf-8")
        self._check("## 1. [Problem] Synthetic shot 1" in md and "Pin 1 at (40, 30), color #1F2937" in md,
                    "report.md content is wrong")

        clip = QGuiApplication.clipboard().text()
        want = build_prompt(3, SMOKE_GOAL, Path(out.report_md))
        self._check(clip == want, f"clipboard prompt mismatch: {clip!r}")

        toast = c.toast
        if isinstance(toast, FakeToast):
            self._check(toast.last is not None and toast.last["action_text"] == "Open folder"
                        and toast.last["message"].startswith("Copied"), "finish toast wrong")
            toast.click_action()
        else:
            self._check(toast.message_label.text().startswith("Copied"), "finish toast wrong")
            toast.action_button.click()
        self._check(self.opened == [str(folder)], f"'Open folder' opened {self.opened!r}")

        if self.parts.get("hotkeys") == "real":  # informational: a taken combination is not a wiring bug
            want = {"capture": SMOKE_HOTKEY_CAPTURE, "delayed": SMOKE_HOTKEY_DELAYED}
            got = dict(getattr(c.hotkeys, "registered", {}) or {})
            if got != want:
                self.warnings.append(f"hotkeys registered as {got}, expected {want}")

        fresh = c.session
        self._check(len(fresh.shots) == 0, "the new session is not empty")
        self._check(fresh.project_path == r"C:\smoke\project" and fresh.framework_hint == "Smoke + Qt",
                    "project path / framework hint were not remembered")
        saved = json.loads(self.store.path.read_text(encoding="utf-8"))
        self._check(saved.get("last_project_path") == r"C:\smoke\project", "settings were not saved on Finish")
        self._check(self.autostart.calls == [], "autostart was touched")
        self._check(not self._tray_missing, "the tray icon is not visible")

    # ------------------------------------------------------------------ endings
    def _succeed(self) -> None:
        self._done = True
        self.exit_code = 0
        self.message = "ok"
        QTimer.singleShot(LINGER_MS, self._leave)

    def _fail(self, message: str) -> None:
        if self._done:
            return
        self._done = True
        self.exit_code = 1
        self.message = message
        QTimer.singleShot(0, self._leave)

    def _leave(self) -> None:
        # exit(), not quit(): quit() would send close events to a visible editor, which ignores them
        self.app.exit(0)

    def _cleanup(self) -> None:
        """Always: unregister hotkeys, hide the tray, then write the result file."""
        c = self.controller
        if c is not None:
            try:
                c.shutdown()
            except Exception as exc:
                self.warnings.append(f"shutdown raised {exc!r}")
                if self.exit_code == 0:
                    self.exit_code, self.message = 1, f"shutdown raised {exc!r}"
            hk = getattr(c, "hotkeys", None)
            registered = getattr(hk, "registered", None) if hk is not None else None
            self.details["hotkeys_registered_after_shutdown"] = dict(registered or {})
            self.details["tray_visible_after_shutdown"] = bool(self.controller.tray.isVisible())
            if registered:
                self.exit_code, self.message = 1, f"hotkeys still registered after shutdown: {registered}"
        if self.errors and self.exit_code == 0:
            self.exit_code, self.message = 1, f"uncaught exception: {self.errors[0].strip().splitlines()[-1]}"
        result = {
            "ok": self.exit_code == 0,
            "message": self.message,
            "parts": self.parts,
            "details": self.details,
            "warnings": self.warnings,
            "seconds": round(time.monotonic() - self._t0, 2),
        }
        try:
            self.work_dir.mkdir(parents=True, exist_ok=True)
            (self.work_dir / "smoke-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        except OSError:
            pass
        line = f"SMOKE {'OK' if self.exit_code == 0 else 'FAILED'}: {self.message} " \
               f"({', '.join(f'{k}={v}' for k, v in self.parts.items())}; {result['seconds']} s)"
        stream = sys.stdout if self.exit_code == 0 else sys.stderr
        if stream is not None:
            try:
                print(line, file=stream)
            except Exception:
                pass


def run_smoke_test(app: Any, args: Any, store: SettingsStore, output_root: Path, work_dir: Path,
                   errors: Optional[list[str]] = None) -> int:
    """Entry used by main(): returns the process exit code (0 = everything worked)."""
    runner = SmokeRunner(app, args, store, output_root, work_dir, errors if errors is not None else [])
    return runner.run()
