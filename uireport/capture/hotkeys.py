"""Global hotkeys. Owner: capture builder (package A).

Threading: RegisterHotKey/UnregisterHotKey and the WM_HOTKEY message loop live on ONE
dedicated background thread (started by start()). That thread never touches Qt widgets;
when a hotkey fires it only emits `activated`. The signal is delivered to receivers on
the Qt main thread (receivers connect with Qt.QueuedConnection).

Layout:
    Win32HotkeyBackend  - the real backend: one daemon thread with its own GetMessage loop.
                          Commands (register/unregister/stop) reach it with
                          PostThreadMessage + a queue. This is also the test seam: tests pass
                          a fake object with the same small interface (see `HotkeyBackend`).
    HotkeyManager       - name -> hotkey bookkeeping, conflict handling, Qt signals.
    TemporaryHotkey     - a short-lived single global hotkey (used to catch Esc during the
                          delayed-capture countdown without leaking it to the target app).
"""
from __future__ import annotations

import ctypes
import queue
import threading
from ctypes import wintypes
from typing import Any, Callable, Optional, Protocol

from PySide6.QtCore import QObject, Signal

from .. import hotkeyspec
from . import winapi

WM_QUIT = 0x0012
WM_USER = 0x0400
WM_HOTKEY = 0x0312
WM_APP = 0x8000
WM_COMMAND_QUEUE = WM_APP + 1  # "look at the command queue"
PM_NOREMOVE = 0x0000
ERROR_HOTKEY_ALREADY_REGISTERED = 1409
ERROR_INVALID_PARAMETER = 87
ERROR_TIMEOUT_LOCAL = -1  # our own "hotkey thread did not answer"
ERROR_NOT_RUNNING_LOCAL = -2

_COMMAND_TIMEOUT_S = 2.0
_JOIN_TIMEOUT_S = 1.0

_user32 = winapi.user32
_kernel32 = winapi.kernel32

if _user32 is not None:
    _user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    _user32.RegisterHotKey.restype = wintypes.BOOL
    _user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.UnregisterHotKey.restype = wintypes.BOOL
    _user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT
    ]
    _user32.PeekMessageW.restype = wintypes.BOOL
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.TranslateMessage.restype = wintypes.BOOL
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.DispatchMessageW.restype = ctypes.c_ssize_t
    _user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.PostThreadMessageW.restype = wintypes.BOOL


class HotkeyBackend(Protocol):
    """Interface HotkeyManager needs. `Win32HotkeyBackend` is the real one; unit tests use a
    fake that never touches Win32."""

    def start(self, on_hotkey: Callable[[int], None]) -> None: ...

    def stop(self) -> None: ...

    def register(self, hk_id: int, modifiers: int, vk: int) -> Optional[int]:
        """Register (modifiers already include MOD_NOREPEAT). None = success, else a Win32
        error code (ERROR_HOTKEY_ALREADY_REGISTERED = 1409 when another program owns it)."""
        ...

    def unregister(self, hk_id: int) -> None: ...


class _Command:
    __slots__ = ("kind", "hk_id", "modifiers", "vk", "done", "result")

    def __init__(self, kind: str, hk_id: int = 0, modifiers: int = 0, vk: int = 0) -> None:
        self.kind = kind
        self.hk_id = hk_id
        self.modifiers = modifiers
        self.vk = vk
        self.done = threading.Event()
        self.result: Optional[int] = None


class Win32HotkeyBackend:
    """Real backend: a daemon thread that registers hotkeys with RegisterHotKey(NULL, ...)
    and pumps its own message loop. RegisterHotKey/UnregisterHotKey MUST run on the thread
    that receives WM_HOTKEY, so the public methods only post a command and wait."""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._commands: "queue.Queue[_Command]" = queue.Queue()
        self._callback: Callable[[int], None] = lambda _id: None
        self._lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------------------
    @property
    def thread_id(self) -> int:
        """Win32 thread id of the message-loop thread (0 when not running). Tests use it to
        PostThreadMessage(WM_HOTKEY, ...) to OUR thread; never to send real input."""
        return self._thread_id

    @property
    def is_running(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive() and self._thread_id)

    def start(self, on_hotkey: Callable[[int], None]) -> None:
        with self._lock:
            if self.is_running:
                self._callback = on_hotkey
                return
            if _user32 is None:
                raise OSError("Global hotkeys are only available on Windows")
            self._callback = on_hotkey
            self._ready.clear()
            self._thread_id = 0
            self._thread = threading.Thread(target=self._run, name="uireport-hotkeys", daemon=True)
            self._thread.start()
        if not self._ready.wait(_COMMAND_TIMEOUT_S) or not self._thread_id:
            raise OSError("The hotkey thread did not start")

    def stop(self) -> None:
        with self._lock:
            t = self._thread
            tid = self._thread_id
            self._thread = None
        if t is None:
            return
        if tid and _user32 is not None:
            _user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
        if t is not threading.current_thread():
            t.join(_JOIN_TIMEOUT_S)
        self._thread_id = 0
        self._fail_pending()

    # -- commands (called from the GUI thread) ----------------------------------------------
    def _submit(self, cmd: _Command) -> Optional[int]:
        tid = self._thread_id
        if not tid or not self.is_running:
            return ERROR_NOT_RUNNING_LOCAL
        self._commands.put(cmd)
        if not _user32.PostThreadMessageW(tid, WM_COMMAND_QUEUE, 0, 0):
            return ERROR_NOT_RUNNING_LOCAL
        if not cmd.done.wait(_COMMAND_TIMEOUT_S):
            return ERROR_TIMEOUT_LOCAL
        return cmd.result

    def register(self, hk_id: int, modifiers: int, vk: int) -> Optional[int]:
        return self._submit(_Command("register", hk_id, modifiers, vk))

    def unregister(self, hk_id: int) -> None:
        self._submit(_Command("unregister", hk_id))

    def post_hotkey(self, hk_id: int, modifiers: int = 0, vk: int = 0) -> bool:
        """TEST HELPER: post a WM_HOTKEY message to our own hotkey thread, exactly what the
        system does on a real key press (no input is synthesised)."""
        tid = self._thread_id
        if not tid or _user32 is None:
            return False
        return bool(_user32.PostThreadMessageW(tid, WM_HOTKEY, hk_id, (vk << 16) | (modifiers & 0xFFFF)))

    # -- the hotkey thread -------------------------------------------------------------------
    def _fail_pending(self) -> None:
        while True:
            try:
                cmd = self._commands.get_nowait()
            except queue.Empty:
                return
            cmd.result = ERROR_NOT_RUNNING_LOCAL
            cmd.done.set()

    def _run_command(self, cmd: _Command, owned: set[int]) -> None:
        try:
            if cmd.kind == "register":
                ctypes.set_last_error(0)
                if _user32.RegisterHotKey(None, cmd.hk_id, cmd.modifiers, cmd.vk):
                    owned.add(cmd.hk_id)
                    cmd.result = None
                else:
                    cmd.result = ctypes.get_last_error() or ERROR_INVALID_PARAMETER
            elif cmd.kind == "unregister":
                if cmd.hk_id in owned:
                    _user32.UnregisterHotKey(None, cmd.hk_id)
                    owned.discard(cmd.hk_id)
                cmd.result = None
        except Exception:  # noqa: BLE001
            cmd.result = ERROR_INVALID_PARAMETER
        finally:
            cmd.done.set()

    def _run(self) -> None:
        msg = wintypes.MSG()
        owned: set[int] = set()
        try:
            _user32.PeekMessageW(ctypes.byref(msg), None, WM_USER, WM_USER, PM_NOREMOVE)  # create the queue
            self._thread_id = int(_kernel32.GetCurrentThreadId())
            self._ready.set()
            while True:
                r = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r == 0 or r == -1:  # WM_QUIT / error
                    break
                if msg.message == WM_HOTKEY:
                    try:
                        self._callback(int(msg.wParam))
                    except Exception:  # noqa: BLE001 - never kill the loop
                        pass
                elif msg.message == WM_COMMAND_QUEUE:
                    while True:
                        try:
                            cmd = self._commands.get_nowait()
                        except queue.Empty:
                            break
                        self._run_command(cmd, owned)
                else:
                    _user32.TranslateMessage(ctypes.byref(msg))
                    _user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            for hk_id in list(owned):  # leave NO global hotkey registered
                _user32.UnregisterHotKey(None, hk_id)
            owned.clear()
            self._thread_id = 0
            self._ready.set()
            self._fail_pending()


# ---------------------------------------------------------------------------------------------
class HotkeyManager(QObject):
    """Registers the two global hotkeys (names: hotkeyspec.NAME_CAPTURE / NAME_DELAYED).

    Signals:
        activated(str)                    hotkey name that fired
        registration_failed(str, str)     (name, human-readable reason), e.g. combination
                                          already registered by another program
    `backend` is a test seam: an object implementing the same tiny interface as the real
    Win32 backend (register/unregister/start/stop) so unit tests never register a real
    global hotkey.
    """

    activated = Signal(str)
    registration_failed = Signal(str, str)

    _FIXED_IDS = {hotkeyspec.NAME_CAPTURE: 1, hotkeyspec.NAME_DELAYED: 2}

    def __init__(self, parent: Optional[QObject] = None, backend: Optional[Any] = None) -> None:
        super().__init__(parent)
        self._backend: Any = backend if backend is not None else Win32HotkeyBackend()
        self._ids: dict[str, int] = dict(self._FIXED_IDS)
        self._names_by_id: dict[int, str] = {v: k for k, v in self._ids.items()}
        self._registered: dict[str, hotkeyspec.Hotkey] = {}
        self._running = False
        self._lock = threading.RLock()

    # -- lifecycle ------------------------------------------------------------------------
    @property
    def backend(self) -> Any:
        return self._backend

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def thread_id(self) -> int:
        """Win32 id of the hotkey thread (0 when stopped / for backends without one). Tests can
        PostThreadMessage(thread_id, WM_HOTKEY, ...) to simulate a press without any real input."""
        return int(getattr(self._backend, "thread_id", 0) or 0)

    def start(self) -> None:
        """Start the hotkey thread. Idempotent."""
        with self._lock:
            if self._running:
                return
            self._backend.start(self._on_hotkey)
            self._running = True

    def stop(self) -> None:
        """Unregister everything, stop and join the thread (leaves NO global hotkey
        registered). Idempotent; also called from the destructor / aboutToQuit."""
        with self._lock:
            if not self._running:
                self._registered.clear()
                return
            self._running = False
            try:
                for name in list(self._registered):
                    try:
                        self._backend.unregister(self._ids[name])
                    except Exception:  # noqa: BLE001
                        pass
                self._registered.clear()
            finally:
                try:
                    self._backend.stop()
                except Exception:  # noqa: BLE001
                    pass

    def __del__(self) -> None:  # pragma: no cover - best effort at interpreter shutdown
        try:
            self.stop()
        except Exception:  # noqa: BLE001
            pass

    # -- hotkey thread callback ---------------------------------------------------------------
    def _on_hotkey(self, hk_id: int) -> None:
        """Runs on the hotkey thread: only maps the id to a name and emits a signal."""
        name = self._names_by_id.get(hk_id)
        if name is not None and name in self._registered:
            self.activated.emit(name)

    # -- configuration --------------------------------------------------------------------------
    def _id_for(self, name: str) -> int:
        if name not in self._ids:
            self._ids[name] = 100 + len(self._ids)
            self._names_by_id[self._ids[name]] = name
        return self._ids[name]

    @staticmethod
    def _describe_error(text: str, code: int) -> str:
        if code == ERROR_HOTKEY_ALREADY_REGISTERED:
            return f"{text} is already in use by another program."
        if code in (ERROR_NOT_RUNNING_LOCAL, ERROR_TIMEOUT_LOCAL):
            return f"The hotkey service is not responding, could not register {text}."
        return f"Windows refused to register {text} (error {code})."

    def set_hotkeys(self, mapping: dict[str, str]) -> dict[str, Optional[str]]:
        """Replace the bindings of the names in `mapping` ({name: 'Ctrl+Alt+S'}; text parsed
        with hotkeyspec.parse_hotkey; MOD_NOREPEAT is added). Returns {name: None on success |
        error message}; each failure also emits registration_failed. A failure for one name
        must not unregister the other, and on failure the previous binding of that name is
        restored when possible.

        Names that are not in `mapping` keep their binding; an empty/None text removes that
        name's binding."""
        results: dict[str, Optional[str]] = {}
        try:
            self.start()  # idempotent
        except Exception as exc:  # noqa: BLE001 - the hotkey thread could not start
            message = f"Global hotkeys are unavailable: {exc}"
            for name in mapping:
                results[name] = message
                self.registration_failed.emit(name, message)
            return results
        with self._lock:
            # 1) parse + validate + detect duplicates -> `wanted` (None = unbind)
            wanted: dict[str, Optional[hotkeyspec.Hotkey]] = {}
            failures: dict[str, str] = {}
            for name, text in mapping.items():
                if text is None or not str(text).strip():
                    wanted[name] = None
                    continue
                err = hotkeyspec.validate_hotkey(text)
                if err:
                    failures[name] = err
                    continue
                wanted[name] = hotkeyspec.parse_hotkey(text)
            # bindings that stay as they are (names not in `wanted`, including names whose
            # NEW text was invalid: their old binding remains active)
            final: dict[str, hotkeyspec.Hotkey] = {
                n: hk for n, hk in self._registered.items() if n not in wanted
            }
            for name, hk in list(wanted.items()):
                if hk is None:
                    continue
                clash = next((o for o, o_hk in final.items() if (o_hk.modifiers, o_hk.vk) == (hk.modifiers, hk.vk)), None)
                if clash is not None:
                    failures[name] = f"{hk.text} is already used for '{clash}'."
                    del wanted[name]
                    continue
                final[name] = hk

            # 2) unchanged bindings are left alone
            todo: dict[str, Optional[hotkeyspec.Hotkey]] = {}
            for name, hk in wanted.items():
                cur = self._registered.get(name)
                if hk is not None and cur is not None and (cur.modifiers, cur.vk) == (hk.modifiers, hk.vk):
                    results[name] = None
                elif hk is None and cur is None:
                    results[name] = None
                else:
                    todo[name] = hk

            # 3) release everything that changes first (so swapping two hotkeys works) ...
            previous: dict[str, Optional[hotkeyspec.Hotkey]] = {}
            for name in todo:
                previous[name] = self._registered.pop(name, None)
                if previous[name] is not None:
                    self._backend.unregister(self._id_for(name))
            # ... then register the new ones
            for name, hk in todo.items():
                if hk is None:
                    results[name] = None
                    continue
                code = self._backend.register(self._id_for(name), hk.modifiers | hotkeyspec.MOD_NOREPEAT, hk.vk)
                if code is None:
                    self._registered[name] = hk
                    results[name] = None
                    continue
                message = self._describe_error(hk.text, int(code))
                old = previous.get(name)
                if old is not None:
                    back = self._backend.register(self._id_for(name), old.modifiers | hotkeyspec.MOD_NOREPEAT, old.vk)
                    if back is None:
                        self._registered[name] = old
                        message += f" {old.text} stays active."
                    else:
                        message += f" The previous hotkey {old.text} could not be restored."
                failures[name] = message

            for name, message in failures.items():
                results[name] = message
        for name, message in failures.items():
            self.registration_failed.emit(name, message)
        return results

    @property
    def registered(self) -> dict[str, str]:
        """{name: canonical hotkey text} currently active."""
        with self._lock:
            return {name: hk.text for name, hk in self._registered.items()}


# ---------------------------------------------------------------------------------------------
class TemporaryHotkey(QObject):
    """One global hotkey held only while needed (e.g. Esc for the countdown lifetime).

    Uses its own backend/thread so it never disturbs the main HotkeyManager. While it is
    held, the key is swallowed system-wide (the target application does not see it), which
    is exactly what we want for Esc during a delayed capture: it must cancel the countdown
    without closing the menu the user is about to capture.

    `pressed` is emitted from the hotkey thread (queued to the GUI thread)."""

    pressed = Signal()

    _ID = 0x7001

    def __init__(
        self,
        modifiers: int = 0,
        vk: int = winapi.VK_ESCAPE,
        parent: Optional[QObject] = None,
        backend: Optional[Any] = None,
    ) -> None:
        super().__init__(parent)
        self._modifiers = modifiers
        self._vk = vk
        self._backend: Any = backend if backend is not None else Win32HotkeyBackend()
        self._active = False
        self._started = False

    @property
    def active(self) -> bool:
        return self._active

    def acquire(self) -> bool:
        """Register the key. Returns False (and holds nothing) if that fails, so the caller
        can fall back to polling GetAsyncKeyState."""
        if self._active:
            return True
        try:
            self._backend.start(self._on_hotkey)
            self._started = True
            code = self._backend.register(self._ID, self._modifiers | hotkeyspec.MOD_NOREPEAT, self._vk)
        except Exception:  # noqa: BLE001
            self.release()
            return False
        if code is not None:
            self.release()
            return False
        self._active = True
        return True

    def release(self) -> None:
        """Unregister and stop the thread. Idempotent; safe from any state."""
        was_started = self._started
        self._active = False
        self._started = False
        if not was_started:
            return
        try:
            self._backend.unregister(self._ID)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._backend.stop()
        except Exception:  # noqa: BLE001
            pass

    def _on_hotkey(self, hk_id: int) -> None:
        if self._active and hk_id == self._ID:
            self.pressed.emit()

    def __del__(self) -> None:  # pragma: no cover
        try:
            self.release()
        except Exception:  # noqa: BLE001
            pass
