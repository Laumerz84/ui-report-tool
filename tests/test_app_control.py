"""--quit: another process asks the running copy to quit, exactly like the tray's Quit (package C).

The channel is a named Win32 event in this Windows session (no sockets, no pipes). Real-app tests
run in child processes with temp settings/output folders and unique instance names, offscreen and
without hotkeys, so they never meet a copy the user is running.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

os.environ["UIREPORT_BLOCK_REGISTRY_WRITES"] = "1"

from uireport.app import control  # noqa: E402

ROOT = Path(os.path.abspath(__file__)).parent.parent
PY = sys.executable
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="named Win32 events")


def unique_name() -> str:
    return f"uireport-ctl-{uuid.uuid4().hex[:10]}"


def pump_until(qapp, done, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not done() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    return done()


def child_env() -> dict:
    env = dict(os.environ)
    env.pop("UIREPORT_SETTINGS", None)
    env.pop("UIREPORT_OUTPUT", None)
    env["UIREPORT_BLOCK_REGISTRY_WRITES"] = "1"
    return env


def run_quit(name: str, settings: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, "-m", "uireport", "--quit", "--offscreen", "--instance-name", name, "--settings-path", str(settings)],
        cwd=str(ROOT), env=child_env(), capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
    )


# ---- the channel ------------------------------------------------------------------------------------------
def test_request_reaches_the_listener_on_the_gui_thread(qapp):
    name = unique_name()
    listener = control.QuitListener(name)
    assert listener.start()
    seen: list = []
    listener.quit_requested.connect(lambda: seen.append(threading.current_thread() is threading.main_thread()))
    try:
        assert control.request_quit(name) is True
        assert pump_until(qapp, lambda: seen)
        assert seen == [True]
        # it keeps listening: a second request arrives too
        assert control.request_quit(name) is True
        assert pump_until(qapp, lambda: len(seen) == 2)
    finally:
        listener.stop()


def test_request_with_nobody_listening_returns_false_fast():
    t0 = time.monotonic()
    assert control.request_quit(unique_name()) is False
    assert time.monotonic() - t0 < 1.0


def test_a_stopped_listener_no_longer_receives_requests(qapp):
    name = unique_name()
    listener = control.QuitListener(name)
    assert listener.start()
    listener.stop()
    assert control.request_quit(name) is False
    assert not listener.is_listening


def test_instances_with_different_names_never_cross_talk(qapp):
    a, b = unique_name(), unique_name()
    la, lb = control.QuitListener(a), control.QuitListener(b)
    got: list = []
    la.quit_requested.connect(lambda: got.append("a"))
    lb.quit_requested.connect(lambda: got.append("b"))
    assert la.start() and lb.start()
    try:
        assert control.request_quit(b) is True
        assert pump_until(qapp, lambda: got)
        pump_until(qapp, lambda: False, timeout=0.2)
        assert got == ["b"]
    finally:
        la.stop()
        lb.stop()


# ---- the --quit flag against real processes ---------------------------------------------------------------
def test_quit_flag_with_no_running_copy_exits_3(test_out):
    proc = run_quit(unique_name(), test_out / "quitter" / "settings.json")
    assert proc.returncode == 3, proc.stdout + proc.stderr


def test_quit_flag_closes_a_real_running_copy_like_the_tray_quit(test_out):
    name = unique_name()
    app = subprocess.Popen(
        [PY, "-m", "uireport", "--offscreen", "--no-hotkeys", "--instance-name", name,
         "--settings-path", str(test_out / "app" / "settings.json"), "--output-root", str(test_out / "reports")],
        cwd=str(ROOT), env=child_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 40
        proc = None
        while time.monotonic() < deadline:  # 3/1 while the app is still starting up
            proc = run_quit(name, test_out / "quitter" / "settings.json")
            if proc.returncode == 0:
                break
            time.sleep(0.3)
        assert proc is not None and proc.returncode == 0, proc.stdout + proc.stderr
        assert app.wait(timeout=20) == 0
    finally:
        if app.poll() is None:
            app.kill()
            app.wait(10)
