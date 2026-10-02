"""Session panel: thumbnails of every shot. Owner: editor builder (package B).

A horizontal filmstrip (QListWidget in list mode, left-to-right, no wrapping, horizontal
scrolling). Items are lightweight (id only); a delegate paints each card and asks the panel for
a cached thumbnail, so only the cards that are actually visible cost anything: 100+ shots stay
responsive.

Thumbnails are the ANNOTATED render: the scaled base image plus the annotations drawn through a
painter transform (annotdraw.draw_annotations with preview_redact=True, so redacted areas are
covered, never revealed). The scaled base and the finished pixmap are cached per shot; the
pixmap is invalidated automatically when the annotations change (signature) and explicitly via
`invalidate()`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QEvent, QMimeData, QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QDrag,
    QFont,
    QFontMetrics,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from ..annotdraw import AnnotStyle, draw_annotations
from ..models import CaptureMode, Session, Shot

ID_ROLE = Qt.ItemDataRole.UserRole
MIME_TYPE = "application/x-uireport-shot"


@dataclass(frozen=True)
class _Metrics:
    """Card geometry in logical px, scaled with the UI font so 150% / large fonts look right."""

    card_w: int
    card_h: int
    pad: int
    thumb_w: int
    thumb_h: int
    x_size: int
    pill_h: int

    @classmethod
    def for_font(cls, fm: QFontMetrics) -> "_Metrics":
        k = max(0.8, fm.height() / 15.0)
        pad = max(4, int(round(5 * k)))
        card_w = int(round(150 * k))
        thumb_w = card_w - 2 * pad
        thumb_h = int(round(72 * k))
        pill_h = fm.height() + 2
        card_h = pad + thumb_h + 4 + pill_h + 2 + fm.height() + pad + 2
        return cls(card_w, card_h, pad, thumb_w, thumb_h, max(14, int(round(16 * k))), pill_h)

    def thumb_rect(self, card: QRectF) -> QRectF:
        return QRectF(card.left() + self.pad, card.top() + self.pad, self.thumb_w, self.thumb_h)

    def x_rect(self, card: QRectF) -> QRectF:
        t = self.thumb_rect(card)
        return QRectF(t.right() - self.x_size - 3, t.top() + 3, self.x_size, self.x_size)


class _ThumbDelegate(QStyledItemDelegate):
    def __init__(self, panel: "SessionPanel") -> None:
        super().__init__(panel)
        self._panel = panel

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:  # noqa: N802
        m = self._panel.metrics()
        return QSize(m.card_w + 6, m.card_h + 6)

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index) -> None:
        panel = self._panel
        shot = panel.shot_by_id(index.data(ID_ROLE))
        if shot is None:
            return
        m = panel.metrics()
        pal = option.palette
        fm = option.fontMetrics
        current = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = QRectF(option.rect).adjusted(3, 3, -3, -3)
        fill = pal.color(QPalette.ColorRole.AlternateBase if hover and not current else QPalette.ColorRole.Base)
        border = pal.color(QPalette.ColorRole.Highlight if current else QPalette.ColorRole.Mid)
        p.setPen(QPen(border, 2.0 if current else 1.0))
        p.setBrush(QBrush(fill))
        p.drawRoundedRect(card, 6, 6)

        # thumbnail area
        thumb = m.thumb_rect(card)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(pal.color(QPalette.ColorRole.Window).darker(112)))
        p.drawRoundedRect(thumb, 3, 3)
        pm = panel.thumbnail(shot, QSize(int(thumb.width()), int(thumb.height())))
        if pm is not None:
            dpr = pm.devicePixelRatio() or 1.0
            w, h = pm.width() / dpr, pm.height() / dpr
            p.drawPixmap(QPointF(thumb.center().x() - w / 2, thumb.center().y() - h / 2), pm)
        else:
            p.setPen(pal.color(QPalette.ColorRole.PlaceholderText))
            p.drawText(thumb, int(Qt.AlignmentFlag.AlignCenter), "no image")

        # role colour stripe along the bottom of the thumbnail
        stripe = QRectF(thumb.left(), thumb.bottom() - 4, thumb.width(), 4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(shot.role.color)))
        p.drawRect(stripe)

        # index badge (top-left)
        d = fm.height() + 2
        badge = QRectF(thumb.left() + 3, thumb.top() + 3, max(d, fm.horizontalAdvance(str(shot.index)) + 8), d)
        bg = QColor(17, 24, 39, 225)
        p.setBrush(QBrush(bg))
        p.drawRoundedRect(badge, d / 2, d / 2)
        f = QFont(option.font)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#FFFFFF"))
        p.drawText(badge, int(Qt.AlignmentFlag.AlignCenter), str(shot.index))

        # delete "x" (top-right) on hover / for the current card
        if hover or current:
            xr = m.x_rect(card)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(17, 24, 39, 225)))
            p.drawEllipse(xr)
            p.setPen(QPen(QColor("#FFFFFF"), 1.6))
            q = xr.width() * 0.3
            c = xr.center()
            p.drawLine(QPointF(c.x() - q, c.y() - q), QPointF(c.x() + q, c.y() + q))
            p.drawLine(QPointF(c.x() - q, c.y() + q), QPointF(c.x() + q, c.y() - q))

        # role pill + capture mode
        y = thumb.bottom() + 4
        label = shot.role.label
        p.setFont(f)
        pw = fm.horizontalAdvance(label) + 14
        pill = QRectF(card.left() + m.pad, y, pw, m.pill_h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(shot.role.color)))
        p.drawRoundedRect(pill, m.pill_h / 2, m.pill_h / 2)
        p.setPen(QColor("#FFFFFF"))
        p.drawText(pill, int(Qt.AlignmentFlag.AlignCenter), label)
        if shot.capture_mode == CaptureMode.DELAYED:
            p.setFont(option.font)
            p.setPen(pal.color(QPalette.ColorRole.PlaceholderText))
            p.drawText(
                QRectF(pill.right() + 6, y, card.right() - pill.right() - m.pad - 6, m.pill_h),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                "delayed",
            )

        # caption snippet
        p.setFont(option.font)
        text_rect = QRectF(card.left() + m.pad, y + m.pill_h + 2, card.width() - 2 * m.pad, fm.height())
        caption = " ".join(shot.caption.split())
        if caption:
            p.setPen(pal.color(QPalette.ColorRole.Text))
            p.drawText(text_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                       fm.elidedText(caption, Qt.TextElideMode.ElideRight, int(text_rect.width())))
        else:
            p.setPen(pal.color(QPalette.ColorRole.PlaceholderText))
            p.drawText(text_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), "(no caption)")
        p.restore()


class _FilmList(QListWidget):
    """Horizontal, wrap-less, scrollable card list with its own drag-and-drop reordering (the
    stock InternalMove drops 'on' an item at the END of the list; this computes a proper
    insertion point from the cursor and draws a drop marker)."""

    delete_clicked = Signal(str)  # the small x on a card
    repressed = Signal(str)  # click / Enter on the already-current card
    dropped = Signal(int, int)  # source row, insertion row (before the move)
    delete_key = Signal(str)
    move_key = Signal(int)  # Ctrl+Left / Ctrl+Right: -1 / +1

    def __init__(self, panel: "SessionPanel") -> None:
        super().__init__(panel)
        self._panel = panel
        self._drag_row = -1
        self._drop_x: Optional[float] = None
        self.setViewMode(QListView.ViewMode.ListMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(False)
        self.setMovement(QListView.Movement.Static)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setUniformItemSizes(True)
        self.setSpacing(2)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setMouseTracking(True)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDropIndicatorShown(False)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # ---- mouse ---------------------------------------------------------
    def _card_rect(self, item: QListWidgetItem) -> QRectF:
        return QRectF(self.visualItemRect(item)).adjusted(3, 3, -3, -3)

    def item_id(self, item: Optional[QListWidgetItem]) -> Optional[str]:
        return item.data(ID_ROLE) if item is not None else None

    def mousePressEvent(self, e: QMouseEvent) -> None:  # noqa: N802
        pos = e.position().toPoint()
        item = self.itemAt(pos)
        if item is not None and e.button() == Qt.MouseButton.LeftButton:
            if self._panel.metrics().x_rect(self._card_rect(item)).contains(QPointF(pos)):
                self.delete_clicked.emit(self.item_id(item))
                e.accept()
                return
        prev = self.currentItem()
        super().mousePressEvent(e)
        if item is not None and item is prev and e.button() == Qt.MouseButton.LeftButton:
            self.repressed.emit(self.item_id(item))

    def keyPressEvent(self, e: QKeyEvent) -> None:  # noqa: N802
        cur = self.item_id(self.currentItem())
        ctrl = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if e.key() == Qt.Key.Key_Delete and cur:
            self.delete_key.emit(cur)
            e.accept()
            return
        if ctrl and e.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right) and cur:
            self.move_key.emit(-1 if e.key() == Qt.Key.Key_Left else 1)
            e.accept()
            return
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            e.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        ):
            if cur:
                self.repressed.emit(cur)
                e.accept()
                return
        super().keyPressEvent(e)

    # ---- drag & drop ---------------------------------------------------
    def startDrag(self, supportedActions) -> None:  # noqa: N802
        item = self.currentItem()
        if item is None:
            return
        self._drag_row = self.row(item)
        rect = self.visualItemRect(item)
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(MIME_TYPE, str(self.item_id(item)).encode("ascii"))
        drag.setMimeData(mime)
        pm = self.viewport().grab(rect)
        drag.setPixmap(pm)
        drag.setHotSpot(self.viewport().mapFromGlobal(QCursor.pos()) - rect.topLeft())
        try:
            drag.exec(Qt.DropAction.MoveAction)  # the outcome is applied by dropEvent; ignore the result
        finally:
            self._drag_row = -1
            self._drop_x = None
            self.viewport().update()

    def insertion_row(self, pos: QPoint) -> int:
        """Row before which a card dropped at `pos` would be inserted (0..count)."""
        n = self.count()
        for r in range(n):
            if pos.x() < self.visualItemRect(self.item(r)).center().x():
                return r
        return n

    def _marker_x(self, ins: int) -> Optional[float]:
        n = self.count()
        if n == 0:
            return None
        if ins >= n:
            return float(self.visualItemRect(self.item(n - 1)).right())
        return float(self.visualItemRect(self.item(ins)).left())

    def dragEnterEvent(self, e) -> None:  # noqa: N802
        if e.mimeData().hasFormat(MIME_TYPE):
            e.setDropAction(Qt.DropAction.MoveAction)
            e.accept()
        else:
            e.ignore()

    def dragMoveEvent(self, e) -> None:  # noqa: N802
        if not e.mimeData().hasFormat(MIME_TYPE):
            e.ignore()
            return
        pos = e.position().toPoint()
        self._drop_x = self._marker_x(self.insertion_row(pos))
        # auto-scroll near the edges
        bar = self.horizontalScrollBar()
        margin = 28
        if pos.x() < margin:
            bar.setValue(bar.value() - 24)
        elif pos.x() > self.viewport().width() - margin:
            bar.setValue(bar.value() + 24)
        self.viewport().update()
        e.setDropAction(Qt.DropAction.MoveAction)
        e.accept()

    def dragLeaveEvent(self, e) -> None:  # noqa: N802
        self._drop_x = None
        self.viewport().update()
        e.accept()

    def dropEvent(self, e) -> None:  # noqa: N802
        src = self._drag_row
        self._drop_x = None
        self.viewport().update()
        if src < 0 or not e.mimeData().hasFormat(MIME_TYPE):
            e.ignore()
            return
        ins = self.insertion_row(e.position().toPoint())
        e.setDropAction(Qt.DropAction.MoveAction)
        e.accept()
        self.dropped.emit(src, ins)

    def paintEvent(self, e) -> None:  # noqa: N802
        super().paintEvent(e)
        if self._drop_x is not None:
            p = QPainter(self.viewport())
            p.setPen(QPen(self.palette().color(QPalette.ColorRole.Highlight), 3))
            x = int(self._drop_x)
            p.drawLine(x, 4, x, self.viewport().height() - 4)
            p.end()


class SessionPanel(QWidget):
    """A view of `session.shots` (thumbnail of the ANNOTATED render, index, role badge,
    caption snippet). It NEVER mutates the session itself; it only emits requests and the
    owner (EditorWindow) applies them with Session.reorder / remove_shot.

      * click a thumbnail       -> shot_selected(shot_id)
      * drag a thumbnail to a new position -> order_changed(list of ALL shot ids in the
        new order)
      * Delete key / context menu 'Delete' / small x button -> shot_delete_requested(id)
    Must stay usable with 10, 25 and 100+ shots (scrolling, cached thumbnails).

    Extra (optional) API: `invalidate`, `update_shot`, `current_id`, `shot_ids`, `move_shot`,
    `move_current`, `request_delete`, `build_context_menu`, `thumbnail`, `metrics`.
    Keyboard on the strip: arrows change the open shot, Delete deletes it, Ctrl+Left/Right
    moves it, Enter re-opens it.
    """

    shot_selected = Signal(str)
    shot_delete_requested = Signal(str)
    order_changed = Signal(list)

    def __init__(self, session: Session, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._session = session
        self._quiet = False
        self._by_id: dict[str, Shot] = {}
        self._base_cache: dict[str, tuple[tuple, QImage]] = {}
        self._thumb_cache: dict[str, tuple[tuple, QPixmap]] = {}
        self._list = _FilmList(self)
        self._delegate = _ThumbDelegate(self)
        self._list.setItemDelegate(self._delegate)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._list)
        self._apply_height()

        self._list.currentItemChanged.connect(self._on_current_changed)
        self._list.repressed.connect(self._emit_selected)
        self._list.delete_clicked.connect(self.request_delete)
        self._list.delete_key.connect(self.request_delete)
        self._list.move_key.connect(self.move_current)
        self._list.dropped.connect(self._on_dropped)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        self.refresh()

    # ------------------------------------------------------------------
    # metrics / height
    # ------------------------------------------------------------------
    def metrics(self) -> _Metrics:
        return _Metrics.for_font(self.fontMetrics())

    def _apply_height(self) -> None:
        m = self.metrics()
        bar = self._list.horizontalScrollBar().sizeHint().height()
        self.setFixedHeight(m.card_h + 6 + bar + 2 * self._list.frameWidth() + 6)

    def changeEvent(self, e) -> None:  # noqa: N802
        super().changeEvent(e)
        if e.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._apply_height()
            self._list.doItemsLayout()

    # ------------------------------------------------------------------
    # session binding
    # ------------------------------------------------------------------
    def set_session(self, session: Session) -> None:
        self._session = session
        self._base_cache.clear()
        self._thumb_cache.clear()
        self.refresh()

    def shot_by_id(self, shot_id: Optional[str]) -> Optional[Shot]:
        return self._by_id.get(shot_id) if shot_id else None

    def shot_ids(self) -> list[str]:
        """Ids in the order the strip currently shows them."""
        return [self._list.item_id(self._list.item(r)) for r in range(self._list.count())]

    def current_id(self) -> Optional[str]:
        return self._list.item_id(self._list.currentItem())

    def refresh(self) -> None:
        """Rebuild from session.shots (keeps the current selection when the shot still exists)."""
        keep = self.current_id()
        shots = list(self._session.shots)
        self._by_id = {s.id: s for s in shots}
        lst = self._list
        was_quiet = self._quiet
        self._quiet = True
        lst.setUpdatesEnabled(False)
        try:
            lst.clear()
            m = self.metrics()
            size = QSize(m.card_w + 6, m.card_h + 6)
            for s in shots:
                it = QListWidgetItem()
                it.setData(ID_ROLE, s.id)
                it.setSizeHint(size)
                it.setToolTip(self._tooltip(s))
                lst.addItem(it)
            if keep is not None:
                for r in range(lst.count()):
                    if lst.item_id(lst.item(r)) == keep:
                        lst.setCurrentRow(r)
                        break
        finally:
            lst.setUpdatesEnabled(True)
            self._quiet = was_quiet
        # forget caches of shots that are gone
        for cache in (self._base_cache, self._thumb_cache):
            for sid in [k for k in cache if k not in self._by_id]:
                del cache[sid]
        lst.viewport().update()

    def _tooltip(self, s: Shot) -> str:
        cap = " ".join(s.caption.split()) or "(no caption)"
        size = f"{s.image_width}×{s.image_height} px" if s.image_width else "no image"
        return f"{s.index}. {s.role.label}: {cap}\n{size}"

    def update_shot(self, shot_id: str) -> None:
        """Cheap repaint of one card after its caption / role changed (no rebuild)."""
        shot = self._by_id.get(shot_id)
        for r in range(self._list.count()):
            it = self._list.item(r)
            if self._list.item_id(it) == shot_id:
                if shot is not None:
                    it.setToolTip(self._tooltip(shot))
                self._list.viewport().update(self._list.visualItemRect(it))
                return

    def set_current(self, shot_id: Optional[str]) -> None:
        """Highlight the shot being edited (does not emit shot_selected)."""
        was = self._quiet
        self._quiet = True
        try:
            if shot_id is None:
                self._list.setCurrentRow(-1)
                self._list.clearSelection()
                return
            for r in range(self._list.count()):
                it = self._list.item(r)
                if self._list.item_id(it) == shot_id:
                    self._list.setCurrentItem(it)
                    self._list.scrollToItem(it, QAbstractItemView.ScrollHint.EnsureVisible)
                    break
        finally:
            self._quiet = was

    # ------------------------------------------------------------------
    # thumbnails
    # ------------------------------------------------------------------
    @staticmethod
    def _signature(shot: Shot) -> tuple:
        return tuple(tuple(a.to_dict().items()) for a in shot.annotations)

    def invalidate(self, shot_id: Optional[str] = None) -> None:
        """Drop cached thumbnails (all when `shot_id` is None) and repaint."""
        if shot_id is None:
            self._thumb_cache.clear()
            self._base_cache.clear()
            self._list.viewport().update()
        else:
            self._thumb_cache.pop(shot_id, None)
            self.update_shot(shot_id)

    def thumbnail(self, shot: Shot, box: QSize) -> Optional[QPixmap]:
        """Annotated thumbnail of `shot` fitted into `box` (logical px), or None without an image."""
        img = shot.image
        if img is None or img.isNull():
            return None
        dpr = float(self.devicePixelRatioF() or 1.0)
        iw, ih = img.width(), img.height()
        scale = min(box.width() * dpr / iw, box.height() * dpr / ih, 1.0)
        tw, th = max(1, int(round(iw * scale))), max(1, int(round(ih * scale)))
        base_key = (img.cacheKey(), tw, th)
        entry = self._base_cache.get(shot.id)
        if entry is None or entry[0] != base_key:
            base = img.scaled(
                tw, th, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
            ).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
            entry = (base_key, base)
            self._base_cache[shot.id] = entry
            self._thumb_cache.pop(shot.id, None)
        key = (base_key, self._signature(shot))
        cached = self._thumb_cache.get(shot.id)
        if cached is not None and cached[0] == key:
            return cached[1]
        canvas = entry[1].copy()
        if shot.annotations:
            p = QPainter(canvas)
            try:
                p.scale(tw / iw, th / ih)  # annotations are in IMAGE pixels
                draw_annotations(
                    p, shot.annotations, AnnotStyle.for_scale(shot.scale_factor), preview_redact=True
                )
            finally:
                p.end()
        pm = QPixmap.fromImage(canvas)
        pm.setDevicePixelRatio(dpr)
        self._thumb_cache[shot.id] = (key, pm)
        return pm

    # ------------------------------------------------------------------
    # user actions -> signals (the panel never mutates the session)
    # ------------------------------------------------------------------
    def _on_current_changed(self, cur: Optional[QListWidgetItem], _prev) -> None:
        if self._quiet or cur is None:
            return
        self._emit_selected(self._list.item_id(cur))

    def _emit_selected(self, shot_id: Optional[str]) -> None:
        if shot_id:
            self.shot_selected.emit(shot_id)

    def request_delete(self, shot_id: str) -> None:
        if shot_id in self._by_id:
            self.shot_delete_requested.emit(shot_id)

    def move_shot(self, shot_id: str, new_index: int) -> bool:
        """Move a card to 0-based position `new_index` (clamped) and emit order_changed with
        the full new id list. Returns False for an unknown id or when nothing moved."""
        ids = self.shot_ids()
        if shot_id not in ids:
            return False
        src = ids.index(shot_id)
        dst = max(0, min(len(ids) - 1, int(new_index)))
        if dst == src:
            return False
        self._move_row(src, dst)
        self.order_changed.emit(self.shot_ids())
        return True

    def move_current(self, delta: int) -> None:
        cur = self.current_id()
        if cur is None:
            return
        self.move_shot(cur, self.shot_ids().index(cur) + int(delta))

    def _move_row(self, src: int, dst: int) -> None:
        lst = self._list
        was = self._quiet
        self._quiet = True
        try:
            item = lst.takeItem(src)
            lst.insertItem(dst, item)
            lst.setCurrentItem(item)
            lst.scrollToItem(item, QAbstractItemView.ScrollHint.EnsureVisible)
        finally:
            self._quiet = was

    def _on_dropped(self, src: int, ins: int) -> None:
        dst = ins - 1 if ins > src else ins
        if dst == src or not (0 <= src < self._list.count()):
            return
        self._move_row(src, dst)
        ids = self.shot_ids()
        # emitted from the event loop, not from inside the drop handler
        QTimer.singleShot(0, lambda: self.order_changed.emit(ids))

    def build_context_menu(self, shot_id: str) -> QMenu:
        menu = QMenu(self)
        ids = self.shot_ids()
        pos = ids.index(shot_id) if shot_id in ids else -1
        act_open = menu.addAction("Edit this shot")
        act_open.triggered.connect(lambda: self._emit_selected(shot_id))
        act_left = menu.addAction("Move left")
        act_left.setEnabled(pos > 0)
        act_left.triggered.connect(lambda: self.move_shot(shot_id, pos - 1))
        act_right = menu.addAction("Move right")
        act_right.setEnabled(0 <= pos < len(ids) - 1)
        act_right.triggered.connect(lambda: self.move_shot(shot_id, pos + 1))
        menu.addSeparator()
        act_del = menu.addAction("Delete")
        act_del.triggered.connect(lambda: self.request_delete(shot_id))
        return menu

    def _on_context_menu(self, pos: QPoint) -> None:
        item = self._list.itemAt(pos)
        if item is None:
            return
        menu = self.build_context_menu(self._list.item_id(item))
        menu.exec(self._list.viewport().mapToGlobal(pos))
