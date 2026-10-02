"""End-to-end: one long session through the REAL application objects on a SYNTHETIC desktop.

Real: AppController, CaptureService, RegionSelector, CountdownOverlay (offscreen overlays driven by
injected mouse / key events), EditorWindow + AnnotationCanvas + SessionPanel (driven with QTest on our
own widgets), write_session, the clipboard, Toast and TrayIcon.
Fake: the screen (a desktop whose pixels encode their own screen coordinates), the OS-wide hotkeys,
the registry and "open folder". No screen grab, no real hotkey, no desktop input, and everything is
written under .test-output/integration.

The journey: 14 captures on a mixed-DPI two-monitor layout (100 % primary + a 150 % monitor with a
negative origin), started every way a user can start one; an Esc in the selector and an Esc during
the countdown in the middle of the session; reorder (keyboard + panel), delete (x button + Delete
key), re-edit through a click on a filmstrip card; Finish with Ctrl+Shift+Enter. Then the files,
report.json, report.md, the clipboard prompt, the toast and the fresh session are checked against
what the "user" did.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Optional

import pytest
from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from integration_support import (
    Stack,
    as_rgb32,
    blank_rect,
    diff_bbox,
    fraction_differing,
    layout_mixed,
    pixel_rgb,
    same_pixels,
    settle,
    wait_until,
)
from test_editor_support import LEFT, NOMOD, click, drag
from uireport.editor.canvas import Tool
from uireport.geometry import IntRect
from uireport.hotkeyspec import NAME_CAPTURE, NAME_DELAYED
from uireport.models import DEFAULT_ROLE, CaptureMode, Role

CTRL = Qt.KeyboardModifier.ControlModifier
ALT = Qt.KeyboardModifier.AltModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier

OUT_ROOT = Path(__file__).resolve().parent.parent / ".test-output" / "integration"

GOAL = "Fix the clipped Save button on the settings page"  # no trailing punctuation on purpose
PROJECT = r"C:\dev\demo-app"
FRAMEWORK = "React + Tailwind"
EXPECTED_TEXT = "The full Save label is visible at every scale"
ACTUAL_TEXT = "The label is cut off at 150% scale"

W, H = 240, 160  # every capture is 240 x 160 physical pixels


# ---------------------------------------------------------------------------------------------
# the "user": what the person does with the mouse and keyboard
# ---------------------------------------------------------------------------------------------
@dataclass
class Expect:
    """What the user did to one shot, i.e. what the output must reflect."""

    n: int  # capture number (1-based, in the order they were taken)
    rect: IntRect
    monitor_index: int
    mode: CaptureMode
    kind: str  # "A": two pins + redaction, "B": two pins + rectangle + arrow + ruler
    role: Role = Role.PROBLEM
    caption: str = ""
    pins: list[tuple[int, int, str]] = field(default_factory=list)
    redact: Optional[IntRect] = None
    box: Optional[IntRect] = None
    arrow: Optional[tuple[int, int, int, int]] = None
    ruler: Optional[tuple[int, int, int, int]] = None


def px_point(canvas, ix: int, iy: int) -> tuple[int, int]:
    """An integer widget point inside image pixel (ix, iy) at 1:1 zoom (canvas.image_to_widget of the
    pixel's top-left corner, rounded UP so the point is inside the pixel whatever the fractional offset)."""
    wx, wy = canvas.image_to_widget(float(ix), float(iy))
    return (math.ceil(wx - 1e-6), math.ceil(wy - 1e-6))


class Journey:
    def __init__(self, stack: Stack) -> None:
        self.s = stack
        self.rig = stack.rig
        self.exp: dict[str, Expect] = {}
        self.order: list[str] = []  # the shot ids in the order the user arranged them
        self.cancels: list[dict[str, Any]] = []
        self.starts: list[dict[str, Any]] = []
        self.deleted: list[Expect] = []
        self.notes: list[str] = []

    # ---- small helpers ------------------------------------------------------------------------------------
    @property
    def ed(self):
        return self.s.editor

    @property
    def session(self):
        return self.s.session

    def monitor(self, index: int):
        return next(m for m in self.rig.monitors if m.index == index)

    def region_for(self, n: int, mon) -> IntRect:
        # offsets are multiples of 30 so every scale divides them exactly
        return IntRect(mon.rect.x + 150 + 30 * (n % 6), mon.rect.y + 90 + 30 * (n % 5), W, H)

    def press_shortcut(self, mods) -> None:
        """Ctrl+Enter & co. go to whatever widget has the keyboard focus, like a real key press."""
        target = QApplication.focusWidget() or self.ed.caption_edit
        QTest.keyClick(target, Qt.Key.Key_Return, mods)

    # ---- starting a capture -------------------------------------------------------------------------------------
    def start(self, via: str, delayed: bool) -> None:
        s = self.s
        if via == "hotkey":
            s.hotkeys.press(NAME_DELAYED if delayed else NAME_CAPTURE)  # queued, like the hotkey thread
            QCoreApplication.processEvents()  # only the queued hotkey signal needs to be delivered
        elif via == "shortcut":
            self.press_shortcut((CTRL | ALT) if delayed else CTRL)
        elif via == "button":
            (self.ed.next_delayed_button if delayed else self.ed.next_button).click()
        elif via == "tray_menu":  # right-click the tray icon, choose "Capture" / "Delayed capture"
            (s.tray.action_delayed if delayed else s.tray.action_capture).trigger()
        elif via == "tray_icon":  # left-click the tray icon
            assert not delayed
            s.tray.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
        else:  # pragma: no cover
            raise AssertionError(via)

    def _await_selector(self, via: str, delayed: bool, editor_was_visible: bool, t0: float) -> None:
        s, rig = self.s, self.rig
        rec: dict[str, Any] = {"via": via, "delayed": delayed, "editor_was_visible": editor_was_visible}
        if editor_was_visible:
            # hidden at once (so it cannot be in the grab); the capture itself starts after the short settle.
            # (the tray MENU item waits a moment for its popup to close before it even asks for a capture)
            if via == "tray_menu" and not delayed:
                assert wait_until(lambda: not self.ed.isVisible(), 2)
            else:
                assert not self.ed.isVisible(), "the editor must be hidden right away"
        if delayed:
            assert wait_until(lambda: s.service.state == "countdown", 5), f"no countdown ({s.service.state})"
            rec["countdown_seconds"] = rig.countdowns[-1]._seconds
            assert s.service._selector is None and rig.grab_count == self._grabs_before, "nothing may be frozen while counting down"
            assert wait_until(lambda: s.service.state == "selecting", 10), f"countdown never finished ({s.service.state})"
        elif not editor_was_visible:
            # no editor: the selector opens INSIDE the start call chain, with no timer and no delay
            rec["selecting_without_waiting"] = s.service.state == "selecting"
            assert s.service.state == "selecting", "a normal capture must open the selector immediately"
        else:
            assert wait_until(lambda: s.service.state == "selecting", 5)
        rec["seconds_to_selector"] = round(time.monotonic() - t0, 3)
        self.starts.append(rec)

    # ---- taking a shot -----------------------------------------------------------------------------------------------
    def take(self, n: int, *, via: str, delayed: bool, mon_index: int, kind: str, role: Role, caption: str):
        s, rig = self.s, self.rig
        mon = self.monitor(mon_index)
        region = self.region_for(n, mon)
        rig.put_cursor_on(mon)
        rig.foreground = next(w for w in rig.windows if mon.rect.contains_rect(w.rect))
        before = len(self.session.shots)
        editor_was_visible = self.ed is not None and self.ed.isVisible()
        self._grabs_before = rig.grab_count
        t0 = time.monotonic()
        self.start(via, delayed)
        self._await_selector(via, delayed, editor_was_visible, t0)
        rig.drag_region(region)
        assert wait_until(lambda: len(self.session.shots) == before + 1 and self.ed.isVisible(), 5), \
            f"capture {n} never reached the editor"
        shot = self.session.shots[-1]
        exp = Expect(n=n, rect=region, monitor_index=mon_index,
                     mode=CaptureMode.DELAYED if delayed else CaptureMode.NORMAL, kind=kind, role=role, caption=caption)
        self.exp[shot.id] = exp
        self.order.append(shot.id)

        # what the person sees right after the capture
        assert shot.capture_rect == region, (n, shot.capture_rect, region)
        assert shot.capture_mode == exp.mode and shot.monitor.index == mon_index
        assert self.ed.current_shot_id() == shot.id
        assert QApplication.focusWidget() is self.ed.caption_edit, "the caption box must be focused"
        assert s.tray.action_show_session.text() == f"Show session ({len(self.session.shots)})"
        assert rig.grab_count == self._grabs_before + 1, "exactly one grab per capture"

        # caption, role, annotations
        QTest.keyClicks(self.ed.caption_edit, caption)
        if role != DEFAULT_ROLE:
            self.ed.role_control.buttons[role].click()
        self.annotate(shot, exp)
        assert shot.caption == caption and shot.role == role
        return shot

    # ---- annotating (mouse + keyboard on the real canvas) -------------------------------------------------
    def prepare_canvas(self) -> Any:
        c = self.ed.canvas
        c.zoom_actual()  # 1 image pixel per screen pixel: the mode a developer measuring pixels would use
        settle(10)
        assert abs(c.zoom() * c.device_pixel_ratio() - 1.0) < 1e-6
        return c

    def add_pin(self, exp: Expect, ix: int, iy: int, note: str) -> None:
        c = self.prepare_canvas()
        c.set_tool(Tool.PIN)
        before = len(c.shot().pins)
        click(c, *px_point(c, ix, iy))
        assert c.pending_edit_active(), "the note editor opens right after placing a pin"
        QTest.keyClicks(c._note, note)
        QTest.keyClick(c._note, Qt.Key.Key_Return)
        pins = c.shot().pins
        assert len(pins) == before + 1 and (pins[-1].x, pins[-1].y) == (ix, iy), (pins[-1].x, pins[-1].y, ix, iy)
        assert pins[-1].note == note and pins[-1].n == before + 1
        exp.pins.append((ix, iy, note))
        c.set_tool(Tool.SELECT)

    def drag_box(self, tool: Tool, x0: int, y0: int, x1: int, y1: int) -> IntRect:
        """Rectangle-like drag (rect / redact use the nearest grid line, so they may differ by one pixel)."""
        c = self.prepare_canvas()
        c.set_tool(tool)
        n_before = len(c.shot().annotations)
        drag(c, px_point(c, x0, y0), px_point(c, x1, y1))
        assert len(c.shot().annotations) == n_before + 1
        a = c.shot().annotations[-1]
        got = IntRect(a.x, a.y, a.w, a.h)
        assert abs(got.x - x0) <= 1 and abs(got.y - y0) <= 1 and abs(got.w - (x1 - x0)) <= 1 and abs(got.h - (y1 - y0)) <= 1, \
            (got, (x0, y0, x1, y1))
        c.set_tool(Tool.SELECT)
        return got

    def drag_line(self, tool: Tool, x0: int, y0: int, x1: int, y1: int) -> tuple[int, int, int, int]:
        c = self.prepare_canvas()
        c.set_tool(tool)
        n_before = len(c.shot().annotations)
        drag(c, px_point(c, x0, y0), px_point(c, x1, y1))
        assert len(c.shot().annotations) == n_before + 1
        a = c.shot().annotations[-1]
        assert (a.x1, a.y1, a.x2, a.y2) == (x0, y0, x1, y1)  # arrow / ruler ends are exact pixels
        c.set_tool(Tool.SELECT)
        return (x0, y0, x1, y1)

    def annotate(self, shot, exp: Expect) -> None:
        self.add_pin(exp, 40, 40, f"#{exp.n} first pin")
        self.add_pin(exp, 200, 55, f"#{exp.n} second pin")
        if exp.kind == "A":
            exp.redact = self.drag_box(Tool.REDACT, 120, 100, 210, 140)
        else:
            exp.box = self.drag_box(Tool.RECT, 20, 100, 90, 140)
            exp.arrow = self.drag_line(Tool.ARROW, 120, 110, 210, 140)
            exp.ruler = self.drag_line(Tool.RULER, 20, 150, 120, 150)

    # ---- Esc in the middle of the session ------------------------------------------------------------------
    def cancel_with_esc(self, *, delayed: bool) -> None:
        s, rig = self.s, self.rig
        ed = self.ed
        ids = [x.id for x in self.session.shots]
        open_id = ed.current_shot_id()
        grabs, cancelled = rig.grab_count, len(s.cancelled)
        self._grabs_before = grabs
        rec: dict[str, Any] = {"delayed": delayed}
        if delayed:
            rig.set_clock_speed(1.0)  # a real 3 second countdown: there is time to press Esc
            try:
                self.start("shortcut", delayed=True)
                assert not ed.isVisible()
                assert wait_until(lambda: s.service.state == "countdown", 3)
                rec["badge_visible"] = rig.countdowns[-1].is_visible
                rig.esc_in_countdown()
            finally:
                rig.set_clock_speed(60.0)
        else:
            self.start("shortcut", delayed=False)
            assert wait_until(lambda: s.service.state == "selecting", 5)
            rec["overlays"] = len(rig.selector.overlays)
            rig.esc_in_selector()
        assert wait_until(lambda: not s.service.is_active and ed.isVisible(), 5), "the editor must come back"
        rec.update(
            cancelled_mode=s.cancelled[-1] if len(s.cancelled) == cancelled + 1 else None,
            cancel_signals=len(s.cancelled) - cancelled,
            session_unchanged=[x.id for x in self.session.shots] == ids,
            same_shot_open=ed.current_shot_id() == open_id,
            grabs=rig.grab_count - grabs,
            idle=not s.service.is_active,
            no_overlay_left=all(not sel.overlays for sel in rig.selectors),
            tray_count=s.tray.action_show_session.text(),
            failed=list(s.failed),
        )
        if delayed:
            guard = rig.esc_guards[-1]
            rec["esc_grab_released"] = guard.released
            rec["badge_gone"] = not rig.countdowns[-1].is_visible
        self.cancels.append(rec)

    # ---- filmstrip: reorder / delete / re-edit -------------------------------------------------------------
    def click_card(self, shot_id: str) -> None:
        panel = self.ed.panel
        lst = panel._list
        item = lst.item(panel.shot_ids().index(shot_id))
        lst.scrollToItem(item)
        settle(10)
        QTest.mouseClick(lst.viewport(), LEFT, NOMOD, lst.visualItemRect(item).center())
        assert wait_until(lambda: self.ed.current_shot_id() == shot_id, 3), "clicking a thumbnail must open that shot"

    def move_shot(self, shot_id: str, new_index: int) -> None:
        assert self.ed.panel.move_shot(shot_id, new_index)  # what a drop on the strip does
        self.order.insert(new_index, self.order.pop(self.order.index(shot_id)))
        assert [x.id for x in self.session.shots] == self.order
        assert [x.index for x in self.session.shots] == list(range(1, len(self.order) + 1))

    def nudge_open_shot(self, delta: int) -> None:
        """Ctrl+Left / Ctrl+Right on the strip."""
        cur = self.ed.current_shot_id()
        i = self.order.index(cur)
        self.ed.panel.move_current(delta)
        j = max(0, min(len(self.order) - 1, i + delta))
        self.order.insert(j, self.order.pop(i))
        assert [x.id for x in self.session.shots] == self.order

    def delete_with_x(self, shot_id: str) -> None:
        self.ed.panel.request_delete(shot_id)  # the small x on a card
        self.deleted.append(self.exp.pop(shot_id))
        self.order.remove(shot_id)
        assert [x.id for x in self.session.shots] == self.order

    def delete_open_shot_with_delete_key(self, shot_id: str) -> str:
        self.click_card(shot_id)
        i = self.order.index(shot_id)
        QTest.keyClick(self.ed.panel._list, Qt.Key.Key_Delete)
        self.deleted.append(self.exp.pop(shot_id))
        self.order.remove(shot_id)
        assert [x.id for x in self.session.shots] == self.order
        neighbour = self.order[min(i, len(self.order) - 1)]
        assert self.ed.current_shot_id() == neighbour, "deleting the open shot opens its neighbour"
        return neighbour

    def re_edit(self, shot_id: str, extra_caption: str, new_role: Role, pin: tuple[int, int, str]) -> None:
        exp = self.exp[shot_id]
        self.click_card(shot_id)
        # the saved state comes back: caption, role and annotations
        assert self.ed.caption_edit.toPlainText() == exp.caption
        assert self.ed.role_control.role() == exp.role
        assert [(p.x, p.y, p.note) for p in self.ed.canvas.shot().pins] == exp.pins
        QTest.keyClicks(self.ed.caption_edit, extra_caption)
        exp.caption += extra_caption
        self.ed.role_control.buttons[new_role].click()
        exp.role = new_role
        self.add_pin(exp, *pin)
        assert self.session.get_shot(shot_id).caption == exp.caption


# ---------------------------------------------------------------------------------------------
# the journey (runs once; the tests below look at its results)
# ---------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def journey(qapp):
    folder = OUT_ROOT / "journey"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    mp = pytest.MonkeyPatch()
    stack = Stack(folder, mp, layout_mixed(), delay_seconds=3)
    j = Journey(stack)
    try:
        run_journey(j)
        yield j
    finally:
        stack.shutdown()
        mp.undo()


def run_journey(j: Journey) -> None:
    s = j.s
    T = j.take
    P, WANT, CTX, AFTER = Role.PROBLEM, Role.WANT, Role.CONTEXT, Role.AFTER

    # -- 1: nothing is open yet: the hotkey opens the selector with no delay --------------------------------
    assert s.editor is None and not s.service.is_active
    T(1, via="hotkey", delayed=False, mon_index=1, kind="A", role=P, caption="Save button text is clipped")
    ed = j.ed
    assert ed is not None and ed.isVisible() and s.tray.action_show_session.isEnabled()

    # session fields (goal, project folder, framework, expected / actual) typed into the editor
    ed.fields.set_expanded(True)
    QTest.keyClicks(ed.fields.goal_edit, GOAL)
    QTest.keyClicks(ed.fields.project_edit, PROJECT)
    ed.fields.framework_combo.setEditText(FRAMEWORK)
    QTest.keyClicks(ed.fields.expected_edit, EXPECTED_TEXT)
    QTest.keyClicks(ed.fields.actual_edit, ACTUAL_TEXT)
    assert (j.session.goal, j.session.project_path, j.session.framework_hint) == (GOAL, PROJECT, FRAMEWORK)
    ed.fields.set_expanded(False)

    # -- 2..6: every way to start the next capture, normal and delayed, on both monitors --------------------------
    T(2, via="shortcut", delayed=True, mon_index=2, kind="B", role=WANT, caption="Mockup: the Save button as designed")
    T(3, via="shortcut", delayed=False, mon_index=2, kind="A", role=CTX, caption="Whole settings page, for orientation")
    T(4, via="button", delayed=False, mon_index=1, kind="B", role=AFTER, caption="After the fix: label fits")
    T(5, via="tray_menu", delayed=False, mon_index=1, kind="A", role=P, caption="Hover state of the Save button")
    T(6, via="hotkey", delayed=True, mon_index=2, kind="B", role=P, caption="Menu open at 150 percent")

    # -- Esc in the middle of the session: in the selector, then during the countdown -------------------------
    j.cancel_with_esc(delayed=False)
    j.cancel_with_esc(delayed=True)
    assert len(j.session.shots) == 6

    # -- 7..10 -------------------------------------------------------------------------------------------------------------
    T(7, via="shortcut", delayed=False, mon_index=1, kind="B", role=P, caption="Tooltip is cut off")
    T(8, via="hotkey", delayed=False, mon_index=2, kind="A", role=WANT, caption="Reference for the dialog")
    T(9, via="tray_menu", delayed=True, mon_index=1, kind="A", role=P, caption="Dropdown while open")
    T(10, via="tray_icon", delayed=False, mon_index=2, kind="B", role=CTX, caption="Sidebar at 150 percent")
    ids = list(j.order)
    assert len(ids) == 10

    # -- reorder (drop on the strip, Ctrl+Left), re-edit (click a thumbnail), delete (x, Delete key) ------------
    j.move_shot(ids[0], 4)  # shot 1 to position 5
    j.nudge_open_shot(-1)  # the open shot (10) one step left
    j.re_edit(ids[1], " - re-edited", Role.PROBLEM, (120, 30, "added while re-editing"))
    j.delete_with_x(ids[2])  # capture 3
    j.delete_open_shot_with_delete_key(ids[6])  # capture 7
    assert len(j.session.shots) == 8

    # -- 11..14, then Finish -----------------------------------------------------------------------------------------
    T(11, via="shortcut", delayed=False, mon_index=1, kind="A", role=AFTER, caption="Final check on 100 percent")
    T(12, via="shortcut", delayed=False, mon_index=2, kind="B", role=P, caption="Final check on 150 percent")
    T(13, via="shortcut", delayed=True, mon_index=1, kind="B", role=WANT, caption="Delayed reference")
    T(14, via="shortcut", delayed=False, mon_index=2, kind="A", role=P, caption="Last one")
    assert len(j.session.shots) == 12

    # the in-memory originals are untouched by everything above (redaction is applied at save time only)
    for shot in j.session.shots:
        e = j.exp[shot.id]
        crop = s.rig.desktop.image.copy(e.rect.x - s.rig.desktop.origin.x, e.rect.y - s.rig.desktop.origin.y, W, H)
        assert same_pixels(shot.image, crop), f"capture {e.n}: the original image was modified"
    j.notes.append("originals intact before finish")

    j.tray_text_before_finish = s.tray.action_show_session.text()
    j.session_before_finish = j.session
    j.shots_before_finish = list(j.session.shots)
    j.goal_open_shot = j.ed.current_shot_id()
    j.press_shortcut(CTRL | SHIFT)  # Finish
    assert not j.ed.isVisible(), "the editor is hidden while the report is written"
    assert wait_until(lambda: s.finished or s.failed, 60), "Finish never completed"
    assert s.failed == [], s.failed


# ---------------------------------------------------------------------------------------------
# what came out
# ---------------------------------------------------------------------------------------------
def png(path: str | Path) -> QImage:
    img = QImage(str(path))
    assert not img.isNull(), f"cannot read {path}"
    return as_rgb32(img)


def crop_of(j: Journey, e: Expect) -> QImage:
    d = j.rig.desktop
    return d.image.copy(e.rect.x - d.origin.x, e.rect.y - d.origin.y, W, H)


def final_shots(j: Journey):
    """(position, Shot, Expect) for the shots of the finished session, in the user's final order."""
    old = j.session_before_finish
    assert [x.id for x in old.shots] == j.order
    return [(i, shot, j.exp[shot.id]) for i, shot in enumerate(old.shots, start=1)]


def test_the_journey_produced_a_12_shot_report(journey):
    j = journey
    assert len(j.s.finished) == 1 and j.s.failed == []
    out = j.s.finished[0]
    assert len(j.order) == 12 and len(j.deleted) == 2
    assert {e.n for e in j.exp.values()} == {1, 2, 4, 5, 6, 8, 9, 10, 11, 12, 13, 14}
    assert Path(out.folder).is_dir()
    # everything went under the test output folder, nowhere else
    assert os.path.normcase(os.path.abspath(out.folder)).startswith(os.path.normcase(os.path.abspath(OUT_ROOT)))


def test_normal_capture_opened_with_no_delay_and_every_start_path_worked(journey):
    j = journey
    normal_no_editor = [r for r in j.starts if not r["delayed"] and not r["editor_was_visible"]]
    assert normal_no_editor and all(r["selecting_without_waiting"] for r in normal_no_editor)
    vias = {(r["via"], r["delayed"]) for r in j.starts}
    assert vias == {("hotkey", False), ("hotkey", True), ("shortcut", False), ("shortcut", True),
                    ("button", False), ("tray_menu", False), ("tray_menu", True), ("tray_icon", False)}
    for r in j.starts:
        if r["delayed"]:
            assert r["countdown_seconds"] == 3  # the delay from the settings
    # with the editor open, the selector opens right after the short settle (120 ms)
    with_editor = [r for r in j.starts if not r["delayed"] and r["editor_was_visible"]]
    assert with_editor and all(r["seconds_to_selector"] < 1.5 for r in with_editor), with_editor


def test_esc_in_the_selector_and_in_the_countdown_keep_the_session(journey):
    sel, cd = journey.cancels
    assert sel["delayed"] is False and cd["delayed"] is True
    for c in (sel, cd):
        assert c["cancel_signals"] == 1 and c["session_unchanged"] and c["same_shot_open"]
        assert c["idle"] and c["no_overlay_left"] and c["failed"] == []
        assert c["tray_count"] == "Show session (6)"
    assert sel["cancelled_mode"] == "normal" and sel["overlays"] == 2  # one overlay per monitor
    assert cd["cancelled_mode"] == "delayed"
    assert cd["grabs"] == 0, "nothing may be grabbed when the countdown is cancelled"
    assert cd["esc_grab_released"] and cd["badge_gone"] and cd["badge_visible"]
    assert sel["grabs"] == 1  # the selector had frozen the screen; that image was thrown away


def test_reorder_delete_and_reedit_are_reflected_in_the_final_session(journey):
    j = journey
    order_numbers = [j.exp[i].n for i in j.order]
    # 1..10; capture 1 dropped at position 5; capture 10 nudged one step left; captures 3 and 7 deleted;
    # then 11..14 appended
    assert order_numbers == [2, 4, 5, 1, 6, 8, 10, 9, 11, 12, 13, 14], order_numbers
    assert [s.index for s in j.session_before_finish.shots] == list(range(1, 13))
    e2 = next(e for e in j.exp.values() if e.n == 2)
    assert e2.caption.endswith(" - re-edited") and e2.role == Role.PROBLEM and len(e2.pins) == 3


def test_both_pngs_exist_for_every_shot_and_are_never_scaled(journey):
    j = journey
    folder = Path(j.s.finished[0].folder)
    names = sorted(p.name for p in folder.iterdir())
    want = sorted(["report.md", "report.json"] + [f"{i:02d}.png" for i in range(1, 13)] + [f"{i:02d}_annotated.png" for i in range(1, 13)])
    assert names == want, "exactly the report files and two PNGs per shot, no leftovers"
    assert (Path(j.s.store.settings.output_root) / "latest.json").is_file()
    for i, shot, e in final_shots(j):
        for name in (f"{i:02d}.png", f"{i:02d}_annotated.png"):
            img = png(folder / name)
            assert (img.width(), img.height()) == (W, H), f"{name}: physical size, not logical"


def test_original_pngs_are_the_exact_screen_pixels_outside_redactions(journey):
    j = journey
    folder = Path(j.s.finished[0].folder)
    d = j.rig.desktop
    for i, shot, e in final_shots(j):
        img = png(folder / f"{i:02d}.png")
        # corners decode to the screen points the region was dragged over (crop offset == capture_rect)
        for px, py in [(0, 0), (W - 1, 0), (0, H - 1), (W - 1, H - 1), (100, 80)]:
            assert d.rgb_matches(pixel_rgb(img, px, py), e.rect.x + px, e.rect.y + py), (i, px, py)
        if e.redact is not None:
            assert same_pixels(blank_rect(img, e.redact), blank_rect(crop_of(j, e), e.redact)), f"shot {i}"
        else:
            assert same_pixels(img, crop_of(j, e)), f"shot {i} must be a plain copy of the screen"


def test_redaction_is_present_in_both_saved_images(journey):
    j = journey
    folder = Path(j.s.finished[0].folder)
    checked = 0
    for i, shot, e in final_shots(j):
        if e.redact is None:
            continue
        crop = crop_of(j, e)
        orig, annot = png(folder / f"{i:02d}.png"), png(folder / f"{i:02d}_annotated.png")
        inner = IntRect(e.redact.x + 2, e.redact.y + 2, e.redact.w - 4, e.redact.h - 4)
        assert fraction_differing(orig, crop, inner) > 0.9, f"shot {i}: 01.png is not redacted"
        assert fraction_differing(annot, crop, inner) > 0.9, f"shot {i}: _annotated.png is not redacted"
        assert fraction_differing(annot, orig, e.redact) == 0.0, "the same pixelation in both images"
        # just outside the box the screen is intact in both
        ring = IntRect(e.redact.x - 3, e.redact.y - 3, 2, e.redact.h + 6)
        assert fraction_differing(orig, crop, ring) == 0.0 and fraction_differing(annot, crop, ring) == 0.0
        checked += 1
    assert checked == 6  # captures 1, 5, 8, 9, 11, 14 are kind A


def test_pins_in_the_annotated_png_are_centred_on_the_pixel_reported_in_report_json(journey):
    j = journey
    folder = Path(j.s.finished[0].folder)
    doc = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    scale_seen = set()
    for i, (jshot, (pos, shot, e)) in enumerate(zip(doc["shots"], final_shots(j)), start=1):
        orig, annot = png(folder / f"{i:02d}.png"), png(folder / f"{i:02d}_annotated.png")
        s = jshot["monitor"]["scale_percent"] / 100.0
        scale_seen.add(s)
        pins = [a for a in jshot["annotations"] if a["type"] == "pin"]
        assert [(p["x"], p["y"]) for p in pins] == [(x, y) for x, y, _ in e.pins]
        for p in pins:
            x, y = p["x"], p["y"]
            window = IntRect(x - 34, y - 34, 69, 69)
            bbox = diff_bbox(annot, orig, window)
            assert bbox is not None, f"shot {i} pin {p['n']}: nothing was drawn"
            cx, cy = bbox.x + bbox.w / 2.0, bbox.y + bbox.h / 2.0
            assert abs(cx - (x + 0.5)) <= 1.0 and abs(cy - (y + 0.5)) <= 1.0, (i, p, bbox)
            expect_w = 2 * (12 * s + max(1, round(s)))  # pin radius + half the white halo, scales with the monitor
            assert abs(bbox.w - expect_w) <= 4, f"shot {i}: pin size {bbox.w} vs {expect_w} at {s * 100:.0f}%"
        if e.kind == "A":
            # only the two pins were drawn on top of the (redacted) original
            w1, w2 = IntRect(40 - 34, 40 - 34, 69, 69), IntRect(200 - 34, 55 - 34, 69, 69)
            a2 = blank_rect(blank_rect(annot, w1), w2)
            o2 = blank_rect(blank_rect(orig, w1), w2)
            assert same_pixels(a2, o2), f"shot {i}: something else was drawn on the annotated image"
    assert scale_seen == {1.0, 1.5}


def test_report_json_pin_colours_come_from_the_original_pixels_at_the_pin(journey):
    j = journey
    folder = Path(j.s.finished[0].folder)
    doc = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    d = j.rig.desktop
    n = 0
    for i, (jshot, (pos, shot, e)) in enumerate(zip(doc["shots"], final_shots(j)), start=1):
        orig = png(folder / f"{i:02d}.png")
        for p in (a for a in jshot["annotations"] if a["type"] == "pin"):
            gx, gy = e.rect.x + p["x"], e.rect.y + p["y"]
            assert p["color"] == d.color_at(gx, gy), f"shot {i}: colour is not the desktop colour at ({gx}, {gy})"
            assert p["color"] == "#{:02X}{:02X}{:02X}".format(*pixel_rgb(orig, p["x"], p["y"]))
            n += 1
    assert n == 25  # 12 shots x 2 pins + the pin added while re-editing


def test_report_json_matches_what_the_user_did(journey):
    j = journey
    folder = Path(j.s.finished[0].folder)
    doc = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    assert doc["schema_version"] >= 1 and doc["tool"]["name"]
    sess = doc["session"]
    assert (sess["goal"], sess["project_path"], sess["framework_hint"]) == (GOAL, PROJECT, FRAMEWORK)
    assert (sess["expected"], sess["actual"]) == (EXPECTED_TEXT, ACTUAL_TEXT)
    assert sess["shot_count"] == 12 and sess["folder"] == str(PureWindowsPath(folder))
    assert sess["system"]["windows_version"].startswith("Windows 11") and sess["system"]["theme_apps"] == "dark"
    assert [(m["index"], m["scale_percent"], m["rect"]["x"], m["rect"]["y"]) for m in sess["system"]["monitors"]] == \
        [(1, 100, 0, 0), (2, 150, -2880, -300)]
    assert len(doc["shots"]) == 12
    for i, (jshot, (pos, shot, e)) in enumerate(zip(doc["shots"], final_shots(j)), start=1):
        assert jshot["index"] == i and jshot["role"] == e.role.value and jshot["caption"] == e.caption
        assert jshot["capture_mode"] == e.mode.value and jshot["selection"] == "region"
        assert jshot["image_size"] == {"width_px": W, "height_px": H}
        assert jshot["capture_rect"] == {"x": e.rect.x, "y": e.rect.y, "w": W, "h": H}
        assert jshot["monitor"]["index"] == e.monitor_index
        assert jshot["monitor"]["scale_percent"] == (150 if e.monitor_index == 2 else 100)
        assert jshot["monitor"]["dpi"] == (144 if e.monitor_index == 2 else 96)
        assert jshot["window"]["process_name"] == "demo.exe" and jshot["window"]["size_px"] == {"width": 1200, "height": 700}
        assert jshot["window"]["scale_percent"] == jshot["monitor"]["scale_percent"]
        lw = 800 if e.monitor_index == 2 else 1200
        lh = 467 if e.monitor_index == 2 else 700
        assert jshot["window"]["size_logical"] == {"width": lw, "height": lh}
        assert jshot["files"] == {"original": str(PureWindowsPath(folder / f"{i:02d}.png")),
                                  "annotated": str(PureWindowsPath(folder / f"{i:02d}_annotated.png"))}
        assert Path(jshot["files"]["original"]).is_file() and Path(jshot["files"]["annotated"]).is_file()
        anns = jshot["annotations"]
        pins = [(a["x"], a["y"], a["note"]) for a in anns if a["type"] == "pin"]
        assert pins == e.pins and [a["n"] for a in anns if a["type"] == "pin"] == list(range(1, len(e.pins) + 1))
        reds = [a for a in anns if a["type"] == "redact"]
        assert [(a["x"], a["y"], a["w"], a["h"]) for a in reds] == ([(e.redact.x, e.redact.y, e.redact.w, e.redact.h)] if e.redact else [])
        rects = [a for a in anns if a["type"] == "rect"]
        assert [(a["x"], a["y"], a["w"], a["h"]) for a in rects] == ([(e.box.x, e.box.y, e.box.w, e.box.h)] if e.box else [])
        arrows = [a for a in anns if a["type"] == "arrow"]
        assert [(a["x1"], a["y1"], a["x2"], a["y2"]) for a in arrows] == ([e.arrow] if e.arrow else [])
        rulers = [a for a in anns if a["type"] == "ruler"]
        assert [(a["x1"], a["y1"], a["x2"], a["y2"]) for a in rulers] == ([e.ruler] if e.ruler else [])
        if e.ruler:
            assert rulers[0]["length_px"] == 100.0


def test_report_md_reads_well_and_lists_everything(journey):
    j = journey
    folder = Path(j.s.finished[0].folder)
    md = (folder / "report.md").read_text(encoding="utf-8")
    assert "\r" not in md and md.endswith("\n")
    head = md.split("\n## ")[0]
    assert head.startswith(f"# Report: {GOAL}\n")
    for line in (f"- **Goal:** {GOAL}", f"- **Project folder:** {PROJECT}", f"- **App / framework:** {FRAMEWORK}",
                 f"- **Expected:** {EXPECTED_TEXT}", f"- **Actual:** {ACTUAL_TEXT}", "12 screenshots",
                 "Windows 11 Pro 10.0.26200 (synthetic)", "apps theme dark"):
        assert line in head, line
    assert "1920x1080 @ 100%" in head and "2880x1620 @ 150%" in head and "at (-2880, -300)" in head and "primary" in head
    assert "How to read this" in head
    sections = md.split("\n## ")[1:]
    assert len(sections) == 12
    for i, (sec, (pos, shot, e)) in enumerate(zip(sections, final_shots(j)), start=1):
        first = sec.splitlines()[0]
        assert first.startswith(f"{i}. [{e.role.label}] "), first
        assert f"- **Caption:** {e.caption}" in sec
        assert f"- **Annotated image:** {PureWindowsPath(folder / f'{i:02d}_annotated.png')}" in sec
        assert f"- **Original image:** {PureWindowsPath(folder / f'{i:02d}.png')}" in sec
        mode = "delayed capture" if e.mode == CaptureMode.DELAYED else "normal capture"
        assert f", {mode}, region {W}x{H} px at screen ({e.rect.x}, {e.rect.y})" in sec
        pct = 150 if e.monitor_index == 2 else 100
        mon_name = "Side 150" if e.monitor_index == 2 else "Primary 100"
        mon_size = "2880x1620" if e.monitor_index == 2 else "1920x1080"
        assert f'- **Monitor:** {e.monitor_index} (\\\\.\\DISPLAY{e.monitor_index} "{mon_name}", {mon_size}, scale {pct}%)' in sec
        logical = "800x467 logical @150%" if pct == 150 else "1200x700 logical @100%"
        assert f"1200x700 px ({logical})" in sec and "demo.exe (C:\\Demo\\demo.exe)" in sec
        for k, (x, y, note) in enumerate(e.pins, start=1):
            color = j.rig.desktop.color_at(e.rect.x + x, e.rect.y + y)
            assert f"- Pin {k} at ({x}, {y}), color {color}: {note}" in sec, (i, k)
        if e.redact:
            assert "**Redactions:** 1 region redacted in both images." in sec
        if e.box:
            assert f"- Rect 1: x={e.box.x}, y={e.box.y}, {e.box.w}x{e.box.h}" in sec
        if e.arrow:
            assert "- Arrow 1: (120, 110) -> (210, 140)" in sec
        if e.ruler:
            extra = f" ({round(100 / 1.5, 1)} logical px at 150%)" if pct == 150 else ""
            assert f"- Ruler 1: (20, 150) -> (120, 150) = 100 px{extra}" in sec, sec


def test_clipboard_holds_the_exact_paste_prompt(journey):
    j = journey
    folder = Path(j.s.finished[0].folder)
    want = (f"I captured 12 screenshots. Goal: {GOAL}. "
            f"Read {PureWindowsPath(folder)}\\report.md and look at every image it lists "
            f"(annotated and original), then help me with the goal.")
    assert QGuiApplication.clipboard().text() == want
    assert "\n" not in want and PureWindowsPath(folder).is_absolute()


def test_folder_name_slug_toast_and_open_folder_button(journey):
    j = journey
    out = j.s.finished[0]
    folder = Path(out.folder)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{4}_fix-the-clipped-save-button[a-z0-9-]*", folder.name), folder.name
    assert len(folder.name.split("_", 2)[2]) <= 40
    toast = j.s.toast
    assert toast.message_label.text() == "Copied — paste into Claude"
    assert toast.action_button.text() == "Open folder" and not toast.action_button.isHidden()
    toast.action_button.click()
    assert j.s.opened == [str(folder)]


def test_fresh_session_remembers_project_and_framework_and_the_tray_resets(journey):
    j = journey
    s = j.s
    assert j.tray_text_before_finish == "Show session (12)"
    assert s.controller.session is not j.session_before_finish and s.session.shots == []
    assert (s.session.project_path, s.session.framework_hint) == (PROJECT, FRAMEWORK)
    assert s.session.goal == ""  # the goal belongs to the finished session
    assert not s.editor.isVisible() and s.editor.current_shot_id() is None
    assert s.tray.action_show_session.text() == "Show session (0)" and not s.tray.action_show_session.isEnabled()
    assert "Shots in this session: 0" in s.tray.toolTip()
    saved = json.loads(Path(s.store.path).read_text(encoding="utf-8"))
    assert saved["last_project_path"] == PROJECT and saved["last_framework_hint"] == FRAMEWORK
    assert s.autostart.calls == []  # "start with Windows" was never touched
    assert not s.service.is_active


def test_in_memory_originals_are_still_untouched_after_the_write(journey):
    j = journey
    for i, shot, e in final_shots(j):
        assert same_pixels(shot.image, crop_of(j, e)), f"capture {e.n}"
        assert shot.original_path and shot.annotated_path  # the writer recorded absolute paths on the shots


# ---------------------------------------------------------------------------------------------
# separate scenarios
# ---------------------------------------------------------------------------------------------
@pytest.fixture
def stack(qapp, test_out, monkeypatch):
    st = Stack(test_out, monkeypatch, layout_mixed(), delay_seconds=3)
    yield st
    st.shutdown()


def finish_via_shortcut(st: Stack) -> None:
    QTest.keyClick(QApplication.focusWidget() or st.editor.caption_edit, Qt.Key.Key_Return, CTRL | SHIFT)
    assert wait_until(lambda: st.finished or st.failed, 30)


def test_delayed_capture_shows_the_open_menu_and_never_the_countdown(stack):
    """Simulated compositor: whatever is visible at the moment of the grab ends up in the frozen image.
    A menu that the user opens during the countdown must be in the PNG; the countdown badge must not."""
    rig = stack.rig
    stack.store.settings.delay_seconds = 1
    mon = rig.monitors[0]  # 100 % primary
    menu = IntRect(mon.rect.x + 1500, mon.rect.y + 700, 120, 90)  # where the "open menu" is drawn
    MENU_COLOR = "#00AA55"
    state = {"open": False, "seen_badge_during_countdown": False}

    def menu_layer(img):
        if state["open"]:
            from PySide6.QtGui import QColor, QPainter

            p = QPainter(img)
            p.fillRect(menu.x - rig.desktop.origin.x, menu.y - rig.desktop.origin.y, menu.w, menu.h, QColor(MENU_COLOR))
            p.end()

    rig.screen_layers.append(menu_layer)
    badge = rig.badge_rect_for(mon)
    region = IntRect(mon.rect.x + 1400, mon.rect.y + 660, 500, 380)  # contains both the menu and the badge spot
    assert region.contains_rect(menu) and region.contains_rect(badge)
    rig.put_cursor_on(mon)

    # (a) a NORMAL capture before the menu is opened: no menu in it
    stack.hotkeys.press(NAME_CAPTURE)
    QCoreApplication.processEvents()
    assert stack.service.state == "selecting"
    rig.drag_region(region)
    assert wait_until(lambda: len(stack.session.shots) == 1, 5)
    before = stack.session.shots[0]
    assert pixel_rgb(before.image, menu.x - region.x + 10, menu.y - region.y + 10) != (0, 0xAA, 0x55)

    # (b) a DELAYED capture: the user opens a menu while the badge counts down
    rig.set_clock_speed(1.0)
    try:
        stack.hotkeys.press(NAME_DELAYED)
        assert wait_until(lambda: stack.service.state == "countdown", 5) or stack.service.state == "countdown"
        cd = rig.countdowns[-1]
        assert cd.is_visible  # the badge really is on screen while counting
        state["seen_badge_during_countdown"] = True
        state["open"] = True  # <- the user opens the menu now
        assert wait_until(lambda: stack.service.state == "selecting", 10)
    finally:
        rig.set_clock_speed(60.0)
    assert rig.badge_visible_at_grab == 0, "the countdown badge was still on screen when the screen was frozen"
    rig.drag_region(region)
    assert wait_until(lambda: len(stack.session.shots) == 2, 5)
    shot = stack.session.shots[1]
    assert shot.capture_mode == CaptureMode.DELAYED

    # the menu is in the frozen image ... and in the saved PNG; the badge spot is untouched screen
    finish_via_shortcut(stack)
    assert stack.failed == [], stack.failed
    folder = Path(stack.finished[0].folder)
    for name in ("02.png", "02_annotated.png"):
        img = png(folder / name)
        inside = pixel_rgb(img, menu.x - region.x + 60, menu.y - region.y + 45)
        assert inside == (0, 0xAA, 0x55), f"{name}: the open menu is missing from the saved PNG"
        bx, by = badge.x - region.x + badge.w // 2, badge.y - region.y + badge.h // 2
        assert pixel_rgb(img, bx, by) != (255, 0, 255), f"{name}: the countdown badge is in the PNG"
        assert stack.rig.desktop.rgb_matches(pixel_rgb(img, bx, by), region.x + bx, region.y + by)
    img1 = png(folder / "01.png")  # the normal capture never contained the menu
    assert pixel_rgb(img1, menu.x - region.x + 60, menu.y - region.y + 45) != (0, 0xAA, 0x55)
    assert state["seen_badge_during_countdown"]


def test_negative_control_a_badge_left_on_screen_would_be_caught(stack):
    """The simulation above only means something if a still-visible badge WOULD end up in the grab."""
    rig = stack.rig
    mon = rig.monitors[0]
    rig.put_cursor_on(mon)
    from uireport.capture.countdown import CountdownOverlay

    cd = rig._countdown_factory(3, mon, None)
    cd.start()
    try:
        assert cd.is_visible
        img, origin = rig._grab()
        b = rig.badge_rect_for(mon)
        assert pixel_rgb(img, b.x + 5 - origin.x, b.y + 5 - origin.y) == (255, 0, 255)
        assert rig.badge_visible_at_grab == 1
    finally:
        cd.close()


def test_a_failed_write_keeps_the_session_and_the_editor_comes_back_on_the_same_shot(stack):
    j = Journey(stack)
    j.take(1, via="hotkey", delayed=False, mon_index=1, kind="A", role=Role.PROBLEM, caption="first")
    j.take(2, via="shortcut", delayed=False, mon_index=2, kind="B", role=Role.WANT, caption="second")
    ed = stack.editor
    ids = [s.id for s in stack.session.shots]
    j.click_card(ids[0])  # the user is looking at shot 1 when pressing Finish
    # a FILE where the reports folder should be: the write cannot succeed
    blocker = Path(stack.test_out) / "blocked"
    blocker.write_text("not a folder", encoding="utf-8")
    stack.store.settings.output_root = str(blocker / "reports")
    QGuiApplication.clipboard().setText("previous clipboard text")
    finish_via_shortcut(stack)
    assert stack.failed and stack.finished == []
    assert [s.id for s in stack.session.shots] == ids, "the session must survive a failed save"
    assert ed.isVisible() and ed.current_shot_id() == ids[0]
    assert stack.toast.is_error and "Could not save" in stack.toast.message_label.text()
    assert QGuiApplication.clipboard().text() == "previous clipboard text"
    # fix the problem and Finish again
    stack.failed.clear()
    stack.store.settings.output_root = str(Path(stack.test_out) / "reports")
    finish_via_shortcut(stack)
    assert stack.failed == [] and len(stack.finished) == 1
    assert len(list(Path(stack.finished[0].folder).glob("*.png"))) == 4


def test_a_second_session_after_finish_gets_its_own_folder(stack):
    j = Journey(stack)
    j.take(1, via="hotkey", delayed=False, mon_index=1, kind="A", role=Role.PROBLEM, caption="same caption")
    finish_via_shortcut(stack)
    first = Path(stack.finished[0].folder)
    stack.finished.clear()
    j2 = Journey(stack)
    j2.take(2, via="tray_icon", delayed=False, mon_index=2, kind="B", role=Role.AFTER, caption="same caption")
    assert j2.starts[-1]["selecting_without_waiting"], "a left click on the tray icon opens the selector at once"
    finish_via_shortcut(stack)
    second = Path(stack.finished[0].folder)
    assert first != second and first.parent == second.parent
    assert len(list(second.glob("*.png"))) == 2
    assert QGuiApplication.clipboard().text().startswith("I captured 1 screenshot.")
    assert str(PureWindowsPath(second)) in QGuiApplication.clipboard().text()
    doc = json.loads((second / "report.json").read_text(encoding="utf-8"))
    assert doc["shots"][0]["monitor"]["scale_percent"] == 150 and doc["session"]["shot_count"] == 1
