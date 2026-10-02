"""Annotation canvas: shows one shot's original image and edits its annotations.
Owner: editor builder (package B).

View model
----------
`zoom` is the view scale: LOGICAL widget pixels per IMAGE pixel. Fit-to-window is the default
(never upscales beyond 1:1); "1:1" means exactly one image pixel per DEVICE pixel, i.e.
`zoom == 1 / devicePixelRatio` logical px per image px, so the screenshot is sharp. The QImage
itself always has devicePixelRatio 1.0 (image pixels are physical pixels).

    widget point (logical)  <->  image point (float image px)
        image = (widget - offset) / zoom          widget = image * zoom + offset

`offset` centres the image when it is smaller than the viewport, else it is minus the scroll
position (the image origin is then snapped to the device-pixel grid). `widget_to_image` and
`image_to_widget` are the only converters and are exact inverses of each other.

Painting keeps a cached, pre-scaled pixmap when zoomed out (so a 5120x1440 capture repaints in
about a millisecond) and blits only the visible source rectangle, unsmoothed, when zoomed in.
Annotations are painted by annotdraw.draw_annotations inside a painter that is transformed to
image pixels, so the live view matches the saved *_annotated.png.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Optional

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QWheelEvent,
)
from PySide6.QtWidgets import QLineEdit, QScrollBar, QToolTip, QWidget

from .. import imageops
from ..annotdraw import AnnotStyle, annotation_bounds, draw_annotation, draw_annotations
from ..models import (
    Annotation,
    AnnotationType,
    PinAnn,
    RectAnn,
    RedactAnn,
    RulerAnn,
    Shot,
)
from . import shapes


class Tool(StrEnum):
    SELECT = "select"  # select / move / delete existing annotations
    PIN = "pin"
    RECT = "rect"
    ARROW = "arrow"
    RULER = "ruler"
    REDACT = "redact"


TOOL_LABELS: dict[Tool, str] = {
    Tool.SELECT: "Select",
    Tool.PIN: "Pin",
    Tool.RECT: "Rect",
    Tool.ARROW: "Arrow",
    Tool.RULER: "Ruler",
    Tool.REDACT: "Redact",
}
# Single-key shortcuts; only active while the canvas has keyboard focus.
TOOL_KEYS: dict[Tool, str] = {
    Tool.SELECT: "V",
    Tool.PIN: "P",
    Tool.RECT: "R",
    Tool.ARROW: "A",
    Tool.RULER: "M",
    Tool.REDACT: "B",
}
TOOL_TIPS: dict[Tool, str] = {
    Tool.SELECT: "Select / move: click an annotation to select it, drag to move it, Delete removes it",
    Tool.PIN: "Pin: click to drop a numbered pin, then type its note",
    Tool.RECT: "Rectangle: drag to outline an area",
    Tool.ARROW: "Arrow: drag from the tail to the head",
    Tool.RULER: "Ruler: drag between two points to measure the distance in image pixels",
    Tool.REDACT: "Redact / blur box: drag over anything sensitive (pixelated in BOTH saved images)",
}
_DRAW_KIND = {
    Tool.RECT: AnnotationType.RECT,
    Tool.ARROW: AnnotationType.ARROW,
    Tool.RULER: AnnotationType.RULER,
    Tool.REDACT: AnnotationType.REDACT,
}

MIN_ZOOM = 0.02
MAX_ZOOM = 32.0
FIT_MARGIN = 6  # logical px of breathing room around the image in fit mode
PICK_TOLERANCE = 6.0  # logical px
HISTORY_LIMIT = 200


# ---------------------------------------------------------------------------
# undo / redo commands (per shot, in memory)
# ---------------------------------------------------------------------------
class _AddCmd:
    def __init__(self, ann: Annotation, index: int) -> None:
        self.ann, self.index = ann, index

    def redo(self, shot: Shot) -> None:
        shot.add_annotation(self.ann, self.index)

    def undo(self, shot: Shot) -> None:
        shot.remove_annotation(self.ann.id)


class _RemoveCmd:
    def __init__(self, ann: Annotation, index: int) -> None:
        self.ann, self.index = ann, index

    def redo(self, shot: Shot) -> None:
        shot.remove_annotation(self.ann.id)

    def undo(self, shot: Shot) -> None:
        shot.add_annotation(self.ann, self.index)


class _ChangeCmd:
    def __init__(self, ann: Annotation, before: dict, after: dict) -> None:
        self.ann, self.before, self.after = ann, before, after

    def redo(self, shot: Shot) -> None:
        shapes.apply_state(self.ann, self.after)

    def undo(self, shot: Shot) -> None:
        shapes.apply_state(self.ann, self.before)


@dataclass
class _Drag:
    kind: str  # "draw" | "move" | "pan"
    start_img: tuple[float, float] = (0.0, 0.0)
    start_pos: QPointF = None  # type: ignore[assignment]
    ann: Optional[Annotation] = None
    before: Optional[dict] = None
    moved: bool = False
    pan_h: int = 0
    pan_v: int = 0


class _NoteEdit(QLineEdit):
    """Small inline editor for a pin's note (child of the canvas, not a popup window, so it
    never steals window activation)."""

    cancelled = Signal()

    def keyPressEvent(self, e: QKeyEvent) -> None:
        if e.key() == Qt.Key.Key_Escape:
            e.accept()
            self.cancelled.emit()
            return
        super().keyPressEvent(e)


class AnnotationCanvas(QWidget):
    """Draws `shot.image` (fit-to-window, plus 1:1 / zoom) and, on top, the shot's
    annotations using uireport.annotdraw.draw_annotations (redactions with
    preview_redact=True) inside a painter transformed to image pixels.

    Editing rules:
      * PIN: click -> PinAnn at the clicked IMAGE pixel; colour = imageops.sample_hex(
        shot.image, x, y) from the ORIGINAL image (never from the annotated render);
        then a small inline editor for the pin's note opens; pin numbers stay 1..N via
        shot.add_annotation / remove_annotation.
      * RECT / REDACT / ARROW / RULER: press-drag-release; ignore drags shorter than 3
        image px; RectAnn/RedactAnn normalised (x, y, w, h >= 0); all coordinates clamped
        to the image and stored as ints in IMAGE PIXELS. A live ruler shows its length.
      * SELECT: click selects the topmost annotation, drag moves it, Delete removes it.
      * Undo / redo (Ctrl+Z / Ctrl+Y) per shot, in memory.
    The canvas mutates `shot.annotations` directly (through Shot.add_annotation /
    remove_annotation) and never modifies `shot.image`.

    Keyboard (only while the canvas has focus, so typing in the caption is never hijacked):
    V/P/R/A/M/B pick a tool, Delete/Backspace removes the selection, Ctrl+Z / Ctrl+Y (or
    Ctrl+Shift+Z) undo / redo, arrow keys nudge the selection by 1 px (Shift: 10 px), F fits,
    1 shows 1:1, +/- zoom, Esc cancels a drag.

    Extra (optional) API on top of the contract: signals `selection_changed`, `history_changed`,
    `view_changed`; methods `shot`, `selected_id`, `select`, `set_pin_note`, `delete_annotation`,
    `edit_pin_note`, `commit_pending_edit`, `can_undo`, `can_redo`, `zoom`, `set_zoom`, `is_fit`,
    `set_fit`, `zoom_fit`, `zoom_actual`, `zoom_in`, `zoom_out`, `device_pixel_ratio`.
    """

    annotations_changed = Signal()  # any add / move / delete / note edit
    tool_changed = Signal(object)  # Tool
    selection_changed = Signal()  # selected annotation changed (EditorWindow keeps its list in sync)
    history_changed = Signal()  # undo / redo availability may have changed
    view_changed = Signal()  # zoom / scroll changed

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._shot: Optional[Shot] = None
        self._tool: Tool = Tool.SELECT
        self._selected: Optional[str] = None
        self._fit = True
        self._zoom = 1.0
        self._dpr_override: Optional[float] = None  # test hook
        self._undo: list = []
        self._redo: list = []
        self._drag: Optional[_Drag] = None
        self._preview: Optional[Annotation] = None
        self._hud: Optional[str] = None
        self._hud_pos = QPointF()
        self._scaled: Optional[tuple[tuple, QPixmap]] = None
        self._layout_sig: Optional[tuple] = None
        self._need_h = False
        self._need_v = False
        self._viewport = (0.0, 0.0)
        self._note_pin: Optional[PinAnn] = None
        self._note_is_new = False

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setMinimumSize(200, 140)

        self._hbar = QScrollBar(Qt.Orientation.Horizontal, self)
        self._vbar = QScrollBar(Qt.Orientation.Vertical, self)
        for bar in (self._hbar, self._vbar):
            bar.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            bar.hide()
            bar.valueChanged.connect(self._on_scroll)

        self._note = _NoteEdit(self)
        self._note.hide()
        self._note.editingFinished.connect(self._on_note_finished)
        self._note.cancelled.connect(self._on_note_cancelled)
        self._update_cursor()

    # ------------------------------------------------------------------
    # basic accessors
    # ------------------------------------------------------------------
    def sizeHint(self) -> QSize:
        return QSize(760, 420)

    def shot(self) -> Optional[Shot]:
        return self._shot

    def tool(self) -> Tool:
        return self._tool

    def selected_id(self) -> Optional[str]:
        return self._selected

    def device_pixel_ratio(self) -> float:
        if self._dpr_override:
            return float(self._dpr_override)
        try:
            return float(self.devicePixelRatioF() or 1.0)
        except Exception:  # pragma: no cover - defensive
            return 1.0

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def _img_size(self) -> tuple[int, int]:
        s = self._shot
        if s is None:
            return (0, 0)
        if s.image is not None:
            return (int(s.image.width()), int(s.image.height()))
        return (0, 0)

    def _has_image(self) -> bool:
        return self._shot is not None and self._shot.image is not None and self._img_size()[0] > 0

    def _style(self) -> AnnotStyle:
        return AnnotStyle.for_scale(self._shot.scale_factor if self._shot else 1.0)

    # ------------------------------------------------------------------
    # shot / tool
    # ------------------------------------------------------------------
    def set_shot(self, shot: Optional[Shot]) -> None:
        """Show `shot` (None = empty state). Resets selection and the undo stack."""
        self.commit_pending_edit()
        self._shot = shot
        self._selected = None
        self._undo.clear()
        self._redo.clear()
        self._drag = None
        self._preview = None
        self._hud = None
        self._scaled = None
        self._layout_sig = None
        self._fit = True
        self._note.hide()
        self._note_pin = None
        self._relayout()
        self.update()
        self.selection_changed.emit()
        self.history_changed.emit()
        self.view_changed.emit()

    def set_tool(self, tool: Tool) -> None:
        tool = Tool(tool)
        if tool == self._tool:
            return
        self.commit_pending_edit()
        self._cancel_drag()
        self._tool = tool
        self._update_cursor()
        self.tool_changed.emit(tool)

    def _update_cursor(self) -> None:
        if self._drag is not None and self._drag.kind == "pan":
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif self._tool == Tool.SELECT:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)

    # ------------------------------------------------------------------
    # view geometry (zoom / scroll / coordinate mapping)
    # ------------------------------------------------------------------
    def is_fit(self) -> bool:
        return self._fit

    def zoom(self) -> float:
        """Effective view scale (logical widget px per image px)."""
        return self._effective_zoom()

    def _fit_zoom(self) -> float:
        iw, ih = self._img_size()
        if iw <= 0 or ih <= 0:
            return 1.0
        w = max(1, self.width() - 2 * FIT_MARGIN)
        h = max(1, self.height() - 2 * FIT_MARGIN)
        z = min(w / iw, h / ih, 1.0 / self.device_pixel_ratio())
        return max(MIN_ZOOM, z)

    def _effective_zoom(self) -> float:
        return self._fit_zoom() if self._fit else self._zoom

    def set_fit(self, fit: bool = True) -> None:
        if fit:
            self._fit = True
        else:
            self._zoom = self._effective_zoom()
            self._fit = False
        self._relayout()
        self.update()
        self.view_changed.emit()

    def zoom_fit(self) -> None:
        self.set_fit(True)

    def zoom_actual(self) -> None:
        """1:1 = one image pixel per DEVICE pixel."""
        self.set_zoom(1.0 / self.device_pixel_ratio())

    def zoom_in(self) -> None:
        self.set_zoom(self._effective_zoom() * 1.25)

    def zoom_out(self) -> None:
        self.set_zoom(self._effective_zoom() / 1.25)

    def set_zoom(self, zoom: float, anchor: Optional[QPointF] = None) -> None:
        """Zoom to `zoom` (logical px per image px), keeping the widget point `anchor` (default:
        the viewport centre) over the same image point."""
        if not self._has_image():
            return
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, float(zoom)))
        self._ensure_layout()
        old_z, ox, oy = self._layout()
        if anchor is None:
            anchor = QPointF(self._viewport[0] / 2.0, self._viewport[1] / 2.0)
        ix, iy = (anchor.x() - ox) / old_z, (anchor.y() - oy) / old_z
        self._fit = False
        self._zoom = zoom
        self._relayout()
        self._hbar.setValue(int(round(ix * zoom - anchor.x())))
        self._vbar.setValue(int(round(iy * zoom - anchor.y())))
        self.update()
        self.view_changed.emit()

    def _ensure_layout(self) -> None:
        iw, ih = self._img_size()
        sig = (self.width(), self.height(), self._effective_zoom(), iw, ih)
        if sig != self._layout_sig:
            self._relayout()

    def _relayout(self) -> None:
        """Recompute scrollbar visibility / ranges / geometry for the current size and zoom."""
        iw, ih = self._img_size()
        W, H = self.width(), self.height()
        z = self._effective_zoom()
        self._layout_sig = (W, H, z, iw, ih)
        if iw <= 0 or ih <= 0:
            self._need_h = self._need_v = False
            self._viewport = (float(W), float(H))
            self._hbar.hide()
            self._vbar.hide()
            return
        cw, ch = iw * z, ih * z
        sbw = self._vbar.sizeHint().width()
        sbh = self._hbar.sizeHint().height()
        need_h = need_v = False
        for _ in range(2):
            vw = W - (sbw if need_v else 0)
            vh = H - (sbh if need_h else 0)
            need_h = cw > vw + 0.5
            need_v = ch > vh + 0.5
        vw = W - (sbw if need_v else 0)
        vh = H - (sbh if need_h else 0)
        self._need_h, self._need_v = need_h, need_v
        self._viewport = (float(vw), float(vh))
        self._hbar.setGeometry(0, H - sbh, max(0, vw), sbh)
        self._vbar.setGeometry(W - sbw, 0, sbw, max(0, vh))
        self._hbar.setRange(0, max(0, int(math.ceil(cw - vw))) if need_h else 0)
        self._vbar.setRange(0, max(0, int(math.ceil(ch - vh))) if need_v else 0)
        self._hbar.setPageStep(max(1, int(vw)))
        self._vbar.setPageStep(max(1, int(vh)))
        self._hbar.setSingleStep(40)
        self._vbar.setSingleStep(40)
        self._hbar.setVisible(need_h)
        self._vbar.setVisible(need_v)
        if self._note_pin is not None:
            self._place_note()

    def _layout(self) -> tuple[float, float, float]:
        """(zoom, origin x, origin y): where the image's top-left corner sits in the widget."""
        if not self._has_image():
            return (1.0, 0.0, 0.0)
        self._ensure_layout()
        z = self._effective_zoom()
        iw, ih = self._img_size()
        cw, ch = iw * z, ih * z
        vw, vh = self._viewport
        ox = (vw - cw) / 2.0 if not self._need_h else -float(self._hbar.value())
        oy = (vh - ch) / 2.0 if not self._need_v else -float(self._vbar.value())
        d = self.device_pixel_ratio()
        ox = round(ox * d) / d  # snap the image origin to the device-pixel grid (sharp 1:1)
        oy = round(oy * d) / d
        return (z, ox, oy)

    def widget_to_image(self, x: float, y: float) -> tuple[float, float]:
        """Map a widget-local logical point to image pixel coordinates (float). Pure
        function of the current zoom / offset / devicePixelRatio; unit-testable."""
        z, ox, oy = self._layout()
        return ((x - ox) / z, (y - oy) / z)

    def image_to_widget(self, x: float, y: float) -> tuple[float, float]:
        z, ox, oy = self._layout()
        return (x * z + ox, y * z + oy)

    def image_rect_on_widget(self) -> QRectF:
        """The image's rectangle in widget logical coordinates."""
        z, ox, oy = self._layout()
        iw, ih = self._img_size()
        return QRectF(ox, oy, iw * z, ih * z)

    def _on_scroll(self, _value: int) -> None:
        if self._note_pin is not None:
            self._place_note()
        self.update()
        self.view_changed.emit()

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        self._relayout()
        if self._fit:
            self.view_changed.emit()  # the fit zoom follows the widget size

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        self._relayout()

    # ------------------------------------------------------------------
    # painting
    # ------------------------------------------------------------------
    def _background(self) -> QColor:
        return self.palette().color(QPalette.ColorRole.Window).darker(112)

    def paintEvent(self, e) -> None:  # noqa: N802
        p = QPainter(self)
        try:
            p.fillRect(self.rect(), self._background())
            if self._shot is None:
                self._paint_message(p, "No screenshot yet\nCapture with the hotkey or the tray icon.")
                return
            if not self._has_image():
                self._paint_message(p, "Image not available for this shot\n(loaded from a saved report).")
                return
            z, ox, oy = self._layout()
            iw, ih = self._img_size()
            target = QRectF(ox, oy, iw * z, ih * z)
            # frame
            p.setPen(QPen(self.palette().color(QPalette.ColorRole.Mid), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(target.adjusted(-0.5, -0.5, 0.5, 0.5))
            view = QRectF(0, 0, self._viewport[0], self._viewport[1])
            visible = target.intersected(view)
            if visible.isEmpty():
                return
            p.save()
            p.setClipRect(visible)
            self._paint_image(p, z, ox, oy, iw, ih, view)
            # annotations, in image pixels, so the live view equals the saved PNG
            p.save()
            p.translate(ox, oy)
            p.scale(z, z)
            style = self._style()
            draw_annotations(
                p, self._shot.annotations, style, preview_redact=True, selected_id=self._selected  # type: ignore[union-attr]
            )
            if self._preview is not None:
                draw_annotation(p, self._preview, style, preview_redact=True)
            p.restore()
            p.restore()
            self._paint_selection_locator(p, z)
            self._paint_hud(p)
        finally:
            p.end()

    def _paint_message(self, p: QPainter, text: str) -> None:
        p.setPen(self.palette().color(QPalette.ColorRole.PlaceholderText))
        p.drawText(self.rect(), int(Qt.AlignmentFlag.AlignCenter), text)

    def _scaled_pixmap(self, device_scale: float) -> QPixmap:
        img = self._shot.image  # type: ignore[union-attr]
        tw = max(1, int(round(img.width() * device_scale)))
        th = max(1, int(round(img.height() * device_scale)))
        key = (img.cacheKey(), tw, th)
        if self._scaled is not None and self._scaled[0] == key:
            return self._scaled[1]
        scaled = img.scaled(
            tw, th, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        pm = QPixmap.fromImage(scaled)
        pm.setDevicePixelRatio(1.0)
        self._scaled = (key, pm)
        return pm

    def _paint_image(self, p: QPainter, z: float, ox: float, oy: float, iw: int, ih: int, view: QRectF) -> None:
        img: QImage = self._shot.image  # type: ignore[union-attr]
        device_scale = z * self.device_pixel_ratio()
        if device_scale >= 0.999:
            # zoomed in / 1:1: blit only the visible source rectangle, unsmoothed (crisp pixels)
            sx0 = max(0, int(math.floor((0 - ox) / z)))
            sy0 = max(0, int(math.floor((0 - oy) / z)))
            sx1 = min(iw, int(math.ceil((view.width() - ox) / z)))
            sy1 = min(ih, int(math.ceil((view.height() - oy) / z)))
            if sx1 <= sx0 or sy1 <= sy0:
                return
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            tgt = QRectF(ox + sx0 * z, oy + sy0 * z, (sx1 - sx0) * z, (sy1 - sy0) * z)
            p.drawImage(tgt, img, QRectF(sx0, sy0, sx1 - sx0, sy1 - sy0))
        else:
            pm = self._scaled_pixmap(device_scale)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            p.drawPixmap(QRectF(ox, oy, iw * z, ih * z), pm, QRectF(pm.rect()))

    def _paint_selection_locator(self, p: QPainter, z: float) -> None:
        """When zoomed far out the selected annotation is tiny; outline it with a minimum-size
        marker in widget space so it can always be found."""
        ann = self._find(self._selected)
        if ann is None:
            return
        b = annotation_bounds(ann, self._style())
        if b.isEmpty():
            return
        cx, cy = self.image_to_widget(b.center().x(), b.center().y())
        w, h = b.width() * z, b.height() * z
        if w >= 18 and h >= 18:
            return
        w, h = max(w, 18.0), max(h, 18.0)
        r = QRectF(cx - w / 2, cy - h / 2, w, h)
        p.save()
        p.setBrush(Qt.BrushStyle.NoBrush)
        pen = QPen(QColor("#FFFFFF"), 3)
        p.setPen(pen)
        p.drawRect(r)
        pen = QPen(QColor("#2563EB"), 1.5)
        pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawRect(r)
        p.restore()

    def _paint_hud(self, p: QPainter) -> None:
        if not self._hud:
            return
        font = QFont(self.font())
        font.setBold(True)
        fm = QFontMetricsF(font)
        pad = 5.0
        tw, th = fm.horizontalAdvance(self._hud), fm.height()
        x = min(self._hud_pos.x() + 14, max(0.0, self.width() - tw - 2 * pad - 2))
        y = min(self._hud_pos.y() + 18, max(0.0, self.height() - th - 2 * pad - 2))
        box = QRectF(x, y, tw + 2 * pad, th + pad)
        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        bg = QColor("#111827")
        bg.setAlpha(225)
        p.setBrush(QBrush(bg))
        p.drawRoundedRect(box, 4, 4)
        p.setFont(font)
        p.setPen(QColor("#FFFFFF"))
        p.drawText(box, int(Qt.AlignmentFlag.AlignCenter), self._hud)
        p.restore()

    # ------------------------------------------------------------------
    # history helpers
    # ------------------------------------------------------------------
    def _find(self, ann_id: Optional[str]) -> Optional[Annotation]:
        if ann_id is None or self._shot is None:
            return None
        return self._shot.find_annotation(ann_id)

    def _emit_changed(self) -> None:
        self.annotations_changed.emit()
        self.history_changed.emit()
        self.update()

    def _record(self, cmd) -> None:
        """Remember an already-applied command."""
        self._undo.append(cmd)
        if len(self._undo) > HISTORY_LIMIT:
            del self._undo[0]
        self._redo.clear()

    def _do(self, cmd) -> None:
        cmd.redo(self._shot)
        self._record(cmd)
        self._emit_changed()

    def _drop_stale_selection(self) -> None:
        if self._selected is not None and self._find(self._selected) is None:
            self._selected = None
            self.selection_changed.emit()

    def undo(self) -> None:
        self.commit_pending_edit()
        if not self._undo or self._shot is None:
            return
        cmd = self._undo.pop()
        cmd.undo(self._shot)
        self._redo.append(cmd)
        self._drop_stale_selection()
        self._emit_changed()

    def redo(self) -> None:
        self.commit_pending_edit()
        if not self._redo or self._shot is None:
            return
        cmd = self._redo.pop()
        cmd.redo(self._shot)
        self._undo.append(cmd)
        self._drop_stale_selection()
        self._emit_changed()

    # ------------------------------------------------------------------
    # selection / deletion / notes
    # ------------------------------------------------------------------
    def select(self, ann_id: Optional[str]) -> None:
        if ann_id is not None and self._find(ann_id) is None:
            ann_id = None
        if ann_id == self._selected:
            return
        self._selected = ann_id
        self.update()
        self.selection_changed.emit()

    def delete_selected(self) -> None:
        if self._selected is not None:
            self.delete_annotation(self._selected)

    def delete_annotation(self, ann_id: str) -> None:
        if self._shot is None:
            return
        self.commit_pending_edit()
        ann = self._find(ann_id)
        if ann is None:
            return
        index = self._shot.annotations.index(ann)
        self._do(_RemoveCmd(ann, index))
        self._drop_stale_selection()

    def set_pin_note(self, ann_id: str, text: str) -> None:
        """Change a pin's note (undoable). No-op when unchanged."""
        pin = self._find(ann_id)
        if not isinstance(pin, PinAnn) or pin.note == text:
            return
        self._do(_ChangeCmd(pin, {"note": pin.note}, {"note": text}))

    def edit_pin_note(self, ann_id: str) -> None:
        """Open the small inline note editor next to the pin."""
        pin = self._find(ann_id)
        if not isinstance(pin, PinAnn):
            return
        if self._note_pin is not None and self._note_pin.id != pin.id:
            self.commit_pending_edit()
        self.select(pin.id)
        self._note_pin = pin
        self._note.setPlaceholderText(f"Note for pin {pin.n}  (Enter saves, Esc skips)")
        self._note.setText(pin.note)
        self._place_note()
        self._note.show()
        self._note.raise_()
        self._note.setFocus(Qt.FocusReason.OtherFocusReason)
        self._note.selectAll()

    def pending_edit_active(self) -> bool:
        return self._note_pin is not None

    def commit_pending_edit(self) -> None:
        """Commit and close the inline note editor if it is open (EditorWindow calls this
        before Next / Finish / hide / switching shots)."""
        if self._note_pin is not None:
            self._finish_note(commit=True)

    def _place_note(self) -> None:
        pin = self._note_pin
        if pin is None:
            return
        z, _, _ = self._layout()
        cx, cy = self.image_to_widget(pin.x + 0.5, pin.y + 0.5)
        r = self._style().pin_radius * z
        w = max(220, min(340, self.width() // 3))
        self._note.setFixedWidth(w)
        h = self._note.sizeHint().height()
        x = cx + r + 8
        if x + w > self.width() - 4:
            x = cx - r - 8 - w
        x = max(4.0, min(x, self.width() - w - 4.0))
        y = max(4.0, min(cy - h / 2.0, self.height() - h - 4.0))
        self._note.move(int(x), int(y))

    def _finish_note(self, *, commit: bool) -> None:
        pin = self._note_pin
        if pin is None:
            return
        self._note_pin = None  # guards against the editingFinished that follows hide()
        text = self._note.text().strip()
        had_focus = self._note.hasFocus()
        self._note.hide()
        if commit and text != pin.note and self._find(pin.id) is not None:
            if self._note_is_new and self._undo and getattr(self._undo[-1], "ann", None) is pin:
                pin.note = text  # fold the note into the "add pin" undo step
                self._emit_changed()
            else:
                self._do(_ChangeCmd(pin, {"note": pin.note}, {"note": text}))
        self._note_is_new = False
        if had_focus:
            self.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_note_finished(self) -> None:
        self._finish_note(commit=True)

    def _on_note_cancelled(self) -> None:
        self._finish_note(commit=False)

    # ------------------------------------------------------------------
    # mouse
    # ------------------------------------------------------------------
    def _tol(self) -> float:
        z, _, _ = self._layout()
        return PICK_TOLERANCE / max(z, 1e-6)

    def _pin_color(self, x: int, y: int) -> str:
        img = self._shot.image if self._shot else None  # ORIGINAL image, never a render
        return imageops.sample_hex(img, x, y) if img is not None else ""

    def mousePressEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        if self._shot is None or not self._has_image():
            return
        pos = e.position()
        if e.button() == Qt.MouseButton.MiddleButton:
            self._begin_pan(pos)
            return
        if e.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.commit_pending_edit()
        fx, fy = self.widget_to_image(pos.x(), pos.y())
        iw, ih = self._img_size()
        style = self._style()
        tol = self._tol()
        tool = self._tool
        if tool == Tool.SELECT:
            ann = shapes.pick(self._shot.annotations, fx, fy, tol, style)
            self.select(ann.id if ann else None)
            if ann is not None:
                self._drag = _Drag("move", (fx, fy), pos, ann, shapes.state_of(ann))
            return
        if tool == Tool.PIN:
            existing = shapes.pick_pin(self._shot.annotations, fx, fy, tol, style)
            if existing is not None:
                self._note_is_new = False
                self.edit_pin_note(existing.id)
                return
            if not (0 <= fx < iw and 0 <= fy < ih):
                return  # clicked the margin around the image
            x, y = shapes.snap_pixel(fx, iw), shapes.snap_pixel(fy, ih)
            pin = PinAnn(x=x, y=y, note="", color=self._pin_color(x, y))
            self._do(_AddCmd(pin, len(self._shot.annotations)))
            self.select(pin.id)
            self._note_is_new = True
            self.edit_pin_note(pin.id)
            return
        self._drag = _Drag("draw", (fx, fy), pos)

    def mouseDoubleClickEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        if self._shot is None or e.button() != Qt.MouseButton.LeftButton or not self._has_image():
            return
        fx, fy = self.widget_to_image(e.position().x(), e.position().y())
        pin = shapes.pick_pin(self._shot.annotations, fx, fy, self._tol(), self._style())
        if pin is not None:
            if not (self.pending_edit_active() and self._note_pin is pin):
                self._note_is_new = False
            self.edit_pin_note(pin.id)
            return
        self.mousePressEvent(e)  # a fast second click is a second press, not a double-click

    def mouseMoveEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        pos = e.position()
        d = self._drag
        if d is None:
            return
        if d.kind == "pan":
            dx = pos.x() - d.start_pos.x()
            dy = pos.y() - d.start_pos.y()
            self._hbar.setValue(d.pan_h - int(round(dx)))
            self._vbar.setValue(d.pan_v - int(round(dy)))
            return
        fx, fy = self.widget_to_image(pos.x(), pos.y())
        iw, ih = self._img_size()
        if d.kind == "draw":
            kind = _DRAW_KIND[self._tool]
            self._preview = shapes.build_annotation(kind, d.start_img, (fx, fy), iw, ih, min_len=0)
            self._hud = self._hud_text(self._preview)
            self._hud_pos = pos
            self.update()
        elif d.kind == "move" and d.ann is not None and d.before is not None:
            if not d.moved:
                if (pos - d.start_pos).manhattanLength() < 3:
                    return
                d.moved = True
            dx = int(round(fx - d.start_img[0]))
            dy = int(round(fy - d.start_img[1]))
            new = shapes.moved_state(d.ann, d.before, dx, dy, iw, ih)
            if isinstance(d.ann, PinAnn):
                new["color"] = self._pin_color(new["x"], new["y"])
            shapes.apply_state(d.ann, new)
            self.update()

    def mouseReleaseEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        d = self._drag
        if d is None:
            return
        if d.kind == "pan":
            if e.button() == Qt.MouseButton.MiddleButton:
                self._drag = None
                self._update_cursor()
            return
        if e.button() != Qt.MouseButton.LeftButton:
            return
        self._drag = None
        self._preview = None
        self._hud = None
        pos = e.position()
        fx, fy = self.widget_to_image(pos.x(), pos.y())
        iw, ih = self._img_size()
        if d.kind == "draw" and self._shot is not None:
            ann = shapes.build_annotation(_DRAW_KIND[self._tool], d.start_img, (fx, fy), iw, ih)
            if ann is not None:
                self._do(_AddCmd(ann, len(self._shot.annotations)))
                self.select(ann.id)
            self.update()
        elif d.kind == "move" and d.ann is not None and d.before is not None:
            after = shapes.state_of(d.ann)
            if d.moved and after != d.before:
                self._record(_ChangeCmd(d.ann, d.before, after))
                self._emit_changed()
            self.update()

    def _begin_pan(self, pos: QPointF) -> None:
        self._drag = _Drag("pan", start_pos=pos, pan_h=self._hbar.value(), pan_v=self._vbar.value())
        self._update_cursor()

    def _cancel_drag(self) -> None:
        d = self._drag
        if d is not None and d.kind == "move" and d.ann is not None and d.before is not None:
            shapes.apply_state(d.ann, d.before)
        self._drag = None
        self._preview = None
        self._hud = None
        self._update_cursor()
        self.update()

    def _hud_text(self, ann: Optional[Annotation]) -> Optional[str]:
        if ann is None or self._shot is None:
            return None
        if isinstance(ann, RulerAnn):
            return shapes.ruler_text(ann, self._shot.scale_factor)
        if isinstance(ann, (RectAnn, RedactAnn)):
            return f"{ann.w} × {ann.h} px"
        return None

    def wheelEvent(self, e: QWheelEvent) -> None:  # noqa: N802
        if not self._has_image():
            return
        delta = e.angleDelta()
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = delta.y() / 120.0
            if steps:
                self.set_zoom(self._effective_zoom() * (1.25 ** steps), e.position())
            e.accept()
            return
        if e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self._hbar.setValue(self._hbar.value() - (delta.y() or delta.x()))
        else:
            self._vbar.setValue(self._vbar.value() - delta.y())
            self._hbar.setValue(self._hbar.value() - delta.x())
        e.accept()

    # ------------------------------------------------------------------
    # tooltips
    # ------------------------------------------------------------------
    def tooltip_at(self, x: float, y: float) -> Optional[str]:
        """Tooltip text for the widget point (rulers: length plus logical px when the capture
        monitor is scaled; pins: number, position, colour, note)."""
        if self._shot is None or not self._has_image():
            return None
        fx, fy = self.widget_to_image(x, y)
        ann = shapes.pick(self._shot.annotations, fx, fy, self._tol(), self._style())
        if isinstance(ann, RulerAnn):
            return "Ruler: " + shapes.ruler_text(ann, self._shot.scale_factor)
        if isinstance(ann, PinAnn):
            text = f"Pin {ann.n} at ({ann.x}, {ann.y})"
            if ann.color:
                text += f", color {ann.color}"
            if ann.note:
                text += f": {ann.note}"
            return text
        return None

    def event(self, e: QEvent) -> bool:
        if e.type() == QEvent.Type.ToolTip:
            pos = e.pos()  # type: ignore[attr-defined]
            text = self.tooltip_at(pos.x(), pos.y())
            if text:
                QToolTip.showText(e.globalPos(), text, self)  # type: ignore[attr-defined]
            else:
                QToolTip.hideText()
                e.ignore()
            return True
        return super().event(e)

    # ------------------------------------------------------------------
    # keyboard (only while the canvas has focus)
    # ------------------------------------------------------------------
    def keyPressEvent(self, e: QKeyEvent) -> None:  # noqa: N802
        key = e.key()
        mods = e.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        alt = bool(mods & (Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier))
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        if ctrl and not alt:
            if key == Qt.Key.Key_Z:
                (self.redo if shift else self.undo)()
                e.accept()
                return
            if key == Qt.Key.Key_Y:
                self.redo()
                e.accept()
                return
            if key == Qt.Key.Key_0:
                self.zoom_fit()
                e.accept()
                return
            if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
                self.zoom_in()
                e.accept()
                return
            if key == Qt.Key.Key_Minus:
                self.zoom_out()
                e.accept()
                return
            super().keyPressEvent(e)
            return
        if alt:
            super().keyPressEvent(e)
            return
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_selected()
            e.accept()
            return
        if key == Qt.Key.Key_Escape:
            if self._drag is not None:
                self._cancel_drag()
            else:
                self.select(None)
            e.accept()
            return
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            step = 10 if shift else 1
            dx = {Qt.Key.Key_Left: -step, Qt.Key.Key_Right: step}.get(Qt.Key(key), 0)
            dy = {Qt.Key.Key_Up: -step, Qt.Key.Key_Down: step}.get(Qt.Key(key), 0)
            if self._selected is not None:
                self._nudge(dx, dy)
            else:
                self._hbar.setValue(self._hbar.value() + dx * 20)
                self._vbar.setValue(self._vbar.value() + dy * 20)
            e.accept()
            return
        if key == Qt.Key.Key_F:
            self.zoom_fit()
            e.accept()
            return
        if key == Qt.Key.Key_1:
            self.zoom_actual()
            e.accept()
            return
        if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom_in()
            e.accept()
            return
        if key == Qt.Key.Key_Minus:
            self.zoom_out()
            e.accept()
            return
        if 0x41 <= key <= 0x5A:
            letter = chr(key)
            for tool, k in TOOL_KEYS.items():
                if k == letter:
                    self.set_tool(tool)
                    e.accept()
                    return
        super().keyPressEvent(e)

    def _nudge(self, dx: int, dy: int) -> None:
        ann = self._find(self._selected)
        if ann is None or self._shot is None:
            return
        iw, ih = self._img_size()
        before = shapes.state_of(ann)
        new = shapes.moved_state(ann, before, dx, dy, iw, ih)
        if isinstance(ann, PinAnn):
            new["color"] = self._pin_color(new["x"], new["y"])
        if new == before:
            return
        shapes.apply_state(ann, new)
        self._record(_ChangeCmd(ann, before, new))
        self._emit_changed()
