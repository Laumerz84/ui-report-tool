"""Icons, tray menu, toast and settings window (package C). Offscreen Qt only."""
from __future__ import annotations

import time

import pytest
from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QGuiApplication, QKeyEvent
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from uireport.app import toast as toast_module
from uireport.app.icons import ICON_SIZES, make_app_icon
from uireport.app.settings_window import HotkeyEdit, SettingsWindow, hotkey_from_key_event
from uireport.app.toast import MARGIN, Toast
from uireport.app.tray import MENU_CLOSE_SETTLE_MS, TrayIcon
from uireport.settings import Settings, SettingsStore

CTRL = Qt.KeyboardModifier.ControlModifier
ALT = Qt.KeyboardModifier.AltModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
META = Qt.KeyboardModifier.MetaModifier
NOMOD = Qt.KeyboardModifier.NoModifier


def pump(ms: int = 0) -> None:
    end = time.monotonic() + ms / 1000.0
    while True:
        QApplication.processEvents()
        if time.monotonic() >= end:
            break
        time.sleep(0.005)


def wait_until(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return bool(cond())


def key_press(widget, key, mods=NOMOD, text="", native_vk=0):
    ev = QKeyEvent(QEvent.Type.KeyPress, key, mods, 0, native_vk, 0, text)
    QApplication.sendEvent(widget, ev)
    return ev


# ---------------------------------------------------------------------------
# icons
# ---------------------------------------------------------------------------
def test_app_icon_has_every_size_and_real_pixels(qapp):
    icon = make_app_icon()
    assert not icon.isNull()
    sizes = {(s.width(), s.height()) for s in icon.availableSizes()}
    assert {(n, n) for n in ICON_SIZES} <= sizes
    for n in (16, 32, 64, 128):
        pm = icon.pixmap(QSize(n, n))
        assert (pm.width(), pm.height()) == (n, n)
        img = pm.toImage()
        assert img.pixelColor(n // 2, n // 2).alpha() == 255  # painted, centre opaque
        assert img.pixelColor(0, 0).alpha() == 0  # rounded tile: corner is transparent


def test_app_icon_custom_size_is_added(qapp):
    icon = make_app_icon(96)
    assert (96, 96) in {(s.width(), s.height()) for s in icon.availableSizes()}


# ---------------------------------------------------------------------------
# tray
# ---------------------------------------------------------------------------
def test_tray_menu_has_exact_labels_in_order(qapp):
    tray = TrayIcon()
    assert tray.menu_labels() == [
        "Capture", "Delayed capture", "Show session (0)", "Settings", "Open reports folder", "Quit",
    ]
    assert tray.contextMenu() is not None
    assert not tray.icon().isNull()


def test_tray_show_session_entry_follows_the_count(qapp):
    tray = TrayIcon()
    assert not tray.action_show_session.isEnabled()
    tray.set_session_count(3)
    assert tray.action_show_session.text() == "Show session (3)"
    assert tray.action_show_session.isEnabled()
    assert "Shots in this session: 3" in tray.toolTip()
    tray.set_session_count(0)
    assert tray.action_show_session.text() == "Show session (0)" and not tray.action_show_session.isEnabled()
    tray.set_session_count(-4)  # nonsense is clamped
    assert tray.action_show_session.text() == "Show session (0)"
    assert tray.menu_labels()[:3] == ["Capture", "Delayed capture", "Show session (0)"]


def test_tray_actions_emit_their_signals(qapp):
    tray = TrayIcon()
    got: list[str] = []
    for name in ("capture", "delayed", "settings", "open_folder", "show_session", "quit"):
        getattr(tray, f"{name}_requested").connect(lambda n=name: got.append(n))
    tray.set_session_count(2)
    for action in tray.contextMenu().actions():
        if not action.isSeparator():
            action.trigger()
    # every item answers at once except "Capture", which waits for the popup menu to disappear
    assert got == ["delayed", "show_session", "settings", "open_folder", "quit"]
    pump(MENU_CLOSE_SETTLE_MS + 150)
    assert got == ["delayed", "show_session", "settings", "open_folder", "quit", "capture"]


def test_tray_menu_capture_waits_for_the_menu_to_close_but_the_icon_click_does_not(qapp):
    """The screen is frozen right after the request, so a popup that is still fading out would be in the
    screenshot. The menu item therefore waits a moment; the icon click and the hotkeys have no popup."""
    tray = TrayIcon()
    got: list[str] = []
    tray.capture_requested.connect(lambda: got.append("capture"))
    tray.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)  # left click on the icon
    assert got == ["capture"]  # instant
    got.clear()
    t0 = time.monotonic()
    tray.action_capture.trigger()  # "Capture" in the menu
    assert got == []
    pump(MENU_CLOSE_SETTLE_MS + 250)
    assert got == ["capture"]
    assert MENU_CLOSE_SETTLE_MS <= 200  # short enough to feel immediate
    assert time.monotonic() - t0 >= MENU_CLOSE_SETTLE_MS / 1000.0 - 0.01


def test_tray_left_click_is_capture_and_other_clicks_are_not(qapp):
    tray = TrayIcon()
    got: list[str] = []
    tray.capture_requested.connect(lambda: got.append("capture"))
    tray.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
    assert got == ["capture"]
    for reason in (
        QSystemTrayIcon.ActivationReason.Context,
        QSystemTrayIcon.ActivationReason.DoubleClick,
        QSystemTrayIcon.ActivationReason.MiddleClick,
    ):
        tray.activated.emit(reason)
    assert got == ["capture"]


def test_tray_tooltip_shows_hotkeys_and_menu_labels_stay_exact(qapp):
    tray = TrayIcon()
    tray.set_hotkey_hint("Ctrl+Alt+S", "Ctrl+Alt+D")
    tip = tray.toolTip()
    assert "Ctrl+Alt+S" in tip and "Ctrl+Alt+D" in tip
    assert "Shots in this session: 0" in tip
    assert tray.menu_labels()[0] == "Capture" and tray.menu_labels()[1] == "Delayed capture"
    assert "Ctrl+Alt+S" in tray.action_capture.toolTip()


# ---------------------------------------------------------------------------
# toast
# ---------------------------------------------------------------------------
def test_toast_window_flags_and_attributes(qapp):
    t = Toast()
    flags = t.windowFlags()
    assert flags & Qt.WindowType.FramelessWindowHint
    assert flags & Qt.WindowType.WindowStaysOnTopHint
    assert flags & Qt.WindowType.WindowDoesNotAcceptFocus
    assert flags & Qt.WindowType.Tool
    assert t.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)


def test_toast_show_message_action_and_placement(qapp):
    t = Toast()
    called: list[int] = []
    t.show_toast("Copied \u2014 paste into Claude", action_text="Open folder", action=lambda: called.append(1))
    assert t.isVisible()
    assert t.message_label.text() == "Copied \u2014 paste into Claude"
    assert t.action_button.text() == "Open folder" and not t.action_button.isHidden()
    assert not t.is_error
    # bottom-right corner of the primary screen's available area
    avail = QGuiApplication.primaryScreen().availableGeometry()
    g = t.geometry()
    assert g.right() <= avail.right() - MARGIN + 1
    assert g.bottom() <= avail.bottom() - MARGIN + 1
    assert avail.right() - g.right() <= MARGIN + 2
    assert avail.bottom() - g.bottom() <= MARGIN + 2
    assert avail.contains(g)
    t.action_button.click()
    assert called == [1]
    assert not t.isVisible()  # clicking the action dismisses


def test_toast_without_action_hides_the_button_and_x_dismisses(qapp):
    t = Toast()
    t.show_toast("Saving...")
    assert t.action_button.isHidden()
    assert t.isVisible()
    t.close_button.click()
    assert not t.isVisible()


def test_toast_error_style_and_replacement(qapp):
    t = Toast()
    t.show_toast("first", action_text="Open", action=lambda: None)
    t.show_toast("Something failed", error=True)
    assert t.is_error and t.message_label.text() == "Something failed"
    assert t.action_button.isHidden()  # the previous action is gone
    t.show_toast("fine again")
    assert not t.is_error


def test_toast_auto_hides_and_sticky_toast_does_not(qapp):
    t = Toast()
    t.show_toast("short lived", timeout_ms=60)
    assert t.isVisible()
    assert wait_until(lambda: not t.isVisible(), 3)
    t.show_toast("sticky", timeout_ms=0)
    pump(150)
    assert t.isVisible()
    t.dismiss()
    assert not t.isVisible()
    t.dismiss()  # idempotent


def test_toast_dismiss_drops_the_action_and_failing_action_is_contained(qapp):
    t = Toast()
    t.show_toast("x", action_text="Go", action=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    t.action_button.click()  # must not raise
    assert not t.isVisible()


def test_toast_is_excluded_from_capture_once_on_a_real_platform(qapp, monkeypatch):
    import uireport.capture.winapi as winapi

    calls: list[tuple] = []
    monkeypatch.setattr(toast_module, "_has_real_hwnd", lambda: True)
    monkeypatch.setattr(winapi, "exclude_from_capture", lambda hwnd, exclude=True: calls.append((hwnd, exclude)) or True)
    t = Toast()
    t.show_toast("one")
    t.show_toast("two")
    assert calls == [(int(t.winId()), True)]  # once, with the toast's own window handle


def test_toast_survives_a_failing_exclude_from_capture(qapp, monkeypatch):
    import uireport.capture.winapi as winapi

    monkeypatch.setattr(toast_module, "_has_real_hwnd", lambda: True)

    def boom(hwnd, exclude=True):
        raise NotImplementedError("stub")

    monkeypatch.setattr(winapi, "exclude_from_capture", boom)
    t = Toast()
    t.show_toast("still shown")
    assert t.isVisible()


# ---------------------------------------------------------------------------
# hotkey recording helpers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "key, mods, native, expected",
    [
        (Qt.Key.Key_S, CTRL | ALT, 0, "Ctrl+Alt+S"),
        (Qt.Key.Key_D, CTRL | ALT | SHIFT, 0, "Ctrl+Alt+Shift+D"),
        (Qt.Key.Key_F12, CTRL | SHIFT, 0, "Ctrl+Shift+F12"),
        (Qt.Key.Key_F24, CTRL | ALT | SHIFT, 0, "Ctrl+Alt+Shift+F24"),
        (Qt.Key.Key_5, META | SHIFT, 0, "Shift+Win+5"),
        (Qt.Key.Key_Space, CTRL | ALT, 0, "Ctrl+Alt+Space"),
        (Qt.Key.Key_Print, CTRL, 0, "Ctrl+PrintScreen"),
        (Qt.Key.Key_Left, CTRL | ALT, 0, "Ctrl+Alt+Left"),
        (Qt.Key.Key_PageDown, ALT, 0, "Alt+PageDown"),
        # AltGr layouts: Qt reports another character but the native VK is still 'S' (0x53)
        (0x15B, CTRL | ALT, 0x53, "Ctrl+Alt+S"),  # U+015B, Polish s-acute
        (Qt.Key.Key_Control, CTRL, 0, None),
        (Qt.Key.Key_Shift, SHIFT, 0, None),
        (Qt.Key.Key_Alt, ALT, 0, None),
        (Qt.Key.Key_Meta, META, 0, None),
        (Qt.Key.Key_Exclam, CTRL, 0, None),
    ],
)
def test_hotkey_from_key_event(key, mods, native, expected):
    assert hotkey_from_key_event(int(key), mods, native) == expected


def test_hotkey_edit_records_a_combination(qapp):
    edit = HotkeyEdit("Ctrl+Alt+S")
    got: list[str] = []
    edit.hotkey_changed.connect(got.append)
    key_press(edit, Qt.Key.Key_Control, CTRL)  # bare modifiers are ignored
    key_press(edit, Qt.Key.Key_K, CTRL | ALT | SHIFT, "k")
    assert edit.value() == "Ctrl+Alt+Shift+K"
    assert got == ["Ctrl+Alt+Shift+K"]


def test_hotkey_edit_escape_restores_and_tab_is_not_swallowed(qapp):
    edit = HotkeyEdit("Ctrl+Alt+S")
    edit.set_value("Ctrl+Alt+S")
    key_press(edit, Qt.Key.Key_K, CTRL | ALT | SHIFT)
    assert edit.value() == "Ctrl+Alt+Shift+K"
    ev = key_press(edit, Qt.Key.Key_Escape)
    assert ev.isAccepted() and edit.value() == "Ctrl+Alt+S"
    assert not key_press(edit, Qt.Key.Key_Tab).isAccepted()  # lets focus move on
    assert not key_press(edit, Qt.Key.Key_Return).isAccepted()  # the dialog's default button
    key_press(edit, Qt.Key.Key_Backspace)
    assert edit.value() == "Ctrl+Alt+S"  # cannot be cleared by accident


# ---------------------------------------------------------------------------
# settings window
# ---------------------------------------------------------------------------
@pytest.fixture
def store(test_out):
    st = SettingsStore(test_out / "settings.json")
    st.settings.output_root = str(test_out / "reports")
    st.settings.last_project_path = r"C:\dev\app"
    st.settings.last_framework_hint = "React + Tailwind"
    return st


def test_settings_window_shows_the_stored_values(qapp, store):
    w = SettingsWindow(store)
    assert w.capture_edit.value() == "Ctrl+Alt+S" and w.delayed_edit.value() == "Ctrl+Alt+D"
    assert w.delay_spin.value() == 3
    assert (w.delay_spin.minimum(), w.delay_spin.maximum()) == (1, 30)
    assert w.output_edit.text() == store.settings.output_root
    assert w.project_edit.text() == r"C:\dev\app" and w.framework_edit.text() == "React + Tailwind"
    assert w.autostart_check.isChecked() is False  # "Start with Windows" defaults to off
    assert w.current_settings() == store.settings
    assert w.ok_button.isEnabled()


def test_settings_window_edits_the_text_copied_on_finish(qapp, store):
    w = SettingsWindow(store)
    assert w.copy_text_edit.toPlainText() == ""  # empty = the built-in Claude prompt
    assert "{shots}" in w.copy_text_help.text() and "{report}" in w.copy_text_help.text()
    assert "I captured" in w.copy_text_edit.placeholderText()
    w.copy_text_edit.setPlainText("  Bug report: {shots}\nSee {folder}  ")
    assert w.current_settings().copy_text_template == "Bug report: {shots}\nSee {folder}"
    w.copy_text_default.click()
    assert w.copy_text_edit.toPlainText() == "" and w.current_settings().copy_text_template == ""


def test_settings_window_altgr_note_is_soft(qapp, store):
    w = SettingsWindow(store)
    assert not w.capture_note.isHidden()  # Ctrl+Alt+S is AltGr+S on Polish/German layouts
    assert "AltGr" in w.capture_note.text()
    assert w.ok_button.isEnabled()  # a warning, not an error
    w.capture_edit.set_value("Ctrl+Shift+F9")
    w.capture_edit.hotkey_changed.emit("Ctrl+Shift+F9")
    assert w.capture_note.isHidden()


def test_settings_window_invalid_hotkeys_block_ok(qapp, store):
    w = SettingsWindow(store)
    key_press(w.capture_edit, Qt.Key.Key_F5, SHIFT)  # Shift alone is not enough
    assert w.capture_edit.value() == "Shift+F5"
    assert not w.capture_note.isHidden() and "Ctrl, Alt or Win" in w.capture_note.text()
    assert not w.ok_button.isEnabled() and not w.apply_button.isEnabled()
    key_press(w.capture_edit, Qt.Key.Key_S, META | SHIFT)  # Win+Shift+S is Snipping Tool
    assert w.capture_edit.value() == "Shift+Win+S"
    assert "reserved" in w.capture_note.text()
    assert not w.ok_button.isEnabled()
    key_press(w.capture_edit, Qt.Key.Key_F8, CTRL | SHIFT)
    assert w.ok_button.isEnabled()


def test_settings_window_duplicate_hotkeys_and_bad_folder_block_ok(qapp, store):
    w = SettingsWindow(store)
    key_press(w.delayed_edit, Qt.Key.Key_S, CTRL | ALT)
    assert not w.ok_button.isEnabled()
    assert "different" in w.status_label.text() and not w.status_label.isHidden()
    key_press(w.delayed_edit, Qt.Key.Key_D, CTRL | ALT)
    assert w.ok_button.isEnabled() and w.status_label.isHidden()
    w.output_edit.setText("   ")
    assert not w.ok_button.isEnabled() and "Output folder" in w.status_label.text()


def test_settings_window_ok_emits_applied_with_the_widget_values_and_closes(qapp, store):
    w = SettingsWindow(store)
    got: list[Settings] = []
    w.applied.connect(got.append)
    w.show()
    key_press(w.capture_edit, Qt.Key.Key_F9, CTRL | SHIFT)
    key_press(w.delayed_edit, Qt.Key.Key_F10, CTRL | SHIFT)
    w.delay_spin.setValue(7)
    w.output_edit.setText(r"D:\Reports")
    w.project_edit.setText(r"D:\code\thing")
    w.framework_edit.setText("WPF")
    w.autostart_check.setChecked(True)
    w.ok_button.click()
    assert len(got) == 1
    s = got[0]
    assert (s.hotkey_capture, s.hotkey_delayed) == ("Ctrl+Shift+F9", "Ctrl+Shift+F10")
    assert s.delay_seconds == 7 and s.output_root == r"D:\Reports"
    assert s.last_project_path == r"D:\code\thing" and s.last_framework_hint == "WPF"
    assert s.start_with_windows is True
    assert s.problems() == []
    assert not w.isVisible() and w.result() == 1  # accepted
    assert store.settings.hotkey_capture == "Ctrl+Alt+S"  # the window itself never saves anything


def test_settings_window_stays_open_when_the_controller_reports_an_error(qapp, store):
    w = SettingsWindow(store)
    w.applied.connect(lambda s: w.show_error("Ctrl+Shift+F9 is already used by another program"))
    w.show()
    key_press(w.capture_edit, Qt.Key.Key_F9, CTRL | SHIFT)
    w.ok_button.click()
    assert w.isVisible()
    assert "already used by another program" in w.status_label.text()
    assert not w.status_label.isHidden()
    key_press(w.capture_edit, Qt.Key.Key_F8, CTRL | SHIFT)  # editing clears the stale error
    assert "already used" not in w.status_label.text()


def test_settings_window_apply_keeps_open_and_cancel_emits_nothing(qapp, store):
    w = SettingsWindow(store)
    got: list[Settings] = []
    w.applied.connect(got.append)
    w.show()
    w.delay_spin.setValue(9)
    w.apply_button.click()
    assert len(got) == 1 and got[0].delay_seconds == 9
    assert w.isVisible() and "applied" in w.status_label.text().lower()
    w.cancel_button.click()
    assert len(got) == 1 and not w.isVisible()
    assert store.settings.delay_seconds == 3


def test_settings_window_ok_does_nothing_while_invalid(qapp, store):
    w = SettingsWindow(store)
    got = []
    w.applied.connect(got.append)
    w.show()
    key_press(w.capture_edit, Qt.Key.Key_F5, SHIFT)
    w._emit_apply(close=True)  # the disabled button cannot be clicked, so call the slot
    assert got == [] and w.isVisible()


def test_settings_window_default_buttons_reset_the_hotkeys(qapp, store):
    w = SettingsWindow(store)
    key_press(w.capture_edit, Qt.Key.Key_F9, CTRL | SHIFT)
    key_press(w.delayed_edit, Qt.Key.Key_F10, CTRL | SHIFT)
    w.capture_default.click()
    w.delayed_default.click()
    assert (w.capture_edit.value(), w.delayed_edit.value()) == ("Ctrl+Alt+S", "Ctrl+Alt+D")


def test_settings_window_reports_hotkey_recording(qapp, store, monkeypatch):
    w = SettingsWindow(store)
    seen: list[bool] = []
    w.hotkey_recording.connect(seen.append)
    focused = {"capture": False, "delayed": False}
    monkeypatch.setattr(w.capture_edit, "hasFocus", lambda: focused["capture"])
    monkeypatch.setattr(w.delayed_edit, "hasFocus", lambda: focused["delayed"])
    focused["capture"] = True
    w.capture_edit.recording_changed.emit(True)
    pump(20)
    assert seen == [True]
    # focus jumping from one hotkey field to the other does not flap the state
    focused["capture"], focused["delayed"] = False, True
    w.capture_edit.recording_changed.emit(False)
    w.delayed_edit.recording_changed.emit(True)
    pump(20)
    assert seen == [True]
    focused["delayed"] = False
    w.delayed_edit.recording_changed.emit(False)
    pump(20)
    assert seen == [True, False]
    # closing the dialog while recording releases the state
    focused["capture"] = True
    w.capture_edit.recording_changed.emit(True)
    pump(20)
    w.reject()
    assert seen[-1] is False
