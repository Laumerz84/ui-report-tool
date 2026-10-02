"""EditorWindow: focus, shortcuts, roles, live edits, re-edit, reorder / delete, empty state,
session swap, placement. Offscreen; input is sent to our own widgets with QTest only."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QCloseEvent, QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from test_editor_support import Recorder, click, drag, make_session, pixel_center
from uireport.editor import EditorWindow, placement
from uireport.editor.canvas import Tool
from uireport.editor.marks_list import COL_NOTE
from uireport.models import ROLE_COLORS, RectAnn, Role, Session

CTRL = Qt.KeyboardModifier.ControlModifier
ALT = Qt.KeyboardModifier.AltModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
KEYPAD = Qt.KeyboardModifier.KeypadModifier


@pytest.fixture
def windows(qapp):
    made: list[EditorWindow] = []
    yield made
    for w in made:
        w.hide()
        w.deleteLater()
    qapp.processEvents()


@pytest.fixture
def make_editor(qapp, make_shot, windows):
    """make_editor(n=3, present=True) -> (EditorWindow, Session); shot 0 open and caption focused."""

    def _make(n: int = 3, present: bool = True, w: int = 200, h: int = 120):
        sess = make_session(make_shot, n, w, h)
        ed = EditorWindow(sess)
        windows.append(ed)
        ed.refresh()
        if n and present:
            ed.show_shot(sess.shots[0].id)
            ed.present(sess.shots[0].monitor)
            qapp.processEvents()
        return ed, sess

    return _make


def _draw_pin(ed, ix, iy, note=None):
    c = ed.canvas
    c.set_tool(Tool.PIN)
    click(c, *pixel_center(c, ix, iy))
    if note is not None:
        c._note.setText(note)
    c.commit_pending_edit()


# ---------------------------------------------------------------------------
# focus / present
# ---------------------------------------------------------------------------
def test_caption_box_is_focused_after_show_shot_and_present(qapp, make_editor):
    ed, sess = make_editor(2)
    assert ed.isVisible()
    assert QApplication.focusWidget() is ed.caption_edit
    # focus stolen by the canvas, then a new capture arrives: focused again, cursor at the end
    ed.canvas.setFocus()
    sess.shots[1].caption = "second shot"
    ed.show_shot(sess.shots[1].id)
    ed.present(sess.shots[1].monitor)
    qapp.processEvents()
    assert QApplication.focusWidget() is ed.caption_edit
    assert ed.caption_edit.textCursor().position() == len("second shot")


def test_show_shot_alone_focuses_the_caption_of_a_visible_window(qapp, make_editor):
    ed, sess = make_editor(2)
    ed.canvas.setFocus()
    qapp.processEvents()
    ed.show_shot(sess.shots[1].id)
    assert QApplication.focusWidget() is ed.caption_edit


def test_present_is_idempotent_and_restores_a_minimised_window(qapp, make_editor):
    ed, sess = make_editor(1)
    ed.showMinimized()
    qapp.processEvents()
    ed.present(sess.shots[0].monitor)
    qapp.processEvents()
    assert ed.isVisible() and not ed.isMinimized()
    ed.present()
    assert ed.isVisible() and QApplication.focusWidget() is ed.caption_edit


def test_present_places_the_window_inside_the_available_area(qapp, make_editor, monkeypatch):
    ed, sess = make_editor(1, present=False)
    # the offscreen platform has no real fonts, which inflates every text width; allow the layout to shrink
    ed.centralWidget().setMinimumSize(1, 1)
    screen = QGuiApplication.primaryScreen()
    monkeypatch.setattr(placement, "find_screen_for_monitor", lambda m, s=None: screen)
    ed.show_shot(sess.shots[0].id)
    ed.present(sess.shots[0].monitor)
    qapp.processEvents()
    ag = screen.availableGeometry()
    assert ag.contains(ed.frameGeometry())
    assert ed.width() <= max(int(ag.width() * 0.8), 1) + 1 and ed.height() <= int(ag.height() * 0.8) + 1
    assert ed.width() >= 200 and ed.height() >= 200  # resizable, but not collapsed


def test_second_present_keeps_the_users_geometry_on_the_same_screen(qapp, make_editor, monkeypatch):
    ed, sess = make_editor(1, present=False)
    ed.centralWidget().setMinimumSize(1, 1)
    screen = QGuiApplication.primaryScreen()
    monkeypatch.setattr(placement, "find_screen_for_monitor", lambda m, s=None: screen)
    ed.show_shot(sess.shots[0].id)
    ed.present(sess.shots[0].monitor)
    ed.resize(500, 420)
    ed.move(30, 40)
    ed.hide_for_capture()
    ed.present(sess.shots[0].monitor)
    qapp.processEvents()
    assert (ed.width(), ed.height()) == (500, 420) and (ed.x(), ed.y()) == (30, 40)


def test_hide_for_capture_hides_now_and_flushes_events(qapp, make_editor, monkeypatch):
    ed, sess = make_editor(1)
    calls = []
    real = QApplication.processEvents
    monkeypatch.setattr(QApplication, "processEvents", staticmethod(lambda *a, **k: (calls.append(a), real(*a, **k))[1]))
    ed.hide_for_capture()
    assert not ed.isVisible() and calls  # hidden synchronously, event queue flushed
    n = len(calls)
    ed.hide_for_capture()  # already hidden: harmless
    assert not ed.isVisible() and len(calls) >= n
    assert len(sess.shots) == 1  # the session is untouched


def test_hide_for_capture_commits_a_pending_pin_note(qapp, make_editor):
    ed, sess = make_editor(1)
    ed.canvas.set_tool(Tool.PIN)
    click(ed.canvas, *pixel_center(ed.canvas, 50, 40))
    ed.canvas._note.setText("typed before hide")
    ed.hide_for_capture()
    assert sess.shots[0].pins[0].note == "typed before hide"


def test_closing_with_x_only_hides_and_keeps_the_session(qapp, make_editor):
    ed, sess = make_editor(3)
    rec = Recorder(ed.session_changed)
    ev = QCloseEvent()
    ed.closeEvent(ev)
    assert not ev.isAccepted()  # Qt must not destroy / close the window
    assert not ed.isVisible() and len(sess.shots) == 3 and rec.count == 1
    ed.present()
    assert ed.isVisible() and ed.current_shot_id() == sess.shots[0].id
    ed.close()
    assert not ed.isVisible() and len(sess.shots) == 3


# ---------------------------------------------------------------------------
# Next / Next (delayed) / Finish
# ---------------------------------------------------------------------------
COMBOS = [
    (CTRL, "next_requested"),
    (CTRL | ALT, "next_delayed_requested"),
    (CTRL | SHIFT, "finish_requested"),
]


@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Enter], ids=["Return", "keypad-Enter"])
@pytest.mark.parametrize("extra", [Qt.KeyboardModifier.NoModifier, KEYPAD], ids=["plain", "with-keypad-flag"])
@pytest.mark.parametrize("mods,signal_name", COMBOS, ids=["ctrl", "ctrl-alt", "ctrl-shift"])
def test_shortcuts_fire_while_the_caption_has_focus(qapp, make_editor, key, extra, mods, signal_name):
    ed, sess = make_editor(2)
    assert QApplication.focusWidget() is ed.caption_edit
    recs = {n: Recorder(getattr(ed, n)) for n in ("next_requested", "next_delayed_requested", "finish_requested")}
    QTest.keyClick(ed.caption_edit, key, mods | extra)
    assert {n: r.count for n, r in recs.items()} == {n: int(n == signal_name) for n in recs}
    assert ed.caption_edit.toPlainText() == "caption 1"  # the key never became a line break


def test_plain_enter_inserts_a_newline_in_the_caption(qapp, make_editor):
    ed, sess = make_editor(1)
    ed.caption_edit.setPlainText("")
    QTest.keyClicks(ed.caption_edit, "line one")
    QTest.keyClick(ed.caption_edit, Qt.Key.Key_Return)
    QTest.keyClicks(ed.caption_edit, "line two")
    assert sess.shots[0].caption == "line one\nline two"


def test_shortcuts_fire_from_other_focus_widgets_too(qapp, make_editor):
    ed, sess = make_editor(2)
    ed.fields.set_expanded(True)
    qapp.processEvents()
    rec = Recorder(ed.next_requested)
    rec_d = Recorder(ed.next_delayed_requested)
    rec_f = Recorder(ed.finish_requested)
    for w in (ed.canvas, ed.marks, ed.panel._list, ed.fields.goal_edit, ed.fields.expected_edit, ed.fields.project_edit):
        w.setFocus()
        qapp.processEvents()
        QTest.keyClick(w, Qt.Key.Key_Return, CTRL)
        QTest.keyClick(w, Qt.Key.Key_Enter, CTRL | ALT)
        QTest.keyClick(w, Qt.Key.Key_Return, CTRL | SHIFT)
    assert (rec.count, rec_d.count, rec_f.count) == (6, 6, 6)
    assert sess.goal == "" and sess.expected == ""  # no stray newlines typed into the multi-line boxes


def test_buttons_show_their_shortcut_and_emit(qapp, make_editor):
    ed, sess = make_editor(1)
    assert "Ctrl+Enter" in ed.next_button.text() and ed.next_button.text().startswith("Next")
    assert "Ctrl+Alt+Enter" in ed.next_delayed_button.text() and "delayed" in ed.next_delayed_button.text()
    assert "Ctrl+Shift+Enter" in ed.finish_button.text() and ed.finish_button.text().startswith("Finish")
    recs = [Recorder(ed.next_requested), Recorder(ed.next_delayed_requested), Recorder(ed.finish_requested)]
    for b in (ed.next_button, ed.next_delayed_button, ed.finish_button):
        QTest.mouseClick(b, Qt.MouseButton.LeftButton)
    assert [r.count for r in recs] == [1, 1, 1]


def test_finish_is_disabled_without_shots(qapp, make_editor):
    ed, sess = make_editor(0)
    assert not ed.finish_button.isEnabled()
    ed.present()
    qapp.processEvents()
    rec = Recorder(ed.finish_requested)
    QTest.keyClick(ed.caption_edit, Qt.Key.Key_Return, CTRL | SHIFT)
    QTest.mouseClick(ed.finish_button, Qt.MouseButton.LeftButton)
    assert rec.count == 0
    # Next is still available so a fresh capture can be started from an empty editor
    rn = Recorder(ed.next_requested)
    QTest.keyClick(ed.canvas, Qt.Key.Key_Return, CTRL)
    assert rn.count == 1


def test_next_flushes_the_pin_note_and_session_changed_before_the_signal(qapp, make_editor):
    ed, sess = make_editor(1)
    order = []
    ed.session_changed.connect(lambda: order.append("changed"))
    ed.next_requested.connect(lambda: order.append(("next", sess.shots[0].pins[0].note)))
    ed.canvas.set_tool(Tool.PIN)
    click(ed.canvas, *pixel_center(ed.canvas, 50, 40))
    order.clear()
    ed.canvas._note.setText("half typed note")
    ed.canvas._note.setFocus()
    QTest.keyClick(ed.canvas._note, Qt.Key.Key_Return, CTRL)  # the shortcut works from the note editor too
    assert order[-2:] == ["changed", ("next", "half typed note")]


def test_next_on_a_re_edited_shot_behaves_the_same(qapp, make_editor):
    ed, sess = make_editor(3)
    ed.show_shot(sess.shots[0].id)
    rec = Recorder(ed.next_requested)
    QTest.keyClick(ed.caption_edit, Qt.Key.Key_Return, CTRL)
    assert rec.count == 1 and [s.caption for s in sess.shots] == ["caption 1", "caption 2", "caption 3"]
    assert ed.current_shot_id() == sess.shots[0].id  # the editor itself never captures or adds shots


# ---------------------------------------------------------------------------
# roles
# ---------------------------------------------------------------------------
def test_role_control_default_tooltips_and_clicks(qapp, make_editor):
    ed, sess = make_editor(2)
    assert list(ed.role_buttons) == [Role.SHOT, Role.PROBLEM, Role.WANT, Role.CONTEXT, Role.AFTER]
    assert [b.text() for b in ed.role_buttons.values()] == ["Shot", "Problem", "Want", "Context", "After"]
    for role, btn in ed.role_buttons.items():
        assert btn.toolTip() == role.description
        assert ROLE_COLORS[role] in btn.styleSheet()
        assert btn.isCheckable()
    assert sess.shots[0].role == Role.SHOT and ed.role_buttons[Role.SHOT].isChecked()
    rec = Recorder(ed.session_changed)
    QTest.mouseClick(ed.role_buttons[Role.WANT], Qt.MouseButton.LeftButton)
    assert sess.shots[0].role == Role.WANT and rec.count == 1
    assert [b.isChecked() for b in ed.role_buttons.values()] == [False, False, True, False, False]
    QTest.mouseClick(ed.role_buttons[Role.AFTER], Qt.MouseButton.LeftButton)
    assert sess.shots[0].role == Role.AFTER
    assert [b.isChecked() for b in ed.role_buttons.values()] == [False, False, False, False, True]
    assert sess.shots[1].role == Role.SHOT  # other shots untouched


def test_role_clicks_do_not_take_focus_from_the_caption(qapp, make_editor):
    ed, sess = make_editor(1)
    QTest.mouseClick(ed.role_buttons[Role.CONTEXT], Qt.MouseButton.LeftButton)
    assert QApplication.focusWidget() is ed.caption_edit


@pytest.mark.parametrize("n,role", [(1, Role.SHOT), (2, Role.PROBLEM), (3, Role.WANT), (4, Role.CONTEXT),
                                    (5, Role.AFTER)])
def test_alt_digits_pick_the_role_even_while_typing(qapp, make_editor, n, role):
    ed, sess = make_editor(1)
    QTest.keyClick(ed.caption_edit, getattr(Qt.Key, f"Key_{n}"), ALT)
    assert sess.shots[0].role == role and ed.role_buttons[role].isChecked()
    assert ed.caption_edit.toPlainText() == "caption 1"  # the digit was not typed


# ---------------------------------------------------------------------------
# live edits -> session
# ---------------------------------------------------------------------------
def test_caption_edits_write_the_shot_live_and_emit(qapp, make_editor):
    ed, sess = make_editor(2)
    ed.caption_edit.clear()
    rec = Recorder(ed.session_changed)
    QTest.keyClicks(ed.caption_edit, "Save is clipped")
    assert sess.shots[0].caption == "Save is clipped"
    assert rec.count >= len("Save is clipped")
    assert sess.shots[1].caption == "caption 2"


def test_tool_letters_never_hijack_the_caption(qapp, make_editor):
    ed, sess = make_editor(1)
    ed.caption_edit.clear()
    QTest.keyClicks(ed.caption_edit, "vprambx 1")
    assert ed.caption_edit.toPlainText() == "vprambx 1"
    assert ed.canvas.tool() == Tool.SELECT
    ed.canvas.setFocus()
    QTest.keyClick(ed.canvas, Qt.Key.Key_R)
    assert ed.canvas.tool() == Tool.RECT and ed.tool_buttons[Tool.RECT].isChecked()


def test_tool_buttons_and_canvas_stay_in_sync(qapp, make_editor):
    ed, sess = make_editor(1)
    for tool, btn in ed.tool_buttons.items():
        assert btn.toolTip()
        QTest.mouseClick(btn, Qt.MouseButton.LeftButton)
        assert ed.canvas.tool() == tool and btn.isChecked()
    ed.canvas.set_tool(Tool.PIN)
    assert ed.tool_buttons[Tool.PIN].isChecked() and not ed.tool_buttons[Tool.REDACT].isChecked()
    assert QApplication.focusWidget() is ed.caption_edit  # tool buttons do not steal focus


def test_annotation_edits_emit_session_changed_and_feed_marks_and_panel(qapp, make_editor):
    ed, sess = make_editor(1)
    shot = sess.shots[0]
    rec = Recorder(ed.session_changed)
    _draw_pin(ed, 50, 40, "clipped")
    ed.canvas.set_tool(Tool.RULER)
    c = ed.canvas
    drag(c, pixel_center(c, 10, 100), pixel_center(c, 110, 100))
    assert len(shot.pins) == 1 and len(shot.rulers) == 1 and rec.count >= 2
    assert ed.marks.topLevelItemCount() == 2
    pin_row = ed.marks.topLevelItem(0)
    assert pin_row.text(0) == "Pin 1" and pin_row.text(COL_NOTE) == "clipped"
    assert ed.marks.topLevelItem(1).text(0) == "Ruler" and ed.marks.topLevelItem(1).text(COL_NOTE) == "100 px"
    # the filmstrip thumbnail shows the ANNOTATED render (cache invalidated by the edit)
    assert ed.panel.thumbnail(shot, QSize(120, 70)) is not None
    # undo through the toolbar button
    QTest.mouseClick(ed.undo_button, Qt.MouseButton.LeftButton)
    assert len(shot.rulers) == 0 and ed.marks.topLevelItemCount() == 1
    QTest.mouseClick(ed.redo_button, Qt.MouseButton.LeftButton)
    assert len(shot.rulers) == 1 and ed.marks.topLevelItemCount() == 2


def test_marks_list_selection_edit_and_delete_route_through_the_canvas(qapp, make_editor):
    ed, sess = make_editor(1)
    shot = sess.shots[0]
    _draw_pin(ed, 30, 30, "first")
    _draw_pin(ed, 90, 60, "second")
    p1, p2 = shot.pins
    # list -> canvas selection and back
    ed.marks.setCurrentItem(ed.marks.topLevelItem(1))
    assert ed.canvas.selected_id() == p2.id
    ed.canvas.select(p1.id)
    assert ed.marks.selected_annotation_id() == p1.id
    # editing the note cell edits the pin (undoable)
    ed.marks.topLevelItem(0).setText(COL_NOTE, "renamed")
    assert p1.note == "renamed"
    ed.canvas.undo()
    assert p1.note == "first"
    # delete via the list
    ed.marks.delete_requested.emit(p1.id)
    assert shot.pins == [p2] and p2.n == 1
    QTest.mouseClick(ed.delete_mark_button, Qt.MouseButton.LeftButton)  # nothing selected any more
    ed.canvas.select(p2.id)
    QTest.mouseClick(ed.delete_mark_button, Qt.MouseButton.LeftButton)
    assert shot.pins == []


def test_session_field_edits_emit_session_changed(qapp, make_editor):
    ed, sess = make_editor(1)
    rec = Recorder(ed.session_changed)
    ed.fields.goal_edit.setPlainText("Fix the clipped Save button")
    ed.fields.framework_combo.setEditText("WPF")
    assert sess.goal == "Fix the clipped Save button" and sess.framework_hint == "WPF" and rec.count == 2


# ---------------------------------------------------------------------------
# show_shot / re-edit
# ---------------------------------------------------------------------------
def test_show_shot_switches_every_control(qapp, make_editor):
    ed, sess = make_editor(3)
    a, b, c = sess.shots
    b.caption, b.role = "second caption", Role.CONTEXT
    b.add_annotation(RectAnn(x=5, y=5, w=40, h=20))
    ed.show_shot(b.id)
    assert ed.current_shot_id() == b.id and ed.canvas.shot() is b
    assert ed.caption_edit.toPlainText() == "second caption"
    assert ed.role_buttons[Role.CONTEXT].isChecked() and not ed.role_buttons[Role.SHOT].isChecked()
    assert ed.marks.topLevelItemCount() == 1
    assert ed.panel.current_id() == b.id
    assert "2 of 3" in ed.shot_label.text() and "2 of 3" in ed.windowTitle()
    assert QApplication.focusWidget() is ed.caption_edit
    assert not ed.canvas.can_undo()  # per-shot history starts empty
    ed.show_shot(a.id)
    assert ed.caption_edit.toPlainText() == "caption 1" and ed.role_buttons[Role.SHOT].isChecked()
    assert ed.marks.topLevelItemCount() == 0
    ed.show_shot("does-not-exist")  # unknown ids are ignored
    assert ed.current_shot_id() == a.id


def test_show_shot_for_the_open_shot_keeps_undo_history(qapp, make_editor):
    ed, sess = make_editor(1)
    _draw_pin(ed, 40, 40, "n")
    assert ed.canvas.can_undo()
    ed.show_shot(sess.shots[0].id)  # e.g. the controller re-showing the editor after a cancelled capture
    assert ed.canvas.can_undo() and QApplication.focusWidget() is ed.caption_edit


def test_clicking_a_thumbnail_reopens_that_shot_for_editing(qapp, make_editor):
    ed, sess = make_editor(4)
    lst = ed.panel._list
    qapp.processEvents()
    item = lst.item(2)
    click(lst.viewport(), *(lambda r: (r.center().x(), r.center().y() + 5))(lst.visualItemRect(item)))
    assert ed.current_shot_id() == sess.shots[2].id
    assert ed.caption_edit.toPlainText() == "caption 3"
    assert QApplication.focusWidget() is ed.caption_edit  # ready to retype the caption


def test_re_edit_an_earlier_shot_caption_role_and_annotations(qapp, make_editor):
    ed, sess = make_editor(4)
    first, second = sess.shots[0], sess.shots[1]
    _draw_pin(ed, 20, 20, "on shot one")
    ed.show_shot(second.id)
    _draw_pin(ed, 80, 50, "on shot two")
    # come back to shot one and change everything
    ed.show_shot(first.id)
    assert [p.note for p in first.pins] == ["on shot one"]
    ed.caption_edit.setPlainText("edited later")
    QTest.mouseClick(ed.role_buttons[Role.AFTER], Qt.MouseButton.LeftButton)
    _draw_pin(ed, 100, 100, "second pin on one")
    assert first.caption == "edited later" and first.role == Role.AFTER
    assert [(p.n, p.note) for p in first.pins] == [(1, "on shot one"), (2, "second pin on one")]
    assert [p.note for p in second.pins] == ["on shot two"]  # the other shot is untouched
    assert sess.shots[2].caption == "caption 3"


def test_image_less_shot_is_tolerated_everywhere(qapp, make_editor):
    ed, sess = make_editor(2)
    sess.shots[1].image = None  # loaded from report.json
    ed.refresh()
    ed.show_shot(sess.shots[1].id)
    qapp.processEvents()
    assert ed.canvas.shot() is sess.shots[1] and not ed.grab().isNull()
    assert ed.panel.thumbnail(sess.shots[1], ed.panel.size()) is None
    ed.caption_edit.setPlainText("still editable")
    assert sess.shots[1].caption == "still editable"


# ---------------------------------------------------------------------------
# panel requests: delete / reorder
# ---------------------------------------------------------------------------
def test_deleting_the_open_shot_opens_its_neighbour(qapp, make_editor):
    ed, sess = make_editor(4)
    ids = [s.id for s in sess.shots]
    ed.show_shot(ids[1])
    rec = Recorder(ed.session_changed)
    ed.panel.shot_delete_requested.emit(ids[1])
    assert [s.id for s in sess.shots] == [ids[0], ids[2], ids[3]]
    assert [s.index for s in sess.shots] == [1, 2, 3]
    assert ed.current_shot_id() == ids[2]  # the next shot takes its place
    assert ed.caption_edit.toPlainText() == "caption 3" and rec.count >= 1
    assert ed.panel.shot_ids() == [s.id for s in sess.shots]
    ed.show_shot(ids[3])
    ed.panel.shot_delete_requested.emit(ids[3])  # last one: the previous shot opens
    assert ed.current_shot_id() == ids[2]


def test_deleting_another_shot_keeps_the_open_one(qapp, make_editor):
    ed, sess = make_editor(4)
    ids = [s.id for s in sess.shots]
    ed.show_shot(ids[2])
    ed.panel.shot_delete_requested.emit(ids[0])
    assert ed.current_shot_id() == ids[2] and ed.panel.current_id() == ids[2]
    assert "2 of 3" in ed.shot_label.text()
    ed.panel.shot_delete_requested.emit("nope")  # stale request: ignored
    assert len(sess.shots) == 3


def test_deleting_every_shot_shows_the_empty_state(qapp, make_editor):
    ed, sess = make_editor(2)
    ed.next_requested.connect(lambda: None)
    for s in list(sess.shots):
        ed.panel.shot_delete_requested.emit(s.id)
    assert sess.shots == [] and ed.current_shot_id() is None
    assert ed.canvas.shot() is None
    assert not ed.caption_edit.isEnabled() and ed.caption_edit.toPlainText() == ""
    assert not ed.role_control.isEnabled() and not ed.tool_bar.isEnabled()
    assert not ed.finish_button.isEnabled()
    assert ed.panel.shot_ids() == []
    assert not ed.grab().isNull()
    # typing / role clicks in the empty state cannot fail or create shots
    ed.role_control.set_role(Role.WANT, emit=True)
    assert sess.shots == []


def test_empty_state_recovers_when_a_shot_is_added(qapp, make_editor, make_shot):
    ed, sess = make_editor(1)
    ed.panel.shot_delete_requested.emit(sess.shots[0].id)
    assert ed.current_shot_id() is None
    shot = make_shot(200, 120, caption="fresh")
    sess.add_shot(shot)
    ed.refresh()
    assert ed.current_shot_id() is None and ed.finish_button.isEnabled()  # refresh never opens a shot itself
    ed.show_shot(shot.id)
    ed.present(shot.monitor)
    assert ed.caption_edit.isEnabled() and ed.caption_edit.toPlainText() == "fresh"
    assert QApplication.focusWidget() is ed.caption_edit and ed.tool_bar.isEnabled()


def test_reorder_from_the_panel_updates_the_session(qapp, make_editor):
    ed, sess = make_editor(4)
    ids = [s.id for s in sess.shots]
    ed.show_shot(ids[1])
    rec = Recorder(ed.session_changed)
    ed.panel.move_shot(ids[3], 0)
    assert [s.id for s in sess.shots] == [ids[3], ids[0], ids[1], ids[2]]
    assert [s.index for s in sess.shots] == [1, 2, 3, 4]
    assert ed.current_shot_id() == ids[1] and ed.panel.current_id() == ids[1]
    assert "3 of 4" in ed.shot_label.text()  # the label follows the shot to its new position
    assert rec.count == 1
    assert ed.panel.shot_ids() == [s.id for s in sess.shots]
    # a stale / invalid order is rejected without touching the session
    ed.panel.order_changed.emit(ids[:2])
    assert [s.id for s in sess.shots] == [ids[3], ids[0], ids[1], ids[2]]


def test_twelve_plus_shots_with_reorder_delete_and_re_edit(qapp, make_editor):
    ed, sess = make_editor(14)
    ids = [s.id for s in sess.shots]
    assert ed.panel.shot_ids() == ids and ed.finish_button.isEnabled()
    # annotate a few
    for k in (0, 6, 13):
        ed.show_shot(ids[k])
        _draw_pin(ed, 10 + k, 20 + k, f"pin on {k}")
    # reorder: last to the front, 5th to the end, then a middle move
    ed.panel.move_shot(ids[13], 0)
    ed.panel.move_shot(ids[4], 13)
    ed.panel.move_shot(ids[7], 5)
    expected = list(ids)
    expected.insert(0, expected.pop(13))
    expected.append(expected.pop(expected.index(ids[4])))
    expected.insert(5, expected.pop(expected.index(ids[7])))
    assert [s.id for s in sess.shots] == expected
    assert [s.index for s in sess.shots] == list(range(1, 15))
    # delete the open shot and two others
    ed.show_shot(ids[6])
    for sid in (ids[6], ids[2], ids[9]):
        ed.panel.shot_delete_requested.emit(sid)
    remaining = [s for s in expected if s not in (ids[6], ids[2], ids[9])]
    assert [s.id for s in sess.shots] == remaining and len(sess.shots) == 11
    assert ed.current_shot_id() in remaining and ed.panel.shot_ids() == remaining
    # re-edit a shot that survived with its annotations
    ed.show_shot(ids[13])
    assert [p.note for p in sess.get_shot(ids[13]).pins] == ["pin on 13"]
    ed.caption_edit.setPlainText("still here")
    assert sess.get_shot(ids[13]).caption == "still here"
    assert ed.panel.thumbnail(sess.get_shot(ids[13]), ed.panel.size()) is not None
    assert [s.index for s in sess.shots] == list(range(1, 12))


# ---------------------------------------------------------------------------
# session swap / discard
# ---------------------------------------------------------------------------
def test_set_session_swaps_everything(qapp, make_editor, make_shot):
    ed, old = make_editor(3)
    _draw_pin(ed, 30, 30, "old")
    fresh = Session(project_path=r"C:\dev\remembered", framework_hint="WinUI 3")
    ed.set_session(fresh)
    assert ed.current_shot_id() is None and ed.canvas.shot() is None
    assert ed.panel.shot_ids() == [] and not ed.finish_button.isEnabled()
    assert ed.fields.project_edit.text() == r"C:\dev\remembered"
    assert ed.fields.framework_combo.currentText() == "WinUI 3"
    assert not ed.caption_edit.isEnabled()
    shot = make_shot(200, 120)
    fresh.add_shot(shot)
    ed.refresh()
    ed.show_shot(shot.id)
    ed.caption_edit.setPlainText("new session")
    assert fresh.shots[0].caption == "new session"
    assert [s.caption for s in old.shots] == ["caption 1", "caption 2", "caption 3"]  # old session untouched
    ed.fields.goal_edit.setPlainText("goal for the new one")
    assert fresh.goal == "goal for the new one" and old.goal == ""


def test_discard_asks_first(qapp, make_editor, monkeypatch):
    ed, sess = make_editor(3)
    rec = Recorder(ed.discard_session_requested)
    asked = []

    def fake(answer):
        def q(parent, title, text, buttons=None, default=None):
            asked.append((title, text))
            return answer

        return staticmethod(q)

    monkeypatch.setattr(QMessageBox, "question", fake(QMessageBox.StandardButton.Cancel))
    QTest.mouseClick(ed.discard_button, Qt.MouseButton.LeftButton)
    assert rec.count == 0 and len(asked) == 1 and "3 screenshots" in asked[0][1]
    monkeypatch.setattr(QMessageBox, "question", fake(QMessageBox.StandardButton.Yes))
    QTest.mouseClick(ed.discard_button, Qt.MouseButton.LeftButton)
    assert rec.count == 1
    assert len(sess.shots) == 3  # the editor itself never throws data away: the controller does


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------
def test_refresh_after_the_controller_adds_a_shot_keeps_the_open_shot(qapp, make_editor, make_shot):
    ed, sess = make_editor(2)
    ed.show_shot(sess.shots[0].id)
    shot = make_shot(200, 120, caption="third")
    sess.add_shot(shot)
    ed.refresh()
    assert ed.current_shot_id() == sess.shots[0].id and ed.panel.shot_ids() == [s.id for s in sess.shots]
    assert "1 of 3" in ed.shot_label.text()
    ed.show_shot(shot.id)  # what the controller does next
    assert ed.current_shot_id() == shot.id and ed.caption_edit.toPlainText() == "third"


def test_refresh_when_the_open_shot_was_removed_behind_our_back(qapp, make_editor):
    ed, sess = make_editor(3)
    ed.show_shot(sess.shots[1].id)
    sess.remove_shot(sess.shots[1].id)
    ed.refresh()
    assert ed.current_shot_id() == sess.shots[1].id  # the neighbour that took its place
    for s in list(sess.shots):
        sess.remove_shot(s.id)
    ed.refresh()
    assert ed.current_shot_id() is None and not ed.caption_edit.isEnabled()


def test_canvas_only_keys_do_not_leak_out_of_the_caption(qapp, make_editor):
    """Ctrl+Z / Delete typed into the caption edit the TEXT; they never touch the annotations."""
    ed, sess = make_editor(1)
    _draw_pin(ed, 40, 40, "keep me")
    _draw_pin(ed, 90, 60, "and me")
    assert ed.canvas.selected_id() is not None
    ed.caption_edit.setFocus()
    ed.caption_edit.clear()
    QTest.keyClicks(ed.caption_edit, "abc")
    QTest.keyClick(ed.caption_edit, Qt.Key.Key_Z, CTRL)  # undoes the typing in the caption
    assert len(sess.shots[0].pins) == 2 and sess.shots[0].caption != "abc"
    QTest.keyClick(ed.caption_edit, Qt.Key.Key_Delete)
    QTest.keyClick(ed.caption_edit, Qt.Key.Key_Backspace)
    assert len(sess.shots[0].pins) == 2
    # the same keys DO act on the annotations once the canvas has focus
    ed.canvas.setFocus()
    QTest.keyClick(ed.canvas, Qt.Key.Key_Delete)
    assert len(sess.shots[0].pins) == 1 and [p.n for p in sess.shots[0].pins] == [1]
    QTest.keyClick(ed.canvas, Qt.Key.Key_Z, CTRL)
    assert len(sess.shots[0].pins) == 2


def test_undo_history_is_per_shot(qapp, make_editor):
    ed, sess = make_editor(2)
    _draw_pin(ed, 40, 40, "first shot pin")
    assert ed.canvas.can_undo() and ed.undo_button.isEnabled()
    ed.show_shot(sess.shots[1].id)
    assert not ed.canvas.can_undo() and not ed.undo_button.isEnabled()
    ed.canvas.undo()  # nothing to undo on this shot: must not touch shot one
    assert len(sess.shots[0].pins) == 1
