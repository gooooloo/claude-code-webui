# claude-code-permission-web-approver

A web-based approval UI for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) permission requests. Approve or deny tool calls from your phone, tablet, or any browser on your network.

## How it works

```
Claude Code session                  Web browser
      |                                   |
      |-- hook fires ------------------>  |
      |   (approve-dialog.sh)             |
      |                                   |
      |   writes request to               |
      |   /tmp/claude-approvals/          |
      |                                   |
      |          approval-server.py       |
      |          polls queue dir          |
      |          serves web UI  --------> |  User sees request
      |                                   |  clicks Allow / Deny
      |          writes response <------- |
      |                                   |
      |<-- hook reads response            |
      |   (allow or deny)                 |
```

1. **`approve-dialog.sh`** — Claude Code `PermissionRequest` hook. Receives the tool call, writes a `.request.json` to `/tmp/claude-approvals/`, and polls for a `.response.json`.
2. **`approval-server.py`** — Python HTTP server (port 19836). Serves a single-page UI that shows pending requests and lets you approve/deny them.
3. **`post-cleanup.sh`** — `PostToolUse` hook. Cleans up stale request files after a tool finishes executing.
4. **`install.sh`** — Registers the hooks in a project's `.claude/settings.json`.

## Features

- **Allow** — approve a single tool call
- **Always Allow** — approve and add the pattern to the project's `settings.local.json` so it won't ask again
- **Deny** — reject the tool call
- Auto-cleanup of stale requests (dead processes)
- Dark-themed, mobile-friendly UI
- No dependencies beyond Python 3 standard library

## Requirements

- Python 3
- `jq`
- Bash
- `uuidgen` (available by default on macOS; install `uuid-runtime` on Debian/Ubuntu)

## Installation

1. Clone this repo anywhere you like:
   ```bash
   git clone https://github.com/gooooloo/claude-code-permission-web-approver.git
   ```

2. Start the approval server:
   ```bash
   /path/to/claude-code-permission-web-approver/approval-server.py
   ```

3. In any project directory, install the hooks:
   ```bash
   /path/to/claude-code-permission-web-approver/install.sh
   ```

4. Open `http://localhost:19836` in your browser (or use your machine's LAN IP from a phone/tablet).

5. Run Claude Code — permission requests will appear in the web UI instead of the terminal.

## Security note

The server binds to `0.0.0.0:19836` by default, making it accessible from your local network. This is intentional — it allows you to approve requests from a phone or another device. If you only need local access, change the bind address to `127.0.0.1` in `approval-server.py`.

There is no authentication on the web UI. Anyone on your network who can reach port 19836 can approve or deny requests. Use on trusted networks only, or add your own auth layer.

## License

MIT
