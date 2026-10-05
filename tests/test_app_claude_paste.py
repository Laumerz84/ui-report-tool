"""Paste-into-Claude on Finish (package C). Fake windows and a fake keyboard only: these tests
never focus a real window or send a real key. The real paste is blocked for the whole test run."""
from __future__ import annotations

from dataclasses import dataclass, field

from uireport.app import claude_paste as cp

CLAUDE_EXE = r"C:\Program Files\WindowsApps\Claude_2.19675.0.0_x64__pzs8sxrjxfjjc\app\Claude.exe"
CLI_EXE = r"C:\Users\me\.local\bin\claude.exe"


def win(hwnd, exe, title="Claude", cls=cp.ELECTRON_CLASS, visible=True, iconic=False):
    return cp.Window(hwnd=hwnd, exe=exe, title=title, class_name=cls, visible=visible, iconic=iconic)


TEXT = "I captured 1 screenshot. Read G:\\UIReports\\x\\report.md and look at every image"


@dataclass
class Fake:
    windows: list
    fg_sequence: list = field(default_factory=list)  # what GetForegroundWindow returns, call by call
    held: int = 0  # how many checks the Finish shortcut's modifiers are still held
    send_ok: bool = True
    has_prompt: bool = True  # Claude's message box found
    box: str = ""  # what the message box contains
    lands: bool = True  # does Ctrl+V actually put the clipboard text in the box?
    readable: bool = True
    log: list = field(default_factory=list)

    def deps(self) -> cp.PasteDeps:
        def foreground():
            value = self.fg_sequence.pop(0) if len(self.fg_sequence) > 1 else (self.fg_sequence or [0])[0]
            self.log.append(("fg", value))
            return value

        def modifiers_down():
            self.log.append("mods?")
            if self.held > 0:
                self.held -= 1
                return True
            return False

        def send_ctrl_v():
            self.log.append("CTRL+V")
            if self.send_ok and self.lands:
                self.box += TEXT  # what a real paste puts in the box
            return self.send_ok

        return cp.PasteDeps(
            windows=lambda: list(self.windows),
            restore=lambda h: self.log.append(("restore", h)),
            focus=lambda h: self.log.append(("focus", h)) or True,
            foreground=foreground,
            modifiers_down=modifiers_down,
            send_ctrl_v=send_ctrl_v,
            sleep=lambda s: self.log.append(("sleep", s)),
            focus_prompt=lambda h: self.log.append(("focus_prompt", h)) or self.has_prompt,
            prompt_text=lambda h: self.box if self.readable else None,
        )

    def keys_sent(self) -> int:
        return self.log.count("CTRL+V")


def test_claude_closed_means_nothing_is_focused_or_typed():
    f = Fake([win(1, r"C:\Windows\explorer.exe", "Files", "CabinetWClass"),
              win(2, CLI_EXE, "claude", "ConsoleWindowClass")])  # the CLI in a console is not the app
    assert cp.paste_into_claude(TEXT, f.deps()) == cp.RESULT_NOT_OPEN
    assert f.keys_sent() == 0 and not any(isinstance(e, tuple) and e[0] == "focus" for e in f.log)


def test_open_claude_is_brought_forward_and_gets_exactly_one_paste():
    f = Fake([win(1, r"C:\x\other.exe", "Other"), win(7, CLAUDE_EXE)], fg_sequence=[7])
    assert cp.paste_into_claude(TEXT, f.deps()) == cp.RESULT_PASTED
    assert ("focus", 7) in f.log and f.keys_sent() == 1
    assert f.log.index(("focus", 7)) < f.log.index("CTRL+V")


def test_it_waits_until_the_finish_shortcut_keys_are_released():
    f = Fake([win(7, CLAUDE_EXE)], fg_sequence=[7], held=3)
    assert cp.paste_into_claude(TEXT, f.deps()) == cp.RESULT_PASTED
    assert f.log.count("mods?") == 4 and f.log.index("CTRL+V") > max(i for i, e in enumerate(f.log) if e == "mods?")


def test_keys_held_too_long_means_nothing_is_typed():
    f = Fake([win(7, CLAUDE_EXE)], fg_sequence=[7], held=10_000)
    assert cp.paste_into_claude(TEXT, f.deps()) == cp.RESULT_NOT_FOCUSED
    assert f.keys_sent() == 0


def test_a_minimised_claude_is_restored_first():
    f = Fake([win(7, CLAUDE_EXE, iconic=True)], fg_sequence=[7])
    assert cp.paste_into_claude(TEXT, f.deps()) == cp.RESULT_PASTED
    assert f.log.index(("restore", 7)) < f.log.index(("focus", 7))


def test_the_topmost_visible_claude_window_wins():
    f = Fake([win(3, CLAUDE_EXE, visible=False), win(4, CLAUDE_EXE, title=""),
              win(5, CLAUDE_EXE, "Claude", cls="SomethingElse"), win(6, CLAUDE_EXE), win(8, CLAUDE_EXE)],
             fg_sequence=[6])
    assert cp.paste_into_claude(TEXT, f.deps()) == cp.RESULT_PASTED
    assert ("focus", 6) in f.log


def test_the_cursor_is_put_in_claudes_message_box_before_pasting():
    f = Fake([win(7, CLAUDE_EXE)], fg_sequence=[7])
    assert cp.paste_into_claude(TEXT, f.deps()) == cp.RESULT_PASTED
    assert f.log.index(("focus_prompt", 7)) < f.log.index("CTRL+V")


def test_no_message_box_means_nothing_is_typed():
    f = Fake([win(7, CLAUDE_EXE)], fg_sequence=[7], has_prompt=False)  # e.g. Claude is showing its settings
    assert cp.paste_into_claude(TEXT, f.deps()) == cp.RESULT_NO_PROMPT
    assert f.keys_sent() == 0


def test_a_paste_that_does_not_land_is_reported_not_claimed():
    f = Fake([win(7, CLAUDE_EXE)], fg_sequence=[7], lands=False)
    assert cp.paste_into_claude(TEXT, f.deps()) == cp.RESULT_NOT_LANDED


def test_text_already_in_the_box_does_not_count_as_a_new_paste():
    f = Fake([win(7, CLAUDE_EXE)], fg_sequence=[7], box="draft " + TEXT, lands=False)
    assert cp.paste_into_claude(TEXT, f.deps()) == cp.RESULT_NOT_LANDED
    f2 = Fake([win(7, CLAUDE_EXE)], fg_sequence=[7], box="draft " + TEXT)
    assert cp.paste_into_claude(TEXT, f2.deps()) == cp.RESULT_PASTED  # appended a second copy


def test_whitespace_changes_by_the_message_box_still_count_as_landed():
    multi = "Report:\n  {see below}\n\nRead G:\\x\\report.md"
    f = Fake([win(7, CLAUDE_EXE)], fg_sequence=[7], lands=False)
    deps = f.deps()
    deps.send_ctrl_v = lambda: setattr(f, "box", "Report: {see below}\nRead G:\\x\\report.md") or True
    assert cp.paste_into_claude(multi, deps) == cp.RESULT_PASTED


def test_an_unreadable_message_box_is_trusted():
    f = Fake([win(7, CLAUDE_EXE)], fg_sequence=[7], readable=False)
    assert cp.paste_into_claude(TEXT, f.deps()) == cp.RESULT_PASTED


def test_a_non_store_install_is_recognised_too():
    exe = r"C:\Users\me\AppData\Local\AnthropicClaude\app-1.2.3\claude.exe"
    assert cp.is_claude_desktop(win(1, exe))
    assert not cp.is_claude_desktop(win(1, CLI_EXE, "claude", "ConsoleWindowClass"))
    assert not cp.is_claude_desktop(win(1, r"C:\x\notclaude.exe"))


def test_if_claude_never_comes_to_the_front_nothing_is_typed():
    f = Fake([win(7, CLAUDE_EXE)], fg_sequence=[99])  # another window keeps the focus
    assert cp.paste_into_claude(TEXT, f.deps()) == cp.RESULT_NOT_FOCUSED
    assert f.keys_sent() == 0


def test_focus_lost_just_before_the_paste_types_nothing():
    f = Fake([win(7, CLAUDE_EXE)], fg_sequence=[7, 99])  # front at first, then something else jumps in
    assert cp.paste_into_claude(TEXT, f.deps()) == cp.RESULT_NOT_FOCUSED
    assert f.keys_sent() == 0


class FakeBox:
    def __init__(self, valid=True):
        self.valid = valid


def test_the_message_box_is_found_once_and_then_remembered():
    searches = []
    box = cp._PromptBox(search=lambda h: searches.append(h) or FakeBox(), is_valid=lambda b: b.valid)
    first = box.control(7)
    assert box.control(7) is first and box.control(7) is first
    assert searches == [7]


def test_a_rebuilt_message_box_is_searched_again():
    searches = []
    box = cp._PromptBox(search=lambda h: searches.append(h) or FakeBox(), is_valid=lambda b: b.valid)
    first = box.control(7)
    first.valid = False  # Claude re-rendered its message box (e.g. another session was opened)
    second = box.control(7)
    assert second is not first and searches == [7, 7]


def test_not_finding_a_box_is_not_remembered():
    results = [None, FakeBox()]
    box = cp._PromptBox(search=lambda h: results.pop(0), is_valid=lambda b: True)
    assert box.control(7) is None
    assert box.control(7) is not None  # looked again the next time


def test_prewarm_is_disabled_for_the_whole_test_run(monkeypatch):
    called = []
    monkeypatch.setattr(cp, "_windows", lambda: called.append(1) or [])
    cp.prewarm()
    assert called == []


def test_the_real_paste_is_disabled_for_the_whole_test_run(monkeypatch):
    called = []
    monkeypatch.setattr(cp, "default_deps", lambda: called.append(1))
    assert cp.paste_into_claude(TEXT) == cp.RESULT_DISABLED  # conftest sets UIREPORT_NO_PASTE
    monkeypatch.delenv(cp.BLOCK_ENV, raising=False)
    assert cp.paste_into_claude(TEXT) == cp.RESULT_DISABLED  # the offscreen platform blocks it as well
    assert called == []
