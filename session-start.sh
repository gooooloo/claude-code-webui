#!/bin/bash
# SessionStart hook for Claude Code
#
# Called on session lifecycle events: startup, resume, /clear, /compact.
# Sends a POST to the approval server's /api/session-reset endpoint so it can:
#   - Clear stale .request.json / .prompt-waiting.json files for this session
#   - Reset session-level auto-allow rules stored in server memory
#
# Fire-and-forget: silently ignores errors if the server is offline.
#
# Input:  JSON on stdin with { source: "startup"|"resume"|"clear"|"compact" }
# Output: (none)

INPUT=$(cat)

SESSION_ID="${PPID}"
SOURCE=$(echo "$INPUT" | jq -r '.source // "unknown"')

SERVER="http://localhost:19836"

# Notify the approval server (fire-and-forget; silently ignore if server is offline)
curl -s -o /dev/null --max-time 2 \
  -X POST "$SERVER/api/session-reset" \
  -H "Content-Type: application/json" \
  -d "{\"session_id\":\"${SESSION_ID}\",\"source\":\"${SOURCE}\"}" 2>/dev/null || true

exit 0
