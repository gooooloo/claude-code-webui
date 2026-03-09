# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A web UI for Claude Code that replaces default terminal prompts with a browser-based interface. Users approve/deny tool executions, submit prompts, upload images, and manage sessions through any browser on the LAN (phones, tablets, desktops).

**Architecture:** Transcript-driven, cross-platform (Tmux on Linux/macOS, Windows Terminal on Windows), minimal hooks.

**Core principles:**
- **Transcript = single source of truth** — all session state is derived from transcript JSONL, server doesn't maintain a state machine
- **Platform-native prompt delivery** — prompts sent via `tmux send-keys` (Linux/macOS) or `WriteConsoleInput` (Windows), no file polling for prompts
- **3 Python hooks** — PermissionRequest, SessionStart, SessionEnd (no bash, no jq/curl dependencies)

**Flow:** Claude Code hook (Python) → writes JSON request to temp dir or POSTs to server → Python HTTP server serves dashboard UI that polls `/api/sessions` → user interacts → hook reads response JSON or server sends via tmux/console.

## Architecture

- **server.py** — Python HTTP server (port 19836). Session registry, transcript incremental parser, multi-session dashboard UI, API endpoints. Background threads for auto-allow and zombie session cleanup.
- **frontend.py** — Extracted HTML/CSS/JS for the dashboard UI. Imported by server.py.
- **permission-request.py** — `PermissionRequest` hook. Parses tool calls, checks `settings.local.json` for pre-approved glob patterns, falls back to auto-allow if server is offline, otherwise queues a request JSON and polls for response.
- **session-start.py** — `SessionStart` hook. Discovers transcript path, POSTs to `/api/session/register` with tmux pane/socket (Linux) or console_pid (Windows), cwd, and source.
- **session-end.py** — `SessionEnd` hook. POSTs to `/api/session/deregister`, local fallback cleanup of request files.
- **platform_utils.py** — Cross-platform utilities. OS detection, temp directory paths, process tree walking (via `/proc` on Linux, `CreateToolhelp32Snapshot` on Windows), path encoding.
- **win_send_keys.py** — Windows console input helper. Attaches to a target process's console via `AttachConsole` and injects keyboard input via `WriteConsoleInputW`. Runs as a subprocess to avoid disrupting the server's console.
- **channel_feishu.py** — Optional Feishu notification channel. Polls `/api/sessions` for state changes, sends permission cards and idle cards. Prompt delivery via `/api/send-prompt`. Also manages Feishu topic naming (first user prompt), message routing by thread_id, and session pinning/unpinning.
- **channel_teams.py** — Optional Microsoft Teams notification channel. Two modes: webhook (notification-only via Incoming Webhook) and graph (bidirectional via Microsoft Graph API with OAuth2 client_credentials). Sends Adaptive Cards for permission requests, syncs transcript entries, receives user decisions and prompts.
- **install.sh** — Linux/macOS installer. Creates symlinks and merges hook config into settings.json. Requires `--project`, `--global`, or `--all`. Depends on `jq`.
- **install.ps1** — Windows installer (PowerShell). Copies hook files and merges hook config into settings.json. Accepts `-Scope Project|Global|All`.
- **uninstall.sh** — Linux/macOS uninstaller. Reverses install.sh. Depends on `jq`.
- **uninstall.ps1** — Windows uninstaller (PowerShell). Reverses install.ps1.
- **dev.sh** — Development helper. Uses `entr` to auto-restart `server.py` when `frontend.py`, `server.py`, or `channel_feishu.py` changes.

## Running

```bash
# Start the server (Linux/macOS/Windows)
python3 server.py          # localhost only (default)
python3 server.py --lan    # bind 0.0.0.0 for LAN access

# MultiView hub mode (central machine)
python3 server.py --lan --name hub

# MultiView remote (register with hub, manual tunnel ID)
python3 server.py --lan --name "GPU-A100" --tunnel-id 1c6j6jlh --hub-tunnel-id abc123

# MultiView remote (auto-detect tunnel ID)
python3 server.py --lan --name "GPU-A100" --detect-tunnel --hub-tunnel-id abc123

# Development mode (auto-restart on file changes, requires entr, Linux/macOS only)
./dev.sh

# Install hooks — Linux/macOS
/path/to/install.sh --project   # Project-level only
/path/to/install.sh --global    # Global (~/.claude) + symlinks
/path/to/install.sh --all       # Both project + global

# Install hooks — Windows (PowerShell)
.\install.ps1 -Scope Project
.\install.ps1 -Scope Global
.\install.ps1 -Scope All

# Uninstall hooks — Linux/macOS
/path/to/uninstall.sh --project
/path/to/uninstall.sh --global
/path/to/uninstall.sh --all

# Uninstall hooks — Windows (PowerShell)
.\uninstall.ps1 -Scope Project
.\uninstall.ps1 -Scope Global
.\uninstall.ps1 -Scope All
```

No build step, no test suite, no linter.

**Linux/macOS deps:** Python 3, `jq` (install/uninstall scripts), Bash (install scripts). Optional: `entr` (dev.sh).

**Windows deps:** Python 3, PowerShell 5.1+ (install/uninstall scripts). No additional tools required.

## Key Conventions

### Allow Pattern Format
Patterns in `settings.local.json` use `ToolName(pattern)` format with glob matching:
- `Bash(git commit:*)` — `:*` suffix means "starts with"
- `Write(/some/path/*)` — directory-scoped write permission
- `Read`, `WebFetch` — tool-level blanket allow

### Python Escape Sequences in server.py
`HTML_PAGE` is a `"""` triple-quoted string containing inline JS. Python processes escape sequences inside it:
- `\'` in source → `'` in output (not `\'`)
- `\\'` in source → `\'` in output (backslash + quote)
- Rule: for each `\` needed in rendered output, write `\\` in the Python source

### Session Management
- Session ID = PPID (Claude Code's process ID)
- Sessions are registered via `/api/session/register` (from session-start.py hook)
- Session state is derived from transcript JSONL (not stored as a state machine)
- Session-level auto-allow rules are stored in-memory on the server as `{(session_id, tool_name): True}`
- Zombie sessions (dead PIDs) are cleaned up every 30 seconds
- **Pane/console eviction:** when a new session registers on the same tmux pane (Linux) or console_pid (Windows), previous sessions on that pane/console are automatically evicted (including their auto-allow rules)
- **Registration source parameter:** the `source` field (`startup`, `resume`, `clear`, `compact`) controls behavior:
  - `startup` or new session — creates fresh session state
  - `resume`/`compact` — updates transcript path and resets parsing offset, preserving auto-allow rules
  - `clear` — resets transcript offset **and** clears auto-allow rules and stale request files

### Transcript-Driven State
The server reads transcript JSONL files incrementally (tracking byte offset). State is derived from the tail:

| Transcript pattern | Derived state |
|---|---|
| Last assistant `stop_reason: "end_turn"`, no pending tool_use | **idle** |
| Last user message after last assistant | **busy** |
| Unresolved tool_use in last assistant | **busy** |
| `.request.json` exists, tool_use has no tool_result | **permission_prompt** |
| Last tool_use is `AskUserQuestion` | **elicitation** |
| Last tool_use is `ExitPlanMode` | **plan_review** |

### Prompt Delivery
**Linux/macOS (Tmux):** SessionStart hook passes `$TMUX_PANE` and `$TMUX`. Server extracts socket path and sends prompts via `tmux load-buffer` + `tmux paste-buffer` + `tmux send-keys Enter`.

**Windows (Console):** SessionStart hook passes `console_pid` (parent shell PID). Server invokes `win_send_keys.py` as a subprocess, which attaches to the target console via `AttachConsole` and injects keyboard input via `WriteConsoleInputW`.

### Server Offline Fallback
All hooks auto-approve when the server is unreachable, so Claude Code continues to function normally.

### File Communication
Only used for PermissionRequest blocking:
```
<queue_dir>/
  ├── *.request.json    (pending permission requests)
  └── *.response.json   (user decisions)
```
Queue dir: `/tmp/claude-webui` (Linux/macOS) or `%TEMP%\claude-webui` (Windows).

### API Endpoints
| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/` | Dashboard HTML |
| GET | `/multiview` | MultiView page (multi-machine link panel) |
| GET | `/api/sessions` | All sessions with transcript-derived state |
| GET | `/api/session/<id>/transcript` | Parsed transcript entries |
| GET | `/api/pending` | Pending permission requests |
| GET | `/api/image?path=` | Serve uploaded images |
| GET | `/api/multiview/remotes` | Registered remote machines for MultiView |
| POST | `/api/session/register` | Register/update session |
| POST | `/api/session/deregister` | Deregister session |
| POST | `/api/respond` | Approve/deny permission |
| POST | `/api/session-allow` | Session-level auto-allow |
| POST | `/api/send-prompt` | Send prompt via tmux/console |
| POST | `/api/upload-image` | Upload image |
| POST | `/api/multiview/register` | Remote machine self-registration (heartbeat) |
| POST | `/api/session-reset` | Clear session auto-allow rules (legacy) |
| POST | `/api/session-end` | Remove session and clear auto-allow (legacy) |

### MultiView
MultiView (`/multiview`) provides a centralized page to access the same WebUI service across multiple machines. Remote servers register with a hub via heartbeat; the MultiView page auto-discovers and lists all registered machines.

**CLI arguments for hub registration:**

| Argument | Purpose |
|----------|---------|
| `--hub-tunnel-id` | DevTunnels ID of the hub, registers this machine with the hub |
| `--tunnel-id` | DevTunnels ID for this machine |
| `--detect-tunnel` | Auto-detect this machine's devtunnel ID via `devtunnel list` |

**How it works:**
- Remote servers with `--hub-tunnel-id` send a heartbeat (`POST /api/multiview/register`) every 30 seconds with their name and public URL
- The hub keeps an in-memory registry; entries expire after 90 seconds without heartbeat
- The MultiView page polls `GET /api/multiview/remotes` every 15 seconds and lists all machines with "Open" links (new tab)

## Writing Conventions
- All documentation must be written in English.
