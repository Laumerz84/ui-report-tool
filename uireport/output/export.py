"""Getting a capture out of the tool: clipboard, a file in the save folder, or a file to drag. Owner: package C.

Used by the extra-hotkey actions (copy / save / copy_save) and by the editor's Copy, Save as and Drag.
Callers pass the image they want: a fresh capture (`shot.image`) or the marked-up render
(`render.render_annotated(shot)`).
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from PySide6.QtGui import QGuiApplication, QImage

JPG_QUALITY = 92
DRAG_DIR_NAME = ".drag"
DRAG_KEEP_S = 24 * 3600
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def save_dir(settings: Any, output_root: "Path | str") -> Path:
    """The folder for saved screenshots: Settings > Saving, else <reports folder's parent>\\Screenshots."""
    folder = str(getattr(settings, "save_folder", "") or "").strip()
    return Path(folder) if folder else Path(output_root).parent / "Screenshots"


def file_stem(pattern: str, when: Optional[datetime] = None, n: int = 1) -> str:
    """'Screenshot {date} {time}' -> 'Screenshot 2026-10-14 153207'. Illegal file-name characters become '-'."""
    when = when or datetime.now()
    text = (pattern or "Screenshot {date} {time}").replace("{date}", when.strftime("%Y-%m-%d")) \
        .replace("{time}", when.strftime("%H%M%S")).replace("{n}", str(n))
    return _BAD_CHARS.sub("-", text).strip(" .") or "Screenshot"


def unique_path(folder: Path, stem: str, ext: str) -> Path:
    """folder/stem.ext, or 'stem (2).ext', 'stem (3).ext'... so nothing is ever overwritten."""
    path = folder / f"{stem}.{ext}"
    k = 2
    while path.exists():
        path = folder / f"{stem} ({k}).{ext}"
        k += 1
    return path


def write_image(image: QImage, path: Path) -> Path:
    """PNG or JPG by the path's extension. Raises OSError when it can't be written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    jpg = path.suffix.lower() in (".jpg", ".jpeg")
    out = image.convertToFormat(QImage.Format.Format_RGB32) if jpg else image
    if not out.save(str(path), "JPG" if jpg else "PNG", JPG_QUALITY if jpg else -1):
        raise OSError(f"could not write {path}")
    return path


def save_image(image: QImage, settings: Any, output_root: "Path | str") -> Path:
    """Save into the save folder with the configured name and format; returns the file written."""
    folder = save_dir(settings, output_root)
    ext = getattr(settings, "save_format", "png") or "png"
    return write_image(image, unique_path(folder, file_stem(getattr(settings, "save_name", "")), ext))


def copy_image(image: QImage) -> None:
    """Put the image on the clipboard (paste into Discord, Word, Paint, email...)."""
    app = QGuiApplication.instance()
    if app is None or QGuiApplication.clipboard() is None:
        raise RuntimeError("the clipboard is not available")
    QGuiApplication.clipboard().setImage(image)


def drag_file(image: QImage, output_root: "Path | str", stem: str) -> Path:
    """A PNG to drag into another app, in <reports folder>\\.drag (files older than a day are removed)."""
    folder = Path(output_root) / DRAG_DIR_NAME
    folder.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - DRAG_KEEP_S
    for old in folder.glob("*.png"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    path = folder / f"{_BAD_CHARS.sub('-', stem) or 'Screenshot'}.png"
    try:
        if path.exists():
            os.remove(path)
    except OSError:
        path = unique_path(folder, path.stem, "png")
    return write_image(image, path)
