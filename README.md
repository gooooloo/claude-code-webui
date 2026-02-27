# claude-code-webui

A web UI for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that replaces default terminal prompts with a browser-based interface. Approve/deny tool calls, submit prompts, upload images, and manage sessions from your phone, tablet, or any browser on your network.

## How it works

**Permission approval flow:**
```
Claude Code                          Web browser
      |                                   |
      |-- PermissionRequest hook ------>  |
      |   (permission-request.sh)             |
      |   writes .request.json to         |
      |   /tmp/claude-webui/              |
      |                                   |
      |          server.py                |
      |          polls queue dir          |
      |          serves web UI  --------> |  User sees request
      |                                   |  clicks Allow / Deny
      |          writes .response.json <--|
      |                                   |
      |<-- hook reads response            |
      |   (allow or deny)                 |
```

**Prompt submission flow (when Claude is idle):**
```
Claude Code                          Web browser
      |                                   |
      |-- Stop hook fires                 |
      |   (stop.sh)                  |
      |   writes .prompt-waiting.json     |
      |                                   |
      |          server.py                |
      |          shows prompt input  ---> |  User types new prompt
      |                                   |  clicks Submit
      |          writes .prompt-response  |
      |                                   |
      |<-- hook reads response            |
      |   (blocks stop, passes prompt)    |
```

### Components

1. **`permission-request.sh`** — `PermissionRequest` hook. Receives the tool call, writes a `.request.json` to `/tmp/claude-webui/`, and polls for a `.response.json`.
2. **`server.py`** — Python HTTP server (port 19836). Serves a single-page UI that shows pending requests and lets you approve/deny them.
3. **`post-tool-use.sh`** — `PostToolUse` hook. Cleans up stale request/response files after a tool finishes executing.
4. **`stop.sh`** — `Stop` hook. When Claude finishes a task, writes a `.prompt-waiting.json` so the Web UI can accept a follow-up prompt.
5. **`user-prompt-submit.sh`** — `UserPromptSubmit` hook. Cleans up waiting files when a prompt is submitted (from terminal or tmux send-keys).
6. **`install.sh`** — Registers the hooks in a project's `.claude/settings.json`.

## Features

- **Allow / Deny** — approve or reject individual tool calls
- **Always Allow** — approve and add the pattern to `settings.local.json` so it won't ask again
- **Allow Path** — for Write/Edit tools, allow all operations under a directory with hierarchical selection
- **Split Always Allow** — compound Bash commands (pipes and `&&`) are split into individual patterns
- **Session-level auto-allow** — server-side evaluation with multi-select support
- **Prompt submission** — submit follow-up prompts from the Web UI when Claude is idle
- **tmux mode** — in tmux sessions, prompts are delivered via `send-keys` for seamless operation
- **AskUserQuestion support** — answer Claude's questions with option selection or custom text
- **Image upload** — attach images in the Web UI prompt area
- **WebFetch/WebSearch** — permission handling for web access tools
- **Graceful fallback** — when the server is offline, all hooks auto-approve so Claude Code works normally
- Auto-cleanup of stale requests (dead processes)
- Dark-themed, mobile-friendly UI

## Requirements

- Python 3
- `jq`
- `curl`
- Bash
- `uuidgen` (available by default on macOS; install `uuid-runtime` on Debian/Ubuntu)

## Installation

1. Clone this repo anywhere you like:
   ```bash
   git clone https://github.com/gooooloo/claude-code-webui.git
   ```

2. Start the server:
   ```bash
   /path/to/claude-code-webui/server.py
   ```

3. In any project directory, install the hooks:
   ```bash
   /path/to/claude-code-webui/install.sh
   ```

4. **Restart Claude Code** if it's already running — hooks are loaded at startup and won't take effect until the next session.

5. Open `http://localhost:19836` in your browser (or use your machine's LAN IP from a phone/tablet).

6. Run Claude Code — permission requests will appear in the web UI instead of the terminal.

## Hook behavior matrix

Each hook's behavior depends on whether the server is running and whether Claude Code is inside a tmux session:

| Hook | Trigger | Non-tmux + Server Online | Non-tmux + Server Offline | tmux + Server Online | tmux + Server Offline | Timeout |
|------|---------|--------------------------|---------------------------|----------------------|-----------------------|---------|
| **PermissionRequest** (`permission-request.sh`) | Claude requests tool permission | Write request file → poll for approval → allow/deny | Allow immediately, no file written | Same as non-tmux | Allow immediately, no file written | 24h |
| **PostToolUse** (`post-tool-use.sh`) | After tool execution | Clean up request/response files | Same (local files only) | Same | Same | 5s |
| **Stop** (`stop.sh`) | Claude is about to stop | Write waiting file → poll for new prompt → block/approve | Approve immediately, no file written | Write waiting file → approve immediately (Web UI uses tmux send-keys) | Approve immediately, no file written | 24h |
| **UserPromptSubmit** (`user-prompt-submit.sh`) | User submits a prompt | Clean up waiting files for current session | Same (local files only) | Same | Same | 5s |

When the server is offline, all hooks gracefully fall back to non-blocking behavior — permissions are auto-allowed and stop hooks approve immediately — so Claude Code works normally without the web UI.

## Security note

The server binds to `0.0.0.0:19836` by default, making it accessible from your local network. This is intentional — it allows you to approve requests from a phone or another device. If you only need local access, change the bind address to `127.0.0.1` in `server.py`.

There is no authentication on the web UI. Anyone on your network who can reach port 19836 can approve or deny requests. Use on trusted networks only, or add your own auth layer.

## License

MIT
