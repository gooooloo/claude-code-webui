# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A web-based approval UI for Claude Code permission requests. Instead of the default terminal prompts, users approve/deny tool executions through a browser interface (accessible from phones, tablets, or any LAN device).

**Flow:** Claude Code hook → shell script writes JSON request to `/tmp/claude-approvals/` → Python HTTP server serves web UI that polls for requests → user approves/denies → hook reads response JSON → decision returned to Claude Code.

## Architecture

- **approval-server.py** — Python HTTP server (port 19836) with the entire web UI embedded as a single `HTML_PAGE` triple-quoted string. Handles API endpoints (`/api/pending`, `/api/respond`, `/api/submit-prompt`, `/api/session-allow`, `/api/session-reset`, `/api/upload-image`, etc.) and runs a background auto-approve thread for session-level rules.
- **permission-request.sh** — `PermissionRequest` hook. Parses tool calls, checks `settings.local.json` for pre-approved glob patterns, falls back to auto-allow if server is offline, otherwise queues a request JSON and polls for response.
- **stop.sh** — `Stop` hook. In non-tmux mode, polls for prompt submission from the web UI. In tmux mode, writes a marker and returns immediately (the UI delivers prompts via `tmux send-keys`).
- **post-tool-use.sh** — `PostToolUse` hook. Cleans up stale request/response files after tool execution.
- **user-prompt-submit.sh** — `UserPromptSubmit` hook. Cleans up `.prompt-waiting.json` files when user submits a prompt.
- **session-start.sh** — `SessionStart` hook. Notifies the approval server on session start/reset (startup, resume, clear, compact) so it can clear stale requests and session auto-allow rules.
- **install.sh** — Installs symlinks and merges hook config into a project's `.claude/settings.json`.

## Running

```bash
# Start the approval server
./approval-server.py

# Install hooks into a project
/path/to/install.sh
```

No build step, no test suite, no linter. Dependencies: Python 3, Bash, `jq`, `curl`, `uuidgen`.

## Key Conventions

### Allow Pattern Format
Patterns in `settings.local.json` use `ToolName(pattern)` format with glob matching:
- `Bash(git commit:*)` — `:*` suffix means "starts with"
- `Write(/some/path/*)` — directory-scoped write permission
- `Read`, `WebFetch` — tool-level blanket allow

### Python Escape Sequences in approval-server.py
`HTML_PAGE` is a `"""` triple-quoted string containing inline JS. Python processes escape sequences inside it:
- `\'` in source → `'` in output (not `\'`)
- `\\'` in source → `\'` in output (backslash + quote)
- Rule: for each `\` needed in rendered output, write `\\` in the Python source

### Session Management
- Session ID = PPID (Claude Code's process ID)
- Session-level auto-allow rules are stored in-memory on the server as `{(session_id, tool_name): True}`
- Hooks detect stale requests by checking process liveness

### Tmux Mode
Detected via `$TMUX` and `$TMUX_PANE` env vars. Prompts are delivered via `tmux load-buffer` + `paste-buffer` + `send-keys` instead of file polling.

### Server Offline Fallback
All hooks auto-approve when the server is unreachable, so Claude Code continues to function normally.

## Writing Conventions
- All changes to `PRD.md` must be written in English.
