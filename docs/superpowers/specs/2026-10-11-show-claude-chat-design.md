# Show Claude: an instant screenshot chat (design)

Date: 2026-10-11. Status: approved in conversation, awaiting review of this written spec.

## Goal

**What the user asked for:** keep today's report workflow (capture → editor → Finish → paste),
and add a second, more seamless mode. It is the user's own separate Claude chat, where one
action makes Claude see the screen. There is no report, no Finish and no pasting.

**Success:** from anywhere, one hotkey and a drag or click puts the screenshot in front of
Claude, which starts answering within about a second. The user keeps chatting in the same
conversation and can pick up conversations from the Claude app's Code tab.

**Decisions made with the user:**

| Question | Answer |
| --- | --- |
| Where it lives | Inside UI Report Tool (approach A). The Claude logic is adapted from Ekxel's panel, not shared with it. |
| What a capture can be | Drag a region, click a window, or the whole screen (today's overlay: drag, click, `F`). |
| Trigger | A new global hotkey only, default **Ctrl+Alt+Shift+C** (checked free on this PC). It can be changed in Settings. |
| What Claude may do | Full Claude Code in a project folder. It asks before editing files or running commands. |
| Repeat captures | Join the current conversation. A **New chat** button starts over. |
| Project folder | Guessed from the captured window, otherwise the last folder used. A dropdown changes it. |
| Main-app chats | The History list includes Code-tab sessions from the Claude app. They continue as a **copy**. |

## What the user sees

1. **Ctrl+Alt+Shift+C** from anywhere: the screen freezes with today's capture overlay. Drag a
   region, click a window, or press `F` for the whole monitor. `Esc` cancels.
2. On release, the **chat window** appears (always on top while open) with the screenshot
   already sent as a thumbnail message. Claude's reply streams in.
3. The user types follow-ups. Another hotkey press adds a new screenshot to the **same**
   conversation. If Claude is mid-answer, the screenshot waits and goes in as the next message.
4. **Header:** a project-folder dropdown, **New chat**, **History**, and **Stop** (shown while
   Claude is answering).
5. **Approval card:** before an edit or a command, a card shows what Claude wants to do, with
   **Allow** and **Deny**.
6. **History:** past conversations, newest first, with title, project and time. They come from
   this window and from the Claude app's Code tab, and Ekxel/CLI ones are included. Clicking
   one continues it.
7. Closing the window hides it and keeps the conversation. The next hotkey press continues it.

## Architecture

The new package is `uireport/chat/`. Each unit has one job.

| Unit | Job | Interface (sketch) |
| --- | --- | --- |
| `runner.py` | Runs one conversation turn with the user's Claude Code and streams events back. Adapted from `G:\Sheets\sheets\ai\panel.py`. | `ClaudeRun(QObject)`: `start(folder, blocks, session_id=None, fork=False)`, `stop()`. Signals: `text_delta(str)`, `tool_use(str)`, `session(str)`, `finished(ok, message)`. |
| `window.py` | The chat window: message list (text and image thumbnails), input box, header controls, approval cards. | `ChatWindow(QWidget)`: `show_and_send_image(QImage, guessed_folder)`, `open_session(SessionInfo)`. |
| `approvals.py` + `approve_mcp.py` | Claude Code's permission prompt. `approve_mcp.py` is a tiny stdio MCP server, started by Claude Code via `--mcp-config` and run with the venv python. Its one tool, `approve`, writes a request file and waits for the answer file. `approvals.py` in the app watches the folder and shows the card. | Files in `%APPDATA%\UIReportTool\chat\approvals\`: `<id>.request.json` {tool_name, input}, `<id>.answer.json` {behavior: allow/deny, message}. |
| `sessions.py` | Lists conversations from `~\.claude\projects\*\*.jsonl` (subagent files skipped). | `list_sessions() -> [SessionInfo(id, title, cwd, modified, origin)]`. The title comes from `custom-title`, then `ai-title`, then `summary`, then the first user text. |
| `folder_guess.py` | Maps the captured window to a project folder. | `guess(window_meta, known_folders, last) -> str`. |

**Changes to existing code:**
- `settings.py`: `hotkey_chat` (default `Ctrl+Alt+Shift+C`) and `chat_last_folder`.
- `settings_window.py`: a third hotkey row, "Show Claude".
- The hotkey manager registers the third hotkey.
- `controller.py`: the chat hotkey starts a normal capture tagged as "for chat". When it finishes, the controller hands the shot to the chat window instead of opening the editor. Cancel paths are unchanged.
- The report and Finish flow, including the paste, are **not changed**.

**Claude Code invocation** (per turn, in the chosen folder):

    claude -p --input-format stream-json --output-format stream-json --verbose
           --include-partial-messages
           --mcp-config <cfg with approve_mcp.py> --permission-prompt-tool mcp__uireport__approve
           --allowedTools Read Glob Grep WebSearch WebFetch TodoWrite
           [--resume <session_id> [--fork-session]]

- The user message is one stream-json message with a text block and image blocks. Images are
  PNGs scaled to at most 1568 px on the long side.
- The session id from the first turn is kept for later turns.
- Edits (Edit/Write) and commands (Bash) are not pre-allowed, so they go through the `approve` tool.

**Resume rules:**
- A session started in this window continues with `--resume`.
- A session from anywhere else (Claude app, Ekxel, CLI) continues with `--resume --fork-session`, so the original file is never written to.
- Claude Code runs in the session's recorded `cwd`. If that folder no longer exists, the chat says it can't be continued.

**Folder guess:**
1. Collect candidate project folders from `G:\codebase-dashboard\recent.json` (if present) and the `cwd`s in the session list. Scratch and temp folders are dropped.
2. Look for a candidate that contains a path from the captured window: the exe path, the paths on its process command line (`launch.pyw` of UI Report Tool or Ekxel), or a path in the window title.
3. Take the longest match. Otherwise use `chat_last_folder`. Otherwise use the user's home folder.

## Errors and edge cases

- **`claude` not found or not logged in:** a plain message in the chat with the fix (`claude` to log in).
- **Hotkey during an answer:** the screenshot is queued as the next message. **Stop** kills the run.
- **Approval while the window is hidden:** the window comes forward with the card. No answer in 10 minutes means deny. Any failure of the approval path means deny.
- **No tools act without approval** except read-only ones (Read, Glob, Grep, WebSearch, WebFetch, TodoWrite).
- **Folder gone on resume:** "This conversation's folder no longer exists, so it can't be continued."
- **The no-network rule** (the test in `tests/test_integration_local_only.py`): the app's own code stays socket-free. Approvals go through files. Only the `claude` process talks to Anthropic.

## Docs

`README.md` (new section), `CONTRACT.md` (chat package and rules), and `CLAUDE.md` (how the chat
reaches Claude) are updated with the build.

## Testing

Per the user's instructions there are **no unit tests**. The existing suite is left as is.
At the end, three end-to-end checks run with real Claude Code and a real screen:

1. **Hotkey to answer:** the chat capture flow runs. In-process, the real overlay is driven by Qt
   mouse events, and the hotkey's global registration is checked separately. A region is
   dragged, and Claude replies about its content.
2. **Follow-up + second shot:** a typed follow-up and a second capture go into the same session,
   and Claude's answer refers to the first screenshot.
3. **Continue a main-app chat:** a Code-tab session from History continues as a copy. The
   original `.jsonl` is unchanged (checked by hash), and the copy has a new session id.

## Out of scope

- Chat-tab conversations from the Claude app: they are stored on Anthropic's servers, not locally.
- Making chats from this window appear in the Claude app's sidebar. Whether the app lists CLI
  sessions is unknown, so it is not promised.
- Pasting clipboard images into the chat (possible later).
