"""Contract check (owned by the lead; builders must not edit it).

Every module in CONTRACT.md must import, expose the agreed public names, and keep the
agreed parameter names IN ORDER (a builder may append extra OPTIONAL parameters, never
rename, reorder or drop). Qt classes must expose the agreed signals.
Runs against the stubs today and against the real implementations later.
"""
from __future__ import annotations

import importlib
import inspect

import pytest

# module -> {public name: [param names] for callables/classes (__init__ params, no self);
#            None = just must exist}
CALLABLES: dict[str, dict[str, list[str] | None]] = {
    "uireport.capture.winapi": {
        "enable_per_monitor_dpi_awareness": [],
        "exclude_from_capture": ["hwnd", "exclude"],
        "widget_hwnd": ["widget"],
        "dwm_flush": [],
        "cursor_pos": [],
        "is_key_down": ["vk"],
    },
    "uireport.capture.monitors": {
        "enumerate_monitors": [],
        "virtual_screen_rect": [],
        "find_qscreen": ["monitor"],
    },
    "uireport.capture.grab": {
        "grab_virtual_screen": [],
        "crop_image": ["frozen", "frozen_origin", "rect"],
    },
    "uireport.capture.windowinfo": {
        "WindowSnapshot": ["hwnd", "rect", "title", "pid"],
        "snapshot_top_level_windows": ["exclude_pid"],
        "window_at_point": ["windows", "x", "y"],
        "foreground_hwnd": ["exclude_pid"],
        "browser_kind": ["process_name"],
        "window_meta_from_hwnd": ["hwnd", "monitors"],
        "read_browser_url": ["hwnd", "browser", "timeout_s"],
    },
    "uireport.capture.sysinfo": {
        "windows_version_string": [],
        "theme_modes": [],
        "collect_system_meta": ["monitors"],
    },
    "uireport.capture.selector": {
        "Selection": ["kind", "rect", "monitor", "window"],
        "RegionSelector": ["frozen", "frozen_origin", "monitors", "windows", "parent"],
    },
    "uireport.capture.countdown": {"CountdownOverlay": ["seconds", "monitor", "parent"]},
    "uireport.capture.hotkeys": {"HotkeyManager": ["parent", "backend"]},
    "uireport.capture.service": {"CaptureService": ["get_settings", "parent"]},
    "uireport.editor.canvas": {"Tool": None, "AnnotationCanvas": ["parent"]},
    "uireport.editor.session_panel": {"SessionPanel": ["session", "parent"]},
    "uireport.editor.session_fields": {"SessionFieldsWidget": ["session", "parent"]},
    "uireport.editor.editor_window": {"EditorWindow": ["session", "parent"]},
    "uireport.output.naming": {
        "slugify": ["text", "max_len"],
        "session_folder_name": ["session", "now"],
        "unique_folder": ["root", "name"],
        "shot_filenames": ["index", "total"],
    },
    "uireport.output.render": {
        "render_original": ["shot"],
        "render_annotated": ["shot"],
        "save_png": ["image", "path"],
    },
    "uireport.output.report": {
        "build_report_dict": ["session"],
        "build_report_md": ["session", "folder"],
    },
    "uireport.output.writer": {
        "SessionOutput": ["folder", "report_md", "report_json", "image_files"],
        "write_session": ["session", "output_root", "now", "progress"],
        "write_latest_pointer": ["output_root", "out", "session"],
    },
    "uireport.output.clipboard": {
        "build_prompt": ["shot_count", "goal", "report_md_path"],
        "copy_prompt_to_clipboard": ["text"],
    },
    "uireport.app.controller": {
        "AppController": [
            "store", "capture", "hotkeys", "editor_factory", "tray", "toast",
            "settings_window_factory", "autostart", "enable_hotkeys", "parent",
        ],
    },
    "uireport.app.tray": {"TrayIcon": ["parent"]},
    "uireport.app.toast": {"Toast": ["parent"]},
    "uireport.app.settings_window": {"SettingsWindow": ["store", "parent"]},
    "uireport.app.autostart": {
        "RUN_KEY": None,
        "VALUE_NAME": None,
        "WinRegistryBackend": None,
        "build_launch_command": ["pythonw", "launcher"],
        "is_enabled": ["backend"],
        "set_enabled": ["enabled", "backend", "command"],
    },
    "uireport.app.icons": {"make_app_icon": ["size"]},
    "uireport.app.main": {"parse_args": ["argv"], "main": ["argv"]},
}

SIGNALS: dict[tuple[str, str], list[str]] = {
    ("uireport.capture.selector", "RegionSelector"): ["selected", "cancelled"],
    ("uireport.capture.countdown", "CountdownOverlay"): ["finished", "cancelled"],
    ("uireport.capture.hotkeys", "HotkeyManager"): ["activated", "registration_failed"],
    ("uireport.capture.service", "CaptureService"): ["captured", "cancelled", "failed"],
    ("uireport.editor.canvas", "AnnotationCanvas"): ["annotations_changed", "tool_changed"],
    ("uireport.editor.session_panel", "SessionPanel"): [
        "shot_selected", "shot_delete_requested", "order_changed",
    ],
    ("uireport.editor.session_fields", "SessionFieldsWidget"): ["changed"],
    ("uireport.editor.editor_window", "EditorWindow"): [
        "next_requested", "next_delayed_requested", "finish_requested",
        "discard_session_requested", "session_changed",
    ],
    ("uireport.app.controller", "AppController"): [
        "session_finished", "finish_failed", "quit_requested",
    ],
    ("uireport.app.tray", "TrayIcon"): [
        "capture_requested", "delayed_requested", "settings_requested",
        "open_folder_requested", "show_session_requested", "quit_requested",
    ],
    ("uireport.app.settings_window", "SettingsWindow"): ["applied"],
}

METHODS: dict[tuple[str, str], list[str]] = {
    ("uireport.capture.hotkeys", "HotkeyManager"): ["start", "stop", "set_hotkeys", "registered"],
    ("uireport.capture.service", "CaptureService"): ["is_active", "start", "cancel"],
    ("uireport.capture.selector", "RegionSelector"): ["start", "close"],
    ("uireport.capture.countdown", "CountdownOverlay"): ["start", "hide", "close"],
    ("uireport.editor.canvas", "AnnotationCanvas"): [
        "set_shot", "set_tool", "tool", "undo", "redo", "delete_selected",
        "widget_to_image", "image_to_widget",
    ],
    ("uireport.editor.session_panel", "SessionPanel"): ["set_session", "refresh", "set_current"],
    ("uireport.editor.session_fields", "SessionFieldsWidget"): ["set_session", "refresh"],
    ("uireport.editor.editor_window", "EditorWindow"): [
        "set_session", "refresh", "show_shot", "current_shot_id", "present", "hide_for_capture",
    ],
    ("uireport.app.controller", "AppController"): [
        "session", "start", "shutdown", "request_capture", "finish_session",
        "discard_session", "show_settings", "show_session", "open_reports_folder", "quit",
    ],
    ("uireport.app.tray", "TrayIcon"): ["set_session_count", "set_hotkey_hint"],
    ("uireport.app.toast", "Toast"): ["show_toast", "dismiss"],
    ("uireport.app.settings_window", "SettingsWindow"): ["show_error", "current_settings"],
}


def _params(obj) -> list[str]:
    target = obj.__init__ if inspect.isclass(obj) else obj
    names = [
        p.name
        for p in inspect.signature(target).parameters.values()
        if p.name != "self" and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
    ]
    return names


@pytest.mark.parametrize("module", sorted(CALLABLES))
def test_module_imports_and_signatures(module):
    mod = importlib.import_module(module)
    for name, expected in CALLABLES[module].items():
        assert hasattr(mod, name), f"{module}.{name} missing"
        if expected is None:
            continue
        actual = _params(getattr(mod, name))
        assert actual[: len(expected)] == expected, (
            f"{module}.{name}: parameters {actual} do not start with the contract {expected}"
        )


@pytest.mark.parametrize("key", sorted(SIGNALS))
def test_signals_exist(key):
    module, cls_name = key
    cls = getattr(importlib.import_module(module), cls_name)
    for sig in SIGNALS[key]:
        assert hasattr(cls, sig), f"{module}.{cls_name}.{sig} signal missing"


@pytest.mark.parametrize("key", sorted(METHODS))
def test_methods_exist(key):
    module, cls_name = key
    cls = getattr(importlib.import_module(module), cls_name)
    for m in METHODS[key]:
        assert hasattr(cls, m), f"{module}.{cls_name}.{m} missing"


def test_package_entry_points_import():
    import uireport.__main__  # noqa: F401
    import uireport.capture  # noqa: F401
    import uireport.editor  # noqa: F401
