"""The ready-to-paste prompt. Owner: output/app builder (package C)."""
from __future__ import annotations

import os
import re
from pathlib import Path, PureWindowsPath

GOAL_MAX_CHARS = 300

# user-editable text copied on Finish (Settings -> "Text copied on Finish"); empty = build_prompt()
PLACEHOLDERS = {
    "shots": '"3 screenshots" / "1 screenshot"',
    "count": "the number of screenshots",
    "goal": "the session goal (one line, may be empty)",
    "report": "full path of report.md",
    "folder": "full path of the report folder",
}
_PLACEHOLDER_RE = re.compile(r"\{(" + "|".join(PLACEHOLDERS) + r")\}")


def _one_line_goal(goal: str) -> str:
    text = " ".join(str(goal or "").split())
    if len(text) > GOAL_MAX_CHARS:
        text = text[: GOAL_MAX_CHARS - 3].rstrip() + "..."
    return text


def build_prompt(shot_count: int, goal: str, report_md_path: Path) -> str:
    """Short, single-paragraph prompt (details stay in report.md):

    'I captured N screenshots. Goal: <goal>. Read <absolute path>\\report.md and
    look at every image it lists (annotated and original), then help me with the goal.'

    N == 1 -> 'I captured 1 screenshot.'. Empty goal -> drop the 'Goal: ...' sentence and end
    with 'then help me with what they show.' Deliberately not about UI (screenshots can be of
    anything); the user can replace it entirely in Settings. Goal is collapsed to
    one line and trimmed to ~300 chars. The path is absolute, backslash form."""
    p = Path(report_md_path)
    if not p.is_absolute():
        p = Path(os.path.abspath(str(p)))
    path_text = str(PureWindowsPath(str(p)))
    n = int(shot_count)
    noun = "screenshot" if n == 1 else "screenshots"
    head = f"I captured {n} {noun}."
    goal_text = _one_line_goal(goal)
    read = f"Read {path_text} and look at every image it lists (annotated and original)"
    if goal_text:
        end = "" if goal_text[-1] in ".!?" else "."
        return f"{head} Goal: {goal_text}{end} {read}, then help me with the goal."
    return f"{head} {read}, then help me with what they show."


def render_copy_text(template: str, shot_count: int, goal: str, report_md_path: Path) -> str:
    """The text copied on Finish. A blank `template` gives the built-in Claude prompt
    (build_prompt); otherwise each known {placeholder} (see PLACEHOLDERS) is replaced and
    everything else - unknown {names}, stray braces - is kept exactly as typed."""
    if not str(template or "").strip():
        return build_prompt(shot_count, goal, report_md_path)
    p = Path(report_md_path)
    if not p.is_absolute():
        p = Path(os.path.abspath(str(p)))
    n = int(shot_count)
    values = {
        "shots": f"{n} {'screenshot' if n == 1 else 'screenshots'}",
        "count": str(n),
        "goal": _one_line_goal(goal),
        "report": str(PureWindowsPath(str(p))),
        "folder": str(PureWindowsPath(str(p.parent))),
    }
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], str(template))


def copy_prompt_to_clipboard(text: str) -> None:
    """QGuiApplication.clipboard().setText(text) (GUI thread). Text only, no network."""
    from PySide6.QtGui import QGuiApplication

    if QGuiApplication.instance() is None:
        raise RuntimeError("no QGuiApplication: cannot use the clipboard")
    clipboard = QGuiApplication.clipboard()
    if clipboard is None:
        raise RuntimeError("the clipboard is not available")
    clipboard.setText(text)
