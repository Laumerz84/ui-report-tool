"""Pure geometry of the annotation editor: snapping, building from drags, hit-testing, moving."""
from __future__ import annotations

import pytest

from uireport.annotdraw import AnnotStyle
from uireport.editor import shapes
from uireport.models import (
    AnnotationType,
    ArrowAnn,
    PinAnn,
    RectAnn,
    RedactAnn,
    RulerAnn,
)

STYLE = AnnotStyle.for_scale(1.0)


def test_snap_pixel_is_floor_and_clamped():
    assert shapes.snap_pixel(20.99, 200) == 20
    assert shapes.snap_pixel(0.0, 200) == 0
    assert shapes.snap_pixel(-5.2, 200) == 0
    assert shapes.snap_pixel(999.0, 200) == 199
    assert shapes.snap_pixel(5.0, 0) == 0


def test_snap_edge_is_nearest_grid_line_and_clamped():
    assert shapes.snap_edge(20.5, 200) == 21
    assert shapes.snap_edge(20.49, 200) == 20
    assert shapes.snap_edge(-3.0, 200) == 0
    assert shapes.snap_edge(999.0, 200) == 200  # edges may sit on the far border


def test_rect_from_drag_is_normalised_int_and_clamped():
    a = shapes.build_annotation(AnnotationType.RECT, (60.2, 50.0), (10.0, 20.0), 200, 120)
    assert isinstance(a, RectAnn)
    assert (a.x, a.y, a.w, a.h) == (10, 20, 50, 30)
    assert all(type(v) is int for v in (a.x, a.y, a.w, a.h))
    c = shapes.build_annotation(AnnotationType.RECT, (-50.0, -50.0), (500.0, 500.0), 200, 120)
    assert (c.x, c.y, c.w, c.h) == (0, 0, 200, 120)
    r = shapes.build_annotation(AnnotationType.REDACT, (10.0, 10.0), (40.0, 40.0), 200, 120)
    assert isinstance(r, RedactAnn) and (r.x, r.y, r.w, r.h) == (10, 10, 30, 30)


@pytest.mark.parametrize("kind,cls", [(AnnotationType.ARROW, ArrowAnn), (AnnotationType.RULER, RulerAnn)])
def test_line_tools_use_pixel_indices_and_clamp(kind, cls):
    a = shapes.build_annotation(kind, (10.7, 20.2), (110.9, 20.9), 200, 120)
    assert isinstance(a, cls)
    assert (a.x1, a.y1, a.x2, a.y2) == (10, 20, 110, 20)
    c = shapes.build_annotation(kind, (-9.0, -9.0), (900.0, 900.0), 200, 120)
    assert (c.x1, c.y1, c.x2, c.y2) == (0, 0, 199, 119)  # pixel indices: last pixel is W-1


def test_ruler_length_is_euclidean_in_image_pixels():
    a = shapes.build_annotation(AnnotationType.RULER, (10.0, 10.0), (40.0, 50.0), 200, 120)
    assert isinstance(a, RulerAnn) and a.length_px == 50.0


def test_drags_shorter_than_three_px_are_ignored():
    for kind in (AnnotationType.RECT, AnnotationType.REDACT, AnnotationType.ARROW, AnnotationType.RULER):
        assert shapes.build_annotation(kind, (10.0, 10.0), (12.0, 11.0), 200, 120) is None
        assert shapes.build_annotation(kind, (10.0, 10.0), (10.0, 10.0), 200, 120) is None
        ok = shapes.build_annotation(kind, (10.0, 10.0), (13.0, 10.0), 200, 120)
        assert ok is not None  # exactly 3 px is accepted
    # the live preview (min_len=0) still returns something to draw
    assert shapes.build_annotation(AnnotationType.RECT, (10.0, 10.0), (10.0, 10.0), 200, 120, min_len=0) is not None


def test_build_rejects_non_drag_kinds():
    with pytest.raises(ValueError):
        shapes.build_annotation(AnnotationType.PIN, (1.0, 1.0), (5.0, 5.0), 200, 120)


def test_pick_prefers_precise_hits_over_box_interiors():
    big = RectAnn(x=10, y=10, w=150, h=90)
    pin = PinAnn(n=1, x=80, y=50)
    anns = [pin, big]  # rectangle drawn AFTER (above) the pin
    assert shapes.pick(anns, 80.5, 50.5, 4.0, STYLE) is pin
    assert shapes.pick(anns, 60.0, 30.0, 4.0, STYLE) is big  # interior
    assert shapes.pick(anns, 10.0, 60.0, 4.0, STYLE) is big  # border
    assert shapes.pick(anns, 190.0, 110.0, 4.0, STYLE) is None


def test_pick_topmost_wins_and_segments():
    a1 = ArrowAnn(x1=10, y1=10, x2=110, y2=10)
    a2 = RulerAnn(x1=10, y1=10, x2=110, y2=10)
    assert shapes.pick([a1, a2], 60.0, 10.5, 3.0, STYLE) is a2
    assert shapes.pick([a1, a2], 60.0, 40.0, 3.0, STYLE) is None
    assert shapes.seg_distance(5, 5, 0, 0, 10, 0) == 5.0
    assert shapes.seg_distance(15, 0, 0, 0, 10, 0) == 5.0  # beyond the end: distance to the end point
    assert shapes.seg_distance(3, 4, 0, 0, 0, 0) == 5.0  # degenerate segment


def test_pick_pin_only_returns_pins():
    pin = PinAnn(n=1, x=20, y=20)
    rect = RectAnn(x=0, y=0, w=100, h=100)
    assert shapes.pick_pin([rect, pin], 20.5, 20.5, 4.0, STYLE) is pin
    assert shapes.pick_pin([rect], 20.5, 20.5, 4.0, STYLE) is None


def test_clamp_delta_keeps_annotations_inside_the_image():
    rect = RectAnn(x=10, y=10, w=50, h=30)
    before = shapes.state_of(rect)
    assert shapes.clamp_delta(rect, before, -100, -100, 200, 120) == (-10, -10)
    assert shapes.clamp_delta(rect, before, 500, 500, 200, 120) == (200 - 60, 120 - 40)
    pin = PinAnn(n=1, x=5, y=5)
    assert shapes.clamp_delta(pin, shapes.state_of(pin), 1000, 1000, 200, 120) == (194, 114)
    arrow = ArrowAnn(x1=20, y1=30, x2=60, y2=10)
    assert shapes.clamp_delta(arrow, shapes.state_of(arrow), -100, -100, 200, 120) == (-20, -10)


def test_moved_state_translates_every_coordinate():
    arrow = ArrowAnn(x1=20, y1=30, x2=60, y2=10)
    new = shapes.moved_state(arrow, shapes.state_of(arrow), 5, -3, 200, 120)
    assert new == {"x1": 25, "y1": 27, "x2": 65, "y2": 7}
    rect = RectAnn(x=10, y=10, w=5, h=5)
    assert shapes.moved_state(rect, shapes.state_of(rect), 3, 4, 200, 120) == {"x": 13, "y": 14, "w": 5, "h": 5}
    pin = PinAnn(n=1, x=1, y=2, note="n", color="#000000")
    st = shapes.moved_state(pin, shapes.state_of(pin), 10, 10, 200, 120)
    assert (st["x"], st["y"], st["note"]) == (11, 12, "n")


def test_state_round_trip():
    pin = PinAnn(n=1, x=1, y=2, note="a", color="#010203")
    st = shapes.state_of(pin)
    pin.x, pin.note, pin.color = 9, "b", "#FFFFFF"
    shapes.apply_state(pin, st)
    assert (pin.x, pin.y, pin.note, pin.color) == (1, 2, "a", "#010203")


def test_ruler_text_adds_logical_px_on_scaled_monitors():
    r = RulerAnn(x1=10, y1=20, x2=110, y2=20)
    assert shapes.ruler_text(r, 1.0) == "100 px"
    assert shapes.ruler_text(r, 1.5) == "100 px (66.7 logical px @150%)"
    assert shapes.ruler_text(r, 2.0) == "100 px (50 logical px @200%)"
    diag = RulerAnn(x1=0, y1=0, x2=3, y2=4)
    assert shapes.ruler_text(diag, 1.0) == "5 px"
    assert shapes.format_len(67.0) == "67" and shapes.format_len(67.4) == "67.4"


def test_describe_rows():
    kind, at, detail = shapes.describe(PinAnn(n=2, x=412, y=88, note="clipped", color="#1F2937"))
    assert kind == "Pin 2" and "412" in at and "#1F2937" in at and detail == "clipped"
    assert shapes.describe(RulerAnn(x1=0, y1=0, x2=100, y2=0), 1.5)[2].startswith("100 px (66.7")
    assert shapes.describe(RectAnn(x=1, y=2, w=3, h=4))[2] == "3 × 4 px"
