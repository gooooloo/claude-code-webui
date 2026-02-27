#!/bin/bash
# Uninstaller for Claude Code WebUI hooks
#
# Reverses what install.sh does: removes hook configuration from settings.json
# and (for global scope) removes symlinks from ~/.claude/hooks/.
#
# Scopes:
#   --project  Remove hooks from <cwd>/.claude/settings.json
#   --global   Remove hooks from ~/.claude/settings.json + remove symlinks from ~/.claude/hooks/
#   --all      Do both --project and --global
#
# Usage: /path/to/uninstall.sh --project|--global|--all
# Deps:  jq

set -e

PROJECT_DIR="$(pwd)"
HOOKS_DIR="$HOME/.claude/hooks"

usage() {
  echo "Usage: $0 --project|--global|--all"
  echo ""
  echo "  --project  Remove hooks from <cwd>/.claude/settings.json"
  echo "  --global   Remove hooks from ~/.claude/settings.json + symlinks"
  echo "  --all      Remove both project and global"
  exit 1
}

prompt_scope() {
  echo "Select uninstall scope:"
  echo "  1) project  — Remove hooks from <cwd>/.claude/settings.json"
  echo "  2) global   — Remove hooks from ~/.claude/settings.json + symlinks"
  echo "  3) all      — Remove both project and global"
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

remove_hooks_from_settings() {
  local settings_file="$1"

  if [ ! -f "$settings_file" ]; then
    echo "No settings file found: $settings_file (skipping)"
    return
  fi

  local updated
  updated=$(jq 'del(.hooks)' "$settings_file")

  if [ "$updated" = "{}" ]; then
    rm -f "$settings_file"
    echo "Removed empty: $settings_file"

    # Remove .claude/ dir if empty (only for project-level)
    local settings_dir
    settings_dir="$(dirname "$settings_file")"
    if [ -d "$settings_dir" ] && [ -z "$(ls -A "$settings_dir")" ]; then
      rmdir "$settings_dir"
      echo "Removed empty directory: $settings_dir"
    fi
  else
    echo "$updated" > "$settings_file"
    echo "Removed hooks from: $settings_file"
  fi
}

remove_symlinks() {
  local removed=0
  for script in permission-request.sh post-tool-use.sh stop.sh user-prompt-submit.sh session-start.sh session-end.sh; do
    if [ -L "$HOOKS_DIR/$script" ]; then
      rm -f "$HOOKS_DIR/$script"
      removed=$((removed + 1))
    fi
  done
  echo "Removed $removed symlinks from: $HOOKS_DIR"
}

if [ "$DO_GLOBAL" = true ]; then
  remove_symlinks
  remove_hooks_from_settings "$HOME/.claude/settings.json"
  echo "WebUI hooks uninstalled globally"
fi

if [ "$DO_PROJECT" = true ]; then
  remove_hooks_from_settings "$PROJECT_DIR/.claude/settings.json"
  echo "WebUI hooks uninstalled for: $PROJECT_DIR"
fi
