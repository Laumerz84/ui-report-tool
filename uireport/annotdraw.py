"""The ONE place annotations are painted. The editor canvas uses it for the live view and
the output package uses it for the saved *_annotated.png, so what you see is what you get.

Painter contract: the QPainter must already be in IMAGE-PIXEL coordinates. For the saved
image that is the identity transform on a QImage; the editor canvas applies its zoom
transform (painter.scale(zoom, zoom)) before calling in. Stroke widths, pin radius and
font size are expressed in image pixels and scale with the capture monitor's Windows
scale (AnnotStyle.for_scale), so a 150% monitor gets 1.5x thicker marks.

A QGuiApplication (or QApplication) must exist before painting: pin numbers and ruler
labels use text, and Qt aborts the process if the font database is touched without one.

Pin coordinates are pixel indices; the pin is centred on the pixel centre (x + 0.5, y + 0.5)
so a pin at (412, 88) is symmetric around that pixel in the PNG.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QPainter,
    QPen,
    QPolygonF,
)

from .imageops import apply_redactions, ensure_argb32
from .models import (
    Annotation,
    ArrowAnn,
    PinAnn,
    RectAnn,
    RedactAnn,
    RulerAnn,
    Shot,
)


@dataclass(frozen=True)
class AnnotStyle:
    scale: float = 1.0
    pin_color: str = "#E5484D"
    rect_color: str = "#E5484D"
    arrow_color: str = "#E5484D"
    ruler_color: str = "#0EA5E9"
    halo_color: str = "#FFFFFF"
    text_color: str = "#FFFFFF"
    label_bg: str = "#111827"

    @classmethod
    def for_scale(cls, scale: float) -> "AnnotStyle":
        return cls(scale=max(1.0, float(scale or 1.0)))

    @property
    def stroke(self) -> float:
        return round(2.0 * self.scale, 2)

    @property
    def halo(self) -> float:
        return self.stroke + 2.0 * max(1.0, round(self.scale))

    @property
    def pin_radius(self) -> float:
        return round(12.0 * self.scale, 2)

    @property
    def font_px(self) -> int:
        return max(9, int(round(12 * self.scale)))

    @property
    def arrow_head(self) -> float:
        return round(14.0 * self.scale, 2)

    @property
    def tick(self) -> float:
        return round(6.0 * self.scale, 2)


def _center(x: int, y: int) -> QPointF:
    return QPointF(x + 0.5, y + 0.5)


def _pen(color: str, width: float, *, dashed: bool = False) -> QPen:
    pen = QPen(QColor(color))
    pen.setWidthF(width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    if dashed:
        pen.setStyle(Qt.PenStyle.DashLine)
    return pen


def _font(px: int, bold: bool = True) -> QFont:
    f = QFont("Segoe UI")
    f.setPixelSize(px)
    f.setBold(bold)
    return f


def annotation_bounds(ann: Annotation, style: AnnotStyle) -> QRectF:
    """Bounding box in image pixels, including strokes/halo/labels. Handy for hit
    testing and dirty-rect repaints."""
    m = style.halo + 2
    if isinstance(ann, PinAnn):
        r = style.pin_radius + style.halo
        c = _center(ann.x, ann.y)
        return QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)
    if isinstance(ann, (RectAnn, RedactAnn)):
        return QRectF(ann.x - m, ann.y - m, ann.w + 2 * m, ann.h + 2 * m)
    if isinstance(ann, ArrowAnn):
        r = QRectF(QPointF(ann.x1 + 0.5, ann.y1 + 0.5), QPointF(ann.x2 + 0.5, ann.y2 + 0.5)).normalized()
        g = m + style.arrow_head
        return r.adjusted(-g, -g, g, g)
    if isinstance(ann, RulerAnn):
        r = QRectF(QPointF(ann.x1 + 0.5, ann.y1 + 0.5), QPointF(ann.x2 + 0.5, ann.y2 + 0.5)).normalized()
        g = m + style.tick + 6 * style.scale + 60 * style.scale  # room for the length label
        return r.adjusted(-g, -g, g, g)
    return QRectF()


def draw_annotation(
    painter: QPainter,
    ann: Annotation,
    style: AnnotStyle,
    *,
    preview_redact: bool = False,
    selected: bool = False,
) -> None:
    """Paint one annotation. Redaction boxes are only painted when `preview_redact` is True
    (editor live view); the saved images get real pixelation from imageops instead."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    try:
        if isinstance(ann, PinAnn):
            _draw_pin(painter, ann, style)
        elif isinstance(ann, RectAnn):
            _draw_rect(painter, ann, style)
        elif isinstance(ann, ArrowAnn):
            _draw_arrow(painter, ann, style)
        elif isinstance(ann, RulerAnn):
            _draw_ruler(painter, ann, style)
        elif isinstance(ann, RedactAnn):
            if preview_redact:
                _draw_redact_preview(painter, ann, style)
        if selected:
            b = annotation_bounds(ann, style)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(_pen("#FFFFFF", max(1.0, style.scale)))
            painter.drawRect(b)
            painter.setPen(_pen("#2563EB", max(1.0, style.scale), dashed=True))
            painter.drawRect(b)
    finally:
        painter.restore()


def draw_annotations(
    painter: QPainter,
    annotations: Iterable[Annotation],
    style: AnnotStyle,
    *,
    preview_redact: bool = False,
    selected_id: Optional[str] = None,
) -> None:
    for ann in annotations:
        draw_annotation(
            painter, ann, style, preview_redact=preview_redact, selected=(ann.id == selected_id)
        )


def render_annotated(
    base: QImage,
    annotations: Iterable[Annotation],
    scale: float = 1.0,
    *,
    redact: bool = True,
) -> QImage:
    """Return a NEW image: `base` with redactions applied (when redact=True) and every
    non-redaction annotation painted on top. `base` itself is never modified."""
    anns = list(annotations)
    if redact:
        img = apply_redactions(base, [a for a in anns if isinstance(a, RedactAnn)])
    else:
        img = ensure_argb32(base).copy()
    style = AnnotStyle.for_scale(scale)
    p = QPainter(img)
    try:
        draw_annotations(p, [a for a in anns if not isinstance(a, RedactAnn)], style)
    finally:
        p.end()
    return img


def render_shot_annotated(shot: Shot) -> QImage:
    """Convenience: annotated render of shot.image using the shot's own monitor scale."""
    if shot.image is None:
        raise ValueError("shot has no image")
    return render_annotated(shot.image, shot.annotations, shot.scale_factor)


# ---------------------------------------------------------------------------
# individual shapes
# ---------------------------------------------------------------------------
def _draw_pin(p: QPainter, a: PinAnn, s: AnnotStyle) -> None:
    c = _center(a.x, a.y)
    r = s.pin_radius
    p.setPen(_pen(s.halo_color, 2.0 * max(1.0, round(s.scale))))
    p.setBrush(QBrush(QColor(s.pin_color)))
    p.drawEllipse(c, r, r)
    text = str(a.n if a.n > 0 else "?")
    px = int(round(13 * s.scale)) if len(text) < 2 else max(9, int(round(11 * s.scale)))
    p.setFont(_font(px))
    p.setPen(QColor(s.text_color))
    p.drawText(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r), int(Qt.AlignmentFlag.AlignCenter), text)


def _draw_rect(p: QPainter, a: RectAnn, s: AnnotStyle) -> None:
    rect = QRectF(a.x, a.y, a.w, a.h)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(_pen(s.halo_color, s.halo))
    p.drawRect(rect)
    fill = QColor(s.rect_color)
    fill.setAlpha(30)
    p.setBrush(QBrush(fill))
    p.setPen(_pen(s.rect_color, s.stroke))
    p.drawRect(rect)


def _arrow_head_polygon(x1: float, y1: float, x2: float, y2: float, size: float) -> QPolygonF:
    ang = math.atan2(y2 - y1, x2 - x1)
    spread = math.radians(24)
    p1 = QPointF(x2 - size * math.cos(ang - spread), y2 - size * math.sin(ang - spread))
    p2 = QPointF(x2 - size * math.cos(ang + spread), y2 - size * math.sin(ang + spread))
    return QPolygonF([QPointF(x2, y2), p1, p2])


def _draw_arrow(p: QPainter, a: ArrowAnn, s: AnnotStyle) -> None:
    x1, y1, x2, y2 = a.x1 + 0.5, a.y1 + 0.5, a.x2 + 0.5, a.y2 + 0.5
    if math.hypot(x2 - x1, y2 - y1) < 1:
        return
    head = _arrow_head_polygon(x1, y1, x2, y2, s.arrow_head)
    # halo pass, then colour pass
    for color, width in ((s.halo_color, s.halo), (s.arrow_color, s.stroke)):
        p.setPen(_pen(color, width))
        p.setBrush(QBrush(QColor(color)))
        p.drawLine(QLineF(x1, y1, x2, y2))
        p.drawPolygon(head)


def _draw_ruler(p: QPainter, a: RulerAnn, s: AnnotStyle) -> None:
    x1, y1, x2, y2 = a.x1 + 0.5, a.y1 + 0.5, a.x2 + 0.5, a.y2 + 0.5
    length = math.hypot(x2 - x1, y2 - y1)
    if length < 0.5:
        return
    ux, uy = (x2 - x1) / length, (y2 - y1) / length
    nx, ny = -uy, ux  # unit normal
    t = s.tick
    p.setBrush(Qt.BrushStyle.NoBrush)
    for color, width in ((s.halo_color, s.halo), (s.ruler_color, s.stroke)):
        p.setPen(_pen(color, width))
        p.drawLine(QLineF(x1, y1, x2, y2))
        p.drawLine(QLineF(x1 - nx * t, y1 - ny * t, x1 + nx * t, y1 + ny * t))
        p.drawLine(QLineF(x2 - nx * t, y2 - ny * t, x2 + nx * t, y2 + ny * t))
    # length label (rounded to whole px) beside the midpoint
    label = f"{int(round(a.length_px))} px"
    font = _font(s.font_px)
    fm = QFontMetricsF(font)
    tw, th = fm.horizontalAdvance(label), fm.height()
    pad = 4 * s.scale
    mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    off = t + th / 2 + pad + 2 * s.scale
    if ny > 0:  # keep the label above a mostly horizontal ruler, right of a mostly vertical one
        off = -off
    lx, ly = mx + nx * off, my + ny * off
    box = QRectF(lx - tw / 2 - pad, ly - th / 2 - pad / 2, tw + 2 * pad, th + pad)
    p.setPen(Qt.PenStyle.NoPen)
    bg = QColor(s.label_bg)
    bg.setAlpha(230)
    p.setBrush(QBrush(bg))
    p.drawRoundedRect(box, 4 * s.scale, 4 * s.scale)
    p.setFont(font)
    p.setPen(QColor(s.text_color))
    p.drawText(box, int(Qt.AlignmentFlag.AlignCenter), label)


def _draw_redact_preview(p: QPainter, a: RedactAnn, s: AnnotStyle) -> None:
    rect = QRectF(a.x, a.y, a.w, a.h)
    fill = QColor("#111827")
    fill.setAlpha(225)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(fill))
    p.drawRect(rect)
    p.setBrush(QBrush(QColor(255, 255, 255, 60), Qt.BrushStyle.BDiagPattern))
    p.drawRect(rect)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(_pen("#F59E0B", max(1.0, s.scale), dashed=True))
    p.drawRect(rect)
