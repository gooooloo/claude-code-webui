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
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
import uuid
import cgi

from frontend import HTML_PAGE
from platform_utils import IS_WINDOWS, get_queue_dir, get_image_dir, is_process_alive, find_claude_pid, get_process_children, get_process_name, encode_project_path, send_prompt

try:
    from channel_feishu import start_feishu_channel
    _has_feishu = True
except ImportError:
    _has_feishu = False


QUEUE_DIR = get_queue_dir()
IMAGE_DIR = get_image_dir()
PORT = 19836
server_name = "local"
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
# These are volatile (in-memory only, cleared on session end/clear/server restart).
# The hook queries these via GET /api/check-auto-allow.
# For persistent rules, see settings.local.json (checked directly by the hook).
session_auto_allow = {}


# ── Transcript parsing ──

def update_session_state(sid):
    """Read new transcript entries incrementally and derive session state.

    THREADING NOTE: The read-offset → read-file → write-offset sequence below
    is safe only because HTTPServer (without ThreadingMixIn) serialises all HTTP
    requests on a single thread.  If the server is ever made multi-threaded,
    this needs a per-session lock or compare-and-swap on the offset.
    """
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
            # Defend against /compact and /clear race: if the file was rewritten
            # shorter than our offset, reset to 0 (the hook registration will
            # also reset, but this closes the race window before it arrives).
            f.seek(0, 2)  # seek to end
            file_size = f.tell()
            if offset > file_size:
                offset = 0
                with sessions_lock:
                    s = sessions.get(sid)
                    if s:
                        s["transcript_offset"] = 0
                        s["transcript_entries"] = []
            f.seek(offset)
            new_data = f.read()
    except IOError:
        # On Windows, mandatory file locking can cause IOError when Claude Code
        # is writing. Skip this poll cycle; next poll will retry. Do NOT sleep
        # here — it blocks the single-threaded HTTP server.
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
            # Last tool_use is resolved — fall through to idle

        if stop_reason == "end_turn" or not tool_uses or _all_tool_uses_resolved(entries, tool_uses):
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
            if str(data.get("session_id", "")) != str(sid):
                continue
            # Check if the hook process that wrote this request is still alive.
            # If it's dead (SIGKILL, OOM, etc.), atexit cleanup didn't fire and
            # the file is orphaned — no process is waiting for the response.
            hook_pid = data.get("pid")
            if hook_pid:
                try:
                    if not is_process_alive(int(hook_pid)):
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                        continue
                except (ValueError, TypeError):
                    pass
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


def _all_tool_uses_resolved(entries, tool_uses):
    """Check if all tool_use blocks have matching tool_results."""
    if not tool_uses:
        return True
    return all(_has_tool_result(entries, tu.get("id", "")) for tu in tool_uses)


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
        # No console_pid (auto-discovered session) — check transcript mtime
        transcript_path = session_data.get("transcript_path", "")
        if transcript_path:
            try:
                mtime = os.path.getmtime(transcript_path)
                if time.time() - mtime < 7200:  # active within last 2 hours
                    return True
            except OSError:
                pass
        # Grace period for sessions still registering
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
                    # Determine if this session can receive prompts
                    if IS_WINDOWS:
                        can_prompt = bool(s.get("console_pid"))
                    else:
                        can_prompt = bool(s.get("tmux_pane"))
                    entry = {
                        "session_id": sid,
                        "cwd": s["cwd"],
                        "state": s["derived_state"],
                        "last_summary": s["last_summary"],
                        "last_user_prompt": s["last_user_prompt"],
                        "last_activity": s["last_activity"],
                        "registered_at": s["registered_at"],
                        "prompt_capable": can_prompt,
                    }
                    # Attach pending request if in permission_prompt state
                    if s["derived_state"] == "permission_prompt":
                        pr = _find_pending_request(sid)
                        if pr:
                            entry["pending_request"] = pr
                    result.append(entry)

            self._respond_json({"sessions": result, "name": server_name})

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

        elif path == "/api/check-auto-allow":
            params = parse_qs(parsed.query)
            sid = params.get("session_id", [""])[0]
            tname = params.get("tool_name", [""])[0]
            result = (sid, tname) in session_auto_allow if sid and tname else False
            self._respond_json({"auto_allow": result})

        elif path == "/api/pending":
            # Legacy endpoint — scan .request.json files
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
            console_pid = body.get("console_pid", "")
            cwd = body.get("cwd", "")

            with sessions_lock:
                # Evict other sessions on the same tmux pane (new session_id on same pane)
                if source in ("startup", "resume", "clear") and tmux_pane:
                    evict = [k for k, v in sessions.items() if k != sid and v.get("tmux_pane") == tmux_pane]
                    for k in evict:
                        del sessions[k]
                        keys_to_remove = [ak for ak in session_auto_allow if ak[0] == k]
                        for ak in keys_to_remove:
                            del session_auto_allow[ak]
                    if evict:
                        print(f"[~] Evicted session(s) on pane {tmux_pane}: {evict}")

                # Evict other sessions on the same Windows console_pid (new session_id on same pane)
                if source in ("startup", "resume", "clear") and console_pid:
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

            # Check session state before delivery to avoid pasting into busy terminal
            update_session_state(sid)
            with sessions_lock:
                state = sessions[sid]["derived_state"] if sid in sessions else "unknown"
            if state not in ("idle", "elicitation", "plan_review"):
                self.send_error(409, f"Session is {state}, not idle")
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
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
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


def _scan_sessions_from_transcripts():
    """Discover sessions by scanning ~/.claude/projects/ for recent transcript files.

    Used on Windows where tmux is not available. Registers sessions as read-only
    (no console_pid for prompt delivery) until the next hook fires.
    """
    home = os.path.expanduser("~")
    projects_dir = os.path.join(home, ".claude", "projects")
    if not os.path.isdir(projects_dir):
        return

    now = time.time()
    for proj in os.listdir(projects_dir):
        proj_path = os.path.join(projects_dir, proj)
        if not os.path.isdir(proj_path):
            continue
        for jsonl_file in glob.glob(os.path.join(proj_path, "*.jsonl")):
            try:
                mtime = os.path.getmtime(jsonl_file)
            except OSError:
                continue
            # Only consider transcripts modified in last 2 hours
            if now - mtime > 7200:
                continue

            session_id = os.path.splitext(os.path.basename(jsonl_file))[0]
            with sessions_lock:
                if session_id in sessions:
                    continue
                sessions[session_id] = {
                    "transcript_path": jsonl_file,
                    "tmux_pane": "",
                    "tmux_socket": "",
                    "console_pid": "",
                    "cwd": proj_path,
                    "registered_at": now,
                    "transcript_offset": 0,
                    "transcript_entries": [],
                    "derived_state": "busy",
                    "last_activity": now,
                    "last_summary": "",
                    "last_user_prompt": "",
                }
            print(f"[*] Auto-discovered session: {session_id} project={proj}")


def scan_existing_sessions():
    """Scan tmux panes for running claude processes and register them."""
    if IS_WINDOWS:
        _scan_sessions_from_transcripts()
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
    parser.add_argument("--name", default="local", help="Display name for this machine in the page title (default: local)")
    parser.add_argument("--lan", action="store_true", help="Listen on 0.0.0.0 instead of 127.0.0.1 (allow LAN access)")
    args = parser.parse_args()

    global server_name
    server_name = args.name

    os.makedirs(QUEUE_DIR, exist_ok=True)

    # Scan for existing sessions before starting
    try:
        scan_existing_sessions()
    except Exception as e:
        print(f"[!] Session scan failed: {e}")

    # Background threads
    threading.Thread(target=zombie_cleanup_loop, daemon=True).start()

    if _has_feishu:
        try:
            start_feishu_channel()
        except Exception as e:
            print(f"[feishu] Failed to start: {e}")

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
