"""grab.py: crop maths on synthetic images, plus ONE real in-memory desktop grab (never saved)."""
from __future__ import annotations

import time

import pytest
from PySide6.QtGui import QColor, QImage

from uireport.capture import grab, monitors
from uireport.geometry import IntRect


def coded_image(w, h):
    img = QImage(w, h, QImage.Format.Format_RGB32)
    for y in range(h):
        for x in range(w):
            img.setPixel(x, y, QColor(x % 256, y % 256, ((x // 256) + (y // 256) * 8) % 256).rgb())
    return img


def test_crop_copies_exact_pixels_never_scaled(qapp):
    img = coded_image(300, 200)
    out = grab.crop_image(img, IntRect(0, 0, 300, 200), IntRect(40, 30, 100, 60))
    assert (out.width(), out.height()) == (100, 60)
    assert out.devicePixelRatio() == 1.0
    for (x, y) in ((0, 0), (99, 59), (50, 20)):
        assert out.pixel(x, y) == img.pixel(40 + x, 30 + y)


def test_crop_uses_the_virtual_screen_origin(qapp):
    img = coded_image(400, 100)
    origin = IntRect(-200, -50, 400, 100)  # a monitor left of / above the primary
    out = grab.crop_image(img, origin, IntRect(-150, -30, 60, 40))
    assert (out.width(), out.height()) == (60, 40)
    assert out.pixel(0, 0) == img.pixel(50, 20)
    assert out.pixel(59, 39) == img.pixel(109, 59)


def test_crop_clamps_to_the_frozen_image(qapp):
    img = coded_image(100, 100)
    out = grab.crop_image(img, IntRect(0, 0, 100, 100), IntRect(80, 80, 100, 100))
    assert (out.width(), out.height()) == (20, 20) and out.pixel(0, 0) == img.pixel(80, 80)
    out = grab.crop_image(img, IntRect(0, 0, 100, 100), IntRect(-30, -10, 50, 40))
    assert (out.width(), out.height()) == (20, 30) and out.pixel(0, 0) == img.pixel(0, 0)


def test_crop_outside_raises(qapp):
    with pytest.raises(ValueError):
        grab.crop_image(coded_image(10, 10), IntRect(0, 0, 10, 10), IntRect(50, 50, 5, 5))


def test_crop_is_detached_from_the_frozen_image(qapp):
    img = coded_image(50, 50)
    out = grab.crop_image(img, IntRect(0, 0, 50, 50), IntRect(0, 0, 10, 10))
    before = out.pixel(3, 3)
    img.fill(QColor("#000000"))
    assert out.pixel(3, 3) == before


def test_crop_ignores_a_high_dpr_on_the_source(qapp):
    img = coded_image(100, 100)
    img.setDevicePixelRatio(2.0)
    out = grab.crop_image(img, IntRect(0, 0, 100, 100), IntRect(10, 10, 20, 20))
    assert (out.width(), out.height()) == (20, 20) and out.devicePixelRatio() == 1.0


def test_real_grab_matches_the_virtual_screen_and_is_self_owned(qapp):
    """One real grab, in memory only, assert on metadata (never on content, never saved)."""
    vs = monitors.virtual_screen_rect()
    t0 = time.perf_counter()
    img, origin = grab.grab_virtual_screen()
    dt = time.perf_counter() - t0
    assert origin == vs
    assert (img.width(), img.height()) == (vs.w, vs.h)
    assert img.devicePixelRatio() == 1.0
    assert not img.isNull() and img.format() == QImage.Format.Format_RGB32
    assert dt < 1.5  # the lead measured ~90 ms for 5120x1440 on the naive path
    # self-owned: the buffer stays valid after another grab and after touching pixels
    img.setPixel(0, 0, QColor("#010203").rgb())
    assert img.pixel(0, 0) == QColor("#010203").rgb()
    img2, _ = grab.grab_virtual_screen()
    assert img.pixel(0, 0) == QColor("#010203").rgb() and img2.size() == img.size()
    # a crop of the real grab has exactly the requested size
    c = grab.crop_image(img, origin, IntRect(origin.x + 5, origin.y + 5, 64, 48))
    assert (c.width(), c.height()) == (64, 48)


def test_real_monitor_enumeration_metadata_only(qapp):
    mons = monitors.enumerate_monitors()
    assert mons and [m.index for m in mons] == list(range(1, len(mons) + 1))
    assert sum(1 for m in mons if m.is_primary) == 1
    names = [m.name for m in mons]
    assert names == sorted(names, key=monitors.natural_key)
    union = IntRect()
    for m in mons:
        assert m.rect.w > 0 and m.rect.h > 0 and m.dpi >= 96 and m.name.startswith("\\\\.\\DISPLAY")
        union = union.union(m.rect)
    assert union == monitors.virtual_screen_rect()
    # every monitor maps to a QScreen (offscreen: the primary fallback)
    for m in mons:
        assert monitors.find_qscreen(m) is not None
