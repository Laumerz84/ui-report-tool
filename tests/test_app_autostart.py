"""'Start with Windows' (package C). SAFETY: nothing here may touch the real registry Run key or the
Startup folder - only in-memory fakes and a mocked `winreg` module are used."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Second line of defence for the whole pytest process: the real backend refuses to write.
os.environ["UIREPORT_BLOCK_REGISTRY_WRITES"] = "1"

from uireport.app import autostart  # noqa: E402
from uireport.app.autostart import (  # noqa: E402
    BLOCK_ENV,
    RUN_KEY,
    VALUE_NAME,
    MemoryBackend,
    WinRegistryBackend,
    build_launch_command,
    is_enabled,
    set_enabled,
)


class DictBackend:
    """Minimal RegistryBackend whose delete() raises KeyError for a missing value (strict fake)."""

    def __init__(self):
        self.values: dict[str, str] = {}
        self.log: list[tuple] = []

    def get(self, value_name):
        return self.values.get(value_name)

    def set(self, value_name, command):
        self.log.append(("set", value_name, command))
        self.values[value_name] = command

    def delete(self, value_name):
        self.log.append(("delete", value_name))
        del self.values[value_name]  # KeyError when missing


def test_constants():
    assert RUN_KEY == r"Software\Microsoft\Windows\CurrentVersion\Run"
    assert VALUE_NAME == "UIReportTool"


def test_block_env_is_set_for_the_whole_test_process():
    assert os.environ[BLOCK_ENV] == "1"
    assert autostart.registry_writes_blocked()


def test_build_launch_command_quotes_both_paths():
    cmd = build_launch_command(
        Path(r"C:\Program Files\Tool\.venv\Scripts\pythonw.exe"), Path(r"C:\My Apps\ui report\launch.pyw")
    )
    assert cmd == r'"C:\Program Files\Tool\.venv\Scripts\pythonw.exe" "C:\My Apps\ui report\launch.pyw"'


def test_build_launch_command_defaults_point_at_this_project():
    cmd = build_launch_command()
    parts = [p for p in cmd.split('"') if p.strip()]
    assert len(parts) == 2 and cmd.startswith('"') and cmd.endswith('"')
    pythonw, launcher = Path(parts[0]), Path(parts[1])
    assert pythonw.is_absolute() and launcher.is_absolute()
    assert launcher.name == "launch.pyw" and launcher.is_file()
    assert launcher == Path(os.path.abspath(autostart.__file__)).parents[2] / "launch.pyw"  # next to the package
    assert pythonw.parent == Path(sys.executable).parent
    assert pythonw.name in ("pythonw.exe", Path(sys.executable).name)


def test_enable_disable_and_is_enabled_with_the_memory_backend():
    b = MemoryBackend()
    assert not is_enabled(b)
    set_enabled(True, b, command='"x" "y"')
    assert is_enabled(b)
    assert b.get(VALUE_NAME) == '"x" "y"'
    set_enabled(False, b)
    assert not is_enabled(b)
    assert b.get(VALUE_NAME) is None


def test_enable_uses_the_default_command_and_is_idempotent():
    b = MemoryBackend()
    set_enabled(True, b)
    first = b.get(VALUE_NAME)
    assert first == build_launch_command()
    set_enabled(True, b)
    assert b.get(VALUE_NAME) == first
    assert list(b.values) == [VALUE_NAME]


def test_removing_a_missing_entry_is_not_an_error_even_for_a_strict_backend():
    b = DictBackend()
    set_enabled(False, b)  # KeyError inside delete() is swallowed
    set_enabled(False, b)
    assert not is_enabled(b)
    set_enabled(True, b, command="c")
    set_enabled(False, b)
    set_enabled(False, b)
    assert b.log == [("delete", VALUE_NAME), ("delete", VALUE_NAME), ("set", VALUE_NAME, "c"),
                     ("delete", VALUE_NAME), ("delete", VALUE_NAME)]


def test_blank_value_counts_as_disabled():
    b = MemoryBackend()
    b.values[VALUE_NAME] = "   "
    assert not is_enabled(b)


# ---------------------------------------------------------------------------
# WinRegistryBackend against a MOCKED winreg module
# ---------------------------------------------------------------------------
class FakeKey:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values: dict[tuple[str, str, str], str] = {}
        self.calls: list[tuple] = []

    def OpenKey(self, hive, path, reserved=0, access=0):  # noqa: N802
        self.calls.append(("OpenKey", hive, path, access))
        return FakeKey(self, path)

    def CreateKeyEx(self, hive, path, reserved=0, access=0):  # noqa: N802
        self.calls.append(("CreateKeyEx", hive, path, access))
        return FakeKey(self, path)

    def QueryValueEx(self, key, name):  # noqa: N802
        try:
            return self.values[("HKCU", key.path, name)], self.REG_SZ
        except KeyError:
            raise FileNotFoundError(name)

    def SetValueEx(self, key, name, reserved, kind, value):  # noqa: N802
        self.calls.append(("SetValueEx", key.path, name, kind, value))
        self.values[("HKCU", key.path, name)] = value

    def DeleteValue(self, key, name):  # noqa: N802
        self.calls.append(("DeleteValue", key.path, name))
        try:
            del self.values[("HKCU", key.path, name)]
        except KeyError:
            raise FileNotFoundError(name)


@pytest.fixture
def fake_winreg(monkeypatch):
    fake = FakeWinreg()
    monkeypatch.setattr(autostart, "winreg", fake)
    assert autostart.winreg is fake  # from here on the real registry is unreachable
    return fake


def test_win_backend_refuses_writes_while_blocked(fake_winreg):
    b = WinRegistryBackend()
    with pytest.raises(PermissionError):
        b.set(VALUE_NAME, "cmd")
    with pytest.raises(PermissionError):
        b.delete(VALUE_NAME)
    assert fake_winreg.calls == []  # blocked before any registry call


def test_win_backend_talks_to_the_run_key_only(fake_winreg, monkeypatch):
    monkeypatch.delenv(BLOCK_ENV)  # safe: winreg is mocked (see fixture)
    b = WinRegistryBackend()
    assert b.get(VALUE_NAME) is None
    b.set(VALUE_NAME, '"a" "b"')
    assert b.get(VALUE_NAME) == '"a" "b"'
    assert is_enabled(b)
    set_enabled(False, b)
    set_enabled(False, b)  # missing value: FileNotFoundError is swallowed
    assert b.get(VALUE_NAME) is None
    paths = {c[2] if c[0] in ("OpenKey", "CreateKeyEx") else c[1] for c in fake_winreg.calls}
    assert paths == {RUN_KEY}
    assert ("SetValueEx", RUN_KEY, VALUE_NAME, FakeWinreg.REG_SZ, '"a" "b"') in fake_winreg.calls


def test_win_backend_without_winreg_raises_oserror(monkeypatch):
    monkeypatch.setattr(autostart, "winreg", None)
    with pytest.raises(OSError):
        WinRegistryBackend().get(VALUE_NAME)
