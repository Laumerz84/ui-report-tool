"""Pure (Qt-free) geometry helpers for the annotation canvas: building annotations from a
drag, hit-testing, moving with clamping, and snapshot/restore of an annotation's mutable
fields (used by undo/redo). Owner: editor builder (package B).

All coordinates are IMAGE pixels (CONTRACT.md section 4). Conventions:

  * pins, arrow ends and ruler ends are pixel INDICES, clamped to 0..W-1 / 0..H-1
    (the pixel under the cursor = floor of the continuous image coordinate);
  * rect / redact corners are pixel-grid EDGES, clamped to 0..W / 0..H (nearest grid
    line = floor(v + 0.5)); the stored box is normalised (w, h >= 0).
"""
from __future__ import annotations

import math
from typing import Any, Optional, Sequence

from ..annotdraw import AnnotStyle
from ..geometry import IntRect
from ..models import (
    Annotation,
    AnnotationType,
    ArrowAnn,
    PinAnn,
    RectAnn,
    RedactAnn,
    RulerAnn,
)

MIN_DRAG_PX = 3  # drags shorter than this (image px) create nothing


# ---------------------------------------------------------------------------
# snapping / construction
# ---------------------------------------------------------------------------
def clamp_int(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else hi if v > hi else v


def snap_pixel(v: float, size: int) -> int:
    """Continuous image coordinate -> index of the pixel containing it, clamped to 0..size-1."""
    return clamp_int(int(math.floor(v)), 0, max(0, size - 1))


def snap_edge(v: float, size: int) -> int:
    """Continuous image coordinate -> nearest pixel-grid line, clamped to 0..size."""
    return clamp_int(int(math.floor(v + 0.5)), 0, max(0, size))


def build_annotation(
    kind: AnnotationType,
    a: tuple[float, float],
    b: tuple[float, float],
    iw: int,
    ih: int,
    *,
    min_len: float = MIN_DRAG_PX,
) -> Optional[Annotation]:
    """Annotation for a press-drag-release from image point `a` to `b` (continuous image
    coordinates, may lie outside the image: they are clamped). Returns None when the drag is
    shorter than `min_len` image pixels (pass min_len=0 for a live preview)."""
    if kind in (AnnotationType.RECT, AnnotationType.REDACT):
        x1, y1 = snap_edge(a[0], iw), snap_edge(a[1], ih)
        x2, y2 = snap_edge(b[0], iw), snap_edge(b[1], ih)
        r = IntRect.from_points(x1, y1, x2, y2)
        if min_len > 0 and math.hypot(r.w, r.h) < min_len:
            return None
        cls = RectAnn if kind == AnnotationType.RECT else RedactAnn
        return cls(x=r.x, y=r.y, w=r.w, h=r.h)
    if kind in (AnnotationType.ARROW, AnnotationType.RULER):
        x1, y1 = snap_pixel(a[0], iw), snap_pixel(a[1], ih)
        x2, y2 = snap_pixel(b[0], iw), snap_pixel(b[1], ih)
        if min_len > 0 and math.hypot(x2 - x1, y2 - y1) < min_len:
            return None
        cls = ArrowAnn if kind == AnnotationType.ARROW else RulerAnn
        return cls(x1=x1, y1=y1, x2=x2, y2=y2)
    raise ValueError(f"cannot build an annotation of kind {kind!r} from a drag")


# ---------------------------------------------------------------------------
# hit testing
# ---------------------------------------------------------------------------
def seg_distance(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> float:
    """Distance from a point to the segment (x1,y1)-(x2,y2)."""
    dx, dy = x2 - x1, y2 - y1
    l2 = dx * dx + dy * dy
    if l2 <= 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / l2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def hit_priority(ann: Annotation, x: float, y: float, tol: float, style: AnnotStyle) -> Optional[int]:
    """0 = precise hit (pin body, line, box border), 1 = interior of a box, None = miss.
    (x, y) is a continuous image coordinate, `tol` the pick tolerance in image px."""
    if isinstance(ann, PinAnn):
        d = math.hypot(x - (ann.x + 0.5), y - (ann.y + 0.5))
        return 0 if d <= max(style.pin_radius + 1.0, tol) else None
    if isinstance(ann, (ArrowAnn, RulerAnn)):
        d = seg_distance(x, y, ann.x1 + 0.5, ann.y1 + 0.5, ann.x2 + 0.5, ann.y2 + 0.5)
        return 0 if d <= max(tol, style.stroke) else None
    if isinstance(ann, (RectAnn, RedactAnn)):
        outer_l, outer_t = ann.x - tol, ann.y - tol
        outer_r, outer_b = ann.x + ann.w + tol, ann.y + ann.h + tol
        if not (outer_l <= x <= outer_r and outer_t <= y <= outer_b):
            return None
        inner_l, inner_t = ann.x + tol, ann.y + tol
        inner_r, inner_b = ann.x + ann.w - tol, ann.y + ann.h - tol
        if inner_l < x < inner_r and inner_t < y < inner_b:
            return 1
        return 0
    return None


def pick(
    annotations: Sequence[Annotation], x: float, y: float, tol: float, style: AnnotStyle
) -> Optional[Annotation]:
    """Topmost annotation under the point. Precise hits (pins, lines, box borders) win over
    box interiors so a pin drawn over a big rectangle stays selectable; within one class the
    last-drawn (topmost) annotation wins."""
    for wanted in (0, 1):
        for ann in reversed(annotations):
            if hit_priority(ann, x, y, tol, style) == wanted:
                return ann
    return None


def pick_pin(
    annotations: Sequence[Annotation], x: float, y: float, tol: float, style: AnnotStyle
) -> Optional[PinAnn]:
    for ann in reversed(annotations):
        if isinstance(ann, PinAnn) and hit_priority(ann, x, y, tol, style) == 0:
            return ann
    return None


# ---------------------------------------------------------------------------
# state snapshot / move
# ---------------------------------------------------------------------------
_STATE_FIELDS: dict[type, tuple[str, ...]] = {
    PinAnn: ("x", "y", "color", "note"),
    RectAnn: ("x", "y", "w", "h"),
    RedactAnn: ("x", "y", "w", "h"),
    ArrowAnn: ("x1", "y1", "x2", "y2"),
    RulerAnn: ("x1", "y1", "x2", "y2"),
}


def state_of(ann: Annotation) -> dict[str, Any]:
    """Snapshot of the user-editable fields of an annotation (for undo/redo)."""
    return {f: getattr(ann, f) for f in _STATE_FIELDS.get(type(ann), ())}


def apply_state(ann: Annotation, state: dict[str, Any]) -> None:
    for k, v in state.items():
        setattr(ann, k, v)


def _extents(state: dict[str, Any], ann: Annotation, iw: int, ih: int):
    """(min_x, max_x, min_y, max_y, limit_x, limit_y): coordinate range the annotation
    occupies and the largest value its coordinates may reach inside the image."""
    if isinstance(ann, PinAnn):
        return state["x"], state["x"], state["y"], state["y"], iw - 1, ih - 1
    if isinstance(ann, (RectAnn, RedactAnn)):
        return state["x"], state["x"] + state["w"], state["y"], state["y"] + state["h"], iw, ih
    xs = (state["x1"], state["x2"])
    ys = (state["y1"], state["y2"])
    return min(xs), max(xs), min(ys), max(ys), iw - 1, ih - 1


def clamp_delta(ann: Annotation, before: dict[str, Any], dx: int, dy: int, iw: int, ih: int) -> tuple[int, int]:
    """Largest translation <= (dx, dy) that keeps the annotation inside the image."""
    min_x, max_x, min_y, max_y, lim_x, lim_y = _extents(before, ann, iw, ih)
    dx = clamp_int(int(dx), -min_x, max(-min_x, lim_x - max_x))
    dy = clamp_int(int(dy), -min_y, max(-min_y, lim_y - max_y))
    return dx, dy


def moved_state(ann: Annotation, before: dict[str, Any], dx: int, dy: int, iw: int, ih: int) -> dict[str, Any]:
    """`before` translated by (dx, dy) (clamped so it stays inside the image)."""
    dx, dy = clamp_delta(ann, before, dx, dy, iw, ih)
    new = dict(before)
    if isinstance(ann, PinAnn):
        new["x"] += dx
        new["y"] += dy
    elif isinstance(ann, (RectAnn, RedactAnn)):
        new["x"] += dx
        new["y"] += dy
    else:
        new["x1"] += dx
        new["x2"] += dx
        new["y1"] += dy
        new["y2"] += dy
    return new


def describe(ann: Annotation, scale_factor: float = 1.0) -> tuple[str, str, str]:
    """(kind label, position text, detail text) for lists and tooltips."""
    if isinstance(ann, PinAnn):
        return (f"Pin {ann.n}", f"({ann.x}, {ann.y})" + (f"  {ann.color}" if ann.color else ""), ann.note)
    if isinstance(ann, RulerAnn):
        return ("Ruler", f"({ann.x1}, {ann.y1}) → ({ann.x2}, {ann.y2})", ruler_text(ann, scale_factor))
    if isinstance(ann, RectAnn):
        return ("Rect", f"x={ann.x} y={ann.y}", f"{ann.w} × {ann.h} px")
    if isinstance(ann, RedactAnn):
        return ("Redact", f"x={ann.x} y={ann.y}", f"{ann.w} × {ann.h} px")
    if isinstance(ann, ArrowAnn):
        return ("Arrow", f"({ann.x1}, {ann.y1}) → ({ann.x2}, {ann.y2})", "")
    return (str(ann.type), "", "")


def format_len(v: float) -> str:
    """100.0 -> '100', 67.4 -> '67.4'."""
    return f"{v:.1f}".rstrip("0").rstrip(".")


def ruler_text(ann: RulerAnn, scale_factor: float = 1.0) -> str:
    """'100 px' at 100% scale; '100 px (67 logical px @150%)' on a scaled monitor."""
    length = ann.length_px
    text = f"{format_len(length)} px"
    if abs(scale_factor - 1.0) > 1e-6:
        logical = round(length / scale_factor, 1)
        text += f" ({format_len(logical)} logical px @{int(round(scale_factor * 100))}%)"
    return text
