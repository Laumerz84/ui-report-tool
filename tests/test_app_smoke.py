"""Entry point, single instance, launchers and the --smoke-test run (package C).

Everything runs in child processes with temp settings/output folders under .test-output/app.
Nothing here registers a real global hotkey except through the smoke test's unusual
Ctrl+Alt+Shift+F23/F24 combos (unregistered before it exits), and nothing touches the registry
Run key, %APPDATA%\\UIReportTool or %USERPROFILE%\\UIReports.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

os.environ["UIREPORT_BLOCK_REGISTRY_WRITES"] = "1"

ROOT = Path(os.path.abspath(__file__)).parent.parent  # not .resolve(): see note in test_app_smoke
PY = sys.executable
IS_WINDOWS = sys.platform == "win32"


@pytest.fixture
def short_dir():
    """A short-named scratch folder under .test-output/app: CreateProcess limits the working
    directory (and cmd's `cd /d`) to MAX_PATH, and the per-test folders here are longer than that."""
    d = ROOT / ".test-output" / "app" / f"t{uuid.uuid4().hex[:6]}"
    d.mkdir(parents=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def unique_name() -> str:
    return f"uireport-test-{uuid.uuid4().hex[:10]}"


def run_module(args: list[str], *, env: dict | None = None, timeout: float = 120, cwd: Path = ROOT):
    full_env = dict(os.environ)
    full_env.pop("UIREPORT_SETTINGS", None)
    full_env.pop("UIREPORT_OUTPUT", None)
    if env:
        full_env.update(env)
    t0 = time.monotonic()
    proc = subprocess.run(
        [PY, "-m", "uireport", *args],
        cwd=str(cwd), env=full_env, capture_output=True, text=True, timeout=timeout,
        stdin=subprocess.DEVNULL,
    )
    return proc, time.monotonic() - t0


def read_result(settings_dir: Path) -> dict:
    return json.loads((settings_dir / "smoke-result.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------
def test_parse_args_defaults_and_flags():
    from uireport.app.main import parse_args

    a = parse_args([])
    assert (a.settings_path, a.output_root, a.smoke_test, a.offscreen, a.no_hotkeys) == (None, None, False, False, False)
    assert a.instance_name == "UIReportTool"
    b = parse_args(["--settings-path", "s.json", "--output-root", "out", "--smoke-test", "--offscreen",
                    "--no-hotkeys", "--instance-name", "x"])
    assert (b.settings_path, b.output_root, b.smoke_test, b.offscreen, b.no_hotkeys, b.instance_name) == (
        "s.json", "out", True, True, True, "x")


def test_launch_pyw_works_from_any_working_directory(short_dir):
    proc = subprocess.run([PY, str(ROOT / "launch.pyw"), "--help"], cwd=str(short_dir), capture_output=True,
                          text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stderr
    assert "--smoke-test" in proc.stdout and "--instance-name" in proc.stdout


def test_python_dash_m_uireport_help(short_dir):
    proc, _ = run_module(["--help"], cwd=short_dir, env={"PYTHONPATH": str(ROOT)})
    assert proc.returncode == 0 and "usage" in proc.stdout.lower()


# ---------------------------------------------------------------------------
# single instance
# ---------------------------------------------------------------------------
def test_single_instance_guard(qapp):
    from uireport.app.main import SingleInstance

    name = unique_name()
    first, second = SingleInstance(name), SingleInstance(name)
    assert first.acquire()
    try:
        assert not second.acquire()  # the name is taken
        assert SingleInstance(unique_name()).acquire()  # another name is free
    finally:
        first.release()
    third = SingleInstance(name)
    assert third.acquire()  # free again after release
    third.release()
    first.release()  # idempotent


def test_second_launch_says_already_running_and_exits_zero(qapp, test_out):
    from uireport.app.main import SingleInstance

    name = unique_name()
    holder = SingleInstance(name)
    assert holder.acquire()
    try:
        proc, secs = run_module(
            ["--offscreen", "--no-hotkeys", "--instance-name", name, "--settings-path", str(test_out / "s.json")],
            timeout=60,
        )
    finally:
        holder.release()
    assert proc.returncode == 0, proc.stderr
    assert "already running" in proc.stderr.lower()
    assert not (test_out / "s.json").exists()  # it never got as far as the settings
    assert secs < 30


# ---------------------------------------------------------------------------
# smoke test
# ---------------------------------------------------------------------------
def test_smoke_test_runs_the_whole_pipeline_and_exits_zero(test_out):
    settings = test_out / "settings.json"
    reports = test_out / "reports"
    proc, secs = run_module(
        ["--smoke-test", "--offscreen", "--instance-name", unique_name(),
         "--settings-path", str(settings), "--output-root", str(reports)],
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr[-2000:]!r}"
    assert "SMOKE OK" in proc.stdout
    result = read_result(test_out)
    assert result["ok"] is True and result["message"] == "ok"
    assert result["seconds"] < 15 and secs < 60  # exits by itself, quickly
    # which parts were real vs fake is reported (so the integrator can see what the wiring proved)
    assert set(result["parts"]) >= {"capture", "hotkeys", "editor", "toast", "tray"}
    assert all(v in ("real", "fake", "off") for v in result["parts"].values())
    folders = [p for p in reports.iterdir() if p.is_dir()]
    assert len(folders) == 1
    assert {p.name for p in folders[0].iterdir()} == {
        "report.md", "report.json", "01.png", "01_annotated.png", "02.png", "02_annotated.png",
        "03.png", "03_annotated.png",
    }
    assert (reports / "latest.json").is_file()
    assert (test_out / "uireport.log").is_file()  # log next to the settings file
    saved = json.loads(settings.read_text(encoding="utf-8"))
    assert saved["start_with_windows"] is False  # "start with Windows" was never switched on
    assert saved["hotkey_capture"] == "Ctrl+Alt+Shift+F23" and saved["hotkey_delayed"] == "Ctrl+Alt+Shift+F24"
    assert saved["output_root"] == str(reports)


def test_smoke_test_with_no_hotkeys(test_out):
    proc, _ = run_module(
        ["--smoke-test", "--offscreen", "--no-hotkeys", "--instance-name", unique_name(),
         "--settings-path", str(test_out / "settings.json"), "--output-root", str(test_out / "reports")],
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert read_result(test_out)["parts"]["hotkeys"] == "off"


def test_smoke_test_reports_failure_with_a_message_and_nonzero_exit(test_out):
    blocker = test_out / "not_a_folder"
    blocker.write_text("x")
    proc, _ = run_module(
        ["--smoke-test", "--offscreen", "--no-hotkeys", "--instance-name", unique_name(),
         "--settings-path", str(test_out / "settings.json"), "--output-root", str(blocker / "reports")],
    )
    assert proc.returncode == 1
    assert "SMOKE FAILED" in proc.stderr
    result = read_result(test_out)
    assert result["ok"] is False and result["message"] != "ok"


def test_smoke_test_without_paths_never_touches_the_real_folders(test_out):
    fake_appdata, fake_home = test_out / "appdata", test_out / "home"
    fake_appdata.mkdir()
    fake_home.mkdir()
    real_appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "UIReportTool"
    real_reports = Path.home() / "UIReports"
    before = {p: (p.exists(), p.stat().st_mtime if p.exists() else 0) for p in (real_appdata, real_reports)}
    proc, _ = run_module(
        ["--smoke-test", "--offscreen", "--no-hotkeys", "--instance-name", unique_name()],
        env={"APPDATA": str(fake_appdata), "USERPROFILE": str(fake_home), "HOME": str(fake_home)},
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "SMOKE OK" in proc.stdout
    assert list(fake_appdata.iterdir()) == []  # not even the redirected %APPDATA% was used
    assert list(fake_home.iterdir()) == []
    after = {p: (p.exists(), p.stat().st_mtime if p.exists() else 0) for p in (real_appdata, real_reports)}
    assert after == before


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows launchers")
def test_double_click_launcher_starts_pythonw_and_the_smoke_test_finishes_by_itself(test_out, short_dir):
    """UIReportTool.bat -> .venv pythonw.exe -> launch.pyw -> main(): the whole double-click path,
    made harmless with --smoke-test (it writes smoke-result.json and exits on its own)."""
    import psutil

    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.is_file():
        pytest.skip("no .venv in this checkout")
    name = unique_name()
    args = [str(ROOT / "UIReportTool.bat"), "--smoke-test", "--offscreen", "--no-hotkeys",
            "--instance-name", name, "--settings-path", str(test_out / "settings.json"),
            "--output-root", str(test_out / "reports")]
    # cmd /s /c strips exactly one outer pair of quotes, so paths with spaces (e.g. a folder
    # called "UI Report Tool") survive - plain `cmd /c` mangles several quoted arguments.
    proc = subprocess.run(
        f'cmd /s /c "{subprocess.list2cmdline(args)}"',
        cwd=str(short_dir), capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result_file = test_out / "smoke-result.json"
    deadline = time.monotonic() + 90
    while not result_file.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert result_file.exists(), "the launched app never wrote its smoke result"
    assert json.loads(result_file.read_text(encoding="utf-8"))["ok"] is True

    def leftovers():
        out = []
        for p in psutil.process_iter(["pid", "cmdline"]):
            try:
                if name in " ".join(p.info["cmdline"] or []):
                    out.append(p.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return out

    end = time.monotonic() + 20
    while leftovers() and time.monotonic() < end:
        time.sleep(0.2)
    assert leftovers() == [], "the launched process is still running"


# ---------------------------------------------------------------------------
# batch files
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["UIReportTool.bat", "setup.bat"])
def test_batch_files_are_ascii_with_crlf(name):
    data = (ROOT / name).read_bytes()
    assert all(b < 128 for b in data), "non-ASCII byte in a .bat file"
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")  # CRLF only (goto labels need it)


def test_launcher_and_setup_contents():
    launcher = (ROOT / "UIReportTool.bat").read_text(encoding="ascii")
    assert r".venv\Scripts\pythonw.exe" in launcher and "launch.pyw" in launcher and "setup.bat" in launcher
    setup = (ROOT / "setup.bat").read_text(encoding="ascii")
    assert "-m venv" in setup and "requirements.txt" in setup
    for line in setup.splitlines():
        if "pip install" in line:  # every pip call goes through the venv's own python, never the global one
            assert r".venv\Scripts\python.exe" in line, line
    assert "--user" not in setup and "--break-system-packages" not in setup
    src = (ROOT / "launch.pyw").read_text(encoding="utf-8")
    assert "sys.path" in src and "uireport.app.main" in src and "main()" in src


def _restricted_env() -> dict:
    return {**os.environ, "PATH": os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")}


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows launchers")
def test_launcher_without_a_venv_tells_the_user_to_run_setup(short_dir):
    env = _restricted_env()
    if subprocess.run(["where", "pyw"], env=env, capture_output=True, cwd=str(short_dir)).returncode == 0:
        pytest.skip("a pyw launcher is reachable even with a minimal PATH")
    copy = short_dir / "app"
    copy.mkdir()
    shutil.copy2(ROOT / "UIReportTool.bat", copy / "UIReportTool.bat")
    proc = subprocess.run(["cmd", "/c", str(copy / "UIReportTool.bat")], cwd=str(short_dir), env=env,
                          capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert proc.returncode == 1
    assert "setup.bat" in proc.stdout and ".venv" in proc.stdout


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows launchers")
def test_setup_without_python_explains_how_to_install_it(short_dir):
    env = _restricted_env()
    for exe in ("py", "python"):
        if subprocess.run(["where", exe], env=env, capture_output=True, cwd=str(short_dir)).returncode == 0:
            pytest.skip(f"{exe} is reachable even with a minimal PATH; not running setup.bat")
    copy = short_dir / "app"
    copy.mkdir()
    shutil.copy2(ROOT / "setup.bat", copy / "setup.bat")
    (copy / "requirements.txt").write_text("", encoding="ascii")
    proc = subprocess.run(["cmd", "/c", str(copy / "setup.bat")], cwd=str(copy), env=env,
                          capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert proc.returncode == 1
    assert "winget install Python.Python.3.12" in proc.stdout and "Add python.exe to PATH" in proc.stdout
    assert not (copy / ".venv").exists()
