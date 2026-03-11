#!/usr/bin/env python3
"""
SessionStart hook for Claude Code WebUI (new architecture).

Called on session lifecycle events: startup, resume, /clear, /compact.
Registers this session with the server, passing transcript path, tmux info, and cwd.

Input:  JSON on stdin with { source: "startup"|"resume"|"clear"|"compact" }
Output: (none)
"""

import json
import os
import sys
import glob
import urllib.request

from platform_utils import get_queue_dir, find_claude_pid, IS_WINDOWS, encode_project_path

SERVER = "http://127.0.0.1:19836"
QUEUE_DIR = get_queue_dir()


def find_transcript_path():
    """Find the most recently modified transcript JSONL for this project."""
    project_dir = os.getcwd()
    # Claude Code encodes project path: /home/user/project -> -home-user-project
    encoded = encode_project_path(project_dir)
    projects_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects", encoded)
    if not os.path.isdir(projects_dir):
        return ""
    jsonl_files = glob.glob(os.path.join(projects_dir, "*.jsonl"))
    if not jsonl_files:
        return ""
    # Return the most recently modified
    return max(jsonl_files, key=os.path.getmtime)


def main():
    # On Windows, Ctrl-C sends CTRL_C_EVENT to ALL processes in the console,
    # including this hook subprocess.  Ignore it so we survive long enough to
    # complete the registration POST.
    import signal
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, signal.SIG_IGN)

    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        input_data = {}

    source = input_data.get("source", "unknown")
    session_id = input_data.get("session_id", "") or str(find_claude_pid())
    project_dir = os.getcwd()

    # Debug log
    import datetime
    os.makedirs(QUEUE_DIR, exist_ok=True)
    with open(os.path.join(QUEUE_DIR, "session-start-debug.log"), "a") as f:
        f.write(f"{datetime.datetime.now()} ppid={os.getppid()} session_id={session_id} source={source} input={json.dumps(input_data)}\n")
    transcript_path = input_data.get("transcript_path", "") or find_transcript_path()

    body_dict = {
        "session_id": session_id,
        "source": source,
        "transcript_path": transcript_path,
        "cwd": project_dir,
    }

    if IS_WINDOWS:
        # On Windows there's no tmux; pass the console host PID instead
        body_dict["console_pid"] = find_claude_pid()
    else:
        body_dict["tmux_pane"] = os.environ.get("TMUX_PANE", "")
        body_dict["tmux_socket"] = os.environ.get("TMUX", "")

    body = json.dumps(body_dict).encode()

    try:
        req = urllib.request.Request(
            f"{SERVER}/api/session/register",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass  # Fire-and-forget; server may be offline


if __name__ == "__main__":
    main()
