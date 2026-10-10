"""report.md / report.json content. Owner: output/app builder (package C). Pure functions,
no Qt, no disk access (the writer does the I/O)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any, Optional

from ..models import CaptureMode, MonitorMeta, Session, Shot
from .naming import shot_filenames

UNTITLED_GOAL = "Screenshots"
NO_CAPTION = "(no caption)"
HEADLINE_MAX = 90


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def win_path(path: "str | Path") -> str:
    """Absolute-looking path in backslash form (also for paths given with forward slashes)."""
    return str(PureWindowsPath(str(path)))


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def _bullet_text(text: str) -> str:
    """Multi-line user text inside a Markdown bullet: continuation lines are indented."""
    lines = [ln.rstrip() for ln in str(text or "").strip().splitlines()]
    return "\n  ".join(ln if ln else "" for ln in lines)


def _num(value: float) -> str:
    """100.0 -> '100', 66.7 -> '66.7'."""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _parse_iso(text: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None


def _fmt_minute(text: str) -> str:
    dt = _parse_iso(text)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else str(text)


def _fmt_second(text: str) -> str:
    dt = _parse_iso(text)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else str(text)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _headline(shot: Shot) -> str:
    first = _one_line(shot.caption)
    if not first:
        return NO_CAPTION
    if len(first) > HEADLINE_MAX:
        first = first[: HEADLINE_MAX - 3].rstrip() + "..."
    return first


def _monitor_line(m: MonitorMeta) -> str:
    bits = [f"{m.width_px}x{m.height_px} @ {m.scale_percent}%"]
    if m.is_primary:
        bits.append("primary")
    bits.append(f"at ({m.rect.x}, {m.rect.y})")
    name = m.name or (f'"{m.label}"' if m.label else "")
    if m.name and m.label and m.label != m.name:
        name = f'{m.name} "{m.label}"'
    return f"{m.index}: {name} ({', '.join(bits)})" if name else f"{m.index}: ({', '.join(bits)})"


def build_report_dict(session: Session) -> dict[str, Any]:
    """The report.json document == session.to_dict() (paths already absolute because the
    writer sets shot.original_path / annotated_path and session.folder before calling)."""
    return session.to_dict()


# ---------------------------------------------------------------------------
# report.md
# ---------------------------------------------------------------------------
_LEGEND = (
    "**How to read this:** shots are in order. *Shot* = a screenshot with no particular role; "
    "*Problem* = what is wrong now; *Want* = a "
    "reference/mockup of the target; *Context* = orientation only; *After* = the result after a "
    "fix, to verify it. Coordinates are pixels of the ORIGINAL image (top-left origin, x right, "
    "y down). Open every image listed below (annotated and original). Before answering, quote "
    "back word for word the text the user wrote for each shot (its caption and any annotation "
    "labels), or say that a shot has none."
)


def _header_lines(session: Session, folder: Path) -> list[str]:
    goal_line = _one_line(session.goal)
    if len(goal_line) > 120:
        goal_line = goal_line[:117].rstrip() + "..."
    out = [f"# Report: {goal_line}" if goal_line else f"# {UNTITLED_GOAL}", ""]
    fields = (
        ("Goal", session.goal),
        ("Project folder", session.project_path),
        ("App / framework", session.framework_hint),
        ("Expected", session.expected),
        ("Actual", session.actual),
    )
    for label, value in fields:
        if str(value or "").strip():
            out.append(f"- **{label}:** {_bullet_text(value)}")
    out.append(
        f"- **Created:** {_fmt_minute(session.created_at)} - {_plural(len(session.shots), 'screenshot')}"
    )
    out.append(f"- **Report folder:** {win_path(folder)}")

    sysm = session.system
    env: list[str] = []
    if sysm.windows_version:
        env.append(sysm.windows_version)
    if sysm.theme_apps and sysm.theme_apps != "unknown":
        env.append(f"apps theme {sysm.theme_apps}")
    if sysm.theme_system and sysm.theme_system != "unknown" and sysm.theme_system != sysm.theme_apps:
        env.append(f"system theme {sysm.theme_system}")
    if sysm.tool_version:
        env.append(f"tool v{sysm.tool_version}")
    if env:
        out.append(f"- **Environment:** {' - '.join(env)}")
    if sysm.monitors:
        out.append(f"- **Monitors:** {'; '.join(_monitor_line(m) for m in sysm.monitors)}")
    out.append("")
    out.append(_LEGEND)
    out.append("")
    return out


def _capture_line(shot: Shot) -> str:
    mode = "delayed capture" if shot.capture_mode == CaptureMode.DELAYED else "normal capture"
    w = shot.image_width or shot.capture_rect.w
    h = shot.image_height or shot.capture_rect.h
    kind = shot.selection or "region"
    text = f"{_fmt_second(shot.timestamp)}, {mode}, {kind} {w}x{h} px"
    if not shot.capture_rect.is_empty:
        text += f" at screen ({shot.capture_rect.x}, {shot.capture_rect.y})"
    return text


def _monitor_shot_line(shot: Shot) -> Optional[str]:
    m = shot.monitor
    if m is None:
        return None
    name = m.name
    if m.label and m.label != m.name:
        name = f'{m.name} "{m.label}"' if m.name else f'"{m.label}"'
    inner = ", ".join(p for p in (name, f"{m.width_px}x{m.height_px}", f"scale {m.scale_percent}%") if p)
    return f"{m.index} ({inner})"


def _window_line(shot: Shot) -> Optional[str]:
    w = shot.window
    if w is None:
        return None
    parts: list[str] = []
    if w.title:
        parts.append(f'"{w.title}"')
    if w.process_name:
        exe = f" ({w.exe_path})" if w.exe_path else ""
        parts.append(f"{w.process_name}{exe}")
    elif w.exe_path:
        parts.append(w.exe_path)
    if not w.rect.is_empty:
        pw, ph = w.size_px
        lw, lh = w.size_logical
        parts.append(f"{pw}x{ph} px ({lw}x{lh} logical @{w.scale_percent}%)")
    return " - ".join(parts) if parts else None


def _shot_lines(shot: Shot, position: int, total: int, folder: Path) -> list[str]:
    orig_name, ann_name = shot_filenames(position, total)
    annotated = shot.annotated_path or str(folder / ann_name)
    original = shot.original_path or str(folder / orig_name)
    role = shot.role

    out = [f"## {position}. [{role.label}] {_headline(shot)}", ""]
    desc = role.description
    out.append(f"- **Role:** {role.label} - {desc[:1].lower() + desc[1:]}")
    out.append(f"- **Caption:** {_bullet_text(shot.caption) if shot.caption.strip() else NO_CAPTION}")
    out.append(f"- **Annotated image:** {win_path(annotated)}")
    out.append(f"- **Original image:** {win_path(original)}")
    out.append(f"- **Captured:** {_capture_line(shot)}")
    mon = _monitor_shot_line(shot)
    if mon:
        out.append(f"- **Monitor:** {mon}")
    win = _window_line(shot)
    if win:
        out.append(f"- **Window:** {win}")
    if shot.window and shot.window.url:
        out.append(f"- **URL:** {shot.window.url}")

    scale_pct = shot.monitor.scale_percent if shot.monitor else 100
    pins, rulers, rects, arrows = shot.pins, shot.rulers, shot.rects, shot.arrows
    if pins:
        out += ["", "**Pins**"]
        for p in pins:
            line = f"- Pin {p.n} at ({p.x}, {p.y})"
            if p.color:
                line += f", color {p.color}"
            note = _one_line(p.note)
            if note:
                line += f": {note}"
            out.append(line)
    if rulers:
        out += ["", "**Measurements**"]
        for i, r in enumerate(rulers, start=1):
            line = f"- Ruler {i}: ({r.x1}, {r.y1}) -> ({r.x2}, {r.y2}) = {_num(r.length_px)} px"
            extras = []
            if scale_pct != 100:
                extras.append(f"{_num(shot.to_logical(r.length_px))} logical px at {scale_pct}%")
            if r.dx and r.dy:
                extras.append(f"dx {r.dx}, dy {r.dy}")
            if extras:
                line += f" ({'; '.join(extras)})"
            out.append(line)
    if rects:
        out += ["", "**Rectangles**"]
        for i, r in enumerate(rects, start=1):
            out.append(f"- Rect {i}: x={r.x}, y={r.y}, {r.w}x{r.h}")
    if arrows:
        out += ["", "**Arrows**"]
        for i, a in enumerate(arrows, start=1):
            out.append(f"- Arrow {i}: ({a.x1}, {a.y1}) -> ({a.x2}, {a.y2})")
    n_red = len(shot.redactions)
    if n_red:
        noun = "region" if n_red == 1 else "regions"
        out += ["", f"**Redactions:** {n_red} {noun} redacted in both images."]
    if not (pins or rulers or rects or arrows or n_red):
        out += ["", "*No annotations on this shot.*"]
    out.append("")
    return out


def build_report_md(session: Session, folder: Path) -> str:
    """Human- and Claude-readable Markdown. Required structure (CONTRACT.md section 7):
    title, goal + session fields (only non-empty ones), environment line, a short 'how to
    read this' legend, then one '## N. [Role] caption-headline' section per shot in order
    with role, caption, ABSOLUTE paths of the annotated and the original image, capture
    info (time, normal/delayed, size, screen rect), monitor + scale, window title / process
    / exe / size in px and logical px, URL when known, the pin list
    ('1 at (412, 88), color #1F2937: button text is clipped'), measurements (rulers, with
    logical-px equivalent when the scale is not 100%), rectangles, arrows and a redaction
    count. UTF-8, LF newlines, paths in backslash form."""
    folder = Path(folder)
    lines = _header_lines(session, folder)
    total = len(session.shots)
    for position, shot in enumerate(session.shots, start=1):
        lines += _shot_lines(shot, position, total, folder)
    text = "\n".join(lines).rstrip("\n") + "\n"
    return text.replace("\r\n", "\n").replace("\r", "\n")
