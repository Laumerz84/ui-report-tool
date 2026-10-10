# UI Report Tool - build contract

Windows 11 tray tool: capture a series of screenshots, caption + annotate each, then hand
the whole set to Claude Code as one report folder plus a ready-to-paste prompt.
Python 3.14 + PySide6 6.11 + pywin32 + psutil + uiautomation. Everything is local.

This file is the single source of truth for the three parallel builders and the integration
agent. **The stub files under `uireport/` are the exact signatures**; `tests/test_contract_signatures.py`
enforces them (module imports, public names, parameter names in order, signals, methods).
You may append extra OPTIONAL parameters/methods; never rename, reorder or remove contract items.
If you believe the contract is wrong, do the safest compatible thing, and write the deviation in
your final message (the integration agent reads it).

```
 hotkey thread ──activated(str)──►  AppController  ◄──── TrayIcon / Toast / SettingsWindow      (C)
 (capture pkg A)   queued           │      │  ▲
                                    │      │  └─ next / next_delayed / finish / discard /
      CaptureService (A) ◄─ start(mode)    │       session_changed  ── EditorWindow (B)
        │ countdown → selector             │
        └─ captured(Shot) / cancelled(mode) / failed(msg) ─►  Session (shared model, lead)
                                           └─ finish → output.writer.write_session (C) → PNGs, report.md/json
```

---------------------------------------------------------------------------------------------------

## 1. Ownership (disjoint; nobody edits another package's files)

| Owner | Files |
|---|---|
| **lead (shared, DONE, read-only for builders)** | `uireport/__init__.py`, `models.py`, `geometry.py`, `hotkeyspec.py`, `settings.py`, `imageops.py`, `annotdraw.py`, `requirements.txt`, `pytest.ini`, `CONTRACT.md`, `tests/conftest.py`, `tests/test_models.py`, `tests/test_settings.py`, `tests/test_geometry.py`, `tests/test_hotkeyspec.py`, `tests/test_imageops_annotdraw.py`, `tests/test_contract_signatures.py` |
| **A capture** | `uireport/capture/*.py` (`__init__, winapi, monitors, grab, windowinfo, sysinfo, selector, countdown, hotkeys, service`) + new `tests/test_capture_*.py` |
| **B editor** | `uireport/editor/*.py` (`__init__, canvas, editor_window, session_panel, session_fields`) + new helper modules inside `uireport/editor/` + new `tests/test_editor_*.py` |
| **C output + app** | `uireport/output/*.py` (`__init__, naming, render, report, writer, clipboard`), `uireport/app/*.py` (`__init__, controller, tray, toast, settings_window, autostart, icons, main`), `uireport/__main__.py`, `launch.pyw`, `UIReportTool.bat`, `setup.bat`, `README.md` + new `tests/test_output_*.py`, `tests/test_app_*.py` |

If a shared (lead) module has a bug, do not edit it: work around it and report it in your final message.

## 2. Shared modules you can rely on (already implemented and tested)

* `uireport.models` - `Session`, `Shot`, `Role` (PROBLEM/WANT/CONTEXT/AFTER, `.label/.description/.color`),
  `CaptureMode` (NORMAL/DELAYED), `AnnotationType`, annotation classes `PinAnn(n,x,y,note,color)`,
  `RectAnn(x,y,w,h)`, `ArrowAnn(x1,y1,x2,y2)`, `RulerAnn(x1,y1,x2,y2; dx,dy,length_px derived)`,
  `RedactAnn(x,y,w,h,style)`, `MonitorMeta`, `WindowMeta`, `SystemMeta`, `annotation_from_dict`,
  `monitor_at_point`, `monitor_for_rect`, `new_id()`, `now_iso()`.
  Key behaviours: `Shot.add_annotation()/remove_annotation()` keep pin numbers contiguous 1..N;
  `Session.add_shot/remove_shot/move_shot/reorder/renumber` keep `Shot.index` 1-based and contiguous;
  `Session.to_dict()` IS the report.json document (`schema_version`, `tool`, `session{...system{...}}`,
  `shots[...]`); `Shot.image` is a runtime-only QImage (never serialised); `Shot.scale_factor` and
  `Shot.to_logical(px)` derive logical sizes from the capture monitor.
* `uireport.geometry` - `IntRect` (half-open, `from_points`, `intersection`, `union`, `clamp_to`, ...),
  `scale_factor_from_dpi`, `scale_percent_from_dpi`, `dpi_from_scale_percent`, `to_logical`, `to_physical`.
* `uireport.hotkeyspec` - `parse_hotkey`, `normalize_hotkey`, `format_hotkey`, `validate_hotkey`
  (needs Ctrl/Alt/Win, blocks reserved combos such as Win+Shift+S), `altgr_warning` (soft warning:
  Ctrl+Alt+letter == AltGr+letter on Polish/German layouts), `NAME_CAPTURE="capture"`, `NAME_DELAYED="delayed"`.
* `uireport.settings` - `Settings` (defaults: Ctrl+Alt+S / Ctrl+Alt+D, delay 3 s, output root
  `%USERPROFILE%\UIReports`, remembered project path + framework hint, start_with_windows False),
  `SettingsStore(path=None)` (`.settings`, `.save()`, `.update(**kw)`, `.reload()`), `load_settings`,
  `save_settings`, `resolve_settings_path`, `default_output_root`; `Settings.new_session()` /
  `remember_session(session)`. Path override chain: explicit arg > `$UIREPORT_SETTINGS` > `%APPDATA%\UIReportTool\settings.json`;
  output root: `$UIREPORT_OUTPUT` > `%USERPROFILE%\UIReports`.
* `uireport.imageops` - `sample_hex(image, x, y) -> "#RRGGBB"` (upper case), `apply_redactions(image, redactions) -> new QImage`
  (heavy pixelation, input untouched), `pixelate_region`, `ensure_argb32`.
* `uireport.annotdraw` - the ONE painter of annotations: `AnnotStyle.for_scale(scale)`, `draw_annotation`,
  `draw_annotations(painter, anns, style, preview_redact=, selected_id=)`, `annotation_bounds`,
  `render_annotated(base, annotations, scale, redact=True) -> new QImage`, `render_shot_annotated(shot)`.
  The editor canvas (live view) and the writer (saved PNG) both use it, so the preview equals the file.
  It needs a `QGuiApplication` to exist (text drawing).
* `tests/conftest.py` fixtures: `test_out` (fresh folder under `.test-output/<package>/<test>`), `qapp`,
  `make_image(w,h,color)`, `make_shot(w,h,scale_percent,**fields)`. It forces `QT_QPA_PLATFORM=offscreen`.

## 3. Package APIs (behaviour; exact signatures are in the stub files)

### 3.A capture (`uireport/capture/`)

| Module | Public API | Notes |
|---|---|---|
| `winapi` | `enable_per_monitor_dpi_awareness()`, `exclude_from_capture(hwnd, exclude=True)`, `widget_hwnd(w)`, `dwm_flush()`, `cursor_pos()`, `is_key_down(vk)` | PMv2 (fallback PMv1). Qt 6 already sets PMv2 - treat "already set" as success. |
| `monitors` | `enumerate_monitors() -> list[MonitorMeta]`, `virtual_screen_rect()`, `find_qscreen(monitor)` | See section 4 for QScreen matching. |
| `grab` | `grab_virtual_screen() -> (QImage, IntRect)`, `crop_image(frozen, origin, rect)` | GDI BitBlt SRCCOPY\|CAPTUREBLT of the whole virtual desktop; measured by the lead: the naive pywin32 path grabs 5120x1440 (29 MB) in memory in ~80-100 ms (fine as a baseline, faster is better). dpr of every produced QImage stays 1.0. |
| `windowinfo` | `WindowSnapshot`, `snapshot_top_level_windows`, `window_at_point`, `foreground_hwnd`, `browser_kind`, `window_meta_from_hwnd`, `read_browser_url` | URL via `uiautomation`, worker thread, 1.5 s timeout, silent on failure. |
| `sysinfo` | `windows_version_string`, `theme_modes`, `collect_system_meta` | Read-only registry access. |
| `selector` | `Selection`, `RegionSelector` (`selected(Selection)`, `cancelled()`) | Frozen-screen overlays, one per monitor. |
| `countdown` | `CountdownOverlay` (`finished()`, `cancelled()`) | Non-activating, click-through, excluded from capture. |
| `hotkeys` | `HotkeyManager` (`activated(str)`, `registration_failed(str,str)`, `start/stop/set_hotkeys/registered`) | Own thread with own message loop. `set_hotkeys` semantics: see section 11. |
| `service` | `CaptureService` (`captured(Shot)`, `cancelled(str)`, `failed(str)`, `is_active`, `start(mode)->bool`, `cancel()`) | Orchestrates normal/delayed capture. |

Everything else in the spec's "Metadata to capture automatically" that is per-shot is produced by
`CaptureService` and stored in the `Shot`: order index is assigned later by `Session.add_shot`;
`timestamp`, `capture_mode`, `selection`, `image_width/height`, `capture_rect`, `monitor`, `window`.
Per-session `SystemMeta` comes from `collect_system_meta()` (called by the controller).

### 3.B editor (`uireport/editor/`)

`EditorWindow(session)` is the only class C touches: signals `next_requested`, `next_delayed_requested`,
`finish_requested`, `discard_session_requested`, `session_changed`; methods `set_session`, `refresh`,
`show_shot(id)`, `current_shot_id`, `present(monitor_hint)`, `hide_for_capture`. It mutates the shared
`Session`/`Shot` objects in place (caption, role, annotations, session fields, reorder, delete). It composes
`AnnotationCanvas` (tools Select/Pin/Rect/Arrow/Ruler/Redact, `Tool` enum, undo/redo),
`SessionPanel` (thumbnails, drag-reorder, delete, click-to-re-edit; emits requests only) and
`SessionFieldsWidget` (goal, project folder + Browse, framework hint, expected, actual).
B has no dependency on `uireport.capture`, `uireport.output` or `uireport.app`.

### 3.C output + app

* `output.naming` / `render` / `report` / `writer` / `clipboard` - see section 7.
* `app.controller.AppController` - see section 6. `app.tray.TrayIcon`, `app.toast.Toast`,
  `app.settings_window.SettingsWindow`, `app.autostart`, `app.icons.make_app_icon`, `app.main.main`.
* C depends on A only through: `CaptureService`, `HotkeyManager`, `collect_system_meta`,
  `enable_per_monitor_dpi_awareness`, `exclude_from_capture`, `dwm_flush`, `enumerate_monitors`; on B only through
  `EditorWindow`. Because A's functions are stubs until A finishes, C wraps calls to them in `try/except`
  where a failure must not break C's own tests (e.g. the toast's `exclude_from_capture`) and injects fakes elsewhere.
  Unit-test the controller with fakes that have the same signals (a `FakeCapture`, `FakeEditor`, ...).

## 4. Coordinates, DPI and sizes (read this twice)

1. **Physical pixels everywhere in data.** Every stored coordinate is a physical (device) pixel:
   * *Virtual-screen coordinates* (`Shot.capture_rect`, `MonitorMeta.rect`, `WindowMeta.rect`): Win32
     coordinates of a Per-Monitor-V2 aware process; origin = top-left of the primary monitor;
     x/y may be negative (monitors left of / above the primary).
   * *Image pixels* (all annotation fields, `Shot.image_width/height`): origin = top-left pixel of the
     saved `NN.png`; x right, y down; **the PNG is a 1:1 copy of screen pixels: never scaled, never
     upscaled, `image_width == capture_rect.w`**. Pins/points are integer pixel *indices*; a pin at
     (412, 88) points at the pixel whose top-left corner is (412, 88) and is painted centred on (412.5, 88.5).
   * `QImage.devicePixelRatio()` of every captured/stored image stays **1.0** (so `width()` is physical).
2. **Scale.** `MonitorMeta.dpi` is the effective DPI (96/120/144/192); `scale_percent = dpi/96*100`
   (100/125/150/200); `scale_factor = dpi/96`. `Shot.monitor` is the monitor holding the selection.
3. **Logical size** (DIPs / what a developer sees) is always *derived*: `logical = round(physical / scale_factor)`
   (`geometry.to_logical`). `WindowMeta.size_logical` uses `WindowMeta.dpi`: `GetDpiForWindow` for DPI-aware
   windows; for DPI-unaware windows the DPI of the monitor they are on (Windows stretches those bitmaps).
   Rulers are measured in physical (image) pixels; the report additionally prints the logical equivalent
   when the scale is not 100% (`Shot.to_logical`).
4. **Annotation look scales with the monitor**: `AnnotStyle.for_scale(shot.scale_factor)`. Do not invent a
   second style.
5. **Qt logical coordinates are only for widgets.** Qt widgets use logical pixels. With Qt 6 mixed-DPI:
   `QScreen.geometry().topLeft()` is the *native physical* top-left of that screen while `geometry().size()`
   is *logical* (physical size = `size * devicePixelRatio()`). On Windows **`QScreen.name()` is the friendly monitor
   name (e.g. `'49C1R'`), not `\\.\DISPLAY1`** - match monitors to screens by geometry (`capture.monitors.find_qscreen`).
   Selector overlays: one frameless top-most widget per monitor placed on the matching QScreen; convert a widget-local
   logical point to physical with the ratio `monitor.rect.w / widget.width()` (per monitor - never a global scale),
   then add `monitor.rect.x/y`. A drag never leaves the monitor where it started (selection stays on one monitor,
   so `Shot.monitor` is unambiguous).
6. **Editor canvas**: shows the image at `zoom` image-px per logical-px (fit-to-window by default; 1:1 means
   `1/devicePixelRatio` logical px per image px so it looks sharp); `widget_to_image` / `image_to_widget` are the only
   converters. Pin colour is sampled with `imageops.sample_hex(shot.image, x, y)` from the ORIGINAL image.
7. **Timestamps** are local time with UTC offset, whole seconds (`models.now_iso()`).

## 5. Threading

* **Qt main (GUI) thread**: all widgets, overlays, timers, `CaptureService`, `AppController`, the screen grab and
  the clipboard. Never touch a widget from another thread.
* **Hotkey thread** (`HotkeyManager`, package A): a daemon `threading.Thread` that calls `RegisterHotKey` /
  `UnregisterHotKey` and runs its own `GetMessage` loop (those calls must happen on the thread that pumps
  `WM_HOTKEY`). Commands (register/unregister/stop) reach it with `PostThreadMessage` + a queue. It emits
  `activated(name)` and nothing else. Receivers connect with `Qt.ConnectionType.QueuedConnection`, so the handler
  runs on the GUI thread. `stop()` unregisters everything and joins (<= 1 s).
* **URL worker** (`CaptureService`): `uiautomation` runs on a short-lived worker thread
  (own COM init); the GUI thread never blocks on it for more than 2 s (poll with a `QTimer`, then give up: `url=None`).
* **Writer worker** (`AppController.finish_session`): `write_session` uses only `QImage`/`QPainter` (no widgets) and runs
  on a worker (`QThreadPool`/`QThread`); the result is delivered back on the GUI thread (queued signal). Meanwhile
  the editor is hidden and a "Saving..." toast is shown. `Shot.image` is read-only during the write.
* Esc during the delayed countdown: the countdown window is non-activating (so an open menu keeps focus), Esc is
  caught globally (temporary `RegisterHotKey(Esc)` on the hotkey thread or `GetAsyncKeyState` polling by a `QTimer`) and
  released as soon as the countdown ends.

## 6. Flows, signals and cancel paths

State owned by `AppController`: `session` (long-lived; only Finish, Discard or Quit ends it), `editor`
(created lazily on the first captured shot, then reused), `capture` (CaptureService). Constant
`HIDE_SETTLE_MS = 120` (wait after hiding the editor so the compositor has removed it).

| # | Trigger | What happens |
|---|---|---|
| F1 | Capture hotkey / tray icon click / tray "Capture" | `request_capture(NORMAL)`. Ignored while `capture.is_active`. `toast.dismiss()`. If the editor is visible: `editor.hide_for_capture()` + `winapi.dwm_flush()`-style settle (`HIDE_SETTLE_MS`), then `capture.start(NORMAL)`. **If the editor is not visible, `capture.start` is called synchronously in the same call stack: the region selector opens with no delay.** |
| F2 | Delayed hotkey / tray "Delayed capture" / editor "Next (delayed)" | Same with `DELAYED`: `CaptureService` shows the countdown (delay from settings), hides it, settles, then freezes + opens the selector. |
| F3 | `capture.captured(shot)` | `session.add_shot(shot)` -> ensure editor -> `editor.refresh()` -> `editor.show_shot(shot.id)` -> `editor.present(shot.monitor)` (caption box focused) -> `tray.set_session_count(n)`. |
| F4 | `capture.cancelled(mode)` (Esc in the selector or the countdown; right-click in the selector) | Nothing is added, session untouched. If the session has shots: `editor.show_shot(<the shot that was open before>)` + `present()`; else idle in the tray. |
| F5 | `capture.failed(msg)` | Error toast; then same as F4. |
| F6 | Editor **Next** (Ctrl+Enter) | The current shot is already in the session (edits are live): controller runs F1. **Next (delayed)** (Ctrl+Alt+Enter): F2. |
| F7 | Editor **Finish** (Ctrl+Shift+Enter) | `finish_session()`: `session.system = collect_system_meta()`; `settings.remember_session(session)` + `store.save()`; hide editor; `write_session(session, output_root)` on a worker; on the GUI thread: `copy_prompt_to_clipboard(build_prompt(...))`; toast **"Copied \u2014 paste into Claude"** with an **"Open folder"** button (`os.startfile(folder)`); new session `settings.new_session()`; `editor.set_session(new)`; emit `session_finished`. On error: `finish_failed(msg)`, error toast, session kept, editor re-shown. Finish is disabled in the editor while the session has no shots. |
| F8 | Editor `session_changed` | `tray.set_session_count(len(session.shots))`; update remembered project/framework in memory (persisted on Finish/Quit). |
| F9 | Session panel: click thumbnail | `EditorWindow.show_shot(id)` (re-edit caption, role, annotations, all live). Drag reorder -> `Session.reorder(ids)`; delete -> `Session.remove_shot(id)` (if the open shot was deleted, open its neighbour, or show the empty state when no shots remain); each emits `session_changed`. |
| F10 | Editor window closed with X | Only hides; the session and its shots stay. Tray "Show session (N)" or the next capture brings the editor back. |
| F11 | Hotkey while the editor is open | Treated as the editor's Next / Next (delayed) (F6): the open shot is kept, then capture starts. |
| F12 | Editor "Discard session" | Confirm dialog in the editor, then `discard_session_requested` -> controller `discard_session()`: fresh session, editor hidden, tray count 0. |
| F13 | Tray "Settings" | `SettingsWindow` -> `applied(Settings)` -> controller: `store.settings = ...; store.save()`; `hotkeys.set_hotkeys({...})` (failures shown inline via `show_error`, dialog stays open); `autostart.set_enabled(flag)`; tray hints refreshed. |
| F14 | Tray "Open reports folder" | `mkdir -p output_root` then `os.startfile`. |
| F15 | Tray "Quit" | If the session has shots: ask *Finish now / Discard and quit / Cancel*. Then `shutdown()` (unregister hotkeys) and `quit_requested`. |
| F16 | Hotkey registration fails (combination taken) | Toast + tray message naming the combination; the app keeps running; the tray menu still works. |

Cancel-path guarantee: the frozen grab and every overlay are discarded on cancel; `CaptureService` returns to idle
(`is_active == False`), so the next hotkey works immediately.

## 7. Output

Folder: `<output_root>\<YYYY-MM-DD_HHMM>_<slug>\` (local time at Finish; slug from the goal, else the first
caption, else `session`; ASCII lower-case, hyphens, <= 40 chars; collisions get `-2`, `-3`).
Files (lossless PNG; redactions applied to BOTH; both always written even if there are no annotations):
`01.png`, `01_annotated.png`, `02.png`, `02_annotated.png`, ... (index zero-padded to `max(2, len(str(total)))` digits),
`report.md`, `report.json`. Plus `<output_root>\latest.json` (pointer for a future MCP server).
All paths written into reports are **absolute, backslash form**. Files are UTF-8; Markdown uses LF.

`report.json` = `Session.to_dict()` after the writer set `shot.original_path`, `shot.annotated_path`, `session.folder`.

`report.md` skeleton (C may polish wording, must keep every listed element):

```markdown
# UI report: <goal, or "UI issue">

- **Goal:** Fix the clipped Save button
- **Project folder:** C:\dev\app
- **App / framework:** React + Tailwind
- **Expected:** Full label visible
- **Actual:** Label cut off at 150% scale
- **Created:** 2026-09-29 14:32 - 3 screenshots
- **Environment:** Windows 11 Pro 10.0.26200 - apps theme dark - tool v0.1.0
- **Monitors:** 1: \\.\DISPLAY1 (5120x1440 @ 100%, primary); 2: \\.\DISPLAY2 (3840x2160 @ 150%)

**How to read this:** shots are in order. *Problem* = what is wrong now; *Want* = a reference/mockup of the
target; *Context* = orientation only; *After* = the result after a fix, to verify it. Coordinates are pixels of the
ORIGINAL image (top-left origin). Open every image listed below (annotated and original). Before answering,
quote back word for word the text the user wrote for each shot (its caption and any annotation labels), or say
that a shot has none.

## 1. [Problem] Save button text is clipped
- **Caption:** Save button text is clipped
- **Annotated image:** C:\Users\me\UIReports\2026-09-29_1432_fix-clipped-save\01_annotated.png
- **Original image:** C:\Users\me\UIReports\2026-09-29_1432_fix-clipped-save\01.png
- **Captured:** 2026-09-29 14:32:05, normal capture, region 640x300 px at screen (100, 50)
- **Monitor:** 2 (\\.\DISPLAY2, 3840x2160, scale 150%)
- **Window:** "Settings - My App" - chrome.exe (C:\Program Files\...\chrome.exe) - 1500x900 px (1000x600 logical @150%)
- **URL:** http://localhost:3000/settings

**Pins**
1. at (412, 88), color #1F2937: button text is clipped
**Measurements**
- Ruler 1: (10, 20) -> (110, 20) = 100 px (67 logical px at 150%)
**Rectangles**
- Rect 1: x=10, y=20, 100x50
**Arrows**
- Arrow 1: (5, 5) -> (50, 60)
**Redactions:** 1 region redacted in both images.
```

Prompt (clipboard, single paragraph, details stay in report.md):
`I captured 3 screenshots. Goal: Fix the clipped Save button. Read C:\Users\me\UIReports\2026-09-29_1432_fix-clipped-save\report.md and look at every image it lists (annotated and original), then help me with the goal.`

## 8. Entry point and how to run

* Code: `python -m uireport` (`uireport/__main__.py` -> `uireport.app.main.main`). Venv python:
  `<project>\.venv\Scripts\python.exe -m uireport` (`pythonw.exe` for no console).
* Flags: `--settings-path PATH`, `--output-root PATH`, `--smoke-test`, `--offscreen`, `--no-hotkeys`,
  `--instance-name NAME` (see `app/main.py` docstring). `--smoke-test` must exit by itself in a few seconds with
  code 0 and must not leave hotkeys registered or windows open; it uses a temp settings file and temp output root
  (`.test-output/...`) and, if it registers hotkeys at all, uses unusual combos (e.g. `Ctrl+Alt+Shift+F23/F24`).
* One double-click launch: `UIReportTool.bat` (starts `pythonw.exe launch.pyw` from the venv, falls back to a
  system `pythonw`/`pyw -3`, and tells the user to run `setup.bat` when the venv is missing); `setup.bat` creates
  `.venv` and installs `requirements.txt`. `launch.pyw` puts its own folder on `sys.path` then calls `main()`.
* "Start with Windows": `app.autostart` writes `HKCU\...\Run\UIReportTool` = `"<pythonw>" "<project>\launch.pyw"`.
  Default OFF. Tests use an in-memory backend only.
* Logging: `main()` logs to `<settings folder>\uireport.log` (rotating) and shows a message box for uncaught
  exceptions, because `pythonw` has no console.
* Single instance: a second launch shows "already running" and exits 0.

## 9. Environment, testing rules and known platform facts

* Interpreter: `<project>\.venv\Scripts\python.exe` (Python 3.14.4). Packages (already installed there): PySide6 6.11.2,
  pywin32 312, psutil 7.2.2, uiautomation 2.0.29 (+ comtypes 1.4.17), pytest 9.1.1. Never install anything else
  system-wide; if you need another package add it to the venv with the venv's pip and tell the lead in your report.
* Run tests: `set QT_QPA_PLATFORM=offscreen` then `.venv\Scripts\python.exe -m pytest tests -q`
  (conftest sets it too). New test files only as `tests/test_<yourpackage>_*.py`
  (`capture`, `editor`, `output`, `app`). Tests write only under `.test-output\<package>\...` (fixture `test_out`).
* **Never** write to `%USERPROFILE%\UIReports` or `%APPDATA%\UIReportTool` from tests or smoke runs; pass paths.
* **Never** touch the registry `Run` key or the Startup folder (use fakes). Reading `HKCU\...\Themes\Personalize` is fine.
* **No desktop control**: never send synthetic clicks/keystrokes (`SendInput`, `keybd_event`, `mouse_event`,
  windows-mcp, computer-use, Chrome tools). Simulating a hotkey by `PostThreadMessage(hotkey_thread, WM_HOTKEY, ...)`
  to our own thread, or emitting Qt signals, is fine.
* Real screen grabs in tests: in memory only, assert on size/metadata (and on pixels of a window YOU created),
  never write a real grab to disk, never log window titles/URLs of the user's windows.
* Showing real windows on the real desktop is limited to a *single* tiny non-activating probe window
  (`WS_EX_NOACTIVATE | WS_EX_TRANSPARENT`, <= 1 s, far corner of the primary monitor) when verifying
  `exclude_from_capture` / hide-before-grab; everything else uses `offscreen` or fakes.
* Registering a real global hotkey in a test: only an unusual combo (`Ctrl+Alt+Shift+F23`), for < 1 s, always
  unregistered in a `finally`. Prefer the fake backend.
* Every process/thread you start must end by itself; leave nothing running.
* **Offscreen fonts:** with `QT_QPA_PLATFORM=offscreen` on this machine text renders as tofu boxes; do not assert on
  glyphs. Shapes/positions/pixels are fine. To eyeball text you can use `QT_QPA_PLATFORM=windows` with a
  `QGuiApplication` and paint into a `QImage` only (no window shown).
* `QGuiApplication`/`QApplication` must exist before any text painting or widget creation (Qt aborts otherwise).
* This dev machine has ONE monitor (5120x1440 @ 100%). Multi-monitor / 150% / negative-origin behaviour must be tested
  with synthetic `MonitorMeta` lists and geometry math (fake enumerations, `QScreen` stubs), and listed as
  "not verified on real hardware" in the final report.
* Cannot be verified by an automated agent (leave to the human first run): real hotkey press, a real open menu in
  a delayed capture, HDR/DRM-protected content, real UIA URL reads in every browser version.

## 10. Extension hooks (do not build now, do not block)

* Recordings: `Shot.media` ("image" now; "recording" later), `Shot.extra` preserves unknown keys.
* Compare mode (Problem vs After): `Shot.pair_with` (id of the paired shot) is serialised already.
* MCP server: `<output_root>\latest.json` points at the newest `report.md` / `report.json`; `report.json` has
  `schema_version`.

---------------------------------------------------------------------------------------------------

## 11. Integration notes (what the three packages actually do; supersedes the wording above where they differ)

Reconciled by the integration pass. Signatures in the stub files never changed; everything below is either
an optional addition or a clarification of behaviour.

**capture (A)**
* `HotkeyManager.set_hotkeys(mapping)`: names that are NOT in `mapping` keep their current binding; an empty
  text unbinds that name; the text is validated with `hotkeyspec.validate_hotkey` (reserved combinations such
  as Win+Shift+S and keys without Ctrl/Alt/Win come back as errors and never reach the OS); a combination used by
  the other name is an error; a failed registration keeps the previous binding of that name. Returns
  `{name: None | error text}` and never raises. `stop()` unregisters everything. The controller pauses the
  hotkeys while a Settings field records a shortcut by sending `{name: ""}` for both names.
* `CaptureService` does not hide the editor and does not wait for the compositor after hiding it: the controller
  does both (`hide_for_capture()` + `HIDE_SETTLE_MS`). `start(NORMAL)` is synchronous. `cancelled` / `failed` are
  emitted after the service is idle again, so a slot may call `start()` from inside them.
* Window DPI rule: `GetDpiForWindow` for per-monitor-aware windows, the DPI of the monitor holding most of the window
  for system-aware and DPI-unaware windows. Bare hosts in a browser URL get `https://`, loopback / LAN / `.localhost`
  hosts get `http://`.
* Native popup menus (`#32768`) can be picked with a click in window mode; click-through overlays of other
  programs are not pickable.

**editor (B)**
* `AnnotationCanvas` zoom means LOGICAL widget pixels per IMAGE pixel; 1:1 is `1 / devicePixelRatio`. The default tool is
  Select (a stray click never drops a pin). Pins and arrow / ruler ends are pixel INDICES (floor of the click);
  rectangle and redaction corners are grid EDGES (nearest line).
* `session_changed` is emitted synchronously for every edit (one per caption keystroke) and also before Next / Next
  (delayed) / Finish; the controller's handler must stay cheap. `shot.caption` is stored raw, the report strips it.
* `hide_for_capture()` flushes paint events but leaves user input queued (a repeated key press cannot re-enter the
  controller from inside the call). `set_session()` leaves no shot open; `refresh()` never opens one.

**output + app (C)**
* `AppController.__init__` has three extra keyword-only arguments (`output_root`, `confirm_quit`, `open_path`);
  `start(welcome=False)`. `quit_requested` is connected to `QApplication.exit(0)`, not `quit()`, because the editor
  ignores close events (its X only hides it).
* Tray: `Show session (N)` is always in the menu and disabled at N = 0. Menu labels stay exact; hotkeys appear in the
  tooltips. Left click on the icon = Capture, instantly. The menu item `Capture` waits `MENU_CLOSE_SETTLE_MS` (90 ms,
  after a compositor flush) so its own popup cannot end up in the frozen screenshot; hotkeys, the icon click and
  the editor buttons are not delayed by it.
* "Start with Windows": the Run key is the truth. Opening Settings first reads the real state (the entry may have been
  removed in Task Manager); Apply writes only when the checkbox differs from the real state.
* Finish remembers the open shot so a failed save reopens the editor on it. "Show session" is ignored while a capture
  is in progress.
* `report.md` prints ruler lengths as physical px plus the logical equivalent with one decimal (`66.7 logical px at
  150%`), pin lines as `- Pin 1 at (412, 88), color #1F2937: note`.
* `latest.json` carries `schema_version`; `--smoke-test` writes `smoke-result.json` (with tray / hotkey evidence)
  next to the settings file it used.

**Testing notes**
* `tests/integration_support.py` builds a synthetic desktop whose pixel colours encode their own screen coordinates
  (13 bits x, 11 bits y, wrapping) and wires the REAL CaptureService / RegionSelector / CountdownOverlay / AppController /
  EditorWindow / writer / Toast / TrayIcon to it; only the OS hotkeys, the registry, "open folder" and the system-meta probe
  are replaced. `test_integration_coordinates.py` (150 %, mixed DPI, three monitors, odd rounding),
  `test_integration_e2e.py` (14 captures, Esc in selector and countdown, reorder / delete / re-edit, Finish, open menu in a
  delayed capture) and `test_integration_settings.py` are built on it.
* The agent sandbox virtualises `%APPDATA%\Roaming`: never use `Path.resolve()` on project paths in new code, and run scripted
  edits with the venv's python (the Microsoft Store `python` on PATH sees a different file view).
* Reading a UI Automation element of a window created a few milliseconds earlier intermittently blocks for UIA's internal
  3 s timeout (measured 4 of 40); a settled or long-lived window never does (0 of 70). The test host window therefore settles
  0.5 s before it is read. Production reads long-lived browser windows and has a hard 1.5 s limit anyway.
