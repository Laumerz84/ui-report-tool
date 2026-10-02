"""windowinfo: hit-testing, filtering, DPI rule, URL normalisation and the URL worker's hard timeout.
Real-desktop checks only look at structure (never titles) and are read-only."""
from __future__ import annotations

import os
import time

import pytest

from uireport.capture import windowinfo as wi
from uireport.geometry import IntRect
from uireport.models import MonitorMeta


# ---- browser_kind --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,expected",
    [
        ("chrome.exe", "chrome"), ("Chrome.EXE", "chrome"), ("chrome", "chrome"),
        ("msedge.exe", "edge"), ("MSEdge.exe", "edge"),
        ("firefox.exe", "firefox"), ("Firefox.exe", "firefox"),
        ("notepad.exe", None), ("", None), ("chromedriver.exe", None), ("edge.exe", None), (None, None),
    ],
)
def test_browser_kind(name, expected):
    assert wi.browser_kind(name) == expected


# ---- hit testing ------------------------------------------------------------------------------------------
def snap(hwnd, x, y, w, h, title="t", pid=1):
    return wi.WindowSnapshot(hwnd, IntRect(x, y, w, h), title, pid)


def test_window_at_point_returns_the_topmost():
    top = snap(1, 100, 100, 200, 200)
    back = snap(2, 0, 0, 1000, 1000)
    ws = [top, back]  # z-order: topmost first
    assert wi.window_at_point(ws, 150, 150) is top
    assert wi.window_at_point(ws, 500, 500) is back
    assert wi.window_at_point(ws, 1500, 500) is None
    assert wi.window_at_point([], 1, 1) is None


def test_window_at_point_edges_are_half_open():
    w = snap(1, 10, 10, 100, 50)
    assert wi.window_at_point([w], 10, 10) is w
    assert wi.window_at_point([w], 109, 59) is w
    assert wi.window_at_point([w], 110, 30) is None
    assert wi.window_at_point([w], 50, 60) is None


def test_window_at_point_works_with_negative_coordinates():
    w = snap(1, -2560, -180, 2560, 1440)
    assert wi.window_at_point([w], -1, -1) is w
    assert wi.window_at_point([w], 0, 0) is None  # x range ends at -1


# ---- window under a selection ----------------------------------------------------------------------------------
def test_dominant_window_is_the_one_covering_the_selection_not_one_elsewhere():
    focused_elsewhere = snap(1, 2000, 0, 900, 600, "Explorer")
    terminal = snap(2, 0, 0, 1000, 800, "Terminal")
    assert wi.dominant_window([focused_elsewhere, terminal], IntRect(100, 100, 400, 300)) is terminal


def test_dominant_window_respects_z_order_where_windows_overlap():
    top = snap(1, 0, 0, 500, 500, "Top")
    back = snap(2, 0, 0, 1000, 1000, "Back")
    assert wi.dominant_window([top, back], IntRect(50, 50, 200, 200)) is top


def test_dominant_window_picks_the_window_showing_most_of_a_spanning_selection():
    small_top = snap(1, 0, 0, 100, 100, "Small")  # covers 25% of the selection
    big_back = snap(2, 0, 0, 1000, 600, "Big")  # visible in the other 75%
    assert wi.dominant_window([small_top, big_back], IntRect(0, 0, 400, 100)) is big_back


def test_dominant_window_works_with_negative_coordinates_and_tiny_selections():
    left = snap(1, -2560, -180, 2560, 1440, "Left monitor app")
    assert wi.dominant_window([left], IntRect(-300, -100, 3, 2)) is left


def test_dominant_window_is_none_when_nothing_covers_the_selection():
    assert wi.dominant_window([snap(1, 0, 0, 100, 100)], IntRect(500, 500, 50, 50)) is None
    assert wi.dominant_window([], IntRect(0, 0, 50, 50)) is None
    assert wi.dominant_window([snap(1, 0, 0, 100, 100)], IntRect(10, 10, 0, 0)) is None


# ---- filtering -------------------------------------------------------------------------------------------------
def raw(hwnd, **kw):
    base = dict(visible=True, iconic=False, cloaked=False, ex_style=0, class_name="Cls", title="Title", pid=10,
                rect=IntRect(0, 0, 100, 100))
    base.update(kw)
    return wi.RawWindow(hwnd=hwnd, **base)


def test_filter_windows_rules():
    from uireport.capture import winapi

    rows = [
        raw(1),  # ok, topmost
        raw(2, visible=False),
        raw(3, iconic=True),
        raw(4, cloaked=True),
        raw(5, title=""),
        raw(6, title="   "),
        raw(7, ex_style=winapi.WS_EX_TOOLWINDOW),
        raw(8, rect=IntRect(0, 0, 0, 50)),
        raw(9, pid=4242),  # our own process
        raw(10, ex_style=winapi.WS_EX_TRANSPARENT | winapi.WS_EX_LAYERED),  # click-through overlay
        raw(11, ex_style=winapi.WS_EX_LAYERED),  # plain layered window: fine
        raw(12, class_name="#32768", title="", ex_style=winapi.WS_EX_TOOLWINDOW),  # native popup menu: pickable
    ]
    out = wi.filter_windows(rows, exclude_pid=4242)
    assert [w.hwnd for w in out] == [1, 11, 12]  # z-order preserved
    assert out[2].title == "(menu)"
    assert [w.hwnd for w in wi.filter_windows(rows, exclude_pid=None)] == [1, 9, 11, 12]


def test_filter_windows_returns_frozen_snapshots():
    out = wi.filter_windows([raw(1, title="Hello", pid=7, rect=IntRect(5, 6, 7, 8))])
    assert out == [wi.WindowSnapshot(1, IntRect(5, 6, 7, 8), "Hello", 7)]


# ---- DPI rule -------------------------------------------------------------------------------------------------------
def test_window_dpi_rule():
    per_monitor, system, unaware = 2, 1, 0
    assert wi.choose_window_dpi(per_monitor, 144, 96) == 144  # DPI-aware: GetDpiForWindow
    assert wi.choose_window_dpi(per_monitor, 0, 120) == 120  # API returned nothing: monitor DPI
    assert wi.choose_window_dpi(unaware, 96, 144) == 144  # stretched by Windows: monitor DPI
    assert wi.choose_window_dpi(system, 96, 192) == 192
    assert wi.choose_window_dpi(unaware, 96, 0) == 96


# ---- URL normalisation --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw_value,expected",
    [
        ("https://example.com/a?b=1#c", "https://example.com/a?b=1#c"),
        ("http://intranet.corp/x", "http://intranet.corp/x"),
        ("example.com/path", "https://example.com/path"),  # Chrome hides https://
        ("www.example.co.uk", "https://www.example.co.uk"),
        ("localhost:3000/settings", "http://localhost:3000/settings"),
        ("localhost", "http://localhost"),
        ("127.0.0.1:5000", "http://127.0.0.1:5000"),
        ("192.168.1.20:8080/admin", "http://192.168.1.20:8080/admin"),
        ("[::1]:3000/x", "http://[::1]:3000/x"),
        ("myapp.localhost:4200", "http://myapp.localhost:4200"),
        ("chrome://settings/", "chrome://settings/"),
        ("about:blank", "about:blank"),
        ("file:///C:/dev/index.html", "file:///C:/dev/index.html"),
        ("  example.com  ", "https://example.com"),
        ("\u200eexample.com/x", "https://example.com/x"),
        ("", None), ("   ", None), (None, None),
        ("how to center a div", None),  # a search being typed
        ("hello", None),  # single word: a search term
        ("intranet-server/path", None),
        ("user@example.com", None),  # an e-mail address in a page field is not a URL
        ("https://user:pw@host.example/x", "https://user:pw@host.example/x"),
    ],
)
def test_normalize_browser_url(raw_value, expected):
    assert wi.normalize_browser_url(raw_value) == expected


# ---- read_browser_url: hard timeout, silent failures ----------------------------------------------------------------------
def test_read_browser_url_rejects_non_browsers_and_bad_handles():
    assert wi.read_browser_url(123, "notepad") is None
    assert wi.read_browser_url(0, "chrome") is None


def test_read_browser_url_returns_the_worker_result(monkeypatch):
    seen = {}

    def fake(hwnd, browser, deadline):
        seen["args"] = (hwnd, browser)
        return "https://example.com"

    monkeypatch.setattr(wi, "_read_url_com", fake)
    assert wi.read_browser_url(4321, "edge", 1.0) == "https://example.com"
    assert seen["args"] == (4321, "edge")


def test_read_browser_url_timeout_is_a_hard_limit(monkeypatch):
    import threading

    release = threading.Event()
    monkeypatch.setattr(wi, "_read_url_com", lambda h, b, d: (release.wait(5), "https://late.example")[1])
    t0 = time.monotonic()
    try:
        assert wi.read_browser_url(1, "chrome", 0.3) is None
        assert time.monotonic() - t0 < 1.0
    finally:
        release.set()


def test_read_browser_url_swallows_every_exception(monkeypatch):
    def boom(h, b, d):
        raise OSError("COM error -2147220991")

    monkeypatch.setattr(wi, "_read_url_com", boom)
    assert wi.read_browser_url(1, "firefox", 1.0) is None


def test_read_browser_url_on_a_bogus_window_is_silent():
    # the real COM path against a handle that does not exist: must return None, not raise
    t0 = time.monotonic()
    assert wi.read_browser_url(0x7FFFFF0, "chrome", 1.0) is None
    assert time.monotonic() - t0 < 2.5


# ---- real desktop, read-only, structure only ------------------------------------------------------------------------------------
def test_real_snapshot_has_sane_structure_and_excludes_our_process():
    own = os.getpid()
    ws = wi.snapshot_top_level_windows(own)
    assert isinstance(ws, list)
    for w in ws:
        assert isinstance(w, wi.WindowSnapshot)
        assert w.pid != own and w.pid > 0
        assert not w.rect.is_empty and w.title.strip()
        assert isinstance(w.hwnd, int) and w.hwnd != 0
    # everything else shows up when we do not exclude ourselves
    assert len(wi.snapshot_top_level_windows(None)) >= len(ws)


def test_real_foreground_hwnd_is_none_or_a_window_of_another_process():
    h = wi.foreground_hwnd(os.getpid())
    if h is not None:
        assert isinstance(h, int)
        assert wi._pid_of(h) != os.getpid()
