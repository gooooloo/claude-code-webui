#!/usr/bin/env python3
"""
Claude Code WebUI Server — Transcript-driven, Tmux-only architecture.

Maintains a session registry, parses transcript JSONL files incrementally,
and serves a multi-session dashboard UI.

Usage: python3 server.py
Then open http://localhost:19836
"""

import json
import glob
import os
import re
import signal
import subprocess
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import uuid
import cgi

try:
    from channel_feishu import start_feishu_channel
    _has_feishu = True
except ImportError:
    _has_feishu = False

QUEUE_DIR = "/tmp/claude-webui"
IMAGE_DIR = "/tmp/claude-images"
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
        return

    if not new_data:
        return

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

    if not new_entries:
        return

    with sessions_lock:
        s = sessions.get(sid)
        if not s:
            return
        s["transcript_offset"] = offset + bytes_consumed
        s["transcript_entries"].extend(new_entries)
        s["last_activity"] = time.time()

        # Derive state from transcript
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
        # Collapse whitespace
        clean = re.sub(r"\s+", " ", clean).strip()
        if clean:
            user_prompt = clean[:200]
            break

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
                    summary = text[:200]

        # Check for pending permission request
        pending_request = _find_pending_request(sid)
        if pending_request:
            # Check if the tool_use has already been resolved in transcript
            req_tool = pending_request.get("tool_name", "")
            req_input = pending_request.get("tool_input", {})
            if _tool_use_resolved_in_transcript(entries, req_tool, req_input):
                # TUI already approved — clean up stale files
                _cleanup_stale_request(pending_request.get("id", ""))
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

        if stop_reason == "end_turn":
            return "idle", summary, user_prompt

    # Check ordering: if last user message is after last assistant
    if last_user and last_assistant:
        user_idx = -1
        asst_idx = -1
        for i, entry in enumerate(entries):
            if entry is last_user:
                user_idx = i
            if entry is last_assistant:
                asst_idx = i
        if user_idx > asst_idx:
            return "busy", "", user_prompt

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
    """Check if a tool_use matching this request has a tool_result in transcript."""
    # Walk backwards looking for the matching tool_use
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
            # Check if there's a tool_result for this tool_id
            if _has_tool_result(entries[i:], tool_id):
                return True
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
    """Scan pending requests and auto-approve those matching session auto-allow rules."""
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
        if (sid, tname) in session_auto_allow:
            try:
                with open(resp_path, "w") as f:
                    json.dump({"decision": "allow"}, f)
                print(f"[~] Auto-allowed {tname} for session {sid}")
            except IOError:
                pass


def auto_allow_loop():
    """Background thread: periodically check for auto-allowable requests."""
    while True:
        if session_auto_allow:
            check_auto_allow()
        time.sleep(0.5)


# ── Zombie session cleanup ──

def _is_session_alive(sid, session_data):
    """Check if a session is still active."""
    # For numeric session IDs (PIDs), check if the process is alive
    try:
        pid = int(sid)
        os.kill(pid, 0)
        return True
    except ValueError:
        pass  # Non-numeric (UUID) session ID
    except (OSError, ProcessLookupError):
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


# ── Tmux interaction ──

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


# ── HTML Dashboard ──

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Sessions</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
    background: #1a1a2e;
    color: #e0e0e0;
    min-height: 100vh;
  }
  .header {
    padding: 16px 24px;
    display: flex;
    align-items: center;
    gap: 12px;
    border-bottom: 1px solid #2a2a4a;
  }
  .header h1 {
    font-size: 18px;
    color: #a78bfa;
  }
  .header .status {
    font-size: 12px;
    color: #666;
    margin-left: auto;
  }
  .header .back-btn {
    background: #2a2a4a;
    color: #a78bfa;
    border: none;
    padding: 6px 14px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
    display: none;
  }
  .header .back-btn:hover { background: #3a3a5a; }
  .container { padding: 16px 24px; }

  /* ── Dashboard view ── */
  .dashboard { }
  .empty {
    text-align: center;
    color: #555;
    margin-top: 80px;
    font-size: 16px;
  }
  .empty .dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    background: #4ade80;
    border-radius: 50%;
    margin-right: 8px;
    animation: pulse 2s ease-in-out infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 0.3; }
    50% { opacity: 1; }
  }
  .session-card {
    background: #16213e;
    border: 1px solid #2a2a4a;
    border-left: 4px solid #666;
    border-radius: 12px;
    padding: 16px 20px;
    margin-bottom: 12px;
    cursor: pointer;
    transition: all 0.2s;
    animation: slideIn 0.3s ease;
  }
  .session-card:hover { border-color: #a78bfa55; background: #1a2744; }
  .session-card.state-idle { border-left-color: #4ade80; }
  .session-card.state-busy { border-left-color: #3b82f6; }
  .session-card.state-permission_prompt { border-left-color: #ef4444; }
  .session-card.state-elicitation { border-left-color: #22d3ee; }
  .session-card.state-plan_review { border-left-color: #a78bfa; }
  @keyframes slideIn {
    from { opacity: 0; transform: translateY(-10px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .sc-top {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 8px;
  }
  .state-badge {
    padding: 3px 10px;
    border-radius: 6px;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
  }
  .badge-idle { background: #4ade8022; color: #4ade80; }
  .badge-busy { background: #3b82f622; color: #3b82f6; }
  .badge-permission_prompt { background: #ef444422; color: #ef4444; }
  .badge-elicitation { background: #22d3ee22; color: #22d3ee; }
  .badge-plan_review { background: #a78bfa22; color: #a78bfa; }
  .sc-project {
    font-weight: 700;
    font-size: 14px;
    color: #e0e0e0;
  }
  .sc-sid {
    font-size: 11px;
    color: #666;
    margin-left: auto;
  }
  .sc-user-prompt {
    font-size: 13px;
    color: #c9d1d9;
    line-height: 1.5;
    max-height: 40px;
    overflow: hidden;
    margin-bottom: 4px;
  }
  .sc-user-prompt::before {
    content: '> ';
    color: #58a6ff;
  }
  .sc-summary {
    font-size: 13px;
    color: #999;
    line-height: 1.5;
    max-height: 40px;
    overflow: hidden;
  }
  .sc-time {
    font-size: 11px;
    color: #555;
    margin-top: 6px;
  }
  .sc-actions {
    margin-top: 10px;
    display: flex;
    gap: 8px;
  }
  .attention-count {
    background: #ef4444;
    color: white;
    border-radius: 50%;
    width: 22px;
    height: 22px;
    font-size: 12px;
    font-weight: 700;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    margin-left: 8px;
  }

  /* ── Session detail view ── */
  .session-detail { display: none; }
  .transcript-view {
    max-height: calc(100vh - 280px);
    overflow-y: auto;
    margin-bottom: 16px;
    padding-right: 8px;
  }
  .msg {
    margin-bottom: 12px;
    padding: 12px 16px;
    border-radius: 10px;
    font-size: 13px;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .msg-user {
    background: #1e3a5f;
    border-left: 3px solid #3b82f6;
  }
  .msg-assistant {
    background: #1a2744;
    border-left: 3px solid #a78bfa;
  }
  .msg-tool {
    background: #0f0f23;
    border-left: 3px solid #f97316;
    font-size: 12px;
  }
  .msg-label {
    font-size: 11px;
    font-weight: 700;
    margin-bottom: 4px;
    text-transform: uppercase;
  }
  .msg-user .msg-label { color: #3b82f6; }
  .msg-assistant .msg-label { color: #a78bfa; }
  .msg-tool .msg-label { color: #f97316; }
  .msg-content {
    overflow: hidden;
    position: relative;
  }
  .msg-content.collapsed { max-height: 200px; }
  .msg-content.collapsed::after {
    content: '';
    position: absolute;
    bottom: 0; left: 0; right: 0;
    height: 40px;
    background: linear-gradient(transparent, #1a2744);
    pointer-events: none;
  }
  .msg-user .msg-content.collapsed::after {
    background: linear-gradient(transparent, #1e3a5f);
  }
  .msg-tool .msg-content.collapsed::after {
    background: linear-gradient(transparent, #0f0f23);
  }
  .msg-toggle {
    background: none;
    border: none;
    color: #a78bfa;
    font-size: 12px;
    cursor: pointer;
    padding: 2px 0;
    font-weight: 600;
  }

  /* ── Permission card in detail view ── */
  .perm-card {
    background: #16213e;
    border: 1px solid #ef444444;
    border-left: 4px solid #ef4444;
    border-radius: 12px;
    padding: 16px 20px;
    margin-bottom: 16px;
  }
  .perm-header {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 10px;
  }
  .perm-tool {
    padding: 4px 10px;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 600;
  }
  .perm-tool-bash { background: #ef444422; color: #ef4444; }
  .perm-tool-write { background: #f9731622; color: #f97316; }
  .perm-tool-plan { background: #a78bfa22; color: #a78bfa; }
  .perm-tool-question { background: #22d3ee22; color: #22d3ee; }
  .perm-tool-web { background: #3b82f622; color: #3b82f6; }
  .perm-tool-other { background: #66666622; color: #999; }
  .perm-detail {
    background: #0f0f23;
    border-radius: 8px;
    padding: 12px;
    font-size: 13px;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-all;
    margin-bottom: 12px;
    max-height: 300px;
    overflow: auto;
  }
  .perm-sub {
    color: #aaa;
    font-size: 12px;
    margin-bottom: 12px;
  }
  .allow-info {
    font-size: 12px;
    color: #888;
    margin-bottom: 12px;
  }
  .allow-info code {
    color: #facc15;
    background: #facc1511;
    padding: 2px 6px;
    border-radius: 4px;
  }
  .path-select-area { margin-bottom: 12px; }
  .path-option {
    background: #0f0f23;
    border: 2px solid #2a2a4a;
    border-radius: 8px;
    padding: 10px 14px;
    margin-bottom: 6px;
    cursor: pointer;
    transition: all 0.15s;
    font-size: 13px;
    font-family: monospace;
  }
  .path-option:hover { border-color: #f59e0b55; background: #16213e; }
  .path-option .path-label { color: #f59e0b; font-weight: 600; }
  .path-option .path-pattern { color: #888; font-size: 11px; margin-top: 2px; }

  /* ── Question card styles ── */
  .q-option {
    background: #0f0f23;
    border: 2px solid #2a2a4a;
    border-radius: 8px;
    padding: 12px 14px;
    margin-bottom: 8px;
    cursor: pointer;
    transition: all 0.15s;
    display: flex;
    align-items: flex-start;
    gap: 10px;
  }
  .q-option:hover { border-color: #22d3ee55; background: #16213e; }
  .q-option.selected { border-color: #22d3ee; background: #22d3ee0d; }
  .q-option .q-check {
    width: 20px; height: 20px;
    border: 2px solid #444;
    border-radius: 4px;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 14px;
    margin-top: 1px;
    transition: all 0.15s;
  }
  .q-option.selected .q-check {
    border-color: #22d3ee;
    background: #22d3ee;
    color: #0f0f23;
  }
  .q-option .q-label { color: #e0e0e0; font-weight: 600; font-size: 14px; }
  .q-option .q-desc { color: #888; font-size: 12px; margin-top: 2px; }
  .q-custom-area { margin-top: 10px; }
  .q-custom-toggle {
    background: none;
    border: 1px dashed #444;
    border-radius: 8px;
    color: #888;
    padding: 10px 14px;
    width: 100%;
    text-align: left;
    font-size: 13px;
    cursor: pointer;
  }
  .q-custom-toggle:hover { border-color: #22d3ee55; color: #aaa; }
  .q-custom-input {
    width: 100%;
    background: #0f0f23;
    border: 2px solid #22d3ee55;
    border-radius: 8px;
    color: #e0e0e0;
    padding: 10px 12px;
    font-size: 16px;
    font-family: monospace;
    line-height: 1.5;
    resize: vertical;
    min-height: 60px;
  }
  .q-custom-input:focus { outline: none; border-color: #22d3ee; }

  /* Plan feedback */
  .plan-feedback-input {
    width: 100%;
    background: #0f0f23;
    border: 1px solid #2a2a4a;
    border-radius: 8px;
    color: #e0e0e0;
    padding: 10px 12px;
    font-size: 16px;
    font-family: monospace;
    line-height: 1.5;
    resize: vertical;
    min-height: 60px;
  }
  .plan-feedback-input:focus { outline: none; border-color: #a78bfa; }

  /* ── Prompt input area ── */
  .prompt-area {
    border-top: 1px solid #2a2a4a;
    padding: 16px 24px;
    background: #16213e;
  }
  .image-upload-area {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
  }
  .btn-upload-image {
    background: #1e293b;
    border: 1px dashed #444;
    color: #a78bfa;
    padding: 6px 12px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 12px;
    white-space: nowrap;
  }
  .btn-upload-image:hover { border-color: #a78bfa; }
  .image-preview-area {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .image-thumb {
    position: relative;
    width: 60px;
    height: 60px;
    border-radius: 6px;
    overflow: hidden;
    border: 1px solid #333;
  }
  .image-thumb img { width: 100%; height: 100%; object-fit: cover; }
  .image-thumb .remove-btn {
    position: absolute;
    top: -4px; right: -4px;
    width: 18px; height: 18px;
    background: #ef4444;
    color: white;
    border: none;
    border-radius: 50%;
    font-size: 11px;
    cursor: pointer;
    padding: 0;
  }
  .prompt-row {
    display: flex;
    gap: 10px;
    align-items: flex-end;
  }
  .prompt-input {
    flex: 1;
    background: #0f0f23;
    border: 1px solid #2a2a4a;
    border-radius: 8px;
    color: #e0e0e0;
    padding: 10px 12px;
    font-size: 16px;
    font-family: monospace;
    line-height: 1.5;
    resize: vertical;
    min-height: 44px;
    max-height: 200px;
  }
  .prompt-input:focus { outline: none; border-color: #a78bfa; }
  .prompt-input::placeholder { color: #555; }
  .quick-actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 8px;
  }
  .btn-quick {
    background: #1e1e3a;
    border: 1px solid #2a2a4a;
    color: #ccc;
    padding: 5px 12px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
  }
  .btn-quick:hover { background: #2a2a4a; color: #fff; }

  /* ── Buttons ── */
  button {
    padding: 8px 20px;
    border: none;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s;
  }
  button:active { transform: scale(0.97); }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-allow { background: #3b82f6; color: white; }
  .btn-allow:hover { background: #2563eb; }
  .btn-allow-lg { background: #3b82f6; color: white; padding: 10px 28px; font-size: 14px; }
  .btn-allow-lg:hover { background: #2563eb; }
  .btn-always { background: #16a34a; color: white; }
  .btn-always:hover { background: #15803d; }
  .btn-session { background: #0d9488; color: white; }
  .btn-session:hover { background: #0f766e; }
  .btn-deny { background: #333; color: #ccc; }
  .btn-deny:hover { background: #ef4444; color: white; }
  .btn-deny-sm { background: #333; color: #ccc; padding: 6px 14px; font-size: 12px; }
  .btn-deny-sm:hover { background: #ef4444; color: white; }
  .btn-feedback { background: #a78bfa22; color: #c4b5fd; }
  .btn-feedback:hover { background: #a78bfa44; }
  .btn-answer { background: #22d3ee; color: #0f0f23; font-weight: 700; }
  .btn-answer:hover { background: #06b6d4; }
  .btn-send { background: #a78bfa; color: white; padding: 10px 24px; white-space: nowrap; }
  .btn-send:hover { background: #8b5cf6; }
  .btn-allow-path { background: #78350f; color: #fbbf24; }
  .btn-allow-path:hover { background: #92400e; }
  .buttons {
    display: flex;
    gap: 10px;
    justify-content: flex-end;
    margin-top: 10px;
  }

  /* Markdown rendering */
  .md-h1, .md-h2, .md-h3 { font-weight: 700; margin: 4px 0 2px; }
  .md-h1 { color: #a78bfa; font-size: 15px; }
  .md-h2 { color: #c4b5fd; font-size: 14px; }
  .md-h3 { color: #ddd6fe; font-size: 13px; }
  code {
    background: #1e1e3a;
    color: #facc15;
    padding: 1px 5px;
    border-radius: 3px;
    font-size: 12px;
  }

  @media (max-width: 600px) {
    .container { padding: 12px; }
    .prompt-area { padding: 12px; }
    .session-card { padding: 12px 14px; }
    .perm-card { padding: 12px 14px; }
    .buttons { flex-wrap: wrap; gap: 8px; }
    .buttons button { flex: 1 1 calc(50% - 8px); min-width: 80px; }
  }
</style>
</head>
<body>
<div class="header">
  <button class="back-btn" id="backBtn" onclick="showDashboard()">Back</button>
  <h1 id="pageTitle">Claude Sessions</h1>
  <span class="status" id="status">Connected</span>
</div>

<!-- Dashboard view -->
<div class="container dashboard" id="dashboardView">
  <div id="sessionList"></div>
</div>

<!-- Session detail view -->
<div class="session-detail" id="detailView">
  <div class="container">
    <div id="permCards"></div>
    <div class="transcript-view" id="transcriptView"></div>
  </div>
  <div class="prompt-area" id="promptArea">
    <div class="quick-actions">
      <button class="btn-quick" onclick="quickPrompt('/compact')">/compact</button>
      <button class="btn-quick" onclick="quickPrompt('/clear')">/clear</button>
    </div>
    <div class="image-upload-area">
      <input type="file" id="imageFile" accept="image/*" style="display:none" onchange="handleImageFile(this)">
      <button class="btn-upload-image" onclick="document.getElementById('imageFile').click()">+ Image</button>
      <div class="image-preview-area" id="imagePreview"></div>
    </div>
    <div class="prompt-row">
      <textarea class="prompt-input" id="promptInput" placeholder="Send a prompt to this session..." rows="1"></textarea>
      <button class="btn-send" onclick="sendPrompt()">Send</button>
    </div>
  </div>
</div>

<script>
// ── State ──
let currentView = 'dashboard';
let currentSessionId = null;
let respondedIds = new Set();
let imagePaths = [];
let pollTimer = null;
let lastDashboardHash = '';
let lastPermCardId = '';
let lastTranscriptHash = '';

// Question state
const questionSelections = {};
const questionMultiSelect = {};

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

function renderMarkdown(text) {
  let s = esc(text.trim());
  s = s.replace(/^### (.+)$/gm, '<span class="md-h3">$1</span>');
  s = s.replace(/^## (.+)$/gm, '<span class="md-h2">$1</span>');
  s = s.replace(/^# (.+)$/gm, '<span class="md-h1">$1</span>');
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  s = s.replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>');
  s = s.replace(/^(\\s*)[*-] (.+)$/gm, '$1&#8226; $2');
  return s;
}

function toolCat(name) {
  if (name === 'ExitPlanMode') return 'plan';
  if (name === 'AskUserQuestion') return 'question';
  if (name === 'Bash' || name === 'mcp__acp__Bash') return 'bash';
  if (/Write|Edit/.test(name)) return 'write';
  if (name === 'WebFetch' || name === 'WebSearch') return 'web';
  return 'other';
}

function stateLabel(s) {
  const m = {
    idle: 'Idle',
    busy: 'Working',
    permission_prompt: 'Needs Approval',
    elicitation: 'Question',
    plan_review: 'Plan Review'
  };
  return m[s] || s;
}

// ── Dashboard ──

async function fetchSessions() {
  try {
    const res = await fetch('/api/sessions');
    const data = await res.json();
    document.getElementById('status').textContent = 'Last checked: ' + new Date().toLocaleTimeString();
    renderDashboard(data.sessions || []);
  } catch (e) {
    document.getElementById('status').textContent = 'Connection error';
  }
}

function renderDashboard(sessions) {
  const el = document.getElementById('sessionList');
  if (sessions.length === 0) {
    if (lastDashboardHash !== 'empty') {
      el.innerHTML = '<div class="empty"><span class="dot"></span>No active sessions</div>';
      lastDashboardHash = 'empty';
    }
    document.title = 'Claude Sessions';
    return;
  }
  const needAttention = sessions.filter(s =>
    s.state === 'permission_prompt' || s.state === 'elicitation' || s.state === 'plan_review' || s.state === 'idle'
  ).length;
  document.title = needAttention > 0 ? '(' + needAttention + ') Claude Sessions' : 'Claude Sessions';

  // Skip re-render if nothing changed
  const hash = sessions.map(s => s.session_id + ':' + (s.state||'') + ':' + (s.last_summary||'') + ':' + (s.last_user_prompt||'') + ':' + (s.last_activity||'') + ':' + (s.pending_request ? s.pending_request.id : '')).join('|');
  if (hash === lastDashboardHash) return;
  lastDashboardHash = hash;

  let html = '';
  sessions.forEach(s => {
    const project = (s.cwd || '').split('/').pop() || '?';
    const state = s.state || 'busy';
    const summary = esc(s.last_summary || '');
    const userPrompt = esc(s.last_user_prompt || '');
    const time = s.last_activity ? new Date(s.last_activity * 1000).toLocaleTimeString() : '';
    html += '<div class="session-card state-' + state + '" onclick="openSession(\\'' + esc(s.session_id) + '\\')">';
    html += '<div class="sc-top">';
    html += '<span class="state-badge badge-' + state + '">' + stateLabel(state) + '</span>';
    html += '<span class="sc-project">' + esc(project) + '</span>';
    html += '<span class="sc-sid">Session ' + esc(s.session_id) + '</span>';
    html += '</div>';
    if (userPrompt) html += '<div class="sc-user-prompt">' + userPrompt + '</div>';
    if (summary) html += '<div class="sc-summary">' + summary + '</div>';
    if (time) html += '<div class="sc-time">' + time + '</div>';

    // Inline permission approve/deny on dashboard
    if (state === 'permission_prompt' && s.pending_request) {
      const pr = s.pending_request;
      html += '<div class="sc-actions" onclick="event.stopPropagation()">';
      html += '<span style="color:#ef4444;font-size:12px;font-weight:700">' + esc(pr.tool_name) + '</span>';
      html += ' <span style="color:#888;font-size:12px">' + esc((pr.detail || '').substring(0, 80)) + '</span>';
      html += ' <button class="btn-allow" style="padding:5px 14px;font-size:12px" onclick="respond(\\'' + esc(pr.id) + '\\',\\'allow\\',this)">Allow</button>';
      html += ' <button class="btn-deny-sm" onclick="respond(\\'' + esc(pr.id) + '\\',\\'deny\\',this)">Deny</button>';
      html += '</div>';
    }
    html += '</div>';
  });
  el.innerHTML = html;
}

// ── Session Detail ──

function openSession(sid) {
  currentSessionId = sid;
  currentView = 'detail';
  document.getElementById('dashboardView').style.display = 'none';
  document.getElementById('detailView').style.display = 'block';
  document.getElementById('backBtn').style.display = 'block';
  document.getElementById('pageTitle').textContent = 'Session ' + sid;
  document.getElementById('transcriptView').innerHTML = '';
  document.getElementById('permCards').innerHTML = '';
  lastPermCardId = '';
  lastTranscriptHash = '';
  fetchSessionDetail();
  startDetailPolling();
}

function showDashboard() {
  currentSessionId = null;
  currentView = 'dashboard';
  document.getElementById('dashboardView').style.display = 'block';
  document.getElementById('detailView').style.display = 'none';
  document.getElementById('backBtn').style.display = 'none';
  document.getElementById('pageTitle').textContent = 'Claude Sessions';
  stopDetailPolling();
  lastDashboardHash = '';
  fetchSessions();
}

let detailPollTimer = null;
function startDetailPolling() {
  stopDetailPolling();
  detailPollTimer = setInterval(fetchSessionDetail, 1000);
}
function stopDetailPolling() {
  if (detailPollTimer) { clearInterval(detailPollTimer); detailPollTimer = null; }
}

async function fetchSessionDetail() {
  if (!currentSessionId) return;
  try {
    const res = await fetch('/api/sessions');
    const data = await res.json();
    const session = (data.sessions || []).find(s => s.session_id === currentSessionId);
    if (!session) {
      showDashboard();
      return;
    }

    // Render permission card if applicable
    renderPermCards(session);

    // Fetch transcript
    const tRes = await fetch('/api/session/' + currentSessionId + '/transcript?limit=50');
    const tData = await tRes.json();
    renderTranscript(tData.entries || []);
  } catch (e) {
    console.error('fetchSessionDetail error:', e);
  }
}

function renderPermCards(session) {
  const el = document.getElementById('permCards');
  if (!session.pending_request) {
    if (lastPermCardId) { el.innerHTML = ''; lastPermCardId = ''; }
    return;
  }
  const pr = session.pending_request;
  if (respondedIds.has(pr.id)) {
    if (lastPermCardId) { el.innerHTML = ''; lastPermCardId = ''; }
    return;
  }
  // Skip re-render if same permission request is already shown
  if (pr.id === lastPermCardId) return;
  lastPermCardId = pr.id;

  const cat = toolCat(pr.tool_name);
  const isBenign = ['plan','question','web'].includes(cat) || pr.tool_name === 'Read';
  const denyClass = isBenign ? 'btn-deny-sm' : 'btn-deny';
  const allowClass = isBenign ? 'btn-allow-lg' : 'btn-allow';

  let html = '<div class="perm-card" id="perm-' + esc(pr.id) + '">';
  html += '<div class="perm-header">';
  html += '<span class="perm-tool perm-tool-' + cat + '">' + esc(pr.tool_name) + '</span>';
  html += '</div>';

  if (cat === 'plan') {
    // Plan review card
    const planText = (pr.tool_input && pr.tool_input.plan) || pr.detail || '';
    html += '<div class="perm-detail">' + renderMarkdown(planText) + '</div>';
    if (pr.detail_sub) html += '<div class="perm-sub">' + esc(pr.detail_sub) + '</div>';
    html += '<div id="feedback-area-' + esc(pr.id) + '" style="display:none">';
    html += '<textarea class="plan-feedback-input" id="feedback-input-' + esc(pr.id) + '" placeholder="Tell Claude what to change..." rows="3"></textarea>';
    html += '<div class="buttons" style="margin-top:8px">';
    html += '<button class="btn-deny-sm" onclick="toggleFeedback(\\'' + esc(pr.id) + '\\')">Cancel</button>';
    html += '<button class="btn-allow" onclick="submitFeedback(\\'' + esc(pr.id) + '\\')">Send Feedback</button>';
    html += '</div></div>';
    html += '<div class="buttons" id="plan-buttons-' + esc(pr.id) + '">';
    html += '<button class="btn-deny-sm" onclick="respond(\\'' + esc(pr.id) + '\\',\\'deny\\',this)">Deny</button>';
    html += '<button class="btn-feedback" onclick="toggleFeedback(\\'' + esc(pr.id) + '\\')">Feedback</button>';
    html += '<button class="btn-allow-lg" onclick="respond(\\'' + esc(pr.id) + '\\',\\'allow\\',this)">Approve</button>';
    html += '</div>';

  } else if (cat === 'question') {
    // Question card
    html += renderQuestionCard(pr);

  } else {
    // Standard permission card
    if (pr.detail) html += '<div class="perm-detail">' + esc(pr.detail) + '</div>';
    if (pr.detail_sub) html += '<div class="perm-sub">' + esc(pr.detail_sub) + '</div>';

    // Allow info
    const hasMulti = pr.allow_patterns && pr.allow_patterns.length > 1;
    if (hasMulti) {
      html += '<div class="allow-info">"Always Allow All" will apply to: ' +
        pr.allow_patterns.map(p => '<code>' + esc(p) + '</code>').join(', ') + '</div>';
    } else {
      html += '<div class="allow-info">"Always Allow" will apply to: <code>' + esc(pr.allow_pattern || '') + '</code></div>';
    }

    // Session-allow info for Read/Edit/Write
    if (['Read','Edit','Write'].includes(pr.tool_name)) {
      html += '<div class="allow-info">"Allow this session" will auto-approve all <code>' + esc(pr.tool_name) + '</code> calls in session ' + esc(String(pr.session_id)) + '</div>';
    }

    // Path select area for Edit/Write
    if (['Edit','Write'].includes(pr.tool_name)) {
      html += '<div class="path-select-area" id="path-area-' + esc(pr.id) + '" style="display:none"></div>';
    }

    // Split patterns area
    if (hasMulti) {
      html += '<div class="path-select-area" id="split-area-' + esc(pr.id) + '" style="display:none"></div>';
    }

    html += '<div class="buttons">';
    html += '<button class="' + denyClass + '" onclick="respond(\\'' + esc(pr.id) + '\\',\\'deny\\',this)">Deny</button>';
    if (hasMulti) {
      html += '<button class="btn-always" onclick="respondAlwaysAll(\\'' + esc(pr.id) + '\\',this)">Always Allow All</button>';
      html += '<button class="btn-always" style="background:#0d9488" onclick="toggleSplitPatterns(\\'' + esc(pr.id) + '\\')">Allow Command...</button>';
    } else {
      html += '<button class="btn-always" onclick="respond(\\'' + esc(pr.id) + '\\',\\'always\\',this)">Always Allow</button>';
    }
    if (['Edit','Write'].includes(pr.tool_name)) {
      html += '<button class="btn-allow-path" onclick="togglePathSelect(\\'' + esc(pr.id) + '\\')">Allow Path</button>';
    }
    if (['Read','Edit','Write'].includes(pr.tool_name)) {
      html += '<button class="btn-session" onclick="respondSessionAllow(\\'' + esc(pr.id) + '\\',\\'' + esc(String(pr.session_id)) + '\\',\\'' + esc(pr.tool_name) + '\\',this)">Allow this session</button>';
    }
    html += '<button class="' + allowClass + '" onclick="respond(\\'' + esc(pr.id) + '\\',\\'allow\\',this)">Allow</button>';
    html += '</div>';
  }
  html += '</div>';
  el.innerHTML = html;
}

function renderQuestionCard(pr) {
  const questions = pr.tool_input && pr.tool_input.questions;
  if (!questions || !Array.isArray(questions) || questions.length === 0) {
    return '<div class="perm-detail">' + esc(pr.detail || '(no question data)') + '</div>' +
      '<div class="buttons">' +
      '<button class="btn-deny-sm" onclick="respond(\\'' + esc(pr.id) + '\\',\\'deny\\',this)">Deny</button>' +
      '<button class="btn-allow-lg" onclick="respond(\\'' + esc(pr.id) + '\\',\\'allow\\',this)">Allow</button>' +
      '</div>';
  }
  questionSelections[pr.id] = {};
  questionMultiSelect[pr.id] = questions.some(q => q.multiSelect);
  questions.forEach((q, qi) => { questionSelections[pr.id][qi] = new Set(); });

  let h = '';
  questions.forEach((q, qi) => {
    h += '<div data-qidx="' + qi + '" style="margin-bottom:14px">';
    h += '<div style="color:#22d3ee;font-weight:700;font-size:15px;margin-bottom:10px">' + esc(q.question) + '</div>';
    if (q.options && Array.isArray(q.options)) {
      q.options.forEach((opt, oi) => {
        h += '<div class="q-option" id="qopt-' + esc(pr.id) + '-' + qi + '-' + oi + '" onclick="toggleQOpt(\\'' + esc(pr.id) + '\\',' + qi + ',' + oi + ',' + !!q.multiSelect + ')">';
        h += '<div class="q-check"></div><div>';
        h += '<div class="q-label">' + esc(opt.label) + '</div>';
        if (opt.description) h += '<div class="q-desc">' + esc(opt.description) + '</div>';
        h += '</div></div>';
      });
    }
    h += '</div>';
  });
  h += '<div class="q-custom-area">';
  h += '<button class="q-custom-toggle" id="q-custom-btn-' + esc(pr.id) + '" onclick="toggleQCustom(\\'' + esc(pr.id) + '\\')">Type a custom answer...</button>';
  h += '<textarea class="q-custom-input" id="q-custom-input-' + esc(pr.id) + '" style="display:none" placeholder="Type your answer..." rows="2"></textarea>';
  h += '</div>';
  h += '<div class="buttons" style="margin-top:14px">';
  h += '<button class="btn-deny-sm" onclick="respond(\\'' + esc(pr.id) + '\\',\\'deny\\',this)">Deny</button>';
  h += '<button class="btn-answer" id="q-submit-' + esc(pr.id) + '" onclick="submitQAnswer(\\'' + esc(pr.id) + '\\')">Send Answer</button>';
  h += '</div>';
  return h;
}

function toggleQOpt(reqId, qIdx, optIdx, multi) {
  const sel = questionSelections[reqId][qIdx];
  if (sel.has(optIdx)) sel.delete(optIdx);
  else { if (!multi) sel.clear(); sel.add(optIdx); }
  const card = document.getElementById('perm-' + reqId);
  if (!card) return;
  const section = card.querySelector('[data-qidx="' + qIdx + '"]');
  if (!section) return;
  section.querySelectorAll('.q-option').forEach((el, i) => {
    el.classList.toggle('selected', sel.has(i));
  });
  if (!multi && sel.size > 0) {
    const ci = document.getElementById('q-custom-input-' + reqId);
    const cb = document.getElementById('q-custom-btn-' + reqId);
    if (ci) { ci.value = ''; ci.style.display = 'none'; }
    if (cb) cb.style.display = 'block';
  }
}

function toggleQCustom(reqId) {
  const btn = document.getElementById('q-custom-btn-' + reqId);
  const input = document.getElementById('q-custom-input-' + reqId);
  const multi = questionMultiSelect[reqId];
  if (input.style.display === 'none') {
    input.style.display = 'block'; btn.style.display = 'none';
    if (!multi) {
      const sel = questionSelections[reqId];
      for (const qi in sel) {
        sel[qi].clear();
        const card = document.getElementById('perm-' + reqId);
        if (card) card.querySelectorAll('[data-qidx="' + qi + '"] .q-option').forEach(el => el.classList.remove('selected'));
      }
    }
    input.focus();
  } else {
    input.style.display = 'none'; input.value = ''; btn.style.display = 'block';
  }
}

function submitQAnswer(reqId) {
  const sel = questionSelections[reqId];
  const ci = document.getElementById('q-custom-input-' + reqId);
  const customText = ci ? ci.value.trim() : '';
  const selected = [];
  for (const qi in sel) {
    sel[qi].forEach(oi => {
      const el = document.getElementById('qopt-' + reqId + '-' + qi + '-' + oi);
      if (el) { const l = el.querySelector('.q-label'); if (l) selected.push(l.textContent); }
    });
  }
  const multi = questionMultiSelect[reqId];
  if (multi && customText) selected.push(customText);
  else if (!multi && customText) {
    const btn = document.getElementById('q-submit-' + reqId);
    respond(reqId, 'deny', btn, 'User answered: ' + customText);
    return;
  }
  if (selected.length === 0) {
    const btn = document.getElementById('q-submit-' + reqId);
    btn.style.background = '#ef4444'; btn.textContent = 'Select an option';
    setTimeout(() => { btn.style.background = ''; btn.textContent = 'Send Answer'; }, 1500);
    return;
  }
  const msg = 'User answered: ' + selected.join(', ');
  const btn = document.getElementById('q-submit-' + reqId);
  respond(reqId, 'deny', btn, msg);
}

function renderTranscript(entries) {
  const el = document.getElementById('transcriptView');
  // Skip re-render if transcript hasn't changed
  const tHash = entries.length + ':' + (entries.length > 0 ? JSON.stringify(entries[entries.length - 1]).length : 0);
  if (tHash === lastTranscriptHash) return;
  lastTranscriptHash = tHash;
  const wasAtBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  let html = '';
  entries.forEach(e => {
    if (e.type === 'user') {
      const content = e.message && e.message.content;
      if (!content) return;
      let text = '';
      let hasToolResult = false;
      if (Array.isArray(content)) {
        content.forEach(c => {
          if (typeof c === 'string') text += c;
          else if (c.type === 'text') text += c.text || '';
          else if (c.type === 'tool_result') hasToolResult = true;
        });
      }
      if (hasToolResult) return; // Skip tool result messages
      if (!text.trim()) return;
      html += '<div class="msg msg-user"><div class="msg-label">You</div><div class="msg-content">' + esc(text) + '</div></div>';
    } else if (e.type === 'assistant') {
      const msg = e.message || {};
      const content = msg.content || [];
      let text = '';
      let tools = [];
      content.forEach(c => {
        if (typeof c === 'object') {
          if (c.type === 'text') text += c.text || '';
          else if (c.type === 'tool_use') tools.push(c);
        }
      });
      if (text.trim()) {
        html += '<div class="msg msg-assistant"><div class="msg-label">Claude</div><div class="msg-content">' + renderMarkdown(text) + '</div></div>';
      }
      tools.forEach(t => {
        let detail = '';
        if (t.name === 'Bash' || t.name === 'mcp__acp__Bash') detail = (t.input && t.input.command) || '';
        else if (t.name === 'Write' || t.name === 'Edit' || t.name === 'mcp__acp__Write' || t.name === 'mcp__acp__Edit') detail = (t.input && t.input.file_path) || '';
        else if (t.name === 'Read') detail = (t.input && t.input.file_path) || '';
        else detail = JSON.stringify(t.input || {}).substring(0, 200);
        html += '<div class="msg msg-tool"><div class="msg-label">' + esc(t.name) + '</div><div class="msg-content">' + esc(detail) + '</div></div>';
      });
    }
  });
  el.innerHTML = html || '<div style="color:#555;text-align:center;padding:40px">No transcript entries</div>';
  // Only auto-scroll if user was already at the bottom
  if (wasAtBottom) el.scrollTop = el.scrollHeight;
}

// ── Actions ──

async function respond(id, decision, btn, message) {
  if (btn) {
    const card = btn.closest('.perm-card, .session-card, .sc-actions');
    if (card) card.querySelectorAll('button').forEach(b => b.disabled = true);
    btn.textContent = '...';
  }
  try {
    const body = {id, decision};
    if (message) body.message = message;
    await fetch('/api/respond', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    respondedIds.add(id);
  } catch (e) {
    if (btn) btn.textContent = 'Error';
  }
}

async function respondAlwaysAll(reqId, btn) {
  const card = btn.closest('.perm-card');
  if (card) card.querySelectorAll('button').forEach(b => b.disabled = true);
  btn.textContent = '...';
  // Get patterns from the session data
  try {
    const res = await fetch('/api/pending');
    const data = await res.json();
    const req = (data.requests || []).find(r => r.id === reqId);
    const patterns = (req && req.allow_patterns) || [];
    await fetch('/api/respond', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: reqId, decision: 'always', allow_patterns: patterns})
    });
    respondedIds.add(reqId);
  } catch (e) {
    btn.textContent = 'Error';
  }
}

async function respondSessionAllow(id, sessionId, toolName, btn) {
  const card = btn.closest('.perm-card');
  if (card) card.querySelectorAll('button').forEach(b => b.disabled = true);
  btn.textContent = '...';
  try {
    await fetch('/api/session-allow', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id, session_id: sessionId, tool_name: toolName})
    });
    respondedIds.add(id);
  } catch (e) {
    btn.textContent = 'Error';
  }
}

function toggleFeedback(id) {
  const area = document.getElementById('feedback-area-' + id);
  const buttons = document.getElementById('plan-buttons-' + id);
  if (area.style.display === 'none') {
    area.style.display = 'block';
    buttons.style.display = 'none';
    document.getElementById('feedback-input-' + id).focus();
  } else {
    area.style.display = 'none';
    buttons.style.display = 'flex';
  }
}

async function submitFeedback(id) {
  const input = document.getElementById('feedback-input-' + id);
  const feedback = input.value.trim();
  if (!feedback) { input.focus(); return; }
  const card = document.getElementById('perm-' + id);
  if (card) card.querySelectorAll('button, textarea').forEach(el => el.disabled = true);
  try {
    await fetch('/api/respond', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id, decision: 'deny', message: feedback})
    });
    respondedIds.add(id);
  } catch (e) {}
}

function togglePathSelect(reqId) {
  const area = document.getElementById('path-area-' + reqId);
  if (!area) return;
  if (area.style.display !== 'none') { area.style.display = 'none'; return; }
  // Fetch request data to build path options
  fetch('/api/pending').then(r => r.json()).then(data => {
    const req = (data.requests || []).find(r => r.id === reqId);
    if (!req) return;
    const filePath = (req.tool_input && req.tool_input.file_path) || '';
    const projectDir = req.project_dir || '';
    const toolName = req.tool_name || 'Write';
    if (!filePath) return;
    const rel = projectDir && filePath.startsWith(projectDir) ? filePath.slice(projectDir.length).replace(/^\\//, '') : filePath;
    const parts = rel.split('/').filter(Boolean);
    const projectName = projectDir.split('/').filter(Boolean).pop() || '';
    let html = '';
    const rootPattern = toolName + '(' + projectDir + '/*)';
    html += '<div class="path-option" onclick="submitPathAllow(\\'' + reqId + '\\',\\'' + esc(rootPattern) + '\\')">';
    html += '<div class="path-label">' + esc(projectName + '/*') + '</div>';
    html += '<div class="path-pattern">' + esc(rootPattern) + '</div></div>';
    let cumPath = projectDir;
    for (let i = 0; i < parts.length - 1; i++) {
      cumPath += '/' + parts[i];
      const displayPath = projectName + '/' + parts.slice(0, i + 1).join('/') + '/*';
      const pattern = toolName + '(' + cumPath + '/*)';
      html += '<div class="path-option" onclick="submitPathAllow(\\'' + reqId + '\\',\\'' + esc(pattern) + '\\')">';
      html += '<div class="path-label">' + esc(displayPath) + '</div>';
      html += '<div class="path-pattern">' + esc(pattern) + '</div></div>';
    }
    area.innerHTML = html;
    area.style.display = 'block';
  });
}

function toggleSplitPatterns(reqId) {
  const area = document.getElementById('split-area-' + reqId);
  if (!area) return;
  if (area.style.display !== 'none') { area.style.display = 'none'; return; }
  fetch('/api/pending').then(r => r.json()).then(data => {
    const req = (data.requests || []).find(r => r.id === reqId);
    if (!req || !req.allow_patterns) return;
    let html = '';
    req.allow_patterns.forEach(pat => {
      html += '<div class="path-option" onclick="submitPathAllow(\\'' + reqId + '\\',\\'' + esc(pat) + '\\')">';
      html += '<div class="path-label">Allow: <code>' + esc(pat) + '</code></div></div>';
    });
    area.innerHTML = html;
    area.style.display = 'block';
  });
}

async function submitPathAllow(reqId, pattern) {
  try {
    await fetch('/api/respond', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: reqId, decision: 'always', allow_pattern: pattern})
    });
    respondedIds.add(reqId);
  } catch (e) {}
}

// ── Prompt ──

async function sendPrompt() {
  if (!currentSessionId) return;
  const input = document.getElementById('promptInput');
  let prompt = input.value.trim();
  const images = imagePaths.slice();
  if (!prompt && images.length === 0) { input.focus(); return; }
  if (images.length > 0) {
    const refs = images.map(p => 'Please look at this image: ' + p).join('\\n');
    prompt = refs + (prompt ? '\\n\\n' + prompt : '');
  }
  input.value = '';
  imagePaths = [];
  document.getElementById('imagePreview').innerHTML = '';
  try {
    await fetch('/api/send-prompt', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: currentSessionId, prompt})
    });
  } catch (e) {
    console.error('Failed to send prompt:', e);
  }
}

async function quickPrompt(prompt) {
  if (!currentSessionId) return;
  try {
    await fetch('/api/send-prompt', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: currentSessionId, prompt})
    });
  } catch (e) {}
}

// ── Image upload ──

async function handleImageFile(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  const formData = new FormData();
  formData.append('image', file);
  try {
    const resp = await fetch('/api/upload-image', { method: 'POST', body: formData });
    const data = await resp.json();
    if (data.path) {
      imagePaths.push(data.path);
      renderImagePreviews();
    }
  } catch (e) {}
  input.value = '';
}

function renderImagePreviews() {
  const area = document.getElementById('imagePreview');
  area.innerHTML = imagePaths.map((p, i) =>
    '<div class="image-thumb">' +
    '<img src="/api/image?path=' + encodeURIComponent(p) + '">' +
    '<button class="remove-btn" onclick="imagePaths.splice(' + i + ',1);renderImagePreviews()">x</button>' +
    '</div>'
  ).join('');
}

// Ctrl+Enter to send
document.getElementById('promptInput').addEventListener('keydown', function(e) {
  if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
    e.preventDefault();
    sendPrompt();
  }
});

// Paste images
document.getElementById('promptInput').addEventListener('paste', function(e) {
  const items = e.clipboardData && e.clipboardData.items;
  if (!items) return;
  for (const item of items) {
    if (item.type.startsWith('image/')) {
      e.preventDefault();
      const file = item.getAsFile();
      const formData = new FormData();
      formData.append('image', file);
      fetch('/api/upload-image', { method: 'POST', body: formData })
        .then(r => r.json())
        .then(data => { if (data.path) { imagePaths.push(data.path); renderImagePreviews(); } });
      return;
    }
  }
});

// ── Polling ──
fetchSessions();
pollTimer = setInterval(() => {
  if (currentView === 'dashboard') fetchSessions();
}, 2000);
</script>
</body>
</html>"""


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
            self._respond_json({"sessions": result})

        elif path.startswith("/api/session/") and path.endswith("/transcript"):
            # /api/session/<id>/transcript?limit=50&after=0
            parts = path.split("/")
            sid = parts[3] if len(parts) > 3 else ""
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
                            os.kill(int(pid), 0)
                        except (OSError, ProcessLookupError, ValueError):
                            os.remove(fpath)
                            continue
                    requests.append(data)
                except (json.JSONDecodeError, IOError):
                    continue
            self._respond_json({"requests": requests})

        elif path.startswith("/api/image"):
            params = parse_qs(parsed.query)
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
            cwd = body.get("cwd", "")

            with sessions_lock:
                # Evict other sessions on the same tmux pane
                if tmux_pane:
                    evict = [k for k, v in sessions.items() if k != sid and v.get("tmux_pane") == tmux_pane]
                    for k in evict:
                        del sessions[k]
                        keys_to_remove = [ak for ak in session_auto_allow if ak[0] == k]
                        for ak in keys_to_remove:
                            del session_auto_allow[ak]
                    if evict:
                        print(f"[~] Evicted session(s) on pane {tmux_pane}: {evict}")

                if source == "startup" or sid not in sessions:
                    sessions[sid] = {
                        "transcript_path": transcript_path,
                        "tmux_pane": tmux_pane,
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
                                try:
                                    os.remove(fpath)
                                except OSError:
                                    pass
                                try:
                                    os.remove(resp_path)
                                except OSError:
                                    pass
                        except (json.JSONDecodeError, IOError):
                            continue

            print(f"[*] Session registered: {sid} source={source} pane={tmux_pane}")
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

            with sessions_lock:
                s = sessions.get(sid)
            if not s:
                self.send_error(404, "Session not found")
                return

            if tmux_send_prompt(s, prompt):
                print(f"[>] Prompt sent via tmux to session {sid}: {prompt[:80]}")
                self._respond_json({"ok": True})
            else:
                self.send_error(500, "Failed to send prompt via tmux")

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
                try:
                    with open(f"/proc/{child_pid}/comm") as f:
                        comm = f.read().strip()
                    if comm not in ("claude", "node"):
                        continue
                    with open(f"/proc/{child_pid}/cmdline") as f:
                        cmdline = f.read()
                    if "claude" not in cmdline:
                        continue
                except (FileNotFoundError, PermissionError):
                    continue

                # Get cwd
                try:
                    cwd = os.readlink(f"/proc/{child_pid}/cwd")
                except (FileNotFoundError, PermissionError):
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

    server = HTTPServer(("0.0.0.0", PORT), WebUIHandler)
    print(f"Claude Code WebUI Server running on http://localhost:{PORT}")
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
