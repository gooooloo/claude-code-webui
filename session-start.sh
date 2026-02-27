#!/bin/bash
# SessionStart hook for Claude Code
#
# Called on session lifecycle events: startup, resume, /clear, /compact.
# Sends a POST to the server's /api/session-reset endpoint so it can:
#   - Clear stale .request.json / .prompt-waiting.json files for this session
#   - Reset session-level auto-allow rules stored in server memory
#
# Special handling for /clear in tmux mode:
#   After /clear, the Stop hook does NOT fire, so no new .prompt-waiting.json
#   is created. This script re-creates one after the session-reset call so the
#   web UI prompt card reappears for the user.
#
# Fire-and-forget: silently ignores errors if the server is offline.
#
# Input:  JSON on stdin with { source: "startup"|"resume"|"clear"|"compact" }
# Output: (none)

INPUT=$(cat)

SESSION_ID="${PPID}"
SOURCE=$(echo "$INPUT" | jq -r '.source // "unknown"')

SERVER="http://localhost:19836"

# Notify the server (fire-and-forget; silently ignore if server is offline)
curl -s -o /dev/null --max-time 2 \
  -X POST "$SERVER/api/session-reset" \
  -H "Content-Type: application/json" \
  -d "{\"session_id\":\"${SESSION_ID}\",\"source\":\"${SOURCE}\"}" 2>/dev/null || true

QUEUE_DIR="/tmp/claude-webui"

# In tmux mode after /clear, re-create a prompt-waiting card so the web UI
# shows the prompt input (Stop hook doesn't fire after /clear).
if [ "$SOURCE" = "clear" ] && [ -n "$TMUX" ] && [ -n "$TMUX_PANE" ]; then
  mkdir -p "$QUEUE_DIR"
  REQUEST_ID=$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid 2>/dev/null || date +%s%N)
  WAITING_FILE="$QUEUE_DIR/$REQUEST_ID.prompt-waiting.json"
  jq -n \
    --arg id "$REQUEST_ID" \
    --arg timestamp "$(date +%s)" \
    --arg session_id "$SESSION_ID" \
    --arg project_dir "$(pwd)" \
    --arg tmux_pane "$TMUX_PANE" \
    '{
      id: $id,
      type: "prompt-waiting",
      timestamp: ($timestamp | tonumber),
      pid: ($session_id | tonumber),
      session_id: ($session_id | tonumber),
      project_dir: $project_dir,
      last_response: "",
      tmux_mode: true,
      tmux_pane: $tmux_pane
    }' > "$WAITING_FILE"
fi

exit 0
