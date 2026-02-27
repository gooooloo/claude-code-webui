#!/bin/bash
# Installer for Claude Code WebUI hooks
#
# Installs hook configuration and symlinks for the web-approval UI.
# Requires an explicit scope argument — no default behavior.
#
# Scopes:
#   --project  Install hooks into <cwd>/.claude/settings.json (project-level only)
#   --global   Install hooks into ~/.claude/settings.json + create symlinks in ~/.claude/hooks/
#   --all      Do both --project and --global
#
# Usage: /path/to/install.sh --project|--global|--all
# Deps:  jq

set -e

SHARED_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(pwd)"
HOOKS_DIR="$HOME/.claude/hooks"

usage() {
  echo "Usage: $0 --project|--global|--all"
  echo ""
  echo "  --project  Install hooks into <cwd>/.claude/settings.json"
  echo "  --global   Install hooks into ~/.claude/settings.json + symlinks"
  echo "  --all      Install both project and global"
  exit 1
}

prompt_scope() {
  echo "Select install scope:"
  echo "  1) project  — Install hooks into <cwd>/.claude/settings.json"
  echo "  2) global   — Install hooks into ~/.claude/settings.json + symlinks"
  echo "  3) all      — Install both project and global"
  echo ""
  printf "Enter choice [1-3]: "
  read -r choice
  case "$choice" in
    1) DO_PROJECT=true ;;
    2) DO_GLOBAL=true ;;
    3) DO_PROJECT=true; DO_GLOBAL=true ;;
    *) echo "Invalid choice"; exit 1 ;;
  esac
}

DO_PROJECT=false
DO_GLOBAL=false

case "${1:-}" in
  --project) DO_PROJECT=true ;;
  --global)  DO_GLOBAL=true ;;
  --all)     DO_PROJECT=true; DO_GLOBAL=true ;;
  "")        prompt_scope ;;
  *)         usage ;;
esac

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

install_symlinks() {
  mkdir -p "$HOOKS_DIR"
  for script in permission-request.sh post-tool-use.sh stop.sh user-prompt-submit.sh session-start.sh session-end.sh; do
    ln -sf "$SHARED_DIR/$script" "$HOOKS_DIR/$script"
  done
  echo "Symlinked hooks to: $HOOKS_DIR"
}

install_settings() {
  local settings_file="$1"
  local settings_dir
  settings_dir="$(dirname "$settings_file")"

  mkdir -p "$settings_dir"

  if [ -f "$settings_file" ]; then
    EXISTING=$(cat "$settings_file")
    echo "$EXISTING" | jq --argjson hooks "$HOOKS_CONFIG" '.hooks = $hooks' > "$settings_file.tmp" && mv "$settings_file.tmp" "$settings_file"
    echo "Updated: $settings_file"
  else
    echo "$HOOKS_CONFIG" | jq '{hooks: .}' > "$settings_file"
    echo "Created: $settings_file"
  fi
}

if [ "$DO_GLOBAL" = true ]; then
  install_symlinks
  install_settings "$HOME/.claude/settings.json"
  echo "WebUI hooks installed globally (all projects)"
fi

if [ "$DO_PROJECT" = true ]; then
  install_settings "$PROJECT_DIR/.claude/settings.json"
  echo "WebUI hooks installed for: $PROJECT_DIR"
fi

echo "Start the server: python3 $SHARED_DIR/server.py"
echo "Then open: http://localhost:19836"
