# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A web UI for Claude Code that replaces default terminal prompts with a browser-based interface. Users approve/deny tool executions, submit prompts, upload images, and manage sessions through any browser on the LAN (phones, tablets, desktops).

**Architecture:** Transcript-driven, Tmux-only, minimal hooks.

**Core principles:**
- **Transcript = single source of truth** — all session state is derived from transcript JSONL, server doesn't maintain a state machine
- **Tmux-only prompt delivery** — prompts sent via `tmux send-keys`, no file polling for prompts
- **3 Python hooks** — PermissionRequest, SessionStart, SessionEnd (no bash, no jq/curl dependencies)

**Flow:** Claude Code hook (Python) → writes JSON request to `/tmp/claude-webui/` or POSTs to server → Python HTTP server serves dashboard UI that polls `/api/sessions` → user interacts → hook reads response JSON or server sends via tmux.

## Architecture

- **server.py** — Python HTTP server (port 19836). Session registry, transcript incremental parser, multi-session dashboard UI, API endpoints. Background threads for auto-allow and zombie session cleanup.
- **permission-request.py** — `PermissionRequest` hook. Parses tool calls, checks `settings.local.json` for pre-approved glob patterns, falls back to auto-allow if server is offline, otherwise queues a request JSON and polls for response.
- **session-start.py** — `SessionStart` hook. Discovers transcript path, POSTs to `/api/session/register` with tmux pane, socket, cwd, and source.
- **session-end.py** — `SessionEnd` hook. POSTs to `/api/session/deregister`, local fallback cleanup of request files.
- **channel_feishu.py** — Optional Feishu notification channel. Polls `/api/sessions` for state changes, sends permission cards and idle cards. Prompt delivery via `/api/send-prompt`.
- **install.sh** — Installs symlinks and merges hook config into settings.json. Requires `--project`, `--global`, or `--all`.
- **uninstall.sh** — Reverses install.sh: removes hook config and symlinks. Same `--project`/`--global`/`--all` interface.

## Running

```bash
# Start the server
./server.py

# Install hooks (pick a scope)
/path/to/install.sh --project   # Project-level only
/path/to/install.sh --global    # Global (~/.claude) + symlinks
/path/to/install.sh --all       # Both project + global

# Uninstall hooks
/path/to/uninstall.sh --project
/path/to/uninstall.sh --global
/path/to/uninstall.sh --all
```

No build step, no test suite, no linter. Dependencies: Python 3, Bash (install scripts only).

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

### Tmux Prompt Delivery
SessionStart hook passes `$TMUX_PANE` and `$TMUX`. Server extracts socket path and sends prompts via:
```python
subprocess.run(["tmux", "-S", socket_path, "send-keys", "-t", pane, prompt, "Enter"])
```

### Server Offline Fallback
All hooks auto-approve when the server is unreachable, so Claude Code continues to function normally.

### File Communication
Only used for PermissionRequest blocking:
```
/tmp/claude-webui/
  ├── *.request.json    (pending permission requests)
  └── *.response.json   (user decisions)
```

### API Endpoints
| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/` | Dashboard HTML |
| GET | `/api/sessions` | All sessions with transcript-derived state |
| GET | `/api/session/<id>/transcript` | Parsed transcript entries |
| GET | `/api/pending` | Pending permission requests |
| GET | `/api/image?path=` | Serve uploaded images |
| POST | `/api/session/register` | Register/update session |
| POST | `/api/session/deregister` | Deregister session |
| POST | `/api/respond` | Approve/deny permission |
| POST | `/api/session-allow` | Session-level auto-allow |
| POST | `/api/send-prompt` | Send prompt via tmux |
| POST | `/api/upload-image` | Upload image |
| POST | `/api/session-reset` | Clear session auto-allow rules (legacy) |
| POST | `/api/session-end` | Remove session and clear auto-allow (legacy) |

## Writing Conventions
- All changes to `PRD.md` must be written in English.
