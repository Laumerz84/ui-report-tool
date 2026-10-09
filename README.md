# UI Report Tool

A small Windows 11 tray tool for sending UI bug reports to Claude Code (or any AI assistant).
You capture a series of screenshots, caption and mark up each one, then hand the **whole set** over
in one paste: Claude gets the images, your notes, exact pixel coordinates, colours, window sizes and
display scales, and can diagnose, fix or restyle the UI it sees.

Everything is local. The tool makes **no network calls** (only `setup.bat` downloads libraries, once).

## What it does

1. **Capture** with a hotkey or the tray icon: the screen freezes instantly and you **drag** a region,
   **click** a window to capture just that window, or press **F** (or Enter) for the whole monitor.
   **Esc** or a right-click cancels.
2. **Delayed capture** has its own hotkey: a small countdown appears in a screen corner (3 s by
   default), so you can open a menu or trigger a hover state first. The countdown is never in the shot.
3. An **editor** opens with the caption box focused. Add numbered **pins** (each with a note),
   **rectangles**, **arrows**, a **ruler** (measures pixels) and **redact** boxes for anything
   sensitive (applied to both saved images).
4. Give each shot a **role** with one click: *Problem* (what is wrong now), *Want* (a reference or
   mockup), *Context* (orientation only), *After* (the result after a fix, to verify it).
5. Keep capturing (**Next**), review and reorder the thumbnails, then **Finish**. The report is saved
   and a ready-to-paste prompt is on your clipboard.

## Install

You need **Windows 10/11** and **Python 3.11 or newer** (the tool was built and tested with 3.14).

1. **Install Python** if you do not have it:
   * download it from <https://www.python.org/downloads/> and, on the first installer page, tick
     **"Add python.exe to PATH"**; or
   * open a terminal and run `winget install Python.Python.3.12`.
2. **Double-click `setup.bat`.** It creates a private `.venv` folder next to it and installs the
   libraries (PySide6, pywin32, psutil, uiautomation) into it. Nothing is installed system-wide.
   It needs an internet connection once and takes a few minutes.
3. **Double-click `UIReportTool.bat`.** The tool starts in the system tray (no console window).
   Windows may hide new tray icons under the **^** arrow next to the clock: drag the icon out to keep it visible.

## The tray icon

Left-click the icon = **Capture** (the selector opens at once). Right-click for the menu:

| Menu item | What it does |
|---|---|
| Capture | normal capture, opens the region selector immediately |
| Delayed capture | countdown first, then the region selector |
| Show session (N) | brings the editor back with your N shots (greyed out when N is 0) |
| Settings | hotkeys, delay, reports folder, start with Windows |
| Open reports folder | opens the folder where reports are saved |
| Quit | asks *Finish now / Discard and quit / Cancel* if you have unsaved shots |

Hover over the icon to see the hotkeys and the number of shots in the current session.

## Hotkeys

| Action | Default |
|---|---|
| Capture | **Ctrl+Alt+S** |
| Delayed capture | **Ctrl+Alt+D** |

(`Win+Shift+S` is Windows' own Snipping Tool, so it is not used.)
Change them in **Settings**: click a hotkey field and press the new combination. The delay length
(1 to 30 seconds) is in the same window.

> **Polish, German and other AltGr keyboard layouts:** `Ctrl+Alt+<letter>` is the same as
> `AltGr+<letter>`, which types special characters (for example `AltGr+S` is *s with an acute accent* on a Polish keyboard). A global hotkey on that combination would swallow those
> characters. Settings shows a warning next to such a combination; pick something with **Shift**
> or a function key instead, for example `Ctrl+Shift+F9` and `Ctrl+Shift+F10`.

## In the editor

| Key | Action |
|---|---|
| **Ctrl+Enter** | *Next*: save this shot and start a normal capture for the next one |
| **Ctrl+Alt+Enter** | *Next (delayed)*: save this shot and start a delayed capture |
| **Ctrl+Shift+Enter** | *Finish*: save the report and copy the prompt |
| **Ctrl+Alt+Shift+Enter** | *Finish to...*: like Finish, but first pick which open Claude pane (left / right in split view) to paste into, with the arrow keys and Enter (Esc cancels). With one pane it pastes straight there; it pastes even if the paste-on-Finish option is off |
| **Esc** | in the region selector or during the countdown: cancel that capture (your session is kept) |

Alt+1 to Alt+4 set the role. Click a thumbnail to re-edit that shot, drag thumbnails to reorder, use
the delete button to remove one. There is no limit on the number of shots. Closing the editor window
with X only hides it: your shots stay, and *Show session* in the tray menu (or the next capture)
brings it back.

## Where reports are saved

```
%USERPROFILE%\UIReports\
    latest.json                      pointer to the newest report (for tools that want to find it)
    2026-09-29_1432_fix-clipped-save\
        01.png                       original screenshot (lossless, never scaled)
        01_annotated.png             the same with your pins/rectangles/arrows/rulers
        02.png  02_annotated.png ...
        report.md                    what Claude reads: goal, notes, coordinates, window and monitor info
        report.json                  the same data in structured form
```

Redactions are applied to **both** images. The folder name is the date, time and a short name taken
from your goal (or the first caption). You can change the reports folder in Settings.

## The paste prompt

On **Finish** the tool copies a short prompt to the clipboard and shows a *"Copied — paste into
Claude"* card with an **Open folder** button:

> I captured 3 screenshots. Goal: Fix the clipped Save button. Read
> C:\Users\you\UIReports\2026-09-29_1432_fix-clipped-save\report.md and look at every image it lists
> (annotated and original), then help me with the goal.

Paste it into Claude Code; the details stay in `report.md`.

## Settings

Right-click the tray icon and choose **Settings**. Everything is saved when you press **OK** or **Apply**:

| Setting | What it does |
|---|---|
| Capture hotkey / Delayed capture hotkey | Click the field, then press the new combination. **Default** puts the original back. A combination that Windows or another program already uses is reported in the window, and the old one keeps working. |
| Delay length | How many seconds the delayed-capture countdown runs (1 to 30, default 3). |
| Reports folder | Where session folders are created (default `%USERPROFILE%\UIReports`). |
| Project folder, App / framework | Pre-filled into every new session (remembered from your last session). |
| Start UI Report Tool when I sign in to Windows | Off by default, see below. |
| On Finish, also paste it into the Claude app if it's open | On by default. If the Claude desktop app is open, Finish brings it to the front and pastes the text into its message box (one Ctrl+V; you press Enter). Claude is never started; if it is closed, or won't come to the front, the text is only copied. |
| Text copied on Finish | Your own text for the clipboard instead of the built-in Claude prompt. Fill-ins: `{shots}` ("3 screenshots"), `{count}`, `{goal}`, `{report}` (path of report.md), `{folder}`. Empty or **Default** = the built-in prompt. |

## Start with Windows

In **Settings**, tick **Start UI Report Tool when I sign in to Windows** and press OK. This adds one per-user
entry (`UIReportTool` under `HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run`) that starts the tool in
the tray when you sign in. Untick it and press OK to remove the entry again. Nothing outside your own user account is
touched, and you can also switch it off in **Windows Settings > Apps > Startup**. The Settings window always shows the
real state, so if you switched it off there, the box will be unticked.

## First run: a walkthrough

1. Double-click `setup.bat` (first time only), then `UIReportTool.bat`. On the very first launch a
   card near the clock tells you the tool is running.
2. Find the icon in the tray (maybe under **^**). Right-click → **Settings**, check the hotkeys, click OK.
3. Open something to report, for example a window whose button text is clipped. Press **Ctrl+Alt+S**.
   The screen freezes: drag around the problem area.
4. The editor opens. Type a caption ("Save button text is clipped"), choose the role **Problem**,
   add a **pin** on the clipped text and type a short note.
5. Press **Ctrl+Alt+Enter** (*Next, delayed*), open a menu or hover a button within 3 seconds, and let
   the screen freeze with that state visible. Change the role to **Want** for a mockup, or **Context**.
6. Try **Esc**: press Ctrl+Alt+S, then Esc. The selector closes and nothing is lost.
7. Fill in the **goal** (one or two sentences) and, once, your project folder and framework
   ("React + Tailwind"); they are remembered for the next session.
8. Press **Ctrl+Shift+Enter** (*Finish*). Click **Open folder** on the card to see the files.
9. Paste (Ctrl+V) into Claude Code. Done.

## Troubleshooting

* **"Could not register ... hotkey"**: another program already uses that combination. Pick a different one
  in Settings. The tray menu works in the meantime.
* **Nothing happens when I press the hotkey**: check the tray icon is there (the tool may not be running,
  or the icon is hidden under **^**). Another program may be capturing the keys; try a `Ctrl+Shift+F..` combination.
  On AltGr layouts see the note above.
* **Double-clicking `UIReportTool.bat` shows "not set up yet"**: run `setup.bat` first.
* **"Already running"** appears when you start it twice: use the existing tray icon.
* **The tool disappeared / crashed**: the log is `%APPDATA%\UIReportTool\uireport.log`; settings are in
  `%APPDATA%\UIReportTool\settings.json` (delete that file to reset to the defaults).
* **`setup.bat` fails while downloading**: check your internet connection and run it again.
* **The saved image looks too small on a 150 % display**: that is expected. Images are exact
  screen pixels; `report.md` lists both the pixel size and the logical size (what a developer sees).

## Uninstall

1. If you turned on *Start with Windows*, untick it in **Settings** first (or switch it off in Windows Settings > Apps > Startup).
2. Right-click the tray icon, **Quit**.
3. Delete this folder. Your reports stay in `%USERPROFILE%\UIReports` until you delete them; the settings and the log
   are in `%APPDATA%\UIReportTool`.

## For developers

```
.venv\Scripts\python.exe -m uireport                  # run with a console
.venv\Scripts\python.exe -m uireport --smoke-test --offscreen   # end-to-end self test, exits by itself
.venv\Scripts\pythonw.exe launch.pyw --quit           # ask the running copy to quit, like its tray Quit
.venv\Scripts\python.exe -m pytest tests -q           # unit + integration tests (Qt runs offscreen; no real screen or hotkeys)
```

`--quit` exits with 0 when the running copy was asked (it may still ask about unsaved shots), 3 when no copy is running,
and 1 when a copy is running that is too old to understand `--quit`. The App Launcher's Stop button uses it.

Other flags: `--settings-path`, `--output-root`, `--no-hotkeys`, `--instance-name`, `--verbose`.
`CONTRACT.md` describes the architecture.

## Ideas for later

* short screen **recordings** for animation and hover glitches
* a **compare mode** pairing each *Problem* shot with its *After* shot
* an **MCP server** so Claude can fetch the latest report itself, without pasting (`latest.json` is the hook)
