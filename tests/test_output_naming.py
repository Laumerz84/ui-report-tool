"""naming.py and clipboard.build_prompt / copy_prompt_to_clipboard (package C, output half)."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

from uireport.models import Session, Shot
from uireport.output.clipboard import build_prompt, copy_prompt_to_clipboard
from uireport.output.naming import session_folder_name, shot_filenames, slugify, unique_folder

NOW = datetime(2026, 9, 29, 14, 32, 5)


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text, expected",
    [
        ("Fix the clipped Save button", "fix-the-clipped-save-button"),
        ("  Hello,   World!!  ", "hello-world"),
        ("Zażółć gęślą jaźń", "zazolc-gesla-jazn"),
        ("Crème brûlée à la façon", "creme-brulee-a-la-facon"),
        ("Straße & Größe", "strasse-grosse"),
        ("UPPER_case-and_under", "upper-case-and-under"),
        ("v2.0: 150% scale", "v2-0-150-scale"),
        ("", "session"),
        ("   ", "session"),
        ("!!!???", "session"),
        ("日本語のテスト", "session"),
        ("---a---", "a"),
    ],
)
def test_slugify_cases(text, expected):
    assert slugify(text) == expected


def test_slugify_is_ascii_lowercase_and_bounded():
    s = slugify("Ünïcödé ÄÖÜ " * 30)
    assert len(s) <= 40
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", s)


def test_slugify_long_text_cuts_on_a_word_boundary():
    s = slugify("the quick brown fox jumps over the lazy dog again and again and again")
    assert len(s) <= 40
    assert not s.endswith("-")
    # every produced word is a whole word of the input
    words = set("the quick brown fox jumps over the lazy dog again and again and again".split())
    assert all(w in words for w in s.split("-"))


def test_slugify_single_huge_word_is_hard_cut():
    s = slugify("x" * 200)
    assert s == "x" * 40


def test_slugify_custom_max_len():
    assert slugify("alpha beta gamma delta", max_len=12) == "alpha-beta"
    assert len(slugify("abcdefghijklmnop", max_len=5)) == 5


def test_slugify_word_boundary_exactly_at_limit():
    text = "a" * 39 + " bbb"
    assert slugify(text) == "a" * 39


# ---------------------------------------------------------------------------
# folder names
# ---------------------------------------------------------------------------
def test_folder_name_from_goal():
    s = Session(goal="Fix the clipped Save button")
    assert session_folder_name(s, NOW) == "2026-09-29_1432_fix-the-clipped-save-button"


def test_folder_name_falls_back_to_first_caption_then_session():
    s = Session(goal="")
    s.shots = [Shot(caption="   "), Shot(caption="Sidebar overlaps content")]
    assert session_folder_name(s, NOW) == "2026-09-29_1432_sidebar-overlaps-content"
    assert session_folder_name(Session(), NOW) == "2026-09-29_1432_session"


def test_folder_name_unsluggable_goal_uses_caption():
    s = Session(goal="日本語")
    s.shots = [Shot(caption="Real caption")]
    assert session_folder_name(s, NOW) == "2026-09-29_1432_real-caption"


def test_folder_name_defaults_to_now():
    name = session_folder_name(Session(goal="x"))
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{4}_x", name)


def test_unique_folder_collisions(test_out):
    root = test_out
    assert unique_folder(root, "run") == root / "run"
    (root / "run").mkdir()
    assert unique_folder(root, "run") == root / "run-2"
    (root / "run-2").mkdir()
    (root / "run-3").mkdir()
    assert unique_folder(root, "run") == root / "run-4"
    assert not (root / "run-4").exists()  # never creates it


def test_shot_filenames_padding():
    assert shot_filenames(1, 3) == ("01.png", "01_annotated.png")
    assert shot_filenames(12, 12) == ("12.png", "12_annotated.png")
    assert shot_filenames(1, 99) == ("01.png", "01_annotated.png")
    assert shot_filenames(1, 100) == ("001.png", "001_annotated.png")
    assert shot_filenames(100, 100) == ("100.png", "100_annotated.png")
    assert shot_filenames(7, 1234) == ("0007.png", "0007_annotated.png")


def test_100_plus_shots_sort_lexicographically():
    names = [shot_filenames(i, 120)[0] for i in range(1, 121)]
    assert names == sorted(names)
    assert len(set(names)) == 120


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------
REPORT = Path(r"C:\Users\me\UIReports\2026-09-29_1432_fix-clipped-save\report.md")


def test_prompt_matches_the_contract_example():
    assert build_prompt(3, "Fix the clipped Save button", REPORT) == (
        "I captured 3 screenshots. Goal: Fix the clipped Save button. "
        r"Read C:\Users\me\UIReports\2026-09-29_1432_fix-clipped-save\report.md and look at every image "
        "it lists (annotated and original), then help me with the goal."
    )


def test_prompt_singular_and_goal_punctuation():
    p = build_prompt(1, "Fix it.", REPORT)
    assert p.startswith("I captured 1 screenshot. Goal: Fix it. Read ")
    assert ".." not in p
    assert "screenshots" not in p
    assert build_prompt(2, "Why?", REPORT).startswith("I captured 2 screenshots. Goal: Why? Read ")


def test_prompt_goal_is_collapsed_to_one_line_and_trimmed():
    p = build_prompt(2, "  Line one\nline   two\r\n\tline three  ", REPORT)
    assert "Goal: Line one line two line three. Read" in p
    assert "\n" not in p and "\r" not in p
    long_goal = "word " * 200
    p2 = build_prompt(2, long_goal, REPORT)
    goal_part = p2.split("Goal: ", 1)[1].split(" Read ", 1)[0]
    assert len(goal_part) <= 300
    assert goal_part.endswith("...")


def test_prompt_without_goal():
    p = build_prompt(4, "   ", REPORT)
    assert p == (
        "I captured 4 screenshots. "
        r"Read C:\Users\me\UIReports\2026-09-29_1432_fix-clipped-save\report.md and look at every image "
        "it lists (annotated and original), then help me with what they show."
    )
    assert "Goal" not in p


def test_prompt_is_not_about_any_particular_subject():
    for goal in ("", "Compare these invoices"):
        p = build_prompt(2, goal, REPORT)
        assert "UI" not in p.replace("UIReports", "")  # only the folder name may say UI
        assert "restyle" not in p and "issue" not in p


def test_prompt_path_is_absolute_backslash_form(test_out):
    p = build_prompt(1, "g", test_out / "x" / "report.md")
    path = p.split("Read ", 1)[1].split(" and look", 1)[0]
    assert Path(path).is_absolute()
    assert "/" not in path
    assert path.endswith("\\report.md")


def test_copy_prompt_to_clipboard_round_trip(qapp):
    from PySide6.QtGui import QGuiApplication

    text = "I captured 1 screenshot of a UI issue. Goal: x. \u2014 test"
    copy_prompt_to_clipboard(text)
    assert QGuiApplication.clipboard().text() == text
