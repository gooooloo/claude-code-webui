# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A web UI for Claude Code that replaces default terminal prompts with a browser-based interface. Users approve/deny tool executions, submit prompts, upload images, manage sessions, and export conversations through any browser or Feishu.

**Architecture:** Transcript-driven, cross-platform (Tmux on Linux/macOS, Windows Terminal on Windows), minimal hooks.

**Core principles:**
- **Transcript = single source of truth** — all session state is derived from transcript JSONL, server doesn't maintain a state machine
- **Platform-native prompt delivery** — prompts sent via `tmux send-keys` (Linux/macOS) or `WriteConsoleInput` (Windows), no file polling for prompts
- **3 Python hooks** — PermissionRequest, SessionStart, SessionEnd (no bash, no jq/curl dependencies)

**Flow:** Claude Code hook (Python) → writes JSON request to temp dir or POSTs to server → Python HTTP server serves dashboard UI that polls `/api/sessions` → user interacts → hook reads response JSON or server sends via tmux/console.

## Architecture

- **server.py** — Python HTTP server (port 19836). Session registry, transcript incremental parser, multi-session dashboard UI, API endpoints. Background thread for zombie session cleanup. Stores volatile session-level auto-allow rules in memory (queried by hook via API).
- **frontend.py** — Extracted HTML/CSS/JS for the dashboard UI. Imported by server.py.
- **hook-permission-request.py** — `PermissionRequest` hook. All auto-allow decision logic is centralized here (persistent rules, smart rules, session rules via server API query). Falls back to auto-allow if server is offline. If nothing matches, queues a request JSON and polls for response.
- **hook-session-start.py** — `SessionStart` hook. Discovers transcript path, POSTs to `/api/session/register` with `terminal_id` (tmux pane ID on Linux, shell PID on Windows), `tmux_socket` (Linux only), cwd, and source.
- **hook-session-end.py** — `SessionEnd` hook. POSTs to `/api/session/deregister`, local fallback cleanup of request files.
- **platform_utils.py** — Cross-platform utilities. OS detection, temp directory paths, process tree walking (via `/proc` on Linux, `CreateToolhelp32Snapshot` on Windows), path encoding.
- **win_send_keys.py** — Windows console input helper. Attaches to a target process's console via `AttachConsole` and injects keyboard input via `WriteConsoleInputW`. Runs as a subprocess to avoid disrupting the server's console.
- **channel_feishu.py** — Optional Feishu notification channel. Polls `/api/sessions` for state changes, sends permission cards and idle cards. Prompt delivery via `/api/send-prompt`. Also manages Feishu topic naming (first user prompt), message routing by thread_id, and session pinning/unpinning.
- **install.sh** — Linux/macOS installer. Creates symlinks and merges hook config into settings.json. Flags: `--project`, `--global`, `--daemon`. Depends on `jq`. `--daemon` installs systemd services (Linux only).
- **claude-webui-linux.service** — Systemd unit for server.py (auto-start, crash restart).
- **claude-webui-watcher-linux.service** — Systemd unit that watches `.py` files and restarts the server on changes.
- **install.ps1** — Windows installer (PowerShell). Copies hook files and merges hook config into settings.json. Accepts `-Scope Project|Global|All`.
- **uninstall.sh** — Linux/macOS uninstaller. Reverses install.sh. Flags: `--project`, `--global`, `--daemon`. Depends on `jq`.
- **uninstall.ps1** — Windows uninstaller (PowerShell). Reverses install.ps1.
- **dev.sh** — Development helper. Uses `entr` to auto-restart `server.py` when `frontend.py`, `server.py`, or `channel_feishu.py` changes.

## Running

```bash
# Start the server (Linux/macOS/Windows)
python3 server.py          # localhost only (default)
python3 server.py --lan    # bind 0.0.0.0 for LAN access
# Development mode (auto-restart on file changes, requires entr, Linux/macOS only)
./dev.sh

# Install hooks — Linux/macOS
/path/to/install.sh --project   # Project-level only
/path/to/install.sh --global    # Global (~/.claude) + symlinks
/path/to/install.sh --daemon    # Install systemd service (Linux only)
/path/to/install.sh --global --daemon  # Combine flags

# Install hooks — Windows (PowerShell)
.\install.ps1 -Scope Project
.\install.ps1 -Scope Global
.\install.ps1 -Scope All

# Uninstall hooks — Linux/macOS
/path/to/uninstall.sh --project
/path/to/uninstall.sh --global
/path/to/uninstall.sh --daemon

# Uninstall hooks — Windows (PowerShell)
.\uninstall.ps1 -Scope Project
.\uninstall.ps1 -Scope Global
.\uninstall.ps1 -Scope All
```

No build step, no linter.

**Testing:** `python3 -m pytest tests/ -v` — 103 tests covering core logic (platform_utils, permission rules, server state derivation). Run tests after any code change to these modules.

**Linux/macOS deps:** Python 3, `jq` (install/uninstall scripts), Bash (install scripts). Optional: `entr` (dev.sh), `inotify-tools` (--daemon).

**Windows deps:** Python 3, PowerShell 5.1+ (install/uninstall scripts). No additional tools required.

## Key Conventions

### Auto-Allow Tiers
All auto-allow logic lives in `hook-permission-request.py`. Checked in order, first match wins:

| Tier | Name | Where it lives | Lifetime | Example |
|------|------|---------------|----------|---------|
| 1 | Persistent rules | `.claude/settings.local.json` | Survives restarts | `Bash(git commit:*)` |
| 2 | Smart rules | Hook code | Always on | Read-only tools, read-only Bash, project-internal edits |
| 3 | Tmux allowlist | Hook code | Always on | `tmux send-keys` (used by WebUI prompt delivery) |
| 4 | Session rules | Server memory, queried via `GET /api/check-auto-allow` | Until session end/clear/restart | User clicks "Allow for session" in UI |
| 5 | Server offline | Hook code | Fallback | Server unreachable → allow everything |

**Why session rules live on the server, not the hook:** The hook is a short-lived process (runs once per permission request, then exits). It has no persistent memory. Session rules need to survive across multiple hook invocations within a session, so they're stored in the server's memory and queried via API. They can't be written to a file by the hook because the server manages their lifecycle (cleared on session end, clear, zombie cleanup, pane eviction).

### Allow Pattern Format
Patterns in `settings.local.json` (tier 1) use `ToolName(pattern)` format with glob matching:
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
- Sessions are registered via `/api/session/register` (from hook-session-start.py hook)
- Session state is derived from transcript JSONL (not stored as a state machine)
- Session-level auto-allow rules: see tier 4 in [Auto-Allow Tiers](#auto-allow-tiers)
- Zombie sessions (dead PIDs) are cleaned up every 30 seconds
- **Terminal eviction:** when a new session registers with the same `terminal_id` (tmux pane on Linux, shell PID on Windows), previous sessions on that terminal are automatically evicted (including their auto-allow rules)
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
SessionStart hook registers `terminal_id` — a stable per-pane identifier (tmux pane ID on Linux/macOS, shell PID on Windows). The server uses it for prompt delivery:

**Linux/macOS (Tmux):** `terminal_id` = `$TMUX_PANE`. Server sends prompts via `tmux load-buffer` + `tmux paste-buffer` + `tmux send-keys Enter`, using `tmux_socket` for the socket path.

**Windows (Console):** `terminal_id` = shell PID (claude's parent process). Server invokes `win_send_keys.py` as a subprocess, which calls `AttachConsole(shell_pid)` and injects keyboard input via `WriteConsoleInputW`.

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
| GET | `/api/sessions` | All sessions with transcript-derived state |
| GET | `/api/session/<id>/transcript` | Parsed transcript entries |
| GET | `/api/check-auto-allow` | Check session auto-allow rules (used by hook) |
| GET | `/api/pending` | Pending permission requests |
| GET | `/api/image?path=` | Serve uploaded images |
| POST | `/api/session/register` | Register/update session |
| POST | `/api/session/deregister` | Deregister session |
| POST | `/api/respond` | Approve/deny permission |
| POST | `/api/session-allow` | Session-level auto-allow |
| POST | `/api/send-prompt` | Send prompt via tmux/console |
| POST | `/api/upload-image` | Upload image |
| POST | `/api/session-reset` | Clear session auto-allow rules (legacy) |
| POST | `/api/session-end` | Remove session and clear auto-allow (legacy) |

## Writing Conventions
- All documentation must be written in English.

## Development Rules
- **UI changes require Playwright verification**: Any change to `frontend.py` or other UI-related code must be verified using Playwright (via MCP) before considering the task complete. Start the server (`python3 server.py`), navigate to the relevant page, and visually confirm the change works as expected. Take a screenshot to show the result.
