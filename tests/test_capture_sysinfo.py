"""sysinfo: version formatting, theme registry reads (read-only) and the per-session bundle."""
from __future__ import annotations

import inspect

import pytest

from uireport import __version__
from uireport.capture import sysinfo
from uireport.geometry import IntRect
from uireport.models import MonitorMeta, SystemMeta


@pytest.mark.parametrize(
    "product,major,minor,build,expected",
    [
        ("Windows 10 Pro", 10, 0, 26200, ("Windows 11 Pro 10.0.26200", 26200)),  # ProductName lies on Win 11
        ("Windows 10 Home", 10, 0, 22000, ("Windows 11 Home 10.0.22000", 22000)),  # boundary: 22000 is Windows 11
        ("Windows 10 Pro", 10, 0, 21999, ("Windows 10 Pro 10.0.21999", 21999)),
        ("Windows 10 Enterprise LTSC", 10, 0, 19044, ("Windows 10 Enterprise LTSC 10.0.19044", 19044)),
        ("Windows 11 Education", 10, 0, 26100, ("Windows 11 Education 10.0.26100", 26100)),
        ("", 10, 0, 22631, ("Windows 11 10.0.22631", 22631)),
    ],
)
def test_format_windows_version(product, major, minor, build, expected):
    assert sysinfo.format_windows_version(product, major, minor, build) == expected


def test_windows_version_string_on_this_machine():
    text, build = sysinfo.windows_version_string()
    assert isinstance(text, str) and text.startswith("Windows ")
    assert build > 0 and str(build) in text
    assert ("Windows 11" in text) == (build >= 22000)


@pytest.mark.parametrize(
    "apps,system,expected",
    [(0, 0, ("dark", "dark")), (1, 1, ("light", "light")), (0, 1, ("dark", "light")), (None, None, ("unknown", "unknown"))],
)
def test_theme_modes_from_registry_values(monkeypatch, apps, system, expected):
    values = {"AppsUseLightTheme": apps, "SystemUsesLightTheme": system}
    calls = []

    def fake(hive, path, value):
        calls.append((hive, path, value))
        return values.get(value)

    monkeypatch.setattr(sysinfo, "_read_reg", fake)
    assert sysinfo.theme_modes() == expected
    assert all(h == "HKEY_CURRENT_USER" and p.endswith("Themes\\Personalize") for h, p, v in calls)


def test_theme_modes_never_raise(monkeypatch):
    def boom(*a):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(sysinfo, "_read_reg", boom)
    assert sysinfo.theme_modes() == ("unknown", "unknown")


def test_real_theme_values_are_valid_words():
    apps, system = sysinfo.theme_modes()
    assert apps in ("light", "dark", "unknown") and system in ("light", "dark", "unknown")


def test_registry_access_is_read_only():
    src = inspect.getsource(sysinfo)
    for forbidden in ("SetValueEx", "CreateKey", "DeleteKey", "DeleteValue", "KEY_WRITE", "KEY_ALL_ACCESS"):
        assert forbidden not in src


def test_collect_system_meta_with_injected_monitors():
    mons = [MonitorMeta(index=1, name=r"\\.\DISPLAY1", is_primary=True, rect=IntRect(0, 0, 3840, 2160), dpi=144)]
    meta = sysinfo.collect_system_meta(mons)
    assert isinstance(meta, SystemMeta)
    assert meta.monitors == mons and meta.monitors is not mons
    assert meta.tool_version == __version__
    assert meta.windows_version.startswith("Windows ") and meta.windows_build > 0
    assert meta.theme_apps in ("light", "dark", "unknown")
    assert meta.to_dict()["monitors"][0]["scale_percent"] == 150


def test_collect_system_meta_enumerates_when_no_monitors_given(qapp):
    meta = sysinfo.collect_system_meta()
    assert len(meta.monitors) >= 1 and meta.monitors[0].rect.w > 0
