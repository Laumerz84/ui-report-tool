"""Pure-Python geometry helpers shared by every package (no Qt, no Win32).

Conventions (see CONTRACT.md section 4):
  * "physical" pixels  = real device pixels. Screen coordinates, capture rects,
    image pixels and window rects are ALL physical pixels.
  * "logical" pixels   = physical / scale, where scale = dpi / 96
    (1.0 at 100%, 1.25 at 125%, 1.5 at 150%, ...).
  * IntRect is half-open: it covers x <= px < x + w and y <= py < y + h.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

BASE_DPI = 96


@dataclass(frozen=True)
class IntRect:
    """Integer rectangle (top-left + size). Half-open: right/bottom are exclusive."""

    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0

    # ---- derived -------------------------------------------------------
    @property
    def left(self) -> int:
        return self.x

    @property
    def top(self) -> int:
        return self.y

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def size(self) -> tuple[int, int]:
        return (self.w, self.h)

    @property
    def area(self) -> int:
        return max(0, self.w) * max(0, self.h)

    @property
    def is_empty(self) -> bool:
        return self.w <= 0 or self.h <= 0

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.w / 2.0, self.y + self.h / 2.0)

    # ---- queries -------------------------------------------------------
    def contains_point(self, px: float, py: float) -> bool:
        return self.x <= px < self.right and self.y <= py < self.bottom

    def contains_rect(self, other: "IntRect") -> bool:
        return (
            not other.is_empty
            and self.x <= other.x
            and self.y <= other.y
            and other.right <= self.right
            and other.bottom <= self.bottom
        )

    def intersection(self, other: "IntRect") -> Optional["IntRect"]:
        left = max(self.x, other.x)
        top = max(self.y, other.y)
        right = min(self.right, other.right)
        bottom = min(self.bottom, other.bottom)
        if right <= left or bottom <= top:
            return None
        return IntRect(left, top, right - left, bottom - top)

    def intersects(self, other: "IntRect") -> bool:
        return self.intersection(other) is not None

    def union(self, other: "IntRect") -> "IntRect":
        if self.is_empty:
            return other
        if other.is_empty:
            return self
        left = min(self.x, other.x)
        top = min(self.y, other.y)
        right = max(self.right, other.right)
        bottom = max(self.bottom, other.bottom)
        return IntRect(left, top, right - left, bottom - top)

    def translated(self, dx: int, dy: int) -> "IntRect":
        return IntRect(self.x + dx, self.y + dy, self.w, self.h)

    def clamp_to(self, bounds: "IntRect") -> "IntRect":
        """Intersection with bounds; an empty rect at bounds' origin if disjoint."""
        inter = self.intersection(bounds)
        return inter if inter is not None else IntRect(bounds.x, bounds.y, 0, 0)

    # ---- constructors --------------------------------------------------
    @staticmethod
    def from_points(x1: float, y1: float, x2: float, y2: float) -> "IntRect":
        """Normalised rect spanning two drag points (any order), rounded to ints."""
        left, right = sorted((int(round(x1)), int(round(x2))))
        top, bottom = sorted((int(round(y1)), int(round(y2))))
        return IntRect(left, top, right - left, bottom - top)

    @staticmethod
    def from_ltrb(left: int, top: int, right: int, bottom: int) -> "IntRect":
        return IntRect(int(left), int(top), int(right) - int(left), int(bottom) - int(top))

    # ---- (de)serialisation --------------------------------------------
    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.w, self.h)

    def to_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @staticmethod
    def from_dict(d: Any) -> "IntRect":
        if isinstance(d, IntRect):
            return d
        if isinstance(d, (list, tuple)) and len(d) == 4:
            return IntRect(int(d[0]), int(d[1]), int(d[2]), int(d[3]))
        if isinstance(d, dict):
            return IntRect(
                int(d.get("x", 0)), int(d.get("y", 0)), int(d.get("w", 0)), int(d.get("h", 0))
            )
        return IntRect()


# ---- DPI / scale helpers ------------------------------------------------
def scale_factor_from_dpi(dpi: int | float) -> float:
    """96 -> 1.0, 120 -> 1.25, 144 -> 1.5. A non-positive dpi is treated as 96."""
    return (dpi / BASE_DPI) if dpi and dpi > 0 else 1.0


def scale_percent_from_dpi(dpi: int | float) -> int:
    """96 -> 100, 120 -> 125, 144 -> 150, 168 -> 175, 192 -> 200."""
    return int(round(scale_factor_from_dpi(dpi) * 100))


def dpi_from_scale_percent(percent: int | float) -> int:
    return int(round(BASE_DPI * percent / 100.0))


def to_logical(physical: int | float, scale: float) -> int:
    """Physical pixels -> logical (DIP) pixels, rounded to the nearest int."""
    return int(round(physical / scale)) if scale and scale > 0 else int(round(physical))


def to_physical(logical: int | float, scale: float) -> int:
    """Logical (DIP) pixels -> physical pixels, rounded to the nearest int."""
    return int(round(logical * scale)) if scale and scale > 0 else int(round(logical))


# ---- misc ----------------------------------------------------------------
def clamp(value: float, lo: float, hi: float) -> float:
    return lo if value < lo else hi if value > hi else value


def distance(x1: float, y1: float, x2: float, y2: float) -> float:
    return math.hypot(x2 - x1, y2 - y1)
