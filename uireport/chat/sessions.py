"""Claude Code conversations on this PC, for the Show Claude History list. Owner: chat.

Every Claude Code session - the Claude app's Code tab, the CLI, Ekxel's panel, this window - is a
JSONL file in ~/.claude/projects/<project>/<session id>.jsonl. Titles come from the newest
custom-title / ai-title / summary line (near the end of the file), the working folder and the first
prompt from the start. Files are read only at their head and tail, so big sessions stay quick.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PROJECTS = Path.home() / ".claude" / "projects"
HEAD_BYTES = 64 * 1024
TAIL_BYTES = 512 * 1024


@dataclass
class SessionInfo:
    id: str
    title: str
    cwd: str
    modified: float
    path: Path


def _lines(path: Path, head: bool) -> list[dict]:
    try:
        with open(path, "rb") as fh:
            if head:
                raw = fh.read(HEAD_BYTES)
            else:
                size = fh.seek(0, os.SEEK_END)
                fh.seek(max(0, size - TAIL_BYTES))
                raw = fh.read()
    except OSError:
        return []
    chunks = raw.split(b"\n")
    chunks = chunks[:-1] if head else chunks[1:]  # drop the partial line at the cut
    out = []
    for chunk in chunks:
        try:
            out.append(json.loads(chunk))
        except ValueError:
            continue
    return out


def _user_text(entry: dict) -> str:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    else:
        return ""
    text = text.strip()
    return "" if text.startswith("<") else text  # system reminders, command wrappers


def session_info(path: Path) -> Optional[SessionInfo]:
    head, tail = _lines(path, head=True), _lines(path, head=False)
    # the LATEST folder: a Claude app session can move folders, and --resume looks it up from there
    cwd = next((e.get("cwd") for e in reversed(tail) if e.get("cwd")), "") or next(
        (e.get("cwd") for e in head if e.get("cwd")), "")
    first = next((t for t in (_user_text(e) for e in head if e.get("type") == "user") if t), "")
    title = ""
    for e in reversed(tail):
        title = e.get("customTitle") or e.get("aiTitle") or e.get("title") or (
            e.get("summary") if e.get("type") == "summary" else "")
        if title:
            break
    title = (title or first or "(untitled)").replace("\n", " ").strip()
    if not cwd and not first:
        return None  # not a conversation (e.g. an empty or foreign file)
    return SessionInfo(id=path.stem, title=title[:80], cwd=cwd, modified=path.stat().st_mtime, path=path)


def list_sessions(limit: int = 30) -> list[SessionInfo]:
    """Newest first. Subagent transcripts (in subfolders) are skipped."""
    try:
        files = sorted(PROJECTS.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    out = []
    for path in files:
        info = session_info(path)
        if info is not None:
            out.append(info)
        if len(out) >= limit:
            break
    return out


def load_messages(path: Path, limit: int = 30) -> list[tuple[str, str]]:
    """The last `limit` chat messages as (role, text): user prompts and Claude's text replies."""
    out: list[tuple[str, str]] = []
    for e in _lines(Path(path), head=False):
        if e.get("isMeta") or e.get("isSidechain"):
            continue
        if e.get("type") == "user":
            text = _user_text(e)
            if text:
                out.append(("user", text))
        elif e.get("type") == "assistant":
            content = (e.get("message") or {}).get("content") or []
            text = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text").strip()
            if text:
                out.append(("assistant", text))
    return out[-limit:]


class OwnSessions:
    """Ids of conversations started in this window: they continue in place; all others continue as a copy."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _load(self) -> list[str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [str(x) for x in data] if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def __contains__(self, sid: str) -> bool:
        return sid in self._load()

    def add(self, sid: str) -> None:
        ids = [x for x in self._load() if x != sid][-499:] + [sid]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(ids), encoding="utf-8")
        except OSError:
            pass
