#!/usr/bin/env python3
"""
Feishu (飞书) notification channel for Claude Code WebUI.

Sends permission requests and prompt-waiting notifications to a Feishu bot,
allowing users to approve/deny/respond from their phone. Coexists with the
browser UI — first responder wins.

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

QUEUE_DIR = "/tmp/claude-webui"
_SAFE_ID_RE = re.compile(r'^[a-zA-Z0-9_-]+$')


def _is_safe_id(request_id):
    """Validate request_id contains only safe characters (no path traversal)."""
    return bool(request_id) and bool(_SAFE_ID_RE.match(request_id))


def _write_exclusive(filepath, data):
    """Atomically create a file only if it doesn't exist (O_CREAT|O_EXCL).

    Returns True if written, False if file already exists.
    Raises IOError on other failures.
    """
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
_notified = set()       # Request IDs already pushed to Feishu
_card_ids = {}          # request_id → feishu message_id (for updating cards)
_lock = threading.Lock()

# ── Config ──

def _config_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config():
    """Load Feishu config from config.json next to this file.

    Returns the feishu dict (including open_id if previously saved), or None.
    """
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


def _save_open_id(open_id):
    """Persist open_id to config.json under feishu.open_id."""
    path = _config_path()
    try:
        with open(path) as f:
            cfg = json.load(f)
        cfg.setdefault("feishu", {})["open_id"] = open_id
        with open(path, "w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        print(f"[feishu] Saved open_id to config.json")
    except (json.JSONDecodeError, IOError) as e:
        print(f"[feishu] Failed to save open_id: {e}")


# ── Card builders ──

_TOOL_COLORS = {
    "Bash": "red",
    "mcp__acp__Bash": "red",
    "Write": "orange",
    "mcp__acp__Write": "orange",
    "Edit": "orange",
    "mcp__acp__Edit": "orange",
    "WebFetch": "blue",
    "WebSearch": "blue",
    "ExitPlanMode": "purple",
    "AskUserQuestion": "green",
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

    # Project & session info
    if project_dir:
        project_name = os.path.basename(project_dir)
        elements.append({
            "tag": "markdown",
            "content": f"**Project:** {project_name}  |  **Session:** {session_id}"
        })

    # Detail (command, file path, etc.)
    if detail:
        elements.append({
            "tag": "markdown",
            "content": f"```\n{_truncate(detail, 1500)}\n```"
        })

    # Sub-detail (old_string for Edit, prompt for WebFetch, etc.)
    if detail_sub:
        elements.append({
            "tag": "markdown",
            "content": f"_{_truncate(detail_sub, 500)}_"
        })

    # Divider before buttons
    elements.append({"tag": "hr"})

    # Allow pattern display
    if allow_pattern:
        elements.append({
            "tag": "markdown",
            "content": f"Pattern: `{allow_pattern}`"
        })

    # Action buttons (value must be a dict, not a JSON string)
    elements.append({
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "Allow"},
                "type": "primary",
                "value": {
                    "request_id": request_id,
                    "decision": "allow",
                    "type": "permission"
                }
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "Always Allow"},
                "type": "default",
                "value": {
                    "request_id": request_id,
                    "decision": "always",
                    "type": "permission"
                }
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "Deny"},
                "type": "danger",
                "value": {
                    "request_id": request_id,
                    "decision": "deny",
                    "type": "permission"
                }
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


def _build_prompt_card(request_id, data):
    """Build a card for prompt-waiting (Claude finished, waiting for next prompt)."""
    last_response = data.get("last_response", "")
    project_dir = data.get("project_dir", "")
    session_id = data.get("session_id", "")

    elements = []

    if project_dir:
        project_name = os.path.basename(project_dir)
        elements.append({
            "tag": "markdown",
            "content": f"**Project:** {project_name}  |  **Session:** {session_id}"
        })

    if last_response:
        elements.append({
            "tag": "markdown",
            "content": f"```\n{_truncate(last_response)}\n```"
        })
    else:
        elements.append({
            "tag": "markdown",
            "content": "Claude has completed the current task."
        })

    elements.append({"tag": "hr"})

    elements.append({
        "tag": "markdown",
        "content": "**Reply to this bot with your next prompt**, or dismiss:"
    })

    elements.append({
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "Dismiss"},
                "type": "default",
                "value": {
                    "request_id": request_id,
                    "decision": "dismiss",
                    "type": "prompt"
                }
            }
        ]
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "💬 Claude is waiting for input"},
            "template": "green"
        },
        "elements": elements
    }


# ── Feishu API helpers ──

def _send_card(open_id, card_content):
    """Send an interactive card to a user. Returns message_id or None."""
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

    body = CreateMessageRequestBody.builder() \
        .receive_id(open_id) \
        .msg_type("interactive") \
        .content(json.dumps(card_content)) \
        .build()

    request = CreateMessageRequest.builder() \
        .receive_id_type("open_id") \
        .request_body(body) \
        .build()

    response = _client.im.v1.message.create(request)
    if response.success():
        return response.data.message_id
    else:
        print(f"[feishu] Failed to send card: {response.code} {response.msg}")
        return None


def _delete_message(message_id):
    """Delete a message by message_id."""
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import DeleteMessageRequest

    request = DeleteMessageRequest.builder() \
        .message_id(message_id) \
        .build()

    response = _client.im.v1.message.delete(request)
    if not response.success():
        print(f"[feishu] Failed to delete message: {response.code} {response.msg}")


def _reply_text(message_id, text):
    """Reply to a message with text."""
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

    body = ReplyMessageRequestBody.builder() \
        .msg_type("text") \
        .content(json.dumps({"text": text})) \
        .build()

    request = ReplyMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(body) \
        .build()

    response = _client.im.v1.message.reply(request)
    if not response.success():
        print(f"[feishu] Failed to reply: {response.code} {response.msg}")


def _send_text(open_id, text):
    """Send a plain text message to a user."""
    import lark_oapi as lark
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
    """Add an allow pattern to settings.local.json (same logic as server.py)."""
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


# ── Event handlers ──

def _handle_message(data):
    """Handle im.message.receive_v1 — auto-discovery + prompt replies."""
    global _target_open_id

    message = data.event.message
    sender = data.event.sender

    # Extract sender open_id
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
        _save_open_id(open_id)
        if message_id:
            _reply_text(message_id, "Connected! You will now receive Claude Code notifications here.")
        return

    # Subsequent messages: treat as prompt submission
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

    # Find the most recent prompt-waiting file and submit the prompt
    prompt_files = sorted(
        glob.glob(os.path.join(QUEUE_DIR, "*.prompt-waiting.json")),
        key=os.path.getmtime,
        reverse=True
    )

    for wf in prompt_files:
        request_id = os.path.basename(wf).replace(".prompt-waiting.json", "")
        response_file = os.path.join(QUEUE_DIR, f"{request_id}.prompt-response.json")

        # First responder wins (atomic create)
        try:
            if not _write_exclusive(response_file, {"action": "submit", "prompt": text}):
                continue  # Another channel already responded
            print(f"[feishu] Prompt submitted for {request_id}: {text[:80]}")

            # Update the Feishu card if we have one
            with _lock:
                mid = _card_ids.get(request_id)
            if mid:
                _delete_message(mid)

            if message_id:
                _reply_text(message_id, f"Prompt sent to Claude.")
        except IOError:
            pass
        return

    # No pending prompt-waiting
    if message_id:
        _reply_text(message_id, "No pending prompt request. Your message was not delivered to Claude.")


def _handle_card_action(data):
    """Handle card.action.trigger — button clicks on cards."""
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

    action = data.event.action if data.event else None
    if not action:
        return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "No action"}})

    # action.value is a Dict[str, Any]
    value = action.value or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "Invalid action"}})

    request_id = value.get("request_id", "")
    decision = value.get("decision", "")
    action_type = value.get("type", "")

    if not request_id or not decision:
        return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "Missing data"}})

    if not _is_safe_id(request_id):
        return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "Invalid request ID"}})

    if action_type == "permission":
        return _handle_permission_action(request_id, decision, value)
    elif action_type == "prompt":
        return _handle_prompt_action(request_id, decision)

    return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "Unknown action type"}})


def _handle_permission_action(request_id, decision, value):
    """Process a permission button click (Allow / Always Allow / Deny)."""
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

    request_file = os.path.join(QUEUE_DIR, f"{request_id}.request.json")
    response_file = os.path.join(QUEUE_DIR, f"{request_id}.response.json")

    if not os.path.exists(request_file):
        return P2CardActionTriggerResponse({"toast": {"type": "warning", "content": "Request expired"}})

    # Read request data before writing response (needed for "always allow")
    req_data = {}
    try:
        with open(request_file) as f:
            req_data = json.load(f)
    except (json.JSONDecodeError, IOError):
        pass

    # Write response atomically (first responder wins)
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

    # Handle "Always Allow" — write to settings (only after winning the race)
    if decision == "always":
        settings_file = req_data.get("settings_file", "")
        # Validate settings_file points to a .claude/ directory
        if settings_file and "/.claude/" in settings_file:
            allow_patterns = req_data.get("allow_patterns") or []
            if not allow_patterns:
                allow_pattern = req_data.get("allow_pattern", "")
                if allow_pattern:
                    allow_patterns = [allow_pattern]
            for pattern in allow_patterns:
                _add_to_settings(settings_file, pattern)

    # Read tool name for the resolved card
    tool_name = ""
    try:
        with open(request_file) as f:
            tool_name = json.load(f).get("tool_name", "")
    except (json.JSONDecodeError, IOError):
        pass

    # Update the card to show resolved status
    with _lock:
        mid = _card_ids.get(request_id)
    if mid:
        _delete_message(mid)

    label = "Allowed" if decision in ("allow", "always") else "Denied"
    return P2CardActionTriggerResponse({"toast": {"type": "success", "content": label}})


def _handle_prompt_action(request_id, decision):
    """Process a prompt card button click (Dismiss)."""
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

    response_file = os.path.join(QUEUE_DIR, f"{request_id}.prompt-response.json")

    try:
        if not _write_exclusive(response_file, {"action": "dismiss"}):
            return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "Already responded"}})
        print(f"[feishu] Prompt dismissed for {request_id}")
    except IOError:
        pass

    with _lock:
        mid = _card_ids.get(request_id)
    if mid:
        _delete_message(mid)

    return P2CardActionTriggerResponse({"toast": {"type": "success", "content": "Dismissed"}})


# ── Notification loop ──

def _notification_loop():
    """Background thread: scan for new requests and update Feishu cards."""
    while True:
        try:
            _scan_once()
        except Exception as e:
            print(f"[feishu] Notification loop error: {e}")
        time.sleep(0.5)


def _scan_once():
    """Single scan iteration — aligned with /api/pending logic from WebUI."""
    global _target_open_id

    if _target_open_id is None:
        return

    with _lock:
        notified_snapshot = set(_notified)

    # Stage 1: Build pending set (same criteria as /api/pending)
    pending = {}  # request_id → (data, type)

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
        pending[request_id] = (data, "permission")

    for path in glob.glob(os.path.join(QUEUE_DIR, "*.prompt-waiting.json")):
        request_id = os.path.basename(path).replace(".prompt-waiting.json", "")
        resp = path.replace(".prompt-waiting.json", ".prompt-response.json")
        if os.path.exists(resp):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            continue
        pending[request_id] = (data, "prompt")

    pending_ids = set(pending.keys())

    # Stage 2a: Resolved requests → delete cards
    # (anything previously notified but no longer pending, regardless of reason)
    resolved = notified_snapshot - pending_ids
    resolved_mids = []
    with _lock:
        for rid in resolved:
            mid = _card_ids.pop(rid, None)
            if mid:
                resolved_mids.append(mid)
            _notified.discard(rid)
    for mid in resolved_mids:
        _delete_message(mid)

    # Stage 2b: New requests → send cards
    for rid, (data, rtype) in pending.items():
        if rid in notified_snapshot:
            continue
        if rtype == "permission":
            card = _build_permission_card(rid, data)
        else:
            card = _build_prompt_card(rid, data)
        mid = _send_card(_target_open_id, card)
        with _lock:
            _notified.add(rid)
            if mid:
                _card_ids[rid] = mid


# ── Public entry point ──

def _patch_ws_card_callback(ws_client):
    """Monkey-patch ws.Client to handle CARD messages.

    The lark-oapi SDK (v1.5.3) ws.Client._handle_data_frame has:
        elif message_type == MessageType.CARD:
            return   # ← does nothing, no response sent
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

        # Handle CARD callback
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
    global _client, _ws_client

    global _target_open_id

    cfg = load_config()
    if cfg is None:
        print("[feishu] Disabled (no config.json or feishu.enabled=false)")
        return

    try:
        import lark_oapi as lark
    except ImportError:
        print("[feishu] lark-oapi not installed (pip install lark-oapi), skipping")
        return

    # Restore persisted open_id
    saved_open_id = cfg.get("open_id")
    if saved_open_id:
        _target_open_id = saved_open_id
        print(f"[feishu] Restored open_id from config: {saved_open_id}")

    app_id = cfg["app_id"]
    app_secret = cfg["app_secret"]

    # HTTP client for sending messages
    _client = lark.Client.builder() \
        .app_id(app_id) \
        .app_secret(app_secret) \
        .log_level(lark.LogLevel.INFO) \
        .build()

    # Event dispatcher
    handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(_handle_message) \
        .register_p2_card_action_trigger(_handle_card_action) \
        .build()

    # WebSocket client (DEBUG to diagnose card callback)
    _ws_client = lark.ws.Client(
        app_id=app_id,
        app_secret=app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.INFO
    )

    # Patch: SDK ws.Client ignores CARD messages (returns early at line 264).
    # We monkey-patch _handle_data_frame to route CARD messages through the
    # event dispatcher's callback processor, so card button clicks work.
    _patch_ws_card_callback(_ws_client)

    # Start WS in background thread
    ws_thread = threading.Thread(target=_ws_client.start, daemon=True)
    ws_thread.start()

    # Start notification loop in background thread
    notify_thread = threading.Thread(target=_notification_loop, daemon=True)
    notify_thread.start()

    print("[feishu] WebSocket client started")
    if _target_open_id is None:
        print("[feishu] Send any message to the bot from Feishu to connect")
