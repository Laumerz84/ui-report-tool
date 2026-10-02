"""Pixel-level helpers shared by the editor (pin colour sampling) and the output
package (redaction). Needs PySide6.QtGui but NOT a QApplication (QImage/QPainter on
images work headless).

All coordinates are image pixels (see CONTRACT.md section 4).
"""
from __future__ import annotations

from typing import Iterable

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter

from .geometry import IntRect
from .models import RedactAnn


def ensure_argb32(image: QImage) -> QImage:
    """Return `image` if it is already ARGB32, else a converted copy."""
    if image.format() == QImage.Format.Format_ARGB32:
        return image
    return image.convertToFormat(QImage.Format.Format_ARGB32)


def sample_hex(image: QImage, x: int, y: int) -> str:
    """Colour of the pixel at (x, y) as upper-case "#RRGGBB". Coordinates are clamped
    into the image. Sample from the ORIGINAL image, never from an annotated one."""
    if image is None or image.isNull():
        return ""
    xi = max(0, min(image.width() - 1, int(x)))
    yi = max(0, min(image.height() - 1, int(y)))
    c = image.pixelColor(xi, yi)
    return f"#{c.red():02X}{c.green():02X}{c.blue():02X}"


def auto_block_size(width: int, height: int) -> int:
    """Pixelation block edge for a redact box: a third of the short side, at least 8 px,
    so even large text becomes unreadable."""
    return max(8, int(round(min(width, height) / 3.0)))


def pixelate_region(image: QImage, rect: IntRect, block: int = 0, solid: bool = False) -> None:
    """Pixelate `rect` of `image` IN PLACE (image must be a non-const ARGB32 QImage)."""
    bounds = IntRect(0, 0, image.width(), image.height())
    r = rect.intersection(bounds)
    if r is None:
        return
    if solid:
        p = QPainter(image)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(r.x, r.y, r.w, r.h, Qt.GlobalColor.black)
        p.end()
        return
    b = block if block > 0 else auto_block_size(r.w, r.h)
    crop = image.copy(r.x, r.y, r.w, r.h)
    small_w = max(1, -(-r.w // b))  # ceil division
    small_h = max(1, -(-r.h // b))
    small = crop.scaled(
        small_w, small_h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
    )
    big = small.scaled(
        r.w, r.h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation
    )
    p = QPainter(image)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
    p.drawImage(r.x, r.y, big)
    p.end()


def apply_redactions(image: QImage, redactions: Iterable[RedactAnn]) -> QImage:
    """Return a NEW ARGB32 image with every redaction box pixelated. The input image is
    never modified, so the in-memory original stays intact for re-editing."""
    out = ensure_argb32(image).copy()
    for r in redactions:
        pixelate_region(out, r.irect, solid=(r.style == "solid"))
    return out
