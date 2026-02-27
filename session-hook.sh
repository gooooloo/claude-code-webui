#!/bin/bash
# SessionStart hook — notifies the approval server when a session starts/resets.
# Triggered on: startup, resume, clear, compact.
# This allows the server to clean up stale requests and session-level auto-allow rules.

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
