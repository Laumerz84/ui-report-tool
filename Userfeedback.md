# User Feedback

Feedback and recommendations from users. Newest first.

## 2026-10-16: Finish pastes the report into both Ekxel's Claude panel and the Claude app

**Status: fixed (2026-10-16).** `send_to_ekxel` now returns ok / unconfirmed / failed and sends a `request_id`. Only "failed" (Ekxel not running, too old, or it said no) falls back to pasting into Claude. A late answer is "unconfirmed": nothing is pasted, and the toast says "Sent to Ekxel's Claude (Ekxel is busy and hasn't confirmed yet)" with a "Paste into Claude instead" action. Ekxel now answers at once and delivers right after, dropping repeats (Laumerz84/sheets, same date). Checked with Ekxel's delivery slowed to 5 s: old Ekxel gives unconfirmed (3.1 s, no second paste), new Ekxel gives ok in 0.03 s with one delivery. Not done here: the redact/PII item below.


**Observed.** One Finish put the same prompt into Ekxel's Claude panel and into the Claude desktop app's message
box. It should go to exactly one place.

**Cause.** `Controller._on_write_done` (`uireport/app/controller.py`) calls `ekxel_link.send_to_ekxel(prompt,
folder)`, which waits up to 3 s for Ekxel's `{"ok": true}`. If the answer is late, `to_ekxel` is False and the code
falls through to `self._paste_fn(prompt)`, pasting into Claude as well. Ekxel answers only after it has read the
report, checked the images, raised its window and queued the prompt on its GUI thread; with a large protected
workbook that takes longer than 3 s, so Ekxel had in fact taken the prompt.

**Recommendation.**

1. Treat "written to Ekxel's socket but no answer yet" as delivered-or-unknown, not as failed: don't paste into
   Claude; show a toast "Sent to Ekxel (no confirmation yet)" with a "Paste into Claude instead" action.
2. Only fall back automatically when the connection itself fails (Ekxel not running / older version).
3. Send a request id with the message and let Ekxel reply as soon as it has accepted it (Ekxel side: Laumerz84/sheets
   `Userfeedback.md`, same date), so a late reply can't cause a second delivery.
4. Make the choice visible: the Finish toast names where the report went, and "Finish to..." stays available to
   pick the other target.

**Test.** With Ekxel slowed (a sleep in its inbox handler of 5 s), press Finish while Ekxel was last used: the
prompt appears only in Ekxel's panel, the toast says so, and the Claude app's message box is untouched.

## 2026-10-16: Screenshots can carry client data to Claude

**Observed.** A capture of a client spreadsheet (names, account numbers) went to Claude through Finish. Ekxel holds
back screenshots of protected workbooks, but the Claude app receives whatever was captured.

**Recommendation.** Before Finish: a redact step in the editor (drag to black out or blur, applied to the saved
image, not as a removable layer), and a warning when a shot's window title or OCR text looks like it holds personal
data (account-number patterns, many capitalised name pairs), with "Redact" / "Remove shot" / "Send anyway".
