"""AnnotationCanvas: view maths, pins / rect / arrow / ruler / redact editing, undo / redo,
selection, notes, painting. Offscreen; events are sent to our own widgets with QTest."""
from __future__ import annotations

import time

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest

from test_editor_support import LEFT, NOMOD, Recorder, click, double_click, drag, edge_point, pixel_center, pt
from uireport.annotdraw import render_annotated
from uireport.editor.canvas import TOOL_KEYS, AnnotationCanvas, Tool
from uireport.models import ArrowAnn, PinAnn, RectAnn, RedactAnn, RulerAnn


@pytest.fixture
def canvas(qapp, make_shot):
    """A 400x300 canvas showing a 200x120 shot at 100% scale, zoom 2.0 => image origin at widget (0, 30)."""
    shot = make_shot(200, 120)
    c = AnnotationCanvas()
    c.resize(400, 300)
    c.set_shot(shot)
    c.set_zoom(2.0)
    yield c
    c.deleteLater()


def test_default_fit_never_upscales_and_centres(qapp, make_shot):
    c = AnnotationCanvas()
    c.resize(400, 300)
    c.set_shot(make_shot(200, 120))
    assert c.is_fit() and c.zoom() == pytest.approx(1.0)
    assert c.image_to_widget(0, 0) == pytest.approx((100.0, 90.0))


def test_fit_downscales_a_wide_capture_and_actual_size_scrolls(qapp, make_shot):
    c = AnnotationCanvas()
    c.resize(800, 400)
    c.set_shot(make_shot(5120, 1440))
    z = c.zoom()
    assert z == pytest.approx(min((800 - 12) / 5120, (400 - 12) / 1440))
    assert not c._need_h and not c._need_v  # everything visible, no scrollbars
    c.zoom_actual()
    assert not c.is_fit() and c.zoom() == pytest.approx(1.0)
    assert c._need_h and c._need_v and c._hbar.maximum() > 0 and c._vbar.maximum() > 0
    # zooming keeps the viewport centre over the same image point (the middle of the capture)
    cx, cy = c.widget_to_image(c._viewport[0] / 2, c._viewport[1] / 2)
    assert (cx, cy) == pytest.approx((2560.0, 720.0), abs=12.0)
    c._hbar.setValue(0)
    c._vbar.setValue(0)
    assert c.widget_to_image(0, 0) == pytest.approx((0.0, 0.0))
    c._hbar.setValue(100)
    c._vbar.setValue(50)
    assert c.widget_to_image(0, 0) == pytest.approx((100.0, 50.0))
    assert c.image_to_widget(100.0, 50.0) == pytest.approx((0.0, 0.0))
    c.zoom_fit()
    assert c.is_fit() and not c._need_h


@pytest.mark.parametrize("dpr", [1.0, 1.25, 1.5, 2.0])
@pytest.mark.parametrize("zoom", [0.25, 0.5, 1.0, 2.0, 3.7])
def test_widget_image_round_trip_is_exact(qapp, make_shot, dpr, zoom):
    c = AnnotationCanvas()
    c._dpr_override = dpr
    c.resize(500, 350)
    c.set_shot(make_shot(1000, 600))
    c.set_zoom(zoom)
    for wx, wy in [(0, 0), (13.5, 77.25), (250, 175), (499.9, 349.9)]:
        ix, iy = c.widget_to_image(wx, wy)
        assert c.image_to_widget(ix, iy) == pytest.approx((wx, wy), abs=1e-9)
    for ix, iy in [(0, 0), (10.5, 20.25), (999, 599)]:
        wx, wy = c.image_to_widget(ix, iy)
        assert c.widget_to_image(wx, wy) == pytest.approx((ix, iy), abs=1e-9)


@pytest.mark.parametrize("dpr", [1.0, 1.25, 1.5, 2.0])
def test_one_to_one_is_one_image_pixel_per_device_pixel(qapp, make_shot, dpr):
    c = AnnotationCanvas()
    c._dpr_override = dpr
    c.resize(300, 200)
    c.set_shot(make_shot(1000, 600))
    c.zoom_actual()
    assert c.zoom() == pytest.approx(1.0 / dpr)
    x0, _ = c.image_to_widget(0, 0)
    x1, _ = c.image_to_widget(1, 0)
    assert x1 - x0 == pytest.approx(1.0 / dpr)
    # origin is snapped to the device-pixel grid so 1:1 stays sharp
    assert (x0 * dpr) == pytest.approx(round(x0 * dpr))


def test_fit_is_capped_at_actual_size_for_scaled_displays(qapp, make_shot):
    c = AnnotationCanvas()
    c._dpr_override = 1.5
    c.resize(800, 600)
    c.set_shot(make_shot(100, 60))
    assert c.zoom() == pytest.approx(1.0 / 1.5)


def test_set_zoom_keeps_the_anchor_point_fixed(qapp, make_shot):
    c = AnnotationCanvas()
    c.resize(400, 300)
    c.set_shot(make_shot(2000, 1200))
    c.set_zoom(1.0)
    c._hbar.setValue(300)
    c._vbar.setValue(200)
    anchor = QPointF(120.0, 90.0)
    before = c.widget_to_image(anchor.x(), anchor.y())
    c.set_zoom(2.0, anchor)
    after = c.widget_to_image(anchor.x(), anchor.y())
    assert after == pytest.approx(before, abs=1.0 / 2.0)  # within one scroll step (integer scrollbar)
    assert c.zoom() == pytest.approx(2.0)


def test_pin_click_lands_on_the_clicked_image_pixel(canvas):
    canvas.set_tool(Tool.PIN)
    shot = canvas.shot()
    click(canvas, 41, 51)  # widget (41, 51) -> image (20.5, 10.5) -> pixel (20, 10)
    click(canvas, 200, 150)  # image (100, 60)
    click(canvas, 299, 199)  # image (149.5, 84.5) -> pixel (149, 84)
    assert [(p.x, p.y) for p in shot.pins] == [(20, 10), (100, 60), (149, 84)]
    assert [p.n for p in shot.pins] == [1, 2, 3]
    assert all(type(p.x) is int and type(p.y) is int for p in shot.pins)


def test_pin_click_outside_the_image_is_ignored(canvas):
    canvas.set_tool(Tool.PIN)
    click(canvas, 10, 10)  # above the image (image starts at y=30)
    click(canvas, 390, 295)  # below it
    assert canvas.shot().pins == []


def test_pin_colour_is_sampled_from_the_original_image(qapp, make_shot):
    shot = make_shot(200, 120, color="#FFFFFF")
    shot.image.setPixelColor(20, 10, QColor("#12AB34"))
    shot.image.setPixelColor(100, 60, QColor("#FEDCBA"))
    key_before = shot.image.cacheKey()
    # a redaction and a rectangle already cover the spot that will be sampled
    shot.add_annotation(RedactAnn(x=10, y=0, w=40, h=30))
    shot.add_annotation(RectAnn(x=5, y=2, w=60, h=20))
    c = AnnotationCanvas()
    c.resize(400, 300)
    c.set_shot(shot)
    c.set_zoom(2.0)
    c.set_tool(Tool.PIN)
    click(c, 41, 51)
    pin = shot.pins[0]
    assert (pin.x, pin.y) == (20, 10)
    assert pin.color == "#12AB34"
    # moving the pin re-samples from the original image
    c.set_tool(Tool.SELECT)
    px, py = pixel_center(c, 20, 10)
    tx, ty = pixel_center(c, 100, 60)
    drag(c, (px, py), (tx, ty), via=[(px + 10, py + 10)])
    assert (pin.x, pin.y) == (100, 60)
    assert pin.color == "#FEDCBA"
    # the shot's image was never touched
    assert shot.image.cacheKey() == key_before
    assert shot.image.pixelColor(20, 10).name().upper() == "#12AB34"


def test_rect_by_drag_is_normalised_int_and_clamped(canvas):
    shot = canvas.shot()
    canvas.set_tool(Tool.RECT)
    drag(canvas, edge_point(canvas, 10, 20), edge_point(canvas, 60, 50))
    drag(canvas, edge_point(canvas, 160, 100), edge_point(canvas, 120, 70))  # dragged up-left
    drag(canvas, (-40, 5), (399, 299))  # far outside the image on both sides
    r1, r2, r3 = shot.rects
    assert (r1.x, r1.y, r1.w, r1.h) == (10, 20, 50, 30)
    assert (r2.x, r2.y, r2.w, r2.h) == (120, 70, 40, 30)
    assert (r3.x, r3.y, r3.w, r3.h) == (0, 0, 200, 120)
    assert all(type(v) is int for r in (r1, r2, r3) for v in (r.x, r.y, r.w, r.h))
    assert all(r.w >= 0 and r.h >= 0 for r in (r1, r2, r3))


def test_redact_by_drag(canvas):
    canvas.set_tool(Tool.REDACT)
    drag(canvas, edge_point(canvas, 30, 40), edge_point(canvas, 90, 80))
    (red,) = canvas.shot().redactions
    assert isinstance(red, RedactAnn) and (red.x, red.y, red.w, red.h) == (30, 40, 60, 40)


def test_arrow_and_ruler_by_drag_and_ruler_length(canvas):
    shot = canvas.shot()
    canvas.set_tool(Tool.ARROW)
    drag(canvas, pixel_center(canvas, 10, 20), pixel_center(canvas, 60, 50))
    canvas.set_tool(Tool.RULER)
    drag(canvas, pixel_center(canvas, 10, 20), pixel_center(canvas, 110, 20))
    drag(canvas, pixel_center(canvas, 10, 10), pixel_center(canvas, 13, 14))  # 3-4-5
    (arrow,) = shot.arrows
    assert isinstance(arrow, ArrowAnn) and (arrow.x1, arrow.y1, arrow.x2, arrow.y2) == (10, 20, 60, 50)
    r1, r2 = shot.rulers
    assert isinstance(r1, RulerAnn) and (r1.x1, r1.y1, r1.x2, r1.y2) == (10, 20, 110, 20)
    assert r1.length_px == 100.0 and r1.dx == 100 and r1.dy == 0
    assert r2.length_px == 5.0


def test_drags_shorter_than_three_image_px_are_ignored(qapp, make_shot):
    c = AnnotationCanvas()
    c.resize(400, 300)
    shot = make_shot(200, 120)
    c.set_shot(shot)
    c.set_zoom(1.0)  # one widget px == one image px
    rec = Recorder(c.annotations_changed)
    for tool in (Tool.RECT, Tool.ARROW, Tool.RULER, Tool.REDACT):
        c.set_tool(tool)
        drag(c, pixel_center(c, 50, 50), pixel_center(c, 52, 51))
        drag(c, pixel_center(c, 50, 50), pixel_center(c, 50, 50))
    assert shot.annotations == [] and rec.count == 0 and not c.can_undo()
    c.set_tool(Tool.RULER)
    drag(c, pixel_center(c, 50, 50), pixel_center(c, 53, 50))
    assert len(shot.rulers) == 1


def test_live_preview_exists_while_dragging(canvas):
    canvas.set_tool(Tool.RULER)
    QTest.mousePress(canvas, LEFT, NOMOD, pt(*pixel_center(canvas, 10, 20)))
    QTest.mouseMove(canvas, pt(*pixel_center(canvas, 110, 20)))
    assert isinstance(canvas._preview, RulerAnn) and canvas._hud == "100 px"
    assert canvas.shot().annotations == []  # nothing committed until release
    QTest.mouseRelease(canvas, LEFT, NOMOD, pt(*pixel_center(canvas, 110, 20)))
    assert canvas._preview is None and len(canvas.shot().rulers) == 1


def test_ruler_tooltip_shows_logical_px_only_on_scaled_monitors(qapp, make_shot):
    for pct, expect_logical in ((100, False), (150, True)):
        shot = make_shot(200, 120, scale_percent=pct)
        c = AnnotationCanvas()
        c.resize(400, 300)
        c.set_shot(shot)
        c.set_zoom(2.0)
        c.set_tool(Tool.RULER)
        drag(c, pixel_center(c, 10, 20), pixel_center(c, 110, 20))
        x, y = pixel_center(c, 60, 20)
        tip = c.tooltip_at(x, y)
        assert tip is not None and tip.startswith("Ruler: 100 px")
        assert ("logical px" in tip) is expect_logical
        if expect_logical:
            assert "66.7" in tip and "150%" in tip
        assert c.tooltip_at(*pixel_center(c, 150, 100)) is None


def test_undo_redo_round_trip_and_signals(canvas):
    shot = canvas.shot()
    rec = Recorder(canvas.annotations_changed)
    canvas.set_tool(Tool.RECT)
    drag(canvas, edge_point(canvas, 10, 20), edge_point(canvas, 60, 50))
    canvas.set_tool(Tool.PIN)
    click(canvas, 41, 51)
    canvas.commit_pending_edit()
    assert len(shot.annotations) == 2 and canvas.can_undo() and not canvas.can_redo()
    ids = [a.id for a in shot.annotations]
    canvas.undo()
    assert [a.id for a in shot.annotations] == ids[:1] and canvas.can_redo()
    canvas.undo()
    assert shot.annotations == []
    canvas.redo()
    canvas.redo()
    assert [a.id for a in shot.annotations] == ids
    assert rec.count >= 6
    # keyboard: Ctrl+Z / Ctrl+Y (and Ctrl+Shift+Z) with the canvas focused
    QTest.keyClick(canvas, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert len(shot.annotations) == 1
    QTest.keyClick(canvas, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
    assert len(shot.annotations) == 2
    QTest.keyClick(canvas, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(canvas, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert len(shot.annotations) == 2
    canvas.redo()  # nothing to redo any more: harmless
    canvas.undo(); canvas.undo(); canvas.undo()  # extra undos are harmless
    assert shot.annotations == []


def test_new_action_clears_the_redo_stack(canvas):
    canvas.set_tool(Tool.RECT)
    drag(canvas, edge_point(canvas, 10, 20), edge_point(canvas, 60, 50))
    canvas.undo()
    assert canvas.can_redo()
    drag(canvas, edge_point(canvas, 70, 20), edge_point(canvas, 90, 50))
    assert not canvas.can_redo() and len(canvas.shot().rects) == 1


def test_set_shot_resets_selection_and_history(canvas, make_shot):
    canvas.set_tool(Tool.RECT)
    drag(canvas, edge_point(canvas, 10, 20), edge_point(canvas, 60, 50))
    assert canvas.selected_id() is not None and canvas.can_undo()
    canvas.set_shot(make_shot(200, 120))
    assert canvas.selected_id() is None and not canvas.can_undo() and not canvas.can_redo()
    assert canvas.is_fit()
    canvas.set_shot(None)
    assert canvas.shot() is None


def test_select_click_and_delete_key(canvas):
    shot = canvas.shot()
    canvas.set_tool(Tool.RECT)
    drag(canvas, edge_point(canvas, 10, 20), edge_point(canvas, 60, 50))
    drag(canvas, edge_point(canvas, 100, 20), edge_point(canvas, 150, 50))
    r1, r2 = shot.rects
    canvas.set_tool(Tool.SELECT)
    canvas.select(None)
    click(canvas, *edge_point(canvas, 10, 35))  # left border of r1
    assert canvas.selected_id() == r1.id
    QTest.keyClick(canvas, Qt.Key.Key_Delete)
    assert shot.rects == [r2] and canvas.selected_id() is None
    canvas.undo()
    assert shot.rects == [r1, r2]  # restored at its original position
    click(canvas, 390, 290)  # empty space deselects
    assert canvas.selected_id() is None
    canvas.select(r2.id)
    canvas.delete_selected()
    assert shot.rects == [r1]
    canvas.delete_selected()  # nothing selected: harmless


def test_pin_renumbering_after_deleting_the_middle_pin(canvas):
    shot = canvas.shot()
    canvas.set_tool(Tool.PIN)
    for ix in (20, 60, 100):
        click(canvas, *pixel_center(canvas, ix, 30))
        canvas.commit_pending_edit()
    p1, p2, p3 = shot.pins
    assert [p.n for p in shot.pins] == [1, 2, 3]
    canvas.set_tool(Tool.SELECT)
    click(canvas, *pixel_center(canvas, 60, 30))
    assert canvas.selected_id() == p2.id
    canvas.delete_selected()
    assert shot.pins == [p1, p3] and [p.n for p in shot.pins] == [1, 2]
    canvas.undo()
    assert shot.pins == [p1, p2, p3] and [p.n for p in shot.pins] == [1, 2, 3]


def test_moving_an_annotation_is_one_undo_step_and_clamped(canvas):
    shot = canvas.shot()
    canvas.set_tool(Tool.RECT)
    drag(canvas, edge_point(canvas, 10, 20), edge_point(canvas, 60, 50))
    (rect,) = shot.rects
    canvas.set_tool(Tool.SELECT)
    start = edge_point(canvas, 10, 35)
    drag(canvas, start, (start[0] + 40, start[1] + 20), via=[(start[0] + 10, start[1] + 5)])
    assert (rect.x, rect.y, rect.w, rect.h) == (30, 30, 50, 30)
    drag(canvas, edge_point(canvas, 30, 45), (-500, -500), via=[(0, 30)])  # far off the top-left
    assert (rect.x, rect.y) == (0, 0)
    canvas.undo()
    assert (rect.x, rect.y) == (30, 30)
    canvas.undo()
    assert (rect.x, rect.y) == (10, 20)
    canvas.redo()
    assert (rect.x, rect.y) == (30, 30)


def test_a_plain_click_on_an_annotation_does_not_move_or_record(canvas):
    shot = canvas.shot()
    canvas.set_tool(Tool.RECT)
    drag(canvas, edge_point(canvas, 10, 20), edge_point(canvas, 60, 50))
    canvas.undo(); canvas.redo()
    n_undo = len(canvas._undo)
    canvas.set_tool(Tool.SELECT)
    click(canvas, *edge_point(canvas, 10, 35))
    assert len(canvas._undo) == n_undo
    assert (shot.rects[0].x, shot.rects[0].y) == (10, 20)


def test_arrow_keys_nudge_the_selection(canvas):
    shot = canvas.shot()
    canvas.set_tool(Tool.PIN)
    click(canvas, *pixel_center(canvas, 50, 50))
    canvas.commit_pending_edit()
    pin = shot.pins[0]
    canvas.set_tool(Tool.SELECT)
    QTest.keyClick(canvas, Qt.Key.Key_Right)
    QTest.keyClick(canvas, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
    assert (pin.x, pin.y) == (51, 60)
    canvas.undo()
    assert (pin.x, pin.y) == (51, 50)


def test_pin_note_editor_lifecycle(canvas):
    shot = canvas.shot()
    canvas.set_tool(Tool.PIN)
    click(canvas, *pixel_center(canvas, 50, 40))
    pin = shot.pins[0]
    assert canvas.pending_edit_active() and canvas.selected_id() == pin.id
    QTest.keyClicks(canvas._note, "button text clipped")
    QTest.keyClick(canvas._note, Qt.Key.Key_Return)
    assert pin.note == "button text clipped" and not canvas.pending_edit_active()
    # the note is part of the "add pin" step: one undo removes the pin, redo restores it WITH its note
    canvas.undo()
    assert shot.pins == []
    canvas.redo()
    assert shot.pins == [pin] and pin.note == "button text clipped"
    # double-click edits the note later; Esc keeps the old text, Enter records an undoable change
    double_click(canvas, *pixel_center(canvas, 50, 40))
    assert canvas.pending_edit_active() and canvas._note.text() == "button text clipped"
    QTest.keyClick(canvas._note, Qt.Key.Key_Escape)
    assert pin.note == "button text clipped" and not canvas.pending_edit_active()
    double_click(canvas, *pixel_center(canvas, 50, 40))
    canvas._note.setText("new wording")
    QTest.keyClick(canvas._note, Qt.Key.Key_Return)
    assert pin.note == "new wording"
    canvas.undo()
    assert pin.note == "button text clipped"
    # commit_pending_edit() (called by the window before Next / Finish) commits typed text
    canvas.edit_pin_note(pin.id)
    canvas._note.setText("via commit")
    canvas.commit_pending_edit()
    assert pin.note == "via commit"
    # set_pin_note is undoable and a no-op when unchanged
    n = len(canvas._undo)
    canvas.set_pin_note(pin.id, "via commit")
    assert len(canvas._undo) == n
    canvas.set_pin_note(pin.id, "other")
    assert pin.note == "other" and len(canvas._undo) == n + 1


def test_clicking_the_canvas_commits_an_open_note(canvas):
    shot = canvas.shot()
    canvas.set_tool(Tool.PIN)
    click(canvas, *pixel_center(canvas, 50, 40))
    canvas._note.setText("typed, then clicked away")
    canvas.set_tool(Tool.SELECT)  # switching tools commits too
    assert shot.pins[0].note == "typed, then clicked away"


def test_tool_shortcuts_only_act_on_the_canvas(qapp, canvas):
    assert len(set(TOOL_KEYS.values())) == len(TOOL_KEYS) == len(Tool)
    seen = Recorder(canvas.tool_changed)
    for tool, key in TOOL_KEYS.items():
        QTest.keyClick(canvas, getattr(Qt.Key, f"Key_{key}"))
        assert canvas.tool() == tool
    assert [c[0] for c in seen.calls][-1] == list(TOOL_KEYS)[-1]
    # Ctrl/Alt + letter is not a tool switch
    canvas.set_tool(Tool.SELECT)
    QTest.keyClick(canvas, Qt.Key.Key_R, Qt.KeyboardModifier.ControlModifier)
    assert canvas.tool() == Tool.SELECT


def test_view_keys(canvas):
    QTest.keyClick(canvas, Qt.Key.Key_1)
    assert canvas.zoom() == pytest.approx(1.0) and not canvas.is_fit()
    QTest.keyClick(canvas, Qt.Key.Key_Plus)
    assert canvas.zoom() == pytest.approx(1.25)
    QTest.keyClick(canvas, Qt.Key.Key_Minus)
    assert canvas.zoom() == pytest.approx(1.0)
    QTest.keyClick(canvas, Qt.Key.Key_F)
    assert canvas.is_fit()


def test_middle_button_pans(qapp, make_shot):
    c = AnnotationCanvas()
    c.resize(300, 200)
    c.set_shot(make_shot(1000, 600))
    c.zoom_actual()
    h0, v0 = c._hbar.value(), c._vbar.value()
    QTest.mousePress(c, Qt.MouseButton.MiddleButton, NOMOD, pt(100, 100))
    QTest.mouseMove(c, pt(60, 80))
    QTest.mouseRelease(c, Qt.MouseButton.MiddleButton, NOMOD, pt(60, 80))
    assert (c._hbar.value(), c._vbar.value()) == (h0 + 40, v0 + 20)  # dragging the content left / up
    assert c.shot().annotations == []


def test_live_view_equals_the_saved_annotated_png(qapp, make_shot):
    """The canvas paints through annotdraw at image scale, so at 1:1 the pixels of the image area
    equal render_annotated() (no redaction: those are previews on screen and pixelated in the file)."""
    shot = make_shot(200, 120, color="#DDE6F3", scale_percent=150)
    shot.add_annotation(RectAnn(x=20, y=20, w=80, h=40))
    shot.add_annotation(ArrowAnn(x1=110, y1=20, x2=190, y2=60))
    shot.add_annotation(RulerAnn(x1=20, y1=100, x2=120, y2=100))
    shot.add_annotation(PinAnn(n=1, x=60, y=30, color="#DDE6F3"))
    shot.renumber_pins()
    c = AnnotationCanvas()
    c.resize(400, 300)
    c.set_shot(shot)
    c.select(None)
    live = c.grab().toImage().convertToFormat(QImage.Format.Format_ARGB32)
    ox, oy = int(c.image_to_widget(0, 0)[0]), int(c.image_to_widget(0, 0)[1])
    assert c.zoom() == pytest.approx(1.0)
    crop = live.copy(ox, oy, 200, 120)
    saved = render_annotated(shot.image, shot.annotations, shot.scale_factor).convertToFormat(QImage.Format.Format_ARGB32)
    worst = 0
    for y in range(120):
        for x in range(200):
            if (48 <= x <= 72 and 18 <= y <= 42) or (10 <= x <= 130 and 64 <= y <= 84):
                continue  # pin number / ruler label glyphs (offscreen has no fonts: text placement differs per paint device)
            a, b = crop.pixelColor(x, y), saved.pixelColor(x, y)
            worst = max(worst, abs(a.red() - b.red()), abs(a.green() - b.green()), abs(a.blue() - b.blue()))
    assert worst <= 2, f"live view differs from the saved render by {worst} levels"


def test_painting_a_5120x1440_capture_is_fast_and_cached(qapp, make_shot):
    shot = make_shot(5120, 1440, color="#334455")
    for i in range(20):
        shot.add_annotation(RectAnn(x=100 + i * 200, y=100, w=150, h=80))
    c = AnnotationCanvas()
    c.resize(1000, 500)
    t = time.perf_counter()
    c.set_shot(shot)
    first = c.grab()
    t_first = time.perf_counter() - t
    assert not first.isNull()
    cached = c._scaled
    assert cached is not None
    t = time.perf_counter()
    for _ in range(10):
        c.update()
        c.grab()
    per_paint = (time.perf_counter() - t) / 10
    assert c._scaled is cached  # the pre-scaled pixmap is reused
    assert t_first < 3.0 and per_paint < 0.25, (t_first, per_paint)
    c.zoom_actual()  # zoomed in: only the visible source rectangle is blitted
    t = time.perf_counter()
    for _ in range(5):
        c.grab()
    assert (time.perf_counter() - t) / 5 < 0.25


def test_shot_without_an_image_is_tolerated(qapp, make_shot):
    shot = make_shot(200, 120)
    shot.image = None  # e.g. a session loaded from report.json
    c = AnnotationCanvas()
    c.resize(400, 300)
    c.set_shot(shot)
    c.set_tool(Tool.PIN)
    click(c, 50, 50)
    drag(c, (10, 10), (100, 100))
    assert shot.annotations == []
    assert not c.grab().isNull()
    assert c.widget_to_image(12.0, 34.0) == (12.0, 34.0)  # identity without an image
    c.set_shot(None)
    assert not c.grab().isNull()


def test_the_canvas_never_modifies_shot_image(canvas):
    shot = canvas.shot()
    key = shot.image.cacheKey()
    copy = shot.image.copy()
    canvas.set_tool(Tool.REDACT)
    drag(canvas, edge_point(canvas, 30, 40), edge_point(canvas, 90, 80))
    canvas.set_tool(Tool.PIN)
    click(canvas, *pixel_center(canvas, 50, 50))
    canvas.commit_pending_edit()
    canvas.grab()
    assert shot.image.cacheKey() == key and shot.image == copy


def test_selection_locator_is_drawn_even_when_zoomed_far_out(qapp, make_shot):
    shot = make_shot(5120, 1440, color="#FFFFFF")
    c = AnnotationCanvas()
    c.resize(600, 300)
    c.set_shot(shot)
    c.set_tool(Tool.PIN)
    click(c, *pixel_center(c, 2000, 700))
    c.commit_pending_edit()
    img = c.grab().toImage()
    cx, cy = (int(v) for v in c.image_to_widget(2000.5, 700.5))
    # the white frame + dashed blue box around the tiny pin are visible in widget space
    non_white = sum(
        1
        for dx in range(-10, 11)
        for dy in range(-10, 11)
        if img.pixelColor(cx + dx, cy + dy).blue() > img.pixelColor(cx + dx, cy + dy).red() + 40
    )
    assert non_white > 0
