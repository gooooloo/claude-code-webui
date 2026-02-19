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
- [x] **Bug: Web UI submitted prompt not delivered to Claude**
  - Root cause: `systemMessage` in stop hooks is only a warning display, not a prompt injection. Claude reads the `reason` field instead.
  - Fix: moved the user's prompt into the `reason` field of the stop hook response.
- [x] **Show Claude's last response in the prompt-waiting card**
  - When the Web UI displays a "Claude is ready" waiting card, it currently only shows a text input for the next prompt.
  - The user needs to see what Claude just did/said in order to decide what to instruct next, but this context is only visible in the terminal.
  - Pass Claude's last response (the stop hook's stdin or relevant context) through to the waiting card, and render it above the prompt input area.
  - This makes the remote workflow fully self-contained: the user can read Claude's output and respond without switching back to the terminal.
- [x] **Add "Allow Path" button for Write/Edit tools**
  - When the requested tool is `Write` or `Edit`, show a new button on the approval card to configure always-allow path rules
  - Clicking the button opens a path selection interaction:
    - Automatically generate hierarchical directory options from the project root to the target file path (e.g., `/project/` → `/project/src/` → `/project/src/components/`)
    - User can select a directory level to allow all file modifications within that directory and its subdirectories
    - Also supports manual input/editing of custom paths
  - Once a path is selected, subsequent Write/Edit requests under that path are auto-approved without further review
  - Purpose: Give users granular control over which directories allow automatic file modifications, rather than a blanket global allow
- [x] **Avoid exposing absolute paths (and usernames) in settings.json hooks**
  - Problem: The `install.sh` script writes hardcoded absolute paths (e.g., `/Users/john/projects/claude-code-permission-web-approver/approve-dialog.sh`) into Claude Code's `settings.json`. This leaks the user's filesystem layout and potentially their system username when the git repo is shared or committed.
  - Need to find an approach that avoids embedding user-specific absolute paths in any file that might be checked into version control or shared.
  - Possible directions to explore:
    - Use a symlink or wrapper installed to a well-known location (e.g., `~/.local/bin/`) so the hook path is generic
    - Use an environment variable (e.g., `$CLAUDE_APPROVER_HOME`) that resolves at runtime
    - Have the hook script resolve its own location dynamically rather than relying on a hardcoded path
  - Goal: A user can clone the repo, run install, and use the hook without their personal file paths appearing in any shared/committed configuration
- [x] **Increase default visible height for Plan detail view**
  - Problem: The collapsed `.detail` area currently has `max-height: 120px` (~6 lines on desktop, ~4 lines on mobile at 80px), which is almost never enough to read a plan without expanding.
  - Increase the default collapsed height so that at least 15-20 lines are visible without clicking "Show more" (e.g., `max-height: 360px` desktop / `240px` mobile).
  - Plans are typically long and important for decision-making; users should be able to scan most of a plan at a glance.
  - Consider whether plan cards specifically should have a larger default than other card types, or if the increase should apply globally.
- [x] **Fix mobile button area height when card has 4 buttons**
  - On mobile, when a card has 4 buttons (e.g., Deny / Always Allow / Allow this session / Allow), the button area doesn't have enough height and buttons may overlap or get cut off.
  - Ensure the `.buttons` container wraps properly on small screens and all buttons remain fully visible and tappable.
- [x] **Split "Always Allow" for compound Bash commands (pipes and &&)**
  - When a Bash command contains pipes (`|`) or `&&`, the current "Always Allow" button only creates a single allow pattern for the first command.
  - Instead, parse the compound command and offer individual "Always Allow" entries for each sub-command.
  - Example: `foo xxx | bar xxx` → show two allow options: `Bash(foo:*)` and `Bash(bar:*)`.
  - Example: `npm run build && npm test` → show two allow options: `Bash(npm run:*)` and `Bash(npm test:*)`.
  - This gives users finer-grained control and avoids needing to re-approve each sub-command separately in future requests.
- [ ] **Investigate: feed prompts to idle Claude Code via stdio instead of stop hook**
  - Currently, the stop hook blocks Claude from stopping and injects a prompt via `systemMessage`. This approach has issues: multiple hook instances can accumulate, and there's inherent complexity in managing the hook lifecycle.
  - Investigate whether it's possible to send instructions directly to an idle/waiting Claude Code process via its stdin (stdio), bypassing the stop hook entirely.
  - If feasible, this could simplify the architecture: instead of blocking the stop event, just let Claude stop normally and then pipe a new prompt into its stdin when the user submits one from the Web UI.
  - Research areas: how Claude Code reads stdin, whether it accepts input when in the idle/prompt-waiting state, and whether there are any APIs or IPC mechanisms that could be leveraged.
- [ ] *(on hold)* **Add "Clear Context and Edit" shortcut (Shift+Tab) to ExitPlanMode card**
  - When an ExitPlanMode approval card is shown in the Web UI, add a keyboard shortcut (Shift+Tab) or button that triggers the "Clear context and edit" action.
  - This mirrors the Shift+Tab behavior available in the Claude Code CLI terminal.
  - Allows the user to clear the current context and re-edit the plan directly from the Web UI without switching back to the terminal.
- [x] **Update quick-action buttons: replace "Push to GitHub" with "Clean up this task" and reorder**
  - Replace the "Push to GitHub" button with "Clean up this task" (prompt: "Commit the current changes and push to GitHub")
  - Reorder buttons to: "Clean up this task" first, then "/clear", then "Next TODO"
  - "Clean up this task" combines commit + push into one action (prompt: "Commit the current changes and push"), which is more useful as a default workflow step
- [x] **Fix last line clipping in response textbox on prompt-waiting card**
  - In the Web UI prompt-waiting card, the response textbox (showing Claude's last response) has a display issue where the last line of text appears partially cut off or clipped by margin/padding.
  - The bottom of the last line is visually obscured, likely caused by insufficient padding-bottom or margin interference in the response container.
  - Ensure the last line of the response text is fully visible with proper spacing at the bottom.
- [x] **Hide "Show more" button when detail content fits without overflow**
  - Currently the "Show more" toggle and gradient overlay always appear on collapsed `.detail` areas, even when the content is short enough to fit within the max-height.
  - Only show the "Show more" button and the fade gradient when the content actually overflows the collapsed container.
  - Use `scrollHeight > clientHeight` (or similar) to detect overflow and conditionally apply the collapsed state.
- [x] **Investigate: support sending images to Claude Code from Web UI**
  - Research how Claude Code accepts image input (e.g., via stdin, file paths, base64-encoded data, or CLI flags).
  - Explore whether the stop hook or prompt submission mechanism can pass image data alongside text prompts.
  - If feasible, design a workflow: user uploads/pastes an image in the Web UI prompt area, and it gets forwarded to Claude Code as part of the next instruction.
  - Consider mobile use cases (camera capture, photo library) as a primary motivation for this feature.
  - **Finding:** Claude Code CLI does not support images via stdin, hooks, or CLI flags. Only interactive mode supports images (drag-drop, Ctrl+V, file path references). The stop hook `reason` field is text-only.
  - **Workaround:** Web UI uploads image → Python server saves to `/tmp/claude-images/` → prompt text prepends file path reference (e.g., "Please look at this image: /tmp/claude-images/xxx.png") → Claude Code uses Read tool to view the image.
- [x] **Implement image upload in Web UI prompt area**
  - Add image upload button and paste handler to the prompt-waiting card
  - Server endpoint to receive and save uploaded images to `/tmp/claude-images/`
  - Prepend file path reference to the user's text prompt before submitting
  - Support mobile use cases (camera capture, photo library upload)
- [ ] *(on hold)* **Add TODO management in Web UI independent of Claude Code session**
  - Problem: Currently adding TODOs to PRD.md requires going through Claude Code, which means the user cannot add new ideas while Claude is busy working on a task.
  - Add a TODO management section in the Web UI where users can add, view, and reorder TODO items at any time, regardless of whether Claude Code is idle or busy.
  - Possible approaches:
    - Web UI directly edits PRD.md (append new TODOs to the file) without involving Claude Code
    - A separate TODO storage (e.g., a JSON file) that Claude reads when starting a new task
    - A dedicated API endpoint on the approval server for CRUD operations on TODOs
  - The key requirement is decoupling: adding a TODO should never block on or interfere with an in-progress Claude Code session.
- [x] **Increase spacing between "Show more" button and quick-action buttons (e.g., /clear)**
  - On mobile especially, the "Show more" toggle and quick-action buttons like "/clear" are too close together, leading to accidental taps.
  - Add more vertical margin/padding between the detail section (including its "Show more" button) and the quick-action button area to prevent mis-taps.
- [x] **Bug: "Allow Path" button click has no effect in Web UI**
  - When clicking the "Allow Path" option on Write/Edit tool approval cards, nothing happens.
  - Investigate why the click handler is not working and fix the issue.
- [x] **Bug: Fetch permission requests not displayed in Web UI**
  - When Claude Code requests permission to fetch a URL (e.g., WebFetch), the approval prompt appears in the terminal but not in the Web UI.
  - Investigate whether the Fetch tool goes through the permission hook system and ensure it is rendered as a card in the Web UI.
  - Added explicit WebFetch/WebSearch handling in approve-dialog.sh and Web UI (blue 'web' category). If the hook is still not triggered, the issue may be in Claude Code's permission routing.
