#!/bin/bash
# UserPromptSubmit hook for Claude Code
# Triggered when the user submits a prompt (either via terminal or tmux send-keys).
# Cleans up any .prompt-waiting.json files belonging to this session,
# so the Web UI card disappears once Claude starts processing.

QUEUE_DIR="/tmp/claude-approvals"

# Session ID is the PPID (the Claude Code process)
SESSION_ID="$PPID"

for waiting_file in "$QUEUE_DIR"/*.prompt-waiting.json; do
  [ -f "$waiting_file" ] || continue
  file_sid=$(jq -r '.session_id // ""' "$waiting_file" 2>/dev/null)
  if [ "$file_sid" = "$SESSION_ID" ]; then
    rm -f "$waiting_file"
  fi
done
