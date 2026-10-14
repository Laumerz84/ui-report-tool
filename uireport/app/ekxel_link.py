"""Send a finished report to Ekxel's built-in Claude panel instead of the Claude desktop app, when an
Ekxel window was in the foreground more recently than Claude's.

FocusTracker polls the foreground window (cheap, every 0.4 s) and remembers when an Ekxel window and a
Claude desktop window were last active. send_to_ekxel() hands the prompt and the report folder to the
running Ekxel over its single-instance local socket ("SheetsSpreadsheetApp-<user>"); Ekxel opens its
Claude panel and sends the prompt with the screenshots attached (see Ekxel's sheets/ai/inbox.py).

Anonymized screenshots (uireport/anon.py): check_protection() asks Ekxel whether a captured screen area shows a
protected Ekxel workbook ("protection_at"), render_anonymized() gets Ekxel's picture of that area with stand-ins
for the personal data ("anon_shot"; Ekxel's sheets/anon_shot.py). Both block, so the controller runs them on a
worker thread (AnonJobs); when Ekxel isn't running or doesn't answer they report "not protected" / failure."""
from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

POLL_MS = 400
NL, NLB = chr(10), bytes([10])   # Ekxel's socket protocol: one JSON object per line
CHECK_TIMEOUT_MS = 2500          # protection_at: geometry only, answered at once
RENDER_TIMEOUT_MS = 30000        # anon_shot: may build Ekxel's anonymized copy first (big workbooks: seconds)


def ekxel_server_name() -> str:
    # SHEETS_SERVER_NAME: the same override Ekxel's own app.py honours (a second copy / a throwaway probe)
    return os.environ.get("SHEETS_SERVER_NAME") or (
        "SheetsSpreadsheetApp-" + (os.environ.get("USERNAME") or os.environ.get("USER") or "user"))


def _foreground_info() -> Optional[tuple[str, str, str]]:
    """(exe path, title, class name) of the foreground window, or None."""
    if sys.platform != "win32":
        return None
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    title = buf.value
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        from ..capture import windowinfo as wi
        exe = wi._exe_path_of_pid(pid.value) or ""
    except Exception:
        exe = ""
    return exe, title, cls.value


def classify(exe: str, title: str, cls: str) -> Optional[str]:
    """'ekxel', 'claude' or None for a foreground window."""
    base = os.path.basename(exe).lower()
    if base in ("pythonw.exe", "python.exe") and "Ekxel" in title:
        return "ekxel"
    if base == "claude.exe" and cls.startswith("Chrome_WidgetWin") and title.strip():
        return "claude"
    return None


class FocusTracker(QObject):
    """Remembers when Ekxel and the Claude desktop app were last in the foreground."""

    def __init__(self, parent: Optional[QObject] = None, info=_foreground_info) -> None:
        super().__init__(parent)
        self._info = info
        self.last = {"ekxel": 0.0, "claude": 0.0}
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self.poll)
        if sys.platform == "win32":
            self._timer.start()

    def poll(self) -> None:
        try:
            info = self._info()
        except Exception:
            return
        if info is None:
            return
        kind = classify(*info)
        if kind:
            self.last[kind] = time.monotonic()

    def prefer_ekxel(self) -> bool:
        """True when an Ekxel window was active more recently than the Claude desktop app."""
        return self.last["ekxel"] > self.last["claude"]


def send_to_ekxel(prompt: str, report_dir: str, timeout_ms: int = 3000) -> bool:
    """Hand the prompt to the running Ekxel and wait for its OK. False if Ekxel isn't running, is an
    older version without the Claude inbox, or didn't take it - the caller then pastes into Claude."""
    from PySide6.QtNetwork import QLocalSocket

    sock = QLocalSocket()
    sock.connectToServer(ekxel_server_name())
    if not sock.waitForConnected(min(timeout_ms, 800)):
        return False
    try:
        line = json.dumps({"rpc": "inbox", "args": {"prompt": prompt, "report_dir": str(report_dir)}}) + NL
        sock.write(line.encode("utf-8"))
        sock.flush()
        buf = b""
        deadline = time.monotonic() + timeout_ms / 1000.0
        while NLB not in buf and time.monotonic() < deadline:
            if not sock.waitForReadyRead(200):
                if sock.state() != QLocalSocket.LocalSocketState.ConnectedState:
                    break
                continue
            buf += bytes(sock.readAll())
        try:
            reply = json.loads(buf.split(NLB, 1)[0].decode("utf-8") or "{}")
        except ValueError:
            reply = {}
        return reply.get("ok") is True
    finally:
        sock.disconnectFromServer()


# ---------------------------------------------------------------------------
# anonymized screenshots (uireport/anon.py)
# ---------------------------------------------------------------------------
def _rpc(name: str, args: dict, timeout_ms: int) -> Optional[dict]:
    """One request on Ekxel's socket; the reply dict, or None (not running / no answer). Any thread."""
    from PySide6.QtNetwork import QLocalSocket

    sock = QLocalSocket()
    sock.connectToServer(ekxel_server_name())
    if not sock.waitForConnected(min(timeout_ms, 800)):
        return None
    try:
        sock.write((json.dumps({"rpc": name, "args": args}) + NL).encode("utf-8"))
        sock.flush()
        buf = b""
        deadline = time.monotonic() + timeout_ms / 1000.0
        while NLB not in buf and time.monotonic() < deadline:
            if not sock.waitForReadyRead(200):
                if sock.state() != QLocalSocket.LocalSocketState.ConnectedState:
                    break
                continue
            buf += bytes(sock.readAll())
        if NLB not in buf:
            return None
        try:
            reply = json.loads(buf.split(NLB, 1)[0].decode("utf-8") or "{}")
        except ValueError:
            return None
        return reply if isinstance(reply, dict) else None
    finally:
        sock.disconnectFromServer()


def _rect_list(rect: Any) -> list[int]:
    return [int(rect.x), int(rect.y), int(rect.w), int(rect.h)]


@dataclass
class AnonResult:
    protected: bool = False          # Ekxel said the area shows protected data
    ok: bool = False                 # ... and drew the anonymized picture (image)
    image: Any = None                # QImage, exactly the area's size
    token: str = ""                  # Ekxel's id of that picture (its Claude panel checks it later)
    title: Optional[str] = None      # the captured window's title, masked by Ekxel
    rects: list = field(default_factory=list)
    error: str = ""
    answered: bool = True            # False: Ekxel didn't reply at all (busy / hung / just closed)


def ekxel_window_in(rect: Any) -> bool:
    """Cheap local pre-check before asking Ekxel: is any window of an Ekxel process inside the area? (Windows: a
    process owning a window titled '...Ekxel...'.) Elsewhere: True (just ask)."""
    try:
        from PySide6.QtGui import QGuiApplication

        if QGuiApplication.platformName() in ("offscreen", "minimal"):
            return False   # no real screen (tests, --smoke-test): nothing was captured from Ekxel
    except Exception:  # noqa: BLE001
        pass
    if sys.platform != "win32":
        return True
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")   # a private handle: the argtypes below don't touch other modules' calls
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        wins: list = []

        def cb(hwnd, _lp):
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            wins.append((pid.value, buf.value, (r.left, r.top, r.right, r.bottom)))
            return True

        proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(cb)
        user32.EnumWindows(proc, 0)
        pids = {pid for pid, title, _r in wins if "Ekxel" in title and pid != os.getpid()}
        if not pids:
            return False
        x0, y0, x1, y1 = rect.x, rect.y, rect.x + rect.w, rect.y + rect.h
        return any(pid in pids and r[0] < x1 and r[2] > x0 and r[1] < y1 and r[3] > y0 for pid, _t, r in wins)
    except Exception:  # noqa: BLE001 - unsure: ask Ekxel
        return True


def check_protection(rect: Any, timeout_ms: int = CHECK_TIMEOUT_MS) -> AnonResult:
    """Does the screen area (virtual-screen physical px) show protected Ekxel data? Not running / no answer: no."""
    reply = _rpc("protection_at", {"rect": _rect_list(rect), "ignore_pid": os.getpid()}, timeout_ms)
    if not reply:
        return AnonResult(error="Ekxel didn't answer", answered=False)
    return AnonResult(protected=bool(reply.get("protected")), rects=list(reply.get("rects") or []))


def render_anonymized(rect: Any, title: str, tmp_dir: "Path | str",
                      timeout_ms: int = RENDER_TIMEOUT_MS) -> AnonResult:
    """Ekxel's anonymized picture of the area. Ekxel writes it to a temporary PNG (only ever anonymized pixels); it is
    read into memory and deleted again. ok=False when Ekxel couldn't make it (Ekxel never sends real pixels)."""
    from PySide6.QtGui import QImage

    out = AnonResult(protected=True)
    tmp = Path(tmp_dir)
    try:
        tmp.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        out.error = f"no temporary folder: {exc}"
        return out
    path = tmp / f"anon-{uuid.uuid4().hex}.png"
    try:
        reply = _rpc("anon_shot", {"rect": _rect_list(rect), "path": str(path), "title": title or "",
                                   "ignore_pid": os.getpid()}, timeout_ms)
        if not reply or reply.get("ok") is not True:
            out.error = str((reply or {}).get("error") or "Ekxel couldn't render it")
            return out
        img = QImage(str(path))
        if img.isNull() or img.width() != rect.w or img.height() != rect.h:
            out.error = "Ekxel's picture has the wrong size"
            return out
        out.ok, out.image = True, img
        out.token = str(reply.get("token") or "")
        t = reply.get("title")
        out.title = t if isinstance(t, str) else None
        return out
    finally:
        try:
            path.unlink()
        except OSError:
            pass


class AnonJobs(QObject):
    """Runs check_protection + render_anonymized for a captured shot on a worker thread.
    checked(shot id, protected) arrives first (the editor shows 'Anonymizing...'), then done(shot id, AnonResult).
    Every collaborator can be replaced (tests / probes)."""

    checked = Signal(str, bool)
    done = Signal(str, object)

    def __init__(self, tmp_dir: Callable[[], "Path | str"], parent: Optional[QObject] = None, *,
                 precheck: Callable[[Any], bool] = ekxel_window_in,
                 check: Callable[[Any], AnonResult] = check_protection,
                 render: Callable[[Any, str, Any], AnonResult] = render_anonymized) -> None:
        super().__init__(parent)
        self._tmp_dir = tmp_dir
        self.precheck, self.check, self.render = precheck, check, render
        self._running: set = set()

    def busy(self, shot_id: Optional[str] = None) -> bool:
        return bool(self._running) if shot_id is None else shot_id in self._running

    def start(self, shot_id: str, rect: Any, title: str, *, skip_check: bool = False) -> bool:
        """False when Ekxel can't be involved (no Ekxel window in the area): the shot stays as captured.
        skip_check: the area is already known to be protected (a retry): render straight away."""
        if shot_id in self._running:
            return True
        if not skip_check:
            try:
                if not self.precheck(rect):
                    return False
            except Exception:  # noqa: BLE001 - unsure: ask
                pass
        self._running.add(shot_id)
        tmp = self._tmp_dir()

        def work() -> None:
            try:
                if skip_check:
                    prot = AnonResult(protected=True)
                else:
                    prot = self.check(rect)
                if not prot.answered:
                    # an Ekxel window is in the area but Ekxel didn't reply: it may be protected, so the shot is
                    # treated like one Ekxel couldn't anonymize (kept real, asked about before it is sent)
                    res = AnonResult(protected=True, error=prot.error or "Ekxel didn't answer")
                else:
                    if not skip_check:
                        self.checked.emit(shot_id, bool(prot.protected))
                    res = self.render(rect, title, tmp) if prot.protected else prot
            except Exception as exc:  # noqa: BLE001 - a broken job is a failed render, never a real send
                res = AnonResult(protected=True, error=f"{type(exc).__name__}: {exc}")
            self.done.emit(shot_id, res)

        threading.Thread(target=work, name=f"anon-{shot_id[:8]}", daemon=True).start()
        return True

    def finished(self, shot_id: str) -> None:
        self._running.discard(shot_id)
