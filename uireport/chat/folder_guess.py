"""Which project folder a screenshot is about. Owner: chat.

Candidates: the Codebase Dashboard's recent folders (a sibling `codebase-dashboard\\recent.json`, if
present) plus the folders of recent Claude Code sessions. The captured window points at one of them
through its exe path, the paths on its command line (e.g. `G:\\Sheets\\launch.pyw` for Ekxel) or a
folder name in its title. No match: the last folder used.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Optional

DASHBOARD_RECENT = Path(__file__).resolve().parents[3] / "codebase-dashboard" / "recent.json"
_SKIP = ("scratch-workspaces", "\\temp\\", "\\appdata\\", "\\.aiwork")


def known_folders(session_cwds: Iterable[str] = ()) -> list[str]:
    raw: list[str] = []
    try:
        data = json.loads(DASHBOARD_RECENT.read_text(encoding="utf-8"))
        raw += [str(x) for x in data if isinstance(x, str)]
    except (OSError, ValueError):
        pass
    raw += [c for c in session_cwds if c]
    home = os.path.normpath(str(Path.home())).lower()
    claude_dir = os.path.join(home, ".claude")
    out, seen = [], set()
    for folder in raw:
        norm = os.path.normpath(folder)
        key = norm.lower()
        too_broad = (home == key or home.startswith(key.rstrip("\\") + "\\") or len(key) <= 3
                     or key == claude_dir or key.startswith(claude_dir + "\\"))
        if too_broad or key in seen or any(s in key + "\\" for s in _SKIP) or not os.path.isdir(norm):
            continue
        seen.add(key)
        out.append(norm)
    return out


def _cmdline(pid: int) -> list[str]:
    try:
        import psutil

        return psutil.Process(pid).cmdline()
    except Exception:  # noqa: BLE001 - gone, access denied
        return []


def guess(window: Optional[Any], known: list[str], last: str = "") -> str:
    """`window` is a WindowMeta (exe_path, title, pid) or None."""
    paths: list[str] = []
    title = ""
    if window is not None:
        paths.append(getattr(window, "exe_path", "") or "")
        if getattr(window, "pid", 0):
            paths += _cmdline(window.pid)
        title = getattr(window, "title", "") or ""
    lowered = [p.lower().replace("/", "\\") for p in paths if p]
    best = ""
    for folder in known:
        prefix = folder.lower().rstrip("\\") + "\\"
        if any(p.startswith(prefix) for p in lowered) and len(folder) > len(best):
            best = folder
    if not best and title:
        segments = {s.strip().lower() for s in re.split(r"\s[-\u2013\u2014|]\s", title)}
        for folder in known:
            name = os.path.basename(folder).lower()
            if len(name) >= 3 and name in segments and len(folder) > len(best):
                best = folder
    if best:
        return best
    if last and os.path.isdir(last):
        return last
    return str(Path.home())
