"""Package A (capture). Owner: capture builder. See CONTRACT.md sections 3.A, 4, 5, 6.

Public surface used by the other packages:
    CaptureService, HotkeyManager, enumerate_monitors, collect_system_meta,
    enable_per_monitor_dpi_awareness, exclude_from_capture
"""
from .hotkeys import HotkeyManager
from .monitors import enumerate_monitors
from .service import CaptureService
from .sysinfo import collect_system_meta
from .winapi import enable_per_monitor_dpi_awareness, exclude_from_capture

__all__ = [
    "CaptureService",
    "HotkeyManager",
    "enumerate_monitors",
    "collect_system_meta",
    "enable_per_monitor_dpi_awareness",
    "exclude_from_capture",
]
