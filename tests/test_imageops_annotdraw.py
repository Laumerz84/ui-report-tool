"""Tests for the shared pixel helpers and the shared annotation painter (headless)."""
import pytest
from PySide6.QtGui import QColor, QImage, QPainter

from uireport.annotdraw import (
    AnnotStyle,
    annotation_bounds,
    draw_annotation,
    render_annotated,
    render_shot_annotated,
)
from uireport.geometry import IntRect
from uireport.imageops import apply_redactions, pixelate_region, sample_hex
from uireport.models import ArrowAnn, PinAnn, RectAnn, RedactAnn, RulerAnn

WHITE = 0xFFFFFFFF


@pytest.fixture(autouse=True)
def _gui_app(qapp):
    """Painting text needs a QGuiApplication (font database), even on QImage."""
    return qapp


def _nonwhite_bbox(img: QImage):
    xs, ys = [], []
    for y in range(img.height()):
        for x in range(img.width()):
            if img.pixel(x, y) != WHITE:
                xs.append(x)
                ys.append(y)
    assert xs, "nothing was drawn"
    return min(xs), min(ys), max(xs), max(ys)


def _checker(w=120, h=80) -> QImage:
    img = QImage(w, h, QImage.Format.Format_ARGB32)
    for y in range(h):
        for x in range(w):
            img.setPixelColor(x, y, QColor(255, 0, 0) if (x // 3 + y // 3) % 2 else QColor(0, 0, 255))
    return img


# ---- sampling --------------------------------------------------------------
def test_sample_hex_reads_original_pixel_uppercase(make_image):
    img = make_image(50, 50, "#FFFFFF")
    img.setPixelColor(12, 34, QColor("#1f2937"))
    assert sample_hex(img, 12, 34) == "#1F2937"
    assert sample_hex(img, 0, 0) == "#FFFFFF"
    assert sample_hex(img, -5, 999) == "#FFFFFF"  # clamped, no exception


# ---- redaction -------------------------------------------------------------
def test_apply_redactions_pixelates_box_only_and_never_mutates_input():
    src = _checker()
    before = src.copy()
    out = apply_redactions(src, [RedactAnn(x=20, y=10, w=60, h=40)])
    assert src == before  # input untouched
    assert out.size() == src.size()
    inside_changed = sum(out.pixel(x, y) != src.pixel(x, y) for x in range(20, 80) for y in range(10, 50))
    assert inside_changed > 0.3 * 60 * 40
    for x, y in ((0, 0), (19, 10), (80, 10), (30, 9), (30, 50), (119, 79)):
        assert out.pixel(x, y) == src.pixel(x, y), (x, y)
    # blocks are large: neighbouring pixels inside the box mostly match
    runs = sum(out.pixel(x, 30) == out.pixel(x + 1, 30) for x in range(20, 79))
    assert runs > 40


def test_redaction_solid_and_out_of_bounds():
    src = _checker()
    out = apply_redactions(src, [RedactAnn(x=100, y=60, w=500, h=500, style="solid")])
    assert out.pixelColor(110, 70) == QColor(0, 0, 0)
    assert out.pixel(0, 0) == src.pixel(0, 0)
    apply_redactions(src, [RedactAnn(x=1000, y=1000, w=10, h=10)])  # fully outside: no crash


def test_pixelate_region_in_place():
    img = _checker()
    pixelate_region(img, IntRect(0, 0, 30, 30), block=10)
    assert img.pixel(0, 0) == img.pixel(5, 5)


# ---- pin placement (coordinates must line up with the PNG) ------------------
def test_pin_is_centred_on_its_pixel(make_image):
    img = make_image(300, 200)
    pin = PinAnn(n=1, x=150, y=100)
    out = render_annotated(img, [pin], 1.0)
    x0, y0, x1, y1 = _nonwhite_bbox(out)
    # centre of the drawn pin == centre of pixel (150, 100), within a pixel
    assert abs((x0 + x1 + 1) / 2 - 150.5) <= 1.0
    assert abs((y0 + y1 + 1) / 2 - 100.5) <= 1.0
    # the pin body colour is present at its centre area, source image untouched
    assert img.pixel(150, 100) == WHITE


def test_pin_at_report_example_coordinates(make_image):
    img = make_image(600, 300)
    out = render_annotated(img, [PinAnn(n=7, x=412, y=88)], 1.0)
    x0, y0, x1, y1 = _nonwhite_bbox(out)
    assert x0 < 412 < x1 and y0 < 88 < y1
    assert abs((x0 + x1 + 1) / 2 - 412.5) <= 1.0 and abs((y0 + y1 + 1) / 2 - 88.5) <= 1.0


def test_marks_scale_with_monitor_scale(make_image):
    img = make_image(400, 400)
    small = _nonwhite_bbox(render_annotated(img, [PinAnn(n=1, x=200, y=200)], 1.0))
    big = _nonwhite_bbox(render_annotated(img, [PinAnn(n=1, x=200, y=200)], 1.5))
    assert (big[2] - big[0]) > (small[2] - small[0])
    assert AnnotStyle.for_scale(1.5).pin_radius > AnnotStyle.for_scale(1.0).pin_radius
    assert AnnotStyle.for_scale(0.5).scale == 1.0  # never below 100%


# ---- other shapes -----------------------------------------------------------
def test_rect_arrow_ruler_draw_inside_expected_area(make_image):
    img = make_image(300, 200)
    out = render_annotated(
        img,
        [RectAnn(x=50, y=40, w=100, h=60), ArrowAnn(x1=200, y1=150, x2=260, y2=100), RulerAnn(x1=20, y1=180, x2=120, y2=180)],
        1.0,
    )
    x0, y0, x1, y1 = _nonwhite_bbox(out)
    assert x0 >= 5 and y0 >= 5 and x1 <= 299 and y1 <= 199
    assert out.pixel(50, 70) != WHITE  # rect left edge stroke
    assert out.pixel(70, 180) != WHITE  # ruler line


def test_zero_length_shapes_do_not_crash(make_image):
    out = render_annotated(make_image(50, 50), [ArrowAnn(x1=5, y1=5, x2=5, y2=5), RulerAnn(x1=9, y1=9, x2=9, y2=9)])
    assert out.size().width() == 50


def test_redact_boxes_are_pixelated_in_render_and_not_painted_as_shapes():
    src = _checker()
    out = render_annotated(src, [RedactAnn(x=10, y=10, w=50, h=30), PinAnn(n=1, x=100, y=60)], 1.0)
    assert out.pixel(15, 15) != src.pixel(15, 15) or out.pixel(16, 16) != src.pixel(16, 16)
    # redaction is real pixel data, not a removable overlay: same result via apply_redactions
    ref = apply_redactions(src, [RedactAnn(x=10, y=10, w=50, h=30)])
    assert out.pixel(12, 12) == ref.pixel(12, 12)


def test_preview_redact_paints_only_when_asked(make_image):
    img = make_image(100, 100)
    for preview, expect_drawn in ((False, False), (True, True)):
        canvas = img.copy()
        p = QPainter(canvas)
        draw_annotation(p, RedactAnn(x=10, y=10, w=40, h=40), AnnotStyle.for_scale(1.0), preview_redact=preview)
        p.end()
        assert (canvas.pixel(30, 30) != WHITE) == expect_drawn


def test_render_shot_annotated_uses_monitor_scale(make_shot):
    widths = {}
    for pct in (100, 150):
        shot = make_shot(300, 200, scale_percent=pct)
        shot.add_annotation(PinAnn(x=150, y=100))
        x0, _, x1, _ = _nonwhite_bbox(render_shot_annotated(shot))
        widths[pct] = x1 - x0 + 1
    # the visible (coloured) disc grows by ~1.5x at 150%
    assert 1.3 < widths[150] / widths[100] < 1.7


def test_annotation_bounds_contain_the_drawing(make_image):
    style = AnnotStyle.for_scale(1.0)
    ann = PinAnn(n=3, x=80, y=60)
    b = annotation_bounds(ann, style)
    x0, y0, x1, y1 = _nonwhite_bbox(render_annotated(make_image(200, 150), [ann], 1.0))
    assert b.left() <= x0 and b.top() <= y0 and b.right() >= x1 and b.bottom() >= y1
