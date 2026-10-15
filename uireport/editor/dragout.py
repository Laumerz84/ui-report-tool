"""Dragging a shot out of the editor into another app (Discord, Word, Paint, a folder...). Owner: editor.

The file itself comes from the controller through a provider callback (shot id -> PNG path with the markup),
so the editor never imports the output package. The drag carries the file URL and the image data, so both
file targets (Explorer, chat apps) and image targets (Word, Paint) accept it.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl
from PySide6.QtGui import QDrag, QImage, QMouseEvent, QPixmap
from PySide6.QtWidgets import QApplication, QToolButton, QWidget

DragProvider = Callable[[str], Optional[str]]


def add_file(mime: QMimeData, path: str) -> None:
    """Put the file (as a URL) and its pixels on a drag's mime data."""
    mime.setUrls([QUrl.fromLocalFile(path)])
    image = QImage(path)
    if not image.isNull():
        mime.setImageData(image)


def drag_file(source: QWidget, path: str) -> None:
    mime = QMimeData()
    add_file(mime, path)
    drag = QDrag(source)
    drag.setMimeData(mime)
    thumb = QImage(path)
    if not thumb.isNull():
        drag.setPixmap(QPixmap.fromImage(thumb.scaled(160, 120, Qt.AspectRatioMode.KeepAspectRatio,
                                                      Qt.TransformationMode.SmoothTransformation)))
    drag.exec(Qt.DropAction.CopyAction)


class DragHandle(QToolButton):
    """'Drag' button: press and drag it onto another app to drop the open shot there."""

    def __init__(self, current_id: Callable[[], Optional[str]], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setText("✥ Drag")
        self.setToolTip("Drag this onto another app or folder to drop the screenshot (with your markup) there")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.provider: Optional[DragProvider] = None
        self._current_id = current_id
        self._press: Optional[QPoint] = None

    def mousePressEvent(self, ev: QMouseEvent) -> None:  # noqa: N802
        self._press = ev.position().toPoint() if ev.button() == Qt.MouseButton.LeftButton else None
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev: QMouseEvent) -> None:  # noqa: N802
        if self._press is None or (ev.position().toPoint() - self._press).manhattanLength() < QApplication.startDragDistance():
            return super().mouseMoveEvent(ev)
        self._press = None
        self.setDown(False)
        sid = self._current_id()
        path = self.provider(sid) if (self.provider and sid) else None
        if path:
            drag_file(self, path)
