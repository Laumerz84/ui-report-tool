import pytest

from uireport.hotkeyspec import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    MOD_WIN,
    altgr_warning,
    format_hotkey,
    normalize_hotkey,
    parse_hotkey,
    validate_hotkey,
)


def test_parse_defaults():
    hk = parse_hotkey("Ctrl+Alt+S")
    assert hk.modifiers == MOD_CONTROL | MOD_ALT
    assert hk.vk == ord("S")
    assert hk.text == "Ctrl+Alt+S"
    assert parse_hotkey("Ctrl+Alt+D").vk == ord("D")


def test_parse_is_forgiving_about_case_spacing_order_and_aliases():
    assert normalize_hotkey(" alt + CONTROL + s ") == "Ctrl+Alt+S"
    assert normalize_hotkey("windows+shift+f12") == "Shift+Win+F12"
    assert normalize_hotkey("ctrl+return") == "Ctrl+Enter"
    assert parse_hotkey("Ctrl+F1").vk == 0x70
    assert parse_hotkey("Ctrl+F24").vk == 0x87


@pytest.mark.parametrize("bad", ["", "   ", "Ctrl+Alt", "Ctrl+A+B", "Ctrl++", "Ctrl+Banana", "Ctrl+Alt+"])
def test_parse_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_hotkey(bad)


def test_format_round_trip():
    for text in ("Ctrl+Alt+S", "Ctrl+Shift+F9", "Alt+Win+PageDown", "Ctrl+Alt+Shift+Win+Z"):
        hk = parse_hotkey(text)
        assert format_hotkey(hk.modifiers, hk.vk) == hk.text == text
    assert MOD_SHIFT and MOD_WIN  # exported flags exist


def test_validate_requires_modifier_and_blocks_reserved():
    assert validate_hotkey("Ctrl+Alt+S") is None
    assert validate_hotkey("Ctrl+Shift+F9") is None
    assert validate_hotkey("F9") is not None
    assert validate_hotkey("Shift+S") is not None  # Shift alone is not enough
    assert "reserved" in validate_hotkey("Win+Shift+S")
    assert validate_hotkey("alt+f4") is not None
    assert validate_hotkey("nonsense") is not None


def test_altgr_warning_only_for_ctrl_alt_character_keys():
    assert altgr_warning("Ctrl+Alt+S") is not None
    assert altgr_warning("Ctrl+Alt+D") is not None
    assert altgr_warning("Ctrl+Alt+Shift+S") is None
    assert altgr_warning("Ctrl+Alt+F9") is None
    assert altgr_warning("Ctrl+S") is None
    assert altgr_warning("garbage") is None
