#!/bin/bash
# SessionEnd hook for Claude Code
#
# Called when a session terminates (user exits Claude Code).
# At this point no hooks are polling for responses, so we aggressively clean up:
#   - Notify the server to clear session auto-allow rules and delete all
#     request/response/prompt files for this session
#   - Locally delete any remaining files for this session as a fallback
#     (in case the server is offline)
#
# Fire-and-forget: silently ignores errors if the server is offline.
#
# Input:  JSON on stdin (currently empty for SessionEnd)
# Output: (none)

SESSION_ID="${PPID}"
QUEUE_DIR="/tmp/claude-approvals"
SERVER="http://localhost:19836"

# Notify the approval server (fire-and-forget)
curl -s -o /dev/null --max-time 2 \
  -X POST "$SERVER/api/session-end" \
  -H "Content-Type: application/json" \
  -d "{\"session_id\":\"${SESSION_ID}\"}" 2>/dev/null || true

# Local fallback cleanup: delete all files belonging to this session
if [ -d "$QUEUE_DIR" ]; then
  for f in "$QUEUE_DIR"/*.request.json "$QUEUE_DIR"/*.prompt-waiting.json; do
    [ -f "$f" ] || continue
    file_sid=$(jq -r '.session_id // ""' "$f" 2>/dev/null)
    if [ "$file_sid" = "$SESSION_ID" ]; then
      base="${f%.request.json}"
      base="${base%.prompt-waiting.json}"
      rm -f "$f" "${base}.response.json" "${base}.prompt-response.json"
    fi
  done
fi

exit 0
