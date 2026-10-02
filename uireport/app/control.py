"""--quit support. Owner: output/app builder (package C).

The running copy owns a named, auto-reset Win32 event (per Windows session: ``Local\\``) and a
watcher thread; another process sets that event to ask it to quit, which runs exactly the same
path as the tray's Quit (so unsaved shots still get the Finish / Discard / Cancel question).
No sockets or pipes are involved - the app stays local-only.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from PySide6.QtCore import QObject, Signal

log = logging.getLogger("uireport")

EVENT_MODIFY_STATE = 0x0002


def event_name(instance_name: str) -> str:
    """Kernel object name for an instance (the name is already sanitised by SingleInstance)."""
    return f"Local\\{instance_name}-quit"


class QuitListener(QObject):
    """Emits ``quit_requested`` on the thread that owns this object (the GUI thread) every time
    another process calls :func:`request_quit` with the same instance name."""

    quit_requested = Signal()

    def __init__(self, instance_name: str, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._name = event_name(instance_name)
        self._event = None
        self._stop_event = None
        self._thread: Optional[threading.Thread] = None

    @property
    def is_listening(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """Create the event and start watching it. False (and a log line) if that is impossible."""
        try:
            import win32event

            self._event = win32event.CreateEvent(None, False, False, self._name)
            self._stop_event = win32event.CreateEvent(None, True, False, None)
        except Exception as exc:  # ImportError off Windows, pywintypes.error otherwise
            log.warning("--quit listener unavailable: %s", exc)
            self._close_handles()
            return False
        self._thread = threading.Thread(target=self._watch, name="uireport-quit-listener", daemon=True)
        self._thread.start()
        return True

    def _watch(self) -> None:
        import win32event

        handles = [self._stop_event, self._event]
        while True:
            rc = win32event.WaitForMultipleObjects(handles, False, win32event.INFINITE)
            if rc != win32event.WAIT_OBJECT_0 + 1:
                return  # stop requested (or the wait failed)
            log.info("quit requested by another process (--quit)")
            # emitted from this worker thread: Qt queues it to the receivers' (GUI) thread
            self.quit_requested.emit()

    def stop(self) -> None:
        """Stop watching and release the event, so --quit no longer finds this copy."""
        if self._stop_event is not None:
            try:
                import win32event

                win32event.SetEvent(self._stop_event)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(2.0)
            self._thread = None
        self._close_handles()

    def _close_handles(self) -> None:
        for attr in ("_event", "_stop_event"):
            h = getattr(self, attr)
            setattr(self, attr, None)
            if h is not None:
                try:
                    h.Close()
                except Exception:
                    pass


def request_quit(instance_name: str) -> bool:
    """Ask the running copy with this instance name to quit. True if one was listening."""
    try:
        import win32api
        import win32event
    except ImportError:
        return False
    try:
        handle = win32event.OpenEvent(EVENT_MODIFY_STATE, False, event_name(instance_name))
    except Exception:  # pywintypes.error: no such event -> nobody is listening
        return False
    try:
        win32event.SetEvent(handle)
    finally:
        win32api.CloseHandle(handle)
    return True
