# claude-code-webui

A web UI for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that replaces default terminal prompts with a browser-based interface. Approve/deny tool calls, submit prompts, upload images, manage sessions, and export conversations from any browser or Feishu.

## Architecture

**Transcript-driven, cross-platform (Tmux on Linux/macOS, Windows Terminal on Windows), 3 Python hooks.**

- All session state is derived from Claude Code's transcript JSONL files — the server doesn't maintain a state machine
- Prompts are delivered via platform-native mechanisms (`tmux send-keys` on Linux/macOS, `WriteConsoleInput` on Windows) — no file polling for prompt submission
- Only 3 hook scripts (all Python, no external dependencies like jq or curl)

### How it works

**Permission approval flow:**
```
Claude Code                          Web browser
      |                                   |
      |-- PermissionRequest hook ------>  |
      |   (hook-permission-request.py)         |
      |   writes .request.json to         |
      |   /tmp/claude-webui/              |
      |                                   |
      |          server.py                |
      |          reads transcript JSONL   |
      |          derives session state    |
      |          serves dashboard  ----> |  User sees request
      |                                   |  clicks Allow / Deny
      |          writes .response.json <--|
      |                                   |
      |<-- hook reads response            |
      |   (allow or deny)                 |
```

**Prompt submission flow:**
```
Claude Code                          Web browser
      |                                   |
      |          server.py                |
      |          shows session dashboard  |
      |          with prompt input  ----> |  User types prompt
      |                                   |  clicks Send
      |                                   |
      |<-- prompt delivered via           |
      |    tmux send-keys (Linux/macOS)   |
      |    or WriteConsoleInput (Windows) |
```

### Components

1. **`server.py`** — Python HTTP server (port 19836). Session registry, transcript parser, multi-session dashboard.
2. **`hook-permission-request.py`** — `PermissionRequest` hook. Auto-allow check, writes `.request.json`, polls for `.response.json`.
3. **`hook-session-start.py`** — `SessionStart` hook. Registers session with server (transcript path, tmux/console info, cwd).
4. **`hook-session-end.py`** — `SessionEnd` hook. Deregisters session, cleans up files.
5. **`platform_utils.py`** — Cross-platform utilities. OS detection, temp directory paths, process tree walking.
6. **`win_send_keys.py`** — Windows console input helper. Injects keyboard input via `WriteConsoleInputW`.
7. **`channel_feishu.py`** — Optional Feishu (Lark) notification channel.
8. **`install.sh`** / **`uninstall.sh`** — Hook installation scripts (Linux/macOS). **`install.ps1`** / **`uninstall.ps1`** — Windows equivalents (PowerShell).

## Features

- **Multi-session dashboard** — see all active Claude Code sessions at a glance
- **Transcript-derived state** — idle, working, needs approval, question, plan review
- **Allow / Deny** — approve or reject individual tool calls
- **Always Allow** — approve and add pattern to `settings.local.json`
- **Allow Path** — for Write/Edit tools, allow all operations under a directory
- **Split Always Allow** — compound Bash commands split into individual patterns
- **Session-level auto-allow** — auto-approve specific tools for a session
- **Prompt submission** — send follow-up prompts from the dashboard (via tmux on Linux/macOS, console input on Windows)
- **AskUserQuestion support** — answer Claude's questions with option selection or custom text
- **Plan review** — approve, deny, or provide feedback on plans
- **Image upload** — attach images in the prompt area
- **Feishu integration** — optional notification channel for mobile approval
- **Graceful fallback** — hooks auto-approve when server is offline
- **Auto-cleanup** — zombie sessions (dead PIDs) cleaned up automatically
- **Dark-themed, mobile-friendly UI**

## Requirements

**Linux/macOS:**
- Python 3
- tmux (required for prompt delivery)
- Bash, `jq` (for install/uninstall scripts)

**Windows:**
- Python 3
- PowerShell 5.1+ (for install/uninstall scripts)

## Installation

1. Clone this repo:
   ```bash
   git clone https://github.com/gooooloo/claude-code-webui.git
   ```

2. Start the server:
   ```bash
   /path/to/claude-code-webui/server.py
   ```

3. Install the hooks:

   **Linux/macOS:**
   ```bash
   # For a single project (run from project directory):
   /path/to/claude-code-webui/install.sh --project

   # Or install globally (all projects):
   /path/to/claude-code-webui/install.sh --global

   # Or both:
   /path/to/claude-code-webui/install.sh --all
   ```

   **Windows (PowerShell):**
   ```powershell
   # For a single project:
   \path\to\claude-code-webui\install.ps1 -Scope Project

   # Or install globally:
   \path\to\claude-code-webui\install.ps1 -Scope Global

   # Or both:
   \path\to\claude-code-webui\install.ps1 -Scope All
   ```

4. **Restart Claude Code** if it's already running — hooks are loaded at startup.

5. Open `http://localhost:19836` in your browser (or use your machine's LAN IP from a phone/tablet).

6. Run Claude Code — on Linux/macOS run it **inside tmux**; on Windows run it in **Windows Terminal**. The dashboard will show your sessions and let you interact.

> **Windows limitation:** Prompt delivery uses `AttachConsole`/`WriteConsoleInputW`, which does not support Windows Terminal split panes. If you split a window into multiple panes, prompts may be delivered to the wrong pane or fail entirely. Use one Claude Code session per window (multiple windows are fine).

## Security note

The server binds to `127.0.0.1:19836` by default (local access only). To allow LAN access (e.g. approve requests from a phone or another device), use `--lan`:

```bash
python3 server.py --lan
```

This binds to `0.0.0.0:19836`, making it accessible from your local network.

There is no authentication on the web UI. Anyone on your network who can reach port 19836 can approve or deny requests. Use on trusted networks only, or add your own auth layer.

## License

MIT
