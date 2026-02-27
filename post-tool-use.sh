#!/bin/bash
# PostToolUse hook for Claude Code
#
# Called after Claude finishes executing a tool. Cleans up the matching
# .request.json and .response.json files from /tmp/claude-approvals/ so the
# Web UI no longer shows the completed request.
#
# Matching strategy:
#   1. Exact match — both tool_name and tool_input match → delete and exit
#   2. Fallback — only tool_name matches (for tools like AskUserQuestion where
#      tool_input in PostToolUse includes answers not present in the original request)
#
# Input:  JSON on stdin with { tool_name, tool_input }
# Output: (none)
QUEUE_DIR="/tmp/claude-approvals"

INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // ""')
TOOL_INPUT=$(echo "$INPUT" | jq -r '.tool_input // {}' | jq -Sc '.')

FALLBACK_FILE=""

for req_file in "$QUEUE_DIR"/*.request.json; do
  [ -f "$req_file" ] || continue
  REQ_NAME=$(jq -r '.tool_name // ""' "$req_file")
  [ "$REQ_NAME" = "$TOOL_NAME" ] || continue

  REQ_INPUT=$(jq -Sc '.tool_input // {}' "$req_file")
  if [ "$REQ_INPUT" = "$TOOL_INPUT" ]; then
    # Exact match — clean up and exit
    RESP_FILE="${req_file%.request.json}.response.json"
    rm -f "$req_file" "$RESP_FILE"
    exit 0
  fi

  # Remember first tool_name match as fallback (for tools like AskUserQuestion
  # where tool_input may include answers in PostToolUse but not in the request)
  [ -z "$FALLBACK_FILE" ] && FALLBACK_FILE="$req_file"
done

# No exact match — use fallback (tool_name-only match)
if [ -n "$FALLBACK_FILE" ]; then
  RESP_FILE="${FALLBACK_FILE%.request.json}.response.json"
  rm -f "$FALLBACK_FILE" "$RESP_FILE"
fi
