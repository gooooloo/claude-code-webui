# claude-code-webui

A web UI for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that replaces default terminal prompts with a browser-based interface. Approve/deny tool calls, submit prompts, upload images, and manage sessions from your phone, tablet, or any browser on your network.

## Architecture

**Transcript-driven, Tmux-only, 3 Python hooks.**

- All session state is derived from Claude Code's transcript JSONL files — the server doesn't maintain a state machine
- Prompts are delivered via `tmux send-keys` — no file polling for prompt submission
- Only 3 hook scripts (all Python, no external dependencies like jq or curl)

### How it works

**Permission approval flow:**
```
Claude Code                          Web browser
      |                                   |
      |-- PermissionRequest hook ------>  |
      |   (permission-request.py)         |
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

**Prompt submission flow (tmux):**
```
Claude Code (in tmux)                Web browser
      |                                   |
      |          server.py                |
      |          shows session dashboard  |
      |          with prompt input  ----> |  User types prompt
      |                                   |  clicks Send
      |                                   |
      |<-- tmux send-keys delivers prompt |
```

### Components

1. **`server.py`** — Python HTTP server (port 19836). Session registry, transcript parser, multi-session dashboard.
2. **`permission-request.py`** — `PermissionRequest` hook. Auto-allow check, writes `.request.json`, polls for `.response.json`.
3. **`session-start.py`** — `SessionStart` hook. Registers session with server (transcript path, tmux info, cwd).
4. **`session-end.py`** — `SessionEnd` hook. Deregisters session, cleans up files.
5. **`channel_feishu.py`** — Optional Feishu (Lark) notification channel.
6. **`install.sh`** / **`uninstall.sh`** — Hook installation scripts.

## Features

- **Multi-session dashboard** — see all active Claude Code sessions at a glance
- **Transcript-derived state** — idle, working, needs approval, question, plan review
- **Allow / Deny** — approve or reject individual tool calls
- **Always Allow** — approve and add pattern to `settings.local.json`
- **Allow Path** — for Write/Edit tools, allow all operations under a directory
- **Split Always Allow** — compound Bash commands split into individual patterns
- **Session-level auto-allow** — auto-approve specific tools for a session
- **Prompt submission** — send follow-up prompts via tmux from the dashboard
- **AskUserQuestion support** — answer Claude's questions with option selection or custom text
- **Plan review** — approve, deny, or provide feedback on plans
- **Image upload** — attach images in the prompt area
- **MultiView** — centralized page to monitor the same service across multiple machines via DevTunnels
- **Feishu integration** — optional notification channel for mobile approval
- **Graceful fallback** — hooks auto-approve when server is offline
- **Auto-cleanup** — zombie sessions (dead PIDs) cleaned up automatically
- **Dark-themed, mobile-friendly UI**

## Requirements

- Python 3
- tmux (required for prompt delivery)
- Bash (for install/uninstall scripts)
- `jq` (for install/uninstall scripts only)

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
   ```bash
   # For a single project (run from project directory):
   /path/to/claude-code-webui/install.sh --project

   # Or install globally (all projects):
   /path/to/claude-code-webui/install.sh --global

   # Or both:
   /path/to/claude-code-webui/install.sh --all
   ```

4. **Restart Claude Code** if it's already running — hooks are loaded at startup.

5. Open `http://localhost:19836` in your browser (or use your machine's LAN IP from a phone/tablet).

6. Run Claude Code **inside tmux** — the dashboard will show your sessions and let you interact.

## Upgrading from the old architecture

If you had the previous version installed (6 bash hooks):

```bash
# Uninstall old hooks
/path/to/claude-code-webui/uninstall.sh --all

# Pull latest
cd /path/to/claude-code-webui && git pull

# Install new hooks
/path/to/claude-code-webui/install.sh --all
```

The new install script automatically cleans up old `.sh` symlinks.

## MultiView (multi-machine monitoring)

MultiView lets you monitor the same WebUI service running on multiple machines from a single page. Remote servers self-register with a central hub; the MultiView page auto-discovers all machines and provides quick-open links.

### DevTunnels setup (one-time per machine)

[Microsoft DevTunnels](https://learn.microsoft.com/en-us/azure/developer/dev-tunnels/overview) gives each machine a public HTTPS URL without port forwarding. Install the CLI:

```bash
# Windows (winget)
winget install Microsoft.devtunnel

# Linux
curl -sL https://aka.ms/DevTunnelCliInstall | bash

# macOS
brew install --cask devtunnel
```

Login and create a **named tunnel** (persistent — survives reboots, only `devtunnel delete` removes it):

```bash
devtunnel login
devtunnel create --id my-machine        # named ID for local reference
devtunnel port create --tunnel-id my-machine --port-number 19836
```

Each time you need the tunnel active, just host it:

```bash
devtunnel host --tunnel-id my-machine
```

The public URL will be `https://<random-id>-19836.asse.devtunnels.ms`. The `<random-id>` is assigned once at creation time and stays the same as long as you don't delete the tunnel. You can find it via `devtunnel list`.

> **Tip:** You can expose multiple ports on the same tunnel:
> ```bash
> devtunnel port create --tunnel-id my-machine --port-number 8080
> devtunnel port create --tunnel-id my-machine --port-number 3000
> ```
> Each port gets its own URL: `https://<random-id>-8080.asse.devtunnels.ms`, etc.

### MultiView setup

1. **Pick one machine as the hub** (the one you'll open in your browser):
   ```bash
   python3 server.py --lan --name hub
   devtunnel host --tunnel-id hub-machine
   ```

2. **Start remote servers** with `--hub-tunnel-id` pointing to the hub's random ID:
   ```bash
   # With explicit tunnel ID (find it via `devtunnel list`)
   python3 server.py --lan --name "GPU-A100" --tunnel-id 1c6j6jlh --hub-tunnel-id abc123

   # Or auto-detect tunnel ID
   python3 server.py --lan --name "GPU-A100" --detect-tunnel --hub-tunnel-id abc123
   ```

3. Open `https://<hub-tunnel-id>-19836.asse.devtunnels.ms/multiview` — all registered machines appear automatically.

### CLI arguments

| Argument | Purpose |
|----------|---------|
| `--hub <url>` | Full URL of the hub server |
| `--hub-tunnel-id <id>` | DevTunnels ID of the hub (shorthand) |
| `--tunnel-id <id>` | This machine's DevTunnels ID |
| `--detect-tunnel` | Auto-detect devtunnel ID via `devtunnel list` |
| `--self-url <url>` | This machine's public URL (overrides tunnel ID) |
| `--name <name>` | Display name for this machine (default: `local`) |

## Security note

The server binds to `127.0.0.1:19836` by default (local access only). To allow LAN access (e.g. approve requests from a phone or another device), use `--lan`:

```bash
python3 server.py --lan
```

This binds to `0.0.0.0:19836`, making it accessible from your local network.

There is no authentication on the web UI. Anyone on your network who can reach port 19836 can approve or deny requests. Use on trusted networks only, or add your own auth layer.

## License

MIT
