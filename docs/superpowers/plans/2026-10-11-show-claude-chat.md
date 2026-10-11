# Show Claude chat - implementation plan

> For agentic workers: executed natively in the session that wrote it (user's choice: "just do it").
> No unit tests (user's global CLAUDE.md); three end-to-end checks at the end (chosen by the user).

**Goal:** a third hotkey (Ctrl+Alt+Shift+C) that captures with today's overlay and sends the
image straight into a Claude Code chat window inside UI Report Tool.

**Architecture:** new `uireport/chat/` package (runner, approvals, sessions, folder guess,
window). The controller routes a capture started by the chat hotkey to the chat window instead
of the editor.

**Tech stack:** PySide6, the user's `claude` CLI (`-p`, stream-json in/out), a stdlib stdio MCP
server for approvals, psutil.

**Spec:** `docs/superpowers/specs/2026-10-11-show-claude-chat-design.md`

## Global constraints
- Default hotkey: `Ctrl+Alt+Shift+C`, setting `hotkey_chat`, hotkey name `chat`.
- Pre-allowed tools: Read, Glob, Grep, WebSearch, WebFetch, TodoWrite. Everything else asks.
- Approval timeout: 10 minutes, then deny. Any approval failure means deny.
- Images: PNG, longest side at most 1568 px.
- The app's own code: no sockets or network modules. Approvals go through files.
- Main-app sessions continue with `--resume --fork-session`; this window's own sessions continue with `--resume`.
- The report, Finish and paste flow stays unchanged.

## Review focus
- The chat hotkey pressed while a report session's editor is open: the editor must come back afterwards.
- The chat hotkey pressed while Claude is answering: the screenshot is queued, not lost.
- Changing the folder mid-conversation: a new chat starts. A resumed session needs its own cwd.
- An approval while the window is hidden: the window comes forward. No answer means deny after 10 minutes.
- `claude` missing or logged out: a plain message, no crash.

## Tasks
1. **Hotkey + settings:** add `NAME_CHAT` to `hotkeyspec.py`. In `settings.py`, add `hotkey_chat`
   and `chat_last_folder` (validation and dedupe across all three hotkeys). In
   `settings_window.py`, add a third hotkey row. In `controller.py`, add the mapping, the label,
   and the revert-on-failure step.
2. **`uireport/chat/runner.py`:** `find_claude()`, `png_bytes(QImage)`, and `ClaudeRun(QObject)`.
   `start(folder, text, images, session_id, fork, approval_dir)` streams the run. Signals:
   `text_delta`, `tool`, `session`, `status`, `finished(ok, message)`. `stop()` ends it.
3. **`uireport/chat/approve_mcp.py`** (standalone stdio MCP, stdlib only) and
   **`uireport/chat/approvals.py`** (`ApprovalWatcher` polls the folder for requests and writes
   answers; `ApprovalCard` widget; `describe()`).
4. **`uireport/chat/sessions.py`:** `list_sessions()` reads `~\.claude\projects` and returns
   `SessionInfo(id, title, cwd, modified, path)`. `load_messages(path)` returns the last turns.
   It also handles the registry of this window's own session ids.
5. **`uireport/chat/folder_guess.py`:** `known_folders()` reads the codebase-dashboard
   `recent.json` (sibling folder) plus session cwds. `guess(window, known, last)` matches the
   exe, command line and title.
6. **`uireport/chat/window.py`:** `ChatWindow` with a header (folder, New chat, History, Stop),
   a transcript (markdown with image thumbnails), an input box, approval cards and a queue.
7. **Controller wiring:** `_on_hotkey(NAME_CHAT)` sets the chat flag and calls
   `request_capture(NORMAL)`. In `_on_captured`, a chat-flagged shot goes to `chat.send_screenshot`
   and the editor is restored. The cancel and fail paths clear the flag.
8. **Docs:** README, CONTRACT, CLAUDE.md.
9. **E2E checks** (real Claude, real screen): hotkey to answer, follow-up plus second shot, and
   continuing a main-app chat as a copy. Then restart the app detached, and commit.
