"""PNG rendering of the two saved images per shot. Owner: output/app builder (package C).

Thin wrappers over the shared, already-implemented helpers in uireport.imageops and
uireport.annotdraw (do not re-implement drawing or redaction here)."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtGui import QImage

from .. import annotdraw, imageops
from ..models import Shot


def _require_image(shot: Shot) -> QImage:
    if shot.image is None or shot.image.isNull():
        raise ValueError(f"shot {shot.index or shot.id} has no image to save")
    return shot.image


def render_original(shot: Shot) -> QImage:
    """shot.image with every RedactAnn applied (imageops.apply_redactions), NO other marks.
    This is what 01.png contains. Never modifies shot.image. Size == shot.image size."""
    return imageops.apply_redactions(_require_image(shot), shot.redactions)


def render_annotated(shot: Shot) -> QImage:
    """Redactions applied + pins/rects/arrows/rulers painted
    (annotdraw.render_annotated(shot.image, shot.annotations, shot.scale_factor)).
    This is what 01_annotated.png contains. Same pixel size as the original, never upscaled."""
    return annotdraw.render_annotated(_require_image(shot), shot.annotations, shot.scale_factor)


def has_real_alpha(image: QImage) -> bool:
    """True when at least one pixel is not fully opaque. Captured screens are opaque even
    though we keep them in an ARGB32 buffer, so this is normally False."""
    if not image.hasAlphaChannel():
        return False
    fmt = image.format()
    if fmt not in (QImage.Format.Format_ARGB32, QImage.Format.Format_ARGB32_Premultiplied):
        return True  # exotic alpha format: keep it as is
    try:
        raw = image.constBits()
        data = memoryview(raw).cast("B")
        total = image.width() * image.height()
        if total == 0:
            return False
        # 32-bit formats have no row padding; alpha is the 4th byte of every little-endian pixel.
        return bytes(data[3 : total * 4 : 4]).count(255) != total
    except Exception:  # pragma: no cover - defensive: assume alpha, save as-is
        return True


def save_png(image: QImage, path: Path) -> None:
    """Lossless PNG (quality 100 / default compression). Saves as opaque RGB
    (Format_RGB32) unless the image really has alpha. Creates parent folders. Raises OSError
    if Qt fails to write."""
    if image is None or image.isNull():
        raise OSError(f"cannot save an empty image to {path}")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    out = image
    if not has_real_alpha(image):
        if image.format() != QImage.Format.Format_RGB32:
            out = image.convertToFormat(QImage.Format.Format_RGB32)
    tmp = target.with_name(target.name + ".tmp")
    try:
        if not out.save(str(tmp), "PNG"):
            raise OSError(f"Qt could not write the PNG file {target}")
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
