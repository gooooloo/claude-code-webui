#!/bin/bash
# Installer for Claude Code WebUI hooks (new architecture)
#
# Installs 3 Python hook scripts (PermissionRequest, SessionStart, SessionEnd).
# No external dependencies (no jq, curl — hooks are pure Python).
#
# Scopes:
#   --project  Install hooks into <cwd>/.claude/settings.json (project-level only)
#   --global   Install hooks into ~/.claude/settings.json + create symlinks in ~/.claude/hooks/
#   --all      Do both --project and --global
#
# Usage: /path/to/install.sh --project|--global|--all
# Deps:  jq (for settings.json manipulation only)

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
          "command": "python3 \"$HOME/.claude/hooks/permission-request.py\"",
          "timeout": 86400
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
          "command": "python3 \"$HOME/.claude/hooks/session-start.py\"",
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
          "command": "python3 \"$HOME/.claude/hooks/session-end.py\"",
          "timeout": 5
        }
      ]
    }
  ]
}'

install_symlinks() {
  mkdir -p "$HOOKS_DIR"
  # Also remove old .sh symlinks if they exist
  for old_script in permission-request.sh post-tool-use.sh stop.sh user-prompt-submit.sh session-start.sh session-end.sh; do
    [ -L "$HOOKS_DIR/$old_script" ] && rm -f "$HOOKS_DIR/$old_script"
  done
  for script in permission-request.py session-start.py session-end.py; do
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
