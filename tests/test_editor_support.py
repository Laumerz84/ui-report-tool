"""Helpers shared by the editor tests (owner: editor builder). Contains no tests itself.

Mouse / key events are sent with QTest to OUR OWN offscreen widgets only; nothing here can
reach the real desktop. NOTE: QTest.mouseMove() only delivers a move event while a button is held
(after mousePress); with no button pressed it calls QCursor.setPos(), which would move the REAL
cursor on a non-offscreen platform. So moves are only ever issued inside drag() (press ... release).
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

LEFT = Qt.MouseButton.LeftButton
NOMOD = Qt.KeyboardModifier.NoModifier


def pt(x: float, y: float) -> QPoint:
    return QPoint(int(round(x)), int(round(y)))


def click(widget, x: float, y: float, mods=NOMOD) -> None:
    QTest.mouseClick(widget, LEFT, mods, pt(x, y))


def double_click(widget, x: float, y: float) -> None:
    QTest.mouseDClick(widget, LEFT, NOMOD, pt(x, y))


def drag(widget, p1, p2, mods=NOMOD, via=()) -> None:
    """press at p1, move through `via` points then to p2, release at p2 (widget coordinates)."""
    QTest.mousePress(widget, LEFT, mods, pt(*p1))
    for v in via:
        QTest.mouseMove(widget, pt(*v))
    QTest.mouseMove(widget, pt(*p2))
    QTest.mouseRelease(widget, LEFT, mods, pt(*p2))


def pixel_center(canvas, ix: int, iy: int) -> tuple[float, float]:
    """Widget point at the centre of image pixel (ix, iy)."""
    return canvas.image_to_widget(ix + 0.5, iy + 0.5)


def edge_point(canvas, ex: int, ey: int) -> tuple[float, float]:
    """Widget point on the pixel-grid edge (ex, ey)."""
    return canvas.image_to_widget(float(ex), float(ey))


class Recorder:
    """Counts emissions of a Qt signal and remembers the arguments."""

    def __init__(self, signal) -> None:
        self.calls: list[tuple] = []
        signal.connect(lambda *a: self.calls.append(a))

    @property
    def count(self) -> int:
        return len(self.calls)

    def clear(self) -> None:
        self.calls.clear()


def make_session(make_shot, n: int, w: int = 200, h: int = 120, **session_kw):
    from uireport.models import Session

    sess = Session(**session_kw)
    for i in range(n):
        shot = make_shot(w, h, caption=f"caption {i + 1}")
        sess.add_shot(shot)
    return sess
