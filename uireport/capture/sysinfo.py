"""Per-session environment facts. Owner: capture builder (package A).

Registry access is strictly READ-ONLY (winreg.OpenKey with KEY_READ)."""
from __future__ import annotations

import platform
import re
import sys
from typing import Any, Optional

from .. import __version__
from ..models import MonitorMeta, SystemMeta

_NT_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
_PERSONALIZE_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
WINDOWS_11_MIN_BUILD = 22000


def _read_reg(hive_name: str, path: str, value: str) -> Optional[Any]:
    """Read one registry value (read-only). Returns None if anything is missing/unreadable."""
    try:
        import winreg

        hive = getattr(winreg, hive_name)
        with winreg.OpenKey(hive, path, 0, winreg.KEY_READ) as key:
            data, _type = winreg.QueryValueEx(key, value)
            return data
    except (OSError, ImportError, AttributeError):
        return None


def _os_version_tuple() -> tuple[int, int, int]:
    """(major, minor, build) of the running Windows."""
    try:
        v = sys.getwindowsversion()  # type: ignore[attr-defined]
        return (int(v.major), int(v.minor), int(v.build))
    except AttributeError:
        pass
    m = re.match(r"(\d+)\.(\d+)\.(\d+)", platform.version())
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return (0, 0, 0)


def format_windows_version(product_name: str, major: int, minor: int, build: int) -> tuple[str, int]:
    """Pure formatter: ('Windows 11 Pro 10.0.26200', 26200).

    HKLM ProductName still says 'Windows 10 ...' on Windows 11, so the Windows generation
    comes from the build number (>= 22000 = Windows 11) and only the edition word
    (Pro / Home / Enterprise / Education ...) is taken from ProductName."""
    generation = 11 if build >= WINDOWS_11_MIN_BUILD else 10 if major >= 10 else major
    edition = re.sub(r"^\s*Windows\s+(?:\d+(?:\.\d+)?\s*)?", "", product_name or "", flags=re.I).strip()
    parts = [f"Windows {generation}" if generation else "Windows", edition, f"{major}.{minor}.{build}"]
    return (" ".join(p for p in parts if p), int(build))


def windows_version_string() -> tuple[str, int]:
    """(e.g. 'Windows 11 Pro 10.0.26200', 26200). Windows 11 = build >= 22000. Never raises."""
    try:
        major, minor, build = _os_version_tuple()
        product = _read_reg("HKEY_LOCAL_MACHINE", _NT_KEY, "ProductName")
        build_reg = _read_reg("HKEY_LOCAL_MACHINE", _NT_KEY, "CurrentBuildNumber")
        if build_reg is not None:
            try:
                build = int(str(build_reg))
            except ValueError:
                pass
        return format_windows_version(str(product or ""), major, minor, build)
    except Exception:  # noqa: BLE001
        return ("Windows", 0)


def _theme_word(value: Optional[Any]) -> str:
    if value is None:
        return "unknown"
    try:
        return "light" if int(value) != 0 else "dark"
    except (TypeError, ValueError):
        return "unknown"


def theme_modes() -> tuple[str, str]:
    """(apps_theme, system_theme), each 'light' | 'dark' | 'unknown', read (read-only)
    from HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize
    (AppsUseLightTheme / SystemUsesLightTheme; 0 = dark, 1 = light)."""
    try:
        apps = _theme_word(_read_reg("HKEY_CURRENT_USER", _PERSONALIZE_KEY, "AppsUseLightTheme"))
        system = _theme_word(_read_reg("HKEY_CURRENT_USER", _PERSONALIZE_KEY, "SystemUsesLightTheme"))
        return (apps, system)
    except Exception:  # noqa: BLE001
        return ("unknown", "unknown")


def collect_system_meta(monitors: Optional[list[MonitorMeta]] = None) -> SystemMeta:
    """SystemMeta with windows version/build, themes, the monitor layout (calls
    enumerate_monitors() when `monitors` is None) and the tool version."""
    if monitors is None:
        try:
            from .monitors import enumerate_monitors

            monitors = enumerate_monitors()
        except Exception:  # noqa: BLE001
            monitors = []
    version, build = windows_version_string()
    apps, system = theme_modes()
    return SystemMeta(
        windows_version=version,
        windows_build=build,
        theme_apps=apps,
        theme_system=system,
        monitors=list(monitors),
        tool_version=__version__,
    )
