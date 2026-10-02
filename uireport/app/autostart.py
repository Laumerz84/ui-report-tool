"""'Start with Windows' toggle. Owner: output/app builder (package C).

SAFETY: tests and the smoke test must NEVER touch the real HKCU Run key or the Startup
folder. Every function takes an injectable backend; tests pass an in-memory fake. As a second
line of defence WinRegistryBackend.set/delete refuse to run while the environment variable
UIREPORT_BLOCK_REGISTRY_WRITES=1 is set (the test suite and --smoke-test set it).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional, Protocol

try:  # winreg only exists on Windows; keep the module importable everywhere
    import winreg  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "UIReportTool"
BLOCK_ENV = "UIREPORT_BLOCK_REGISTRY_WRITES"


class RegistryBackend(Protocol):
    def get(self, value_name: str) -> Optional[str]: ...
    def set(self, value_name: str, command: str) -> None: ...
    def delete(self, value_name: str) -> None: ...


def registry_writes_blocked() -> bool:
    return os.environ.get(BLOCK_ENV, "") == "1"


class WinRegistryBackend:
    """Real backend: winreg on HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run."""

    def _require_winreg(self):
        if winreg is None:
            raise OSError("the Windows registry is not available on this platform")
        return winreg

    def get(self, value_name: str) -> Optional[str]:
        reg = self._require_winreg()
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_READ) as key:
                value, _type = reg.QueryValueEx(key, value_name)
        except FileNotFoundError:
            return None
        return str(value) if value is not None else None

    def set(self, value_name: str, command: str) -> None:
        if registry_writes_blocked():
            raise PermissionError(f"registry writes are blocked ({BLOCK_ENV}=1)")
        reg = self._require_winreg()
        with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
            reg.SetValueEx(key, value_name, 0, reg.REG_SZ, command)

    def delete(self, value_name: str) -> None:
        if registry_writes_blocked():
            raise PermissionError(f"registry writes are blocked ({BLOCK_ENV}=1)")
        reg = self._require_winreg()
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
                reg.DeleteValue(key, value_name)
        except FileNotFoundError:
            pass  # nothing to remove


def project_root() -> Path:
    """The folder that contains launch.pyw (two levels above uireport/app/autostart.py)."""
    return Path(os.path.abspath(__file__)).parents[2]


def build_launch_command(pythonw: Optional[Path] = None, launcher: Optional[Path] = None) -> str:
    """'"<...\\.venv\\Scripts\\pythonw.exe>" "<project>\\launch.pyw"' - both quoted, absolute.
    Defaults: pythonw next to sys.executable (pythonw.exe if it exists, else sys.executable),
    launcher = <project root>\\launch.pyw."""
    if pythonw is None:
        exe = Path(sys.executable)
        candidate = exe.with_name("pythonw.exe")
        pythonw = candidate if candidate.exists() else exe
    if launcher is None:
        launcher = project_root() / "launch.pyw"
    py = os.path.abspath(str(pythonw))
    ln = os.path.abspath(str(launcher))
    return f'"{py}" "{ln}"'


def is_enabled(backend: Optional[RegistryBackend] = None) -> bool:
    b = backend if backend is not None else WinRegistryBackend()
    value = b.get(VALUE_NAME)
    return bool(value and str(value).strip())


def set_enabled(
    enabled: bool, backend: Optional[RegistryBackend] = None, command: Optional[str] = None
) -> None:
    """Add (value = command or build_launch_command()) or remove the Run entry. Removing
    a missing entry is not an error."""
    b = backend if backend is not None else WinRegistryBackend()
    if enabled:
        b.set(VALUE_NAME, command or build_launch_command())
        return
    try:
        b.delete(VALUE_NAME)
    except (KeyError, FileNotFoundError):
        pass


class MemoryBackend:
    """In-memory RegistryBackend (used by tests, the smoke test and as a safe stand-in)."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, value_name: str) -> Optional[str]:
        return self.values.get(value_name)

    def set(self, value_name: str, command: str) -> None:
        self.values[value_name] = command

    def delete(self, value_name: str) -> None:
        self.values.pop(value_name, None)
