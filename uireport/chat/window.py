"""The Show Claude chat window. Owner: chat.

A screenshot from the chat hotkey (or typed text) goes straight to the user's Claude Code through
ClaudeRun; the reply streams into a markdown transcript with image thumbnails. The header picks the
project folder, starts a new chat, reopens a past conversation (History: this window's chats and the
Claude app's Code-tab sessions; those continue as a copy) and stops a long answer. Approval cards ask
before edits and commands. Closing only hides the window: the conversation stays.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QImage, QKeyEvent, QTextCursor
from PySide6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout, QLabel, QMenu, QPlainTextEdit,
                               QPushButton, QTextBrowser, QToolButton, QVBoxLayout, QWidget)

from ..app.icons import make_app_icon
from . import folder_guess, sessions
from .approvals import ApprovalCard, ApprovalWatcher
from .runner import ClaudeRun, png_bytes

THUMB_WIDTH = 300
RENDER_MS = 50
BROWSE = "__browse__"


def _md_escape(text: str) -> str:
    return re.sub(r"([\\`*_#\[\]<>|])", r"\\\1", text)


def _age(ts: float) -> str:
    mins = max(0, int((time.time() - ts) / 60))
    if mins < 60:
        return f"{mins} min ago"
    if mins < 48 * 60:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} d ago"


class _Transcript(QTextBrowser):
    """Markdown transcript whose `shot:N` images come from memory."""

    def __init__(self) -> None:
        super().__init__()
        self.images: dict[str, QImage] = {}
        self.setOpenExternalLinks(True)

    def loadResource(self, rtype: int, url):  # noqa: N802 (Qt naming)
        image = self.images.get(url.toString())
        return image if image is not None else super().loadResource(rtype, url)


class _Input(QPlainTextEdit):
    submit = Signal()

    def keyPressEvent(self, ev: QKeyEvent) -> None:  # noqa: N802
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (ev.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.submit.emit()
            return
        super().keyPressEvent(ev)


class ChatWindow(QWidget):
    def __init__(self, store: Any, chat_dir: Path, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, Qt.WindowType.Window | Qt.WindowType.WindowStaysOnTopHint)
        self.store = store
        self.setWindowTitle("Show Claude")
        self.setWindowIcon(make_app_icon())
        self.resize(580, 780)
        chat_dir = Path(chat_dir)
        self.own = sessions.OwnSessions(chat_dir / "own_sessions.json")
        self.runner = ClaudeRun(chat_dir / "approvals", self)
        self.watcher = ApprovalWatcher(chat_dir / "approvals", self)
        self.session_id: Optional[str] = None
        self.fork_next = False
        last = getattr(store.settings, "chat_last_folder", "") or ""
        self.folder = last if os.path.isdir(last) else str(Path.home())
        self.md = ""
        self.live = ""
        self.queue: list[tuple[str, list[QImage], str]] = []
        self.always: set[str] = set()
        self.approvals: list[tuple[str, str, object]] = []
        self._shots = 0

        # -- header
        self.folder_box = QComboBox()
        self.folder_box.setToolTip("The project folder Claude works in. Changing it starts a new chat.")
        self.folder_box.setMinimumContentsLength(24)
        self.folder_box.activated.connect(self._on_folder_picked)
        self.new_btn = QPushButton("New chat")
        self.new_btn.clicked.connect(self.new_chat)
        self.history_btn = QToolButton()
        self.history_btn.setText("History")
        self.history_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.history_menu = QMenu(self)
        self.history_menu.aboutToShow.connect(self._fill_history)
        self.history_btn.setMenu(self.history_menu)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.runner.stop)
        head = QHBoxLayout()
        head.addWidget(self.folder_box, 1)
        for w in (self.new_btn, self.history_btn, self.stop_btn):
            head.addWidget(w)

        # -- transcript, approvals, status, input
        self.view = _Transcript()
        self.card = ApprovalCard()
        self.card.answered.connect(self._on_card)
        self.status = QLabel()
        self.status.setStyleSheet("color: #6B6B6B; font-size: 11px;")
        self.input = _Input()
        self.input.setPlaceholderText("Ask Claude... (Enter sends, Shift+Enter new line; "
                                      "type first, then press the Show Claude hotkey to send both)")
        self.input.setFixedHeight(84)
        self.input.submit.connect(self._send_typed)
        self.send_btn = QPushButton("Send")
        self.send_btn.clicked.connect(self._send_typed)
        row = QHBoxLayout()
        row.addWidget(self.input, 1)
        row.addWidget(self.send_btn, 0, Qt.AlignmentFlag.AlignBottom)
        root = QVBoxLayout(self)
        root.addLayout(head)
        root.addWidget(self.view, 1)
        root.addWidget(self.card)
        root.addWidget(self.status)
        root.addLayout(row)

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(RENDER_MS)
        self._render_timer.timeout.connect(self._render)
        self.runner.text_delta.connect(self._on_delta)
        self.runner.tool.connect(self._on_tool)
        self.runner.session.connect(self._on_session)
        self.runner.status.connect(self.status.setText)
        self.runner.finished.connect(self._on_finished)
        self.watcher.request.connect(self._on_approval)
        self._fill_folders()

    # ---- public ------------------------------------------------------------------------------
    def send_screenshot(self, image: QImage, window: Any = None) -> None:
        """From the chat hotkey: show the window and send the screenshot (with any typed text)."""
        if self.session_id is None and not self.runner.busy and not self.md:
            known = folder_guess.known_folders(s.cwd for s in sessions.list_sessions(30))
            self._set_folder(folder_guess.guess(window, known, self.folder), announce=False)
        where = ""
        if window is not None:
            app = os.path.basename(getattr(window, "exe_path", "") or "")
            where = f"[Screenshot: {getattr(window, 'title', '') or 'untitled window'} - {app or 'unknown app'}]"
        typed = self.input.toPlainText().strip()
        self.input.clear()
        text = "\n".join(t for t in (where or "[Screenshot]", typed) if t)
        self._present()
        self._send(text, [image], shown=typed)

    def open_session(self, info: sessions.SessionInfo) -> None:
        if self.runner.busy:
            self.status.setText("Claude is still answering - press Stop first.")
            return
        if not info.cwd or not os.path.isdir(info.cwd):
            self._note("This conversation's folder no longer exists, so it can't be continued.")
            return
        self._reset()
        self.session_id = info.id
        self.fork_next = info.id not in self.own
        self._set_folder(info.cwd, announce=False)
        how = " as a copy (the original stays as it is)" if self.fork_next else ""
        self.md = f"*Continuing **{_md_escape(info.title)}**{how}.*\n\n"
        for role, text in sessions.load_messages(info.path, 20):
            text = text if len(text) < 1500 else text[:1500] + " ..."
            self.md += ("---\n\n**You:** " + _md_escape(text) if role == "user" else text) + "\n\n"
        self._render()
        self._present()

    def new_chat(self) -> None:
        if self.runner.busy:
            self.runner.stop()
        self._reset()
        self._render()
        self.status.setText("New chat.")

    def shutdown(self) -> None:
        self.runner.stop()
        self.watcher.deny_all("The Show Claude window closed.")

    # ---- sending -----------------------------------------------------------------------------
    def _send_typed(self) -> None:
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.input.clear()
        self._send(text, [], shown=text)

    def _send(self, text: str, images: list[QImage], shown: str) -> None:
        if self.runner.busy:
            self.queue.append((text, images, shown))
            self.status.setText("Claude is still answering - this goes in next.")
            return
        self.md += "---\n\n**You:** " + (_md_escape(shown) if shown else "") + "\n\n"
        for image in images:
            self._shots += 1
            key = f"shot:{self._shots}"
            self.view.images[key] = image.scaledToWidth(min(THUMB_WIDTH, image.width()),
                                                        Qt.TransformationMode.SmoothTransformation)
            self.md += f"![screenshot]({key})\n\n"
        self._render()
        error = self.runner.start(self.folder, text, [png_bytes(i) for i in images],
                                  session_id=self.session_id, fork=self.fork_next)
        if error:
            self._note(f"**Couldn't send:** {_md_escape(error)}")
            return
        self.stop_btn.setEnabled(True)
        try:
            self.store.settings.chat_last_folder = self.folder
            self.store.save()
        except Exception:  # noqa: BLE001 - remembering the folder is a nicety
            pass

    # ---- runner events -----------------------------------------------------------------------
    def _on_delta(self, text: str) -> None:
        self.live += text
        self._render_timer.start()

    def _flush(self) -> None:
        if self.live.strip():
            self.md += self.live.rstrip() + "\n\n"
        self.live = ""

    def _on_tool(self, line: str) -> None:
        self._flush()
        self.md += f"*▸ {_md_escape(line)}*\n\n"
        self._render_timer.start()

    def _on_session(self, sid: str) -> None:
        self.session_id = sid
        self.fork_next = False  # after a fork the new id is this window's own conversation
        self.own.add(sid)

    def _on_finished(self, ok: bool, message: str) -> None:
        self._flush()
        if message:
            self.md += f"**Claude stopped:** {_md_escape(message)}\n\n"
        self.stop_btn.setEnabled(False)
        self.watcher.deny_all("Claude finished this turn.")
        self.approvals.clear()
        self.card.hide()
        self._render()
        if self.queue:
            text, images, shown = self.queue.pop(0)
            self._send(text, images, shown=shown)

    # ---- approvals ---------------------------------------------------------------------------
    def _on_approval(self, rid: str, tool: str, inp: object) -> None:
        if tool in self.always:
            self.watcher.answer(rid, True)
            return
        self.approvals.append((rid, tool, inp))
        if len(self.approvals) == 1:
            self._present()
        self._show_card()

    def _show_card(self) -> None:
        if not self.approvals:
            self.card.hide()
            return
        _rid, tool, inp = self.approvals[0]
        self.card.show_request(tool, inp, waiting=len(self.approvals) - 1)

    def _on_card(self, what: str) -> None:
        if not self.approvals:
            return
        rid, tool, _inp = self.approvals.pop(0)
        if what == "always":
            self.always.add(tool)
        self.watcher.answer(rid, what != "deny", "" if what != "deny" else "The user denied this.")
        for other in [a for a in self.approvals if a[1] in self.always]:
            self.approvals.remove(other)
            self.watcher.answer(other[0], True)
        self._show_card()

    # ---- folder ------------------------------------------------------------------------------
    def _fill_folders(self) -> None:
        known = folder_guess.known_folders(s.cwd for s in sessions.list_sessions(30))
        self.folder_box.clear()
        for folder in [self.folder] + [k for k in known if k.lower() != self.folder.lower()]:
            self.folder_box.addItem(folder, folder)
        self.folder_box.addItem("Browse...", BROWSE)
        self.folder_box.setCurrentIndex(0)

    def _set_folder(self, folder: str, announce: bool) -> None:
        if os.path.normcase(folder) == os.path.normcase(self.folder):
            return
        if announce and (self.session_id or self.md):
            self.new_chat()
            self._note(f"Now working in {_md_escape(folder)} - that starts a new chat.")
        self.folder = folder
        self._fill_folders()

    def _on_folder_picked(self, index: int) -> None:
        data = self.folder_box.itemData(index)
        if data == BROWSE:
            picked = QFileDialog.getExistingDirectory(self, "Folder for Claude to work in", self.folder)
            if picked:
                self._set_folder(os.path.normpath(picked), announce=True)
            else:
                self._fill_folders()
        elif data:
            self._set_folder(str(data), announce=True)

    # ---- history -----------------------------------------------------------------------------
    def _fill_history(self) -> None:
        self.history_menu.clear()
        for info in sessions.list_sessions(25):
            mine = " (this window)" if info.id in self.own else ""
            label = f"{info.title[:60]}  -  {os.path.basename(info.cwd) or info.cwd}  -  {_age(info.modified)}{mine}"
            self.history_menu.addAction(label.replace("&", "&&"), lambda i=info: self.open_session(i))
        if self.history_menu.isEmpty():
            self.history_menu.addAction("No conversations yet").setEnabled(False)

    # ---- helpers -----------------------------------------------------------------------------
    def _reset(self) -> None:
        self.session_id, self.fork_next = None, False
        self.md, self.live = "", ""
        self.queue.clear()
        self.always.clear()
        self.watcher.deny_all("The user started a new chat.")
        self.approvals.clear()
        self.card.hide()
        self.view.images.clear()

    def _note(self, text: str) -> None:
        self.md += f"*{text}*\n\n"
        self._render()

    def _render(self) -> None:
        self.view.setMarkdown(self.md + self.live)
        self.view.moveCursor(QTextCursor.MoveOperation.End)
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _present(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()

    def closeEvent(self, ev: QCloseEvent) -> None:  # noqa: N802 - closing only hides; the chat stays
        ev.ignore()
        self.hide()
