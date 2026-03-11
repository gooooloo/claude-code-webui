# Prompt Delivery: Failure Modes Analysis

This document analyzes why prompts sent from the WebUI sometimes fail to reach Claude Code. Covers both Linux/macOS (tmux) and Windows (WriteConsoleInputW) delivery paths.

## Delivery Architecture

```
User types in WebUI
  │
  ├─► Frontend: POST /api/send-prompt {session_id, prompt}
  │     │
  │     ▼
  ├─► Server: send_prompt(session_info, prompt_text)
  │     │
  │     ├─► Linux/macOS: _send_prompt_tmux()
  │     │     1. tmux load-buffer -       (stdin → tmux buffer)
  │     │     2. tmux paste-buffer -t %N   (buffer → target pane)
  │     │     3. tmux send-keys -t %N Enter
  │     │
  │     └─► Windows: _send_prompt_windows()
  │           subprocess → win_send_keys.py <console_pid> <text>
  │             1. FreeConsole()
  │             2. AttachConsole(target_pid)
  │             3. WriteConsoleInputW(key events for each char + \r)
  │
  ▼
Claude Code receives text + Enter on stdin
```

---

## Cross-Platform Issues

### P1. Frontend does not check HTTP response status

**Location:** frontend.py:1623–1631

```javascript
await fetch('/api/send-prompt', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({session_id: currentSessionId, prompt})
});
// ← response.ok / status code never checked
```

If the server returns 500 ("Failed to send prompt"), the error is only logged to `console.error`. The user sees no indication that the prompt was not delivered.

There are actually **three** call sites with this issue:
- `sendPrompt()` (frontend.py:1623–1631) — logs to `console.error`
- `quickPrompt()` (frontend.py:1641–1647) — **empty catch block** `catch (e) {}`
- `sendDashboardPrompt()` (frontend.py:1667–1675) — logs to `console.error`

**Impact:** Every delivery failure on the backend is invisible to the user. This is the #1 reason users think "it sent but nothing happened".

### P2. Server does not verify session state before delivery

**Location:** server.py `POST /api/send-prompt` handler

The handler checks `session_id` and `prompt` are present, finds the session, and calls `send_prompt()`. It does **not** verify `derived_state == "idle"`.

The frontend hides the input box for non-idle sessions, but there's a race window:
1. Frontend polls → session is idle → shows input box
2. Claude Code starts processing → session transitions to busy
3. User presses Send → prompt delivered to a busy terminal

The pasted text mingles with ongoing output. The Enter key may be consumed by whatever Claude Code is currently doing, or it may inject a spurious user message mid-tool-execution.

**Impact:** Medium — depends on timing. More likely with slow poll intervals or when Claude Code transitions quickly.

---

## Linux/macOS Issues (Tmux)

### T1. Three-step tmux commands never check return codes

**Location:** platform_utils.py:345–363

```python
subprocess.run(cmd_base + ["load-buffer", "-"], input=prompt.encode(), ...)
subprocess.run(cmd_base + ["paste-buffer", "-t", pane, "-d"], ...)
subprocess.run(cmd_base + ["send-keys", "-t", pane, "Enter"], ...)
return True  # ← always True unless an exception is raised
```

If any of the three commands fails (returncode != 0), the function still returns `True`. The server logs `[>] Prompt sent` and returns `{"ok": true}` to the frontend. Possible failure scenarios:

- `load-buffer` fails (tmux server issue) → `paste-buffer` pastes stale/empty buffer → wrong text or nothing delivered
- `paste-buffer` fails (pane closed/invalid) → `send-keys Enter` sends a bare Enter to nothing
- `send-keys` fails (pane gone) → text was pasted but no Enter

**Impact:** High — the most common cause of silent prompt loss on Linux/macOS.

### T2. Tmux buffer is global — concurrent sends race

**Location:** platform_utils.py:347–354

`load-buffer -` writes to the **default** (unnamed) tmux paste buffer, which is shared across all panes on the same tmux server. The three commands are not atomic.

**Race scenario (two prompts to two sessions on the same tmux server):**

```
Thread A: load-buffer "hello"     →  buffer = "hello"
Thread B: load-buffer "world"     →  buffer = "world"  (overwrites!)
Thread A: paste-buffer -t %1      →  pastes "world" into pane %1  (WRONG!)
Thread B: paste-buffer -t %2      →  buffer already deleted by -d flag  (EMPTY!)
```

In practice this requires near-simultaneous sends — unlikely from a single user, but possible when the Feishu channel and WebUI send concurrently, or when the user sends prompts to multiple sessions rapidly.

**Impact:** Low in single-user scenarios, medium with Feishu channel active.

### T3. Stale pane ID after session restart

If Claude Code restarts but the SessionStart hook fails to register (server offline, hook error), the server retains the old `tmux_pane`. The old pane may have been closed or reused by another process. `paste-buffer` and `send-keys` target the wrong pane.

**Impact:** Low — typically the hook succeeds and updates the pane info.

### T4. Bracket paste mode interference

Some shell configurations enable bracket paste mode. When tmux `paste-buffer` is used, the pasted text is wrapped in `\e[200~...\e[201~` escape sequences. If Claude Code's Node.js readline layer doesn't handle bracket paste correctly, the prompt text may be corrupted or silently discarded.

**Impact:** Unknown — depends on the terminal and Node.js version.

---

## Windows Issues (WriteConsoleInputW)

### W1. Console input buffer overflow silently truncates long prompts

**Location:** win_send_keys.py:66–103

```python
full_text = text + "\r"          # e.g., 500 chars → 501 chars
records = []                     # → 1002 INPUT_RECORDs (key_down + key_up per char)
# ... build records ...
success = kernel32.WriteConsoleInputW(handle, arr, len(records), ctypes.byref(written))
return written.value > 0         # ← only checks that at least 1 record was written!
```

The Windows console input buffer has a finite capacity. If the buffer can only accept N records out of M total:
- `success` is True (the call itself succeeded)
- `written.value = N` (partial write)
- The function returns `True` (N > 0)

The `\r` (Enter) is the **last** record in the array. If the buffer is too full to accept all records, the Enter is never written. The user sees: partial text appears in the terminal, but no Enter, so Claude Code never processes it.

For a 500-character prompt, that's 1002 records. If the buffer only holds 512 records (a common default), only the first 256 characters are written, and Enter is dropped.

**Impact:** High — this is very likely the most common Windows failure mode. Any prompt longer than ~256 characters is at risk.

### W2. Auto-discovered sessions cannot receive prompts

**Location:** platform_utils.py:312–316, server.py:856–869

```python
if IS_WINDOWS:
    console_pid = session_info.get("console_pid")
    if console_pid:
        return _send_prompt_windows(console_pid, prompt_text)
    return False  # ← auto-discovered sessions hit this
```

Windows sessions discovered by `_scan_sessions_from_transcripts()` have `console_pid = ""` because the transcript file scan cannot determine which console hosts the process. `send_prompt()` returns False immediately.

The session appears in the UI with an input box (the frontend shows input for idle sessions regardless of platform capability), but every send fails. Combined with P1 (frontend ignores errors), the user types, presses Send, and nothing happens — with no error message.

**Impact:** High — affects all Windows sessions that haven't been registered by the hook.

### W3. `AttachConsole` is mutually exclusive

**Location:** win_send_keys.py:27

`AttachConsole(target_pid)` attaches the calling process to the target's console. Only **one** external process can be attached to a given console at any time.

If two prompts are sent nearly simultaneously (e.g., Feishu + WebUI, or rapid sends from the dashboard), the server spawns two `win_send_keys.py` subprocesses. The second `AttachConsole` fails because the first is still attached.

**Impact:** Low — requires near-simultaneous sends.

### W4. Prompt passed via command line argument

**Location:** platform_utils.py:323–326

```python
result = subprocess.run(
    [sys.executable, "win_send_keys.py", str(console_pid), text],
    ...
)
```

The prompt is passed as `sys.argv[2]`. This has two issues:

1. **Length limit:** Windows `CreateProcessW` has a ~32767 character command line limit. Prompts exceeding this (rare but possible with image references or long context) cause the subprocess to fail.

2. **Encoding:** Special characters in the prompt (quotes, backslashes, Unicode) pass through Python's subprocess argument encoding. While `CreateProcessW` handles Unicode, the quoting logic for special characters (especially `"` and `\`) may mangle the prompt.

**Impact:** Low for normal prompts, medium for very long or specially-crafted prompts.

### W5. `\n` in multi-line prompts is not Enter on Windows

**Location:** win_send_keys.py:66–88

```python
full_text = text + "\r"
for ch in full_text:
    rec_down.Event.KeyEvent.uChar = ch
```

Each character is injected as a `KEY_EVENT` with its Unicode value. If the prompt contains `\n` (0x0A), it's injected as character code 0x0A. But Windows consoles use `\r` (0x0D) for Enter. Claude Code's Node.js readline may not interpret `0x0A` key events as line breaks, causing multi-line prompts to lose their line structure.

**Impact:** Medium — affects any multi-line prompt (e.g., pasted code blocks, image references with newlines).

### W6. `wVirtualKeyCode = 0` for all keys

**Location:** win_send_keys.py:74, 84

All `KEY_EVENT` records have `wVirtualKeyCode = 0`. For regular characters, this works because console applications typically read `uChar` for text input. But for the final `\r` (Enter), some applications check `wVirtualKeyCode == VK_RETURN (0x0D)` to detect Enter specifically. If Claude Code's Node.js tty layer relies on virtual key codes, the Enter key is not recognized — the prompt text appears but is never submitted.

**Impact:** Unknown — depends on Node.js version and Windows tty implementation. Could be a significant contributor to "text appears but doesn't execute" symptoms.

### W7. 10-second subprocess timeout blocks HTTP server

**Location:** platform_utils.py:326

```python
result = subprocess.run(..., timeout=10)
```

If `AttachConsole` blocks or `WriteConsoleInputW` hangs, the entire HTTP handler thread is blocked for up to 10 seconds. Since `HTTPServer` is single-threaded, all other HTTP requests (UI polling, other sessions) stall.

**Impact:** Low — the subprocess rarely hangs, but when it does, the entire UI freezes.

---

## Summary

| # | Issue | Platform | Symptom | Frequency |
|---|-------|----------|---------|-----------|
| **P1** | Frontend ignores send-prompt response status | All | User sees no error on any failure | Every failure |
| **P2** | Server doesn't check session state before send | All | Prompt pasted into busy terminal | Medium |
| **T1** | Tmux commands don't check return codes | Linux/macOS | Silent prompt loss, reported as success | Medium |
| **T2** | Tmux buffer global race | Linux/macOS | Wrong prompt delivered to wrong pane | Low |
| **W1** | Console input buffer overflow | Windows | Long prompts truncated, Enter dropped | High |
| **W2** | Auto-discovered sessions lack console_pid | Windows | Send always fails silently | High |
| **W3** | AttachConsole mutual exclusion | Windows | Concurrent send fails | Low |
| **W4** | Prompt via command line argument | Windows | Length limit, encoding risk | Low |
| **W5** | `\n` not treated as Enter | Windows | Multi-line prompt format corrupted | Medium |
| **W6** | `wVirtualKeyCode = 0` for Enter | Windows | Enter key potentially ignored | Unknown |
| **W7** | 10-second subprocess timeout | Windows | UI freezes | Low |

## Suggested Fix Priority

1. ~~**P1 + T1**: Frontend error display + tmux return code checking~~ — **FIXED**
2. **W1**: Write records in chunks with flow control, or verify `written == len(records)` and retry/report — needs Windows testing
3. ~~**W2**: Disable prompt input in UI for sessions without delivery capability~~ — **FIXED**
4. ~~**W6**: Set `wVirtualKeyCode = VK_RETURN` for `\r` character~~ — **FIXED** (needs Windows testing)
5. ~~**W5**: Convert `\n` to `\r` in the prompt before building key events~~ — **FIXED** (needs Windows testing)
6. ~~**T2**: Use named tmux buffers to avoid global buffer race~~ — **FIXED**
7. ~~**P2**: Server-side state check before delivery (return 409 if not idle)~~ — **FIXED**

### Additional fixes applied
- ~~**W4**: Prompt passed via stdin instead of command line argument~~ — **FIXED** (needs Windows testing)
- **P1** expanded to cover all three `send-prompt` call sites (`sendPrompt`, `quickPrompt`, `sendDashboardPrompt`)
- `showToast` now supports error styling (red background, longer display)

### Remaining (not fixed)
- **W1**: Console input buffer overflow — needs Windows testing to validate chunked write approach
- **W3**: AttachConsole mutual exclusion — low frequency, fix requires cross-process locking
- **W7**: 10-second subprocess timeout — architectural (single-threaded HTTPServer), low frequency
