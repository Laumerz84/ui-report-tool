"""Window placement maths, monitor -> screen matching (multi-monitor / 150% / negative origin via
stand-in screens) and the foreground-promotion sequence (with a fake Win32 layer). No real windows."""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from uireport.editor import placement
from uireport.editor.editor_window import EditorWindow
from uireport.geometry import IntRect, dpi_from_scale_percent
from uireport.models import MonitorMeta, Session


@dataclass
class _Rect:
    x_: int
    y_: int
    w_: int
    h_: int

    def x(self): return self.x_
    def y(self): return self.y_
    def width(self): return self.w_
    def height(self): return self.h_


class FakeScreen:
    """Just enough of QScreen: native top-left, LOGICAL size, dpr (CONTRACT.md section 4.5)."""

    def __init__(self, name, x, y, phys_w, phys_h, scale_percent):
        self._name = name
        self._dpr = scale_percent / 100.0
        self._geo = _Rect(x, y, round(phys_w / self._dpr), round(phys_h / self._dpr))

    def name(self): return self._name
    def geometry(self): return self._geo
    def devicePixelRatio(self): return self._dpr
    def availableGeometry(self):
        g = self._geo
        return _Rect(g.x_, g.y_, g.w_, g.h_ - 40)


def _mon(index, name, x, y, w, h, pct, label=""):
    return MonitorMeta(index=index, name=name, rect=IntRect(x, y, w, h), dpi=dpi_from_scale_percent(pct), label=label)


def test_find_screen_matches_by_native_geometry_on_a_mixed_dpi_layout():
    left = FakeScreen("49C1R", 0, 0, 5120, 1440, 100)
    right = FakeScreen("DELL U2720Q", 5120, 0, 3840, 2160, 150)  # logical 2560x1440 at native x=5120
    above = FakeScreen("Portable", -1920, -1080, 1920, 1080, 100)  # negative origin
    screens = [left, right, above]
    assert placement.screen_physical_rect(right) == (5120, 0, 3840, 2160)
    assert placement.find_screen_for_monitor(_mon(1, r"\\.\DISPLAY1", 0, 0, 5120, 1440, 100), screens) is left
    assert placement.find_screen_for_monitor(_mon(2, r"\\.\DISPLAY2", 5120, 0, 3840, 2160, 150), screens) is right
    assert placement.find_screen_for_monitor(_mon(3, r"\\.\DISPLAY3", -1920, -1080, 1920, 1080, 100), screens) is above
    # the QScreen name is the friendly name, so \\.\DISPLAYn must never be needed
    assert right.name() != r"\\.\DISPLAY2"


def test_find_screen_falls_back_to_overlap_then_name_then_none():
    a = FakeScreen("A", 0, 0, 1920, 1080, 100)
    b = FakeScreen("B", 1920, 0, 1920, 1080, 100)
    slightly_off = _mon(2, "x", 1900, 0, 1920, 1080, 100)  # not an exact match: most overlap wins
    assert placement.find_screen_for_monitor(slightly_off, [a, b]) is b
    far_away = _mon(9, "x", 9000, 9000, 100, 100, 100, label="A")
    assert placement.find_screen_for_monitor(far_away, [a, b]) is a  # matched by friendly name
    assert placement.find_screen_for_monitor(_mon(9, "x", 9000, 9000, 100, 100, 100), [a, b]) is None
    assert placement.find_screen_for_monitor(None, [a, b]) is None
    assert placement.find_screen_for_monitor(slightly_off, []) is None


def test_compact_size_is_min_of_1100x760_and_80_percent():
    assert placement.compact_size((0, 0, 2560, 1400)) == (1100, 760)
    assert placement.compact_size((0, 0, 1366, 728)) == (1092, 582)
    assert placement.compact_size((0, 0, 800, 600)) == (640, 480)


def test_place_centered_inside_the_available_area_with_negative_origin_and_scaling():
    # a 150% monitor to the left of the primary: logical 2560x1400 available at native x=-3840
    avail = (-3840, 0, 2560, 1400)
    x, y, w, h = placement.place_centered(avail, placement.compact_size(avail))
    assert (w, h) == (1100, 760)
    assert placement.rect_inside((x, y, w, h), avail)
    assert (x, y) == (-3840 + (2560 - 1100) // 2, (1400 - 760) // 2)


def test_place_centered_honours_min_size_but_never_exceeds_the_area():
    avail = (100, 50, 700, 500)
    r = placement.place_centered(avail, (400, 300), min_size=(650, 420))
    assert r[2:] == (650, 420) and placement.rect_inside(r, avail)
    r = placement.place_centered(avail, (400, 300), min_size=(5000, 5000))
    assert r == (100, 50, 700, 500)


def test_clamp_rect_into_shifts_and_shrinks():
    avail = (0, 0, 1000, 700)
    assert placement.clamp_rect_into((900, 650, 300, 200), avail) == (700, 500, 300, 200)
    assert placement.clamp_rect_into((-50, -20, 300, 200), avail) == (0, 0, 300, 200)
    assert placement.clamp_rect_into((10, 10, 3000, 2000), avail) == (0, 0, 1000, 700)
    assert placement.rect_inside((0, 0, 1000, 700), avail) and not placement.rect_inside((1, 0, 1000, 700), avail)


class FakeApi:
    SW_RESTORE, SW_SHOW = 9, 5

    def __init__(self, foreground_after=(), iconic=False, fg=77, fg_thread=5):
        self.log: list[tuple] = []
        self._iconic = iconic
        self._fg = fg
        self._fg_thread = fg_thread
        self._set_calls = 0
        self._succeed_at = foreground_after  # which set_foreground call number makes hwnd foreground

    def is_iconic(self, hwnd): return self._iconic
    def show_window(self, hwnd, cmd): self.log.append(("show", cmd))
    def foreground(self): return self._fg
    def set_foreground(self, hwnd):
        self._set_calls += 1
        self.log.append(("set_fg", self._set_calls))
        if self._set_calls in self._succeed_at:
            self._fg = hwnd
        return self._fg == hwnd
    def bring_to_top(self, hwnd): self.log.append(("top",))
    def window_thread(self, hwnd): return self._fg_thread
    def current_thread(self): return 1
    def attach_input(self, a, b, attach): self.log.append(("attach", attach)); return True
    def set_topmost(self, hwnd, topmost): self.log.append(("topmost", topmost))


def test_foreground_promotion_stops_at_the_first_step_that_works():
    api = FakeApi(foreground_after={1})
    assert placement.bring_to_front(500, api) is True
    assert [e[0] for e in api.log] == ["show", "set_fg"]
    assert api.log[0] == ("show", 5)


def test_foreground_promotion_restores_a_minimised_window():
    api = FakeApi(foreground_after={1}, iconic=True)
    placement.bring_to_front(500, api)
    assert api.log[0] == ("show", 9)


def test_foreground_promotion_attaches_input_then_toggles_topmost():
    api = FakeApi(foreground_after={2})
    assert placement.bring_to_front(500, api) is True
    kinds = [e for e in api.log]
    assert ("attach", True) in kinds and ("attach", False) in kinds  # detached again afterwards
    assert ("topmost", True) not in kinds  # second attempt worked: no topmost needed

    api = FakeApi(foreground_after={3})  # only the topmost toggle gets it to the front
    assert placement.bring_to_front(500, api) is True
    tops = [e for e in api.log if e[0] == "topmost"]
    assert tops == [("topmost", True), ("topmost", False)]  # temporary, always reverted

    api = FakeApi(foreground_after=())
    assert placement.bring_to_front(500, api) is False  # Windows refused: report it, do not crash


def test_foreground_promotion_swallows_api_errors():
    class Boom(FakeApi):
        def show_window(self, hwnd, cmd): raise RuntimeError("boom")

    assert placement.bring_to_front(1, Boom()) is False


def test_editor_sources_never_synthesise_input():
    """No keyboard / mouse injection anywhere in the editor package (foreground lock is beaten
    with legitimate window calls only)."""
    root = pathlib.Path(placement.__file__).parent
    banned = re.compile(r"keybd_event|SendInput|mouse_event|SendKeys|pyautogui|PostMessage|SendMessage|VK_MENU|SetCursorPos")
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not banned.search(text), f"{path.name} mentions synthetic input APIs"
    # and no registry / autostart / network / desktop-control imports
    forbidden_import = re.compile(r"^\s*(import|from)\s+(winreg|socket|urllib|requests|http|pyautogui|uireport\.(capture|output|app))", re.M)
    for path in root.glob("*.py"):
        assert not forbidden_import.search(path.read_text(encoding="utf-8")), path.name


def test_caption_key_filter_is_a_working_fallback(qapp):
    """If a text widget ever accepts the ShortcutOverride, the KeyPress that follows must still run
    the action (and never insert a line break)."""
    ed = EditorWindow(Session())
    hits = []
    ed.next_requested.connect(lambda: hits.append("next"))
    ed.next_delayed_requested.connect(lambda: hits.append("delayed"))
    ed.finish_requested.connect(lambda: hits.append("finish"))
    ed.caption_edit.setEnabled(True)
    ed.canvas.set_shot(None)
    ed.finish_button.setEnabled(True)  # Finish is normally disabled with no shots
    for mods, expect in (
        (Qt.KeyboardModifier.ControlModifier, "next"),
        (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier, "delayed"),
        (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier, "finish"),
        (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.KeypadModifier, "next"),
    ):
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            hits.clear()
            ev = QKeyEvent(QEvent.Type.KeyPress, key, mods)
            QApplication.sendEvent(ed.caption_edit, ev)  # straight to the widget: bypasses QShortcut
            assert hits == [expect] and ev.isAccepted()
    assert ed.caption_edit.toPlainText() == ""
    # a plain Enter is left to the text edit
    ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier, "\r")
    hits.clear()
    QApplication.sendEvent(ed.caption_edit, ev)
    assert hits == [] and ed.caption_edit.toPlainText() == "\n"
    ed.deleteLater()
