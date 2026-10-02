"""Hotkey string <-> (modifiers, virtual-key) conversion. Pure Python, no Win32 calls.

Canonical text form: modifiers in the order Ctrl, Alt, Shift, Win, then the key,
joined by "+", e.g. "Ctrl+Alt+S", "Ctrl+Shift+F12".

Owner: lead (shared). The capture package's HotkeyManager and the settings window
both use this module, so parsing/validation rules live in exactly one place.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Logical names of the two global hotkeys (HotkeyManager.activated emits one of these).
NAME_CAPTURE = "capture"
NAME_DELAYED = "delayed"

# Win32 RegisterHotKey modifier flags
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

_MOD_ORDER = [("Ctrl", MOD_CONTROL), ("Alt", MOD_ALT), ("Shift", MOD_SHIFT), ("Win", MOD_WIN)]
_MOD_ALIASES = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "ctl": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "windows": MOD_WIN,
    "meta": MOD_WIN,
    "super": MOD_WIN,
}


def _build_key_table() -> dict[str, int]:
    table: dict[str, int] = {}
    for c in range(ord("A"), ord("Z") + 1):
        table[chr(c)] = c
    for c in range(ord("0"), ord("9") + 1):
        table[chr(c)] = c
    for n in range(1, 25):
        table[f"F{n}"] = 0x70 + n - 1
    table.update(
        {
            "Space": 0x20,
            "Enter": 0x0D,
            "Tab": 0x09,
            "Esc": 0x1B,
            "Insert": 0x2D,
            "Delete": 0x2E,
            "Home": 0x24,
            "End": 0x23,
            "PageUp": 0x21,
            "PageDown": 0x22,
            "Left": 0x25,
            "Up": 0x26,
            "Right": 0x27,
            "Down": 0x28,
            "PrintScreen": 0x2C,
            "Pause": 0x13,
        }
    )
    return table


KEY_TO_VK: dict[str, int] = _build_key_table()
VK_TO_KEY: dict[int, str] = {v: k for k, v in KEY_TO_VK.items()}
_KEY_ALIASES = {
    "return": "Enter",
    "enter": "Enter",
    "escape": "Esc",
    "esc": "Esc",
    "space": "Space",
    "spacebar": "Space",
    "tab": "Tab",
    "ins": "Insert",
    "insert": "Insert",
    "del": "Delete",
    "delete": "Delete",
    "home": "Home",
    "end": "End",
    "pgup": "PageUp",
    "pageup": "PageUp",
    "pgdn": "PageDown",
    "pagedown": "PageDown",
    "left": "Left",
    "up": "Up",
    "right": "Right",
    "down": "Down",
    "prtsc": "PrintScreen",
    "printscreen": "PrintScreen",
    "print": "PrintScreen",
    "pause": "Pause",
}


@dataclass(frozen=True)
class Hotkey:
    modifiers: int  # OR of MOD_* flags (without MOD_NOREPEAT)
    vk: int  # Windows virtual-key code
    text: str  # canonical text, e.g. "Ctrl+Alt+S"


def _canonical_key(token: str) -> Optional[str]:
    t = token.strip()
    if not t:
        return None
    if len(t) == 1 and t.upper() in KEY_TO_VK:
        return t.upper()
    up = t.upper()
    if up in KEY_TO_VK:  # F1..F24
        return up
    return _KEY_ALIASES.get(t.lower())


def format_hotkey(modifiers: int, vk: int) -> str:
    key = VK_TO_KEY.get(vk)
    if key is None:
        raise ValueError(f"unsupported virtual key 0x{vk:02X}")
    parts = [name for name, flag in _MOD_ORDER if modifiers & flag]
    parts.append(key)
    return "+".join(parts)


def parse_hotkey(text: str) -> Hotkey:
    """Parse "ctrl + alt + s" style text. Raises ValueError with a readable message."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Hotkey is empty")
    modifiers = 0
    key: Optional[str] = None
    for raw in text.split("+"):
        token = raw.strip()
        if not token:
            # tolerate a literal "+" key being unsupported; an empty token is an error
            raise ValueError(f"Malformed hotkey: {text!r}")
        mod = _MOD_ALIASES.get(token.lower())
        if mod is not None:
            modifiers |= mod
            continue
        canon = _canonical_key(token)
        if canon is None:
            raise ValueError(f"Unsupported key {token!r} in hotkey {text!r}")
        if key is not None:
            raise ValueError(f"Hotkey {text!r} has more than one non-modifier key")
        key = canon
    if key is None:
        raise ValueError(f"Hotkey {text!r} has no key (only modifiers)")
    vk = KEY_TO_VK[key]
    return Hotkey(modifiers, vk, format_hotkey(modifiers, vk))


def normalize_hotkey(text: str) -> str:
    """Canonical text form; raises ValueError if unparsable."""
    return parse_hotkey(text).text


# Combinations Windows or nearly every app already owns; refuse to register them.
_RESERVED_RAW = (
    *(
        "Win+Shift+S",  # Snipping Tool (explicitly taken per the spec)
        "Ctrl+Alt+Delete",
        "Ctrl+Shift+Esc",
        "Ctrl+Esc",
        "Alt+Esc",
        "Alt+Tab",
        "Alt+F4",
        "Alt+Space",
        "Win+L",
        "Win+D",
        "Win+E",
        "Win+R",
        "Win+Tab",
        "Win+V",
        "Win+PrintScreen",
        "Ctrl+C",
        "Ctrl+V",
        "Ctrl+X",
        "Ctrl+Z",
        "Ctrl+S",
        "Ctrl+A",
    ),
)
RESERVED_HOTKEYS: frozenset[str] = frozenset(parse_hotkey(t).text for t in _RESERVED_RAW)


def validate_hotkey(text: str) -> Optional[str]:
    """Return None if `text` is an acceptable global hotkey, else a readable error."""
    try:
        hk = parse_hotkey(text)
    except ValueError as exc:
        return str(exc)
    if not (hk.modifiers & (MOD_CONTROL | MOD_ALT | MOD_WIN)):
        return "A global hotkey needs at least one of Ctrl, Alt or Win (Shift alone is not enough)."
    if hk.text in RESERVED_HOTKEYS:
        return f"{hk.text} is reserved by Windows or common apps; pick another combination."
    return None


def altgr_warning(text: str) -> Optional[str]:
    """Soft warning (not an error): Ctrl+Alt+<letter/digit> is what AltGr produces on
    many keyboard layouts (Polish, German, Czech, ...), so registering it globally can
    swallow typed characters such as 's'-with-accent. Returns a message or None."""
    try:
        hk = parse_hotkey(text)
    except ValueError:
        return None
    only_ctrl_alt = (hk.modifiers & (MOD_CONTROL | MOD_ALT)) == (MOD_CONTROL | MOD_ALT)
    no_extra = not (hk.modifiers & (MOD_SHIFT | MOD_WIN))
    is_char = (0x41 <= hk.vk <= 0x5A) or (0x30 <= hk.vk <= 0x39)
    if only_ctrl_alt and no_extra and is_char:
        return (
            f"{hk.text} equals AltGr+{VK_TO_KEY[hk.vk]}, which types a special character on "
            "some keyboard layouts (e.g. Polish, German). If you type with such a layout, "
            "choose a combination with Shift or a function key."
        )
    return None
