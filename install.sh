#!/bin/bash
# Install Claude Code approval hooks into the current project
# Usage: ~/.claude/claude-code-permission-web-approver/install.sh

set -e

SHARED_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(pwd)"
SETTINGS_FILE="$PROJECT_DIR/.claude/settings.json"

HOOKS_CONFIG='{
  "PermissionRequest": [
    {
      "matcher": ".*",
      "hooks": [
        {
          "type": "command",
          "command": "'"$SHARED_DIR"'/approve-dialog.sh",
          "timeout": 86400
        }
      ]
    }
  ],
  "PostToolUse": [
    {
      "matcher": ".*",
      "hooks": [
        {
          "type": "command",
          "command": "'"$SHARED_DIR"'/post-cleanup.sh",
          "timeout": 5
        }
      ]
    }
  ],
  "Stop": [
    {
      "matcher": "*",
      "hooks": [
        {
          "type": "command",
          "command": "'"$SHARED_DIR"'/stop-hook.sh",
          "timeout": 86400
        }
      ]
    }
  ]
}'

mkdir -p "$PROJECT_DIR/.claude"

if [ -f "$SETTINGS_FILE" ]; then
  # Merge hooks into existing settings
  EXISTING=$(cat "$SETTINGS_FILE")
  echo "$EXISTING" | jq --argjson hooks "$HOOKS_CONFIG" '.hooks = $hooks' > "$SETTINGS_FILE.tmp" && mv "$SETTINGS_FILE.tmp" "$SETTINGS_FILE"
  echo "Updated: $SETTINGS_FILE"
else
  # Create new settings file
  echo "$HOOKS_CONFIG" | jq '{hooks: .}' > "$SETTINGS_FILE"
  echo "Created: $SETTINGS_FILE"
fi

echo "Approval hooks installed for: $PROJECT_DIR"
echo "Start the server: python3 $SHARED_DIR/approval-server.py"
echo "Then open: http://localhost:19836"
