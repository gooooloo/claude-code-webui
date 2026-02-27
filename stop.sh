#!/bin/bash
# Stop hook for Claude Code
# Detects when Claude finishes a task and waits for a new prompt from the Web UI.
# If the user submits a prompt, blocks the stop and passes the prompt as a systemMessage.
# If dismissed or timed out, allows Claude to stop normally.

QUEUE_DIR="/tmp/claude-approvals"
mkdir -p "$QUEUE_DIR"

# If already inside a stop-hook continuation, just approve to avoid infinite loops
if [ "${CLAUDE_STOP_HOOK_ACTIVE:-}" = "1" ]; then
  jq -n '{ decision: "approve" }'
  exit 0
fi
export CLAUDE_STOP_HOOK_ACTIVE=1

# Read stdin (stop hooks receive it reliably)
INPUT=$(cat)

# Extract last_assistant_message from stdin JSON
LAST_RESPONSE=$(echo "$INPUT" | jq -r '.last_assistant_message // ""' 2>/dev/null || true)

# Generate unique request ID
REQUEST_ID=$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid 2>/dev/null || date +%s%N)
WAITING_FILE="$QUEUE_DIR/$REQUEST_ID.prompt-waiting.json"
RESPONSE_FILE="$QUEUE_DIR/$REQUEST_ID.prompt-response.json"

PROJECT_DIR="$(pwd)"

# Kill old stop-hook instances from the same session (PPID) before starting
for old_file in "$QUEUE_DIR"/*.prompt-waiting.json; do
  [ -f "$old_file" ] || continue
  old_pid=$(jq -r '.pid // ""' "$old_file" 2>/dev/null)
  old_sid=$(jq -r '.session_id // ""' "$old_file" 2>/dev/null)
  if [ "$old_sid" = "$PPID" ] && [ -n "$old_pid" ] && [ "$old_pid" != "$$" ]; then
    kill "$old_pid" 2>/dev/null || true
    rm -f "$old_file"
    rm -f "${old_file%.prompt-waiting.json}.prompt-response.json"
  fi
done

# Detect tmux mode
TMUX_MODE="false"
TMUX_PANE_ID=""
if [ -n "$TMUX" ] && [ -n "$TMUX_PANE" ]; then
  TMUX_MODE="true"
  TMUX_PANE_ID="$TMUX_PANE"
fi

# Check if the approval server is running before doing anything
if ! curl -s --max-time 2 http://localhost:19836/ > /dev/null 2>&1; then
  # Server not running, no point waiting — approve immediately
  jq -n '{ decision: "approve" }'
  exit 0
fi

# Clean up on exit (only for non-tmux mode; tmux mode exits immediately)
if [ "$TMUX_MODE" = "false" ]; then
  trap 'rm -f "$WAITING_FILE"' EXIT
fi

# Write waiting marker to queue
if [ "$TMUX_MODE" = "true" ]; then
  jq -n \
    --arg id "$REQUEST_ID" \
    --arg type "prompt-waiting" \
    --arg timestamp "$(date +%s)" \
    --arg pid "$$" \
    --arg session_id "$PPID" \
    --arg project_dir "$PROJECT_DIR" \
    --arg last_response "$LAST_RESPONSE" \
    --arg tmux_pane "$TMUX_PANE_ID" \
    '{
      id: $id,
      type: $type,
      timestamp: ($timestamp | tonumber),
      pid: ($pid | tonumber),
      session_id: ($session_id | tonumber),
      project_dir: $project_dir,
      last_response: $last_response,
      tmux_mode: true,
      tmux_pane: $tmux_pane
    }' > "$WAITING_FILE"
else
  jq -n \
    --arg id "$REQUEST_ID" \
    --arg type "prompt-waiting" \
    --arg timestamp "$(date +%s)" \
    --arg pid "$$" \
    --arg session_id "$PPID" \
    --arg project_dir "$PROJECT_DIR" \
    --arg last_response "$LAST_RESPONSE" \
    '{
      id: $id,
      type: $type,
      timestamp: ($timestamp | tonumber),
      pid: ($pid | tonumber),
      session_id: ($session_id | tonumber),
      project_dir: $project_dir,
      last_response: $last_response
    }' > "$WAITING_FILE"
fi

# Tmux mode: approve immediately (Claude shows > prompt, Web UI uses send-keys)
if [ "$TMUX_MODE" = "true" ]; then
  jq -n '{ decision: "approve" }'
  exit 0
fi

# Poll for response (prompt submission or dismiss)
TIMEOUT=86400
ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
  if [ -f "$RESPONSE_FILE" ]; then
    ACTION=$(jq -r '.action // "dismiss"' "$RESPONSE_FILE")
    PROMPT=$(jq -r '.prompt // ""' "$RESPONSE_FILE")
    rm -f "$WAITING_FILE" "$RESPONSE_FILE"

    if [ "$ACTION" = "submit" ] && [ -n "$PROMPT" ]; then
      jq -n --arg prompt "$PROMPT" '{
        decision: "block",
        reason: ("User submitted a new prompt via Web UI:\n" + $prompt)
      }'
    else
      jq -n '{ decision: "approve" }'
    fi
    exit 0
  fi
  sleep 0.5
  ELAPSED=$((ELAPSED + 1))
done

# Timeout: allow Claude to stop
rm -f "$WAITING_FILE" "$RESPONSE_FILE"
jq -n '{ decision: "approve" }'
