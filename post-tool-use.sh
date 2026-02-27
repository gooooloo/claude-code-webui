#!/bin/bash
# Cleanup matching request files after tool execution
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
