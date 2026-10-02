"""AppController flows F1-F16 with fake collaborators (package C). Offscreen Qt only.

FakeCapture / FakeEditor / FakeHotkeys / FakeTray / FakeToast / FakeSettingsWindow /
MemoryAutostart live in uireport.app.fakes; they have exactly the signals and methods of the
real classes, so the integration agent can swap them for the real ones.
"""
from __future__ import annotations

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QGuiApplication

from uireport.app import controller as controller_mod
from uireport.app.controller import AppController
from uireport.app.fakes import (
    FakeCapture,
    FakeEditor,
    FakeHotkeys,
    FakeSettingsWindow,
    FakeToast,
    FakeTray,
    MemoryAutostart,
)
from uireport.hotkeyspec import NAME_CAPTURE, NAME_DELAYED
from uireport.models import CaptureMode, PinAnn, RectAnn, RulerAnn
from uireport.output.clipboard import build_prompt
from uireport.output.writer import write_session as real_write_session
from uireport.settings import SettingsStore

COPIED = "Copied \u2014 paste into Claude"


def pump(ms: int = 0) -> None:
    end = time.monotonic() + ms / 1000.0
    while True:
        QCoreApplication.processEvents()
        if time.monotonic() >= end:
            break
        time.sleep(0.005)


def wait_until(cond, timeout: float = 20.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return bool(cond())


@pytest.fixture
def make_rig(qapp, test_out, monkeypatch):
    """Factory for a controller wired to fakes. Everything writes under test_out."""
    monkeypatch.setattr(controller_mod, "HIDE_SETTLE_MS", 25)  # keep tests fast, semantics unchanged
    created: list[AppController] = []

    def factory(
        *,
        hotkeys_kwargs=None,
        hotkeys=None,
        choice="cancel",
        enable_hotkeys=True,
        output_root=None,
        real_editor_visible_check=True,
    ) -> SimpleNamespace:
        store = SettingsStore(test_out / "settings.json")
        store.settings.output_root = str(test_out / "reports")
        r = SimpleNamespace()
        r.test_out = test_out
        r.store = store
        r.events = []
        r.opened = []
        r.confirm_calls = []
        r.choice = choice
        r.editors = []
        r.windows = []
        r.finished = []
        r.failed = []
        r.quit_signals = []
        r.capture = FakeCapture()
        r.hotkeys = hotkeys if hotkeys is not None else FakeHotkeys(**(hotkeys_kwargs or {}))
        r.tray = FakeTray()
        r.toast = FakeToast()
        r.autostart = MemoryAutostart()

        real_start = r.capture.start

        def start(mode):
            r.events.append(("start", CaptureMode(mode)))
            return real_start(mode)

        r.capture.start = start

        def editor_factory(session):
            ed = FakeEditor(session)
            real_hide = ed.hide_for_capture

            def hide_for_capture():
                r.events.append(("hide_for_capture",))
                real_hide()

            ed.hide_for_capture = hide_for_capture
            r.editors.append(ed)
            return ed

        def window_factory(st):
            win = FakeSettingsWindow(st)
            r.windows.append(win)
            return win

        def confirm(n):
            r.confirm_calls.append(n)
            return r.choice

        c = AppController(
            store,
            capture=r.capture,
            hotkeys=r.hotkeys,
            editor_factory=editor_factory,
            tray=r.tray,
            toast=r.toast,
            settings_window_factory=window_factory,
            autostart=r.autostart,
            enable_hotkeys=enable_hotkeys,
            output_root=output_root,
            confirm_quit=confirm,
            open_path=r.opened.append,
        )
        c.session_finished.connect(r.finished.append)
        c.finish_failed.connect(r.failed.append)
        c.quit_requested.connect(lambda: r.quit_signals.append(True))
        r.controller = c
        created.append(c)
        return r

    yield factory
    for c in created:
        c.shutdown()


def add_shots(r, n: int, make_shot, *, size=(160, 100)) -> list:
    shots = []
    for i in range(n):
        shot = make_shot(size[0] + i, size[1] + i, 150 if i % 4 == 1 else 100, caption=f"caption {i + 1}")
        shot.add_annotation(PinAnn(x=10 + i, y=10, note=f"note {i + 1}", color="#112233"))
        r.capture.finish_with(shot)
        shots.append(shot)
    return shots


# ---------------------------------------------------------------------------
# start / hotkeys / F1 F2 F11
# ---------------------------------------------------------------------------
def test_start_shows_tray_and_registers_both_hotkeys(make_rig):
    r = make_rig()
    r.controller.start()
    assert r.tray.shown and r.tray.count == 0
    assert r.tray.hints == ("Ctrl+Alt+S", "Ctrl+Alt+D")
    assert r.hotkeys.started
    assert r.hotkeys.registered == {NAME_CAPTURE: "Ctrl+Alt+S", NAME_DELAYED: "Ctrl+Alt+D"}
    assert r.toast.shown == []  # no noise at startup
    r.controller.start()  # idempotent
    assert len(r.hotkeys.set_calls) == 1


def test_start_welcome_toast_only_when_asked(make_rig):
    r = make_rig()
    r.controller.start(welcome=True)
    assert "Ctrl+Alt+S" in r.toast.last["message"] and not r.toast.last["error"]


def test_no_hotkeys_mode_never_touches_the_hotkey_manager(make_rig):
    r = make_rig(enable_hotkeys=False)
    r.controller.start()
    assert not r.hotkeys.started and r.hotkeys.set_calls == []
    assert r.tray.shown


def test_capture_starts_synchronously_when_the_editor_is_not_visible(make_rig):
    r = make_rig()
    r.controller.start()
    r.controller.request_capture(CaptureMode.NORMAL)
    # no processEvents, no timer: the region selector opens in the same call stack
    assert r.capture.started == [CaptureMode.NORMAL]
    assert not r.controller._settle_timer.isActive()
    assert r.events == [("start", CaptureMode.NORMAL)]


def test_hotkey_activation_is_queued_to_the_gui_thread_then_starts_at_once(make_rig):
    r = make_rig()
    r.controller.start()
    r.hotkeys.press(NAME_CAPTURE)
    assert r.capture.started == []  # queued connection: not run inside emit()
    pump()
    assert r.capture.started == [CaptureMode.NORMAL]
    assert not r.controller._settle_timer.isActive()  # and no delay was used


def test_hotkey_activations_from_another_thread_are_handled_on_the_gui_thread(make_rig):
    import threading

    r = make_rig()
    r.controller.start()
    seen: list[int] = []
    real_start = r.capture.start

    def start(mode):
        seen.append(threading.get_ident())
        return real_start(mode)

    r.capture.start = start
    worker = threading.Thread(target=lambda: r.hotkeys.activated.emit(NAME_CAPTURE), name="hotkey-thread")
    worker.start()
    worker.join()
    assert wait_until(lambda: seen, 5)
    assert seen == [threading.main_thread().ident]  # never on the hotkey thread
    assert r.capture.started == [CaptureMode.NORMAL]


def test_delayed_hotkey_tray_and_editor_button_start_a_delayed_capture(make_rig, make_shot):
    r = make_rig()
    r.controller.start()
    r.hotkeys.press(NAME_DELAYED)
    pump()
    assert r.capture.started == [CaptureMode.DELAYED]
    r.capture.finish_cancelled()
    r.tray.delayed_requested.emit()
    assert r.capture.started == [CaptureMode.DELAYED, CaptureMode.DELAYED]
    r.capture.finish_cancelled()
    r.tray.capture_requested.emit()
    assert r.capture.started[-1] == CaptureMode.NORMAL


def test_unknown_hotkey_name_is_ignored(make_rig):
    r = make_rig()
    r.controller.start()
    r.hotkeys.press("bogus")
    pump()
    assert r.capture.started == []


def test_visible_editor_is_hidden_then_capture_starts_after_the_settle_time(make_rig, make_shot, monkeypatch):
    flushes = []
    monkeypatch.setattr(controller_mod, "_dwm_flush", lambda: flushes.append(1))
    r = make_rig()
    r.controller.start()
    add_shots(r, 1, make_shot)
    assert r.editors[0].visible
    r.events.clear()
    r.toast.dismissed = 0

    r.hotkeys.press(NAME_CAPTURE)  # F11: hotkey while the editor is open acts like Next
    pump()
    assert r.events == [("hide_for_capture",)]  # hidden, but not started yet
    assert r.capture.started == []
    assert flushes == [1]
    assert r.controller._settle_timer.isActive()
    assert wait_until(lambda: r.capture.started)
    assert r.events == [("hide_for_capture",), ("start", CaptureMode.NORMAL)]
    assert not r.editors[0].visible
    assert r.toast.dismissed >= 1  # a capture start dismisses the toast


def test_editor_next_buttons_run_the_same_path(make_rig, make_shot):
    r = make_rig()
    r.controller.start()
    add_shots(r, 1, make_shot)
    r.editors[0].next_requested.emit()
    assert wait_until(lambda: r.capture.started == [CaptureMode.NORMAL])
    r.capture.finish_cancelled()
    assert r.editors[0].visible  # F4: back to the shot
    r.editors[0].next_delayed_requested.emit()
    assert wait_until(lambda: r.capture.started == [CaptureMode.NORMAL, CaptureMode.DELAYED])


def test_requests_are_ignored_while_a_capture_is_active_or_settling(make_rig, make_shot):
    r = make_rig()
    r.controller.start()
    r.controller.request_capture(CaptureMode.NORMAL)
    r.toast.dismissed = 0
    r.controller.request_capture(CaptureMode.DELAYED)
    r.hotkeys.press(NAME_CAPTURE)
    pump()
    assert r.capture.started == [CaptureMode.NORMAL]
    assert r.toast.dismissed == 0  # ignored entirely
    r.capture.finish_cancelled()

    add_shots(r, 1, make_shot)
    r.controller.request_capture(CaptureMode.NORMAL)  # editor visible -> settling
    r.controller.request_capture(CaptureMode.DELAYED)  # ignored while settling
    assert wait_until(lambda: len(r.capture.started) == 2)
    pump(120)
    assert r.capture.started == [CaptureMode.NORMAL, CaptureMode.NORMAL]


def test_capture_start_failure_shows_an_error_and_restores_the_editor(make_rig, make_shot):
    r = make_rig()
    r.controller.start()
    add_shots(r, 1, make_shot)

    def boom(mode):
        raise RuntimeError("no screen")

    r.capture.start = boom
    r.controller.request_capture(CaptureMode.NORMAL)
    assert wait_until(lambda: r.toast.last and r.toast.last["error"])
    assert "no screen" in r.toast.last["message"]
    assert r.editors[0].visible  # session and editor are back


# ---------------------------------------------------------------------------
# F3 captured / F4 cancelled / F5 failed
# ---------------------------------------------------------------------------
def test_captured_shot_is_added_and_the_editor_shows_it(make_rig, make_shot):
    r = make_rig()
    r.controller.start()
    assert r.editors == []  # created lazily
    shots = add_shots(r, 1, make_shot)
    s = r.controller.session
    assert s.shots == shots and shots[0].index == 1
    assert len(r.editors) == 1
    ed = r.editors[0]
    assert ed.names() == ["refresh", "show_shot", "present"]
    assert ed.calls[1] == ("show_shot", shots[0].id)
    assert ed.calls[2] == ("present", shots[0].monitor)
    assert ed.visible and ed.session is s
    assert r.tray.count == 1
    add_shots(r, 2, make_shot)
    assert len(r.editors) == 1  # reused
    assert [x.index for x in s.shots] == [1, 2, 3] and r.tray.count == 3
    assert ed.current == s.shots[-1].id


@pytest.mark.parametrize("mode", [CaptureMode.NORMAL, CaptureMode.DELAYED])
def test_cancel_with_an_empty_session_stays_idle_in_the_tray(make_rig, mode):
    r = make_rig()
    r.controller.start()
    r.controller.request_capture(mode)
    assert r.capture.is_active
    r.capture.finish_cancelled()  # Esc in the selector / countdown
    assert not r.capture.is_active
    assert r.editors == []  # nothing to show
    assert len(r.controller.session.shots) == 0 and r.tray.count == 0
    r.controller.request_capture(mode)  # the next hotkey works immediately
    assert r.capture.started == [mode, mode]


@pytest.mark.parametrize("mode", [CaptureMode.NORMAL, CaptureMode.DELAYED])
def test_cancel_in_the_middle_of_a_session_keeps_everything(make_rig, make_shot, mode):
    r = make_rig()
    r.controller.start()
    shots = add_shots(r, 3, make_shot)
    r.editors[0].show_shot(shots[1].id)  # the user is re-editing shot 2
    before = list(r.controller.session.shots)
    if mode == CaptureMode.NORMAL:
        r.hotkeys.press(NAME_CAPTURE)
    else:
        r.hotkeys.press(NAME_DELAYED)
    assert wait_until(lambda: r.capture.started == [mode])
    assert not r.editors[0].visible
    r.capture.finish_cancelled()
    ed = r.editors[0]
    assert r.controller.session.shots == before and r.tray.count == 3
    assert ed.visible and ed.current == shots[1].id  # exactly the shot that was open
    assert ed.calls[-1] == ("present", shots[1].monitor)
    assert not r.capture.is_active
    r.hotkeys.press(NAME_CAPTURE)
    assert wait_until(lambda: len(r.capture.started) == 2)  # the next capture works at once


def test_cancel_falls_back_to_the_last_shot_when_the_open_one_was_deleted(make_rig, make_shot):
    r = make_rig()
    r.controller.start()
    shots = add_shots(r, 3, make_shot)
    r.editors[0].show_shot(shots[0].id)
    r.controller.request_capture(CaptureMode.NORMAL)
    assert wait_until(lambda: r.capture.started)
    r.controller.session.remove_shot(shots[0].id)
    r.capture.finish_cancelled()
    assert r.editors[0].current == shots[2].id


def test_capture_failure_shows_an_error_toast_and_restores_the_session(make_rig, make_shot):
    r = make_rig()
    r.controller.start()
    add_shots(r, 2, make_shot)
    r.controller.request_capture(CaptureMode.NORMAL)
    assert wait_until(lambda: r.capture.started)
    r.capture.finish_failed("BitBlt failed")
    assert r.toast.last["error"] and "BitBlt failed" in r.toast.last["message"]
    assert r.editors[0].visible and len(r.controller.session.shots) == 2
    # empty session: only the toast
    r2 = make_rig()
    r2.controller.start()
    r2.controller.request_capture(CaptureMode.NORMAL)
    r2.capture.finish_failed("x")
    assert r2.toast.last["error"] and r2.editors == []


# ---------------------------------------------------------------------------
# F7 finish
# ---------------------------------------------------------------------------
def test_finish_with_no_shots_is_a_noop(make_rig):
    r = make_rig()
    r.controller.start()
    r.controller.finish_session()
    pump(50)
    assert r.toast.shown == [] and r.finished == [] and not r.controller.is_finishing


def test_finish_a_13_shot_session_after_reorder_delete_and_reedit(make_rig, make_shot, test_out):
    r = make_rig()
    r.controller.start()
    add_shots(r, 15, make_shot)
    session = r.controller.session
    ed = r.editors[0]
    session.goal = "Fix the clipped Save button"
    session.project_path = r"C:\dev\proj"
    session.framework_hint = "WPF"
    ed.session_changed.emit()

    # the shared Session API, exactly like the session panel does it
    ids = [s.id for s in session.shots]
    session.reorder(list(reversed(ids)))
    ed.session_changed.emit()
    session.move_shot(ids[0], 0)  # the (now last) first capture to the front
    session.remove_shot(ids[3])
    session.remove_shot(ids[7])
    ed.session_changed.emit()
    assert len(session.shots) == 13 and r.tray.count == 13
    # re-edit: caption, role and annotations of an old shot
    old = session.get_shot(ids[5])
    ed.show_shot(old.id)
    old.caption = "re-edited caption \u2014 \u017c\u00f3\u0142w"
    old.add_annotation(RectAnn(x=5, y=5, w=30, h=20))
    old.add_annotation(RulerAnn(x1=10, y1=40, x2=110, y2=40))
    ed.session_changed.emit()
    order_at_finish = [s.id for s in session.shots]
    QGuiApplication.clipboard().setText("sentinel")

    ed.finish_requested.emit()
    assert r.controller.is_finishing
    assert not ed.visible  # hidden while saving
    assert r.toast.last["message"].startswith("Saving")
    assert wait_until(lambda: r.finished or r.failed)
    assert r.failed == [] and len(r.finished) == 1
    out = r.finished[0]

    # files
    names = {p.name for p in out.folder.iterdir()}
    expected = {"report.md", "report.json"}
    for i in range(1, 14):
        expected |= {f"{i:02d}.png", f"{i:02d}_annotated.png"}
    assert names == expected
    assert out.folder.parent == test_out / "reports"
    assert (test_out / "reports" / "latest.json").is_file()
    doc = json.loads(out.report_json.read_text(encoding="utf-8"))
    assert [s["id"] for s in doc["shots"]] == order_at_finish
    assert doc["session"]["goal"] == "Fix the clipped Save button"
    assert doc["session"]["folder"] == str(out.folder)
    assert doc["session"]["system"]["tool_version"]  # collect_system_meta (or its fallback) ran
    edited = doc["shots"][order_at_finish.index(old.id)]
    assert edited["caption"].startswith("re-edited caption")
    assert [a["type"] for a in edited["annotations"]] == ["pin", "rect", "ruler"]
    md = out.report_md.read_text(encoding="utf-8")
    assert "13 screenshots" in md and "re-edited caption" in md

    # clipboard prompt and toast
    prompt = QGuiApplication.clipboard().text()
    assert prompt == build_prompt(13, "Fix the clipped Save button", out.report_md)
    assert prompt.startswith("I captured 13 screenshots. Goal: Fix the clipped Save button. Read ")
    assert r.toast.last["message"] == COPIED
    assert r.toast.last["action_text"] == "Open folder" and not r.toast.last["error"]
    r.toast.click_action()
    assert r.opened == [str(out.folder)]

    # fresh session, pre-filled from the remembered values; editor reset and hidden
    fresh = r.controller.session
    assert fresh is not session and fresh.shots == []
    assert fresh.project_path == r"C:\dev\proj" and fresh.framework_hint == "WPF"
    assert ed.calls[-1][0] == "set_session" and ed.session is fresh
    assert not ed.visible
    assert r.tray.count == 0
    saved = json.loads(r.store.path.read_text(encoding="utf-8"))
    assert saved["last_project_path"] == r"C:\dev\proj" and saved["last_framework_hint"] == "WPF"
    assert not r.controller.is_finishing
    # and the tool keeps working
    r.controller.request_capture(CaptureMode.NORMAL)
    assert r.capture.started == [CaptureMode.NORMAL]


def test_capture_requests_are_ignored_while_saving(make_rig, make_shot):
    r = make_rig()
    r.controller.start()
    add_shots(r, 2, make_shot)
    r.controller.finish_session()
    assert r.controller.is_finishing
    r.controller.request_capture(CaptureMode.NORMAL)
    r.hotkeys.press(NAME_CAPTURE)
    r.controller.discard_session()  # also refused: the writer is reading the session
    assert len(r.controller.session.shots) == 2
    assert wait_until(lambda: r.finished)
    assert r.capture.started == []


def test_finish_failure_keeps_the_session_and_reopens_the_editor(make_rig, make_shot, monkeypatch, test_out):
    r = make_rig()
    r.controller.start()
    shots = add_shots(r, 4, make_shot)
    r.controller.session.goal = "keep me"
    QGuiApplication.clipboard().setText("sentinel")

    def boom(session, root, progress=None):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(controller_mod, "write_session", boom)
    r.editors[0].finish_requested.emit()
    assert wait_until(lambda: r.failed)
    assert "disk full" in r.failed[0]
    assert r.finished == []
    s = r.controller.session
    assert s.shots == shots and s.goal == "keep me"  # no data loss
    assert r.tray.count == 4
    assert r.toast.last["error"] and "disk full" in r.toast.last["message"]
    assert r.editors[0].visible  # back in front so the user can retry
    assert not r.controller.is_finishing
    assert QGuiApplication.clipboard().text() == "sentinel"  # nothing is copied for a failed save

    # retry works
    monkeypatch.setattr(controller_mod, "write_session", real_write_session)
    r.editors[0].finish_requested.emit()
    assert wait_until(lambda: r.finished)
    assert len(list(r.finished[0].folder.glob("*.png"))) == 8
    assert r.controller.session.shots == []


def test_finish_copies_the_users_own_text_when_a_template_is_set(make_rig, make_shot, test_out):
    r = make_rig()
    r.store.settings.copy_text_template = "Hey, {shots} for you: {report}"
    r.controller.start()
    add_shots(r, 2, make_shot)
    r.controller.finish_session()
    assert wait_until(lambda: r.finished)
    out = r.finished[0]
    assert QGuiApplication.clipboard().text() == f"Hey, 2 screenshots for you: {out.report_md}"
    assert r.toast.last["message"] == "Copied to the clipboard"
    assert r.toast.last["action_text"] == "Open folder"


def test_finish_uses_the_output_root_override_and_does_not_persist_it(make_rig, make_shot, test_out):
    override = test_out / "override_root"
    r = make_rig(output_root=override)
    r.controller.start()
    add_shots(r, 1, make_shot)
    r.controller.finish_session()
    assert wait_until(lambda: r.finished)
    assert r.finished[0].folder.parent == override
    assert not (test_out / "reports").exists()
    saved = json.loads(r.store.path.read_text(encoding="utf-8"))
    assert saved["output_root"] == str(test_out / "reports")  # the override is for this run only


# ---------------------------------------------------------------------------
# F8 / F9 / F10 / F12
# ---------------------------------------------------------------------------
def test_session_changed_updates_the_tray_and_remembers_project_fields_in_memory(make_rig, make_shot):
    r = make_rig()
    r.controller.start()
    shots = add_shots(r, 3, make_shot)
    ed = r.editors[0]
    r.controller.session.remove_shot(shots[1].id)
    ed.session_changed.emit()
    assert r.tray.count == 2
    r.controller.session.project_path = r"C:\x\y"
    r.controller.session.framework_hint = "Qt"
    ed.session_changed.emit()
    assert r.store.settings.last_project_path == r"C:\x\y"
    assert r.store.settings.last_framework_hint == "Qt"
    assert not r.store.path.exists() or json.loads(r.store.path.read_text())["last_project_path"] != r"C:\x\y"  # not saved yet


def test_closing_the_editor_only_hides_it_and_show_session_brings_it_back(make_rig, make_shot):
    r = make_rig()
    r.controller.start()
    shots = add_shots(r, 3, make_shot)
    ed = r.editors[0]
    ed.show_shot(shots[1].id)
    ed.hide()  # the X button
    assert len(r.controller.session.shots) == 3
    r.controller.request_capture(CaptureMode.NORMAL)  # editor not visible: synchronous start
    assert r.capture.started == [CaptureMode.NORMAL] and ("hide_for_capture",) not in r.events
    r.capture.finish_cancelled()
    r.capture.cancel_calls = 0
    ed.hide()
    r.tray.show_session_requested.emit()
    assert ed.visible and ed.current == shots[1].id and ed.calls[-1][0] == "present"
    r.controller.session.remove_shot(shots[1].id)
    ed.hide()
    r.controller.show_session()
    assert ed.current == shots[2].id  # falls back to the last shot


def test_discard_session_starts_fresh_and_hides_the_editor(make_rig, make_shot):
    r = make_rig()
    r.store.settings.last_project_path = r"C:\remembered"
    r.store.settings.last_framework_hint = "Svelte"
    r.controller.start()
    add_shots(r, 3, make_shot)
    old = r.controller.session
    r.editors[0].discard_session_requested.emit()
    fresh = r.controller.session
    assert fresh is not old and fresh.shots == []
    assert (fresh.project_path, fresh.framework_hint) == (r"C:\remembered", "Svelte")
    ed = r.editors[0]
    assert not ed.visible and ed.session is fresh and ("set_session", fresh) in ed.calls
    assert r.tray.count == 0
    r.controller.request_capture(CaptureMode.NORMAL)
    assert r.capture.started == [CaptureMode.NORMAL]  # synchronous again (editor hidden)


# ---------------------------------------------------------------------------
# F13 settings
# ---------------------------------------------------------------------------
def test_show_settings_reuses_the_open_window_and_creates_a_new_one_after_close(make_rig):
    r = make_rig()
    r.controller.start()
    r.tray.settings_requested.emit()
    assert len(r.windows) == 1 and r.windows[0].visible
    r.controller.show_settings()
    assert len(r.windows) == 1
    r.windows[0].close()
    assert r.controller.settings_window is None
    r.controller.show_settings()
    assert len(r.windows) == 2


def test_applied_settings_are_saved_and_hotkeys_reregistered(make_rig):
    r = make_rig()
    r.controller.start()
    r.controller.show_settings()
    win = r.windows[0]
    new = replace(r.store.settings, hotkey_capture="Ctrl+Shift+F9", delay_seconds=6, start_with_windows=True)
    win.applied.emit(new)
    assert win.errors == []
    assert r.hotkeys.registered == {NAME_CAPTURE: "Ctrl+Shift+F9", NAME_DELAYED: "Ctrl+Alt+D"}
    assert r.store.settings.delay_seconds == 6
    saved = json.loads(r.store.path.read_text(encoding="utf-8"))
    assert saved["hotkey_capture"] == "Ctrl+Shift+F9" and saved["delay_seconds"] == 6
    assert saved["start_with_windows"] is True
    assert r.autostart.calls == [True] and r.autostart.enabled
    assert r.tray.hints == ("Ctrl+Shift+F9", "Ctrl+Alt+D")
    # unchanged autostart flag: the registry is not touched again
    win.applied.emit(replace(r.store.settings, delay_seconds=7))
    assert r.autostart.calls == [True]
    win.applied.emit(replace(r.store.settings, start_with_windows=False))
    assert r.autostart.calls == [True, False] and not r.autostart.enabled


def test_a_taken_hotkey_is_shown_inline_and_the_old_one_stays(make_rig):
    r = make_rig(hotkeys_kwargs={"fail_combos": {"Ctrl+Shift+F9": "already registered by another program"}})
    r.controller.start()
    r.controller.show_settings()
    win = r.windows[0]
    toasts_before = len(r.toast.shown)
    win.applied.emit(replace(r.store.settings, hotkey_capture="Ctrl+Shift+F9"))
    assert len(win.errors) == 1
    assert "Ctrl+Shift+F9" in win.errors[0] and "already registered" in win.errors[0]
    assert r.store.settings.hotkey_capture == "Ctrl+Alt+S"  # what is actually registered
    assert r.hotkeys.registered[NAME_CAPTURE] == "Ctrl+Alt+S"
    assert json.loads(r.store.path.read_text(encoding="utf-8"))["hotkey_capture"] == "Ctrl+Alt+S"
    assert len(r.toast.shown) == toasts_before  # inline only, no duplicate toast
    pump(30)  # the manager's queued registration_failed signal must not add a second report
    assert len(r.toast.shown) == toasts_before


def test_autostart_failure_is_reported_and_the_flag_reverted(make_rig):
    r = make_rig()
    r.controller.start()
    r.controller.show_settings()
    r.autostart.fail_with = PermissionError("registry is locked")
    win = r.windows[0]
    win.applied.emit(replace(r.store.settings, start_with_windows=True))
    assert any("Start with Windows" in e and "registry is locked" in e for e in win.errors)
    assert r.store.settings.start_with_windows is False
    assert json.loads(r.store.path.read_text(encoding="utf-8"))["start_with_windows"] is False


def test_hotkeys_are_released_while_a_hotkey_field_records(make_rig):
    r = make_rig()
    r.controller.start()
    r.controller.show_settings()
    win = r.windows[0]
    win.hotkey_recording.emit(True)
    assert r.hotkeys.registered == {}
    assert r.hotkeys.set_calls[-1] == {NAME_CAPTURE: "", NAME_DELAYED: ""}  # empty text = unbind
    win.hotkey_recording.emit(False)
    assert r.hotkeys.registered == {NAME_CAPTURE: "Ctrl+Alt+S", NAME_DELAYED: "Ctrl+Alt+D"}
    win.hotkey_recording.emit(True)
    win.close()  # closing while recording must not leave the hotkeys off
    assert r.hotkeys.registered == {NAME_CAPTURE: "Ctrl+Alt+S", NAME_DELAYED: "Ctrl+Alt+D"}


# ---------------------------------------------------------------------------
# F14 / F15 / F16
# ---------------------------------------------------------------------------
def test_open_reports_folder_creates_it_first(make_rig, test_out):
    r = make_rig()
    root = test_out / "reports"
    assert not root.exists()
    r.tray.open_folder_requested.emit()
    assert root.is_dir() and r.opened == [str(root)]


def test_open_reports_folder_error_is_a_toast_not_a_crash(make_rig, test_out):
    (test_out / "blocker").write_text("x")
    r = make_rig(output_root=test_out / "blocker" / "sub")
    r.controller.open_reports_folder()
    assert r.opened == [] and r.toast.last["error"]


def test_open_folder_action_failure_is_contained(make_rig, make_shot):
    r = make_rig()
    r.controller.start()
    add_shots(r, 1, make_shot)

    def boom(path):
        raise OSError("explorer missing")

    r.controller._open_path_fn = boom
    r.controller.finish_session()
    assert wait_until(lambda: r.finished)
    r.toast.click_action()
    assert r.toast.last["error"] and "explorer missing" in r.toast.last["message"]


def test_quit_without_shots_asks_nothing_and_shuts_down(make_rig):
    r = make_rig()
    r.controller.start()
    r.tray.quit_requested.emit()
    assert r.confirm_calls == []
    assert r.quit_signals == [True]
    assert r.hotkeys.stopped and r.hotkeys.registered == {} and not r.tray.shown


def test_quit_cancel_keeps_running(make_rig, make_shot):
    r = make_rig(choice="cancel")
    r.controller.start()
    add_shots(r, 2, make_shot)
    r.controller.quit()
    assert r.confirm_calls == [2]
    assert r.quit_signals == [] and r.hotkeys.started and r.tray.shown
    assert len(r.controller.session.shots) == 2
    r.choice = "garbage"  # an unknown answer counts as Cancel
    r.controller.quit()
    assert r.quit_signals == []


def test_quit_discard_and_quit(make_rig, make_shot):
    r = make_rig(choice="discard")
    r.controller.start()
    add_shots(r, 2, make_shot)
    r.controller.quit()
    assert r.quit_signals == [True] and r.hotkeys.stopped and not r.tray.shown
    assert r.finished == []  # nothing was written
    assert not (r.test_out / "reports").exists()
    r.controller.quit()  # idempotent
    assert r.quit_signals == [True]


def test_quit_finish_now_writes_the_report_then_quits(make_rig, make_shot):
    r = make_rig(choice="finish")
    r.controller.start()
    add_shots(r, 2, make_shot)
    r.controller.session.goal = "quit and keep"
    r.controller.quit()
    assert r.confirm_calls == [2]
    assert r.quit_signals == []  # not before the files are written
    assert wait_until(lambda: r.quit_signals)
    assert len(r.finished) == 1 and len(list(r.finished[0].folder.glob("*.png"))) == 4
    assert r.hotkeys.stopped
    assert QGuiApplication.clipboard().text().startswith("I captured 2 screenshots. Goal: quit and keep.")


def test_quit_finish_now_that_fails_does_not_quit(make_rig, make_shot, monkeypatch):
    r = make_rig(choice="finish")
    r.controller.start()
    add_shots(r, 2, make_shot)

    def boom(session, root, progress=None):
        raise OSError("nope")

    monkeypatch.setattr(controller_mod, "write_session", boom)
    r.controller.quit()
    assert wait_until(lambda: r.failed)
    assert r.quit_signals == [] and len(r.controller.session.shots) == 2 and r.hotkeys.started


def test_hotkey_registration_failure_toasts_and_the_app_keeps_running(make_rig, make_shot):
    r = make_rig(hotkeys_kwargs={"fail_combos": {"Ctrl+Alt+S": "already registered by another program"}})
    r.controller.start()
    pump(20)
    errors = [t for t in r.toast.shown if t["error"]]
    assert len(errors) == 1
    assert "Ctrl+Alt+S" in errors[0]["message"] and "already registered" in errors[0]["message"]
    assert r.tray.messages and "Ctrl+Alt+S" in r.tray.messages[0][1]
    assert r.hotkeys.registered == {NAME_DELAYED: "Ctrl+Alt+D"}  # the other one still works
    # the tray menu (and the other hotkey) keep working
    r.tray.capture_requested.emit()
    assert r.capture.started == [CaptureMode.NORMAL]
    r.capture.finish_with(make_shot(50, 50))
    assert len(r.controller.session.shots) == 1


def test_both_hotkeys_failing_still_leaves_a_working_tray(make_rig):
    r = make_rig(hotkeys_kwargs={"fail": {NAME_CAPTURE: "taken", NAME_DELAYED: "taken too"}})
    r.controller.start()
    assert len([t for t in r.toast.shown if t["error"]]) == 2
    assert r.tray.shown
    r.tray.delayed_requested.emit()
    assert r.capture.started == [CaptureMode.DELAYED]


def test_hotkey_thread_failing_to_start_is_not_fatal(make_rig):
    r = make_rig()
    r.hotkeys.start_error = RuntimeError("no message loop")
    r.controller.start()
    assert r.toast.last["error"] and "no message loop" in r.toast.last["message"]
    assert r.tray.shown
    r.controller.request_capture(CaptureMode.NORMAL)
    assert r.capture.started == [CaptureMode.NORMAL]


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------
def test_shutdown_is_idempotent_and_later_events_are_ignored(make_rig, make_shot):
    r = make_rig()
    r.controller.start()
    add_shots(r, 1, make_shot)
    r.controller.request_capture(CaptureMode.NORMAL)
    assert wait_until(lambda: r.capture.is_active)
    r.controller.shutdown()
    r.controller.shutdown()
    assert r.capture.cancel_calls == 1 and not r.capture.is_active
    assert r.hotkeys.stopped and not r.tray.shown and not r.editors[0].visible
    n = len(r.controller.session.shots)
    r.capture.finish_with(make_shot(40, 40))  # e.g. a late result: ignored
    assert len(r.controller.session.shots) == n
    assert not r.editors[0].visible  # the cancel did not pop the editor back up
    r.controller.request_capture(CaptureMode.NORMAL)
    assert len(r.capture.started) == 1


def test_real_defaults_are_used_when_nothing_is_injected(qapp, test_out, monkeypatch):
    """Without injected collaborators the controller builds the real tray/toast (no capture, no
    hotkeys here: those are package A and are injected as fakes)."""
    store = SettingsStore(test_out / "settings.json")
    c = AppController(store, capture=FakeCapture(), enable_hotkeys=False, autostart=MemoryAutostart())
    try:
        from uireport.app.toast import Toast
        from uireport.app.tray import TrayIcon

        assert isinstance(c.tray, TrayIcon) and isinstance(c.toast, Toast)
        assert c.hotkeys is None
        c.start()
        assert c.tray.action_show_session.text() == "Show session (0)"
    finally:
        c.shutdown()


# ---------------------------------------------------------------------------
# the REAL HotkeyManager (package A) over a fake Win32 backend: no real global hotkey is registered
# ---------------------------------------------------------------------------
class FakeWin32Backend:
    """Same tiny interface as capture.hotkeys.Win32HotkeyBackend."""

    ALREADY_REGISTERED = 1409

    def __init__(self, taken=()):
        self.taken = set(taken)  # (modifiers, vk) owned by "another program"
        self.active: dict[int, tuple[int, int]] = {}
        self.callback = None
        self.stopped = 0

    def start(self, on_hotkey):
        self.callback = on_hotkey

    def stop(self):
        self.stopped += 1
        self.active.clear()

    def register(self, hk_id, modifiers, vk):
        key = (modifiers & ~0x4000, vk)  # ignore MOD_NOREPEAT
        if key in self.taken or key in {(m & ~0x4000, v) for m, v in self.active.values()}:
            return self.ALREADY_REGISTERED
        self.active[hk_id] = (modifiers, vk)
        return None

    def unregister(self, hk_id):
        self.active.pop(hk_id, None)

    def active_keys(self):
        return sorted((m & ~0x4000, v) for m, v in self.active.values())


CTRL_ALT = 0x0002 | 0x0001


def test_real_hotkey_manager_registers_dispatches_pauses_and_releases(make_rig):
    import threading

    from uireport.capture.hotkeys import HotkeyManager

    backend = FakeWin32Backend()
    r = make_rig(hotkeys=HotkeyManager(backend=backend))
    r.controller.start()
    assert backend.active_keys() == [(CTRL_ALT, ord("D")), (CTRL_ALT, ord("S"))]

    # a press arrives on the hotkey thread and is handled on the GUI thread
    seen: list[int] = []
    real_start = r.capture.start
    r.capture.start = lambda mode: (seen.append(threading.get_ident()), real_start(mode))[1]
    t = threading.Thread(target=lambda: backend.callback(1))  # id 1 = capture
    t.start()
    t.join()
    assert wait_until(lambda: seen, 5)
    assert seen == [threading.main_thread().ident] and r.capture.started == [CaptureMode.NORMAL]

    # recording in the settings window releases both combinations (the real manager keeps
    # names that are missing from the mapping, so the controller unbinds them with empty texts)
    r.capture.finish_cancelled()
    r.controller.show_settings()
    win = r.windows[0]
    win.hotkey_recording.emit(True)
    assert backend.active_keys() == []
    win.hotkey_recording.emit(False)
    assert backend.active_keys() == [(CTRL_ALT, ord("D")), (CTRL_ALT, ord("S"))]

    # applying a new combination re-registers; a taken one is reported inline and the old one stays
    win.applied.emit(replace(r.store.settings, hotkey_capture="Ctrl+Shift+F9"))
    assert win.errors == [] and (0x0002 | 0x0004, 0x78) in backend.active_keys()
    backend.taken.add((0x0002 | 0x0004, 0x79))  # Ctrl+Shift+F10 belongs to another program
    win.applied.emit(replace(r.store.settings, hotkey_delayed="Ctrl+Shift+F10"))
    assert len(win.errors) == 1 and "Ctrl+Shift+F10" in win.errors[0] and "already in use" in win.errors[0]
    assert r.store.settings.hotkey_delayed == "Ctrl+Alt+D"
    assert (CTRL_ALT, ord("D")) in backend.active_keys()  # the working binding survived

    r.controller.shutdown()
    assert backend.active_keys() == [] and backend.stopped >= 1


def test_real_hotkey_manager_taken_combination_at_startup(make_rig):
    from uireport.capture.hotkeys import HotkeyManager

    backend = FakeWin32Backend(taken={(CTRL_ALT, ord("S"))})
    r = make_rig(hotkeys=HotkeyManager(backend=backend))
    r.controller.start()
    pump(30)
    errors = [t for t in r.toast.shown if t["error"]]
    assert len(errors) == 1  # reported once, not twice (return value + registration_failed signal)
    assert "Ctrl+Alt+S" in errors[0]["message"] and "already in use" in errors[0]["message"]
    assert backend.active_keys() == [(CTRL_ALT, ord("D"))]
    r.tray.capture_requested.emit()
    assert r.capture.started == [CaptureMode.NORMAL]  # the tray menu still works
