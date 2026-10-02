"""AppController driving package B's REAL EditorWindow (offscreen), with fake capture/hotkeys/tray/toast.

If EditorWindow is still a stub in a checkout, these tests skip with a clear message.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication

from uireport.app import controller as controller_mod
from uireport.app.controller import AppController
from uireport.app.fakes import FakeCapture, FakeHotkeys, FakeSettingsWindow, FakeToast, FakeTray, MemoryAutostart
from uireport.models import CaptureMode, PinAnn, Session
from uireport.settings import SettingsStore


def wait_until(cond, timeout: float = 20.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return bool(cond())


@pytest.fixture
def rig(qapp, test_out, monkeypatch):
    try:
        from uireport.editor import EditorWindow

        EditorWindow(Session()).deleteLater()  # raises NotImplementedError while B is a stub
    except NotImplementedError:
        pytest.skip("package B's EditorWindow is still a stub")
    monkeypatch.setattr(controller_mod, "HIDE_SETTLE_MS", 25)
    store = SettingsStore(test_out / "settings.json")
    store.settings.output_root = str(test_out / "reports")
    r = SimpleNamespace(store=store, finished=[], failed=[], opened=[])
    r.capture, r.hotkeys, r.tray, r.toast = FakeCapture(), FakeHotkeys(), FakeTray(), FakeToast()
    r.controller = AppController(
        store,
        capture=r.capture,
        hotkeys=r.hotkeys,
        tray=r.tray,
        toast=r.toast,
        settings_window_factory=FakeSettingsWindow,
        autostart=MemoryAutostart(),
        confirm_quit=lambda n: "cancel",
        open_path=r.opened.append,
    )
    r.controller.session_finished.connect(r.finished.append)
    r.controller.finish_failed.connect(r.failed.append)
    r.controller.start()
    yield r
    r.controller.shutdown()
    if r.controller.editor is not None:
        r.controller.editor.deleteLater()


def capture_n(r, n, make_shot):
    shots = []
    for i in range(n):
        shot = make_shot(200 + i, 120 + i, 150 if i == 1 else 100, caption=f"real editor shot {i + 1}")
        shot.add_annotation(PinAnn(x=20, y=20, note="n", color="#112233"))
        r.capture.finish_with(shot)
        shots.append(shot)
    return shots


def test_real_editor_shows_each_captured_shot(rig, make_shot):
    assert rig.controller.editor is None
    shots = capture_n(rig, 3, make_shot)
    ed = rig.controller.editor
    assert ed is not None and ed.isVisible()
    assert ed.current_shot_id() == shots[-1].id
    assert rig.tray.count == 3
    assert rig.controller.session.shots == shots


def test_real_editor_next_hides_it_then_cancel_brings_back_the_same_shot(rig, make_shot):
    shots = capture_n(rig, 3, make_shot)
    ed = rig.controller.editor
    ed.show_shot(shots[1].id)
    assert ed.current_shot_id() == shots[1].id

    ed.next_requested.emit()  # what Ctrl+Enter does
    assert not ed.isVisible()  # hidden right away so it is not in the grab
    assert wait_until(lambda: rig.capture.started == [CaptureMode.NORMAL])

    rig.capture.finish_cancelled()  # Esc in the selector
    assert ed.isVisible() and ed.current_shot_id() == shots[1].id
    assert len(rig.controller.session.shots) == 3

    ed.next_delayed_requested.emit()
    assert wait_until(lambda: rig.capture.started == [CaptureMode.NORMAL, CaptureMode.DELAYED])
    rig.capture.finish_cancelled()  # Esc during the countdown
    assert ed.isVisible() and ed.current_shot_id() == shots[1].id


def test_real_editor_closed_with_x_only_hides_and_show_session_reopens(rig, make_shot):
    shots = capture_n(rig, 2, make_shot)
    ed = rig.controller.editor
    ed.close()  # the X button
    assert not ed.isVisible() and len(rig.controller.session.shots) == 2
    rig.tray.show_session_requested.emit()
    assert ed.isVisible() and ed.current_shot_id() == shots[-1].id
    # not visible -> the next capture starts synchronously
    ed.hide()
    rig.controller.request_capture(CaptureMode.NORMAL)
    assert rig.capture.started == [CaptureMode.NORMAL]


def test_real_editor_finish_and_discard(rig, make_shot, test_out):
    capture_n(rig, 3, make_shot)
    ed = rig.controller.editor
    old_session = rig.controller.session
    old_session.goal = "real editor finish"
    ed.session_changed.emit()

    ed.finish_requested.emit()  # Ctrl+Shift+Enter
    assert not ed.isVisible()
    assert wait_until(lambda: rig.finished or rig.failed)
    assert rig.failed == [] and len(rig.finished) == 1
    assert len(list(rig.finished[0].folder.glob("*.png"))) == 6
    assert rig.toast.last["message"] == "Copied — paste into Claude"
    fresh = rig.controller.session
    assert fresh is not old_session and fresh.shots == []
    assert ed.current_shot_id() is None  # the editor is bound to the new, empty session
    assert not ed.isVisible()

    capture_n(rig, 2, make_shot)  # the same editor is reused for the next session
    assert rig.controller.editor is ed and ed.isVisible()
    ed.discard_session_requested.emit()
    assert rig.controller.session.shots == [] and not ed.isVisible() and rig.tray.count == 0
