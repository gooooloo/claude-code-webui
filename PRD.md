# PRD: Claude Code Permission Web Approver

## Overview

A web-based approval UI for Claude Code permission hooks. Provides a browser interface to approve/deny tool execution requests from Claude Code, replacing the default terminal prompts.

## Goals

- Provide a user-friendly web UI for reviewing and approving Claude Code permission requests
- Support session-level auto-allow for trusted operations
- Enable remote approval workflows (e.g., from a phone or another device)

## Current Features

- Web-based approval/deny interface with request details
- Session-level auto-allow with multi-select support
- AskUserQuestion support with custom text input
- Auto-cleanup of stale requests

## TODO

- [x] **Investigate: auto-allow only triggers when Web UI page is opened/reconnected**
  - Bug: When the Web UI tab is not open (or closed), Claude Code keeps waiting for approval indefinitely, even for requests that were previously marked as "always allow in this session".
  - However, as soon as the Web UI is opened (e.g., from a phone), the auto-allowed request is immediately approved.
  - This suggests the auto-allow logic only runs on client connection/page load rather than being evaluated server-side when the request first arrives.
  - Expected behavior: Session-level auto-allow rules should be evaluated server-side immediately when a new request comes in, regardless of whether any browser client is connected.
- [x] **Hook into "waiting for input" state and enable prompt submission from Web UI**
  - Extend the hook to detect when Claude Code has finished a task and is idle, waiting for the next user instruction (the prompt input state).
  - When this state is detected, show a notification card in the Web UI informing the user that Claude Code is ready for a new instruction.
  - Provide a text input in the Web UI so the user can type and submit the next prompt directly from the browser, without switching back to the terminal.
  - This enables a fully remote workflow where users can monitor task completion and issue follow-up instructions entirely from the Web UI.
- [ ] **Add "Allow Path" button for Write/Edit tools**
  - When the requested tool is `Write` or `Edit`, show a new button on the approval card to configure always-allow path rules
  - Clicking the button opens a path selection interaction:
    - Automatically generate hierarchical directory options from the project root to the target file path (e.g., `/project/` → `/project/src/` → `/project/src/components/`)
    - User can select a directory level to allow all file modifications within that directory and its subdirectories
    - Also supports manual input/editing of custom paths
  - Once a path is selected, subsequent Write/Edit requests under that path are auto-approved without further review
  - Purpose: Give users granular control over which directories allow automatic file modifications, rather than a blanket global allow
- [ ] **Avoid exposing absolute paths (and usernames) in settings.json hooks**
  - Problem: The `install.sh` script writes hardcoded absolute paths (e.g., `/Users/john/projects/claude-code-permission-web-approver/approve-dialog.sh`) into Claude Code's `settings.json`. This leaks the user's filesystem layout and potentially their system username when the git repo is shared or committed.
  - Need to find an approach that avoids embedding user-specific absolute paths in any file that might be checked into version control or shared.
  - Possible directions to explore:
    - Use a symlink or wrapper installed to a well-known location (e.g., `~/.local/bin/`) so the hook path is generic
    - Use an environment variable (e.g., `$CLAUDE_APPROVER_HOME`) that resolves at runtime
    - Have the hook script resolve its own location dynamically rather than relying on a hardcoded path
  - Goal: A user can clone the repo, run install, and use the hook without their personal file paths appearing in any shared/committed configuration
- [ ] **Increase default visible height for Plan detail view**
  - Problem: The collapsed `.detail` area currently has `max-height: 120px` (~6 lines on desktop, ~4 lines on mobile at 80px), which is almost never enough to read a plan without expanding.
  - Increase the default collapsed height so that at least 15-20 lines are visible without clicking "Show more" (e.g., `max-height: 360px` desktop / `240px` mobile).
  - Plans are typically long and important for decision-making; users should be able to scan most of a plan at a glance.
  - Consider whether plan cards specifically should have a larger default than other card types, or if the increase should apply globally.
- [ ] **Fix mobile button area height when card has 4 buttons**
  - On mobile, when a card has 4 buttons (e.g., Deny / Always Allow / Allow this session / Allow), the button area doesn't have enough height and buttons may overlap or get cut off.
  - Ensure the `.buttons` container wraps properly on small screens and all buttons remain fully visible and tappable.
- [ ] **Split "Always Allow" for compound Bash commands (pipes and &&)**
  - When a Bash command contains pipes (`|`) or `&&`, the current "Always Allow" button only creates a single allow pattern for the first command.
  - Instead, parse the compound command and offer individual "Always Allow" entries for each sub-command.
  - Example: `foo xxx | bar xxx` → show two allow options: `Bash(foo:*)` and `Bash(bar:*)`.
  - Example: `npm run build && npm test` → show two allow options: `Bash(npm run:*)` and `Bash(npm test:*)`.
  - This gives users finer-grained control and avoids needing to re-approve each sub-command separately in future requests.
