"""The real SettingsWindow driven through the real AppController: hotkeys, delay length, start with Windows.

The OS-wide hotkey table and the registry are replaced by in-memory fakes (nothing is registered,
nothing is written); everything else is the real application on a synthetic desktop.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from integration_support import Stack, layout_mixed, settle, wait_until
from uireport.geometry import IntRect
from uireport.hotkeyspec import NAME_CAPTURE, NAME_DELAYED
from uireport.models import CaptureMode

CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier


@pytest.fixture
def stack(qapp, test_out, monkeypatch):
    st = Stack(test_out, monkeypatch, layout_mixed(), delay_seconds=3)
    yield st
    win = st.controller.settings_window
    if win is not None:
        win.close()
    st.shutdown()


def open_settings(st: Stack):
    st.tray.action_settings.trigger()
    win = st.controller.settings_window
    assert win is not None and win.isVisible()
    return win


def record(st: Stack, win, edit, key, mods) -> None:
    """Click into a press-to-record hotkey field, press a combination, click somewhere else.
    While the field records, the global hotkeys are released so the combination that is registered right
    now can be pressed again (otherwise Windows would swallow it); they come back when the field is left."""
    before = dict(st.hotkeys.registered)
    win.activateWindow()
    edit.setFocus()
    settle(40)
    assert st.hotkeys.registered == {}, "hotkeys must be released while a field records"
    QTest.keyClick(edit, key, mods)
    edit.clearFocus()
    settle(40)
    assert st.hotkeys.registered == before, "hotkeys must be back once the field is left"


def test_defaults_are_what_the_spec_suggested(stack):
    assert stack.hotkeys.registered == {NAME_CAPTURE: "Ctrl+Alt+S", NAME_DELAYED: "Ctrl+Alt+D"}
    assert stack.store.settings.delay_seconds == 3
    tip = stack.tray.toolTip()
    assert "Ctrl+Alt+S" in tip and "Ctrl+Alt+D" in tip
    assert stack.store.settings.start_with_windows is False


def test_changing_hotkeys_delay_and_autostart_in_the_settings_window(stack):
    win = open_settings(stack)
    assert win.capture_edit.value() == "Ctrl+Alt+S" and win.delayed_edit.value() == "Ctrl+Alt+D"
    assert win.delay_spin.value() == 3 and not win.autostart_check.isChecked()

    record(stack, win, win.capture_edit, Qt.Key.Key_F9, CTRL | SHIFT)
    record(stack, win, win.delayed_edit, Qt.Key.Key_F10, CTRL | SHIFT)
    win.delay_spin.setValue(5)
    win.autostart_check.setChecked(True)
    assert win.capture_edit.value() == "Ctrl+Shift+F9" and win.delayed_edit.value() == "Ctrl+Shift+F10"
    assert win.ok_button.isEnabled()
    win.ok_button.click()
    settle(30)

    s = stack.store.settings
    assert (s.hotkey_capture, s.hotkey_delayed, s.delay_seconds, s.start_with_windows) == \
        ("Ctrl+Shift+F9", "Ctrl+Shift+F10", 5, True)
    assert stack.hotkeys.registered == {NAME_CAPTURE: "Ctrl+Shift+F9", NAME_DELAYED: "Ctrl+Shift+F10"}
    assert stack.autostart.calls == [True] and stack.autostart.enabled
    saved = json.loads(Path(stack.store.path).read_text(encoding="utf-8"))
    assert saved["hotkey_capture"] == "Ctrl+Shift+F9" and saved["delay_seconds"] == 5 and saved["start_with_windows"] is True
    assert "Ctrl+Shift+F9" in stack.tray.toolTip() and "Ctrl+Shift+F10" in stack.tray.toolTip()
    assert stack.controller.settings_window is None  # OK closed the dialog

    # the new hotkeys work, and the delayed capture now counts down from 5
    stack.rig.put_cursor_on(stack.rig.monitors[0])
    stack.hotkeys.press(NAME_DELAYED)
    assert wait_until(lambda: stack.service.state == "countdown", 3)
    assert stack.rig.countdowns[-1]._seconds == 5
    stack.rig.esc_in_countdown()
    assert wait_until(lambda: not stack.service.is_active, 3)


def test_a_taken_hotkey_is_reported_inline_and_the_old_one_keeps_working(stack):
    stack.hotkeys.fail_combos = {"Ctrl+Alt+F8": "already used by another program"}
    win = open_settings(stack)
    record(stack, win, win.capture_edit, Qt.Key.Key_F8, CTRL | Qt.KeyboardModifier.AltModifier)
    assert win.capture_edit.value() == "Ctrl+Alt+F8"
    win.apply_button.click()
    settle(30)
    assert win.isVisible(), "the dialog stays open so the person can pick another combination"
    assert "Ctrl+Alt+F8" in win.status_label.text() and "already used" in win.status_label.text()
    assert stack.store.settings.hotkey_capture == "Ctrl+Alt+S"  # last working combination kept
    assert stack.hotkeys.registered[NAME_CAPTURE] == "Ctrl+Alt+S"
    # a working combination can then be applied from the same dialog
    record(stack, win, win.capture_edit, Qt.Key.Key_F9, CTRL | SHIFT)
    win.ok_button.click()
    settle(30)
    assert stack.store.settings.hotkey_capture == "Ctrl+Shift+F9"
    assert stack.hotkeys.registered[NAME_CAPTURE] == "Ctrl+Shift+F9"


def test_windows_hotkeys_that_are_reserved_or_unsafe_cannot_be_applied(stack):
    win = open_settings(stack)
    record(stack, win, win.capture_edit, Qt.Key.Key_S, Qt.KeyboardModifier.MetaModifier | SHIFT)  # Win+Shift+S is Snipping Tool
    assert not win.ok_button.isEnabled() and not win.apply_button.isEnabled()
    assert win.capture_note.isVisible() or win.capture_note.text()
    win.capture_default.click()
    assert win.capture_edit.value() == "Ctrl+Alt+S" and win.ok_button.isEnabled()


def test_start_with_windows_shows_the_real_state_not_just_the_saved_flag(stack):
    # the saved flag says "on", but the person removed the entry in Task Manager > Startup apps
    stack.store.settings.start_with_windows = True
    stack.autostart.enabled = False
    win = open_settings(stack)
    assert not win.autostart_check.isChecked()
    win.close()
    settle(20)
    # and the other way round
    stack.store.settings.start_with_windows = False
    stack.autostart.enabled = True
    win = open_settings(stack)
    assert win.autostart_check.isChecked()
    win.autostart_check.setChecked(False)
    win.ok_button.click()
    settle(30)
    assert stack.autostart.calls == [False] and not stack.autostart.enabled


def test_unchanged_settings_do_not_touch_start_with_windows(stack):
    win = open_settings(stack)
    win.delay_spin.setValue(4)
    win.ok_button.click()
    settle(30)
    assert stack.store.settings.delay_seconds == 4
    assert stack.autostart.calls == []  # the registry is only written when the checkbox actually changes


def test_the_reports_folder_can_be_changed_and_is_used_by_finish(stack, test_out):
    new_root = test_out / "elsewhere"
    win = open_settings(stack)
    win.output_edit.setText(str(new_root))
    win.ok_button.click()
    settle(30)
    assert stack.store.settings.output_root == str(new_root)
    stack.rig.put_cursor_on(stack.rig.monitors[0])
    stack.hotkeys.press(NAME_CAPTURE)
    settle(30)
    assert stack.service.state == "selecting"
    stack.rig.drag_region(IntRect(150, 90, 240, 160))
    assert wait_until(lambda: len(stack.session.shots) == 1, 5)
    QTest.keyClick(stack.editor.caption_edit, Qt.Key.Key_Return, CTRL | SHIFT)
    assert wait_until(lambda: stack.finished or stack.failed, 30)
    assert stack.failed == []
    assert Path(stack.finished[0].folder).parent == new_root
    assert (new_root / "latest.json").is_file()
