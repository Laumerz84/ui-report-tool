"""SessionPanel (filmstrip) and SessionFieldsWidget. Offscreen; input goes to our own widgets only."""
from __future__ import annotations

import time

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QDropEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog

from test_editor_support import Recorder, click, make_session
from uireport.editor.session_fields import FRAMEWORK_SUGGESTIONS, SessionFieldsWidget
from uireport.editor.session_panel import MIME_TYPE, SessionPanel
from uireport.models import CaptureMode, RectAnn, RedactAnn, Role, Session


@pytest.fixture
def panel_for(qapp, make_shot):
    made = []

    def _make(n: int, w: int = 200, h: int = 120):
        sess = make_session(make_shot, n, w, h)
        panel = SessionPanel(sess)
        panel.resize(900, panel.height())
        panel.show()
        qapp.processEvents()
        made.append(panel)
        return panel, sess

    yield _make
    for p in made:
        p.hide()
        p.deleteLater()


def _card_center(panel: SessionPanel, row: int) -> tuple[float, float]:
    r = panel._list.visualItemRect(panel._list.item(row))
    return (r.center().x(), r.center().y() + 8)  # a little below the centre: away from the delete "x"


def test_lists_every_shot_in_order_and_never_mutates_the_session(qapp, panel_for):
    panel, sess = panel_for(5)
    before = [s.id for s in sess.shots]
    assert panel.shot_ids() == before and panel._list.count() == 5
    panel.refresh()
    panel.set_current(before[2])
    assert panel.current_id() == before[2]
    assert [s.id for s in sess.shots] == before and [s.index for s in sess.shots] == [1, 2, 3, 4, 5]


def test_click_emits_shot_selected_and_set_current_is_silent(qapp, panel_for):
    panel, sess = panel_for(4)
    ids = [s.id for s in sess.shots]
    rec = Recorder(panel.shot_selected)
    panel.set_current(ids[1])  # programmatic: no signal
    assert rec.count == 0 and panel.current_id() == ids[1]
    click(panel._list.viewport(), *_card_center(panel, 2))
    assert rec.calls == [(ids[2],)] and panel.current_id() == ids[2]
    click(panel._list.viewport(), *_card_center(panel, 2))  # clicking the already-open shot re-opens it
    assert rec.calls[-1] == (ids[2],) and rec.count == 2
    panel.set_current(None)
    assert panel.current_id() is None and rec.count == 2


def test_keyboard_navigation_selects_and_enter_reopens(qapp, panel_for):
    panel, sess = panel_for(3)
    ids = [s.id for s in sess.shots]
    panel.set_current(ids[0])
    panel._list.setFocus()
    rec = Recorder(panel.shot_selected)
    QTest.keyClick(panel._list, Qt.Key.Key_Right)
    assert rec.calls == [(ids[1],)]
    QTest.keyClick(panel._list, Qt.Key.Key_Return)
    assert rec.calls[-1] == (ids[1],)


def test_delete_via_x_button_key_and_context_menu(qapp, panel_for):
    panel, sess = panel_for(4)
    ids = [s.id for s in sess.shots]
    rec = Recorder(panel.shot_delete_requested)
    # the small x at the top-right of the thumbnail
    row = 1
    card = panel._list._card_rect(panel._list.item(row))
    x = panel.metrics().x_rect(card).center()
    click(panel._list.viewport(), x.x(), x.y())
    assert rec.calls == [(ids[1],)]
    # Delete key deletes the current shot
    panel.set_current(ids[3])
    panel._list.setFocus()
    QTest.keyClick(panel._list, Qt.Key.Key_Delete)
    assert rec.calls[-1] == (ids[3],)
    # context menu
    menu = panel.build_context_menu(ids[0])
    texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert texts == ["Edit this shot", "Move left", "Move right", "Delete"]
    acts = {a.text(): a for a in menu.actions()}
    assert not acts["Move left"].isEnabled() and acts["Move right"].isEnabled()
    acts["Delete"].trigger()
    assert rec.calls[-1] == (ids[0],)
    sel = Recorder(panel.shot_selected)
    acts["Edit this shot"].trigger()
    assert sel.calls == [(ids[0],)]
    # nothing was deleted by the panel itself
    assert [s.id for s in sess.shots] == ids
    panel.request_delete("unknown")  # stale id: no request
    assert rec.count == 3


def test_move_shot_emits_the_full_new_order(qapp, panel_for):
    panel, sess = panel_for(5)
    ids = [s.id for s in sess.shots]
    rec = Recorder(panel.order_changed)
    assert panel.move_shot(ids[4], 0)
    assert rec.calls[-1] == ([ids[4], ids[0], ids[1], ids[2], ids[3]],)
    assert panel.shot_ids() == [ids[4], ids[0], ids[1], ids[2], ids[3]]
    assert panel.current_id() == ids[4]
    assert panel.move_shot(ids[0], 99)  # clamped to the end
    assert rec.calls[-1][0][-1] == ids[0] and len(rec.calls[-1][0]) == 5
    assert not panel.move_shot(ids[0], 99)  # already there
    assert not panel.move_shot("unknown", 0)
    n = rec.count
    panel.set_current(ids[2])
    panel.move_current(-1)
    assert rec.count == n + 1 and rec.calls[-1][0].index(ids[2]) == panel.shot_ids().index(ids[2])
    assert [s.id for s in sess.shots] == ids  # the session is only changed by the owner


def test_ctrl_arrows_move_the_current_card(qapp, panel_for):
    panel, sess = panel_for(3)
    ids = [s.id for s in sess.shots]
    panel.set_current(ids[1])
    panel._list.setFocus()
    rec = Recorder(panel.order_changed)
    QTest.keyClick(panel._list, Qt.Key.Key_Left, Qt.KeyboardModifier.ControlModifier)
    assert rec.calls == [([ids[1], ids[0], ids[2]],)]


def _drop(panel: SessionPanel, src_row: int, x: float) -> None:
    """Deliver a drop at viewport x exactly as Qt would after a drag started on `src_row`."""
    lst = panel._list
    lst._drag_row = src_row
    mime = QMimeData()
    mime.setData(MIME_TYPE, b"x")
    ev = QDropEvent(QPointF(x, 30.0), Qt.DropAction.MoveAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    lst.dropEvent(ev)
    assert ev.isAccepted()
    lst._drag_row = -1
    QApplication.processEvents()  # order_changed is emitted from the event loop
    QTest.qWait(5)


def test_drag_and_drop_reorders_with_a_proper_insertion_point(qapp, panel_for):
    panel, sess = panel_for(5)
    ids = [s.id for s in sess.shots]
    lst = panel._list
    rec = Recorder(panel.order_changed)
    # drop card 0 in the LEFT half of card 3 -> inserted before card 3
    r3 = lst.visualItemRect(lst.item(3))
    _drop(panel, 0, r3.left() + 5)
    assert rec.calls[-1] == ([ids[1], ids[2], ids[0], ids[3], ids[4]],)
    ids2 = panel.shot_ids()
    # drop card 4 in the LEFT half of card 0 -> to the front
    _drop(panel, 4, lst.visualItemRect(lst.item(0)).left() + 3)
    assert panel.shot_ids()[0] == ids2[4] and rec.calls[-1][0] == panel.shot_ids()
    # drop far to the right (empty area) -> to the end
    n = rec.count
    _drop(panel, 0, lst.viewport().width() - 1)
    assert panel.shot_ids()[-1] == rec.calls[-1][0][-1] and rec.count == n + 1
    # dropping a card onto its own position changes nothing
    n = rec.count
    order = panel.shot_ids()
    _drop(panel, 2, lst.visualItemRect(lst.item(2)).center().x() - 2)
    assert rec.count == n and panel.shot_ids() == order
    assert [s.id for s in sess.shots] == ids  # the panel never touches the session
    # a drop without a drag in progress (foreign data) is ignored
    lst._drag_row = -1
    mime = QMimeData()
    mime.setText("foreign")
    ev = QDropEvent(QPointF(10, 10), Qt.DropAction.MoveAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    lst.dropEvent(ev)
    assert not ev.isAccepted() and rec.count == n


def test_insertion_row_maths(qapp, panel_for):
    panel, sess = panel_for(3)
    lst = panel._list
    assert lst.insertion_row(QPoint(0, 10)) == 0
    assert lst.insertion_row(QPoint(lst.visualItemRect(lst.item(1)).left() + 1, 10)) == 1
    assert lst.insertion_row(QPoint(lst.visualItemRect(lst.item(1)).right() - 1, 10)) == 2
    assert lst.insertion_row(QPoint(lst.viewport().width() - 1, 10)) == 3
    assert lst._marker_x(0) == lst.visualItemRect(lst.item(0)).left()
    assert lst._marker_x(3) == lst.visualItemRect(lst.item(2)).right()


def test_refresh_keeps_the_selection_and_follows_the_session(qapp, panel_for, make_shot):
    panel, sess = panel_for(3)
    ids = [s.id for s in sess.shots]
    panel.set_current(ids[1])
    sess.add_shot(make_shot(200, 120))
    panel.refresh()
    assert panel._list.count() == 4 and panel.current_id() == ids[1]
    sess.remove_shot(ids[1])
    panel.refresh()
    assert panel._list.count() == 3 and panel.current_id() is None
    fresh = Session()
    panel.set_session(fresh)
    assert panel._list.count() == 0 and not panel._thumb_cache and not panel._base_cache


def test_thumbnail_is_the_annotated_render_and_is_cached_and_invalidated(qapp, panel_for):
    panel, sess = panel_for(1, 400, 240)
    shot = sess.shots[0]
    shot.image.fill(QColor("#FFFFFF"))
    box = QSize(140, 84)
    plain = panel.thumbnail(shot, box)
    assert plain is not None and plain.width() <= 140 and plain.height() <= 84
    assert panel.thumbnail(shot, box) is plain  # cache hit
    shot.add_annotation(RectAnn(x=0, y=0, w=400, h=240))
    ann = panel.thumbnail(shot, box)  # signature changed -> re-rendered automatically
    assert ann is not plain
    assert ann.toImage().pixelColor(0, 0) != plain.toImage().pixelColor(0, 0)  # the red frame is drawn
    panel.invalidate(shot.id)
    assert shot.id not in panel._thumb_cache
    assert panel.thumbnail(shot, box) is not ann
    assert panel.thumbnail(shot, box) is panel._thumb_cache[shot.id][1]
    panel.invalidate()
    assert not panel._thumb_cache and not panel._base_cache


def test_thumbnail_covers_redacted_areas_instead_of_revealing_them(qapp, panel_for):
    panel, sess = panel_for(1, 400, 240)
    shot = sess.shots[0]
    shot.image.fill(QColor("#FF0000"))  # "secret" pure red
    shot.add_annotation(RedactAnn(x=100, y=60, w=200, h=120))
    pm = panel.thumbnail(shot, QSize(200, 120))
    img = pm.toImage()
    dpr = pm.devicePixelRatio()
    c = img.pixelColor(int(img.width() / 2), int(img.height() / 2))
    assert c.red() < 90 and c.green() < 90  # dark box, not the original red
    corner = img.pixelColor(2, 2)
    assert corner.red() > 200 and corner.green() < 40  # untouched area still shows the image
    assert dpr >= 1.0


def test_thumbnail_of_a_shot_without_image_is_none(qapp, panel_for):
    panel, sess = panel_for(2)
    sess.shots[0].image = None
    assert panel.thumbnail(sess.shots[0], QSize(100, 60)) is None
    panel.refresh()
    assert not panel.grab().isNull()  # the card paints a placeholder


def test_role_badge_caption_and_capture_mode_data_are_painted_without_error(qapp, panel_for):
    panel, sess = panel_for(4)
    sess.shots[1].role = Role.WANT
    sess.shots[2].capture_mode = CaptureMode.DELAYED
    sess.shots[3].caption = "a very long caption " * 10
    panel.update_shot(sess.shots[1].id)
    img = panel.grab().toImage()
    assert not img.isNull()
    tip = panel._list.item(1).toolTip()
    assert tip.startswith("2. Want:") and "200\u00d7120" in tip


def test_hundred_plus_shots_stay_fast_and_thumbnails_are_lazy(qapp, make_shot):
    sess = make_session(make_shot, 150, 640, 360)
    t = time.perf_counter()
    panel = SessionPanel(sess)
    panel.resize(900, panel.height())
    panel.show()
    QApplication.processEvents()
    panel.grab()
    t_build = time.perf_counter() - t
    assert panel._list.count() == 150 and t_build < 2.0, t_build
    assert len(panel._thumb_cache) <= 20  # only the visible cards were rendered
    t = time.perf_counter()
    panel.refresh()
    panel.grab()
    assert time.perf_counter() - t < 1.0
    # scroll to the end and select the last one
    last = sess.shots[-1].id
    panel.set_current(last)
    QApplication.processEvents()
    assert panel._list.horizontalScrollBar().maximum() > 0 and panel.current_id() == last
    # reorder and delete requests with 150 shots
    rec = Recorder(panel.order_changed)
    panel.move_shot(last, 0)
    assert len(rec.calls[-1][0]) == 150 and rec.calls[-1][0][0] == last
    panel.hide()
    panel.deleteLater()


def test_panel_height_is_compact(qapp, panel_for):
    panel, _ = panel_for(3)
    assert 90 <= panel.height() <= 220


# ---------------------------------------------------------------------------
# SessionFieldsWidget
# ---------------------------------------------------------------------------
@pytest.fixture
def fields_for(qapp):
    made = []

    def _make(**kw):
        sess = Session(**kw)
        w = SessionFieldsWidget(sess)
        made.append(w)
        return w, sess

    yield _make
    for w in made:
        w.deleteLater()


def test_fields_write_straight_into_the_session_and_emit(qapp, fields_for):
    w, sess = fields_for()
    rec = Recorder(w.changed)
    w.goal_edit.setPlainText("  Fix the clipped Save button ")
    w.project_edit.setText(r"C:\dev\app")
    w.framework_combo.setEditText("React + Tailwind")
    w.expected_edit.setPlainText("Full label visible")
    w.actual_edit.setPlainText("Label cut off at 150% scale")
    assert (sess.goal, sess.project_path, sess.framework_hint) == ("Fix the clipped Save button", r"C:\dev\app", "React + Tailwind")
    assert (sess.expected, sess.actual) == ("Full label visible", "Label cut off at 150% scale")
    assert rec.count == 5
    w.goal_edit.moveCursor(w.goal_edit.textCursor().MoveOperation.End)
    QTest.keyClicks(w.goal_edit, " now")
    assert sess.goal == "Fix the clipped Save button  now"  # typing writes live (only the ends are stripped)


def test_framework_combo_is_editable_with_the_required_suggestions(qapp, fields_for):
    w, sess = fields_for()
    assert w.framework_combo.isEditable()
    items = [w.framework_combo.itemText(i) for i in range(w.framework_combo.count())]
    for needed in ("React + Tailwind", "WPF", "WinUI 3", "Qt/QML", "Flutter", "Electron", "SwiftUI", "Next.js"):
        assert needed in items and needed in FRAMEWORK_SUGGESTIONS
    w.framework_combo.setCurrentIndex(items.index("WPF"))  # picking a suggestion writes it too
    assert sess.framework_hint == "WPF"
    w.framework_combo.setEditText("My custom toolkit")
    assert sess.framework_hint == "My custom toolkit"


def test_fields_display_what_the_session_already_has(qapp, fields_for):
    w, sess = fields_for(goal="G", project_path=r"C:\p", framework_hint="Flutter", expected="E", actual="A")
    rec = Recorder(w.changed)
    assert w.goal_edit.toPlainText() == "G" and w.project_edit.text() == r"C:\p"
    assert w.framework_combo.currentText() == "Flutter"
    assert (w.expected_edit.toPlainText(), w.actual_edit.toPlainText()) == ("E", "A")
    assert rec.count == 0  # loading is not an edit
    sess.goal = "changed elsewhere"
    w.refresh()
    assert w.goal_edit.toPlainText() == "changed elsewhere" and rec.count == 0


def test_set_session_reloads_without_touching_either_session(qapp, fields_for):
    w, old = fields_for(goal="old goal", framework_hint="WPF")
    new = Session(goal="", project_path=r"D:\new", framework_hint="Electron")
    rec = Recorder(w.changed)
    w.set_session(new)
    assert w.goal_edit.toPlainText() == "" and w.project_edit.text() == r"D:\new"
    assert w.framework_combo.currentText() == "Electron" and rec.count == 0
    assert (old.goal, old.framework_hint) == ("old goal", "WPF") and new.goal == ""
    w.goal_edit.setPlainText("typed")
    assert new.goal == "typed" and old.goal == "old goal"


def test_browse_uses_the_folder_dialog(qapp, fields_for, monkeypatch, test_out):
    w, sess = fields_for()
    picked = test_out / "proj"
    picked.mkdir()
    seen = {}

    def fake(parent, caption, directory="", *a, **k):
        seen["start"] = directory
        return str(picked).replace("\\", "/")

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(fake))
    rec = Recorder(w.changed)
    w.browse_button.click()
    assert sess.project_path == str(picked) and w.project_edit.text() == str(picked) and rec.count >= 1
    # the dialog now starts at the current project folder
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: (seen.update(start=a[2]), "")[1]))
    w.browse_button.click()
    assert seen["start"] == str(picked) and sess.project_path == str(picked)  # cancelled: unchanged


def test_section_is_collapsible_and_summarises_when_collapsed(qapp, fields_for):
    w, sess = fields_for(goal="Fix the clipped Save button", framework_hint="WPF", project_path=r"C:\dev\my-app")
    w.show()
    qapp.processEvents()
    assert not w.is_expanded() and not w.goal_edit.isVisible()
    text = w._summary.text()
    assert "Fix the clipped Save button" in text and "WPF" in text and "my-app" in text
    rec = Recorder(w.expanded_changed)
    w.set_expanded(True)
    qapp.processEvents()
    assert w.is_expanded() and w.goal_edit.isVisible() and w._summary.text() == "" and rec.calls == [(True,)]
    w.set_expanded(False)
    assert not w.goal_edit.isVisible()
    w.hide()


def test_start_drag_builds_a_drag_carrying_the_shot_id(qapp, panel_for, monkeypatch):
    from uireport.editor import session_panel

    panel, sess = panel_for(3)
    ids = [s.id for s in sess.shots]
    made = {}

    class FakeDrag:
        def __init__(self, source):
            made["source"] = source

        def setMimeData(self, mime):
            made["mime"] = mime

        def setPixmap(self, pm):
            made["pixmap"] = pm

        def setHotSpot(self, hot):
            made["hot"] = hot

        def exec(self, action):
            made["action"] = action
            made["row_during_drag"] = panel._list._drag_row
            return Qt.DropAction.IgnoreAction

    monkeypatch.setattr(session_panel, "QDrag", FakeDrag)
    panel.set_current(ids[1])
    panel._list.startDrag(Qt.DropAction.MoveAction)
    assert bytes(made["mime"].data(MIME_TYPE)).decode("ascii") == ids[1]
    assert made["row_during_drag"] == 1 and made["action"] == Qt.DropAction.MoveAction
    assert not made["pixmap"].isNull()
    assert panel._list._drag_row == -1  # reset once the drag is over
    panel.set_current(None)
    made.clear()
    panel._list.startDrag(Qt.DropAction.MoveAction)  # nothing selected: no drag
    assert not made


def test_cards_paint_in_hover_and_current_states(qapp, panel_for):
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QStyle, QStyleOptionViewItem

    panel, sess = panel_for(2)
    lst = panel._list
    index = lst.model().index(0, 0)
    size = lst.visualItemRect(lst.item(0)).size()

    def render(state) -> QImage:
        img = QImage(size, QImage.Format.Format_ARGB32)
        img.fill(QColor("#FFFFFF"))
        opt = QStyleOptionViewItem()
        opt.initFrom(lst)
        opt.rect = img.rect()
        opt.state = state
        opt.font = lst.font()
        opt.fontMetrics = lst.fontMetrics()
        opt.palette = lst.palette()
        p = QPainter(img)
        panel._delegate.paint(p, opt, index)
        p.end()
        return img

    idle = render(QStyle.StateFlag.State_Enabled)
    hover = render(QStyle.StateFlag.State_Enabled | QStyle.StateFlag.State_MouseOver)
    current = render(QStyle.StateFlag.State_Enabled | QStyle.StateFlag.State_Selected)
    assert idle != hover and idle != current  # the delete "x" appears, the current card gets a highlight border
    x = panel.metrics().x_rect(QRectF(3, 3, size.width() - 6, size.height() - 6)).center()
    px, py = int(x.x() - panel.metrics().x_size * 0.35), int(x.y())  # inside the dark disc, off the white cross
    assert hover.pixelColor(px, py).lightness() < 80 and idle.pixelColor(px, py).lightness() > 120
