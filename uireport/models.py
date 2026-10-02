"""Shared data model: Session, Shot, roles, capture modes, annotations, metadata.

Pure Python (no Qt, no Win32) so it can be unit-tested anywhere. The only runtime
attribute that is a Qt object is `Shot.image` (a QImage, never serialised).

All coordinates stored here are PHYSICAL pixels:
  * annotation coordinates .............. image pixels (origin = top-left of the saved PNG)
  * Shot.capture_rect / MonitorMeta.rect  virtual-screen pixels (origin = primary monitor
                                          top-left, may be negative)
  * WindowMeta.rect ..................... virtual-screen pixels
Logical (DIP) sizes are always derived: logical = round(physical / scale).
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar, Iterable, Optional

from . import APP_NAME, __version__
from .geometry import (
    BASE_DPI,
    IntRect,
    scale_factor_from_dpi,
    scale_percent_from_dpi,
    to_logical,
)

SCHEMA_VERSION = 1


def new_id() -> str:
    """Short unique id (8 hex chars) for sessions, shots and annotations."""
    return uuid.uuid4().hex[:8]


def now_iso() -> str:
    """Local time with UTC offset, whole seconds: 2026-09-29T14:32:05+02:00."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class Role(StrEnum):
    SHOT = "shot"  # neutral default: screenshots aren't always of a problem
    PROBLEM = "problem"
    WANT = "want"
    CONTEXT = "context"
    AFTER = "after"

    @property
    def label(self) -> str:
        return ROLE_LABELS[self]

    @property
    def description(self) -> str:
        return ROLE_DESCRIPTIONS[self]

    @property
    def color(self) -> str:
        return ROLE_COLORS[self]


ROLE_LABELS: dict[Role, str] = {
    Role.SHOT: "Shot",
    Role.PROBLEM: "Problem",
    Role.WANT: "Want",
    Role.CONTEXT: "Context",
    Role.AFTER: "After",
}
ROLE_DESCRIPTIONS: dict[Role, str] = {
    Role.SHOT: "Just a screenshot, no particular role",
    Role.PROBLEM: "What's wrong now",
    Role.WANT: "A reference or mockup of what I want",
    Role.CONTEXT: "For orientation only",
    Role.AFTER: "The result after a fix, to verify it",
}
ROLE_COLORS: dict[Role, str] = {
    Role.SHOT: "#6E56CF",
    Role.PROBLEM: "#E5484D",
    Role.WANT: "#3E63DD",
    Role.CONTEXT: "#8B8D98",
    Role.AFTER: "#30A46C",
}
ROLE_ORDER: tuple[Role, ...] = (Role.SHOT, Role.PROBLEM, Role.WANT, Role.CONTEXT, Role.AFTER)
DEFAULT_ROLE = Role.SHOT


class CaptureMode(StrEnum):
    NORMAL = "normal"
    DELAYED = "delayed"


class AnnotationType(StrEnum):
    PIN = "pin"
    RECT = "rect"
    ARROW = "arrow"
    RULER = "ruler"
    REDACT = "redact"


SELECTION_KINDS = ("region", "window", "monitor")


# ---------------------------------------------------------------------------
# Annotations (coordinates = image pixels)
# ---------------------------------------------------------------------------
@dataclass
class Annotation:
    """Base class. Use the concrete subclasses; `annotation_from_dict` dispatches on type."""

    id: str = field(default_factory=new_id)
    TYPE: ClassVar[AnnotationType]

    def to_dict(self) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    @property
    def type(self) -> AnnotationType:
        return self.TYPE


@dataclass
class PinAnn(Annotation):
    """Numbered pin. (x, y) is the image pixel the pin points at; `color` is the
    "#RRGGBB" of that pixel in the ORIGINAL (unannotated, unredacted) image."""

    TYPE: ClassVar[AnnotationType] = AnnotationType.PIN
    n: int = 0  # 1-based display number; kept contiguous by Shot.renumber_pins()
    x: int = 0
    y: int = 0
    note: str = ""
    color: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.TYPE.value,
            "id": self.id,
            "n": self.n,
            "x": self.x,
            "y": self.y,
            "color": self.color,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PinAnn":
        return cls(
            id=str(d.get("id") or new_id()),
            n=int(d.get("n", 0)),
            x=int(d.get("x", 0)),
            y=int(d.get("y", 0)),
            note=str(d.get("note", "")),
            color=str(d.get("color", "")),
        )


@dataclass
class RectAnn(Annotation):
    """Rectangle outline: top-left (x, y), size (w, h), always normalised (w, h >= 0)."""

    TYPE: ClassVar[AnnotationType] = AnnotationType.RECT
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0

    @classmethod
    def from_points(cls, x1: float, y1: float, x2: float, y2: float, **kw: Any) -> "RectAnn":
        r = IntRect.from_points(x1, y1, x2, y2)
        return cls(x=r.x, y=r.y, w=r.w, h=r.h, **kw)

    @property
    def irect(self) -> IntRect:
        return IntRect(self.x, self.y, self.w, self.h)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.TYPE.value, "id": self.id, "x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RectAnn":
        return cls(
            id=str(d.get("id") or new_id()),
            x=int(d.get("x", 0)),
            y=int(d.get("y", 0)),
            w=int(d.get("w", 0)),
            h=int(d.get("h", 0)),
        )


@dataclass
class ArrowAnn(Annotation):
    """Arrow from tail (x1, y1) to head (x2, y2); the arrowhead is drawn at (x2, y2)."""

    TYPE: ClassVar[AnnotationType] = AnnotationType.ARROW
    x1: int = 0
    y1: int = 0
    x2: int = 0
    y2: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.TYPE.value,
            "id": self.id,
            "x1": self.x1,
            "y1": self.y1,
            "x2": self.x2,
            "y2": self.y2,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ArrowAnn":
        return cls(
            id=str(d.get("id") or new_id()),
            x1=int(d.get("x1", 0)),
            y1=int(d.get("y1", 0)),
            x2=int(d.get("x2", 0)),
            y2=int(d.get("y2", 0)),
        )


@dataclass
class RulerAnn(Annotation):
    """Measurement between two image pixels. length_px is derived and always in
    PHYSICAL (image) pixels; use Shot.to_logical() for the DIP equivalent."""

    TYPE: ClassVar[AnnotationType] = AnnotationType.RULER
    x1: int = 0
    y1: int = 0
    x2: int = 0
    y2: int = 0

    @property
    def dx(self) -> int:
        return self.x2 - self.x1

    @property
    def dy(self) -> int:
        return self.y2 - self.y1

    @property
    def length_px(self) -> float:
        return round(math.hypot(self.dx, self.dy), 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.TYPE.value,
            "id": self.id,
            "x1": self.x1,
            "y1": self.y1,
            "x2": self.x2,
            "y2": self.y2,
            "dx": self.dx,
            "dy": self.dy,
            "length_px": self.length_px,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RulerAnn":
        return cls(
            id=str(d.get("id") or new_id()),
            x1=int(d.get("x1", 0)),
            y1=int(d.get("y1", 0)),
            x2=int(d.get("x2", 0)),
            y2=int(d.get("y2", 0)),
        )


@dataclass
class RedactAnn(Annotation):
    """Redaction box. imageops.apply_redactions() pixelates it in BOTH saved images."""

    TYPE: ClassVar[AnnotationType] = AnnotationType.REDACT
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    style: str = "pixelate"  # reserved: "pixelate" (default) | "solid"

    @classmethod
    def from_points(cls, x1: float, y1: float, x2: float, y2: float, **kw: Any) -> "RedactAnn":
        r = IntRect.from_points(x1, y1, x2, y2)
        return cls(x=r.x, y=r.y, w=r.w, h=r.h, **kw)

    @property
    def irect(self) -> IntRect:
        return IntRect(self.x, self.y, self.w, self.h)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.TYPE.value,
            "id": self.id,
            "x": self.x,
            "y": self.y,
            "w": self.w,
            "h": self.h,
            "style": self.style,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RedactAnn":
        return cls(
            id=str(d.get("id") or new_id()),
            x=int(d.get("x", 0)),
            y=int(d.get("y", 0)),
            w=int(d.get("w", 0)),
            h=int(d.get("h", 0)),
            style=str(d.get("style", "pixelate")),
        )


_ANNOTATION_CLASSES: dict[str, type[Annotation]] = {
    AnnotationType.PIN.value: PinAnn,
    AnnotationType.RECT.value: RectAnn,
    AnnotationType.ARROW.value: ArrowAnn,
    AnnotationType.RULER.value: RulerAnn,
    AnnotationType.REDACT.value: RedactAnn,
}


def annotation_from_dict(d: dict[str, Any]) -> Annotation:
    """Rebuild a concrete annotation from its dict. Raises ValueError on unknown type."""
    t = str(d.get("type", ""))
    cls = _ANNOTATION_CLASSES.get(t)
    if cls is None:
        raise ValueError(f"Unknown annotation type {t!r}")
    return cls.from_dict(d)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Capture metadata
# ---------------------------------------------------------------------------
@dataclass
class MonitorMeta:
    """One monitor. `index` is 1-based in enumerate_monitors() order (sorted by device name)."""

    index: int = 1
    name: str = ""  # Windows device name, e.g. \\.\DISPLAY1
    is_primary: bool = False
    rect: IntRect = field(default_factory=IntRect)  # virtual-screen physical pixels
    dpi: int = BASE_DPI  # effective DPI (96 = 100%, 120 = 125%, 144 = 150%)
    label: str = ""  # friendly display name if known (e.g. "DELL U2720Q"); QScreen.name() on Windows

    @property
    def width_px(self) -> int:
        return self.rect.w

    @property
    def height_px(self) -> int:
        return self.rect.h

    @property
    def scale_factor(self) -> float:
        return scale_factor_from_dpi(self.dpi)

    @property
    def scale_percent(self) -> int:
        return scale_percent_from_dpi(self.dpi)

    @property
    def logical_size(self) -> tuple[int, int]:
        s = self.scale_factor
        return (to_logical(self.rect.w, s), to_logical(self.rect.h, s))

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "label": self.label,
            "is_primary": self.is_primary,
            "rect": self.rect.to_dict(),
            "width_px": self.width_px,
            "height_px": self.height_px,
            "dpi": self.dpi,
            "scale_percent": self.scale_percent,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MonitorMeta":
        rect = IntRect.from_dict(d.get("rect")) if d.get("rect") is not None else IntRect(
            0, 0, int(d.get("width_px", 0)), int(d.get("height_px", 0))
        )
        if "dpi" in d:
            dpi = int(d["dpi"])
        elif "scale_percent" in d:
            dpi = int(round(BASE_DPI * float(d["scale_percent"]) / 100.0))
        else:
            dpi = BASE_DPI
        return cls(
            index=int(d.get("index", 1)),
            name=str(d.get("name", "")),
            is_primary=bool(d.get("is_primary", False)),
            rect=rect,
            dpi=dpi,
            label=str(d.get("label", "")),
        )


BROWSERS = ("chrome", "edge", "firefox")


@dataclass
class WindowMeta:
    """The foreground window at capture time (or the picked window in window mode)."""

    title: str = ""
    process_name: str = ""  # e.g. "chrome.exe"
    exe_path: str = ""
    pid: int = 0
    rect: IntRect = field(default_factory=IntRect)  # visible frame, virtual-screen physical px
    dpi: int = BASE_DPI  # effective DPI used to derive the logical size
    browser: Optional[str] = None  # one of BROWSERS or None
    url: Optional[str] = None  # best-effort via UI Automation; None if unavailable

    @property
    def size_px(self) -> tuple[int, int]:
        return (self.rect.w, self.rect.h)

    @property
    def scale_factor(self) -> float:
        return scale_factor_from_dpi(self.dpi)

    @property
    def scale_percent(self) -> int:
        return scale_percent_from_dpi(self.dpi)

    @property
    def size_logical(self) -> tuple[int, int]:
        s = self.scale_factor
        return (to_logical(self.rect.w, s), to_logical(self.rect.h, s))

    def to_dict(self) -> dict[str, Any]:
        lw, lh = self.size_logical
        return {
            "title": self.title,
            "process_name": self.process_name,
            "exe_path": self.exe_path,
            "pid": self.pid,
            "rect": self.rect.to_dict(),
            "size_px": {"width": self.rect.w, "height": self.rect.h},
            "size_logical": {"width": lw, "height": lh},
            "dpi": self.dpi,
            "scale_percent": self.scale_percent,
            "browser": self.browser,
            "url": self.url,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WindowMeta":
        rect = IntRect.from_dict(d.get("rect"))
        if rect.is_empty and isinstance(d.get("size_px"), dict):
            rect = IntRect(0, 0, int(d["size_px"].get("width", 0)), int(d["size_px"].get("height", 0)))
        return cls(
            title=str(d.get("title", "")),
            process_name=str(d.get("process_name", "")),
            exe_path=str(d.get("exe_path", "")),
            pid=int(d.get("pid", 0) or 0),
            rect=rect,
            dpi=int(d.get("dpi", BASE_DPI) or BASE_DPI),
            browser=d.get("browser") or None,
            url=d.get("url") or None,
        )


# ---------------------------------------------------------------------------
# Shot
# ---------------------------------------------------------------------------
@dataclass
class Shot:
    id: str = field(default_factory=new_id)
    index: int = 0  # 1-based position in the session; maintained by Session.renumber()
    role: Role = DEFAULT_ROLE
    caption: str = ""
    capture_mode: CaptureMode = CaptureMode.NORMAL
    selection: str = "region"  # how the area was chosen: region | window | monitor
    timestamp: str = field(default_factory=now_iso)
    image_width: int = 0  # px of the saved PNG == physical px of the capture, never scaled
    image_height: int = 0
    capture_rect: IntRect = field(default_factory=IntRect)  # virtual-screen physical px
    monitor: Optional[MonitorMeta] = None
    window: Optional[WindowMeta] = None
    annotations: list[Annotation] = field(default_factory=list)
    original_path: Optional[str] = None  # absolute, set by the writer on Finish
    annotated_path: Optional[str] = None
    media: str = "image"  # reserved for a future "recording"
    pair_with: Optional[str] = None  # reserved for a future compare mode (id of the paired shot)
    extra: dict[str, Any] = field(default_factory=dict)  # unknown keys preserved across load/save
    # Runtime only, never serialised: the ORIGINAL QImage (unannotated, unredacted).
    image: Any = field(default=None, repr=False, compare=False)

    # ---- image ----------------------------------------------------------
    def set_image(self, image: Any) -> None:
        """Attach the original QImage and record its pixel size."""
        self.image = image
        if image is not None:
            self.image_width = int(image.width())
            self.image_height = int(image.height())

    @property
    def image_rect(self) -> IntRect:
        return IntRect(0, 0, self.image_width, self.image_height)

    @property
    def scale_factor(self) -> float:
        """Windows display scale of the monitor this was captured on (1.0 if unknown)."""
        return self.monitor.scale_factor if self.monitor else 1.0

    def to_logical(self, physical_px: float) -> float:
        """Convert a length measured in image pixels to logical (DIP) pixels, 1 decimal."""
        return round(physical_px / self.scale_factor, 1)

    # ---- annotations ----------------------------------------------------
    def annotations_of(self, kind: AnnotationType) -> list[Annotation]:
        return [a for a in self.annotations if a.TYPE == kind]

    @property
    def pins(self) -> list[PinAnn]:
        return [a for a in self.annotations if isinstance(a, PinAnn)]

    @property
    def rects(self) -> list[RectAnn]:
        return [a for a in self.annotations if isinstance(a, RectAnn)]

    @property
    def arrows(self) -> list[ArrowAnn]:
        return [a for a in self.annotations if isinstance(a, ArrowAnn)]

    @property
    def rulers(self) -> list[RulerAnn]:
        return [a for a in self.annotations if isinstance(a, RulerAnn)]

    @property
    def redactions(self) -> list[RedactAnn]:
        return [a for a in self.annotations if isinstance(a, RedactAnn)]

    def find_annotation(self, ann_id: str) -> Optional[Annotation]:
        for a in self.annotations:
            if a.id == ann_id:
                return a
        return None

    def add_annotation(self, ann: Annotation, index: Optional[int] = None) -> Annotation:
        """Append (or insert at `index`) and renumber pins so numbers stay 1..N."""
        if index is None:
            self.annotations.append(ann)
        else:
            self.annotations.insert(index, ann)
        self.renumber_pins()
        return ann

    def remove_annotation(self, ann_id: str) -> Optional[Annotation]:
        for i, a in enumerate(self.annotations):
            if a.id == ann_id:
                removed = self.annotations.pop(i)
                self.renumber_pins()
                return removed
        return None

    def renumber_pins(self) -> None:
        """Pins are numbered 1..N in list (creation) order."""
        for n, pin in enumerate(self.pins, start=1):
            pin.n = n

    # ---- serialisation --------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "index": self.index,
            "role": self.role.value,
            "caption": self.caption,
            "capture_mode": self.capture_mode.value,
            "selection": self.selection,
            "timestamp": self.timestamp,
            "image_size": {"width_px": self.image_width, "height_px": self.image_height},
            "capture_rect": self.capture_rect.to_dict(),
            "monitor": self.monitor.to_dict() if self.monitor else None,
            "window": self.window.to_dict() if self.window else None,
            "files": {"original": self.original_path, "annotated": self.annotated_path},
            "annotations": [a.to_dict() for a in self.annotations],
            "media": self.media,
            "pair_with": self.pair_with,
        }
        for k, v in self.extra.items():
            d.setdefault(k, v)
        return d

    _KNOWN_KEYS: ClassVar[frozenset[str]] = frozenset(
        {
            "id", "index", "role", "caption", "capture_mode", "selection", "timestamp",
            "image_size", "capture_rect", "monitor", "window", "files", "annotations",
            "media", "pair_with",
        }
    )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Shot":
        try:
            role = Role(str(d.get("role", DEFAULT_ROLE.value)))
        except ValueError:
            role = DEFAULT_ROLE
        try:
            mode = CaptureMode(str(d.get("capture_mode", CaptureMode.NORMAL.value)))
        except ValueError:
            mode = CaptureMode.NORMAL
        size = d.get("image_size") or {}
        files = d.get("files") or {}
        anns: list[Annotation] = []
        for ad in d.get("annotations") or []:
            try:
                anns.append(annotation_from_dict(ad))
            except (ValueError, TypeError):
                continue  # forward compatibility: skip unknown annotation types
        shot = cls(
            id=str(d.get("id") or new_id()),
            index=int(d.get("index", 0)),
            role=role,
            caption=str(d.get("caption", "")),
            capture_mode=mode,
            selection=str(d.get("selection", "region")),
            timestamp=str(d.get("timestamp") or now_iso()),
            image_width=int(size.get("width_px", 0)),
            image_height=int(size.get("height_px", 0)),
            capture_rect=IntRect.from_dict(d.get("capture_rect")),
            monitor=MonitorMeta.from_dict(d["monitor"]) if d.get("monitor") else None,
            window=WindowMeta.from_dict(d["window"]) if d.get("window") else None,
            annotations=anns,
            original_path=files.get("original"),
            annotated_path=files.get("annotated"),
            media=str(d.get("media", "image")),
            pair_with=d.get("pair_with"),
            extra={k: v for k, v in d.items() if k not in cls._KNOWN_KEYS},
        )
        return shot


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
@dataclass
class SystemMeta:
    """Per-session environment facts (filled by capture.sysinfo.collect_system_meta())."""

    windows_version: str = ""  # e.g. "Windows 11 Pro 10.0.26200"
    windows_build: int = 0
    theme_apps: str = "unknown"  # "light" | "dark" | "unknown"  (AppsUseLightTheme)
    theme_system: str = "unknown"  # "light" | "dark" | "unknown"  (SystemUsesLightTheme)
    monitors: list[MonitorMeta] = field(default_factory=list)  # the monitor layout
    tool_version: str = __version__

    def to_dict(self) -> dict[str, Any]:
        return {
            "windows_version": self.windows_version,
            "windows_build": self.windows_build,
            "theme_apps": self.theme_apps,
            "theme_system": self.theme_system,
            "monitors": [m.to_dict() for m in self.monitors],
            "tool_version": self.tool_version,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SystemMeta":
        return cls(
            windows_version=str(d.get("windows_version", "")),
            windows_build=int(d.get("windows_build", 0) or 0),
            theme_apps=str(d.get("theme_apps", "unknown")),
            theme_system=str(d.get("theme_system", "unknown")),
            monitors=[MonitorMeta.from_dict(m) for m in d.get("monitors") or []],
            tool_version=str(d.get("tool_version", __version__)),
        )


@dataclass
class Session:
    id: str = field(default_factory=new_id)
    created_at: str = field(default_factory=now_iso)
    goal: str = ""
    project_path: str = ""
    framework_hint: str = ""
    expected: str = ""
    actual: str = ""
    shots: list[Shot] = field(default_factory=list)
    system: SystemMeta = field(default_factory=SystemMeta)
    folder: Optional[str] = None  # absolute output folder, set by the writer on Finish
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(cls, project_path: str = "", framework_hint: str = "") -> "Session":
        return cls(project_path=project_path, framework_hint=framework_hint)

    # ---- shot list management ------------------------------------------
    def renumber(self) -> None:
        for i, s in enumerate(self.shots, start=1):
            s.index = i

    def add_shot(self, shot: Shot) -> Shot:
        self.shots.append(shot)
        self.renumber()
        return shot

    def get_shot(self, shot_id: str) -> Optional[Shot]:
        for s in self.shots:
            if s.id == shot_id:
                return s
        return None

    def index_of(self, shot_id: str) -> int:
        """0-based list position, or -1."""
        for i, s in enumerate(self.shots):
            if s.id == shot_id:
                return i
        return -1

    def remove_shot(self, shot_id: str) -> Optional[Shot]:
        i = self.index_of(shot_id)
        if i < 0:
            return None
        removed = self.shots.pop(i)
        self.renumber()
        return removed

    def move_shot(self, shot_id: str, new_index: int) -> bool:
        """Move a shot to 0-based position `new_index` (clamped). False if id unknown."""
        i = self.index_of(shot_id)
        if i < 0:
            return False
        shot = self.shots.pop(i)
        new_index = max(0, min(len(self.shots), new_index))
        self.shots.insert(new_index, shot)
        self.renumber()
        return True

    def reorder(self, ordered_ids: Iterable[str]) -> None:
        """Apply a full new order (used by the drag-reorder panel). Must be a permutation."""
        ids = list(ordered_ids)
        if sorted(ids) != sorted(s.id for s in self.shots):
            raise ValueError("reorder() needs exactly the current shot ids, each once")
        by_id = {s.id: s for s in self.shots}
        self.shots = [by_id[i] for i in ids]
        self.renumber()

    @property
    def shot_count(self) -> int:
        return len(self.shots)

    # ---- serialisation --------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """The whole report.json document."""
        return {
            "schema_version": SCHEMA_VERSION,
            "tool": {"name": APP_NAME, "version": __version__},
            "session": {
                "id": self.id,
                "created_at": self.created_at,
                "goal": self.goal,
                "project_path": self.project_path,
                "framework_hint": self.framework_hint,
                "expected": self.expected,
                "actual": self.actual,
                "folder": self.folder,
                "shot_count": len(self.shots),
                "system": self.system.to_dict(),
                **({"extra": self.extra} if self.extra else {}),
            },
            "shots": [s.to_dict() for s in self.shots],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Session":
        sd = d.get("session") or {}
        sess = cls(
            id=str(sd.get("id") or new_id()),
            created_at=str(sd.get("created_at") or now_iso()),
            goal=str(sd.get("goal", "")),
            project_path=str(sd.get("project_path", "")),
            framework_hint=str(sd.get("framework_hint", "")),
            expected=str(sd.get("expected", "")),
            actual=str(sd.get("actual", "")),
            shots=[Shot.from_dict(s) for s in d.get("shots") or []],
            system=SystemMeta.from_dict(sd.get("system") or {}),
            folder=sd.get("folder"),
            extra=dict(sd.get("extra") or {}),
        )
        sess.renumber()
        return sess


# ---------------------------------------------------------------------------
# Monitor lookup helpers
# ---------------------------------------------------------------------------
def monitor_at_point(monitors: list[MonitorMeta], x: float, y: float) -> Optional[MonitorMeta]:
    """Monitor containing the virtual-screen point, else the nearest one, else None."""
    if not monitors:
        return None
    for m in monitors:
        if m.rect.contains_point(x, y):
            return m

    def dist2(m: MonitorMeta) -> float:
        dx = max(m.rect.left - x, 0, x - (m.rect.right - 1))
        dy = max(m.rect.top - y, 0, y - (m.rect.bottom - 1))
        return dx * dx + dy * dy

    return min(monitors, key=dist2)


def monitor_for_rect(monitors: list[MonitorMeta], rect: IntRect) -> Optional[MonitorMeta]:
    """Monitor with the largest overlap with `rect` (ties -> first), else nearest to its centre."""
    if not monitors:
        return None
    best: Optional[MonitorMeta] = None
    best_area = 0
    for m in monitors:
        inter = m.rect.intersection(rect)
        a = inter.area if inter else 0
        if a > best_area:
            best, best_area = m, a
    if best is not None:
        return best
    cx, cy = rect.center
    return monitor_at_point(monitors, cx, cy)
