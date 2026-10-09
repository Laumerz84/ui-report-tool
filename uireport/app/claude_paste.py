"""Paste the Finish message into the Claude desktop app when it is open. Owner: package C.

This is the one place in the app that sends synthetic keyboard input, and it is deliberately
narrow:
  * only if the Claude desktop app already has a window open (it is never started);
  * only after that window is verified to be the foreground window, checked again right
    before the keys go out;
  * only into Claude's message box: UI Automation puts the cursor there first (a Ctrl+V while
    the user had clicked the conversation would otherwise vanish), and afterwards the box is
    read back to confirm the text arrived - "pasted" is never claimed unverified if it can be read;
  * only Ctrl+V, never Enter, so the user reviews the message and sends it themselves;
  * never while the Finish shortcut's Ctrl/Shift/Alt/Win keys are still held down.
Waiting keeps the Qt event loop running: Qt hands clipboard text over on request, so the app
must stay responsive while Claude reads it.
Disabled when UIREPORT_NO_PASTE=1 (the test run, --smoke-test) or under the offscreen platform.
"""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Optional

RESULT_PASTED = "pasted"
RESULT_NOT_OPEN = "not_open"  # Claude isn't running: the message is only copied
RESULT_NOT_FOCUSED = "not_focused"  # Claude didn't come to the front (or keys stayed held): nothing typed
RESULT_NO_PROMPT = "no_prompt"  # Claude's message box wasn't found (another screen is open): nothing typed
RESULT_NOT_LANDED = "not_landed"  # Ctrl+V went out but the text didn't show up in the box
RESULT_DISABLED = "disabled"

BLOCK_ENV = "UIREPORT_NO_PASTE"
ELECTRON_CLASS = "Chrome_WidgetWin_1"  # the desktop app's top-level window; the CLI never owns one
PROMPT_NAME = "Prompt"  # accessible name of Claude's message box (a ProseMirror editor)
STEP_S = 0.01
FOCUS_WAIT_STEPS, MODIFIER_WAIT_STEPS, VERIFY_STEPS = 100, 200, 150  # up to 1 s, 2 s, 1.5 s
CARET_SETTLE_S = 0.02  # Chromium moves the caret a frame after the accessibility SetFocus
VERIFY_CHARS = 40  # the start of the text that must newly appear in the box


@dataclass
class Window:
    hwnd: int
    exe: str
    title: str
    class_name: str
    visible: bool
    iconic: bool


@dataclass
class Pane:
    """One of a Claude window's chat panes (the app shows up to two side by side)."""
    hwnd: int
    name: str  # accessible name of the pane group: "Primary pane" / "Secondary pane"
    title: str  # the chat shown in it ("" when it isn't showing one)
    left: int  # screen x, to call them left / right

    @property
    def key(self) -> tuple:
        return (self.hwnd, self.name)


@dataclass
class PasteDeps:
    windows: Callable[[], list]  # top-level windows, z-order (topmost first)
    restore: Callable[[int], None]
    focus: Callable[[int], bool]
    foreground: Callable[[], int]
    modifiers_down: Callable[[], bool]
    send_ctrl_v: Callable[[], bool]
    sleep: Callable[[float], None]
    # both take a window hwnd (its first message box) or a Pane.key (that pane's box)
    focus_prompt: Callable[[object], bool]  # cursor into Claude's message box; False if there is none
    prompt_text: Callable[[object], Optional[str]]  # the box's text, None if it can't be read


def is_claude_desktop(w: Window) -> bool:
    """The Claude desktop app's main window (Store or regular install), not the Claude Code CLI."""
    return (os.path.basename(w.exe).lower() == "claude.exe" and w.class_name == ELECTRON_CLASS
            and w.visible and bool(w.title.strip()))


def _blocked() -> bool:
    return os.environ.get(BLOCK_ENV) == "1" or os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen"


def prewarm(pane: Optional[Pane] = None) -> None:
    """Find Claude's message box ahead of time (called while the report is being written), so
    the paste itself doesn't have to search Claude's accessibility tree."""
    if _blocked():
        return
    try:
        if pane is not None:
            _BOX.control(pane.key)
            return
        target = next((w for w in _windows() if is_claude_desktop(w)), None)
        if target is not None:
            _BOX.control(target.hwnd)
    except Exception:  # noqa: BLE001 - a warm-up must never break Finish
        pass


PANE_SUFFIX = " pane"  # the pane groups are named "Primary pane" / "Secondary pane"
TITLE_SUFFIX = ", rename session"  # each pane's chat-title button: "<title>, rename session"


def list_panes() -> list:
    """The chat panes of every open Claude desktop window (topmost window first, left to right
    within a window). Read-only: nothing is focused or clicked. [] if Claude isn't open."""
    if _blocked():
        return []
    try:
        import uiautomation as auto
    except Exception:  # noqa: BLE001
        return []
    out = []
    for w in _windows():
        if not is_claude_desktop(w):
            continue
        try:
            root = auto.ControlFromHandle(w.hwnd)
            found = [c for c, _d in auto.WalkControl(root, maxDepth=22)
                     if c.ControlTypeName == "GroupControl" and (c.Name or "").endswith(PANE_SUFFIX)]
            panes = []
            for group in found:
                btn = group.ButtonControl(searchDepth=20, Compare=lambda c, _d: (c.Name or "").endswith(TITLE_SUFFIX))
                title = btn.Name[: -len(TITLE_SUFFIX)] if btn.Exists(0.3, 0.05) else ""
                panes.append(Pane(hwnd=w.hwnd, name=group.Name, title=title, left=group.BoundingRectangle.left))
            out.extend(sorted(panes, key=lambda p: p.left))
        except Exception:  # noqa: BLE001 - an unreadable window just isn't offered
            continue
    return out


def pane_labels(panes: list) -> list:
    """Menu text for each pane: 'Left: <chat>' / 'Right: <chat>', 'Window 2 ...' when there are several."""
    hwnds = list(dict.fromkeys(p.hwnd for p in panes))
    labels = []
    for p in panes:
        same = [q for q in panes if q.hwnd == p.hwnd]
        side = "" if len(same) == 1 else ("Left" if p is same[0] else "Right" if p is same[-1] else "Middle")
        where = (f"Window {hwnds.index(p.hwnd) + 1} " if len(hwnds) > 1 else "") + side
        where = where.strip() or "Claude"
        labels.append(f"{where}: {p.title or '(no chat open)'}")
    return labels


def _norm(text: str) -> str:
    return " ".join(str(text).split())  # the message box turns newlines into paragraphs


def paste_into_claude(text: str, deps: Optional[PasteDeps] = None, pane: Optional[Pane] = None) -> str:
    """Bring an open Claude desktop window forward, put the cursor in its message box (or in
    `pane`'s, when given) and press Ctrl+V once (`text` is what is on the clipboard).
    Returns a RESULT_*."""
    if deps is None:
        if _blocked():
            return RESULT_DISABLED
        deps = default_deps()
    claude = [w for w in deps.windows() if is_claude_desktop(w)]
    if pane is not None:
        claude = [w for w in claude if w.hwnd == pane.hwnd]
    target = claude[0] if claude else None
    if target is None:
        return RESULT_NOT_OPEN
    box = pane.key if pane is not None else target.hwnd
    if target.iconic:
        deps.restore(target.hwnd)
    deps.focus(target.hwnd)
    for _ in range(FOCUS_WAIT_STEPS):
        if deps.foreground() == target.hwnd:
            break
        deps.sleep(STEP_S)
    else:
        return RESULT_NOT_FOCUSED
    for _ in range(MODIFIER_WAIT_STEPS):
        if not deps.modifiers_down():
            break
        deps.sleep(STEP_S)
    else:
        return RESULT_NOT_FOCUSED  # Ctrl+V plus a held Shift/Alt would be a different command
    if not deps.focus_prompt(box):
        return RESULT_NO_PROMPT
    deps.sleep(CARET_SETTLE_S)
    if deps.foreground() != target.hwnd:
        return RESULT_NOT_FOCUSED
    want = _norm(text)[:VERIFY_CHARS]
    before = deps.prompt_text(box)
    if not deps.send_ctrl_v():
        return RESULT_NOT_FOCUSED
    if not want or before is None:
        return RESULT_PASTED  # nothing to check against: trust the keystroke
    had = _norm(before).count(want)
    for _ in range(VERIFY_STEPS):
        now = deps.prompt_text(box)
        if now is not None and _norm(now).count(want) > had:
            return RESULT_PASTED
        deps.sleep(STEP_S)
    return RESULT_NOT_LANDED


# ---- the real Windows side ------------------------------------------------------------------
INPUT_KEYBOARD, KEYEVENTF_KEYUP = 1, 0x0002
VK_CONTROL, VK_V = 0x11, 0x56
_MODIFIER_VKS = (0x10, 0x11, 0x12, 0x5B, 0x5C)  # Shift, Ctrl, Alt, left/right Win
SW_RESTORE = 9


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):  # only so the union has the size Windows expects
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _key(vk: int, up: bool) -> _INPUT:
    return _INPUT(type=INPUT_KEYBOARD, u=_INPUTUNION(ki=_KEYBDINPUT(wVk=vk, dwFlags=KEYEVENTF_KEYUP if up else 0)))


def _send_ctrl_v() -> bool:
    keys = (_INPUT * 4)(_key(VK_CONTROL, False), _key(VK_V, False), _key(VK_V, True), _key(VK_CONTROL, True))
    sent = ctypes.windll.user32.SendInput(4, keys, ctypes.sizeof(_INPUT))
    return sent == 4


def _modifiers_down() -> bool:
    state = ctypes.windll.user32.GetAsyncKeyState
    return any(state(vk) & 0x8000 for vk in _MODIFIER_VKS)


def _windows() -> list:
    from ..capture import windowinfo as wi  # z-ordered raw enumeration + process path helpers

    out = []
    for raw in wi._enum_raw_windows():
        out.append(Window(hwnd=raw.hwnd, exe=wi._exe_path_of_pid(raw.pid), title=raw.title,
                          class_name=raw.class_name, visible=raw.visible and not raw.cloaked, iconic=raw.iconic))
    return out


def _pumping_sleep(seconds: float) -> None:
    """Sleep while still answering clipboard requests and other posted events (not user input)."""
    from PySide6.QtCore import QCoreApplication, QEventLoop

    app = QCoreApplication.instance()
    end = time.monotonic() + seconds
    while True:
        if app is not None:
            app.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(0.01, left))


def _search_prompt(key):
    """Claude's message box via UI Automation (about 0.2 s), or None. `key` is a window hwnd
    (its first box) or a Pane.key (hwnd, pane name) for that pane's box."""
    import uiautomation as auto

    if isinstance(key, tuple):
        hwnd, pane_name = key
        root = auto.ControlFromHandle(hwnd).GroupControl(Name=pane_name, searchDepth=25)
        if not root.Exists(1.0, 0.05):
            return None
    else:
        root = auto.ControlFromHandle(key)
    box = root.EditControl(Name=PROMPT_NAME, searchDepth=40)
    if box.Exists(1.5, 0.05):  # the first query also wakes Chromium's accessibility tree
        return box
    box = root.EditControl(searchDepth=40, Compare=lambda c, _d: "ProseMirror" in (c.ClassName or ""))
    return box if box.Exists(0.5, 0.05) else None


def _prompt_still_valid(box) -> bool:
    """A remembered box is reused only while Claude still shows that same element."""
    try:
        el = box.Element
        return el.CurrentName == PROMPT_NAME or "ProseMirror" in (el.CurrentClassName or "")
    except Exception:  # noqa: BLE001 - stale element: Claude re-rendered it
        return False


class _PromptBox:
    """Claude's message box, remembered between pastes (searching for it is the slow part)."""

    def __init__(self, search: Callable = _search_prompt, is_valid: Callable = _prompt_still_valid) -> None:
        self._search, self._is_valid = search, is_valid
        self._found: dict[object, object] = {}  # hwnd or Pane.key -> box

    def control(self, key):
        box = self._found.get(key)
        if box is not None and self._is_valid(box):
            return box
        box = self._search(key)
        if box is None:
            self._found.pop(key, None)
        else:
            self._found[key] = box
        return box

    def focus(self, key) -> bool:
        try:
            box = self.control(key)
            if box is None:
                return False
            box.SetFocus()
            return True
        except Exception:  # noqa: BLE001 - accessibility hiccups mean "no box", never a crash
            return False

    def text(self, key) -> Optional[str]:
        try:
            box = self.control(key)
            return None if box is None else box.GetValuePattern().Value
        except Exception:  # noqa: BLE001
            return None


_BOX = _PromptBox()  # shared by prewarm() and every paste


def default_deps() -> PasteDeps:
    from ..capture.winapi import force_foreground

    user32 = ctypes.windll.user32
    box = _BOX
    return PasteDeps(
        windows=_windows,
        restore=lambda hwnd: user32.ShowWindow(hwnd, SW_RESTORE),
        focus=force_foreground,
        foreground=lambda: int(user32.GetForegroundWindow() or 0),
        modifiers_down=_modifiers_down,
        send_ctrl_v=_send_ctrl_v,
        sleep=_pumping_sleep,
        focus_prompt=box.focus,
        prompt_text=box.text,
    )
