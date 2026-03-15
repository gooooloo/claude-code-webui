#!/usr/bin/env python3
"""
Feishu notification channel for Claude Code WebUI.

Each Claude Code session maps to a topic in a Feishu topic group. A root card
is sent to create the topic when a session starts; all subsequent messages
(transcript entries, permission requests, state changes) are appended as
replies within that topic. Topics are preserved after session end.

Requires: pip install lark-oapi
Config:   config.json (see config.example.json)
"""

import errno
import json
import glob
import os
import re
import threading
import time
import urllib.request
import uuid

QUEUE_DIR = "/tmp/claude-webui"
_SERVER_BASE = "http://127.0.0.1:19836"
_SAFE_ID_RE = re.compile(r'^[a-zA-Z0-9_-]+$')
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_THREADS_FILE = os.path.join(_DATA_DIR, "feishu_threads.json")

# Max transcript messages to send per session per scan (avoid Feishu rate limit)
_MAX_MESSAGES_PER_SCAN = 5
# Delay between messages to avoid rate limiting (seconds)
_MESSAGE_DELAY = 0.2


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
        print(f"[feishu] Server API {path} failed: {e}")
        return False


def _server_get(path):
    """GET JSON from the local WebUI server. Returns parsed dict or None."""
    try:
        req = urllib.request.Request(f"{_SERVER_BASE}{path}")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[feishu] Server API GET {path} failed: {e}")
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
_client = None          # lark.Client for REST API calls
_ws_client = None       # lark.ws.Client for WebSocket events
_target_open_id = None  # Auto-discovered user open_id
_topic_chat_id = None   # Topic group chat_id

# session_id -> {
#   "root_message_id": str,     # topic root message ID
#   "sent_index": int,          # number of transcript entries already sent
#   "last_state": str,          # last known state (to detect changes)
#   "pending_request_ids": set, # permission request IDs in this topic
# }
_session_threads = {}

_notified_requests = set()  # permission request IDs already sent
_request_card_ids = {}      # request_id -> feishu message_id (for card updates)
_pending_prompts = {}       # prompt_id -> prompt text (for session picker cards)
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
        _session_threads = data
        print(f"[feishu] Restored {len(data)} session thread(s) from disk")
    except (json.JSONDecodeError, IOError) as e:
        print(f"[feishu] Failed to load threads: {e}")


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
            }
        tmp = _THREADS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(serializable, f, indent=2)
        os.replace(tmp, _THREADS_FILE)
    except (IOError, OSError) as e:
        print(f"[feishu] Failed to save threads: {e}")

# ── Config ──

def _config_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config():
    """Load Feishu config from config.json next to this file."""
    path = _config_path()
    if not os.path.exists(path):
        return None
    with open(path) as f:
        cfg = json.load(f)
    feishu = cfg.get("feishu", {})
    if not feishu.get("enabled", False):
        return None
    if not feishu.get("app_id") or not feishu.get("app_secret"):
        return None
    return feishu


def _save_config_field(key, value):
    """Persist a field to config.json under feishu.<key>."""
    path = _config_path()
    try:
        with open(path) as f:
            cfg = json.load(f)
        cfg.setdefault("feishu", {})[key] = value
        with open(path, "w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        print(f"[feishu] Saved {key} to config.json")
    except (json.JSONDecodeError, IOError) as e:
        print(f"[feishu] Failed to save {key}: {e}")


# ── Topic group management ──

def _create_topic_group():
    """Create a new topic group. Returns chat_id or None."""
    from lark_oapi.api.im.v1 import CreateChatRequest, CreateChatRequestBody

    body = CreateChatRequestBody.builder() \
        .chat_mode("topic") \
        .chat_type("group") \
        .name("Claude Code") \
        .description("Claude Code sessions — each topic is a session") \
        .build()

    request = CreateChatRequest.builder() \
        .request_body(body) \
        .build()

    response = _client.im.v1.chat.create(request)
    if response.success():
        chat_id = response.data.chat_id
        print(f"[feishu] Created topic group: {chat_id}")
        return chat_id
    else:
        print(f"[feishu] Failed to create topic group: {response.code} {response.msg}")
        return None


def _add_user_to_group(chat_id, open_id):
    """Add a user to the topic group."""
    from lark_oapi.api.im.v1 import CreateChatMembersRequest, CreateChatMembersRequestBody

    body = CreateChatMembersRequestBody.builder() \
        .id_list([open_id]) \
        .build()

    request = CreateChatMembersRequest.builder() \
        .chat_id(chat_id) \
        .member_id_type("open_id") \
        .request_body(body) \
        .build()

    response = _client.im.v1.chat_members.create(request)
    if response.success():
        print(f"[feishu] Added user {open_id} to group {chat_id}")
    else:
        print(f"[feishu] Failed to add user to group: {response.code} {response.msg}")


def _ensure_topic_group():
    """Ensure the topic group exists, creating it if needed. Returns chat_id or None."""
    global _topic_chat_id

    if _topic_chat_id:
        return _topic_chat_id

    chat_id = _create_topic_group()
    if chat_id:
        _topic_chat_id = chat_id
        _save_config_field("chat_id", chat_id)
    return chat_id


# ── Card builders ──

_TOOL_COLORS = {
    "Bash": "red", "mcp__acp__Bash": "red",
    "Write": "orange", "mcp__acp__Write": "orange",
    "Edit": "orange", "mcp__acp__Edit": "orange",
    "WebFetch": "blue", "WebSearch": "blue",
    "ExitPlanMode": "purple", "AskUserQuestion": "green",
}

_STATE_COLORS = {
    "idle": "green",
    "busy": "blue",
    "permission_prompt": "red",
    "elicitation": "turquoise",
    "plan_review": "purple",
}

_STATE_LABELS = {
    "idle": "Waiting for input",
    "busy": "Working...",
    "permission_prompt": "Needs approval",
    "elicitation": "Question",
    "plan_review": "Plan review",
}


def _tool_color(tool_name):
    return _TOOL_COLORS.get(tool_name, "grey")


def _truncate(text, max_len=2000):
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "\n... (truncated)"


def _build_permission_card(request_id, data):
    """Build an interactive card for a permission request."""
    tool_name = data.get("tool_name", "Unknown")
    detail = data.get("detail", "")
    detail_sub = data.get("detail_sub", "")
    project_dir = data.get("project_dir", "")
    session_id = data.get("session_id", "")
    allow_pattern = data.get("allow_pattern", "")

    elements = []

    if project_dir:
        project_name = os.path.basename(project_dir)
        elements.append({
            "tag": "markdown",
            "content": f"**Project:** {project_name}  |  **Session:** {session_id}"
        })

    if detail:
        elements.append({
            "tag": "markdown",
            "content": f"```\n{_truncate(detail, 1500)}\n```"
        })

    if detail_sub:
        elements.append({
            "tag": "markdown",
            "content": f"_{_truncate(detail_sub, 500)}_"
        })

    elements.append({"tag": "hr"})

    if allow_pattern:
        elements.append({
            "tag": "markdown",
            "content": f"Pattern: `{allow_pattern}`"
        })

    elements.append({
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "Allow"},
                "type": "primary",
                "value": {"request_id": request_id, "decision": "allow", "type": "permission"}
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "Always Allow"},
                "type": "default",
                "value": {"request_id": request_id, "decision": "always", "type": "permission"}
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "Deny"},
                "type": "danger",
                "value": {"request_id": request_id, "decision": "deny", "type": "permission"}
            },
        ]
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🔐 {tool_name}"},
            "template": _tool_color(tool_name)
        },
        "elements": elements
    }


def _build_permission_resolved_card(request_id, data, decision):
    """Build a card showing a resolved permission request (no buttons)."""
    tool_name = data.get("tool_name", "Unknown")
    detail = data.get("detail", "")

    allowed = decision in ("allow", "always")
    label = "✅ Allowed" if allowed else "❌ Denied"
    template = "green" if allowed else "red"

    elements = []
    if detail:
        elements.append({
            "tag": "markdown",
            "content": f"```\n{_truncate(detail, 500)}\n```"
        })
    elements.append({
        "tag": "markdown",
        "content": f"**{label}**"
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🔐 {tool_name}"},
            "template": template
        },
        "elements": elements
    }


def _build_question_card(request_id, data):
    """Build an interactive card for AskUserQuestion with option buttons.
    Returns None if questions/options are missing (caller should fall back to permission card).
    """
    tool_input = data.get("tool_input", {})
    questions = tool_input.get("questions", [])
    if not questions:
        return None

    session_id = data.get("session_id", "")
    project_dir = data.get("project_dir", "")

    elements = []
    has_buttons = False

    if project_dir:
        project_name = os.path.basename(project_dir)
        elements.append({
            "tag": "markdown",
            "content": f"**Project:** {project_name}  |  **Session:** {session_id}"
        })

    for qi, q in enumerate(questions):
        question_text = q.get("question", "")
        options = q.get("options", [])
        if question_text:
            elements.append({
                "tag": "markdown",
                "content": f"**{question_text}**"
            })
        if options:
            buttons = []
            for oi, opt in enumerate(options):
                label = opt.get("label", f"Option {oi + 1}")
                desc = opt.get("description", "")
                btn_text = f"{label} — {desc}" if desc else label
                buttons.append({
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": _truncate(btn_text, 80)},
                    "type": "primary" if oi == 0 else "default",
                    "value": {
                        "request_id": request_id,
                        "type": "question",
                        "question_index": qi,
                        "option_label": label,
                    }
                })
            elements.append({"tag": "action", "actions": buttons})
            has_buttons = True

    if not has_buttons:
        return None

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "❓ AskUserQuestion"},
            "template": _tool_color("AskUserQuestion")
        },
        "elements": elements
    }


def _build_question_resolved_card(request_id, data, chosen_label):
    """Build a card showing a resolved question (no buttons)."""
    tool_input = data.get("tool_input", {})
    questions = tool_input.get("questions", [])

    elements = []
    for q in questions:
        question_text = q.get("question", "")
        if question_text:
            elements.append({
                "tag": "markdown",
                "content": f"**{question_text}**"
            })
    elements.append({
        "tag": "markdown",
        "content": f"✅ **Answered:** {chosen_label}"
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "❓ AskUserQuestion"},
            "template": "green"
        },
        "elements": elements
    }


def _build_session_root_card(session, subject=None, created_at=None):
    """Build the root card for a new session topic."""
    project_dir = session.get("cwd", "")
    session_id = session.get("session_id", "")
    short_id = session_id[:8] if session_id else "?"
    project_name = os.path.basename(project_dir) if project_dir else "?"
    if not created_at:
        created_at = time.strftime("%Y-%m-%d %H:%M:%S")

    elements = [
        {
            "tag": "markdown",
            "content": f"**Directory:** {project_dir}"
        },
        {
            "tag": "markdown",
            "content": f"**Created:** {created_at}"
        },
        {
            "tag": "markdown",
            "content": f"**Subject:** {subject or '...'}"
        },
    ]

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🚀 {project_name} — {short_id}"},
            "template": "blue"
        },
        "elements": elements
    }


# ── Transcript entry formatting ──

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
    """Convert a transcript entry to a list of (label, text) tuples for posting.

    Returns a list because one assistant entry may produce multiple messages
    (text + tool_use blocks).
    """
    etype = entry.get("type", "")
    msg = entry.get("message", {})
    content = msg.get("content", "")
    results = []

    if etype == "system" and entry.get("subtype") == "compact_boundary":
        meta = entry.get("compactMetadata", {})
        tokens = f"{meta['preTokens'] // 1000}k tokens" if meta.get("preTokens") else ""
        trigger = meta.get("trigger", "")
        detail = ", ".join(filter(None, [trigger, tokens]))
        label = "Context compacted" + (f" ({detail})" if detail else "")
        results.append((f"[System] {label}", None))
        return results

    if etype == "user":
        # Extract user text
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
            results.append(("[You] " + _truncate(text), None))

    elif etype == "assistant":
        if isinstance(content, str):
            if content.strip():
                results.append(("[Claude] " + _truncate(content), None))
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
                    results.append((f"[Tool: {name}] {_truncate(detail, 500)}", None))

            if text_parts:
                combined = "\n".join(text_parts)
                results.insert(0, ("[Claude] " + _truncate(combined), None))

    return results


# ── Feishu API helpers ──

def _send_card_to_group(chat_id, card_content):
    """Send an interactive card to the topic group. Returns message_id or None.

    In a topic group, sending a message to the group creates a new topic.
    """
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

    body = CreateMessageRequestBody.builder() \
        .receive_id(chat_id) \
        .msg_type("interactive") \
        .content(json.dumps(card_content)) \
        .build()

    request = CreateMessageRequest.builder() \
        .receive_id_type("chat_id") \
        .request_body(body) \
        .build()

    response = _client.im.v1.message.create(request)
    if response.success():
        return response.data.message_id
    else:
        print(f"[feishu] Failed to send card to group: {response.code} {response.msg}")
        return None


def _reply_card(parent_message_id, card_content):
    """Reply with an interactive card within a topic. Returns message_id or None."""
    from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

    body = ReplyMessageRequestBody.builder() \
        .msg_type("interactive") \
        .content(json.dumps(card_content)) \
        .reply_in_thread(True) \
        .build()

    request = ReplyMessageRequest.builder() \
        .message_id(parent_message_id) \
        .request_body(body) \
        .build()

    response = _client.im.v1.message.reply(request)
    if response.success():
        return response.data.message_id
    else:
        print(f"[feishu] Failed to reply card: {response.code} {response.msg}")
        return None


def _reply_post(parent_message_id, text):
    """Reply with a post (rich text) message within a topic."""
    from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

    post_content = {
        "zh_cn": {
            "content": [[{"tag": "text", "text": text}]]
        }
    }

    body = ReplyMessageRequestBody.builder() \
        .msg_type("post") \
        .content(json.dumps(post_content)) \
        .reply_in_thread(True) \
        .build()

    request = ReplyMessageRequest.builder() \
        .message_id(parent_message_id) \
        .request_body(body) \
        .build()

    response = _client.im.v1.message.reply(request)
    if not response.success():
        print(f"[feishu] Failed to reply post: {response.code} {response.msg}")


def _adapt_markdown_for_feishu(text):
    """Adapt standard markdown to Feishu card markdown subset.

    Feishu card markdown does NOT support: headings (#), tables, images.
    Convert unsupported syntax to supported alternatives.
    """
    lines = text.split("\n")
    result = []
    in_table = False
    table_lines = []

    for line in lines:
        # Convert headings: # Title → **Title**
        heading_match = re.match(r'^(#{1,6})\s+(.+)$', line)
        if heading_match:
            result.append(f"**{heading_match.group(2)}**")
            continue

        # Detect markdown tables
        if re.match(r'^\s*\|.*\|', line):
            # Skip separator lines like |---|---|
            if re.match(r'^\s*\|[\s\-:|]+\|', line):
                continue
            table_lines.append(line)
            in_table = True
            continue

        # Flush accumulated table as code block
        if in_table:
            result.append("```")
            result.extend(table_lines)
            result.append("```")
            table_lines = []
            in_table = False

        result.append(line)

    # Flush trailing table
    if table_lines:
        result.append("```")
        result.extend(table_lines)
        result.append("```")

    return "\n".join(result)


def _reply_markdown_card(parent_message_id, text):
    """Reply with a card containing a markdown element within a topic."""
    from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

    adapted = _adapt_markdown_for_feishu(text)
    card_content = {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "markdown", "content": adapted}
        ]
    }

    body = ReplyMessageRequestBody.builder() \
        .msg_type("interactive") \
        .content(json.dumps(card_content)) \
        .reply_in_thread(True) \
        .build()

    request = ReplyMessageRequest.builder() \
        .message_id(parent_message_id) \
        .request_body(body) \
        .build()

    response = _client.im.v1.message.reply(request)
    if not response.success():
        print(f"[feishu] Failed to reply markdown card: {response.code} {response.msg}")


def _update_card(message_id, card_content):
    """Update an existing card's content (e.g. mark permission as resolved)."""
    from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

    body = PatchMessageRequestBody.builder() \
        .content(json.dumps(card_content)) \
        .build()

    request = PatchMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(body) \
        .build()

    response = _client.im.v1.message.patch(request)
    if not response.success():
        print(f"[feishu] Failed to update card: {response.code} {response.msg}")


def _pin_message(message_id):
    """Pin a message in a group chat. Used to flag active session topics."""
    from lark_oapi.api.im.v1 import CreatePinRequest, CreatePinRequestBody

    body = CreatePinRequestBody.builder() \
        .message_id(message_id) \
        .build()

    request = CreatePinRequest.builder() \
        .request_body(body) \
        .build()

    response = _client.im.v1.pin.create(request)
    if response.success():
        print(f"[feishu] Pinned message {message_id}")
    else:
        print(f"[feishu] Failed to pin message: {response.code} {response.msg}")


def _unpin_message(message_id):
    """Unpin a message in a group chat. Used to unflag ended session topics."""
    from lark_oapi.api.im.v1 import DeletePinRequest

    request = DeletePinRequest.builder() \
        .message_id(message_id) \
        .build()

    response = _client.im.v1.pin.delete(request)
    if response.success():
        print(f"[feishu] Unpinned message {message_id}")
    else:
        print(f"[feishu] Failed to unpin message: {response.code} {response.msg}")


def _reply_text(message_id, text):
    """Reply to a message with text within a topic."""
    from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

    body = ReplyMessageRequestBody.builder() \
        .msg_type("text") \
        .content(json.dumps({"text": text})) \
        .reply_in_thread(True) \
        .build()

    request = ReplyMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(body) \
        .build()

    response = _client.im.v1.message.reply(request)
    if not response.success():
        print(f"[feishu] Failed to reply: {response.code} {response.msg}")


def _send_text_to_user(open_id, text):
    """Send a plain text message to a user (private chat, for initial connection)."""
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

    body = CreateMessageRequestBody.builder() \
        .receive_id(open_id) \
        .msg_type("text") \
        .content(json.dumps({"text": text})) \
        .build()

    request = CreateMessageRequest.builder() \
        .receive_id_type("open_id") \
        .request_body(body) \
        .build()

    _client.im.v1.message.create(request)


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
            print(f"[feishu] Added to allowlist: {pattern}")
    except (json.JSONDecodeError, IOError) as e:
        print(f"[feishu] Failed to update settings: {e}")


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
        # Skip local command messages (e.g. /clear) and meta entries
        if entry.get("isMeta") or "<command-name>" in text or "<local-command-caveat>" in text:
            continue
        # Strip system tags
        if text:
            text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL)
            text = re.sub(r"<[^>]+>", "", text)
            text = re.sub(r"\s+", " ", text).strip()
        if text:
            return text
    return None


# ── Topic management ──

def _create_session_topic(session):
    """Create a new topic in the topic group for a session. Returns message_id or None."""
    chat_id = _ensure_topic_group()
    if not chat_id:
        return None

    # If we just discovered the user but haven't added them to the group yet, do it now
    if _target_open_id:
        _add_user_to_group(chat_id, _target_open_id)

    card = _build_session_root_card(session)
    mid = _send_card_to_group(chat_id, card)
    if mid:
        print(f"[feishu] Created topic for session {session.get('session_id', '?')}")
        _pin_message(mid)
    return mid


def _sync_transcript(sid, thread):
    """Fetch transcript and send new entries to the topic. Returns count sent."""
    root_mid = thread["root_message_id"]
    sent_index = thread["sent_index"]

    data = _server_get(f"/api/session/{sid}/transcript?limit=500")
    if not data:
        return 0

    entries = data.get("entries", [])

    # Handle /clear — if server has fewer entries than we've sent, reset
    if len(entries) < sent_index:
        thread["sent_index"] = 0
        sent_index = 0

    new_entries = entries[sent_index:]
    if not new_entries:
        return 0

    count = 0
    for entry in new_entries:
        messages = _format_transcript_entry(entry)
        for text, _ in messages:
            if text:
                if text.startswith("[Claude] "):
                    _reply_markdown_card(root_mid, text)
                else:
                    _reply_post(root_mid, text)
                count += 1

        thread["sent_index"] += 1

    return count


# ── Event handlers ──

def _handle_message(data):
    """Handle im.message.receive_v1 — auto-discovery + prompt replies."""
    global _target_open_id

    message = data.event.message
    sender = data.event.sender

    open_id = sender.sender_id.open_id if sender and sender.sender_id else None
    if not open_id:
        return

    message_id = message.message_id if message else None

    # Auto-discovery: first message sets the target user
    newly_connected = False
    with _lock:
        if _target_open_id is None:
            _target_open_id = open_id
            newly_connected = True
    if newly_connected:
        print(f"[feishu] Connected to user: {open_id}")
        _save_config_field("open_id", open_id)

        # Create topic group and add user
        chat_id = _ensure_topic_group()
        if chat_id:
            _add_user_to_group(chat_id, open_id)
            _send_text_to_user(open_id, "Connected! A topic group 'Claude Code' has been created. Each session will appear as a separate topic.")
        else:
            _send_text_to_user(open_id, "Connected! But failed to create topic group.")
        return

    if open_id != _target_open_id:
        return

    # Extract text content
    msg_type = message.message_type if message else None
    content_str = message.content if message else None
    text = ""
    if msg_type == "text" and content_str:
        try:
            content = json.loads(content_str)
            text = content.get("text", "").strip()
        except (json.JSONDecodeError, AttributeError):
            text = ""

    if not text:
        return

    # Route by thread_id — messages within a topic carry the root message's thread_id
    root_id = getattr(message, 'root_id', None) or ''
    target_sid = None

    if root_id:
        with _lock:
            for sid, thread in _session_threads.items():
                if thread["root_message_id"] == root_id:
                    target_sid = sid
                    break

    if target_sid:
        if _server_post("/api/send-prompt", {"session_id": target_sid, "prompt": text}):
            print(f"[feishu] Prompt sent to session {target_sid} (topic match): {text[:80]}")
            if message_id:
                _reply_text(message_id, f"Prompt sent to session {target_sid}.")
        else:
            if message_id:
                _reply_text(message_id, "Failed to send prompt.")
        return

    # Message not in any session topic — show session picker
    print(f"[feishu] Message not in any session topic: {text[:80]}")
    if message_id:
        _show_session_picker(message_id, text)


def _show_session_picker(message_id, prompt_text):
    """Show an interactive card letting the user pick which session to send the prompt to."""
    sessions_data = _server_get("/api/sessions?local_only=1")
    sessions = (sessions_data or {}).get("sessions", [])

    if not sessions:
        _reply_text(message_id, "No active sessions. Please start a Claude Code session first.")
        return

    # Store the prompt text for later retrieval when the user clicks a button
    prompt_id = str(uuid.uuid4())[:8]
    with _lock:
        _pending_prompts[prompt_id] = prompt_text
        # Evict old entries to avoid unbounded growth (keep last 50)
        if len(_pending_prompts) > 50:
            oldest_keys = list(_pending_prompts.keys())[:-50]
            for k in oldest_keys:
                _pending_prompts.pop(k, None)

    elements = [
        {
            "tag": "markdown",
            "content": f"**Your message:**\n{_truncate(prompt_text, 200)}"
        },
        {"tag": "hr"},
        {
            "tag": "markdown",
            "content": "Please select a session to send this prompt to:"
        },
    ]

    buttons = []
    for s in sessions:
        sid = s.get("session_id", "")
        state = s.get("state", "unknown")
        cwd = s.get("cwd", "")
        project_name = os.path.basename(cwd) if cwd else "?"
        short_id = sid[:8] if sid else "?"
        label = f"{project_name} ({short_id}) [{state}]"
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": "primary" if state in ("idle", "elicitation") else "default",
            "value": {
                "type": "send_prompt",
                "prompt_id": prompt_id,
                "session_id": sid,
            }
        })

    elements.append({"tag": "action", "actions": buttons})

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Select Session"},
            "template": "blue"
        },
        "elements": elements
    }

    _reply_card(message_id, card)


def _handle_card_action(data):
    """Handle card.action.trigger — button clicks on cards."""
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

    action = data.event.action if data.event else None
    if not action:
        return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "No action"}})

    value = action.value or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "Invalid action"}})

    request_id = value.get("request_id", "")
    decision = value.get("decision", "")
    action_type = value.get("type", "")

    if action_type == "send_prompt":
        return _handle_send_prompt_action(value)

    if not request_id:
        return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "Missing data"}})

    if not _is_safe_id(request_id):
        return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "Invalid request ID"}})

    if action_type == "question":
        return _handle_question_action(request_id, value)

    if not decision:
        return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "Missing data"}})

    if action_type == "permission":
        return _handle_permission_action(request_id, decision, value)

    return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "Unknown action type"}})


def _handle_send_prompt_action(value):
    """Process a session picker button click — send the stored prompt to the chosen session."""
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

    prompt_id = value.get("prompt_id", "")
    session_id = value.get("session_id", "")

    if not prompt_id or not session_id:
        return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "Missing data"}})

    with _lock:
        prompt_text = _pending_prompts.pop(prompt_id, None)

    if not prompt_text:
        return P2CardActionTriggerResponse({"toast": {"type": "warning", "content": "Prompt expired or already sent"}})

    if _server_post("/api/send-prompt", {"session_id": session_id, "prompt": prompt_text}):
        short_id = session_id[:8]
        print(f"[feishu] Prompt sent to session {session_id} via picker: {prompt_text[:80]}")
        return P2CardActionTriggerResponse({"toast": {"type": "success", "content": f"Sent to {short_id}"}})
    else:
        # Put the prompt back so user can retry
        with _lock:
            _pending_prompts[prompt_id] = prompt_text
        return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "Failed to send prompt"}})


def _handle_permission_action(request_id, decision, value):
    """Process a permission button click (Allow / Always Allow / Deny)."""
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

    request_file = os.path.join(QUEUE_DIR, f"{request_id}.request.json")
    response_file = os.path.join(QUEUE_DIR, f"{request_id}.response.json")

    if not os.path.exists(request_file):
        return P2CardActionTriggerResponse({"toast": {"type": "warning", "content": "Request expired"}})

    req_data = {}
    try:
        with open(request_file) as f:
            req_data = json.load(f)
    except (json.JSONDecodeError, IOError):
        pass

    resp_decision = "allow" if decision in ("allow", "always") else "deny"
    resp_data = {"decision": resp_decision}
    if decision == "deny":
        resp_data["message"] = "User denied via Feishu"
    try:
        if not _write_exclusive(response_file, resp_data):
            return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "Already responded"}})
        print(f"[feishu] Permission {decision} for {request_id}")
    except IOError as e:
        print(f"[feishu] Failed to write response: {e}")
        return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "Write failed"}})

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

    # Update the permission card to show resolved state (no buttons)
    with _lock:
        mid = _request_card_ids.pop(request_id, None)
    if mid:
        resolved_card = _build_permission_resolved_card(request_id, req_data, decision)
        _update_card(mid, resolved_card)

    label = "Allowed" if decision in ("allow", "always") else "Denied"
    return P2CardActionTriggerResponse({"toast": {"type": "success", "content": label}})


def _handle_question_action(request_id, value):
    """Process a question option button click."""
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

    option_label = value.get("option_label", "")
    if not option_label:
        return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "Empty answer"}})

    request_file = os.path.join(QUEUE_DIR, f"{request_id}.request.json")
    response_file = os.path.join(QUEUE_DIR, f"{request_id}.response.json")

    if not os.path.exists(request_file):
        return P2CardActionTriggerResponse({"toast": {"type": "warning", "content": "Request expired"}})

    req_data = {}
    try:
        with open(request_file) as f:
            req_data = json.load(f)
    except (json.JSONDecodeError, IOError):
        pass

    resp_data = {"decision": "deny", "message": f"User answered: {option_label}"}
    try:
        if not _write_exclusive(response_file, resp_data):
            return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "Already responded"}})
        print(f"[feishu] Question answered: {option_label} for {request_id}")
    except IOError as e:
        print(f"[feishu] Failed to write response: {e}")
        return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "Write failed"}})

    # Update the question card to show resolved state (no buttons)
    with _lock:
        mid = _request_card_ids.pop(request_id, None)
    if mid:
        resolved_card = _build_question_resolved_card(request_id, req_data, option_label)
        _update_card(mid, resolved_card)

    return P2CardActionTriggerResponse({"toast": {"type": "success", "content": f"Answered: {option_label}"}})


# ── Notification loop ──

def _notification_loop():
    """Background thread: poll /api/sessions and manage Feishu topics."""
    while True:
        try:
            _scan_once()
        except Exception as e:
            print(f"[feishu] Notification loop error: {e}")
        time.sleep(1)


def _scan_once():
    """Single scan iteration — poll sessions, sync transcripts, handle permissions."""
    if _target_open_id is None:
        return

    if _topic_chat_id is None:
        return

    sessions_data = _server_get("/api/sessions?local_only=1")
    if not sessions_data:
        return

    sessions = sessions_data.get("sessions", [])
    current_sids = {s["session_id"] for s in sessions}

    # ── Stage 1: Per-session topic management ──
    for s in sessions:
        sid = s["session_id"]
        state = s.get("state", "busy")

        with _lock:
            thread = _session_threads.get(sid)

        # New session → create topic
        if thread is None:
            root_mid = _create_session_topic(s)
            if not root_mid:
                continue
            thread = {
                "root_message_id": root_mid,
                "sent_index": 0,
                "last_state": None,
                "pending_request_ids": set(),
                "topic_named": False,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            with _lock:
                _session_threads[sid] = thread
                _save_threads()

        # Sync transcript entries
        sent_before = thread["sent_index"]
        _sync_transcript(sid, thread)
        if thread["sent_index"] != sent_before:
            with _lock:
                _save_threads()

        # Update subject field in root card with first user prompt (once)
        if not thread.get("topic_named") and thread["sent_index"] > 0:
            t_data = _server_get(f"/api/session/{sid}/transcript?limit=50")
            if t_data:
                prompt = _extract_first_user_prompt(t_data.get("entries", []))
                if prompt:
                    short = prompt[:50] + ("..." if len(prompt) > 50 else "")
                    card = _build_session_root_card(s, subject=short, created_at=thread.get("created_at"))
                    _update_card(thread["root_message_id"], card)
                    thread["topic_named"] = True
                    with _lock:
                        _save_threads()
                    print(f"[feishu] Updated subject for session {sid}: {short}")

        # Detect state changes
        prev_state = thread["last_state"]
        thread["last_state"] = state

        if prev_state is not None and prev_state != state:
            root_mid = thread["root_message_id"]
            label = _STATE_LABELS.get(state, state)

            if state == "idle":
                _reply_post(root_mid, f"⏸ {label}")
            elif state == "busy" and prev_state == "idle":
                _reply_post(root_mid, f"▶ {label}")

    # ── Stage 2: Permission requests (sent into session topics) ──
    with _lock:
        notified_snapshot = set(_notified_requests)

    pending = {}
    for path in glob.glob(os.path.join(QUEUE_DIR, "*.request.json")):
        request_id = os.path.basename(path).replace(".request.json", "")
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

    # Resolved requests → update cards to show resolved state
    resolved = notified_snapshot - pending_ids
    with _lock:
        for rid in resolved:
            _request_card_ids.pop(rid, None)
            _notified_requests.discard(rid)
            for thread in _session_threads.values():
                thread["pending_request_ids"].discard(rid)

    # New permission requests → send card into session topic
    for rid, data in pending.items():
        if rid in notified_snapshot:
            continue

        tool_name = data.get("tool_name", "")
        card = None
        if tool_name == "AskUserQuestion":
            card = _build_question_card(rid, data)
        if not card:
            card = _build_permission_card(rid, data)
        req_sid = data.get("session_id", "")

        with _lock:
            thread = _session_threads.get(req_sid)

        if thread:
            mid = _reply_card(thread["root_message_id"], card)
            if mid:
                thread["pending_request_ids"].add(rid)
        else:
            # No topic found — send as top-level message in group (creates new topic)
            mid = _send_card_to_group(_topic_chat_id, card)

        with _lock:
            _notified_requests.add(rid)
            if mid:
                _request_card_ids[rid] = mid

    # ── Stage 3: Ended sessions → send farewell, clean up ──
    with _lock:
        ended_sids = set(_session_threads.keys()) - current_sids

    for sid in ended_sids:
        with _lock:
            thread = _session_threads.pop(sid, None)
            _save_threads()
        if thread:
            _unpin_message(thread["root_message_id"])
            _reply_post(thread["root_message_id"], "🏁 Session ended.")
            print(f"[feishu] Session {sid} ended, topic preserved")


# ── Public entry point ──

def _patch_ws_card_callback(ws_client):
    """Monkey-patch ws.Client to handle CARD messages.

    The lark-oapi SDK (v1.5.3) ws.Client._handle_data_frame has:
        elif message_type == MessageType.CARD:
            return   # does nothing, no response sent
    This causes Feishu error 200340 on card button clicks.

    We replace _handle_data_frame to route CARD messages through the
    event dispatcher's callback processor, same as EVENT messages.
    """
    import base64
    import http as http_module
    from lark_oapi.core.json import JSON
    from lark_oapi.core.const import UTF_8
    from lark_oapi.ws.enum import MessageType
    from lark_oapi.ws.model import Response

    original_handle = ws_client._handle_data_frame

    async def patched_handle_data_frame(frame):
        from lark_oapi.ws.const import HEADER_TYPE, HEADER_BIZ_RT

        hs = frame.headers
        type_ = None
        for h in hs:
            if h.key == HEADER_TYPE:
                type_ = h.value
                break
        if type_ is None:
            return await original_handle(frame)

        message_type = MessageType(type_)
        if message_type != MessageType.CARD:
            return await original_handle(frame)

        pl = frame.payload
        resp = Response(code=http_module.HTTPStatus.OK)
        try:
            start = int(round(time.time() * 1000))
            result = ws_client._event_handler.do_without_validation(pl)
            end = int(round(time.time() * 1000))
            header = hs.add()
            header.key = HEADER_BIZ_RT
            header.value = str(end - start)
            if result is not None:
                resp.data = base64.b64encode(JSON.marshal(result).encode(UTF_8))
        except Exception as e:
            print(f"[feishu] Card callback error: {e}")
            resp = Response(code=http_module.HTTPStatus.INTERNAL_SERVER_ERROR)

        frame.payload = JSON.marshal(resp).encode(UTF_8)
        await ws_client._write_message(frame.SerializeToString())

    ws_client._handle_data_frame = patched_handle_data_frame


def start_feishu_channel():
    """Initialize and start the Feishu notification channel.

    Called from server.py main(). Starts the WebSocket client and
    notification loop in background threads. Raises on config/SDK errors.
    """
    global _client, _ws_client, _target_open_id, _topic_chat_id

    cfg = load_config()
    if cfg is None:
        print("[feishu] Disabled (no config.json or feishu.enabled=false)")
        return

    try:
        import lark_oapi as lark
    except ImportError:
        print("[feishu] lark-oapi not installed (pip install lark-oapi), skipping")
        return

    _load_threads()

    saved_open_id = cfg.get("open_id")
    if saved_open_id:
        _target_open_id = saved_open_id
        print(f"[feishu] Restored open_id from config: {saved_open_id}")

    saved_chat_id = cfg.get("chat_id")
    if saved_chat_id:
        _topic_chat_id = saved_chat_id
        print(f"[feishu] Restored topic group chat_id from config: {saved_chat_id}")

    app_id = cfg["app_id"]
    app_secret = cfg["app_secret"]

    _client = lark.Client.builder() \
        .app_id(app_id) \
        .app_secret(app_secret) \
        .log_level(lark.LogLevel.INFO) \
        .build()

    # If we have the user but no topic group yet, create it now
    if _target_open_id and not _topic_chat_id:
        chat_id = _ensure_topic_group()
        if chat_id:
            _add_user_to_group(chat_id, _target_open_id)

    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(_handle_message) \
        .register_p2_card_action_trigger(_handle_card_action) \
        .build()

    _ws_client = lark.ws.Client(
        app_id=app_id,
        app_secret=app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.INFO
    )

    _patch_ws_card_callback(_ws_client)

    ws_thread = threading.Thread(target=_ws_client.start, daemon=True)
    ws_thread.start()

    notify_thread = threading.Thread(target=_notification_loop, daemon=True)
    notify_thread.start()

    print("[feishu] WebSocket client started (topic group mode)")
    if _target_open_id is None:
        print("[feishu] Send any message to the bot from Feishu to connect")
