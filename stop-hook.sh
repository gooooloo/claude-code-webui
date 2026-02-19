#!/bin/bash
# Stop hook for Claude Code
# Detects when Claude finishes a task and waits for a new prompt from the Web UI.
# If the user submits a prompt, blocks the stop and passes the prompt as a systemMessage.
# If dismissed or timed out, allows Claude to stop normally.

QUEUE_DIR="/tmp/claude-approvals"
mkdir -p "$QUEUE_DIR"

# Read stdin with timeout to avoid blocking if no data is sent
INPUT=$(timeout 1 cat 2>/dev/null || true)

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

# Clean up on exit
trap 'rm -f "$WAITING_FILE"' EXIT

# Write waiting marker to queue
jq -n \
  --arg id "$REQUEST_ID" \
  --arg type "prompt-waiting" \
  --arg timestamp "$(date +%s)" \
  --arg pid "$$" \
  --arg session_id "$PPID" \
  --arg project_dir "$PROJECT_DIR" \
  '{
    id: $id,
    type: $type,
    timestamp: ($timestamp | tonumber),
    pid: ($pid | tonumber),
    session_id: ($session_id | tonumber),
    project_dir: $project_dir
  }' > "$WAITING_FILE"


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
        reason: "User submitted a new prompt via Web UI",
        systemMessage: $prompt
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
