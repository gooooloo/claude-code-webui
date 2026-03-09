#!/usr/bin/env python3
"""
Claude Code WebUI Server — Transcript-driven, Tmux-only architecture.

Maintains a session registry, parses transcript JSONL files incrementally,
and serves a multi-session dashboard UI.

Usage: python3 server.py
Then open http://localhost:19836
"""

import argparse
import json
import glob
import os
import re
import signal
import subprocess
import sys
import time
import threading
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import uuid
import cgi

from frontend import HTML_PAGE
from platform_utils import IS_WINDOWS, get_queue_dir, get_image_dir, is_process_alive, find_claude_pid, get_process_children, get_process_name, encode_project_path

try:
    from channel_feishu import start_feishu_channel
    _has_feishu = True
except ImportError:
    _has_feishu = False

try:
    from channel_teams import start_teams_channel
    _has_teams = True
except ImportError:
    _has_teams = False

QUEUE_DIR = get_queue_dir()
IMAGE_DIR = get_image_dir()
PORT = 19836
# ── Session registry ──
sessions = {}
# sessions[session_id] = {
#     "transcript_path": str,
#     "tmux_pane": str,
#     "tmux_socket": str,
#     "cwd": str,
#     "registered_at": float,
#     "transcript_offset": int,
#     "transcript_entries": list,  # parsed entries (kept for rendering)
#     "derived_state": str,        # idle|busy|permission_prompt|elicitation|plan_review
#     "last_activity": float,
#     "last_summary": str,         # brief summary of last assistant message
# }

sessions_lock = threading.Lock()

# Session-level auto-allow rules: { (session_id, tool_name): True }
session_auto_allow = {}

# ── Smart auto-approve ──
smart_auto_approve = True  # enabled by default

# Read-only command whitelist for Bash tool
# Each entry is a base command name; subcommands checked separately for git etc.
READONLY_COMMANDS = {
    # File viewing
    "cat", "head", "tail", "less", "more", "wc", "file", "stat", "du", "df",
    # Directory listing
    "ls", "tree", "find", "realpath", "dirname", "basename",
    # Search
    "grep", "rg", "ag", "fgrep", "egrep",
    # Version/info
    "echo", "printf", "date", "whoami", "hostname", "uname", "env", "printenv",
    "which", "type", "command", "true", "false", "test",
    # Package info (read-only)
    "npm", "pip", "pip3", "cargo", "go", "python", "python3", "node", "ruby", "java", "javac",
}

# Git subcommands that are read-only
READONLY_GIT_SUBCOMMANDS = {
    "log", "diff", "status", "show", "branch", "tag", "remote", "stash",
    "blame", "shortlog", "describe", "rev-parse", "rev-list", "ls-files",
    "ls-tree", "cat-file", "config",
}

# Commands that are never safe
DANGEROUS_COMMANDS = {
    "rm", "rmdir", "mv", "chmod", "chown", "chgrp", "mkfs", "dd",
    "shutdown", "reboot", "kill", "killall", "pkill",
    "curl", "wget",  # network access
    "ssh", "scp", "rsync",  # remote access
    "sudo", "su", "doas",  # privilege escalation
}

# Tools that are inherently read-only
READONLY_TOOLS = {"Read", "Glob", "Grep", "mcp__acp__Read", "mcp__acp__Glob", "mcp__acp__Grep"}


def _is_readonly_bash(command):
    """Check if a Bash command (possibly compound) is read-only."""
    # Split on pipes, &&, ||, and ;
    parts = re.split(r'\||\&\&|\|\||;', command)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        first_line = part.split("\n")[0].strip()
        tokens = first_line.split()
        if not tokens:
            continue
        base = os.path.basename(tokens[0])
        if not base:
            continue

        # Dangerous commands — immediate reject
        if base in DANGEROUS_COMMANDS:
            return False

        # sed with -i flag is not read-only
        if base == "sed":
            if "-i" in tokens or any(t.startswith("-i") for t in tokens[1:]):
                return False
            continue

        # awk — generally read-only unless redirecting (handled by shell, not here)
        if base in ("awk", "gawk", "mawk", "nawk"):
            continue

        # git — check subcommand
        if base == "git":
            sub = ""
            for t in tokens[1:]:
                if not t.startswith("-"):
                    sub = t
                    break
            if sub not in READONLY_GIT_SUBCOMMANDS:
                return False
            continue

        # Known read-only commands
        if base in READONLY_COMMANDS:
            continue

        # Unknown command — not safe
        return False

    return True


def _is_project_file(file_path, session_cwd):
    """Check if a file path is within the session's project directory."""
    if not file_path or not session_cwd:
        return False
    try:
        real_file = os.path.realpath(file_path)
        real_cwd = os.path.realpath(session_cwd)
        return real_file.startswith(real_cwd + os.sep) or real_file == real_cwd
    except (ValueError, OSError):
        return False


def check_smart_auto_approve(data):
    """Check if a permission request should be auto-approved by smart rules.
    Returns True if the request is safe to auto-approve."""
    if not smart_auto_approve:
        return False

    tname = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    sid = str(data.get("session_id", ""))

    # Rule 1: Inherently read-only tools
    if tname in READONLY_TOOLS:
        return True

    # Rule 2: Read-only Bash commands
    if tname in ("Bash", "mcp__acp__Bash"):
        command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
        if command and _is_readonly_bash(command):
            return True

    # Rule 3: Project-internal file edits
    if tname in ("Write", "Edit", "mcp__acp__Write", "mcp__acp__Edit"):
        file_path = tool_input.get("file_path", "") if isinstance(tool_input, dict) else ""
        with sessions_lock:
            session = sessions.get(sid)
            session_cwd = session.get("cwd", "") if session else ""
        if _is_project_file(file_path, session_cwd):
            return True

    return False

# ── Federation ──
remote_servers = []          # [{"name": str, "url": str}]
local_name = "local"
session_machine_map = {}     # {session_id: remote_url or None(local)}


def proxy_to_remote(remote_url, path, method="GET", body=None, headers=None):
    """Forward a request to a remote WebUI server."""
    url = remote_url.rstrip("/") + path
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    resp = urllib.request.urlopen(req, timeout=5)
    return resp.status, resp.read(), resp.headers.get("Content-Type", "application/json")


def fetch_remote_sessions():
    """Fetch sessions from all remote servers. Returns list of (remote_config, sessions_or_None)."""
    results = []
    for remote in remote_servers:
        try:
            url = remote["url"].rstrip("/") + "/api/sessions"
            req = urllib.request.Request(url)
            resp = urllib.request.urlopen(req, timeout=3)
            data = json.loads(resp.read())
            results.append((remote, data.get("sessions", [])))
        except Exception as e:
            print(f"[!] Federation: {remote['name']} unreachable: {e}")
            results.append((remote, None))
    return results


def _get_remote_url_for_session(session_id):
    """Return remote URL if session is remote, None if local."""
    return session_machine_map.get(session_id)


def _get_original_session_id(session_id):
    """Strip the machine prefix to get the original remote session ID."""
    if ":" in session_id:
        return session_id.split(":", 1)[1]
    return session_id


# ── Transcript parsing ──

def update_session_state(sid):
    """Read new transcript entries incrementally and derive session state."""
    with sessions_lock:
        s = sessions.get(sid)
        if not s:
            return
        path = s["transcript_path"]
        offset = s["transcript_offset"]

    if not path or not os.path.isfile(path):
        return

    try:
        with open(path, "rb") as f:
            f.seek(offset)
            new_data = f.read()
    except IOError:
        new_data = b""

    if new_data:
        text = new_data.decode("utf-8", errors="replace")
        lines = text.split("\n")
        new_entries = []
        bytes_consumed = 0

        for i, line in enumerate(lines):
            # +1 for the \n delimiter, but the last element from split() has no trailing \n
            nl = 1 if i < len(lines) - 1 else 0
            if not line.strip():
                bytes_consumed += len(line.encode("utf-8")) + nl
                continue
            try:
                entry = json.loads(line)
                new_entries.append(entry)
                bytes_consumed += len(line.encode("utf-8")) + nl
            except (json.JSONDecodeError, ValueError):
                if i == len(lines) - 1:
                    # Last line may be incomplete (still being written) — stop here
                    break
                # Mid-file bad line — skip it
                bytes_consumed += len(line.encode("utf-8")) + nl

        if new_entries:
            with sessions_lock:
                s = sessions.get(sid)
                if not s:
                    return
                s["transcript_offset"] = offset + bytes_consumed
                s["transcript_entries"].extend(new_entries)
                s["last_activity"] = time.time()

    # Always derive state — .request.json is an external signal independent of transcript changes
    with sessions_lock:
        s = sessions.get(sid)
        if not s:
            return
        s["derived_state"], s["last_summary"], s["last_user_prompt"] = _derive_state(sid, s)


def _derive_state(sid, s):
    """Derive session state from transcript entries + pending request files."""
    entries = s["transcript_entries"]
    summary = s.get("last_summary", "")
    user_prompt = s.get("last_user_prompt", "")

    # Find last meaningful entries (skip file-history-snapshot, queue-operation)
    last_assistant = None
    last_user = None
    for entry in reversed(entries):
        etype = entry.get("type", "")
        if etype == "assistant" and last_assistant is None:
            last_assistant = entry
        elif etype == "user" and last_user is None:
            last_user = entry
        if last_assistant and last_user:
            break

    # Extract last user prompt text (skip tool_results and system-injected messages)
    for entry in reversed(entries):
        if entry.get("type") != "user":
            continue
        msg = entry.get("message", {})
        content = msg.get("content", "")
        text = ""
        if isinstance(content, str):
            # Skip messages that are purely system-injected XML
            stripped = content.strip()
            if stripped.startswith("<") and not any(c in stripped for c in ["\n"] if stripped.count("<") > 3):
                # Check if it's mostly XML — strip tags and see what's left
                pass
            text = content
        elif isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    parts.append(c.get("text", ""))
                elif isinstance(c, str):
                    parts.append(c)
            text = " ".join(parts)
        if not text.strip():
            continue
        # Strip XML tags and their content for known system tags
        clean = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL)
        clean = re.sub(r"<local-command-caveat>.*?</local-command-caveat>", "", clean, flags=re.DOTALL)
        clean = re.sub(r"<local-command-stdout>.*?</local-command-stdout>", "", clean, flags=re.DOTALL)
        clean = re.sub(r"<task-notification>.*?</task-notification>", "", clean, flags=re.DOTALL)
        clean = re.sub(r"<command-name>.*?</command-name>", "", clean, flags=re.DOTALL)
        clean = re.sub(r"<command-message>.*?</command-message>", "", clean, flags=re.DOTALL)
        clean = re.sub(r"<command-args>.*?</command-args>", "", clean, flags=re.DOTALL)
        # Strip any remaining XML tags
        clean = re.sub(r"<[^>]+>", "", clean)
        # Collapse runs of spaces/tabs (preserve newlines)
        clean = re.sub(r"[^\S\n]+", " ", clean)
        # Collapse 3+ newlines into 2
        clean = re.sub(r"\n{3,}", "\n\n", clean)
        clean = clean.strip()
        if clean:
            user_prompt = clean
            break

    # Check if the last user message is after the last assistant message.
    # When true, the assistant's summary is stale (from a previous turn).
    user_after_assistant = False
    if last_user and last_assistant:
        for i, entry in enumerate(entries):
            if entry is last_user:
                user_idx = i
            if entry is last_assistant:
                asst_idx = i
        user_after_assistant = user_idx > asst_idx

    # Invariant: summary is only shown when it follows the displayed user_prompt
    if user_after_assistant:
        return "busy", "", user_prompt

    # Extract info from last assistant message
    if last_assistant:
        msg = last_assistant.get("message", {})
        content = msg.get("content", [])
        stop_reason = msg.get("stop_reason", "")

        # Get tool_use blocks
        tool_uses = [c for c in content if isinstance(c, dict) and c.get("type") == "tool_use"]

        # Extract summary from text content
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                text = c.get("text", "")
                if text:
                    summary = text

        # Check for pending permission request
        pending_request = _find_pending_request(sid)
        if pending_request:
            # Check if the tool_use has already been resolved in transcript
            req_tool = pending_request.get("tool_name", "")
            req_input = pending_request.get("tool_input", {})
            if _tool_use_resolved_in_transcript(entries, req_tool, req_input):
                req_id = pending_request.get("id", "")
                _cleanup_stale_request(req_id)
            else:
                return "permission_prompt", summary, user_prompt

        if tool_uses:
            last_tool = tool_uses[-1]
            tool_name = last_tool.get("name", "")

            if tool_name == "AskUserQuestion":
                return "elicitation", summary, user_prompt
            if tool_name == "ExitPlanMode":
                return "plan_review", summary, user_prompt

            # Has unresolved tool_use — check if there's a matching tool_result
            tool_id = last_tool.get("id", "")
            if not _has_tool_result(entries, tool_id):
                return "busy", summary, user_prompt

        if stop_reason == "end_turn" or (not tool_uses and stop_reason != "tool_use"):
            return "idle", summary, user_prompt

    # No assistant message yet — idle if no meaningful user input
    # (e.g. session just started, or after /clear which only has system XML)
    if not last_assistant and not user_prompt:
        return "idle", "", ""
    return "busy", "", user_prompt


def _find_pending_request(sid):
    """Find a pending .request.json for this session (no .response.json yet)."""
    for path in glob.glob(os.path.join(QUEUE_DIR, "*.request.json")):
        resp_path = path.replace(".request.json", ".response.json")
        if os.path.exists(resp_path):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
            if str(data.get("session_id", "")) == str(sid):
                return data
        except (json.JSONDecodeError, IOError):
            continue
    return None


def _tool_use_resolved_in_transcript(entries, tool_name, tool_input):
    """Check if the most recent tool_use matching this request has a tool_result."""
    # Walk backwards — only check the LAST tool_use with this name
    for i in range(len(entries) - 1, -1, -1):
        entry = entries[i]
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message", {})
        content = msg.get("content", [])
        for c in content:
            if not isinstance(c, dict) or c.get("type") != "tool_use":
                continue
            if c.get("name") != tool_name:
                continue
            tool_id = c.get("id", "")
            # Only check the most recent matching tool_use — don't continue to older ones
            return _has_tool_result(entries[i:], tool_id)
    return False


def _has_tool_result(entries, tool_id):
    """Check if any user entry has a tool_result matching this tool_use_id."""
    for entry in entries:
        if entry.get("type") != "user":
            continue
        msg = entry.get("message", {})
        content = msg.get("content", [])
        for c in content:
            if isinstance(c, dict) and c.get("type") == "tool_result":
                if c.get("tool_use_id") == tool_id:
                    return True
    return False


def _cleanup_stale_request(request_id):
    """Remove stale request/response files."""
    if not request_id:
        return
    for suffix in (".request.json", ".response.json"):
        path = os.path.join(QUEUE_DIR, f"{request_id}{suffix}")
        try:
            os.remove(path)
        except OSError:
            pass


# ── Auto-allow ──

def check_auto_allow():
    """Scan pending requests and auto-approve those matching session auto-allow or smart rules."""
    for path in glob.glob(os.path.join(QUEUE_DIR, "*.request.json")):
        resp_path = path.replace(".request.json", ".response.json")
        if os.path.exists(resp_path):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            continue
        sid = str(data.get("session_id", ""))
        tname = data.get("tool_name", "")
        approved = False
        reason = ""
        if (sid, tname) in session_auto_allow:
            approved = True
            reason = "session-rule"
        elif check_smart_auto_approve(data):
            approved = True
            reason = "smart-rule"
        if approved:
            try:
                with open(resp_path, "w") as f:
                    json.dump({"decision": "allow"}, f)
                print(f"[~] Auto-allowed {tname} for session {sid} ({reason})")
            except IOError:
                pass


def auto_allow_loop():
    """Background thread: periodically check for auto-allowable requests."""
    while True:
        check_auto_allow()
        time.sleep(0.5)


# ── Zombie session cleanup ──

def _is_session_alive(sid, session_data):
    """Check if a session is still active."""
    # For numeric session IDs (PIDs), check if the process is alive
    try:
        pid = int(sid)
        return is_process_alive(pid)
    except ValueError:
        pass  # Non-numeric (UUID) session ID

    # On Windows, check console_pid if available
    if IS_WINDOWS:
        console_pid = session_data.get("console_pid")
        if console_pid:
            try:
                return is_process_alive(int(console_pid))
            except (ValueError, TypeError):
                pass
        # No console_pid yet — session may still be registering; keep alive
        # for a grace period (60 seconds from registration)
        registered_at = session_data.get("registered_at", 0)
        if time.time() - registered_at < 60:
            return True
        return False

    # For UUID session IDs, check if the tmux pane exists and has claude in its process tree
    tmux_pane = session_data.get("tmux_pane", "")
    tmux_socket = session_data.get("tmux_socket", "")
    if tmux_pane and tmux_socket:
        socket_path = tmux_socket.split(",")[0]
        try:
            # Get the pane's shell PID
            result = subprocess.run(
                ["tmux", "-S", socket_path, "list-panes", "-a",
                 "-F", "#{pane_id} #{pane_pid}"],
                capture_output=True, text=True, timeout=3
            )
            shell_pid = None
            for line in result.stdout.strip().splitlines():
                parts = line.split(" ", 1)
                if len(parts) == 2 and parts[0] == tmux_pane:
                    shell_pid = parts[1]
                    break
            if not shell_pid:
                return False
            # Check if any child of the shell is claude/node
            children = subprocess.run(
                ["pgrep", "-P", shell_pid],
                capture_output=True, text=True, timeout=3
            )
            for child_pid in children.stdout.strip().splitlines():
                child_pid = child_pid.strip()
                if not child_pid:
                    continue
                ps_result = subprocess.run(
                    ["ps", "-p", child_pid, "-o", "comm="],
                    capture_output=True, text=True, timeout=3
                )
                comm = os.path.basename(ps_result.stdout.strip())
                if comm in ("claude", "node"):
                    return True
        except Exception:
            pass

    return False


def zombie_cleanup_loop():
    """Background thread: remove dead sessions every 30s."""
    while True:
        time.sleep(30)
        dead = []
        with sessions_lock:
            for sid in list(sessions.keys()):
                if not _is_session_alive(sid, sessions[sid]):
                    dead.append(sid)
            for sid in dead:
                del sessions[sid]
                # Clear auto-allow rules
                keys_to_remove = [k for k in session_auto_allow if k[0] == sid]
                for k in keys_to_remove:
                    del session_auto_allow[k]
        if dead:
            print(f"[~] Cleaned up {len(dead)} zombie session(s): {dead}")


# ── Prompt delivery ──

def win_send_prompt(console_pid, text):
    """Send a prompt to a Windows console via win_send_keys.py."""
    try:
        result = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(__file__), "win_send_keys.py"),
             str(console_pid), text],
            capture_output=True, timeout=10
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def tmux_send_prompt(session, prompt):
    """Send a prompt to a tmux pane."""
    tmux_socket = session.get("tmux_socket", "")
    pane = session.get("tmux_pane", "")
    if not pane:
        return False

    cmd_base = ["tmux"]
    if tmux_socket:
        socket_path = tmux_socket.split(",")[0]
        cmd_base = ["tmux", "-S", socket_path]

    try:
        # Load prompt into buffer via stdin
        subprocess.run(
            cmd_base + ["load-buffer", "-"],
            input=prompt.encode(), capture_output=True, timeout=5
        )
        # Paste buffer into target pane
        subprocess.run(
            cmd_base + ["paste-buffer", "-t", pane, "-d"],
            capture_output=True, timeout=5
        )
        # Send Enter
        subprocess.run(
            cmd_base + ["send-keys", "-t", pane, "Enter"],
            capture_output=True, timeout=5
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def send_prompt(session_info, prompt_text):
    """Send a prompt to a session, dispatching to the appropriate platform method."""
    if IS_WINDOWS:
        console_pid = session_info.get("console_pid")
        if console_pid:
            return win_send_prompt(console_pid, prompt_text)
        return False
    # Linux/macOS: use tmux
    return tmux_send_prompt(session_info, prompt_text)



# ── HTTP Handler ──

class WebUIHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._respond_html(HTML_PAGE)

        elif path == "/api/sessions":
            # Update all session states from transcripts
            with sessions_lock:
                sids = list(sessions.keys())
            for sid in sids:
                update_session_state(sid)

            result = []
            with sessions_lock:
                for sid, s in sessions.items():
                    entry = {
                        "session_id": sid,
                        "cwd": s["cwd"],
                        "state": s["derived_state"],
                        "last_summary": s["last_summary"],
                        "last_user_prompt": s["last_user_prompt"],
                        "last_activity": s["last_activity"],
                        "registered_at": s["registered_at"],
                    }
                    # Attach pending request if in permission_prompt state
                    if s["derived_state"] == "permission_prompt":
                        pr = _find_pending_request(sid)
                        if pr:
                            entry["pending_request"] = pr
                    result.append(entry)

            # Federation: tag local sessions and merge remote sessions
            for entry in result:
                entry["machine"] = local_name
                session_machine_map[entry["session_id"]] = None

            qs = parse_qs(parsed.query)
            local_only = qs.get("local_only", [""])[0] == "1"

            if remote_servers and not local_only:
                remote_results = fetch_remote_sessions()
                for remote, remote_sessions in remote_results:
                    if remote_sessions is None:
                        continue
                    for rs in remote_sessions:
                        rs["machine"] = remote["name"]
                        original_sid = rs["session_id"]
                        rs["session_id"] = remote["name"] + ":" + str(original_sid)
                        rs["_remote_session_id"] = original_sid
                        session_machine_map[rs["session_id"]] = remote["url"]
                        result.append(rs)

            remote_names = [r["name"] for r in remote_servers]
            self._respond_json({"sessions": result, "local_name": local_name, "remote_names": remote_names})

        elif path.startswith("/api/session/") and path.endswith("/transcript"):
            # /api/session/<id>/transcript?limit=50&after=0
            parts = path.split("/")
            sid = parts[3] if len(parts) > 3 else ""

            # Federation proxy
            remote_url = _get_remote_url_for_session(sid)
            if remote_url:
                try:
                    original_sid = _get_original_session_id(sid)
                    proxy_path = f"/api/session/{original_sid}/transcript"
                    if parsed.query:
                        proxy_path += "?" + parsed.query
                    status, resp_body, ct = proxy_to_remote(remote_url, proxy_path)
                    self.send_response(status)
                    self.send_header("Content-Type", ct)
                    self.end_headers()
                    self.wfile.write(resp_body)
                except Exception as e:
                    self.send_error(502, f"Remote proxy failed: {e}")
                return

            params = parse_qs(parsed.query)
            limit = int(params.get("limit", [50])[0])

            with sessions_lock:
                s = sessions.get(sid)
                if not s:
                    self.send_error(404, "Session not found")
                    return
                entries = list(s["transcript_entries"])

            # Filter to user/assistant only, take last N
            filtered = [e for e in entries if e.get("type") in ("user", "assistant")]
            filtered = filtered[-limit:]
            self._respond_json({"entries": filtered})

        elif path == "/api/pending":
            # Legacy endpoint — scan .request.json files
            check_auto_allow()
            requests = []
            for fpath in sorted(glob.glob(os.path.join(QUEUE_DIR, "*.request.json"))):
                try:
                    with open(fpath) as f:
                        data = json.load(f)
                    resp_path = fpath.replace(".request.json", ".response.json")
                    if os.path.exists(resp_path):
                        continue
                    pid = data.get("pid")
                    if pid:
                        try:
                            if not is_process_alive(int(pid)):
                                os.remove(fpath)
                                continue
                        except (ValueError, TypeError):
                            os.remove(fpath)
                            continue
                    requests.append(data)
                except (json.JSONDecodeError, IOError):
                    continue

            # Federation: aggregate remote pending requests
            if remote_servers:
                for remote in remote_servers:
                    try:
                        url = remote["url"].rstrip("/") + "/api/pending"
                        req = urllib.request.Request(url)
                        resp = urllib.request.urlopen(req, timeout=3)
                        data = json.loads(resp.read())
                        for r in data.get("requests", []):
                            r["machine"] = remote["name"]
                            if "session_id" in r:
                                r["session_id"] = remote["name"] + ":" + str(r["session_id"])
                            requests.append(r)
                    except Exception:
                        pass

            self._respond_json({"requests": requests})

        elif path.startswith("/api/image"):
            params = parse_qs(parsed.query)
            # Federation: route to remote if machine param specified
            machine = params.get("machine", [""])[0]
            if machine and machine != local_name:
                remote_url = next((r["url"] for r in remote_servers if r["name"] == machine), None)
                if remote_url:
                    try:
                        proxy_path = "/api/image?" + parsed.query
                        status, resp_body, ct = proxy_to_remote(remote_url, proxy_path)
                        self.send_response(status)
                        self.send_header("Content-Type", ct)
                        self.end_headers()
                        self.wfile.write(resp_body)
                    except Exception:
                        self.send_error(502, "Remote image proxy failed")
                    return
            # Existing local logic continues...
            img_path = params.get("path", [""])[0]
            if not img_path or not img_path.startswith(IMAGE_DIR) or not os.path.isfile(img_path):
                self.send_error(404)
                return
            ext = os.path.splitext(img_path)[1].lower()
            ct = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                  ".gif": "image/gif", ".webp": "image/webp"}.get(ext, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.end_headers()
            with open(img_path, "rb") as f:
                self.wfile.write(f.read())

        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/session/register":
            body = self._read_json()
            sid = str(body.get("session_id", ""))
            source = body.get("source", "unknown")
            if not sid:
                self.send_error(400, "Missing session_id")
                return

            transcript_path = body.get("transcript_path", "")
            tmux_pane = body.get("tmux_pane", "")
            tmux_socket = body.get("tmux_socket", "")
            console_pid = body.get("console_pid", "")
            cwd = body.get("cwd", "")

            with sessions_lock:
                # Evict other sessions on the same tmux pane (startup/resume = new process)
                if source in ("startup", "resume") and tmux_pane:
                    evict = [k for k, v in sessions.items() if k != sid and v.get("tmux_pane") == tmux_pane]
                    for k in evict:
                        del sessions[k]
                        keys_to_remove = [ak for ak in session_auto_allow if ak[0] == k]
                        for ak in keys_to_remove:
                            del session_auto_allow[ak]
                    if evict:
                        print(f"[~] Evicted session(s) on pane {tmux_pane}: {evict}")

                # Evict other sessions on the same Windows console_pid (startup/resume = new process)
                if source in ("startup", "resume") and console_pid:
                    evict = [k for k, v in sessions.items() if k != sid and v.get("console_pid") == console_pid]
                    for k in evict:
                        del sessions[k]
                        keys_to_remove = [ak for ak in session_auto_allow if ak[0] == k]
                        for ak in keys_to_remove:
                            del session_auto_allow[ak]
                    if evict:
                        print(f"[~] Evicted session(s) on console_pid {console_pid}: {evict}")

                if source == "startup" or sid not in sessions:
                    sessions[sid] = {
                        "transcript_path": transcript_path,
                        "tmux_pane": tmux_pane,
                        "tmux_socket": tmux_socket,
                        "console_pid": console_pid,
                        "cwd": cwd,
                        "registered_at": time.time(),
                        "transcript_offset": 0,
                        "transcript_entries": [],
                        "derived_state": "busy",
                        "last_activity": time.time(),
                        "last_summary": "",
                        "last_user_prompt": "",
                    }
                else:
                    # resume/clear/compact — update path and reset offset
                    s = sessions[sid]
                    s["transcript_path"] = transcript_path
                    s["transcript_offset"] = 0
                    s["transcript_entries"] = []
                    s["last_activity"] = time.time()
                    if tmux_pane:
                        s["tmux_pane"] = tmux_pane
                    if tmux_socket:
                        s["tmux_socket"] = tmux_socket
                    if console_pid:
                        s["console_pid"] = console_pid
                    if cwd:
                        s["cwd"] = cwd

                if source == "clear":
                    # Clear auto-allow rules
                    keys_to_remove = [k for k in session_auto_allow if k[0] == sid]
                    for k in keys_to_remove:
                        del session_auto_allow[k]
                    # Clean up request files for this session
                    for fpath in glob.glob(os.path.join(QUEUE_DIR, "*.request.json")):
                        try:
                            with open(fpath) as f:
                                data = json.load(f)
                            if str(data.get("session_id", "")) == sid:
                                resp_path = fpath.replace(".request.json", ".response.json")
                                for p in (fpath, resp_path):
                                    try:
                                        os.remove(p)
                                    except OSError:
                                        pass
                        except (json.JSONDecodeError, IOError):
                            continue

            pane_info = f"pane={tmux_pane}" if tmux_pane else f"console_pid={console_pid}" if console_pid else "no-pane"
            print(f"[*] Session registered: {sid} source={source} {pane_info}")
            self._respond_json({"ok": True})

        elif path == "/api/session/deregister":
            body = self._read_json()
            sid = str(body.get("session_id", ""))
            if not sid:
                self.send_error(400, "Missing session_id")
                return

            with sessions_lock:
                sessions.pop(sid, None)
                # Clear auto-allow
                keys_to_remove = [k for k in session_auto_allow if k[0] == sid]
                for k in keys_to_remove:
                    del session_auto_allow[k]

            # Clean up request files
            for fpath in glob.glob(os.path.join(QUEUE_DIR, "*.request.json")):
                try:
                    with open(fpath) as f:
                        data = json.load(f)
                    if str(data.get("session_id", "")) == sid:
                        resp_path = fpath.replace(".request.json", ".response.json")
                        for p in (fpath, resp_path):
                            try:
                                os.remove(p)
                            except OSError:
                                pass
                except (json.JSONDecodeError, IOError):
                    continue

            print(f"[*] Session deregistered: {sid}")
            self._respond_json({"ok": True})

        elif path == "/api/respond":
            body = self._read_json()
            request_id = body.get("id", "")
            decision = body.get("decision", "deny")
            message = body.get("message", "")

            # Federation: check if this should be proxied
            sid = str(body.get("session_id", ""))
            remote_url = _get_remote_url_for_session(sid) if sid else None
            if remote_url:
                try:
                    proxy_body = dict(body)
                    proxy_body.pop("session_id", None)  # remote doesn't need federated sid
                    status, resp_body, ct = proxy_to_remote(
                        remote_url, "/api/respond", method="POST",
                        body=json.dumps(proxy_body).encode()
                    )
                    self.send_response(status)
                    self.send_header("Content-Type", ct)
                    self.end_headers()
                    self.wfile.write(resp_body)
                except Exception as e:
                    self.send_error(502, f"Remote proxy failed: {e}")
                return

            request_file = os.path.join(QUEUE_DIR, f"{request_id}.request.json")
            if not os.path.exists(request_file):
                self.send_error(404, "Request not found")
                return

            # "always" → write to settings.local.json
            if decision == "always":
                try:
                    with open(request_file) as f:
                        req_data = json.load(f)
                    settings_file = req_data.get("settings_file", "")
                    allow_patterns = body.get("allow_patterns") or []
                    if not allow_patterns:
                        allow_pattern = body.get("allow_pattern") or req_data.get("allow_pattern", "")
                        if allow_pattern:
                            allow_patterns = [allow_pattern]
                    if settings_file:
                        for pattern in allow_patterns:
                            self._add_to_settings(settings_file, pattern)
                except (json.JSONDecodeError, IOError):
                    pass

            response_file = os.path.join(QUEUE_DIR, f"{request_id}.response.json")
            resp_data = {"decision": decision}
            if message:
                resp_data["message"] = message
            with open(response_file, "w") as f:
                json.dump(resp_data, f)

            self._respond_json({"ok": True})

        elif path == "/api/session-allow":
            body = self._read_json()
            sid = str(body.get("session_id", ""))
            tool_name = body.get("tool_name", "")
            request_id = body.get("id", "")

            # Federation proxy
            remote_url = _get_remote_url_for_session(sid)
            if remote_url:
                try:
                    proxy_body = {"session_id": _get_original_session_id(sid), "tool_name": tool_name, "id": request_id}
                    status, resp_body, ct = proxy_to_remote(
                        remote_url, "/api/session-allow", method="POST",
                        body=json.dumps(proxy_body).encode()
                    )
                    self.send_response(status)
                    self.send_header("Content-Type", ct)
                    self.end_headers()
                    self.wfile.write(resp_body)
                except Exception as e:
                    self.send_error(502, f"Remote proxy failed: {e}")
                return

            if sid and tool_name:
                session_auto_allow[(sid, tool_name)] = True
                print(f"[+] Session auto-allow: {tool_name} for session {sid}")
            if request_id:
                resp_file = os.path.join(QUEUE_DIR, f"{request_id}.response.json")
                req_file = os.path.join(QUEUE_DIR, f"{request_id}.request.json")
                if os.path.exists(req_file):
                    with open(resp_file, "w") as f:
                        json.dump({"decision": "allow"}, f)
            self._respond_json({"ok": True})

        elif path == "/api/send-prompt":
            body = self._read_json()
            sid = str(body.get("session_id", ""))
            prompt = body.get("prompt", "")
            if not sid or not prompt:
                self.send_error(400, "Missing session_id or prompt")
                return

            # Federation proxy
            remote_url = _get_remote_url_for_session(sid)
            if remote_url:
                try:
                    proxy_body = {"session_id": _get_original_session_id(sid), "prompt": prompt}
                    status, resp_body, ct = proxy_to_remote(
                        remote_url, "/api/send-prompt", method="POST",
                        body=json.dumps(proxy_body).encode()
                    )
                    self.send_response(status)
                    self.send_header("Content-Type", ct)
                    self.end_headers()
                    self.wfile.write(resp_body)
                except Exception as e:
                    self.send_error(502, f"Remote proxy failed: {e}")
                return

            with sessions_lock:
                s = sessions.get(sid)
            if not s:
                self.send_error(404, "Session not found")
                return

            if send_prompt(s, prompt):
                print(f"[>] Prompt sent to session {sid}: {prompt[:80]}")
                self._respond_json({"ok": True})
            else:
                self.send_error(500, "Failed to send prompt")

        elif path == "/api/upload-image":
            os.makedirs(IMAGE_DIR, exist_ok=True)
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                self.send_error(400, "Expected multipart/form-data")
                return
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type}
            )
            file_item = form["image"]
            if file_item.filename:
                ext = os.path.splitext(file_item.filename)[1].lower() or ".png"
                filename = str(uuid.uuid4()) + ext
                filepath = os.path.join(IMAGE_DIR, filename)
                with open(filepath, "wb") as f:
                    f.write(file_item.file.read())
                print(f"[img] Saved image: {filepath}")
                self._respond_json({"ok": True, "path": filepath})
            else:
                self.send_error(400, "No file uploaded")

        # Legacy endpoints for backward compatibility
        elif path == "/api/session-reset":
            body = self._read_json()
            sid = str(body.get("session_id", ""))
            source = body.get("source", "unknown")
            if not sid:
                self.send_error(400, "Missing session_id")
                return
            # Clear auto-allow
            keys_to_remove = [k for k in session_auto_allow if k[0] == sid]
            for k in keys_to_remove:
                del session_auto_allow[k]
            print(f"[*] Session reset (legacy): session={sid} source={source}")
            self._respond_json({"ok": True})

        elif path == "/api/session-end":
            body = self._read_json()
            sid = str(body.get("session_id", ""))
            if not sid:
                self.send_error(400, "Missing session_id")
                return
            with sessions_lock:
                sessions.pop(sid, None)
            keys_to_remove = [k for k in session_auto_allow if k[0] == sid]
            for k in keys_to_remove:
                del session_auto_allow[k]
            print(f"[*] Session end (legacy): session={sid}")
            self._respond_json({"ok": True})

        else:
            self.send_error(404)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length))

    def _respond_json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _respond_html(self, html):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def _add_to_settings(self, settings_file, pattern):
        """Add an allow pattern to settings.local.json."""
        try:
            if os.path.exists(settings_file):
                with open(settings_file) as f:
                    settings = json.load(f)
            else:
                settings = {"permissions": {"allow": []}}
            if "permissions" not in settings:
                settings["permissions"] = {"allow": []}
            if "allow" not in settings["permissions"]:
                settings["permissions"]["allow"] = []
            if pattern not in settings["permissions"]["allow"]:
                settings["permissions"]["allow"].append(pattern)
                with open(settings_file, "w") as f:
                    json.dump(settings, f, indent=2)
                    f.write("\n")
                print(f"[+] Added to allowlist: {pattern}")
        except (json.JSONDecodeError, IOError) as e:
            print(f"[!] Failed to update settings: {e}")


def scan_existing_sessions():
    """Scan tmux panes for running claude processes and register them."""
    if IS_WINDOWS:
        # Tmux is not available on Windows; sessions will register via hooks
        return

    # Find all tmux sockets
    import pathlib
    tmux_sockets = []
    for sock_dir in pathlib.Path("/tmp").glob("tmux-*"):
        for sock in sock_dir.iterdir():
            if sock.is_socket():
                tmux_sockets.append(str(sock))
    if not tmux_sockets:
        return

    home = os.path.expanduser("~")
    projects_dir = os.path.join(home, ".claude", "projects")
    if not os.path.isdir(projects_dir):
        return

    for sock_path in tmux_sockets:
        try:
            result = subprocess.run(
                ["tmux", "-S", sock_path, "list-panes", "-a",
                 "-F", "#{pane_id} #{pane_current_command} #{pane_pid}"],
                capture_output=True, text=True, timeout=3
            )
        except Exception:
            continue

        # Build pane_pid -> pane_id mapping
        pane_map = {}  # shell_pid -> pane_id
        for line in result.stdout.strip().splitlines():
            parts = line.split(" ", 2)
            if len(parts) >= 3:
                pane_id, cmd, shell_pid = parts[0], parts[1], parts[2]
                pane_map[shell_pid] = pane_id

        # Find claude processes whose parent is a tmux shell
        for shell_pid, pane_id in pane_map.items():
            try:
                children = subprocess.run(
                    ["pgrep", "-P", shell_pid],
                    capture_output=True, text=True, timeout=3
                )
            except Exception:
                continue

            for child_pid_str in children.stdout.strip().splitlines():
                child_pid = child_pid_str.strip()
                if not child_pid:
                    continue

                # Get process command line (cross-platform)
                try:
                    ps_result = subprocess.run(
                        ["ps", "-p", child_pid, "-o", "comm=,args="],
                        capture_output=True, text=True, timeout=3
                    )
                    if ps_result.returncode != 0 or not ps_result.stdout.strip():
                        continue
                    ps_line = ps_result.stdout.strip()
                    comm = ps_line.split()[0].rsplit("/", 1)[-1]  # basename
                    if comm not in ("claude", "node"):
                        continue
                    if "claude" not in ps_line:
                        continue
                except Exception:
                    continue

                # Get cwd (cross-platform)
                try:
                    lsof_result = subprocess.run(
                        ["lsof", "-p", child_pid, "-Fn", "-a", "-d", "cwd"],
                        capture_output=True, text=True, timeout=3
                    )
                    cwd = None
                    for lsof_line in lsof_result.stdout.splitlines():
                        if lsof_line.startswith("n/"):
                            cwd = lsof_line[1:]
                            break
                    if not cwd:
                        continue
                except Exception:
                    continue

                # Find transcript: encode cwd to project dir name
                encoded = cwd.replace("/", "-")
                if not encoded.startswith("-"):
                    encoded = "-" + encoded
                proj_dir = os.path.join(projects_dir, encoded)
                if not os.path.isdir(proj_dir):
                    continue

                jsonl_files = glob.glob(os.path.join(proj_dir, "*.jsonl"))
                if not jsonl_files:
                    continue
                # Most recently modified transcript
                transcript_path = max(jsonl_files, key=os.path.getmtime)
                # Session ID = filename without extension (UUID)
                session_id = os.path.splitext(os.path.basename(transcript_path))[0]
                tmux_socket = f"{sock_path},0,0"  # simplified; enough for send-keys

                with sessions_lock:
                    if session_id in sessions:
                        continue
                    sessions[session_id] = {
                        "transcript_path": transcript_path,
                        "tmux_pane": pane_id,
                        "tmux_socket": tmux_socket,
                        "cwd": cwd,
                        "registered_at": time.time(),
                        "transcript_offset": 0,
                        "transcript_entries": [],
                        "derived_state": "busy",
                        "last_activity": time.time(),
                        "last_summary": "",
                        "last_user_prompt": "",
                    }
                print(f"[*] Auto-discovered session: {session_id} pane={pane_id} cwd={cwd}")


def main():
    parser = argparse.ArgumentParser(description="Claude Code WebUI Server")
    parser.add_argument("--remotes", help="Path to remotes.json (default: remotes.json in script dir)")
    parser.add_argument("--name", default="local", help="Name for this machine in dashboard (default: local)")
    parser.add_argument("--lan", action="store_true", help="Listen on 0.0.0.0 instead of 127.0.0.1 (allow LAN access)")
    args = parser.parse_args()

    global local_name, remote_servers
    local_name = args.name

    # Load remotes config
    remotes_path = args.remotes
    if not remotes_path:
        remotes_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "remotes.json")
    if os.path.isfile(remotes_path):
        try:
            with open(remotes_path) as f:
                remote_servers = json.load(f)
            print(f"[*] Federation: {len(remote_servers)} remote(s) loaded from {remotes_path}")
            for r in remote_servers:
                print(f"    - {r['name']}: {r['url']}")
        except (json.JSONDecodeError, IOError) as e:
            print(f"[!] Failed to load remotes: {e}")

    os.makedirs(QUEUE_DIR, exist_ok=True)

    # Scan for existing sessions before starting
    try:
        scan_existing_sessions()
    except Exception as e:
        print(f"[!] Session scan failed: {e}")

    # Background threads
    threading.Thread(target=auto_allow_loop, daemon=True).start()
    threading.Thread(target=zombie_cleanup_loop, daemon=True).start()

    if _has_feishu:
        try:
            start_feishu_channel()
        except Exception as e:
            print(f"[feishu] Failed to start: {e}")

    if _has_teams:
        try:
            start_teams_channel()
        except Exception as e:
            print(f"[teams] Failed to start: {e}")

    bind_addr = "0.0.0.0" if args.lan else "127.0.0.1"
    server = HTTPServer((bind_addr, PORT), WebUIHandler)
    print(f"Claude Code WebUI Server running on http://{bind_addr}:{PORT}")
    print(f"Watching: {QUEUE_DIR}")
    print("Transcript-driven architecture | Tmux-only prompt delivery")
    print("Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
