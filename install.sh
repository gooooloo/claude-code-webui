#!/bin/bash
# Installer for Claude Code WebUI hooks
#
# Run this script from the root of a project to install the web-approval hooks.
# It does two things:
#   1. Creates symlinks in ~/.claude/hooks/ pointing to the hook scripts in this repo
#   2. Merges (or creates) .claude/settings.json in the project with hook configuration
#      that tells Claude Code to call these scripts on PermissionRequest, PostToolUse,
#      Stop, UserPromptSubmit, SessionStart, and SessionEnd events
#
# Usage: /path/to/install.sh   (run from project root)
# Deps:  jq

set -e

SHARED_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(pwd)"
SETTINGS_FILE="$PROJECT_DIR/.claude/settings.json"
HOOKS_DIR="$HOME/.claude/hooks"

# Create symlinks in ~/.claude/hooks/ so settings.json doesn't contain user-specific paths
mkdir -p "$HOOKS_DIR"
for script in permission-request.sh post-tool-use.sh stop.sh user-prompt-submit.sh session-start.sh session-end.sh; do
  ln -sf "$SHARED_DIR/$script" "$HOOKS_DIR/$script"
done
echo "Symlinked hooks to: $HOOKS_DIR"

# Use $HOME in commands so settings.json is portable (no hardcoded username/paths)
HOOKS_CONFIG='{
  "PermissionRequest": [
    {
      "matcher": ".*",
      "hooks": [
        {
          "type": "command",
          "command": "bash \"$HOME/.claude/hooks/permission-request.sh\"",
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
          "command": "bash \"$HOME/.claude/hooks/post-tool-use.sh\"",
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
          "command": "bash \"$HOME/.claude/hooks/stop.sh\"",
          "timeout": 86400
        }
      ]
    }
  ],
  "UserPromptSubmit": [
    {
      "matcher": ".*",
      "hooks": [
        {
          "type": "command",
          "command": "bash \"$HOME/.claude/hooks/user-prompt-submit.sh\"",
          "timeout": 5
        }
      ]
    }
  ],
  "SessionStart": [
    {
      "matcher": ".*",
      "hooks": [
        {
          "type": "command",
          "command": "bash \"$HOME/.claude/hooks/session-start.sh\"",
          "timeout": 5
        }
      ]
    }
  ],
  "SessionEnd": [
    {
      "matcher": ".*",
      "hooks": [
        {
          "type": "command",
          "command": "bash \"$HOME/.claude/hooks/session-end.sh\"",
          "timeout": 5
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

echo "WebUI hooks installed for: $PROJECT_DIR"
echo "Start the server: python3 $SHARED_DIR/server.py"
echo "Then open: http://localhost:19836"
