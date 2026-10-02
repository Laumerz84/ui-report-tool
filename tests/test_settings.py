import json
import os

from uireport.models import Session
from uireport.settings import (
    DEFAULT_DELAY_SECONDS,
    ENV_OUTPUT,
    ENV_SETTINGS,
    Settings,
    SettingsStore,
    default_output_root,
    default_settings_path,
    load_settings,
    resolve_settings_path,
    save_settings,
)


def test_defaults_match_spec(monkeypatch):
    monkeypatch.delenv(ENV_OUTPUT, raising=False)
    s = Settings()
    assert s.hotkey_capture == "Ctrl+Alt+S"
    assert s.hotkey_delayed == "Ctrl+Alt+D"
    assert s.delay_seconds == DEFAULT_DELAY_SECONDS == 3
    assert s.start_with_windows is False
    assert s.last_project_path == "" and s.last_framework_hint == ""
    assert s.output_root.endswith("UIReports")
    assert s.problems() == []


def test_default_paths_use_appdata_and_userprofile(monkeypatch):
    monkeypatch.setenv("APPDATA", r"C:\Fake\Roaming")
    monkeypatch.setenv("USERPROFILE", r"C:\Fake\User")
    monkeypatch.delenv(ENV_OUTPUT, raising=False)
    monkeypatch.delenv(ENV_SETTINGS, raising=False)
    assert str(default_settings_path()).replace("/", "\\") == r"C:\Fake\Roaming\UIReportTool\settings.json"
    assert str(default_output_root()).replace("/", "\\") == r"C:\Fake\User\UIReports"


def test_path_resolution_order(monkeypatch, test_out):
    monkeypatch.setenv(ENV_SETTINGS, str(test_out / "env.json"))
    assert resolve_settings_path() == test_out / "env.json"
    assert resolve_settings_path(test_out / "arg.json") == test_out / "arg.json"
    monkeypatch.setenv(ENV_OUTPUT, str(test_out / "out"))
    assert default_output_root() == test_out / "out"


def test_save_load_round_trip(test_out):
    p = test_out / "settings.json"
    s = Settings(
        hotkey_capture="Ctrl+Shift+F9",
        hotkey_delayed="ctrl + alt + f10",
        delay_seconds=5,
        output_root=str(test_out / "reports"),
        last_project_path=r"C:\dev\app",
        last_framework_hint="WPF",
        start_with_windows=True,
    )
    save_settings(s, p)
    assert p.exists()
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["hotkey_delayed"] == "Ctrl+Alt+F10"  # normalised on save
    s2 = load_settings(p)
    assert s2.hotkey_capture == "Ctrl+Shift+F9"
    assert s2.hotkey_delayed == "Ctrl+Alt+F10"
    assert s2.delay_seconds == 5
    assert s2.output_root == str(test_out / "reports")
    assert s2.last_project_path == r"C:\dev\app" and s2.last_framework_hint == "WPF"
    assert s2.start_with_windows is True


def test_save_creates_parent_folders_and_leaves_no_temp_files(test_out):
    p = test_out / "deep" / "er" / "settings.json"
    save_settings(Settings(), p)
    assert p.exists()
    assert [f.name for f in p.parent.iterdir()] == ["settings.json"]


def test_missing_and_corrupt_files_give_defaults(test_out):
    assert load_settings(test_out / "nope.json").hotkey_capture == "Ctrl+Alt+S"
    bad = test_out / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert load_settings(bad).delay_seconds == 3
    weird = test_out / "weird.json"
    weird.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert load_settings(weird).hotkey_delayed == "Ctrl+Alt+D"


def test_invalid_values_fall_back_to_defaults(test_out):
    p = test_out / "s.json"
    p.write_text(
        json.dumps(
            {
                "hotkey_capture": "Win+Shift+S",  # reserved
                "hotkey_delayed": "F5",  # no modifier
                "delay_seconds": 9999,
                "start_with_windows": "yes",  # wrong type
                "output_root": "   ",
                "unknown_key": 1,
            }
        ),
        encoding="utf-8",
    )
    s = load_settings(p)
    assert s.hotkey_capture == "Ctrl+Alt+S"
    assert s.hotkey_delayed == "Ctrl+Alt+D"
    assert s.delay_seconds == 30
    assert s.start_with_windows is False
    assert s.output_root.endswith("UIReports")


def test_duplicate_hotkeys_are_repaired_and_reported():
    s = Settings(hotkey_capture="Ctrl+Alt+D", hotkey_delayed="ctrl+alt+d")
    assert any("different" in p for p in s.problems())
    fixed = s.sanitized()
    assert fixed.hotkey_capture != fixed.hotkey_delayed


def test_problems_reports_bad_hotkeys_and_delay():
    s = Settings(hotkey_capture="S", delay_seconds=0)
    probs = s.problems()
    assert any("Capture hotkey" in p for p in probs)
    assert any("Delay" in p for p in probs)


def test_store_update_persists(test_out):
    store = SettingsStore(test_out / "store.json")
    assert store.settings.delay_seconds == 3
    store.update(delay_seconds=7, last_project_path=r"C:\p")
    again = SettingsStore(test_out / "store.json")
    assert again.settings.delay_seconds == 7 and again.settings.last_project_path == r"C:\p"
    try:
        store.update(nonsense=1)
    except AttributeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected AttributeError")


def test_remember_and_prefill_session():
    s = Settings()
    sess = Session.new(project_path=r"C:\dev\a", framework_hint="React + Tailwind")
    s.remember_session(sess)
    nxt = s.new_session()
    assert nxt.project_path == r"C:\dev\a" and nxt.framework_hint == "React + Tailwind"
    blank = Session.new()
    s.remember_session(blank)  # blank values must not erase what is remembered
    assert s.last_project_path == r"C:\dev\a"


def test_tests_never_touch_real_settings_folder(test_out):
    real = default_settings_path()
    before = real.stat().st_mtime if real.exists() else None
    save_settings(Settings(), test_out / "x.json")
    after = real.stat().st_mtime if real.exists() else None
    assert before == after
    assert os.fspath(test_out) not in os.fspath(real)
