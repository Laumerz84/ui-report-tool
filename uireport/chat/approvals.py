"""App side of Claude Code's permission prompt (see approve_mcp.py). Owner: chat.

ApprovalWatcher polls the approvals folder for <id>.request.json and answers with <id>.answer.json;
ApprovalCard is the "Claude wants to run a command ... [Allow] [Always allow] [Deny]" card. Files, not a
socket, keep the app local-only (tests/test_integration_local_only.py).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

POLL_MS = 150


def describe(tool: str, inp: object) -> tuple[str, str]:
    """(headline, detail) in plain words for the card."""
    inp = inp if isinstance(inp, dict) else {}
    if tool == "Bash":
        return "run a command", str(inp.get("command", ""))
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        verb = {"Write": "write the file", "NotebookEdit": "edit the notebook"}.get(tool, "edit the file")
        return verb, str(inp.get("file_path") or inp.get("notebook_path") or "")
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        return f"use {parts[2] if len(parts) > 2 else tool} ({parts[1] if len(parts) > 1 else ''})", \
            json.dumps(inp, ensure_ascii=False)[:400]
    return f"use {tool}", json.dumps(inp, ensure_ascii=False)[:400]


class ApprovalWatcher(QObject):
    request = Signal(str, str, object)  # id, tool name, tool input

    def __init__(self, folder: Path, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        for leftover in self.folder.glob("*.json"):  # requests from a previous run can't be answered
            if leftover.name.endswith((".request.json", ".answer.json")):
                try:
                    leftover.unlink()
                except OSError:
                    pass
        self._seen: set[str] = set()
        self.pending: list[str] = []
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

    def _poll(self) -> None:
        for req in sorted(self.folder.glob("*.request.json"), key=lambda p: p.stat().st_mtime if p.exists() else 0):
            rid = req.name[: -len(".request.json")]
            if rid in self._seen:
                continue
            try:
                data = json.loads(req.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue  # half-written: next poll
            self._seen.add(rid)
            self.pending.append(rid)
            self.request.emit(rid, str(data.get("tool_name", "")), data.get("input") or {})

    def answer(self, rid: str, allow: bool, message: str = "") -> None:
        if rid in self.pending:
            self.pending.remove(rid)
        target = self.folder / f"{rid}.answer.json"
        tmp = target.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps({"behavior": "allow" if allow else "deny", "message": message}),
                           encoding="utf-8")
            os.replace(tmp, target)
        except OSError:
            pass  # the server times out and denies

    def deny_all(self, message: str) -> None:
        for rid in list(self.pending):
            self.answer(rid, False, message)


class ApprovalCard(QFrame):
    answered = Signal(str)  # "allow" | "always" | "deny"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("approvalCard")
        self.setStyleSheet("#approvalCard { background: #FFF4CE; border: 1px solid #E0C46C; border-radius: 6px; }"
                           "#approvalCard QLabel { color: #3B2F00; }")
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 8, 10, 8)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.RichText)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.label)
        row = QHBoxLayout()
        row.addStretch(1)
        self.allow_btn, self.always_btn, self.deny_btn = QPushButton("Allow"), QPushButton("Always allow"), QPushButton("Deny")
        for btn, what in ((self.allow_btn, "allow"), (self.always_btn, "always"), (self.deny_btn, "deny")):
            btn.clicked.connect(lambda _=False, w=what: self.answered.emit(w))
            row.addWidget(btn)
        v.addLayout(row)
        self.hide()

    def show_request(self, tool: str, inp: object, waiting: int = 0) -> None:
        def esc(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        head, detail = describe(tool, inp)
        more = f" <span style='color:#7A6A2A'>(+{waiting} more waiting)</span>" if waiting else ""
        body = f"<pre style='white-space:pre-wrap;margin:4px 0'>{esc(detail)[:1500]}</pre>" if detail else ""
        self.label.setText(f"<b>Claude wants to {esc(head)}</b>{more}{body}")
        self.always_btn.setText(f"Always allow {tool.split('__')[-1]}")
        self.always_btn.setToolTip("Allow this tool for the rest of this chat without asking")
        self.show()
