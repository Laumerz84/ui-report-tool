"""End-to-end checks for the Show Claude chat: real capture overlay, real screen, real Claude Code.

    .venv\\Scripts\\python.exe "G:\\UI Report Tool\\e2e\\show_claude.py"     (absolute path: the folder guess reads it)

Uses your Claude Code for a few short turns and leaves those conversations in ~/.claude/projects.
1. hotkey -> answer: the chat capture route (AppController.request_chat_capture) over a window that
   reads PURPLE GIRAFFE 42; Claude's reply must name it, and the folder guess must pick this repo.
2. follow-up + 2nd shot: a second capture (ORANGE KOALA 7) and a typed question in the same chat;
   Claude must list both animals in order, in one conversation.
3. continue a main-app chat: a Claude app Code-tab session (not touched for an hour) opens from
   History as a copy; the reply arrives, the original file is byte-for-byte unchanged, and the chat
   now has a different session id.
"""
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.path.abspath(__file__)).parent.parent
sys.path.insert(0, str(ROOT))
os.environ["UIREPORT_NO_PASTE"] = "1"

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication(sys.argv)

from uireport.app.controller import AppController  # noqa: E402
from uireport.chat import sessions  # noqa: E402
from uireport.settings import SettingsStore  # noqa: E402

OUT = ROOT / ".test-output" / "e2e-show-claude"
TURN_TIMEOUT = 240
results: list[tuple[str, bool, str]] = []
signs: list = []


def pump(seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def wait(cond, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def sign(text: str, x: int):
    """A separate process showing `text`; returns (process, (left, top, right, bottom))."""
    proc = subprocess.Popen([sys.executable, str(ROOT / "e2e" / "_sign.py"), text, str(x)],
                            stdout=subprocess.PIPE, text=True)
    signs.append(proc)
    rect = json.loads(proc.stdout.readline())
    pump(0.5)  # painted before the screen freezes
    return proc, rect


def capture_over(ctrl: AppController, rect) -> None:
    ctrl.request_chat_capture()
    sel = ctrl.capture._selector
    assert sel is not None, "the capture overlay did not open"
    ov = sel.overlays[0]
    origin = ov.geometry().topLeft()
    left_, top, right, bottom = rect
    x0, y0 = left_ - origin.x() + 4, top - origin.y() + 4
    x1, y1 = right - origin.x() - 4, bottom - origin.y() - 4

    def send(kind, x, y, button, buttons):
        QApplication.sendEvent(ov, QMouseEvent(kind, QPointF(x, y), QPointF(x, y), button, buttons,
                                               Qt.KeyboardModifier.NoModifier))

    left = Qt.MouseButton.LeftButton
    send(QEvent.Type.MouseButtonPress, x0, y0, left, left)
    send(QEvent.Type.MouseMove, x1, y1, Qt.MouseButton.NoButton, left)
    send(QEvent.Type.MouseButtonRelease, x1, y1, left, Qt.MouseButton.NoButton)


def last_reply(chat) -> str:
    return chat.md.rsplit("**You:**", 1)[-1]


def run_turn(chat, done: list, start) -> bool:
    before = len(done)
    start()
    return wait(lambda: len(done) > before and not chat.runner.busy, TURN_TIMEOUT)


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + " - " + detail, flush=True)


store = SettingsStore(OUT / "settings.json")
store.settings.output_root = str(OUT / "reports")
ctrl = AppController(store, enable_hotkeys=False)
chat = ctrl.chat_window()
done: list = []
chat.runner.finished.connect(lambda ok, msg: done.append((ok, msg)))
try:
    # 1. hotkey -> answer
    one, rect1 = sign("PURPLE GIRAFFE 42", 300)
    ok = run_turn(chat, done, lambda: capture_over(ctrl, rect1))
    reply = last_reply(chat).lower()
    check("1 hotkey -> answer", ok and done[-1][0] and "giraffe" in reply and "42" in reply,
          f"folder={chat.folder}; reply={reply.strip()[:160]!r}")
    check("1b folder guess", os.path.normcase(chat.folder) == os.path.normcase(str(ROOT)), chat.folder)
    first_session = chat.session_id
    one.kill()

    # 2. follow-up + second shot, same conversation
    two, rect2 = sign("ORANGE KOALA 7", 900)
    ok2 = run_turn(chat, done, lambda: capture_over(ctrl, rect2))
    two.kill()
    chat.input.setPlainText("Which two animals were in my two screenshots, in order? Answer with just the two names.")
    ok3 = run_turn(chat, done, chat._send_typed)
    reply = last_reply(chat).lower()
    in_order = "giraffe" in reply and "koala" in reply and reply.index("giraffe") < reply.index("koala")
    check("2 follow-up + 2nd shot", ok2 and ok3 and in_order,
          f"sessions {first_session} -> {chat.session_id}; reply={reply.strip()[:160]!r}")

    # 3. continue a Claude app Code-tab chat as a copy
    def is_app_session(info) -> bool:
        try:
            with open(info.path, "rb") as fh:
                return b'"bridge-session"' in fh.read(256 * 1024)
        except OSError:
            return False

    hour_ago = time.time() - 3600
    pick = next((s for s in sessions.list_sessions(60)
                 if s.modified < hour_ago and s.cwd and os.path.isdir(s.cwd) and is_app_session(s)), None)
    if pick is None:
        check("3 continue main-app chat", False, "skipped: no Claude app session older than an hour with an existing folder")
    else:
        before_hash = hashlib.sha256(pick.path.read_bytes()).hexdigest()
        chat.new_chat()
        chat.open_session(pick)
        chat.input.setPlainText("Start your reply with COPY-OK, then say in one short sentence what this conversation was about.")
        ok4 = run_turn(chat, done, chat._send_typed)
        reply = last_reply(chat)
        same = hashlib.sha256(pick.path.read_bytes()).hexdigest() == before_hash
        check("3 continue main-app chat", ok4 and "COPY-OK" in reply and same and chat.session_id not in (None, pick.id),
              f"'{pick.title}' ({pick.id[:8]}) -> copy {str(chat.session_id)[:8]}; original unchanged={same}; "
              f"reply={reply.strip()[:140]!r}")
finally:
    for proc in signs:
        proc.kill()
    chat.shutdown()
    ctrl.shutdown()
    pump(0.3)

print(f"\n{sum(ok for _, ok, _ in results)}/{len(results)} passed")
sys.exit(0 if all(ok for _, ok, _ in results) else 1)
