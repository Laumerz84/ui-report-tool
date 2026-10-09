# UI Report Tool - notes for Claude

This repo is a Windows tray app that turns screenshots into reports **for you**. The user
captures shots, marks them up and presses Finish. The tool then writes a `report.md` with
images, and pastes or copies a short prompt that points you at it. This file covers three
things: setting the tool up for your user, hooking yourself up so you can read its reports
from any project, and working on the code.

## 1. Set it up for the user

Windows 10/11, Python 3.11+ on PATH.

1. Run `setup.bat` from the repo folder (or do the same by hand: `python -m venv .venv`, then
   `.venv\Scripts\python.exe -m pip install -r requirements.txt`). Everything goes into `.venv`.
2. Start it **detached from your session**. A process started from a Claude Code session can
   die when that session ends, so use one of these:
   - PowerShell:
     `Invoke-CimMethod Win32_Process -MethodName Create -Arguments @{ CommandLine = '"<repo>\.venv\Scripts\pythonw.exe" "<repo>\launch.pyw"'; CurrentDirectory = '<repo>' }`
   - or ask the user to double-click `UIReportTool.bat`.

   It lives in the tray. Hotkeys: **Ctrl+Alt+S** capture, **Ctrl+Alt+D** delayed capture.
   Check it's running with `.venv\Scripts\python.exe -m uireport --smoke-test --offscreen`
   (self test, exits by itself), or look for the tray icon.
3. Ask the user before turning on *Start with Windows* (tray > Settings), because it writes
   an HKCU Run entry. Restart after code changes with `.venv\Scripts\pythonw.exe launch.pyw --quit`,
   then start it again as in step 2.

Settings are in `%APPDATA%\UIReportTool\settings.json` and the log in
`%APPDATA%\UIReportTool\uireport.log`. Reports go to `output_root` from the settings file
(default `%USERPROFILE%\UIReports`). Ask the user which drive they want reports on if it matters.

## 2. Hook yourself up

### When the user pastes a report prompt
The user's message looks like this:

> I captured 3 screenshots. Goal: Fix the clipped Save button. Read
> C:\...\UIReports\2026-09-29_1432_fix-clipped-save\report.md and look at every image it lists
> (annotated and original), then help me with the goal.

Read `report.md` first, then open **every** image it lists, both `NN_annotated.png` and
`NN.png`. The annotated image shows what the user pointed at; the original shows what is
really there. Then act on the goal and the captions.

- **Roles:** *Problem* = broken now, *Want* = target or mockup, *Context* = orientation only,
  *After* = a result to verify against the Problem shot. *Shot* = no particular role.
- **Coordinates** are pixels of the ORIGINAL image: top-left origin, physical pixels. The
  report gives the display scale. For CSS/logical sizes, divide by the scale (`report.md` also
  prints logical values for rulers).
- The report records the window under each capture (title, exe, size) and the monitors. Use
  them to work out which app or project the shots are about.
- The caption is often the real request ("make the icon closer to X", "can the tool do Y?").
  It is not always a bug report.
- `report.json` has the same data, structured (`schema_version` 1).

### When the user says "check my latest report" (no paste)
Read `<output_root>\latest.json`. Its keys are `folder`, `report_md`, `report_json`,
`created_at`, `shot_count` and `goal`, all absolute paths. Get `output_root` from
`%APPDATA%\UIReportTool\settings.json`; if that file doesn't exist, it is
`%USERPROFILE%\UIReports`. Then read the report as above.

### Make it work from every project
Reports are usually about some *other* project, so offer (ask first) to add this to the user's
global `~/.claude/CLAUDE.md`, with the real reports folder filled in:

```markdown
## UI Report Tool
Screenshot reports from my UI Report Tool are in <reports folder>. When I paste "I captured N
screenshots ... Read <path>\report.md", read report.md and open every image it lists (annotated
and original) before answering. "Check my latest report" = read <reports folder>\latest.json
and follow its report_md. Coordinates are original-image pixels; roles are Problem / Want /
Context / After.
```

### How the prompt reaches you
- **Claude desktop app:** with the *paste on Finish* setting on (the default), Finish brings
  Claude forward and pastes into its message box. It never presses Enter; the user sends.
  In split view, **Ctrl+Alt+Shift+Enter** in the editor lets the user pick the left or right
  pane.
- **Claude Code in a terminal / IDE:** the prompt is only copied. The user pastes it with Ctrl+V.
- The user can replace the prompt text in Settings (*Text copied on Finish*). Fill-ins:
  `{shots} {count} {goal} {report} {folder}`.

## 3. Working on the code

- Architecture and every behaviour rule: `CONTRACT.md`. User-facing docs: `README.md`. Keep
  both in sync with changes.
- Run it with a console: `.venv\Scripts\python.exe -m uireport` (`--verbose`, `--no-hotkeys`,
  `--settings-path`, `--output-root`).
- Tests: `.venv\Scripts\python.exe -m pytest tests -q` (Qt offscreen; there is no real screen,
  hotkeys or paste under test). Ask the user whether they want tests run or written.
- `uireport/app/claude_paste.py` is the only code that sends keystrokes. Keep it narrow:
  - only an already-open Claude desktop window, verified in the foreground;
  - only Ctrl+V, never Enter;
  - only after UI Automation has focused the message box;
  - the result is read back before reporting "pasted".

  Claude's message box is the UIA Edit control named `Prompt`. The panes are groups named
  `Primary pane` / `Secondary pane`, and the chat title is a button named
  `<title>, rename session`. If a Claude update renames these, the paste and the pane picker
  stop finding them.
- No network calls at runtime. Everything is local.
