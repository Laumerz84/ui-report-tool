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
ANTIGRAVITY_INPUT_NAME = "Message input"  # accessible name of Antigravity's chat input (ComboBoxControl)
ANTIGRAVITY_CONV_PREFIX = "Conversation: "  # button in each Antigravity split-pane header
STEP_S = 0.01
FOCUS_WAIT_STEPS, MODIFIER_WAIT_STEPS, VERIFY_STEPS = 100, 200, 150  # up to 1 s, 2 s, 1.5 s
CARET_SETTLE_S = 0.02  # Chromium moves the caret a frame after the accessibility SetFocus
VERIFY_CHARS = 40  # the start of the text that must newly appear in the box

_last_target_app: str = "Claude"  # "Claude" | "Antigravity" for the most recent paste attempt


def last_target_app() -> str:
    """Display name ('Claude' or 'Antigravity') of the AI window targeted by the last paste attempt."""
    return _last_target_app


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
    """One of an AI window's chat panes (Claude or Antigravity split panes)."""
    hwnd: int
    name: str  # Claude: "Primary pane" / "Secondary pane"; Antigravity: "agy:0" / "agy:1"
    title: str  # the chat shown in it ("" when it isn't showing one)
    left: int  # screen x, to call them left / right
    app: str = "claude"  # "claude" | "antigravity"

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
    # both take a window hwnd (its active/first message box) or a Pane.key (that pane's box)
    focus_prompt: Callable[[object], bool]  # cursor into the message box; False if there is none
    prompt_text: Callable[[object], Optional[str]]  # the box's text, None if it can't be read


def is_claude_desktop(w: Window) -> bool:
    """The Claude desktop app's main window (Store or regular install), not the Claude Code CLI."""
    return (os.path.basename(w.exe).lower() == "claude.exe" and w.class_name == ELECTRON_CLASS
            and w.visible and bool(w.title.strip()))


def is_antigravity(w: Window) -> bool:
    """Antigravity's main desktop window."""
    return (os.path.basename(w.exe).lower() == "antigravity.exe" and w.class_name == ELECTRON_CLASS
            and w.visible and bool(w.title.strip()))


def ai_app_kind(w: Window) -> Optional[str]:
    """'claude', 'antigravity', or None."""
    if is_claude_desktop(w):
        return "claude"
    if is_antigravity(w):
        return "antigravity"
    return None


def is_ai_desktop(w: Window) -> bool:
    """Either the Claude desktop app or Antigravity."""
    return ai_app_kind(w) is not None


def app_label(kind: Optional[str]) -> str:
    return "Antigravity" if kind == "antigravity" else "Claude"


def _select_target_window(
    windows: list,
    pane: Optional[Pane] = None,
    prefer: Optional[str] = None,
) -> Optional[Window]:
    """Pick the most recently active AI window (Claude or Antigravity).

    `windows` is already in Win32 Z-order (most recently active non-minimised window first).
    `prefer` ('claude' | 'antigravity' from FocusTracker) breaks ties when the app the user was
    using most recently was minimised while an older window of the other app stayed un-minimised."""
    ai = [w for w in windows if is_ai_desktop(w)]
    if pane is not None:
        ai = [w for w in ai if w.hwnd == pane.hwnd]
        return ai[0] if ai else None
    if not ai:
        return None
    open_wins = [w for w in ai if not w.iconic]
    if open_wins:
        kinds_open = {ai_app_kind(w) for w in open_wins}
        if prefer in ("claude", "antigravity") and prefer not in kinds_open:
            minimised_pref = [w for w in ai if w.iconic and ai_app_kind(w) == prefer]
            if minimised_pref:
                return minimised_pref[0]
        return open_wins[0]
    if prefer in ("claude", "antigravity"):
        pref_wins = [w for w in ai if ai_app_kind(w) == prefer]
        if pref_wins:
            return pref_wins[0]
    return ai[0]


def _blocked() -> bool:
    return os.environ.get(BLOCK_ENV) == "1" or os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen"


def prewarm(pane: Optional[Pane] = None, prefer: Optional[str] = None) -> None:
    """Find the target AI app's message box ahead of time (called while the report is being written), so
    the paste itself doesn't have to search the accessibility tree."""
    if _blocked():
        return
    try:
        if pane is not None:
            _BOX.invalidate(pane.key)
            _BOX.control(pane.key)
            return
        target = _select_target_window(_windows(), prefer=prefer)
        if target is not None:
            _BOX.invalidate(target.hwnd)
            _BOX.control(target.hwnd)
    except Exception:  # noqa: BLE001 - a warm-up must never break Finish
        pass


PANE_SUFFIX = " pane"  # Claude's pane groups are named "Primary pane" / "Secondary pane"
TITLE_SUFFIX = ", rename session"  # each Claude pane's chat-title button: "<title>, rename session"


def _walk_pruned(root, max_depth: int = 42, skip_types: tuple = ("ListItemControl",)):
    """Walk UIA descendants while skipping message-list items (hundreds of transcript rows)."""
    stack = [(root, 0)]
    while stack:
        ctrl, depth = stack.pop()
        yield ctrl, depth
        if depth >= max_depth:
            continue
        try:
            children = ctrl.GetChildren()
        except Exception:
            continue
        for ch in reversed(children):
            try:
                ct = ch.ControlTypeName
            except Exception:
                continue
            if ct in skip_types:
                continue
            stack.append((ch, depth + 1))


def _antigravity_pane_title(box) -> str:
    """Read 'Conversation: <title>' from the enclosing Antigravity split-pane header."""
    cur = box
    pane_ctrl = None
    for _ in range(14):
        try:
            cur = cur.GetParentControl()
        except Exception:
            cur = None
        if cur is None:
            break
        try:
            if "group/pane" in (cur.ClassName or ""):
                pane_ctrl = cur
                break
        except Exception:
            continue
    if pane_ctrl is None:
        return ""
    for ch, _d in _walk_pruned(pane_ctrl, max_depth=7):
        try:
            nm = ch.Name or ""
            if ch.ControlTypeName == "ButtonControl" and nm.startswith(ANTIGRAVITY_CONV_PREFIX):
                return nm[len(ANTIGRAVITY_CONV_PREFIX):].strip()
        except Exception:
            continue
    return ""


def _antigravity_panes(hwnd: int) -> list:
    """Return [(Pane, box_control, has_focus), ...] left-to-right for an Antigravity window."""
    import uiautomation as auto

    root = auto.ControlFromHandle(hwnd)
    probe = root.ComboBoxControl(Name=ANTIGRAVITY_INPUT_NAME, searchDepth=42)
    if not probe.Exists(1.5, 0.05):
        return []
    raw_boxes = [
        c for c, _d in _walk_pruned(root, max_depth=42)
        if c.ControlTypeName == "ComboBoxControl" and (c.Name or "") == ANTIGRAVITY_INPUT_NAME
    ]
    items = []
    for b in raw_boxes:
        try:
            rect = b.BoundingRectangle
            if rect.right <= rect.left or rect.bottom <= rect.top:
                continue
            left = int(rect.left)
        except Exception:
            continue
        focused = False
        try:
            focused = bool(b.HasKeyboardFocus)
        except Exception:
            pass
        items.append((left, _antigravity_pane_title(b), b, focused))
    items.sort(key=lambda item: item[0])
    return [
        (Pane(hwnd=hwnd, name=f"agy:{idx}", title=title, left=left, app="antigravity"), b, focused)
        for idx, (left, title, b, focused) in enumerate(items)
    ]


def _pick_antigravity_box(hwnd: int, items: list):
    """Pick the active split pane's input box in Antigravity."""
    if not items:
        return None
    if len(items) == 1:
        return items[0][1]
    for _pane, box, focused in items:
        if focused:
            return box
    win_title = ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        if ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512) > 0:
            win_title = buf.value
    except Exception:
        pass
    head = win_title.split(" - ")[0].rstrip(".…").strip()
    if head:
        for pane, box, _focused in items:
            if pane.title and pane.title.startswith(head):
                return box
    return items[0][1]


def list_panes() -> list:
    """The chat panes of every open Claude desktop and Antigravity window (topmost window first,
    left to right within a window). Read-only: nothing is focused or clicked. [] if none are open."""
    if _blocked():
        return []
    try:
        import uiautomation as auto
    except Exception:  # noqa: BLE001
        return []
    out = []
    for w in _windows():
        kind = ai_app_kind(w)
        if kind == "claude":
            try:
                root = auto.ControlFromHandle(w.hwnd)
                found = [c for c, _d in auto.WalkControl(root, maxDepth=22)
                         if c.ControlTypeName == "GroupControl" and (c.Name or "").endswith(PANE_SUFFIX)]
                panes = []
                for group in found:
                    btn = group.ButtonControl(searchDepth=20, Compare=lambda c, _d: (c.Name or "").endswith(TITLE_SUFFIX))
                    title = btn.Name[: -len(TITLE_SUFFIX)] if btn.Exists(0.3, 0.05) else ""
                    panes.append(Pane(hwnd=w.hwnd, name=group.Name, title=title,
                                      left=group.BoundingRectangle.left, app="claude"))
                out.extend(sorted(panes, key=lambda p: p.left))
            except Exception:  # noqa: BLE001 - an unreadable window just isn't offered
                continue
        elif kind == "antigravity":
            try:
                out.extend(pane for pane, _box, _focused in _antigravity_panes(w.hwnd))
            except Exception:  # noqa: BLE001
                continue
    return out


def pane_labels(panes: list) -> list:
    """Menu text for each pane: 'Left: <chat>' / 'Right: <chat>', 'Window 2 ...', prefixed with app name if both are open."""
    hwnds = list(dict.fromkeys(p.hwnd for p in panes))
    apps = {getattr(p, "app", "claude") for p in panes}
    multi_app = len(apps) > 1
    labels = []
    for p in panes:
        kind = getattr(p, "app", "claude")
        app_name = app_label(kind)
        same = [q for q in panes if q.hwnd == p.hwnd]
        app_hwnds = [h for h in hwnds if any(q.hwnd == h and getattr(q, "app", "claude") == kind for q in panes)]
        side = "" if len(same) == 1 else ("Left" if p is same[0] else "Right" if p is same[-1] else "Middle")
        win_part = f"Window {app_hwnds.index(p.hwnd) + 1} " if len(app_hwnds) > 1 else ""
        prefix = f"{app_name} " if multi_app else ""
        where = (prefix + win_part + side).strip() or app_name
        labels.append(f"{where}: {p.title or '(no chat open)'}")
    return labels


def _norm(text: str) -> str:
    return " ".join(str(text).split())  # the message box turns newlines into paragraphs


def paste_into_claude(
    text: str,
    deps: Optional[PasteDeps] = None,
    pane: Optional[Pane] = None,
    prefer: Optional[str] = None,
) -> str:
    """Bring the most recently active AI desktop window (Claude or Antigravity) forward, put the
    cursor in its message box (or in `pane`'s, when given) and press Ctrl+V once (`text` is what
    is on the clipboard). Returns a RESULT_*."""
    global _last_target_app
    if deps is None:
        if _blocked():
            return RESULT_DISABLED
        deps = default_deps()
    target = _select_target_window(deps.windows(), pane=pane, prefer=prefer)
    if target is None:
        return RESULT_NOT_OPEN
    _last_target_app = app_label(ai_app_kind(target))
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


def _hwnd_app_kind(hwnd: int) -> Optional[str]:
    try:
        from ..capture import windowinfo as wi

        exe = wi._exe_path_of_pid(wi._pid_of(int(hwnd))) or ""
    except Exception:  # noqa: BLE001
        return None
    base = os.path.basename(exe).lower()
    if base == "antigravity.exe":
        return "antigravity"
    if base == "claude.exe":
        return "claude"
    return None


def _search_prompt(key):
    """Claude's or Antigravity's message box via UI Automation, or None. `key` is a window hwnd
    (its active/first box) or a Pane.key (hwnd, pane name) for that pane's box."""
    import uiautomation as auto

    hwnd = key[0] if isinstance(key, tuple) else key
    pane_name = key[1] if isinstance(key, tuple) else None
    is_agy = (isinstance(pane_name, str) and pane_name.startswith("agy:")) or _hwnd_app_kind(hwnd) == "antigravity"
    if is_agy:
        items = _antigravity_panes(hwnd)
        if pane_name is not None:
            for pane, box, _focused in items:
                if pane.name == pane_name:
                    return box
            return None
        return _pick_antigravity_box(hwnd, items)

    if pane_name is not None:
        root = auto.ControlFromHandle(hwnd).GroupControl(Name=pane_name, searchDepth=25)
        if not root.Exists(1.0, 0.05):
            return None
    else:
        root = auto.ControlFromHandle(hwnd)
    box = root.EditControl(Name=PROMPT_NAME, searchDepth=40)
    if box.Exists(1.5, 0.05):  # the first query also wakes Chromium's accessibility tree
        return box
    box = root.EditControl(searchDepth=40, Compare=lambda c, _d: "ProseMirror" in (c.ClassName or ""))
    return box if box.Exists(0.5, 0.05) else None


def _prompt_still_valid(box) -> bool:
    """A remembered box is reused only while the app still shows that same element."""
    try:
        el = box.Element
        nm = el.CurrentName or ""
        cls = el.CurrentClassName or ""
        if nm not in (PROMPT_NAME, ANTIGRAVITY_INPUT_NAME) and "ProseMirror" not in cls:
            return False
        try:
            r = el.CurrentBoundingRectangle
            return (r.right - r.left) > 0 and (r.bottom - r.top) > 0
        except AttributeError:
            return True
    except Exception:  # noqa: BLE001 - stale element: re-rendered
        return False


def _box_text(box) -> Optional[str]:
    """Read the text currently inside Claude's EditControl or Antigravity's ComboBoxControl."""
    if getattr(box, "ControlTypeName", "") == "ComboBoxControl":
        try:
            val = box.GetValuePattern().Value or ""
            if val:
                return val
        except Exception:  # noqa: BLE001
            pass
        try:
            import uiautomation as auto

            parts = [c.Name for c, d in auto.WalkControl(box, maxDepth=6)
                     if d > 0 and c.ControlTypeName == "TextControl" and c.Name]
            if parts:
                return " ".join(parts)
            tp = box.GetPattern(auto.PatternId.TextPattern)
            if tp is not None:
                doc = tp.DocumentRange.GetText(4000) or ""
                if doc and doc.strip() != ANTIGRAVITY_INPUT_NAME:
                    return doc
        except Exception:  # noqa: BLE001
            pass
        return ""
    return box.GetValuePattern().Value


class _PromptBox:
    """The target AI app's message box, remembered within a paste (searching for it is the slow part)."""

    def __init__(self, search: Callable = _search_prompt, is_valid: Callable = _prompt_still_valid) -> None:
        self._search, self._is_valid = search, is_valid
        self._found: dict[object, object] = {}  # hwnd or Pane.key -> box

    def invalidate(self, key) -> None:
        self._found.pop(key, None)

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
            return None if box is None else _box_text(box)
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
