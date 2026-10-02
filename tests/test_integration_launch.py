"""A NORMAL start (not --smoke-test) of the real app in a child process: it must come up, stay alive in
the tray loop, refuse a second copy, and stop when asked. Runs offscreen without hotkeys, with temp
settings / output folders under .test-output; nothing on the real desktop, registry or user folders.
The child is always killed in `finally`."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(os.path.abspath(__file__)).parent.parent  # not .resolve(): the sandbox virtualises AppData
PY = sys.executable


@pytest.fixture
def short_dir():
    d = ROOT / ".test-output" / "integration" / f"t{uuid.uuid4().hex[:6]}"
    d.mkdir(parents=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _env() -> dict:
    env = dict(os.environ)
    for k in ("UIREPORT_SETTINGS", "UIREPORT_OUTPUT"):
        env.pop(k, None)
    env["UIREPORT_BLOCK_REGISTRY_WRITES"] = "1"
    return env


def test_normal_start_stays_alive_refuses_a_second_copy_and_stops(short_dir):
    name = f"uireport-it-{uuid.uuid4().hex[:8]}"
    settings = short_dir / "settings.json"
    common = ["--offscreen", "--no-hotkeys", "--instance-name", name, "--settings-path", str(settings),
              "--output-root", str(short_dir / "reports")]
    first = subprocess.Popen([PY, "-m", "uireport", *common], cwd=str(ROOT), env=_env(), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        # comes up and keeps running (the Qt loop is waiting for the tray / hotkeys)
        log = short_dir / "uireport.log"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and first.poll() is None and not (log.exists() and "starting" in log.read_text(encoding="utf-8")):
            time.sleep(0.1)
        assert first.poll() is None, first.stderr.read().decode(errors="replace")
        time.sleep(2.0)
        assert first.poll() is None, "the app must keep running after start-up"

        # a second copy says so and exits 0
        second = subprocess.run([PY, "-m", "uireport", *common], cwd=str(ROOT), env=_env(), capture_output=True,
                                text=True, timeout=60, stdin=subprocess.DEVNULL)
        assert second.returncode == 0 and "already running" in second.stderr.lower(), second.stderr
        assert first.poll() is None, "the second copy must not disturb the first"
    finally:
        if first.poll() is None:
            first.kill()
        first.wait(timeout=15)
        err = first.stderr.read().decode(errors="replace") if first.stderr else ""
        first.stderr.close()
    text = (short_dir / "uireport.log").read_text(encoding="utf-8")
    assert "starting" in text
    assert "Traceback" not in text and "Traceback" not in err, err + text
    assert " ERROR " not in text, text
    # everything it wrote went into the folder we gave it
    assert not (short_dir / "reports").exists() or not any((short_dir / "reports").iterdir())
