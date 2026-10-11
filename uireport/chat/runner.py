"""Runs one turn of a Show Claude conversation with the user's Claude Code (`claude -p`, stream-json in
and out) and streams the reply back as Qt signals. Adapted from Ekxel's Claude panel
(G:\\Sheets\\sheets\\ai\\panel.py). Owner: chat.

Text and screenshots go in as one stream-json user message (images as base64 PNG content blocks), so
Claude sees them at once. Read-only tools run directly; anything else is asked through the approval
MCP server (approve_mcp.py) and the card in the chat window.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional, Sequence

from PySide6.QtCore import QBuffer, QIODevice, QObject, Qt, Signal
from PySide6.QtGui import QImage

MAX_SIDE = 1568  # longest image edge sent (the API scales larger images down anyway)
READ_ONLY_TOOLS = ("Read", "Glob", "Grep", "WebSearch", "WebFetch", "TodoWrite")
APPROVE_TOOL = "mcp__uireport__approve"
APPROVE_SCRIPT = Path(__file__).with_name("approve_mcp.py")
CREATE_NO_WINDOW = 0x08000000

SYSTEM_NOTE = (
    "You are Claude in the user's 'Show Claude' window (part of their UI Report Tool). The user sends "
    "screenshots of their screen straight from a hotkey, often with no text: look at the screenshot, say "
    "briefly what you see that matters, and help with what they're doing, or ask what they need. A line like "
    "[Screenshot: <window title> - <app>] says where it was taken. You are working in the folder {cwd}. "
    "Reading and searching run directly; editing files, running commands and other actions show the user an "
    "Allow/Deny card first, so say in one line what you're about to do and why. Keep replies short and plain."
)


def find_claude() -> Optional[list[str]]:
    """Command (list) that runs Claude Code. UIREPORT_CLAUDE_CMD (JSON list) overrides it."""
    override = os.environ.get("UIREPORT_CLAUDE_CMD")
    if override:
        return json.loads(override)
    exe = shutil.which("claude")
    if not exe:
        cand = Path.home() / ".local" / "bin" / "claude.exe"
        exe = str(cand) if cand.exists() else None
    return [exe] if exe else None


def console_python() -> str:
    """python.exe (not pythonw) so the approval server has working stdio."""
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "python.exe")
    return cand if os.path.exists(cand) else exe


def png_bytes(image: QImage) -> bytes:
    """The screenshot as PNG, longest side at most MAX_SIDE."""
    if max(image.width(), image.height()) > MAX_SIDE:
        image = image.scaled(MAX_SIDE, MAX_SIDE, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    return bytes(buf.data())


def _describe(name: str, inp: dict) -> str:
    """One short line for a tool Claude is using."""
    inp = inp if isinstance(inp, dict) else {}
    detail = (inp.get("file_path") or inp.get("path") or inp.get("pattern") or inp.get("command")
              or inp.get("url") or inp.get("query") or inp.get("description") or "")
    detail = str(detail).replace("\n", " ")
    return f"{name}: {detail[:120]}" if detail else name


class ClaudeRun(QObject):
    """One turn at a time. `start()` returns an error message if Claude couldn't be started."""

    text_delta = Signal(str)  # streamed reply text
    tool = Signal(str)  # "Read: G:\\x\\y.py" - a tool Claude is using
    session = Signal(str)  # the conversation's session id (new id after a fork)
    status = Signal(str)
    finished = Signal(bool, str)  # ok, error message ("" when ok)
    _line = Signal(int, str)
    _done = Signal(int, object, str)

    def __init__(self, approval_dir: Path, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.approval_dir = Path(approval_dir)
        self.proc: Optional[subprocess.Popen] = None
        self._gen = 0
        self._cfg: Optional[Path] = None
        self._streamed = False
        self._result_error = ""
        self._got_result = False
        self._stopped = False
        self._line.connect(self._on_line, Qt.ConnectionType.QueuedConnection)
        self._done.connect(self._on_done, Qt.ConnectionType.QueuedConnection)

    @property
    def busy(self) -> bool:
        return self.proc is not None

    def start(self, folder: str, text: str, images: Sequence[bytes] = (),
              session_id: Optional[str] = None, fork: bool = False) -> Optional[str]:
        if self.busy:
            return "Claude is still answering."
        cmd = find_claude()
        if not cmd or not cmd[0]:
            return ("Claude Code isn't installed (couldn't find `claude`). Install it, sign in once in a "
                    "terminal with `claude`, then try again.")
        if not os.path.isdir(folder):
            return f"The folder {folder} doesn't exist."
        self._gen += 1
        try:
            self.approval_dir.mkdir(parents=True, exist_ok=True)
            self._cfg = self.approval_dir / f"mcp-{os.getpid()}-{self._gen}.json"
            self._cfg.write_text(json.dumps({"mcpServers": {"uireport": {
                "type": "stdio", "command": console_python(), "args": [str(APPROVE_SCRIPT)],
                "env": {"UIREPORT_APPROVAL_DIR": str(self.approval_dir), "PYTHONUTF8": "1"}}}}),
                encoding="utf-8")
        except OSError as exc:
            return f"Couldn't prepare the approval step: {exc}"
        args = cmd + ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                      "--include-partial-messages", "--mcp-config", str(self._cfg),
                      "--permission-prompt-tool", APPROVE_TOOL,
                      "--allowedTools", *READ_ONLY_TOOLS,
                      "--append-system-prompt", SYSTEM_NOTE.format(cwd=folder)]
        if session_id:
            args += ["--resume", session_id] + (["--fork-session"] if fork else [])
        content: list[dict] = [{"type": "text", "text": text}]
        for png in images:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                        "data": base64.b64encode(png).decode("ascii")}})
        message = (json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n").encode("utf-8")
        try:
            self.proc = subprocess.Popen(args, cwd=folder, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
        except OSError as exc:
            self.proc = None
            return f"Couldn't start Claude Code: {exc}"
        self._streamed, self._result_error, self._got_result, self._stopped = False, "", False, False
        self.status.emit("Starting Claude...")
        gen, proc = self._gen, self.proc

        def feed() -> None:
            try:
                proc.stdin.write(message)
                proc.stdin.close()
            except OSError:
                pass

        def pump() -> None:
            err = b""
            # The message (with screenshots: hundreds of KB) goes in on its own thread while stdout is
            # read here from the start. Writing it all first deadlocks: Claude prints its startup
            # events (hooks, init) before it has read all of stdin, fills the stdout pipe and waits
            # for us, while we wait for it to take the rest of stdin.
            threading.Thread(target=feed, name="show-claude-feed", daemon=True).start()

            def drain() -> None:
                nonlocal err
                err = proc.stderr.read()

            t = threading.Thread(target=drain, daemon=True)
            t.start()
            for raw in proc.stdout:
                self._line.emit(gen, raw.decode("utf-8", "replace"))
            rc = proc.wait()
            t.join(2)
            self._done.emit(gen, rc, err.decode("utf-8", "replace")[-2000:])

        threading.Thread(target=pump, name="show-claude-run", daemon=True).start()
        return None

    def stop(self) -> None:
        if self.proc is None:
            return
        self._stopped = True
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], capture_output=True,
                       creationflags=CREATE_NO_WINDOW)
        self.status.emit("Stopped.")

    # ---- stream-json events -------------------------------------------------------------------
    def _on_line(self, gen: int, line: str) -> None:
        if gen != self._gen or self.proc is None or self._stopped:
            return  # a stopped run's queued lines must not leak into the next chat
        try:
            ev = json.loads(line)
        except ValueError:
            return
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            if ev.get("session_id"):
                self.session.emit(ev["session_id"])
            self.status.emit("Thinking...")
        elif t == "stream_event":
            e = ev.get("event", {})
            if e.get("type") == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta":
                self._streamed = True
                self.text_delta.emit(e["delta"].get("text", ""))
            elif e.get("type") == "message_start":
                self._streamed = False
        elif t == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "text" and not self._streamed:
                    self.text_delta.emit(block.get("text", ""))
                elif block.get("type") == "tool_use" and block.get("name") != APPROVE_TOOL:
                    self.tool.emit(_describe(block.get("name", ""), block.get("input", {})))
                    self.status.emit(f"Working: {block.get('name', '')}...")
            self._streamed = False
        elif t == "result":
            self._got_result = True
            if ev.get("session_id"):
                self.session.emit(ev["session_id"])
            if ev.get("is_error") or ev.get("subtype") != "success":
                self._result_error = str(ev.get("result") or ev.get("subtype") or "error")[:600]
            secs = (ev.get("duration_ms") or 0) / 1000
            self.status.emit(f"Done in {secs:.1f}s." if secs else "Done.")

    def _on_done(self, gen: int, rc: object, err: str) -> None:
        if gen != self._gen:
            return
        self.proc = None
        if self._cfg is not None:
            try:
                self._cfg.unlink()
            except OSError:
                pass
            self._cfg = None
        message = "" if self._stopped else self._result_error
        if not message and not self._stopped and rc not in (0, None) and not self._got_result:
            message = err.strip().splitlines()[-1] if err.strip() else f"Claude Code exited with code {rc}."
            if "login" in err.lower() or "auth" in err.lower():
                message += "  (Open a terminal, run `claude`, and sign in once.)"
        self.finished.emit(not message, message)
