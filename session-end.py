#!/usr/bin/env python3
"""
SessionEnd hook for Claude Code WebUI (new architecture).

Called when a session terminates (user exits Claude Code).
Deregisters the session from the server and cleans up request files locally as fallback.

Input:  JSON on stdin (currently empty for SessionEnd)
Output: (none)
"""

import json
import os
import sys
import glob
import urllib.request

SERVER = "http://127.0.0.1:19836"
QUEUE_DIR = "/tmp/claude-webui"


def _find_claude_pid():
    """Walk up the process tree to find the 'claude' process PID."""
    pid = os.getppid()
    for _ in range(10):
        try:
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read().strip()
            if comm in ("claude", "node"):
                with open(f"/proc/{pid}/cmdline") as f:
                    cmdline = f.read()
                if "claude" in cmdline:
                    return pid
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("PPid:"):
                        pid = int(line.split()[1])
                        break
                else:
                    break
        except (FileNotFoundError, PermissionError, ValueError):
            break
    return os.getppid()


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        input_data = {}

    session_id = input_data.get("session_id", "") or str(_find_claude_pid())

    # Debug log
    import datetime
    os.makedirs(QUEUE_DIR, exist_ok=True)
    with open(os.path.join(QUEUE_DIR, "session-end-debug.log"), "a") as f:
        f.write(f"{datetime.datetime.now()} session_id={session_id} input={json.dumps(input_data)}\n")

    # Notify server (fire-and-forget)
    body = json.dumps({"session_id": session_id}).encode()
    try:
        req = urllib.request.Request(
            f"{SERVER}/api/session/deregister",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass

    # Local fallback cleanup: delete request/response files belonging to this session
    if os.path.isdir(QUEUE_DIR):
        for f in glob.glob(os.path.join(QUEUE_DIR, "*.request.json")):
            try:
                with open(f) as fh:
                    data = json.load(fh)
                if str(data.get("session_id", "")) == session_id:
                    resp = f.replace(".request.json", ".response.json")
                    try:
                        os.remove(f)
                    except OSError:
                        pass
                    try:
                        os.remove(resp)
                    except OSError:
                        pass
            except (json.JSONDecodeError, IOError):
                continue


if __name__ == "__main__":
    main()
