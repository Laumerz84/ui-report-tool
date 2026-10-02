"""Editable text copied on Finish: an empty template keeps the built-in Claude prompt (package C)."""
from __future__ import annotations

from pathlib import Path

from uireport.output.clipboard import PLACEHOLDERS, build_prompt, render_copy_text
from uireport.settings import Settings

REPORT = Path(r"G:\UIReports\2026-09-30_1514_login\report.md")


def test_empty_template_gives_exactly_the_built_in_prompt():
    for template in ("", "   ", "\n"):
        assert render_copy_text(template, 3, "Fix the button", REPORT) == build_prompt(3, "Fix the button", REPORT)
        assert render_copy_text(template, 1, "", REPORT) == build_prompt(1, "", REPORT)


def test_custom_template_fills_every_placeholder():
    t = "Hi! {shots} ({count}) about: {goal}\nReport: {report}\nFolder: {folder}"
    out = render_copy_text(t, 3, "the invoice\n  totals   look off", REPORT)
    assert out == (
        "Hi! 3 screenshots (3) about: the invoice totals look off\n"
        r"Report: G:\UIReports\2026-09-30_1514_login\report.md" "\n"
        r"Folder: G:\UIReports\2026-09-30_1514_login"
    )


def test_one_shot_is_singular_and_an_empty_goal_is_blank():
    assert render_copy_text("{shots}|{goal}|", 1, "", REPORT) == "1 screenshot||"


def test_unknown_placeholders_and_stray_braces_are_left_alone():
    t = "{shots} {oops} {} {{count}} } {"
    assert render_copy_text(t, 2, "", REPORT) == "2 screenshots {oops} {} {2} } {"


def test_every_documented_placeholder_is_replaced():
    out = render_copy_text(" ".join("{" + p + "}" for p in PLACEHOLDERS), 2, "g", REPORT)
    assert "{" not in out and "}" not in out


def test_the_template_is_saved_with_the_settings_and_bad_values_fall_back():
    s = Settings(copy_text_template="Look at {report}")
    assert Settings.from_dict(s.to_dict()).copy_text_template == "Look at {report}"
    assert Settings.from_dict({"copy_text_template": 42}).copy_text_template == ""
    assert Settings().copy_text_template == ""
