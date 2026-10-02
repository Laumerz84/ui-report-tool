"""Pure coordinate / DPI math of the capture package (synthetic monitors, no Win32, no windows)."""
from __future__ import annotations

import pytest

from uireport.capture import monitors as mon_mod
from uireport.capture.selector import (
    clamp_point_to_monitor,
    drag_rect,
    is_click,
    physical_rect_to_widget,
    physical_to_widget_point,
    readout_text,
    widget_point_to_physical,
    window_selection_rect,
)
from uireport.geometry import IntRect, dpi_from_scale_percent
from uireport.models import MonitorMeta, monitor_at_point


def M(index: int, x: int, y: int, w: int, h: int, pct: int = 100, primary: bool = False) -> MonitorMeta:
    return MonitorMeta(
        index=index,
        name=rf"\\.\DISPLAY{index}",
        is_primary=primary,
        rect=IntRect(x, y, w, h),
        dpi=dpi_from_scale_percent(pct),
    )


def logical_size(m: MonitorMeta) -> tuple[int, int]:
    """What Qt reports as the widget size on this monitor: physical / scale, rounded."""
    return (round(m.rect.w / m.scale_factor), round(m.rect.h / m.scale_factor))


LAYOUTS = {
    "100%": [M(1, 0, 0, 1920, 1080, 100, True)],
    "125%": [M(1, 0, 0, 1920, 1080, 125, True)],
    "150%": [M(1, 0, 0, 3840, 2160, 150, True)],
    "200%": [M(1, 0, 0, 3840, 2160, 200, True)],
    "left-of-primary (negative x)": [M(1, -2560, -180, 2560, 1440, 125), M(2, 0, 0, 1920, 1080, 100, True)],
    "above-primary (negative y)": [M(1, 0, 0, 1920, 1080, 100, True), M(2, 0, -1440, 2560, 1440, 100)],
    "mixed DPI side by side": [M(1, 0, 0, 3840, 2160, 150, True), M(2, 3840, 0, 1920, 1080, 100)],
    "ultrawide 100%": [M(1, 0, 0, 5120, 1440, 100, True)],
    "odd 1366x768 @125%": [M(1, 0, 0, 1366, 768, 125, True)],
}


@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_widget_corners_map_to_monitor_corners(layout):
    for m in LAYOUTS[layout]:
        w, h = logical_size(m)
        assert widget_point_to_physical(0, 0, w, h, m) == (m.rect.x, m.rect.y)
        # the far corner of the widget is the far edge of the monitor, even when Qt rounded w/h
        fx, fy = widget_point_to_physical(w, h, w, h, m)
        assert fx == pytest.approx(m.rect.right)
        assert fy == pytest.approx(m.rect.bottom)


@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_centre_and_roundtrip(layout):
    for m in LAYOUTS[layout]:
        w, h = logical_size(m)
        cx, cy = widget_point_to_physical(w / 2, h / 2, w, h, m)
        assert cx == pytest.approx(m.rect.x + m.rect.w / 2)
        assert cy == pytest.approx(m.rect.y + m.rect.h / 2)
        for wx, wy in ((0.0, 0.0), (17.25, 40.5), (w / 3, h / 7), (w - 1, h - 1)):
            px, py = widget_point_to_physical(wx, wy, w, h, m)
            bx, by = physical_to_widget_point(px, py, w, h, m)
            assert (bx, by) == pytest.approx((wx, wy))


def test_scale_factor_is_applied_per_monitor():
    a, b = LAYOUTS["mixed DPI side by side"]
    wa, ha = logical_size(a)  # 2560 x 1440 logical
    wb, hb = logical_size(b)  # 1920 x 1080 logical
    assert (wa, ha) == (2560, 1440) and (wb, hb) == (1920, 1080)
    # same logical offset means 1.5 physical px per logical px on A, 1.0 on B
    assert widget_point_to_physical(100, 100, wa, ha, a) == (150, 150)
    assert widget_point_to_physical(100, 100, wb, hb, b) == (3840 + 100, 100)


def test_negative_origin_monitors():
    left, primary = LAYOUTS["left-of-primary (negative x)"]
    w, h = logical_size(left)  # 2048 x 1152
    assert (w, h) == (2048, 1152)
    x, y = widget_point_to_physical(0, 0, w, h, left)
    assert (x, y) == (-2560, -180)
    x, y = widget_point_to_physical(w - 1, h - 1, w, h, left)
    assert x == pytest.approx(-2560 + (w - 1) * 1.25) and y == pytest.approx(-180 + (h - 1) * 1.25)
    assert monitor_at_point(LAYOUTS["left-of-primary (negative x)"], x, y) is left
    above = LAYOUTS["above-primary (negative y)"][1]
    assert widget_point_to_physical(10, 10, 2560, 1440, above) == (10, -1440 + 10)


def test_physical_rect_to_widget_inverse():
    m = M(1, 0, 0, 3840, 2160, 150, True)
    r = physical_rect_to_widget(IntRect(150, 300, 300, 150), 2560, 1440, m)
    assert (r.x(), r.y(), r.width(), r.height()) == pytest.approx((100, 200, 200, 100))


# ---- drag clamping ----------------------------------------------------------------------------------
def test_drag_stays_on_the_start_monitor():
    a, b = LAYOUTS["mixed DPI side by side"]
    # start on B, drag far left into A and beyond the top: clamped to B's rectangle
    rect = drag_rect((3840 + 500, 300), (-500, -900), b)
    assert rect == IntRect(3840, 0, 500, 300)
    assert b.rect.contains_rect(rect)
    # start on A, drag right into B
    rect = drag_rect((3000, 2000), (5000, 3000), a)
    assert rect == IntRect(3000, 2000, 840, 160)
    assert a.rect.contains_rect(rect)


def test_drag_reverse_direction_normalised():
    m = LAYOUTS["100%"][0]
    assert drag_rect((300, 200), (100, 50), m) == IntRect(100, 50, 200, 150)


def test_far_edge_snap_covers_the_full_monitor():
    m = LAYOUTS["100%"][0]
    # the cursor on the LAST pixel (1919, 1079) must still be able to select everything
    assert drag_rect((0, 0), (1919, 1079), m) == IntRect(0, 0, 1920, 1080)
    assert clamp_point_to_monitor(-3, -3, m) == (0, 0)
    assert clamp_point_to_monitor(5000, 5000, m) == (1920, 1080)
    assert clamp_point_to_monitor(960.0, 540.0, m) == (960.0, 540.0)


def test_drag_rect_degenerate_is_empty():
    m = LAYOUTS["100%"][0]
    assert drag_rect((100, 100), (100, 400), m).is_empty


# ---- click vs drag ------------------------------------------------------------------------------------
def test_click_threshold_is_four_pixels():
    assert is_click((100, 100), (100, 100))
    assert is_click((100, 100), (103, 102))
    assert is_click((100, 100), (97.5, 103.9))
    assert not is_click((100, 100), (104, 100))
    assert not is_click((100, 100), (100, 96))
    assert not is_click((100, 100), (110, 110))


# ---- window hover rect -------------------------------------------------------------------------------------
def test_window_rect_is_clamped_to_the_monitor_under_the_cursor():
    a, b = LAYOUTS["mixed DPI side by side"]
    win = IntRect(3000, 100, 1600, 900)  # straddles A and B
    assert window_selection_rect(win, a) == IntRect(3000, 100, 840, 900)
    assert window_selection_rect(win, b) == IntRect(3840, 100, 760, 900)
    assert window_selection_rect(IntRect(-500, -500, 100, 100), a) is None


def test_readout_text():
    assert readout_text(IntRect(0, 0, 640, 300)) == "640 x 300 px"


# ---- monitor list building + QScreen matching ---------------------------------------------------------------
def test_monitors_sorted_naturally_with_one_based_index():
    raw = [
        mon_mod.RawMonitor(r"\\.\DISPLAY10", IntRect(0, 0, 10, 10), 96, False),
        mon_mod.RawMonitor(r"\\.\DISPLAY2", IntRect(10, 0, 10, 10), 120, False),
        mon_mod.RawMonitor(r"\\.\DISPLAY1", IntRect(20, 0, 10, 10), 0, True),
    ]
    out = mon_mod.build_monitor_list(raw)
    assert [m.name for m in out] == [r"\\.\DISPLAY1", r"\\.\DISPLAY2", r"\\.\DISPLAY10"]
    assert [m.index for m in out] == [1, 2, 3]
    assert out[0].dpi == 96  # a bad dpi of 0 falls back to 96
    assert out[1].scale_percent == 125


class StubScreen:
    """Qt 6 convention: topLeft() is the native physical origin, size() is logical."""

    def __init__(self, x, y, lw, lh, dpr, name="screen"):
        from PySide6.QtCore import QRect

        self._g = QRect(x, y, lw, lh)
        self._dpr = dpr
        self._name = name

    def geometry(self):
        return self._g

    def devicePixelRatio(self):
        return self._dpr

    def name(self):
        return self._name


def test_match_screen_by_geometry_not_by_name():
    mons = LAYOUTS["mixed DPI side by side"]
    s_a = StubScreen(0, 0, 2560, 1440, 1.5, name="49C1R")
    s_b = StubScreen(3840, 0, 1920, 1080, 1.0, name="DELL")
    screens = [s_b, s_a]  # deliberately not in monitor order
    assert mon_mod.match_screen(mons[0].rect, screens) is s_a
    assert mon_mod.match_screen(mons[1].rect, screens) is s_b
    assert mon_mod.screen_physical_rect(s_a) == IntRect(0, 0, 3840, 2160)


def test_match_screen_negative_origin_and_rounding_slack():
    left, primary = LAYOUTS["left-of-primary (negative x)"]
    s_left = StubScreen(-2560, -180, 2048, 1152, 1.25)
    s_primary = StubScreen(0, 0, 1920, 1080, 1.0)
    assert mon_mod.match_screen(left.rect, [s_primary, s_left]) is s_left
    odd = LAYOUTS["odd 1366x768 @125%"][0]
    s_odd = StubScreen(0, 0, 1093, 614, 1.25)  # Qt rounded the logical size: 1093*1.25 = 1366.25
    assert mon_mod.match_screen(odd.rect, [s_odd]) is s_odd


def test_match_screen_falls_back_to_closest_topleft_then_primary():
    mons = LAYOUTS["mixed DPI side by side"]
    a_screen = StubScreen(0, 0, 1000, 1000, 1.0)  # wrong size but same top-left
    far = StubScreen(9000, 9000, 1000, 1000, 1.0)
    assert mon_mod.match_screen(mons[0].rect, [far, a_screen]) is a_screen
    assert mon_mod.match_screen(mons[0].rect, [far], primary="PRIMARY") == "PRIMARY"
    assert mon_mod.match_screen(mons[0].rect, [], primary="PRIMARY") == "PRIMARY"
