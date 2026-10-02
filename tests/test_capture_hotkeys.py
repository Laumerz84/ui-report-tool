"""HotkeyManager: bookkeeping with a fake backend, plus a short real-Win32 round trip that only
uses Ctrl+Alt+Shift+F23 (< 1 s, always unregistered) and never sends real input."""
from __future__ import annotations

import threading
import time

import pytest
from PySide6.QtCore import QEventLoop, Qt, QTimer

from uireport import hotkeyspec
from uireport.capture.hotkeys import (
    ERROR_HOTKEY_ALREADY_REGISTERED,
    HotkeyManager,
    TemporaryHotkey,
    Win32HotkeyBackend,
)

NOREPEAT = hotkeyspec.MOD_NOREPEAT
CAPTURE, DELAYED = hotkeyspec.NAME_CAPTURE, hotkeyspec.NAME_DELAYED


def hk(text):
    h = hotkeyspec.parse_hotkey(text)
    return (h.modifiers, h.vk)


class FakeBackend:
    def __init__(self, taken=()):
        self.taken = {hk(t) for t in taken}  # combinations "owned by another program"
        self.active: dict[int, tuple[int, int]] = {}
        self.calls: list[tuple] = []
        self.started = 0
        self.stopped = 0
        self.callback = None

    def start(self, cb):
        self.started += 1
        self.callback = cb

    def stop(self):
        self.stopped += 1
        self.active.clear()

    def register(self, hk_id, modifiers, vk):
        self.calls.append(("register", hk_id, modifiers & ~NOREPEAT, vk, bool(modifiers & NOREPEAT)))
        key = (modifiers & ~NOREPEAT, vk)
        if key in self.taken or hk_id in self.active or key in {(m & ~NOREPEAT, v) for m, v in self.active.values()}:
            return ERROR_HOTKEY_ALREADY_REGISTERED
        self.active[hk_id] = (modifiers, vk)
        return None

    def unregister(self, hk_id):
        self.calls.append(("unregister", hk_id))
        self.active.pop(hk_id, None)

    def press(self, hk_id):
        self.callback(hk_id)


@pytest.fixture
def mgr(qapp):
    b = FakeBackend()
    m = HotkeyManager(None, b)
    m.failures = []
    m.fired = []
    m.registration_failed.connect(lambda n, msg: m.failures.append((n, msg)))
    m.activated.connect(m.fired.append)
    yield m
    m.stop()


def test_registers_both_hotkeys_with_norepeat(mgr):
    res = mgr.set_hotkeys({CAPTURE: "ctrl + alt + s", DELAYED: "Ctrl+Alt+D"})
    assert res == {CAPTURE: None, DELAYED: None}
    assert mgr.registered == {CAPTURE: "Ctrl+Alt+S", DELAYED: "Ctrl+Alt+D"}
    b = mgr.backend
    assert b.started == 1
    regs = [c for c in b.calls if c[0] == "register"]
    assert len(regs) == 2 and all(c[4] for c in regs)  # MOD_NOREPEAT on both
    assert {(c[2], c[3]) for c in regs} == {hk("Ctrl+Alt+S"), hk("Ctrl+Alt+D")}
    assert mgr.failures == []


def test_a_press_emits_activated_with_the_hotkey_name(mgr):
    mgr.set_hotkeys({CAPTURE: "Ctrl+Alt+S", DELAYED: "Ctrl+Alt+D"})
    b = mgr.backend
    b.press(1)
    b.press(2)
    b.press(99)  # unknown id: ignored
    assert mgr.fired == [CAPTURE, DELAYED]


def test_conflict_is_reported_and_emitted(mgr):
    mgr.backend.taken.add(hk("Ctrl+Alt+D"))
    res = mgr.set_hotkeys({CAPTURE: "Ctrl+Alt+S", DELAYED: "Ctrl+Alt+D"})
    assert res[CAPTURE] is None
    assert "already in use" in res[DELAYED] and "Ctrl+Alt+D" in res[DELAYED]
    assert mgr.failures == [(DELAYED, res[DELAYED])]
    assert mgr.registered == {CAPTURE: "Ctrl+Alt+S"}  # the working one stays active
    mgr.backend.press(2)
    assert mgr.fired == []  # a name that failed to register never fires


def test_failed_rebind_restores_the_previous_binding_and_leaves_the_other_alone(mgr):
    mgr.set_hotkeys({CAPTURE: "Ctrl+Alt+S", DELAYED: "Ctrl+Alt+D"})
    mgr.backend.taken.add(hk("Ctrl+Alt+X"))
    mgr.backend.calls.clear()
    res = mgr.set_hotkeys({CAPTURE: "Ctrl+Alt+X", DELAYED: "Ctrl+Alt+D"})
    assert res[DELAYED] is None and "already in use" in res[CAPTURE]
    assert mgr.registered == {CAPTURE: "Ctrl+Alt+S", DELAYED: "Ctrl+Alt+D"}
    assert "stays active" in res[CAPTURE]
    # the delayed hotkey was never touched
    assert not [c for c in mgr.backend.calls if len(c) > 1 and c[1] == 2]
    # and the restored one really works
    mgr.backend.press(1)
    assert mgr.fired == [CAPTURE]
    assert mgr.failures == [(CAPTURE, res[CAPTURE])]


def test_rebinding_one_name_leaves_the_other_untouched(mgr):
    mgr.set_hotkeys({CAPTURE: "Ctrl+Alt+S", DELAYED: "Ctrl+Alt+D"})
    mgr.backend.calls.clear()
    res = mgr.set_hotkeys({CAPTURE: "Ctrl+Alt+Q"})
    assert res == {CAPTURE: None}
    assert mgr.registered == {CAPTURE: "Ctrl+Alt+Q", DELAYED: "Ctrl+Alt+D"}
    assert not [c for c in mgr.backend.calls if len(c) > 1 and c[1] == 2]


def test_unchanged_mapping_makes_no_backend_calls(mgr):
    mgr.set_hotkeys({CAPTURE: "Ctrl+Alt+S", DELAYED: "Ctrl+Alt+D"})
    mgr.backend.calls.clear()
    assert mgr.set_hotkeys({CAPTURE: "ctrl+alt+s", DELAYED: "Ctrl+Alt+D"}) == {CAPTURE: None, DELAYED: None}
    assert mgr.backend.calls == []


def test_swapping_two_hotkeys_works(mgr):
    mgr.set_hotkeys({CAPTURE: "Ctrl+Alt+S", DELAYED: "Ctrl+Alt+D"})
    res = mgr.set_hotkeys({CAPTURE: "Ctrl+Alt+D", DELAYED: "Ctrl+Alt+S"})
    assert res == {CAPTURE: None, DELAYED: None}
    assert mgr.registered == {CAPTURE: "Ctrl+Alt+D", DELAYED: "Ctrl+Alt+S"}


def test_invalid_reserved_and_duplicate_hotkeys_never_reach_the_os(mgr):
    mgr.set_hotkeys({CAPTURE: "Ctrl+Alt+S"})
    mgr.backend.calls.clear()
    res = mgr.set_hotkeys({DELAYED: "Win+Shift+S"})
    assert res[DELAYED] and "reserved" in res[DELAYED]
    res = mgr.set_hotkeys({DELAYED: "banana"})
    assert res[DELAYED] and "banana" in res[DELAYED]
    res = mgr.set_hotkeys({DELAYED: "S"})
    assert res[DELAYED] and "Ctrl" in res[DELAYED]
    res = mgr.set_hotkeys({DELAYED: "Ctrl+Alt+S"})  # same as the capture hotkey
    assert res[DELAYED] and "already used" in res[DELAYED]
    assert mgr.backend.calls == []
    assert mgr.registered == {CAPTURE: "Ctrl+Alt+S"}
    assert len(mgr.failures) == 4


def test_empty_text_unbinds_a_name(mgr):
    mgr.set_hotkeys({CAPTURE: "Ctrl+Alt+S", DELAYED: "Ctrl+Alt+D"})
    assert mgr.set_hotkeys({CAPTURE: ""}) == {CAPTURE: None}
    assert mgr.registered == {DELAYED: "Ctrl+Alt+D"}
    mgr.backend.press(1)
    assert mgr.fired == []


def test_stop_unregisters_everything_and_is_idempotent(mgr):
    mgr.set_hotkeys({CAPTURE: "Ctrl+Alt+S", DELAYED: "Ctrl+Alt+D"})
    b = mgr.backend
    mgr.stop()
    assert b.active == {} and b.stopped == 1 and mgr.registered == {}
    assert sorted(c[1] for c in b.calls if c[0] == "unregister") == [1, 2]
    mgr.stop()
    assert b.stopped == 1
    # it can be started again
    assert mgr.set_hotkeys({CAPTURE: "Ctrl+Alt+S"}) == {CAPTURE: None}
    assert b.started == 2


def test_start_is_idempotent(mgr):
    mgr.start()
    mgr.start()
    assert mgr.backend.started == 1


def test_set_hotkeys_starts_the_thread_when_needed(mgr):
    assert not mgr.is_running
    mgr.set_hotkeys({CAPTURE: "Ctrl+Alt+S"})
    assert mgr.is_running


def test_a_backend_that_refuses_reports_a_readable_error(qapp):
    class Refusing(FakeBackend):
        def register(self, *a):
            return 87

    m = HotkeyManager(None, Refusing())
    try:
        res = m.set_hotkeys({CAPTURE: "Ctrl+Alt+S"})
        assert "Ctrl+Alt+S" in res[CAPTURE] and "87" in res[CAPTURE]
        assert m.registered == {}
    finally:
        m.stop()


# ---- real Win32 backend -------------------------------------------------------------------------------------
def run_loop_until(predicate, timeout=2.0):
    loop = QEventLoop()
    deadline = time.monotonic() + timeout
    t = QTimer()
    t.setInterval(5)

    def check():
        if predicate() or time.monotonic() > deadline:
            loop.quit()

    t.timeout.connect(check)
    t.start()
    if not predicate():
        loop.exec()
    t.stop()
    return predicate()


def hotkey_threads():
    return [t for t in threading.enumerate() if t.name == "uireport-hotkeys"]


def test_real_backend_thread_lifecycle_leaves_nothing_behind(qapp):
    b = Win32HotkeyBackend()
    b.start(lambda i: None)
    try:
        assert b.is_running and b.thread_id != 0
        assert len(hotkey_threads()) == 1
        b.start(lambda i: None)  # idempotent
        assert len(hotkey_threads()) == 1
    finally:
        t0 = time.monotonic()
        b.stop()
    assert time.monotonic() - t0 < 1.0
    assert hotkey_threads() == [] and not b.is_running and b.thread_id == 0
    b.stop()  # idempotent


def test_real_hotkey_round_trip_with_f23_only(qapp):
    """RegisterHotKey(Ctrl+Alt+Shift+F23) for well under a second, a simulated WM_HOTKEY posted
    to OUR OWN thread (no key is pressed), conflict detection with a second manager."""
    combo = "Ctrl+Alt+Shift+F23"
    m = HotkeyManager()
    m2 = None
    m3 = None
    got_queued: list[str] = []
    got_thread: list[str] = []
    try:
        m.activated.connect(got_queued.append, Qt.ConnectionType.QueuedConnection)
        m.activated.connect(lambda n: got_thread.append(threading.current_thread().name), Qt.ConnectionType.DirectConnection)
        res = m.set_hotkeys({CAPTURE: combo})
        if res[CAPTURE] is not None and "already in use" in res[CAPTURE]:
            pytest.skip("another process (a parallel test run?) holds Ctrl+Alt+Shift+F23")
        assert res == {CAPTURE: None}
        assert m.registered == {CAPTURE: combo}
        # simulate the press exactly as the system does it
        assert m.backend.post_hotkey(1, hk(combo)[0], hk(combo)[1])
        assert run_loop_until(lambda: bool(got_queued))
        assert got_queued == [CAPTURE]
        assert got_thread == ["uireport-hotkeys"]  # emitted from the hotkey thread only
        # a second registrant of the same combination gets the Win32 conflict
        m2 = HotkeyManager()
        failed = []
        m2.registration_failed.connect(lambda n, msg: failed.append((n, msg)))
        res2 = m2.set_hotkeys({DELAYED: combo})
        assert "already in use" in res2[DELAYED] and failed and failed[0][0] == DELAYED
        assert m2.registered == {}
    finally:
        m.stop()
        if m2 is not None:
            m2.stop()
    # after stop() the combination is free again -> nothing was left registered
    try:
        m3 = HotkeyManager()
        assert m3.set_hotkeys({CAPTURE: combo}) == {CAPTURE: None}
    finally:
        if m3 is not None:
            m3.stop()
    assert hotkey_threads() == []


def test_real_rebind_restores_the_previous_hotkey(qapp):
    a, b_combo = "Ctrl+Alt+Shift+F23", "Ctrl+Alt+Shift+F24"
    holder = HotkeyManager()
    m = HotkeyManager()
    try:
        if holder.set_hotkeys({DELAYED: b_combo})[DELAYED] is not None:
            pytest.skip("F24 combination already held elsewhere")
        res = m.set_hotkeys({CAPTURE: a})
        if res[CAPTURE] is not None:
            pytest.skip("F23 combination already held elsewhere")
        res = m.set_hotkeys({CAPTURE: b_combo})  # held by `holder` -> must fail and roll back
        assert "already in use" in res[CAPTURE]
        assert m.registered == {CAPTURE: a}
        assert m.backend.post_hotkey(1)
    finally:
        m.stop()
        holder.stop()
    assert hotkey_threads() == []


def test_temporary_hotkey_acquire_press_release(qapp):
    """The same mechanism the countdown uses for Esc, exercised with F23 instead of Esc."""
    mods = hotkeyspec.MOD_CONTROL | hotkeyspec.MOD_ALT | hotkeyspec.MOD_SHIFT
    guard = TemporaryHotkey(mods, 0x86)  # VK_F23
    other = TemporaryHotkey(mods, 0x86)
    pressed: list[int] = []
    try:
        guard.pressed.connect(lambda: pressed.append(1), Qt.ConnectionType.QueuedConnection)
        if not guard.acquire():
            pytest.skip("F23 combination already held elsewhere")
        assert guard.active
        assert not other.acquire()  # a second grab of the same key fails cleanly (and leaves no thread)
        assert guard._backend.post_hotkey(TemporaryHotkey._ID)
        assert run_loop_until(lambda: bool(pressed))
        assert pressed == [1]
    finally:
        guard.release()
        other.release()
    assert not guard.active and hotkey_threads() == []
    guard.release()  # idempotent
    again = TemporaryHotkey(mods, 0x86)
    try:
        assert again.acquire()  # released for real
    finally:
        again.release()
    assert hotkey_threads() == []


def test_backend_that_cannot_start_reports_errors_instead_of_raising(qapp):
    class Broken(FakeBackend):
        def start(self, cb):
            raise OSError("no message loop")

    m = HotkeyManager(None, Broken())
    failures = []
    m.registration_failed.connect(lambda n, msg: failures.append(n))
    res = m.set_hotkeys({CAPTURE: "Ctrl+Alt+S", DELAYED: "Ctrl+Alt+D"})
    assert all("unavailable" in v for v in res.values()) and sorted(failures) == [CAPTURE, DELAYED]
    assert m.registered == {} and not m.is_running
    m.stop()
