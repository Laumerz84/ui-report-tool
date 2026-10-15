# Capture anywhere: copy, save, drag, and simple per-hotkey actions (design)

Date: 2026-10-14. Approved in conversation ("and then go").

## Goal
UI Report Tool becomes a general screenshot utility without losing its simplicity: a capture can go to
the clipboard, to a file, or be dragged into any app, and the user can add hotkeys that each do one
clear thing. "Very easy to use hotkeys, like it is now."

## What the user gets
- **Built-in hotkeys unchanged:** Ctrl+Alt+S (report), Ctrl+Alt+D (report, delayed), Ctrl+Alt+Shift+C (Show Claude).
- **Extra hotkeys** (Settings > "More hotkeys", + Add hotkey): each row = hotkey + "Then..." + Delay on/off + remove.
  Then = Copy to clipboard / Save to folder / Copy + save / Add to report / Show Claude.
  Copy/Save hotkeys skip the editor entirely: capture, done, a small toast confirms (Save's toast has Open folder).
- **Editor buttons for the shot on screen:** Copy (Ctrl+Shift+C), Save as... (Ctrl+S), and a Drag handle; the
  filmstrip thumbnails can also be dragged out. All three use the shot WITH its markup (pins, arrows, blur).
- **Saving settings:** folder (default `<reports folder's parent>\Screenshots`, e.g. G:\Screenshots), PNG or JPG,
  name pattern (default `Screenshot {date} {time}`; fill-ins {date} {time} {n}).

## Rules
- The three built-in hotkeys keep their actions; new behaviour comes from extra rows (no surprises for today's flow).
- Copy puts an image on the clipboard (QClipboard.setImage). Files never overwrite: " (2)" etc. is appended.
- Drag-out renders a PNG into `<reports folder>\.drag\` (files older than a day are pruned) and offers it as a
  file URL plus image data, so Explorer, Discord, Word, Paint etc. all accept it. Internal reordering keeps working.
- The editor package still never imports output/app code: it asks the controller through signals and an
  injected file provider.
- No network. Nothing on C: by default.

## Verification
No unit tests (user rule). Real-run checks: an extra Copy hotkey capture puts an image on the clipboard; a Save
hotkey writes the file with the right name/format; the editor Copy includes markup; a drag file is produced.
