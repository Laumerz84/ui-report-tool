"""Persistent user settings (JSON under %APPDATA%\\UIReportTool by default).

Everything that touches the disk takes an optional path so tests and the smoke test
never touch the real settings folder:

    settings path resolution:  explicit arg  >  $UIREPORT_SETTINGS  >  %APPDATA%\\UIReportTool\\settings.json
    default output root:       $UIREPORT_OUTPUT  >  %USERPROFILE%\\UIReports
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

from . import APP_ID
from .hotkeyspec import normalize_hotkey, validate_hotkey
from .models import Session

SETTINGS_SCHEMA_VERSION = 1
DEFAULT_HOTKEY_CAPTURE = "Ctrl+Alt+S"
DEFAULT_HOTKEY_DELAYED = "Ctrl+Alt+D"
DEFAULT_DELAY_SECONDS = 3
MIN_DELAY_SECONDS = 1
MAX_DELAY_SECONDS = 30

ENV_SETTINGS = "UIREPORT_SETTINGS"
ENV_OUTPUT = "UIREPORT_OUTPUT"


def default_settings_path() -> Path:
    """%APPDATA%\\UIReportTool\\settings.json (falls back to ~/AppData/Roaming)."""
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / APP_ID / "settings.json"


def resolve_settings_path(path: Optional[str | os.PathLike[str]] = None) -> Path:
    if path:
        return Path(path)
    env = os.environ.get(ENV_SETTINGS)
    if env:
        return Path(env)
    return default_settings_path()


def default_output_root() -> Path:
    """%USERPROFILE%\\UIReports (overridable through $UIREPORT_OUTPUT)."""
    env = os.environ.get(ENV_OUTPUT)
    if env:
        return Path(env)
    return Path(os.environ.get("USERPROFILE") or str(Path.home())) / "UIReports"


@dataclass
class Settings:
    hotkey_capture: str = DEFAULT_HOTKEY_CAPTURE
    hotkey_delayed: str = DEFAULT_HOTKEY_DELAYED
    delay_seconds: int = DEFAULT_DELAY_SECONDS
    output_root: str = field(default_factory=lambda: str(default_output_root()))
    last_project_path: str = ""
    last_framework_hint: str = ""
    start_with_windows: bool = False
    copy_text_template: str = ""  # text copied on Finish; "" = the built-in Claude prompt
    paste_into_claude: bool = True  # on Finish, also paste into the Claude desktop app if it is open

    # ---- validation -----------------------------------------------------
    def problems(self) -> list[str]:
        """Human-readable problems with the current values (empty list = all good)."""
        out: list[str] = []
        for label, value in (("Capture", self.hotkey_capture), ("Delayed capture", self.hotkey_delayed)):
            err = validate_hotkey(value)
            if err:
                out.append(f"{label} hotkey: {err}")
        if not out:
            if normalize_hotkey(self.hotkey_capture) == normalize_hotkey(self.hotkey_delayed):
                out.append("The capture and delayed-capture hotkeys must be different.")
        if not (MIN_DELAY_SECONDS <= self.delay_seconds <= MAX_DELAY_SECONDS):
            out.append(f"Delay must be between {MIN_DELAY_SECONDS} and {MAX_DELAY_SECONDS} seconds.")
        if not str(self.output_root).strip():
            out.append("Output folder must not be empty.")
        return out

    def sanitized(self) -> "Settings":
        """A copy with every invalid value replaced by its default and hotkeys normalised."""
        defaults = Settings()
        s = Settings(**{f.name: getattr(self, f.name) for f in fields(self)})
        for attr, default in (
            ("hotkey_capture", defaults.hotkey_capture),
            ("hotkey_delayed", defaults.hotkey_delayed),
        ):
            value = getattr(s, attr)
            if validate_hotkey(value) is None:
                setattr(s, attr, normalize_hotkey(value))
            else:
                setattr(s, attr, default)
        if s.hotkey_capture == s.hotkey_delayed:
            s.hotkey_delayed = defaults.hotkey_delayed if defaults.hotkey_delayed != s.hotkey_capture else defaults.hotkey_capture
        try:
            s.delay_seconds = int(s.delay_seconds)
        except (TypeError, ValueError):
            s.delay_seconds = defaults.delay_seconds
        s.delay_seconds = max(MIN_DELAY_SECONDS, min(MAX_DELAY_SECONDS, s.delay_seconds))
        if not str(s.output_root).strip():
            s.output_root = defaults.output_root
        s.last_project_path = str(s.last_project_path or "")
        s.last_framework_hint = str(s.last_framework_hint or "")
        s.start_with_windows = bool(s.start_with_windows)
        s.copy_text_template = str(s.copy_text_template or "")
        s.paste_into_claude = bool(s.paste_into_claude)
        return s

    # ---- (de)serialisation ---------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        d["schema_version"] = SETTINGS_SCHEMA_VERSION
        return d

    @classmethod
    def from_dict(cls, d: Any) -> "Settings":
        """Tolerant: unknown keys ignored, wrong types replaced by defaults, then sanitised."""
        base = cls()
        if not isinstance(d, dict):
            return base
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in d:
                continue
            value = d[f.name]
            default = getattr(base, f.name)
            if isinstance(default, bool):
                kwargs[f.name] = value if isinstance(value, bool) else default
            elif isinstance(default, int):
                kwargs[f.name] = value if isinstance(value, int) and not isinstance(value, bool) else default
            elif isinstance(default, str):
                kwargs[f.name] = value if isinstance(value, str) else default
        return cls(**{**{f.name: getattr(base, f.name) for f in fields(cls)}, **kwargs}).sanitized()

    # ---- session glue ---------------------------------------------------
    def new_session(self) -> Session:
        """A fresh Session pre-filled with the remembered project path / framework hint."""
        return Session.new(project_path=self.last_project_path, framework_hint=self.last_framework_hint)

    def remember_session(self, session: Session) -> None:
        """Remember project path + framework hint for the next session (blank values keep the old)."""
        if session.project_path.strip():
            self.last_project_path = session.project_path.strip()
        if session.framework_hint.strip():
            self.last_framework_hint = session.framework_hint.strip()


def load_settings(path: Optional[str | os.PathLike[str]] = None) -> Settings:
    """Load settings; a missing, unreadable or corrupt file yields defaults (never raises)."""
    p = resolve_settings_path(path)
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return Settings.from_dict(json.load(fh))
    except (OSError, ValueError):
        return Settings().sanitized()


def save_settings(settings: Settings, path: Optional[str | os.PathLike[str]] = None) -> Path:
    """Atomically write settings JSON (temp file + replace). Creates parent folders."""
    p = resolve_settings_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(settings.sanitized().to_dict(), indent=2, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


class SettingsStore:
    """Small convenience wrapper: holds the current Settings plus the file path."""

    def __init__(self, path: Optional[str | os.PathLike[str]] = None) -> None:
        self.path: Path = resolve_settings_path(path)
        self.settings: Settings = load_settings(self.path)

    def reload(self) -> Settings:
        self.settings = load_settings(self.path)
        return self.settings

    def save(self) -> Path:
        self.settings = self.settings.sanitized()
        return save_settings(self.settings, self.path)

    def update(self, **changes: Any) -> Settings:
        """Set attributes, sanitise and save in one go."""
        for k, v in changes.items():
            if not hasattr(self.settings, k):
                raise AttributeError(f"Unknown setting {k!r}")
            setattr(self.settings, k, v)
        self.save()
        return self.settings
