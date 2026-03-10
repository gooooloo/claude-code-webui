#!/usr/bin/env python3
"""
PermissionRequest hook for Claude Code WebUI.

Called before Claude executes a tool that requires permission.
All auto-allow logic is centralized here; the server only stores session state.

Auto-allow tiers (checked in order, first match wins):
  1. Persistent rules   — glob patterns in .claude/settings.local.json (survive restarts)
  2. Smart rules         — read-only tools, read-only Bash, project-internal file edits
  3. Tmux allowlist      — tmux commands used by WebUI prompt delivery
  4. Session rules       — per-session per-tool rules stored in server memory
                           (set by user clicking "Allow for session" in the UI,
                           cleared on session end/clear; queried via server API
                           because the hook is a short-lived process with no memory)
  5. Server offline      — if the server is unreachable, auto-allow everything
                           so Claude Code keeps working without the WebUI

If none of the above match, the hook writes a .request.json and polls for a
.response.json written by the server when the user decides in the Web UI.

Input:  JSON on stdin with { tool_name, tool_input }
Output: JSON on stdout with { hookSpecificOutput: { decision: { behavior: "allow"|"deny" } } }
"""

import atexit
import fnmatch
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import uuid

from platform_utils import get_queue_dir, find_claude_pid

QUEUE_DIR = get_queue_dir()
SERVER = "http://127.0.0.1:19836"
TIMEOUT = 86400  # 24 hours


def allow_response():
    """Output an allow decision and exit."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"}
        }
    }))
    sys.exit(0)


def deny_response(message="User denied via web UI"):
    """Output a deny decision and exit."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "deny", "message": message}
        }
    }))
    sys.exit(0)


def build_detail(tool_name, tool_input):
    """Build detail text, detail_sub, allow_pattern, and allow_patterns per tool type."""
    detail = ""
    detail_sub = ""
    allow_pattern = tool_name
    allow_patterns = []

    if tool_name in ("Bash", "mcp__acp__Bash"):
        command = tool_input.get("command", "")
        detail = command
        detail_sub = ""
        # Parse compound commands into individual allow patterns
        # Split on | and && to get individual commands
        parts = re.split(r'\||\&\&', command)
        patterns = []
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
            # Find first non-flag argument as subcommand
            sub = ""
            for t in tokens[1:]:
                if not t.startswith(("-", "/", ".")):
                    sub = t
                    break
            if sub:
                pat = f"Bash({base} {sub}:*)"
            else:
                pat = f"Bash({base}:*)"
            if pat not in patterns:
                patterns.append(pat)
        allow_patterns = patterns
        allow_pattern = patterns[0] if patterns else f"Bash({command})"

    elif tool_name in ("Write", "mcp__acp__Write"):
        file_path = tool_input.get("file_path", "")
        detail = file_path
        allow_pattern = f"Write({file_path})"

    elif tool_name in ("Edit", "mcp__acp__Edit"):
        file_path = tool_input.get("file_path", "")
        old_string = tool_input.get("old_string", "")
        detail = file_path
        detail_sub = "\n".join(old_string.split("\n")[:5]) if old_string else ""
        allow_pattern = f"Edit({file_path})"

    elif tool_name == "ExitPlanMode":
        plan = tool_input.get("plan", "")
        detail = plan if plan else "Exit plan mode"
        # allowedPrompts as subtitle
        allowed = tool_input.get("allowedPrompts", [])
        if allowed:
            parts = [f"{p.get('tool', '?')}: {p.get('prompt', '?')}" for p in allowed]
            detail_sub = "Requested permissions: " + ", ".join(parts)
        allow_pattern = "ExitPlanMode"

    elif tool_name == "AskUserQuestion":
        questions = tool_input.get("questions", [])
        if questions:
            lines = []
            for q in questions:
                lines.append(f"Q: {q.get('question', '')}")
                for opt in q.get("options", []):
                    lines.append(f"  - {opt.get('label', '')} — {opt.get('description', '')}")
            detail = "\n".join(lines)
        else:
            # Fallback
            detail = json.dumps(tool_input, indent=2)[:500]
        allow_pattern = "AskUserQuestion"

    elif tool_name == "WebFetch":
        detail = tool_input.get("url", "")
        detail_sub = tool_input.get("prompt", "")
        allow_pattern = "WebFetch"

    elif tool_name == "WebSearch":
        detail = tool_input.get("query", "")
        allow_pattern = "WebSearch"

    else:
        # Generic: dump tool_input
        items = [f"{k}: {v}" for k, v in list(tool_input.items())[:10]]
        detail = "\n".join(items)
        allow_pattern = tool_name

    return detail, detail_sub, allow_pattern, allow_patterns


def _match_allow_pattern(tool_name, detail, pattern):
    """Check if a single detail string matches an allow pattern."""
    if not pattern:
        return False
    # Exact tool name match (e.g., "Read", "WebSearch", "Bash")
    if pattern == tool_name:
        return True
    # Check ToolName(glob) pattern
    prefix = f"{tool_name}("
    if pattern.startswith(prefix) and pattern.endswith(")"):
        inner = pattern[len(prefix):-1]
        # Convert ":*" suffix to just "*" for fnmatch
        glob_inner = inner.replace(":*", "*")
        if fnmatch.fnmatch(detail, glob_inner):
            return True
    return False


def _check_single_command(tool_name, command_str, allow_list):
    """Check if a single (non-compound) command matches any allow pattern."""
    command_str = command_str.strip()
    if not command_str:
        return True
    # Build detail for this single command (first_line as detail)
    first_line = command_str.split("\n")[0].strip()
    for pattern in allow_list:
        if _match_allow_pattern(tool_name, first_line, pattern):
            return True
        # Also try matching with just the base command + subcommand
        tokens = first_line.split()
        if tokens:
            base = os.path.basename(tokens[0])
            if base:
                sub = ""
                for t in tokens[1:]:
                    if not t.startswith(("-", "/", ".")):
                        sub = t
                        break
                # Try "base sub ..." and "base ..."
                detail_with_sub = f"{base} {sub}" if sub else base
                if _match_allow_pattern(tool_name, detail_with_sub, pattern):
                    return True
                if _match_allow_pattern(tool_name, base, pattern):
                    return True
    return False


def check_auto_allow(tool_name, detail, settings_file):
    """Check if this tool call matches any pre-approved pattern in settings.local.json."""
    if not os.path.isfile(settings_file):
        return False
    try:
        with open(settings_file) as f:
            settings = json.load(f)
    except (json.JSONDecodeError, IOError):
        return False

    allow_list = settings.get("permissions", {}).get("allow", [])
    if not allow_list:
        return False

    # For Bash commands, split compound commands and check each part
    if tool_name in ("Bash", "mcp__acp__Bash"):
        parts = re.split(r'\||\&\&|;', detail)
        non_empty = [p for p in parts if p.strip()]
        if non_empty and all(
            _check_single_command(tool_name, p, allow_list)
            for p in non_empty
        ):
            return True
        return False

    # Non-Bash tools: direct match
    for pattern in allow_list:
        if _match_allow_pattern(tool_name, detail, pattern):
            return True
    return False


# ── Smart auto-approve ──

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

READONLY_GIT_SUBCOMMANDS = {
    "log", "diff", "status", "show", "branch", "tag", "remote", "stash",
    "blame", "shortlog", "describe", "rev-parse", "rev-list", "ls-files",
    "ls-tree", "cat-file", "config",
}

DANGEROUS_COMMANDS = {
    "rm", "rmdir", "mv", "chmod", "chown", "chgrp", "mkfs", "dd",
    "shutdown", "reboot", "kill", "killall", "pkill",
    "curl", "wget",  # network access
    "ssh", "scp", "rsync",  # remote access
    "sudo", "su", "doas",  # privilege escalation
}

READONLY_TOOLS = {"Read", "Glob", "Grep", "mcp__acp__Read", "mcp__acp__Glob", "mcp__acp__Grep"}


def _is_readonly_bash(command):
    """Check if a Bash command (possibly compound) is read-only."""
    parts = re.split(r'\||&&|\|\||;', command)
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
        if base in DANGEROUS_COMMANDS:
            return False
        if base == "sed":
            if "-i" in tokens or any(t.startswith("-i") for t in tokens[1:]):
                return False
            continue
        if base in ("awk", "gawk", "mawk", "nawk"):
            continue
        if base == "git":
            sub = ""
            for t in tokens[1:]:
                if not t.startswith("-"):
                    sub = t
                    break
            if sub not in READONLY_GIT_SUBCOMMANDS:
                return False
            continue
        if base in READONLY_COMMANDS:
            continue
        return False
    return True


def _is_project_file(file_path, project_dir):
    """Check if a file path is within the project directory."""
    if not file_path or not project_dir:
        return False
    try:
        real_file = os.path.realpath(file_path)
        real_cwd = os.path.realpath(project_dir)
        return real_file.startswith(real_cwd + os.sep) or real_file == real_cwd
    except (ValueError, OSError):
        return False


def check_smart_auto_approve(tool_name, tool_input, project_dir):
    """Check if a tool call should be auto-approved by smart rules."""
    if tool_name in READONLY_TOOLS:
        return True
    if tool_name in ("Bash", "mcp__acp__Bash"):
        command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
        if command and _is_readonly_bash(command):
            return True
    if tool_name in ("Write", "Edit", "mcp__acp__Write", "mcp__acp__Edit"):
        file_path = tool_input.get("file_path", "") if isinstance(tool_input, dict) else ""
        if _is_project_file(file_path, project_dir):
            return True
    return False


def main():
    os.makedirs(QUEUE_DIR, exist_ok=True)

    # Read input
    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        input_data = {}

    tool_name = input_data.get("tool_name", "Unknown")
    tool_input = input_data.get("tool_input", {})

    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except (json.JSONDecodeError, ValueError):
            tool_input = {}

    project_dir = os.getcwd()
    settings_file = os.path.join(project_dir, ".claude", "settings.local.json")
    session_id = input_data.get("session_id", "") or str(find_claude_pid())

    # Build detail and patterns
    detail, detail_sub, allow_pattern, allow_patterns = build_detail(tool_name, tool_input)

    # ── Auto-allow tiers (first match wins) ──

    # Tier 1: Persistent rules (settings.local.json glob patterns)
    if check_auto_allow(tool_name, detail, settings_file):
        allow_response()

    # Tier 2: Smart rules (read-only tools, read-only bash, project-internal edits)
    if check_smart_auto_approve(tool_name, tool_input, project_dir):
        allow_response()

    # Tier 3: Tmux allowlist (WebUI uses tmux for prompt delivery)
    if tool_name in ("Bash", "mcp__acp__Bash"):
        command = tool_input.get("command", "").strip()
        first_token = command.split()[0] if command.split() else ""
        if os.path.basename(first_token) == "tmux":
            allow_response()

    # Tier 4: Session rules (per-session per-tool, stored in server memory)
    # Queried via API because this hook is a short-lived process with no memory.
    # This call also doubles as the server-online check (tier 5).
    try:
        req = urllib.request.Request(
            f"{SERVER}/api/check-auto-allow?session_id={urllib.parse.quote(str(session_id))}"
            f"&tool_name={urllib.parse.quote(tool_name)}")
        resp = urllib.request.urlopen(req, timeout=2)
        data = json.loads(resp.read())
        if data.get("auto_allow"):
            allow_response()
    except Exception:
        # Tier 5: Server offline — allow everything so Claude keeps working
        allow_response()

    # Generate request ID
    try:
        request_id = str(uuid.uuid4())
    except Exception:
        request_id = str(int(time.time() * 1e9))

    request_file = os.path.join(QUEUE_DIR, f"{request_id}.request.json")
    response_file = os.path.join(QUEUE_DIR, f"{request_id}.response.json")

    # Clean up request file on exit
    def cleanup():
        try:
            os.remove(request_file)
        except OSError:
            pass
    atexit.register(cleanup)

    # Write request
    request_data = {
        "id": request_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "detail": detail,
        "detail_sub": detail_sub,
        "allow_pattern": allow_pattern,
        "allow_patterns": allow_patterns if allow_patterns else [],
        "settings_file": settings_file,
        "timestamp": int(time.time()),
        "pid": os.getpid(),
        "session_id": session_id,
        "project_dir": project_dir,
    }
    # Write atomically via temp file + os.replace to prevent the server
    # from reading a half-written file.  os.replace works on both POSIX
    # and Windows (unlike os.rename which fails on Windows if dest exists).
    tmp_file = request_file + ".tmp"
    with open(tmp_file, "w") as f:
        json.dump(request_data, f)
    os.replace(tmp_file, request_file)

    # Poll for response
    elapsed = 0
    while elapsed < TIMEOUT:
        if os.path.isfile(response_file):
            try:
                with open(response_file) as f:
                    resp = json.load(f)
            except (json.JSONDecodeError, IOError):
                time.sleep(0.5)
                elapsed += 1
                continue

            decision = resp.get("decision", "deny")
            message = resp.get("message", "User denied via web UI")

            # Cleanup
            try:
                os.remove(request_file)
            except OSError:
                pass
            try:
                os.remove(response_file)
            except OSError:
                pass
            # Unregister atexit since we cleaned up manually
            atexit.unregister(cleanup)

            if decision in ("allow", "always"):
                allow_response()
            else:
                deny_response(message)

        time.sleep(0.5)
        elapsed += 1

    # Timeout
    try:
        os.remove(request_file)
    except OSError:
        pass
    atexit.unregister(cleanup)
    deny_response("Approval timed out")


if __name__ == "__main__":
    main()
