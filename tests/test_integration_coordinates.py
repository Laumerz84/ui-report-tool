"""Coordinates on a simulated 150 % monitor and on mixed-DPI multi-monitor layouts.

The chain under test is the real one: injected mouse events on the selector overlays (sized like Qt
sizes them on a scaled monitor) -> RegionSelector -> CaptureService -> crop -> Shot. The synthetic
desktop encodes each pixel's own screen coordinates in its colour, so the pixels found in the
Shot's image prove which screen area was really cut out.
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from integration_support import (
    LAYOUTS,
    CaptureRig,
    layout_mixed,
    layout_odd_rounding,
    layout_single_150,
    logical_size,
    pixel_rgb,
    wait_until,
)
from uireport.geometry import IntRect
from uireport.models import CaptureMode
from uireport.settings import Settings

EXACT_LAYOUTS = ["single_150", "mixed_100_150", "three_125_100_200"]  # logical size is a whole number


@pytest.fixture
def rig(qapp):
    made: list[CaptureRig] = []

    def make(name_or_layout, **kw) -> CaptureRig:
        monitors = LAYOUTS[name_or_layout]() if isinstance(name_or_layout, str) else name_or_layout
        r = CaptureRig(monitors, lambda: Settings(delay_seconds=3), **kw)
        made.append(r)
        return r

    yield make
    for r in made:
        r.close()
    qapp = None  # noqa: F841


def capture(rig: CaptureRig, action) -> object:
    """Start a normal capture, let `action` play the user, return the emitted Shot."""
    got: list = []
    rig.service.captured.connect(got.append)
    assert rig.service.start(CaptureMode.NORMAL) is True
    assert rig.service.state == "selecting"  # the selector opened inside start(): no delay
    action()
    assert wait_until(lambda: got, 5), "no Shot was emitted"
    rig.service.captured.disconnect(got.append)
    assert not rig.service.is_active
    return got[0]


def check_pixels_come_from(rig: CaptureRig, shot, rect: IntRect) -> None:
    """Corners and centre of the cropped image decode to exactly the screen points they came from."""
    img = shot.image
    assert (img.width(), img.height()) == (rect.w, rect.h) == (shot.image_width, shot.image_height)
    assert img.devicePixelRatio() == 1.0  # never scaled, never a logical-size image
    for px, py in [(0, 0), (rect.w - 1, 0), (0, rect.h - 1), (rect.w - 1, rect.h - 1), (rect.w // 2, rect.h // 2), (7, 11)]:
        assert rig.desktop.rgb_matches(pixel_rgb(img, px, py), rect.x + px, rect.y + py), (px, py)


@pytest.mark.parametrize("layout", EXACT_LAYOUTS)
def test_drag_on_every_monitor_cuts_out_exactly_the_dragged_physical_pixels(rig, layout):
    r = rig(layout)
    for m in r.monitors:
        # offsets and sizes are multiples of 30 so every scale (100/125/150/200 %) divides them exactly
        want = IntRect(m.rect.x + 150, m.rect.y + 90, 300, 180)
        r.put_cursor_on(m)
        shot = capture(r, lambda: r.drag_region(want))
        assert shot.capture_rect == want, f"monitor {m.index} @{m.scale_percent}%"
        assert shot.selection == "region" and shot.capture_mode == CaptureMode.NORMAL
        assert shot.monitor.index == m.index and shot.monitor.scale_percent == m.scale_percent
        assert shot.monitor.rect == m.rect and shot.monitor.dpi == m.dpi
        check_pixels_come_from(r, shot, want)


@pytest.mark.parametrize("layout", EXACT_LAYOUTS)
def test_the_selector_overlay_is_the_logical_size_of_each_monitor(rig, layout):
    r = rig(layout)
    from uireport.models import CaptureMode as M

    assert r.service.start(M.NORMAL)
    for m in r.monitors:
        ov = r.selector.overlay_for(m)
        assert (ov.width(), ov.height()) == logical_size(m)  # 2560x1440 for a 3840x2160 monitor at 150 %
    r.esc_in_selector()
    assert wait_until(lambda: not r.service.is_active)


def test_a_150_percent_drag_in_logical_pixels_is_1_5_times_bigger_in_the_png(rig):
    r = rig("single_150")
    m = r.monitors[0]
    # what the user sees: a 200 x 120 (logical) box at logical (100, 60)
    ov_size = logical_size(m)
    assert ov_size == (2560, 1440)
    shot = capture(r, lambda: r.drag_region(IntRect(150, 90, 300, 180)))  # = logical (100,60)-(300,180)
    assert (shot.image_width, shot.image_height) == (300, 180)  # the PNG keeps every physical pixel
    assert shot.to_logical(shot.image_width) == 200.0 and shot.to_logical(shot.image_height) == 120.0
    assert shot.monitor.logical_size == (2560, 1440)


def test_mixed_dpi_two_monitor_sequence_in_one_service_instance(rig):
    """100 % primary + 150 % secondary with negative x AND y: shots alternate between them."""
    r = rig("mixed_100_150")
    primary, side = r.monitors
    plan = [
        (side, IntRect(-2880 + 30, -300 + 30, 600, 300)),  # top-left corner of the negative-origin monitor
        (primary, IntRect(0, 0, 400, 240)),  # top-left corner of the primary monitor
        (side, IntRect(-2880 + 2880 - 300, -300 + 1620 - 150, 300, 150)),  # bottom-right corner of the side monitor
        (primary, IntRect(1920 - 300, 1080 - 150, 300, 150)),  # bottom-right corner of the primary monitor
    ]
    for mon_, want in plan:
        r.put_cursor_on(mon_)
        shot = capture(r, lambda: r.drag_region(want))
        assert shot.capture_rect == want and shot.monitor.index == mon_.index
        assert shot.monitor.scale_percent == mon_.scale_percent
        check_pixels_come_from(r, shot, want)
    assert r.grab_count == 4  # one real grab per capture, nothing extra


def test_a_drag_that_leaves_the_monitor_stays_on_the_monitor_it_started_on(rig):
    r = rig("mixed_100_150")
    primary, side = r.monitors
    assert r.service.start(CaptureMode.NORMAL)
    got: list = []
    r.service.captured.connect(got.append)
    # press on the 150 % monitor, drag far to the right (over the primary) and down: clamped to the side monitor
    m, ov = r.overlay_at(-2880 + 2100, -300 + 900)
    assert m.index == side.index
    x0, y0 = r._widget_point(side, ov, -2880 + 2100, -300 + 900)
    from PySide6.QtCore import QEvent

    r._send_mouse(ov, QEvent.Type.MouseButtonPress, x0, y0, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    r._send_mouse(ov, QEvent.Type.MouseMove, x0 + 4000, y0 + 4000, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    r._send_mouse(ov, QEvent.Type.MouseButtonRelease, x0 + 4000, y0 + 4000, Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
    assert wait_until(lambda: got, 5)
    shot = got[0]
    assert shot.monitor.index == side.index and side.rect.contains_rect(shot.capture_rect)
    assert shot.capture_rect.right == side.rect.right and shot.capture_rect.bottom == side.rect.bottom
    check_pixels_come_from(r, shot, shot.capture_rect)


@pytest.mark.parametrize("layout", EXACT_LAYOUTS)
def test_f_key_captures_the_whole_monitor_under_the_cursor(rig, layout):
    r = rig(layout)
    for m in r.monitors:
        r.put_cursor_on(m)
        shot = capture(r, lambda: r.press_key(Qt.Key.Key_F))
        assert shot.selection == "monitor" and shot.capture_rect == m.rect
        assert (shot.image_width, shot.image_height) == (m.rect.w, m.rect.h)
        check_pixels_come_from(r, shot, m.rect)


@pytest.mark.parametrize("layout", EXACT_LAYOUTS)
def test_clicking_a_window_captures_its_frame_and_records_its_logical_size(rig, layout):
    r = rig(layout)
    for m in r.monitors:
        win = next(w for w in r.windows if m.rect.contains_rect(w.rect))
        r.put_cursor_on(m)
        shot = capture(r, lambda: r.click_at(win.rect.x + 200, win.rect.y + 150))
        assert shot.selection == "window" and shot.capture_rect == win.rect
        assert shot.window.title == win.title and shot.window.rect == win.rect
        assert shot.window.dpi == m.dpi and shot.window.scale_percent == m.scale_percent
        lw, lh = shot.window.size_logical  # what a developer sees for this window
        assert (lw, lh) == (round(win.rect.w / m.scale_factor), round(win.rect.h / m.scale_factor))
        check_pixels_come_from(r, shot, win.rect)


def test_window_larger_than_its_monitor_is_clamped_to_that_monitor(rig):
    r = rig("mixed_100_150")
    primary, side = r.monitors
    # a window spanning both monitors (as when it is dragged half over): a click on the 150 % side captures
    # only the part that lies on that monitor
    from uireport.capture.windowinfo import WindowSnapshot

    span = WindowSnapshot(hwnd=0x9999, rect=IntRect(-1500, 100, 3000, 700), title="Spans both", pid=4321)
    r.windows.insert(0, span)
    r.put_cursor_on(side)
    shot = capture(r, lambda: r.click_at(-800, 400))
    assert shot.selection == "window" and shot.monitor.index == side.index
    assert shot.capture_rect == IntRect(-1500, 100, 1500, 700)  # x from -1500 up to the monitor's right edge (0)
    assert side.rect.contains_rect(shot.capture_rect)
    check_pixels_come_from(r, shot, shot.capture_rect)


def test_odd_rounded_logical_size_stays_within_one_pixel_and_full_screen_is_exact(rig):
    """1366x768 at 125 %: Qt rounds the overlay to 1093x614, so the ratio is not exactly 1.25."""
    r = rig(layout_odd_rounding())
    m = r.monitors[0]
    assert logical_size(m) == (1093, 614)
    # drag from logical (100,100) to (600,400): physical is (125, 125) .. (750, 500) give or take one pixel
    shot = capture(r, lambda: r.drag_region(IntRect(125, 125, 625, 375)))
    rect = shot.capture_rect
    assert abs(rect.x - 125) <= 1 and abs(rect.y - 125) <= 1
    assert abs(rect.w - 625) <= 1 and abs(rect.h - 375) <= 1
    check_pixels_come_from(r, shot, rect)
    # dragging to (or past) the far corner of the overlay snaps to the monitor edge: nothing is cut off
    shot = capture(r, lambda: r.drag_region(IntRect(0, 0, m.rect.w, m.rect.h)))
    assert shot.capture_rect == m.rect
    check_pixels_come_from(r, shot, m.rect)
    # whole monitor by key
    shot = capture(r, lambda: r.press_key(Qt.Key.Key_F))
    assert shot.capture_rect == m.rect


def test_shot_metadata_of_a_150_percent_capture(rig):
    r = rig(layout_single_150())
    shot = capture(r, lambda: r.drag_region(IntRect(300, 180, 450, 240)))
    d = shot.to_dict()
    assert d["monitor"]["scale_percent"] == 150 and d["monitor"]["dpi"] == 144
    assert d["monitor"]["width_px"] == 3840 and d["monitor"]["height_px"] == 2160
    assert d["capture_rect"] == {"x": 300, "y": 180, "w": 450, "h": 240}
    assert d["image_size"] == {"width_px": 450, "height_px": 240}
    assert d["window"]["dpi"] == 144 and d["window"]["scale_percent"] == 150
    assert d["window"]["size_px"] == {"width": 1200, "height": 700}
    assert d["window"]["size_logical"] == {"width": 800, "height": 467}
    assert shot.timestamp and shot.capture_mode == CaptureMode.NORMAL
