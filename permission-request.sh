#!/bin/bash
# Async permission hook for Claude Code
# Writes request to queue directory, polls for response from web UI

QUEUE_DIR="/tmp/claude-approvals"
mkdir -p "$QUEUE_DIR"

# Settings file is in the project's .claude/ directory, not the shared hooks dir
PROJECT_DIR="$(pwd)"
SETTINGS_FILE="$PROJECT_DIR/.claude/settings.local.json"

INPUT=$(cat)

TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // "Unknown"')
TOOL_INPUT=$(echo "$INPUT" | jq -r '.tool_input // {}')

# Build detail text and allow pattern per tool type
ALLOW_PATTERNS_JSON="[]"
case "$TOOL_NAME" in
  Bash|mcp__acp__Bash)
    COMMAND=$(echo "$TOOL_INPUT" | jq -r '.command // ""')
    DETAIL="$COMMAND"
    DETAIL_SUB=""
    # Parse compound commands (pipes and &&) into individual allow patterns
    # Split on | and && to get individual commands
    echo "$COMMAND" | sed 's/&&/\n/g; s/|/\n/g' | while IFS= read -r SUBCMD; do
      SUBCMD=$(echo "$SUBCMD" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
      [ -z "$SUBCMD" ] && continue
      SCMD_FIRST=$(echo "$SUBCMD" | head -1)
      SCMD_BASE=$(echo "$SCMD_FIRST" | awk '{print $1}' | xargs basename 2>/dev/null)
      [ -z "$SCMD_BASE" ] && continue
      SCMD_SUB=$(echo "$SCMD_FIRST" | tr ' ' '\n' | tail -n +2 | grep -v '^[-/\.]' | head -1)
      if [ -n "$SCMD_SUB" ]; then
        PAT="Bash($SCMD_BASE $SCMD_SUB:*)"
      else
        PAT="Bash($SCMD_BASE:*)"
      fi
      echo "$PAT"
    done | sort -u > /tmp/claude_patterns_$$
    PATTERNS_ARRAY=$(jq -R -s 'split("\n") | map(select(length > 0))' < /tmp/claude_patterns_$$)
    rm -f /tmp/claude_patterns_$$
    ALLOW_PATTERNS_JSON="$PATTERNS_ARRAY"
    # First pattern is the primary allow_pattern
    ALLOW_PATTERN=$(echo "$PATTERNS_ARRAY" | jq -r '.[0] // ""')
    ;;
  Write|mcp__acp__Write)
    FILE=$(echo "$TOOL_INPUT" | jq -r '.file_path // ""')
    DETAIL="$FILE"
    DETAIL_SUB=""
    ALLOW_PATTERN="Write($FILE)"
    ;;
  Edit|mcp__acp__Edit)
    FILE=$(echo "$TOOL_INPUT" | jq -r '.file_path // ""')
    OLD=$(echo "$TOOL_INPUT" | jq -r '.old_string // ""' | head -5)
    DETAIL="$FILE"
    DETAIL_SUB="$OLD"
    ALLOW_PATTERN="Edit($FILE)"
    ;;
  ExitPlanMode)
    # Plan markdown is in tool_input.plan
    PLAN_CONTENT=$(echo "$TOOL_INPUT" | jq -r '.plan // empty' 2>/dev/null)
    if [ -n "$PLAN_CONTENT" ]; then
      DETAIL="$PLAN_CONTENT"
    else
      DETAIL="Exit plan mode"
    fi
    # allowedPrompts as subtitle
    DETAIL_SUB=$(echo "$TOOL_INPUT" | jq -r '
      if .allowedPrompts and (.allowedPrompts | length > 0) then
        "Requested permissions: " +
        ([.allowedPrompts[] | "\(.tool): \(.prompt)"] | join(", "))
      else
        empty
      end' 2>/dev/null)
    ALLOW_PATTERN="ExitPlanMode"
    ;;
  AskUserQuestion)
    # Format questions with their options
    DETAIL=$(echo "$TOOL_INPUT" | jq -r '
      if .questions then
        [.questions[] |
          "Q: \(.question)\n" +
          ([.options[] | "  • \(.label) — \(.description)"] | join("\n"))
        ] | join("\n\n")
      else
        empty
      end' 2>/dev/null)
    if [ -z "$DETAIL" ]; then
      # Fallback: try single question field
      DETAIL=$(echo "$TOOL_INPUT" | jq -r '.questions[0].question // empty' 2>/dev/null)
    fi
    if [ -z "$DETAIL" ]; then
      # Last resort: raw dump
      DETAIL=$(echo "$TOOL_INPUT" | jq -r 'to_entries | map("\(.key): \(.value)") | join("\n")' 2>/dev/null | head -10)
    fi
    DETAIL_SUB=""
    ALLOW_PATTERN="AskUserQuestion"
    ;;
  WebFetch)
    URL=$(echo "$TOOL_INPUT" | jq -r '.url // ""')
    PROMPT_TEXT=$(echo "$TOOL_INPUT" | jq -r '.prompt // ""')
    DETAIL="$URL"
    DETAIL_SUB="$PROMPT_TEXT"
    ALLOW_PATTERN="WebFetch"
    ;;
  WebSearch)
    QUERY=$(echo "$TOOL_INPUT" | jq -r '.query // ""')
    DETAIL="$QUERY"
    DETAIL_SUB=""
    ALLOW_PATTERN="WebSearch"
    ;;
  *)
    DETAIL=$(echo "$TOOL_INPUT" | jq -r 'to_entries | map("\(.key): \(.value)") | join("\n")' 2>/dev/null | head -10)
    DETAIL_SUB=""
    ALLOW_PATTERN="$TOOL_NAME"
    ;;
esac

# ── Auto-allow check: match against settings.local.json allow rules ──
# Claude Code caches settings at startup, so patterns added by the Web UI
# during a session are not picked up. We check them here in the hook instead.
if [ -f "$SETTINGS_FILE" ]; then
  ALLOW_LIST=$(jq -r '.permissions.allow[]? // empty' "$SETTINGS_FILE" 2>/dev/null)
  if [ -n "$ALLOW_LIST" ]; then
    AUTO_ALLOWED=false
    while IFS= read -r pattern; do
      [ -z "$pattern" ] && continue
      if [ "$pattern" = "$TOOL_NAME" ]; then
        # Exact tool name match (e.g., "Read", "WebSearch")
        AUTO_ALLOWED=true
        break
      fi
      # Check if pattern starts with "ToolName(" and ends with ")"
      case "$pattern" in
        "${TOOL_NAME}("*")")
          # Extract the inner part between parentheses
          inner="${pattern#${TOOL_NAME}(}"
          inner="${inner%)}"
          # Match DETAIL (file path or command) against the inner pattern
          # Claude Code uses ":*" suffix as "starts with" (e.g., "git status:*")
          # Convert ":*" to just "*" for bash glob matching
          glob_inner="${inner//:*/*}"
          if [[ "$DETAIL" == $glob_inner ]]; then
            AUTO_ALLOWED=true
            break
          fi
          ;;
      esac
    done <<< "$ALLOW_LIST"

    if [ "$AUTO_ALLOWED" = true ]; then
      jq -n '{
        hookSpecificOutput: {
          hookEventName: "PermissionRequest",
          decision: { behavior: "allow" }
        }
      }'
      exit 0
    fi
  fi
fi

# Check if the approval server is running before waiting for approval
if ! curl -s --max-time 2 http://localhost:19836/ > /dev/null 2>&1; then
  # Server not running, allow directly to avoid blocking Claude
  jq -n '{
    hookSpecificOutput: {
      hookEventName: "PermissionRequest",
      decision: { behavior: "allow" }
    }
  }'
  exit 0
fi

# Generate unique request ID
REQUEST_ID=$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid 2>/dev/null || date +%s%N)
REQUEST_FILE="$QUEUE_DIR/$REQUEST_ID.request.json"
RESPONSE_FILE="$QUEUE_DIR/$REQUEST_ID.response.json"

# Clean up request file on exit (e.g. if Claude Code kills this hook process)
trap 'rm -f "$REQUEST_FILE"' EXIT

# Write request to queue
jq -n \
  --arg id "$REQUEST_ID" \
  --arg tool_name "$TOOL_NAME" \
  --arg tool_input "$TOOL_INPUT" \
  --arg detail "$DETAIL" \
  --arg detail_sub "$DETAIL_SUB" \
  --arg allow_pattern "$ALLOW_PATTERN" \
  --argjson allow_patterns "$ALLOW_PATTERNS_JSON" \
  --arg settings_file "$SETTINGS_FILE" \
  --arg timestamp "$(date +%s)" \
  --arg pid "$$" \
  --arg session_id "$PPID" \
  --arg project_dir "$PROJECT_DIR" \
  '{
    id: $id,
    tool_name: $tool_name,
    tool_input: ($tool_input | fromjson? // {}),
    detail: $detail,
    detail_sub: $detail_sub,
    allow_pattern: $allow_pattern,
    allow_patterns: $allow_patterns,
    settings_file: $settings_file,
    timestamp: ($timestamp | tonumber),
    pid: ($pid | tonumber),
    session_id: ($session_id | tonumber),
    project_dir: $project_dir
  }' > "$REQUEST_FILE"

# Poll for response
TIMEOUT=86400
ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
  if [ -f "$RESPONSE_FILE" ]; then
    DECISION=$(jq -r '.decision // "deny"' "$RESPONSE_FILE")
    DENY_MESSAGE=$(jq -r '.message // "User denied via web UI"' "$RESPONSE_FILE")
    # Cleanup
    rm -f "$REQUEST_FILE" "$RESPONSE_FILE"

    if [ "$DECISION" = "allow" ] || [ "$DECISION" = "always" ]; then
      jq -n '{
        hookSpecificOutput: {
          hookEventName: "PermissionRequest",
          decision: { behavior: "allow" }
        }
      }'
    else
      jq -n --arg msg "$DENY_MESSAGE" '{
        hookSpecificOutput: {
          hookEventName: "PermissionRequest",
          decision: {
            behavior: "deny",
            message: $msg
          }
        }
      }'
    fi
    exit 0
  fi
  sleep 0.5
  ELAPSED=$((ELAPSED + 1))
done

# Timeout: cleanup and deny
rm -f "$REQUEST_FILE" "$RESPONSE_FILE"
jq -n '{
  hookSpecificOutput: {
    hookEventName: "PermissionRequest",
    decision: {
      behavior: "deny",
      message: "Approval timed out"
    }
  }
}'
