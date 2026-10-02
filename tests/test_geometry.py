from uireport.geometry import (
    IntRect,
    dpi_from_scale_percent,
    scale_factor_from_dpi,
    scale_percent_from_dpi,
    to_logical,
    to_physical,
)


def test_rect_basics_half_open():
    r = IntRect(10, 20, 100, 50)
    assert (r.right, r.bottom) == (110, 70)
    assert r.contains_point(10, 20) and r.contains_point(109, 69)
    assert not r.contains_point(110, 20) and not r.contains_point(10, 70)
    assert r.center == (60.0, 45.0)
    assert not r.is_empty and IntRect(0, 0, 0, 5).is_empty


def test_rect_intersection_union_clamp():
    a = IntRect(0, 0, 100, 100)
    b = IntRect(50, 50, 100, 100)
    assert a.intersection(b) == IntRect(50, 50, 50, 50)
    assert a.intersection(IntRect(100, 0, 10, 10)) is None  # touching edges do not overlap
    assert a.union(b) == IntRect(0, 0, 150, 150)
    assert IntRect(-20, -20, 60, 60).clamp_to(a) == IntRect(0, 0, 40, 40)
    assert IntRect(500, 500, 5, 5).clamp_to(a).is_empty
    assert a.contains_rect(IntRect(10, 10, 20, 20)) and not a.contains_rect(b)


def test_from_points_normalises_and_rounds():
    assert IntRect.from_points(110.4, 70.6, 10, 20) == IntRect(10, 20, 100, 51)
    assert IntRect.from_ltrb(1, 2, 11, 22) == IntRect(1, 2, 10, 20)


def test_dict_round_trip_and_tolerance():
    r = IntRect(-5, 6, 7, 8)
    assert IntRect.from_dict(r.to_dict()) == r
    assert IntRect.from_dict([1, 2, 3, 4]) == IntRect(1, 2, 3, 4)
    assert IntRect.from_dict(None) == IntRect()


def test_dpi_scale_conversions():
    assert scale_factor_from_dpi(96) == 1.0
    assert scale_factor_from_dpi(144) == 1.5
    assert scale_percent_from_dpi(120) == 125
    assert scale_percent_from_dpi(168) == 175
    assert scale_percent_from_dpi(0) == 100  # invalid -> 100%
    assert dpi_from_scale_percent(150) == 144 and dpi_from_scale_percent(100) == 96


def test_logical_physical_conversion():
    assert to_logical(1500, 1.5) == 1000
    assert to_logical(1920, 1.25) == 1536
    assert to_physical(1000, 1.5) == 1500
    assert to_logical(101, 1.5) == 67  # rounds to nearest
    assert to_logical(100, 0) == 100  # bad scale is treated as 1.0
