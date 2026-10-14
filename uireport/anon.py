"""Anonymized screenshots from Ekxel (the user's spreadsheet app, G:/Sheets).

When a capture shows an Ekxel workbook whose data is protected (Ekxel's Protect Data "ghost"), Ekxel draws the same
screen area the way Claude may see it - names, account numbers and other personal data replaced by stand-ins - and
that picture becomes the shot's image (app/ekxel_link.py asks, app/controller.py swaps it in at capture time). The
real capture stays IN MEMORY ONLY (`Shot.real_image`), so the user can switch a shot back to it in the editor; it is
never written to disk, so report.md / report.json / the report folder never contain or name it.

Per-shot state (`Shot.anon_state`, runtime only):
    ""            Ekxel isn't involved (not running, nothing protected in the area): as before
    "pending"     the area shows protected data; Ekxel is drawing the anonymized picture
    "anonymized"  the shot's image IS Ekxel's picture; report.json carries extra["anonymized"]
    "real"        protected, but the user chose the real screenshot (asked again once at Finish)
    "failed"      protected, and Ekxel couldn't draw it: the real capture is kept (asked at Finish)
"""
from __future__ import annotations

from typing import Any, Optional

from . import imageops
from .models import Shot

ANON_NOTE = "Anonymized by Ekxel: stand-ins replace personal data"
FAILED_NOTE = "Not anonymized: Ekxel couldn't render it"
REAL_NOTE = "Real screenshot: Claude will see the real data in this screenshot"
PENDING_NOTE = "Anonymizing with Ekxel..."
CAPTION_NOTE = "Your caption and notes are sent as you type them."
EXTRA_KEY = "anonymized"     # report.json: {"by": "Ekxel", "note": ANON_NOTE, "token": <Ekxel's render id>}

PENDING, ANONYMIZED, REAL, FAILED = "pending", "anonymized", "real", "failed"


def is_pending(shot: Shot) -> bool:
    return shot.anon_state == PENDING


def needs_decision(shot: Shot) -> bool:
    """Protected data that would go out unanonymized: Finish asks first."""
    return shot.anon_state in (REAL, FAILED)


def is_anonymized(shot: Shot) -> bool:
    return shot.anon_state == ANONYMIZED and EXTRA_KEY in shot.extra


def mark_pending(shot: Shot) -> None:
    if shot.anon_state in ("", FAILED):
        shot.anon_state = PENDING


def mark_failed(shot: Shot) -> None:
    """Ekxel couldn't draw it: keep (or go back to) the real capture, but remember the area is protected."""
    if shot.real_image is not None:
        _set_image(shot, shot.real_image)
    shot.extra.pop(EXTRA_KEY, None)
    _restore_title(shot)
    shot.anon_state = FAILED


def apply_render(shot: Shot, image: Any, token: str = "", masked_title: Optional[str] = None) -> bool:
    """Ekxel's anonymized picture of the shot's area becomes the shot's image. False (nothing changed) when it doesn't
    have the capture's exact pixel size - then annotations wouldn't line up, so it isn't used."""
    if image is None or image.isNull() or image.width() != shot.image_width or image.height() != shot.image_height:
        return False
    if shot.real_image is None:
        shot.real_image = shot.image
    shot.anon_image = image
    shot.anon_info = {"token": token or "", "title": masked_title,
                      "real_title": shot.anon_info.get("real_title", shot.window.title if shot.window else None)}
    use_anonymized(shot)
    return True


def use_anonymized(shot: Shot) -> bool:
    if shot.anon_image is None:
        return False
    _set_image(shot, shot.anon_image)
    shot.extra[EXTRA_KEY] = {"by": "Ekxel", "note": ANON_NOTE, "token": shot.anon_info.get("token", "")}
    title = shot.anon_info.get("title")
    if shot.window is not None:
        # the window title can name the file (a client's name): it goes out masked too
        shot.window.title = title if isinstance(title, str) else ""
    shot.anon_state = ANONYMIZED
    return True


def use_real(shot: Shot) -> bool:
    """The user's explicit choice (the editor confirms first): Claude will see the real data in this shot."""
    if shot.real_image is None:
        return False
    _set_image(shot, shot.real_image)
    shot.extra.pop(EXTRA_KEY, None)
    _restore_title(shot)
    shot.anon_state = REAL
    return True


def _restore_title(shot: Shot) -> None:
    real = shot.anon_info.get("real_title")
    if shot.window is not None and real is not None:
        shot.window.title = real


def _set_image(shot: Shot, image: Any) -> None:
    shot.set_image(image)
    for pin in shot.pins:   # a pin records the colour under it in the ORIGINAL image: that image just changed
        pin.color = imageops.sample_hex(image, pin.x, pin.y)


def banner(shot: Optional[Shot]) -> tuple[str, str]:
    """(text, kind) for the editor's banner over this shot; kind '' = no banner."""
    if shot is None:
        return "", ""
    st = shot.anon_state
    if st == ANONYMIZED:
        return f"{ANON_NOTE}. {CAPTION_NOTE}", "anonymized"
    if st == PENDING:
        return PENDING_NOTE, "pending"
    if st == REAL:
        return f"{REAL_NOTE}.", "real"
    if st == FAILED:
        return f"{FAILED_NOTE}. You'll be asked before it is sent.", "failed"
    return "", ""
