"""Send a finished report to Ekxel's built-in Claude panel instead of the Claude desktop app, when an
Ekxel window was in the foreground more recently than Claude's.

FocusTracker polls the foreground window (cheap, every 0.4 s) and remembers when an Ekxel window and a
Claude desktop window were last active. send_to_ekxel() hands the prompt and the report folder to the
running Ekxel over its single-instance local socket ("SheetsSpreadsheetApp-<user>"); Ekxel opens its
Claude panel and sends the prompt with the screenshots attached (see Ekxel's sheets/ai/inbox.py)."""
from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from typing import Optional

from PySide6.QtCore import QObject, QTimer

POLL_MS = 400
NL, NLB = chr(10), bytes([10])   # Ekxel's socket protocol: one JSON object per line


def ekxel_server_name() -> str:
    return "SheetsSpreadsheetApp-" + (os.environ.get("USERNAME") or os.environ.get("USER") or "user")


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
