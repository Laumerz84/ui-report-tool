"""Programmatically drawn icons (no image files shipped). Owner: output/app builder (C)."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPen, QPixmap

ICON_SIZES = (16, 24, 32, 48, 64, 128)

_BG = "#2F3A6B"  # deep indigo tile
_BG_EDGE = "#1B2347"
_FRAME = "#FFFFFF"
_PIN = "#E5484D"  # same red as the annotation pins


def draw_app_icon(painter: QPainter, size: int) -> None:
    """Paint the icon into a `size` x `size` square: an indigo rounded tile, white viewfinder
    corner brackets (screen-capture look) and a red numbered-pin dot in the middle."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.scale(size / 100.0, size / 100.0)

    # tile
    painter.setPen(QPen(QColor(_BG_EDGE), 2.0))
    painter.setBrush(QBrush(QColor(_BG)))
    painter.drawRoundedRect(QRectF(3, 3, 94, 94), 20, 20)

    # viewfinder corners
    pen = QPen(QColor(_FRAME), 7.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    lo, hi, arm = 22.0, 78.0, 17.0
    for cx, cy, sx, sy in ((lo, lo, 1, 1), (hi, lo, -1, 1), (lo, hi, 1, -1), (hi, hi, -1, -1)):
        painter.drawLine(QPointF(cx, cy), QPointF(cx + sx * arm, cy))
        painter.drawLine(QPointF(cx, cy), QPointF(cx, cy + sy * arm))

    # pin dot with a white ring
    painter.setPen(QPen(QColor(_FRAME), 4.0))
    painter.setBrush(QBrush(QColor(_PIN)))
    painter.drawEllipse(QPointF(50, 50), 13, 13)
    painter.restore()


def make_app_icon(size: int = 64) -> QIcon:
    """A simple camera-viewfinder style icon painted with QPainter at several sizes
    (16/24/32/48/64/128) so the tray and window title look crisp at any DPI. Needs a
    QGuiApplication."""
    icon = QIcon()
    sizes = sorted({*ICON_SIZES, max(8, int(size))})
    for s in sizes:
        pm = QPixmap(s, s)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        try:
            draw_app_icon(p, s)
        finally:
            p.end()
        icon.addPixmap(pm)
    return icon
