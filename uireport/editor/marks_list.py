"""List of the open shot's annotations (pins with editable notes, measurements, boxes).
Owner: editor builder (package B).

It never edits the model: it emits requests and EditorWindow routes them through the canvas so
they are undoable.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QKeyEvent, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTreeWidget, QTreeWidgetItem, QWidget

from ..models import Annotation, PinAnn, Shot
from . import shapes

ID_ROLE = Qt.ItemDataRole.UserRole
COL_MARK, COL_AT, COL_NOTE = 0, 1, 2


def _swatch(color: str) -> QIcon:
    pm = QPixmap(12, 12)
    pm.fill(QColor(color) if color else QColor(0, 0, 0, 0))
    return QIcon(pm)


class MarksList(QTreeWidget):
    """Pins (with an editable note column), rulers (length, plus logical px on scaled
    monitors), rectangles, arrows and redactions of the open shot."""

    annotation_selected = Signal(object)  # annotation id or None
    note_edited = Signal(str, str)  # pin id, new note
    delete_requested = Signal(str)  # annotation id

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._shot: Optional[Shot] = None
        self._quiet = False
        self.setColumnCount(3)
        self.setHeaderLabels(["Mark", "At (image px)", "Note / value"])
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setIconSize(QSize(12, 12))
        self.setMinimumWidth(220)
        h = self.header()
        h.setStretchLastSection(True)
        h.setSectionResizeMode(COL_MARK, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(COL_AT, QHeaderView.ResizeMode.ResizeToContents)
        self.itemChanged.connect(self._on_item_changed)
        self.itemDoubleClicked.connect(self._on_double_click)
        self.itemSelectionChanged.connect(self._on_selection)

    # ------------------------------------------------------------------
    def sizeHint(self) -> QSize:
        return QSize(300, 200)

    def set_shot(self, shot: Optional[Shot]) -> None:
        self._shot = shot
        self.refresh()

    def selected_annotation_id(self) -> Optional[str]:
        items = self.selectedItems()
        return items[0].data(COL_MARK, ID_ROLE) if items else None

    def refresh(self) -> None:
        keep = self.selected_annotation_id()
        self._quiet = True
        try:
            self.clear()
            shot = self._shot
            if shot is None:
                return
            order = self._ordered(shot.annotations)
            for ann in order:
                self.addTopLevelItem(self._make_item(ann, shot))
            if keep is not None:
                self._select_quiet(keep)
        finally:
            self._quiet = False

    @staticmethod
    def _ordered(anns: list[Annotation]) -> list[Annotation]:
        rank = {"pin": 0, "ruler": 1, "rect": 2, "arrow": 3, "redact": 4}
        return sorted(anns, key=lambda a: (rank.get(a.type.value, 9), anns.index(a)))

    def _make_item(self, ann: Annotation, shot: Shot) -> QTreeWidgetItem:
        kind, at, detail = shapes.describe(ann, shot.scale_factor)
        if isinstance(ann, PinAnn):
            at = f"({ann.x}, {ann.y})"
        item = QTreeWidgetItem([kind, at, detail])
        item.setData(COL_MARK, ID_ROLE, ann.id)
        if isinstance(ann, PinAnn):
            item.setIcon(COL_AT, _swatch(ann.color))
            item.setToolTip(COL_AT, f"Colour of the original pixel under the pin: {ann.color or 'unknown'}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        item.setToolTip(COL_NOTE, detail)
        return item

    def _find_item(self, ann_id: str) -> Optional[QTreeWidgetItem]:
        for i in range(self.topLevelItemCount()):
            it = self.topLevelItem(i)
            if it.data(COL_MARK, ID_ROLE) == ann_id:
                return it
        return None

    def _select_quiet(self, ann_id: Optional[str]) -> None:
        was = self._quiet
        self._quiet = True
        try:
            self.clearSelection()
            if ann_id is not None:
                it = self._find_item(ann_id)
                if it is not None:
                    it.setSelected(True)
                    self.setCurrentItem(it)
        finally:
            self._quiet = was

    def select(self, ann_id: Optional[str]) -> None:
        """Highlight an annotation without emitting annotation_selected."""
        self._select_quiet(ann_id)

    def commit_editor(self) -> None:
        """Close an open note cell editor, committing what was typed."""
        if self.state() == QAbstractItemView.State.EditingState:
            self.setFocus(Qt.FocusReason.OtherFocusReason)

    # ------------------------------------------------------------------
    def _on_selection(self) -> None:
        if not self._quiet:
            self.annotation_selected.emit(self.selected_annotation_id())

    def _on_double_click(self, item: QTreeWidgetItem, column: int) -> None:
        ann_id = item.data(COL_MARK, ID_ROLE)
        if self._shot is None:
            return
        ann = self._shot.find_annotation(ann_id)
        if isinstance(ann, PinAnn):
            self.editItem(item, COL_NOTE)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._quiet or column != COL_NOTE or self._shot is None:
            return
        ann_id = item.data(COL_MARK, ID_ROLE)
        if isinstance(self._shot.find_annotation(ann_id), PinAnn):
            self.note_edited.emit(ann_id, item.text(COL_NOTE).strip())

    def paintEvent(self, e) -> None:  # noqa: N802
        super().paintEvent(e)
        if self.topLevelItemCount() == 0 and self._shot is not None:
            p = QPainter(self.viewport())
            p.setPen(self.palette().color(QPalette.ColorRole.PlaceholderText))
            p.drawText(
                self.viewport().rect().adjusted(8, 28, -8, -8),
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop) | int(Qt.TextFlag.TextWordWrap),
                "Pins, measurements and boxes appear here. Double-click a pin to edit its note.",
            )
            p.end()

    def keyPressEvent(self, e: QKeyEvent) -> None:  # noqa: N802
        if e.key() in (Qt.Key.Key_Delete,) and self.state() != QAbstractItemView.State.EditingState:
            ann_id = self.selected_annotation_id()
            if ann_id:
                self.delete_requested.emit(ann_id)
                e.accept()
                return
        if e.key() == Qt.Key.Key_F2:
            it = self.currentItem()
            if it is not None:
                self._on_double_click(it, COL_NOTE)
                e.accept()
                return
        super().keyPressEvent(e)
