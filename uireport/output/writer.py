"""Writes a finished session to disk. Owner: output/app builder (package C)."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Callable, Optional

from ..models import RedactAnn, Session, now_iso
from .naming import session_folder_name, shot_filenames, unique_folder
from .render import render_annotated, render_original, save_png
from .report import build_report_dict, build_report_md

log = logging.getLogger("uireport.output")

LATEST_SCHEMA_VERSION = 1


@dataclass
class SessionOutput:
    folder: Path  # absolute
    report_md: Path  # absolute
    report_json: Path  # absolute
    image_files: list[Path] = field(default_factory=list)  # every PNG written, in order


def _abs(path: "str | Path") -> Path:
    return Path(os.path.abspath(str(path)))


def _make_session_folder(root: Path, name: str) -> Path:
    """Create root/<name> (or -2, -3, ...) atomically enough that two writers never share one."""
    root.mkdir(parents=True, exist_ok=True)
    for _ in range(1000):
        candidate = unique_folder(root, name)
        try:
            candidate.mkdir(parents=False, exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise OSError(f"could not create a unique session folder under {root}")


def _write_text_atomic(path: Path, text: str) -> None:
    """UTF-8, LF, via a temp file in the same folder so readers never see half a file."""
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_session(
    session: Session,
    output_root: Path,
    *,
    now: Optional[datetime] = None,
    progress: Optional[Callable[[int, int], None]] = None,
) -> SessionOutput:
    """Create output_root/<session_folder_name>/ (unique), then for each shot in order write
    NN.png (redactions applied) and NN_annotated.png, set shot.original_path /
    shot.annotated_path (absolute strings) and session.folder, finally write report.md and
    report.json (UTF-8) and the latest pointer (write_latest_pointer).

    Thread-safe with respect to Qt widgets (uses only QImage/QPainter), so the controller
    runs it on a worker thread. `progress(done, total)` is called after each shot (from the
    calling thread). If anything fails the exception propagates and the partial folder is
    left in place (never delete user data). It never writes anywhere except inside
    `output_root`."""
    root = _abs(output_root)
    total = len(session.shots)
    if total == 0:
        raise ValueError("the session has no screenshots to save")
    for position, shot in enumerate(session.shots, start=1):
        if shot.image is None or shot.image.isNull():
            raise ValueError(f"screenshot {position} has no image data")
    if any(s.index != i for i, s in enumerate(session.shots, start=1)):
        session.renumber()

    folder = _make_session_folder(root, session_folder_name(session, now))
    if folder.parent != root:  # cannot happen (slug is [a-z0-9-]) but never write outside root
        raise OSError(f"refusing to write outside {root}")

    image_files: list[Path] = []
    for position, shot in enumerate(session.shots, start=1):
        orig_name, ann_name = shot_filenames(position, total)
        orig_path = folder / orig_name
        ann_path = folder / ann_name

        original = render_original(shot)
        save_png(original, orig_path)
        if any(not isinstance(a, RedactAnn) for a in shot.annotations):
            annotated = render_annotated(shot)
        else:
            annotated = original  # nothing to paint on top of the (redacted) original
        save_png(annotated, ann_path)
        del original, annotated

        shot.original_path = str(PureWindowsPath(orig_path))
        shot.annotated_path = str(PureWindowsPath(ann_path))
        image_files += [orig_path, ann_path]
        if progress is not None:
            try:
                progress(position, total)
            except Exception:  # a broken progress callback must not lose the report
                log.exception("progress callback failed")

    session.folder = str(PureWindowsPath(folder))
    report_json = folder / "report.json"
    report_md = folder / "report.md"
    _write_text_atomic(report_json, json.dumps(build_report_dict(session), indent=2, ensure_ascii=False) + "\n")
    _write_text_atomic(report_md, build_report_md(session, folder))

    out = SessionOutput(folder=folder, report_md=report_md, report_json=report_json, image_files=image_files)
    write_latest_pointer(root, out, session, now=now)
    return out


def write_latest_pointer(
    output_root: Path, out: SessionOutput, session: Session, *, now: Optional[datetime] = None
) -> Path:
    """output_root/latest.json: {"folder", "report_md", "report_json", "created_at",
    "shot_count", "goal"} (absolute paths). Lets a future MCP server find the newest report."""
    root = _abs(output_root)
    root.mkdir(parents=True, exist_ok=True)
    created = now.astimezone().isoformat(timespec="seconds") if now is not None else now_iso()
    data = {
        "schema_version": LATEST_SCHEMA_VERSION,
        "folder": str(PureWindowsPath(out.folder)),
        "report_md": str(PureWindowsPath(out.report_md)),
        "report_json": str(PureWindowsPath(out.report_json)),
        "created_at": created,
        "shot_count": len(session.shots),
        "goal": session.goal,
    }
    path = root / "latest.json"
    _write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return path
