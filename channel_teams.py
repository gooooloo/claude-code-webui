#!/usr/bin/env python3
"""
Microsoft Teams notification channel for Claude Code WebUI.

Each Claude Code session maps to a reply thread in a Teams channel. A root
message is sent to create the thread when a session starts; all subsequent
messages (transcript entries, permission requests, state changes) are appended
as replies within that thread. Threads are preserved after session end.

Two modes:
  - webhook:   Send-only via Incoming Webhook URL (Adaptive Cards). No replies.
  - graph:     Full bidirectional via Microsoft Graph API (OAuth2 client_credentials).

No third-party dependencies — uses only stdlib (urllib, json, threading).
Config: config.json (see README)
"""

import errno
import json
import glob
import os
import re
import sys
import threading
import time
import urllib.request
import urllib.error
import urllib.parse

from platform_utils import get_queue_dir

QUEUE_DIR = get_queue_dir()
_SERVER_BASE = "http://127.0.0.1:19836"
_SAFE_ID_RE = re.compile(r'^[a-zA-Z0-9_-]+$')
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_THREADS_FILE = os.path.join(_DATA_DIR, "teams_threads.json")

# Rate limiting
_MAX_MESSAGES_PER_SCAN = 5
_MESSAGE_DELAY = 0.3

# Graph API endpoints
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_LOGIN_BASE = "https://login.microsoftonline.com"


def _is_safe_id(request_id):
    """Validate request_id contains only safe characters (no path traversal)."""
    return bool(request_id) and bool(_SAFE_ID_RE.match(request_id))


def _server_post(path, body):
    """POST JSON to the local WebUI server. Returns True on success."""
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{_SERVER_BASE}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[teams] Server API {path} failed: {e}", file=sys.stderr)
        return False


def _server_get(path):
    """GET JSON from the local WebUI server. Returns parsed dict or None."""
    try:
        req = urllib.request.Request(f"{_SERVER_BASE}{path}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[teams] Server API GET {path} failed: {e}", file=sys.stderr)
        return None


def _write_exclusive(filepath, data):
    """Atomically create a file only if it doesn't exist (O_CREAT|O_EXCL)."""
    try:
        fd = os.open(filepath, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        return True
    except OSError as e:
        if e.errno == errno.EEXIST:
            return False
        raise


# ── State ──

_mode = "webhook"  # "webhook" or "graph"
_webhook_url = None

# Graph API state
_tenant_id = None
_client_id = None
_client_secret = None
_team_id = None
_channel_id = None

# OAuth2 token cache
_access_token = None
_token_expires_at = 0.0
_token_lock = threading.Lock()

# session_id -> {
#   "root_message_id": str,     # thread root message ID
#   "sent_index": int,          # number of transcript entries already sent
#   "last_state": str,          # last known state (to detect changes)
#   "pending_request_ids": set, # permission request IDs in this thread
#   "topic_named": bool,        # whether thread subject has been set
#   "created_at": str,          # creation timestamp
#   "last_reply_etag": str,     # etag for polling replies (graph mode)
# }
_session_threads = {}

_notified_requests = set()  # permission request IDs already sent
_lock = threading.Lock()


def _load_threads():
    """Load persisted session threads from disk."""
    global _session_threads
    if not os.path.exists(_THREADS_FILE):
        return
    try:
        with open(_THREADS_FILE) as f:
            data = json.load(f)
        for sid, t in data.items():
            t["pending_request_ids"] = set(t.get("pending_request_ids", []))
            t.setdefault("topic_named", False)
            t.setdefault("last_reply_etag", "")
        _session_threads = data
        print(f"[teams] Restored {len(data)} session thread(s) from disk")
    except (json.JSONDecodeError, IOError) as e:
        print(f"[teams] Failed to load threads: {e}", file=sys.stderr)


def _save_threads():
    """Persist session threads to disk. Must be called with _lock held."""
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        serializable = {}
        for sid, t in _session_threads.items():
            serializable[sid] = {
                "root_message_id": t["root_message_id"],
                "sent_index": t["sent_index"],
                "last_state": t["last_state"],
                "pending_request_ids": list(t.get("pending_request_ids", set())),
                "topic_named": t.get("topic_named", False),
                "created_at": t.get("created_at", ""),
                "last_reply_etag": t.get("last_reply_etag", ""),
            }
        tmp = _THREADS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(serializable, f, indent=2)
        os.replace(tmp, _THREADS_FILE)
    except (IOError, OSError) as e:
        print(f"[teams] Failed to save threads: {e}", file=sys.stderr)


# ── Config ──

def _config_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config():
    """Load Teams config from config.json next to this file."""
    path = _config_path()
    if not os.path.exists(path):
        return None
    with open(path) as f:
        cfg = json.load(f)
    teams = cfg.get("teams", {})
    if not teams:
        return None
    mode = teams.get("mode", "webhook")
    if mode == "webhook":
        if not teams.get("webhook_url"):
            return None
    elif mode == "graph":
        required = ("tenant_id", "client_id", "client_secret", "team_id", "channel_id")
        if not all(teams.get(k) for k in required):
            return None
    else:
        return None
    return teams


# ── OAuth2 Token Management (Graph API mode) ──

def _get_access_token():
    """Get a valid access token, refreshing if needed. Thread-safe."""
    global _access_token, _token_expires_at

    with _token_lock:
        # Return cached token if still valid (with 60s buffer)
        if _access_token and time.time() < _token_expires_at - 60:
            return _access_token

        url = f"{_LOGIN_BASE}/{_tenant_id}/oauth2/v2.0/token"
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": _client_id,
            "client_secret": _client_secret,
            "scope": "https://graph.microsoft.com/.default",
        }).encode()

        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            _access_token = data["access_token"]
            _token_expires_at = time.time() + int(data.get("expires_in", 3600))
            print(f"[teams] OAuth2 token acquired, expires in {data.get('expires_in', '?')}s")
            return _access_token
        except Exception as e:
            print(f"[teams] Failed to acquire OAuth2 token: {e}", file=sys.stderr)
            return None


def _graph_request(method, path, body=None, extra_headers=None):
    """Make an authenticated request to Microsoft Graph API.

    Returns parsed JSON response or None on error.
    """
    token = _get_access_token()
    if not token:
        return None

    url = f"{_GRAPH_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)

    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp_body = resp.read()
            if resp_body:
                return json.loads(resp_body)
            return {}
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode()[:500]
        except Exception:
            pass
        print(f"[teams] Graph API {method} {path} failed: {e.code} {err_body}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[teams] Graph API {method} {path} error: {e}", file=sys.stderr)
        return None


# ── Truncation ──

def _truncate(text, max_len=2000):
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "\n... (truncated)"


# ── Adaptive Card Builders ──

_STATE_LABELS = {
    "idle": "Waiting for input",
    "busy": "Working...",
    "permission_prompt": "Needs approval",
    "elicitation": "Question",
    "plan_review": "Plan review",
}


def _build_permission_card(request_id, data):
    """Build an Adaptive Card for a permission request."""
    tool_name = data.get("tool_name", "Unknown")
    detail = data.get("detail", "")
    session_id = data.get("session_id", "")
    project_dir = data.get("project_dir", "")
    project_name = os.path.basename(project_dir) if project_dir else ""

    facts = [
        {"title": "Tool", "value": tool_name},
    ]
    if detail:
        facts.append({"title": "Detail", "value": _truncate(detail, 500)})
    if session_id:
        facts.append({"title": "Session", "value": session_id})
    if project_name:
        facts.append({"title": "Project", "value": project_name})

    body = [
        {
            "type": "TextBlock",
            "text": "Permission Request",
            "weight": "Bolder",
            "size": "Medium",
        },
        {
            "type": "FactSet",
            "facts": facts,
        },
        {
            "type": "TextBlock",
            "text": "Reply with: **allow**, **deny**, or **always**",
            "wrap": True,
            "color": "Attention",
        },
    ]

    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
    }


def _build_session_root_card(session, subject=None, created_at=None):
    """Build the root Adaptive Card for a new session thread."""
    project_dir = session.get("cwd", "")
    session_id = session.get("session_id", "")
    short_id = session_id[:8] if session_id else "?"
    project_name = os.path.basename(project_dir) if project_dir else "?"
    if not created_at:
        created_at = time.strftime("%Y-%m-%d %H:%M:%S")

    facts = [
        {"title": "Project", "value": project_name},
        {"title": "Directory", "value": project_dir or "?"},
        {"title": "Session", "value": session_id},
        {"title": "Created", "value": created_at},
    ]
    if subject:
        facts.append({"title": "Subject", "value": subject})

    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": f"Claude Code Session - {project_name} ({short_id})",
                "weight": "Bolder",
                "size": "Medium",
            },
            {
                "type": "FactSet",
                "facts": facts,
            },
        ],
    }


def _build_state_change_card(state, session_id):
    """Build a simple Adaptive Card for state change notifications."""
    label = _STATE_LABELS.get(state, state)
    color = "Good" if state == "idle" else "Accent" if state == "busy" else "Attention"
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {
                "type": "TextBlock",
                "text": label,
                "weight": "Bolder",
                "color": color,
            },
        ],
    }


# ── Transcript Entry Formatting ──

def _extract_tool_detail(tool_use):
    """Extract a human-readable detail string from a tool_use block."""
    name = tool_use.get("name", "")
    inp = tool_use.get("input", {})
    if not isinstance(inp, dict):
        inp = {}

    if name in ("Bash", "mcp__acp__Bash"):
        return inp.get("command", "")
    elif name in ("Write", "Edit", "mcp__acp__Write", "mcp__acp__Edit", "Read"):
        return inp.get("file_path", "")
    else:
        return json.dumps(inp, ensure_ascii=False)[:200]


def _format_transcript_entry(entry):
    """Convert a transcript entry to a list of text strings for posting.

    Returns a list because one assistant entry may produce multiple messages
    (text + tool_use blocks).
    """
    etype = entry.get("type", "")
    msg = entry.get("message", {})
    content = msg.get("content", "")
    results = []

    if etype == "user":
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict):
                    if c.get("type") == "text":
                        parts.append(c.get("text", ""))
                elif isinstance(c, str):
                    parts.append(c)
            text = " ".join(parts)

        # Strip system tags
        if text:
            text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL)
            text = re.sub(r"<[^>]+>", "", text)
            text = re.sub(r"[^\S\n]+", " ", text)
            text = text.strip()

        if text:
            results.append("[You] " + _truncate(text))

    elif etype == "assistant":
        if isinstance(content, str):
            if content.strip():
                results.append("[Claude] " + _truncate(content))
        elif isinstance(content, list):
            text_parts = []
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text":
                    t = c.get("text", "")
                    if t.strip():
                        text_parts.append(t)
                elif c.get("type") == "tool_use":
                    name = c.get("name", "unknown")
                    detail = _extract_tool_detail(c)
                    results.append(f"[Tool: {name}] {_truncate(detail, 500)}")

            if text_parts:
                combined = "\n".join(text_parts)
                results.insert(0, "[Claude] " + _truncate(combined))

    return results


def _extract_first_user_prompt(entries):
    """Extract the first user prompt text from transcript entries."""
    for entry in entries:
        if entry.get("type") != "user":
            continue
        content = entry.get("message", {}).get("content", "")
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
        if entry.get("isMeta") or "<command-name>" in text or "<local-command-caveat>" in text:
            continue
        if text:
            text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL)
            text = re.sub(r"<[^>]+>", "", text)
            text = re.sub(r"\s+", " ", text).strip()
        if text:
            return text
    return None


# ── Webhook Mode Helpers ──

def _webhook_send(card, summary="Claude Code Notification"):
    """Send an Adaptive Card via Incoming Webhook. Returns True on success."""
    payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": card,
            }
        ],
    }

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        _webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status in (200, 202)
    except Exception as e:
        print(f"[teams] Webhook send failed: {e}", file=sys.stderr)
        return False


def _webhook_send_text(text):
    """Send a plain text message via Incoming Webhook."""
    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {"type": "TextBlock", "text": text, "wrap": True},
        ],
    }
    return _webhook_send(card)


# ── Graph API Mode Helpers ──

def _graph_channel_path():
    """Return the Graph API path prefix for the configured team/channel."""
    return f"/teams/{_team_id}/channels/{_channel_id}"


def _graph_send_message(content_html, card=None):
    """Send a message to the Teams channel. Returns message ID or None.

    If card is provided, sends as an Adaptive Card attachment.
    Otherwise sends as HTML content.
    """
    path = f"{_graph_channel_path()}/messages"
    body = {}

    if card:
        body = {
            "body": {
                "contentType": "html",
                "content": content_html or "Notification",
            },
            "attachments": [
                {
                    "id": "card1",
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "contentUrl": None,
                    "content": json.dumps(card),
                }
            ],
        }
    else:
        body = {
            "body": {
                "contentType": "html",
                "content": content_html,
            }
        }

    result = _graph_request("POST", path, body)
    if result:
        return result.get("id")
    return None


def _graph_reply_to_message(parent_message_id, content_html, card=None):
    """Reply to a message in the Teams channel. Returns reply ID or None."""
    path = f"{_graph_channel_path()}/messages/{parent_message_id}/replies"
    body = {}

    if card:
        body = {
            "body": {
                "contentType": "html",
                "content": content_html or "Notification",
            },
            "attachments": [
                {
                    "id": "card1",
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "contentUrl": None,
                    "content": json.dumps(card),
                }
            ],
        }
    else:
        body = {
            "body": {
                "contentType": "html",
                "content": content_html,
            }
        }

    result = _graph_request("POST", path, body)
    if result:
        return result.get("id")
    return None


def _graph_get_replies(parent_message_id, top=20):
    """Get replies to a message. Returns list of reply dicts or empty list."""
    path = f"{_graph_channel_path()}/messages/{parent_message_id}/replies?$top={top}&$orderby=createdDateTime desc"
    result = _graph_request("GET", path)
    if result:
        return result.get("value", [])
    return []


# ── Unified Send Helpers (work in both modes) ──

def _send_card_to_channel(card, fallback_text="Claude Code Notification"):
    """Send an Adaptive Card as a new message/thread. Returns message ID or None."""
    if _mode == "webhook":
        _webhook_send(card, summary=fallback_text)
        return None  # Webhooks don't return message IDs
    else:
        return _graph_send_message(fallback_text, card=card)


def _reply_card(parent_message_id, card, fallback_text=""):
    """Reply with an Adaptive Card in a thread. Returns reply ID or None."""
    if _mode == "webhook":
        # Webhooks can't reply to threads, send as top-level
        _webhook_send(card)
        return None
    else:
        if not parent_message_id:
            return None
        return _graph_reply_to_message(parent_message_id, fallback_text, card=card)


def _reply_text(parent_message_id, text):
    """Reply with a text message in a thread."""
    if _mode == "webhook":
        _webhook_send_text(text)
    else:
        if not parent_message_id:
            return
        _graph_reply_to_message(parent_message_id, text)


# ── Settings helper (for "Always Allow") ──

def _add_to_settings(settings_file, pattern):
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
            print(f"[teams] Added to allowlist: {pattern}")
    except (json.JSONDecodeError, IOError) as e:
        print(f"[teams] Failed to update settings: {e}", file=sys.stderr)


# ── Topic/Thread Management ──

def _create_session_thread(session):
    """Create a new thread in the Teams channel for a session. Returns message_id or None."""
    card = _build_session_root_card(session)
    short_id = session.get("session_id", "?")[:8]
    project = os.path.basename(session.get("cwd", "")) or "?"
    mid = _send_card_to_channel(card, fallback_text=f"Claude Code Session - {project} ({short_id})")
    if mid:
        print(f"[teams] Created thread for session {session.get('session_id', '?')}")
    elif _mode == "webhook":
        # Webhook mode: no message ID returned, use session_id as placeholder
        print(f"[teams] Sent session card via webhook for {session.get('session_id', '?')}")
    return mid


def _sync_transcript(sid, thread):
    """Fetch transcript and send new entries to the thread. Returns count sent."""
    root_mid = thread["root_message_id"]
    sent_index = thread["sent_index"]

    data = _server_get(f"/api/session/{sid}/transcript?limit=500")
    if not data:
        return 0

    entries = data.get("entries", [])

    # Handle /clear - if server has fewer entries than we've sent, reset
    if len(entries) < sent_index:
        thread["sent_index"] = 0
        sent_index = 0

    new_entries = entries[sent_index:]
    if not new_entries:
        return 0

    count = 0
    for entry in new_entries:
        if count >= _MAX_MESSAGES_PER_SCAN:
            break

        messages = _format_transcript_entry(entry)
        for text in messages:
            if text:
                _reply_text(root_mid, text)
                count += 1
                if _MESSAGE_DELAY > 0:
                    time.sleep(_MESSAGE_DELAY)

        thread["sent_index"] += 1

    return count


# ── Reply Polling (Graph API mode only) ──

def _poll_replies_for_session(sid, thread):
    """Poll for new replies in a session's thread and handle commands.

    Looks for: allow, deny, always (for permission requests), or prompt text.
    Only works in graph mode.
    """
    if _mode != "graph":
        return

    root_mid = thread.get("root_message_id")
    if not root_mid:
        return

    replies = _graph_get_replies(root_mid, top=10)
    if not replies:
        return

    # Track which replies we've already processed using timestamps
    last_etag = thread.get("last_reply_etag", "")
    new_etag = ""

    for reply in replies:
        reply_id = reply.get("id", "")
        created = reply.get("createdDateTime", "")

        # Use the most recent reply's timestamp as our etag
        if not new_etag and created:
            new_etag = created

        # Skip replies we've already seen
        if last_etag and created and created <= last_etag:
            continue

        # Skip messages from the bot itself (application type)
        msg_from = reply.get("from", {})
        if msg_from.get("application"):
            continue

        # Extract text content
        body = reply.get("body", {})
        text = body.get("content", "").strip()
        if not text:
            continue

        # Strip HTML tags that Teams may add
        text = re.sub(r"<[^>]+>", "", text).strip()
        if not text:
            continue

        text_lower = text.lower().strip()

        # Check if this is a permission response
        if text_lower in ("allow", "deny", "always"):
            _handle_reply_permission(sid, thread, text_lower)
        else:
            # Treat as a prompt
            if _server_post("/api/send-prompt", {"session_id": sid, "prompt": text}):
                print(f"[teams] Prompt sent to session {sid}: {text[:80]}")
                _reply_text(root_mid, f"Prompt sent to session {sid}.")
            else:
                _reply_text(root_mid, "Failed to send prompt.")

    if new_etag:
        thread["last_reply_etag"] = new_etag


def _handle_reply_permission(sid, thread, decision):
    """Handle a permission reply (allow/deny/always) for the most recent pending request."""
    pending_ids = thread.get("pending_request_ids", set())
    if not pending_ids:
        _reply_text(thread.get("root_message_id"), "No pending permission request to respond to.")
        return

    # Respond to the most recent pending request
    request_id = None
    for rid in list(pending_ids):
        req_file = os.path.join(QUEUE_DIR, f"{rid}.request.json")
        resp_file = os.path.join(QUEUE_DIR, f"{rid}.response.json")
        if os.path.exists(req_file) and not os.path.exists(resp_file):
            request_id = rid
            break

    if not request_id:
        _reply_text(thread.get("root_message_id"), "No pending permission request found.")
        return

    req_data = {}
    req_file = os.path.join(QUEUE_DIR, f"{request_id}.request.json")
    try:
        with open(req_file) as f:
            req_data = json.load(f)
    except (json.JSONDecodeError, IOError):
        pass

    resp_decision = "allow" if decision in ("allow", "always") else "deny"
    resp_data = {"decision": resp_decision}
    if decision == "deny":
        resp_data["message"] = "User denied via Teams"

    resp_file = os.path.join(QUEUE_DIR, f"{request_id}.response.json")
    try:
        if not _write_exclusive(resp_file, resp_data):
            _reply_text(thread.get("root_message_id"), "Already responded to this request.")
            return
        print(f"[teams] Permission {decision} for {request_id}")
    except IOError as e:
        print(f"[teams] Failed to write response: {e}", file=sys.stderr)
        return

    # Handle "always" - persist to settings
    if decision == "always":
        settings_file = req_data.get("settings_file", "")
        if settings_file and "/.claude/" in settings_file:
            allow_patterns = req_data.get("allow_patterns") or []
            if not allow_patterns:
                allow_pattern = req_data.get("allow_pattern", "")
                if allow_pattern:
                    allow_patterns = [allow_pattern]
            for pattern in allow_patterns:
                _add_to_settings(settings_file, pattern)

    with _lock:
        pending_ids.discard(request_id)
        _notified_requests.discard(request_id)

    label = "Allowed" if decision in ("allow", "always") else "Denied"
    _reply_text(thread.get("root_message_id"), f"Permission {label} for request {request_id}.")


# ── Notification Loop ──

def _notification_loop():
    """Background thread: poll /api/sessions and manage Teams threads."""
    while True:
        try:
            _scan_once()
        except Exception as e:
            print(f"[teams] Notification loop error: {e}", file=sys.stderr)
        time.sleep(1)


def _scan_once():
    """Single scan iteration - poll sessions, sync transcripts, handle permissions."""
    sessions_data = _server_get("/api/sessions?local_only=1")
    if not sessions_data:
        return

    sessions = sessions_data.get("sessions", [])
    current_sids = {s["session_id"] for s in sessions}

    # ── Stage 1: Per-session thread management ──
    for s in sessions:
        sid = s["session_id"]
        state = s.get("state", "busy")

        with _lock:
            thread = _session_threads.get(sid)

        # New session -> create thread
        if thread is None:
            root_mid = _create_session_thread(s)
            thread = {
                "root_message_id": root_mid or f"webhook_{sid}",
                "sent_index": 0,
                "last_state": None,
                "pending_request_ids": set(),
                "topic_named": False,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "last_reply_etag": "",
            }
            with _lock:
                _session_threads[sid] = thread
                _save_threads()

        # Sync transcript entries (graph mode only, webhook would be too noisy)
        if _mode == "graph":
            sent_before = thread["sent_index"]
            _sync_transcript(sid, thread)
            if thread["sent_index"] != sent_before:
                with _lock:
                    _save_threads()

        # Update subject with first user prompt (graph mode, once)
        if _mode == "graph" and not thread.get("topic_named") and thread["sent_index"] > 0:
            t_data = _server_get(f"/api/session/{sid}/transcript?limit=50")
            if t_data:
                prompt = _extract_first_user_prompt(t_data.get("entries", []))
                if prompt:
                    short = prompt[:50] + ("..." if len(prompt) > 50 else "")
                    # Update the root card with the subject
                    card = _build_session_root_card(s, subject=short, created_at=thread.get("created_at"))
                    # For graph mode, we could update the message but Graph API
                    # doesn't support updating channel messages easily, so just
                    # post a reply with the subject
                    _reply_text(thread["root_message_id"], f"Subject: {short}")
                    thread["topic_named"] = True
                    with _lock:
                        _save_threads()
                    print(f"[teams] Updated subject for session {sid}: {short}")

        # Detect state changes
        prev_state = thread["last_state"]
        thread["last_state"] = state

        if prev_state is not None and prev_state != state:
            root_mid = thread["root_message_id"]
            label = _STATE_LABELS.get(state, state)

            if state == "idle":
                if _mode == "webhook":
                    card = _build_state_change_card(state, sid)
                    _webhook_send(card)
                else:
                    _reply_text(root_mid, f"[Idle] {label}")
            elif state == "busy" and prev_state == "idle":
                if _mode == "graph":
                    _reply_text(root_mid, f"[Busy] {label}")

        # Poll for replies (graph mode only)
        if _mode == "graph":
            try:
                _poll_replies_for_session(sid, thread)
            except Exception as e:
                print(f"[teams] Reply polling error for {sid}: {e}", file=sys.stderr)

    # ── Stage 2: Permission requests ──
    with _lock:
        notified_snapshot = set(_notified_requests)

    pending = {}
    for path in glob.glob(os.path.join(QUEUE_DIR, "*.request.json")):
        request_id = os.path.basename(path).replace(".request.json", "")
        if not _is_safe_id(request_id):
            continue
        resp = path.replace(".request.json", ".response.json")
        if os.path.exists(resp):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            continue
        pending[request_id] = data

    pending_ids = set(pending.keys())

    # Resolved requests - clean up tracking
    resolved = notified_snapshot - pending_ids
    with _lock:
        for rid in resolved:
            _notified_requests.discard(rid)
            for thread in _session_threads.values():
                thread["pending_request_ids"].discard(rid)

    # New permission requests - send card into session thread
    for rid, data in pending.items():
        if rid in notified_snapshot:
            continue

        card = _build_permission_card(rid, data)
        req_sid = data.get("session_id", "")

        with _lock:
            thread = _session_threads.get(req_sid)

        if thread and _mode == "graph":
            mid = _reply_card(thread["root_message_id"], card,
                              fallback_text=f"Permission: {data.get('tool_name', '?')}")
            if mid:
                thread["pending_request_ids"].add(rid)
        else:
            # No thread found or webhook mode - send as top-level
            _send_card_to_channel(card,
                                  fallback_text=f"Permission: {data.get('tool_name', '?')}")
            if thread:
                thread["pending_request_ids"].add(rid)

        with _lock:
            _notified_requests.add(rid)

    # ── Stage 3: Ended sessions - send farewell, clean up ──
    with _lock:
        ended_sids = set(_session_threads.keys()) - current_sids

    for sid in ended_sids:
        with _lock:
            thread = _session_threads.pop(sid, None)
            _save_threads()
        if thread:
            root_mid = thread.get("root_message_id", "")
            if _mode == "graph" and root_mid and not root_mid.startswith("webhook_"):
                _reply_text(root_mid, "Session ended.")
            elif _mode == "webhook":
                _webhook_send_text(f"Session {sid} ended.")
            print(f"[teams] Session {sid} ended, thread preserved")


# ── Public Entry Point ──

def start_teams_channel():
    """Initialize and start the Microsoft Teams notification channel.

    Called from server.py main(). Starts the notification loop in a
    background thread. Raises on config errors.
    """
    global _mode, _webhook_url, _tenant_id, _client_id, _client_secret
    global _team_id, _channel_id

    cfg = load_config()
    if cfg is None:
        print("[teams] Disabled (no config.json or missing teams config)")
        return

    _mode = cfg.get("mode", "webhook")

    if _mode == "webhook":
        _webhook_url = cfg["webhook_url"]
        print(f"[teams] Webhook mode configured")
    elif _mode == "graph":
        _tenant_id = cfg["tenant_id"]
        _client_id = cfg["client_id"]
        _client_secret = cfg["client_secret"]
        _team_id = cfg["team_id"]
        _channel_id = cfg["channel_id"]

        # Verify we can get an access token
        token = _get_access_token()
        if not token:
            print("[teams] WARNING: Could not acquire initial OAuth2 token", file=sys.stderr)
        else:
            print("[teams] Graph API mode configured, token acquired")
    else:
        print(f"[teams] Unknown mode: {_mode}, disabling", file=sys.stderr)
        return

    _load_threads()

    notify_thread = threading.Thread(target=_notification_loop, daemon=True)
    notify_thread.start()

    print(f"[teams] Channel started in {_mode} mode")
