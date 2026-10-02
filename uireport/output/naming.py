"""Session folder and file naming. Owner: output/app builder (package C)."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..models import Session

FALLBACK_SLUG = "session"

# Letters NFKD cannot decompose into "base + accent": transliterate them by hand so that
# e.g. Polish "Zażółć" does not lose its 'l'.
_TRANSLIT = {
    "ł": "l", "Ł": "l", "đ": "d", "Đ": "d", "ð": "d", "Ð": "d", "ø": "o", "Ø": "o",
    "ß": "ss", "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe", "þ": "th", "Þ": "th",
    "ı": "i", "ħ": "h", "Ħ": "h",
}


def _ascii_slug(text: str) -> str:
    """Lower-case ASCII slug WITHOUT the 'session' fallback (empty string if nothing is left)."""
    if not text:
        return ""
    t = "".join(_TRANSLIT.get(ch, ch) for ch in str(text))
    t = unicodedata.normalize("NFKD", t)
    t = t.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def _cut_slug(slug: str, max_len: int) -> str:
    """Shorten to max_len, on a word boundary when that keeps at least half of the budget."""
    max_len = max(1, int(max_len))
    if len(slug) <= max_len:
        return slug
    cut = slug[:max_len]
    if slug[max_len] != "-":  # we split inside a word: fall back to the last whole word
        boundary = cut.rfind("-")
        if boundary >= max_len // 2 and boundary > 0:
            cut = cut[:boundary]
    return cut.strip("-")


def slugify(text: str, max_len: int = 40) -> str:
    """Lower-case ASCII slug: accents stripped (unicodedata NFKD), non [a-z0-9] runs ->
    single '-', trimmed of '-', cut to max_len on a word boundary when possible.
    Returns 'session' for empty/unsluggable input."""
    slug = _cut_slug(_ascii_slug(text), max_len)
    return slug or FALLBACK_SLUG


def session_folder_name(session: Session, now: Optional[datetime] = None) -> str:
    """'2026-09-29_1432_<slug>' - local `now` (default datetime.now()) formatted
    '%Y-%m-%d_%H%M', slug from session.goal (fallback: first non-empty shot caption,
    then 'session')."""
    when = now or datetime.now()
    candidates = [session.goal] + [s.caption for s in session.shots]
    slug = ""
    for cand in candidates:
        slug = _cut_slug(_ascii_slug(cand or ""), 40)
        if slug:
            break
    return f"{when.strftime('%Y-%m-%d_%H%M')}_{slug or FALLBACK_SLUG}"


def unique_folder(root: Path, name: str) -> Path:
    """root/name, or root/name-2, root/name-3 ... if it already exists. Does not create it."""
    root = Path(root)
    candidate = root / name
    n = 2
    while candidate.exists():
        candidate = root / f"{name}-{n}"
        n += 1
    return candidate


def shot_filenames(index: int, total: int) -> tuple[str, str]:
    """('01.png', '01_annotated.png') for index=1. Zero-padded to max(2, len(str(total)))
    digits (so 100+ shots become '001.png')."""
    width = max(2, len(str(max(int(total), 0))))
    stem = f"{int(index):0{width}d}"
    return f"{stem}.png", f"{stem}_annotated.png"
