# Transcript State Derivation: Assumptions, Bugs, and Testing Plan

This document is a comprehensive audit of the transcript-driven state derivation logic in claude-code-webui. It covers every logical assumption the code makes, known bugs, cross-process concurrency hazards, and a testing plan to prevent regressions.

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Assumptions](#assumptions)
  - [1. Transcript Parsing](#1-transcript-parsing)
  - [2. State Derivation](#2-state-derivation)
  - [3. Permission Request Flow](#3-permission-request-flow)
  - [4. Prompt Delivery](#4-prompt-delivery)
  - [5. Session Lifecycle](#5-session-lifecycle)
- [Concurrency Model](#concurrency-model)
  - [In-Process Threading](#in-process-threading)
  - [Cross-Process: Claude Code Writes vs Server Reads](#cross-process-claude-code-writes-vs-server-reads)
  - [Concurrency Scenarios](#concurrency-scenarios)
- [Windows-Specific Differences](#windows-specific-differences)
  - [File Locking](#w1-file-locking--mandatory-locks)
  - [Transcript Discovery](#w2-transcript-discovery)
  - [Session Liveness](#w3-session-liveness-detection)
  - [Prompt Delivery](#w4-prompt-delivery-writeconsoleinputw-vs-tmux)
  - [Path Encoding](#w5-path-encoding)
  - [Request File Atomicity](#w6-requestjson-atomicity)
  - [Process Lifecycle](#w7-process-lifecycle)
  - [Summary Table](#windows-vs-linux-summary)
- [Confirmed Bugs](#confirmed-bugs)
- [Proposed Fixes](#proposed-fixes)
- [Testing Plan](#testing-plan)
  - [Testability Refactoring](#testability-refactoring)
  - [Test Cases](#test-cases)

---

## Architecture Overview

The WebUI server derives all session state by reading Claude Code's transcript JSONL files. There is no shared state machine between Claude Code and the server — the transcript file is the single source of truth.

**Writers:** Claude Code process (appends entries to JSONL, rewrites on `/compact` and `/clear`).

**Readers:** WebUI server (reads incrementally via byte offset, parses JSON lines, derives state).

**No coordination mechanism exists between writer and reader** — no file locking, no signaling, no flock.

Key code locations:

| Function | File | Lines | Purpose |
|----------|------|-------|---------|
| `update_session_state()` | server.py | 67–131 | Incremental file read + trigger state derivation |
| `_derive_state()` | server.py | 133–258 | Pure state derivation from entries + pending request files |
| `_find_pending_request()` | server.py | 261–274 | Scan filesystem for `.request.json` matching session |
| `_tool_use_resolved_in_transcript()` | server.py | 277–294 | Check if a tool_use already has a tool_result |
| `_has_tool_result()` | server.py | 297–308 | Scan entries for matching tool_result |
| `POST /api/session/register` | server.py | 535–625 | Session registration, offset reset, pane eviction |
| `POST /api/send-prompt` | server.py | 713–731 | Prompt delivery entry point |
| `_send_prompt_tmux()` | platform_utils.py | 333–363 | Tmux-based prompt injection |
| hook-permission-request.py | — | 337–474 | Permission request lifecycle (write `.request.json`, poll for response) |

---

## Assumptions

### 1. Transcript Parsing

`update_session_state()` at server.py:67–131.

#### A1. One JSON object per line, `\n`-delimited

The code does `text.split("\n")` and calls `json.loads()` on each line. Multi-line JSON would break parsing.

**Risk:** Low — Claude Code uses standard JSONL format.

#### A2. The file is append-only (except during `/compact` and `/clear`)

The server tracks a byte offset and only reads new bytes from that point forward. If already-written content changes, old entries in memory become stale.

**Risk:** Medium — `/compact` and `/clear` rewrite the file. The hook sends a registration with `source=compact|clear` to reset the offset, but there's a race window (see [Concurrency Scenarios](#concurrency-scenarios)).

#### A3. Re-encoding decoded text produces the same byte length

Line 108: `bytes_consumed += len(line.encode("utf-8")) + nl`. The file is decoded with `errors="replace"` (line 94), which replaces invalid bytes with U+FFFD (3 bytes in UTF-8). If the original invalid byte was 1 byte, re-encoding produces 3 bytes — the offset drifts by 2 per bad byte.

**Risk:** Very low — transcript files are written by Claude Code in clean UTF-8.

#### A4. Incomplete last line triggers `json.loads` failure → safe retry

Line 110–112: if the last element from `split("\n")` fails to parse, the code `break`s without consuming those bytes. Next poll re-reads from the same position.

**Risk:** None for the last line. But mid-file bad lines (not the last element) are skipped and their bytes consumed permanently (line 113–114). See [Scenario C3](#c3-large-entry-write-splits-across-os-level-writes).

---

### 2. State Derivation

`_derive_state()` at server.py:133–258.

#### B1. Entry list order = chronological order

The code uses list index position to determine whether the last user message came after the last assistant message (lines 198–203). Note: the `user_idx` and `asst_idx` variables are assigned inside the loop body and used after the loop — this relies on the fact that `last_user` and `last_assistant` are guaranteed to exist in `entries` (having been found by iterating `reversed(entries)` earlier). If this invariant were ever broken, the code would raise `UnboundLocalError`.

**Risk:** None — JSONL append order is chronological by construction.

#### B2. Only the last `assistant` and last `user` entry matter

Lines 142–149: reverse-walk entries, stop as soon as both are found. All earlier entries are ignored for state derivation.

**Risk:** Low — sufficient for normal conversation flow. But ignores edge cases where intermediate entries might carry state-relevant information.

#### B3. Only the *last* tool_use in the last assistant message determines state

Line 238: `last_tool = tool_uses[-1]`. If an assistant message contains multiple tool_use blocks, only the final one is checked for `AskUserQuestion`, `ExitPlanMode`, or unresolved status.

**Risk:** Medium — if the last tool_use is resolved but an earlier one isn't, the session may appear idle when it's actually waiting on the earlier tool. In practice, Claude Code resolves tool_uses sequentially, so the last one is the "current" one.

#### B4. `stop_reason == "tool_use"` with all tool_uses resolved should mean idle

**This is a confirmed bug.** See [Bug 1](#bug-1-session-stuck-at-busy-when-all-tool_uses-are-resolved).

#### B5. `AskUserQuestion` / `ExitPlanMode` is always the last tool_use in a message

The code checks `tool_uses[-1]` for these names. If Claude places them before another tool_use in the same message, the state won't be `elicitation` or `plan_review`.

**Risk:** Low — Claude Code currently always puts these as the sole or final tool_use.

#### B6. `permission_prompt` takes priority over `elicitation` / `plan_review`

Lines 226–235 run before lines 237–244. If a `.request.json` exists, the state is `permission_prompt` regardless of which tool_use is pending.

**Risk:** None — correct and intentional. A pending permission request is more urgent.

#### B7. The `_tool_use_resolved_in_transcript` stale check matches by `tool_name` only

Line 289: `if c.get("name") != tool_name: continue`. It finds the most recent tool_use with the same name, not the same `tool_use_id` or `tool_input`.

**Risk:** Medium — if the same tool is called twice in succession, the stale check might match the wrong invocation. The request file doesn't carry `tool_use_id`.

#### B8. Dead code in user prompt extraction

Lines 161–163: an `if` condition that executes `pass`, followed by unconditional `text = content`. The branch has zero effect.

**Risk:** None functionally, but confusing to read.

---

### 3. Permission Request Flow

#### C1. One pending `.request.json` per session at a time

`_find_pending_request()` returns the first matching file found by `glob.glob()`. If multiple exist (e.g., from a killed hook), the return order is filesystem-dependent.

**Risk:** High — orphaned request files can cause the wrong request to be displayed. See [Scenario C6](#c6-orphaned-requestjson-from-killed-hook).

#### C2. The hook's `atexit` cleanup fires reliably

The hook registers `atexit` to remove its `.request.json`. But SIGKILL (kill -9), OOM killer, and system crashes don't trigger `atexit`.

**Risk:** Medium — orphaned files accumulate and poison state derivation.

#### C3. The hook writes `.request.json` atomically

The hook does a plain `open` + `json.dump` + `close`. If the server reads the file mid-write, it gets partial JSON.

**Risk:** Low — `_find_pending_request` catches `json.JSONDecodeError` and skips the file. But the permission prompt is invisible for one poll cycle (~2 seconds).

---

### 4. Prompt Delivery

#### D1. Tmux pane is alive and writable

`_send_prompt_tmux()` runs three sequential `tmux` commands: `load-buffer`, `paste-buffer`, `send-keys Enter`. If the pane has been closed, all three fail silently (return False). The user gets no feedback.

**Risk:** Medium — the API returns `{"ok": false}` but the UI may not surface this clearly.

#### D2. No server-side state check before delivery

`POST /api/send-prompt` does not verify the session is idle. The frontend hides the input box for non-idle sessions, but the API itself is unguarded.

**Risk:** Low — only matters if something bypasses the frontend (e.g., Feishu channel, direct API call).

#### D3. The three tmux commands are not atomic

If `load-buffer` succeeds but `paste-buffer` or `send-keys` fails, the prompt is partially delivered or lost entirely.

**Risk:** Low — tmux commands rarely fail independently.

#### D4. Windows auto-discovered sessions can't receive prompts

Sessions found by `_scan_sessions_from_transcripts()` have `console_pid = ""`. Prompt delivery requires a valid `console_pid`, which is only set when the hook fires.

**Risk:** Medium — the session appears in the UI but the prompt input is non-functional until the hook registers.

---

### 5. Session Lifecycle

#### E1. Zombie cleanup every 30 seconds is sufficient

Dead sessions remain visible in the UI for up to 30 seconds.

**Risk:** Low — cosmetic issue.

#### E2. Pane eviction correctly handles terminal reuse

When a new session registers on the same tmux pane, old sessions are evicted (lines 551–559). But if the new registration is delayed, the old session lingers.

**Risk:** Low — the delay is typically sub-second.

#### E3. UUID session liveness depends on tmux being healthy

For auto-discovered sessions (UUID-based IDs), `_is_session_alive()` runs `tmux list-panes` to find the shell PID, then checks for `claude`/`node` child processes. If tmux itself is down, all sessions are marked dead.

**Risk:** Low — if tmux is down, the sessions genuinely can't receive prompts anyway.

---

## Concurrency Model

### In-Process Threading

| Thread | Role | Shared state access |
|--------|------|-------------------|
| HTTP handler (main thread) | Process all HTTP requests, call `update_session_state()` | Direct, under `sessions_lock` |
| zombie_cleanup_loop | Delete dead sessions every 30s | Under `sessions_lock`, only deletes |
| Feishu channel (if enabled) | Poll `localhost/api/sessions` via HTTP | Goes through the HTTP handler, no direct access |

**Key insight:** `HTTPServer` (without `ThreadingMixIn`) is **single-threaded**. All HTTP requests are serialized. The only true in-process concurrency is between the HTTP thread and the zombie cleanup thread — and the zombie thread only deletes sessions.

Therefore, the `read offset → read file → write offset` gap in `update_session_state()` (lines 69–130) is safe under the current architecture: no other HTTP request can interleave. If the server is ever made multi-threaded, this becomes a race condition.

### Cross-Process: Claude Code Writes vs Server Reads

This is where the real concurrency issues live. Claude Code and the server operate on the same transcript file with **zero coordination**.

```
Claude Code process              Transcript JSONL file              WebUI Server process
      │                                │                                  │
      ├── append entry ──────────────► │                                  │
      │                                │ ◄────────────── seek + read ─────┤
      ├── append entry ──────────────► │                                  │
      │                                │                                  │
      ├── /compact: rewrite file ────► │ (file truncated & rewritten)     │
      │                                │ ◄────────────── seek + read ─────┤  ← RACE
      ├── hook: register(compact) ───► │                     ─────────────┤
      │                                │              (offset reset to 0) │
```

### Concurrency Scenarios

#### C1. Normal append — server reads partial last line

Claude Code appends an entry. The server reads while the write is in progress, getting a truncated last line.

**Current behavior:** The truncated line is the last element of `split("\n")` → `json.loads` fails → `break` → bytes not consumed → next poll re-reads successfully.

**Verdict:** Correctly handled.

#### C2. `/compact` or `/clear` rewrites the file — the most dangerous race

**Timeline:**

1. Claude Code rewrites the transcript file (truncate + write new content).
2. Server polls with old offset before the hook's registration request arrives.
3. Hook sends `POST /api/session/register` with `source=compact`, triggering offset reset.

**What happens during step 2:**

- **Old offset > new file size:** `f.seek(old_offset)` goes past EOF, `f.read()` returns `b""`. No new entries parsed. State derivation runs on stale in-memory entries — may show incorrect state, but no data corruption.
- **Old offset < new file size:** The server reads from the middle of the new file. The first "line" is likely a partial JSON object. If it's not the last element from `split("\n")`, it's treated as a mid-file bad line — **bytes consumed, line permanently skipped**. Subsequent lines may parse as valid JSON and get appended to the in-memory entries alongside the old ones, creating **duplicates or inconsistent entries**.

**When step 3 arrives:** entries are cleared, offset reset to 0. Everything recovers. But during the window (typically < 2 seconds), state derivation is unreliable.

#### C3. Large entry write splits across OS-level writes

If a single JSONL entry is very large (e.g., a `Read` tool result with thousands of lines), the kernel may split the `write()` syscall. The server could read the first half of the entry at a moment when it's followed by other complete entries already on disk:

```
Already on disk: {"entry_1": ...}\n{"entry_2": ...}\n
Being written:   {"big_entry": "aaa...    (first half, flushed)
                  ...bbb"}\n              (second half, not yet flushed)
```

Server reads: `{"entry_1": ...}\n{"entry_2": ...}\n{"big_entry": "aaa...`

After `split("\n")`: entries 1 and 2 are not the last element and parse fine. The partial `{"big_entry"...` IS the last element → `json.loads` fails → `break`. Correct.

**But if the split happens at a `\n` boundary within the large entry's content** (impossible — JSON strings escape `\n` as `\\n`), or if the partial data ends with `\n` followed by more partial data, the last-element check may misidentify which element is "last."

**Verdict:** Safe in practice because JSONL entries don't contain literal newlines. The last-element heuristic is reliable for well-formed JSONL.

#### C4. `.request.json` read during hook write

The hook writes the request file with plain `open()` + `json.dump()` + `close()`. The server scans with `glob.glob()` and reads with `json.load()`.

If the server reads mid-write: `json.load()` raises `JSONDecodeError`, caught by the `except` clause in `_find_pending_request()`. The request is invisible for this poll cycle.

**Impact:** The UI shows `busy` instead of `permission_prompt` for ~2 seconds. Minor.

#### C5. `.response.json` read during server write

The server writes the response file (line 692) and the hook polls for it (every 0.5s). If the hook reads mid-write, `json.load()` fails and the hook retries on the next poll iteration.

**Impact:** 0.5-second delay. Negligible.

#### C6. Orphaned `.request.json` from killed hook

If the hook process is killed with SIGKILL (or OOM-killed), `atexit` doesn't fire. The `.request.json` remains on disk with no process listening for a response.

**Impact:** High — `_find_pending_request()` finds this file and returns it. The session appears stuck in `permission_prompt` with a stale request. The user can approve/deny it, a `.response.json` is written, but no hook reads it. The stale check (`_tool_use_resolved_in_transcript`) may eventually clean it up if the tool_use gets a tool_result through other means, but this isn't guaranteed.

#### C7. Multiple rapid appends between polls

Claude Code writes several entries between two server polls. On the next poll, the server reads all of them at once.

**Verdict:** Correctly handled — all lines are parsed in sequence. No data loss.

---

## Windows-Specific Differences

The codebase is cross-platform, but Linux and Windows have fundamentally different behavior in several areas. The shared code (`_derive_state`, `update_session_state`) is platform-agnostic, but its assumptions interact with platform-specific file and process semantics in ways that can produce different bugs on each OS.

### W1. File Locking — Mandatory Locks

**Linux:** File locking is advisory. Multiple processes can freely read and write the same file concurrently. The server's `open(path, "rb")` never blocks, even if Claude Code is mid-write.

**Windows:** File locking is **mandatory by default**. When Claude Code opens the transcript file for writing, the OS may hold an exclusive or share lock on the file. The server's `open(path, "rb")` can raise `IOError` because the file is locked by another process.

**Current mitigation (server.py:83–91):** A retry-once pattern specifically for this:

```python
except IOError as e:
    print(f"[!] Failed to read transcript for session {sid}: {e}, retrying...")
    time.sleep(1)
    try:
        ...
    except IOError:
        new_data = b""
```

**Problems:**

- The 1-second sleep blocks the single-threaded HTTP server. During this sleep, **no other HTTP request can be processed** — the entire UI freezes for all sessions.
- If the retry also fails, `new_data = b""` and the poll is silently skipped. State stays stale.
- If Claude Code holds the file lock for extended writes (large tool results), the 1-second window may not be enough, causing repeated missed polls.
- On Linux this code path is never hit, so it's essentially untested in the primary development environment.

**Concurrency impact:** The `/compact` and `/clear` race (Scenario C2) is **worse on Windows**. During file rewrite, the file may be exclusively locked. The server can't even read the file at all (not even stale data) — it falls through to `new_data = b""`, deriving state from whatever entries are in memory. This window may be wider than on Linux because the rewrite involves open-truncate-write-close under a lock.

### W2. Transcript Discovery

**Linux:** `scan_existing_sessions()` (server.py:873) enumerates tmux sockets → tmux panes → shell PIDs → child processes → finds `claude`/`node` → resolves CWD via `lsof` → finds transcript JSONL. This is process-centric discovery: we know which tmux pane owns the session.

**Windows:** `_scan_sessions_from_transcripts()` (server.py:827) simply scans `~/.claude/projects/*/` for `.jsonl` files modified within the last 2 hours. This is file-centric discovery: we find transcript files but don't know which console hosts them.

**Consequences:**

- **No `console_pid`**: Auto-discovered Windows sessions have `console_pid = ""`. Prompt delivery is impossible until the hook fires and registers the session properly.
- **No pane eviction**: Without a `console_pid` or `tmux_pane`, the eviction logic (server.py:551–570) can't detect that two sessions share the same terminal. If a user restarts Claude Code in the same console, both the old (auto-discovered, dead) and new (hook-registered) sessions may coexist until zombie cleanup runs.
- **Stale transcripts**: A `.jsonl` file modified 1.5 hours ago is picked up even if the session is long dead. The session appears in the UI for up to 2 hours with no way to dismiss it (zombie cleanup considers it alive as long as mtime < 2 hours).
- **CWD is the project directory, not the actual CWD**: The `cwd` field is set to the `~/.claude/projects/<encoded>` path, not the original working directory. This is cosmetically wrong — the UI shows the project dir path instead of the real path.

### W3. Session Liveness Detection

**Linux (numeric PID):** `os.kill(pid, 0)` — instant, reliable, no race.

**Linux (UUID, tmux):** `tmux list-panes` → `pgrep` → `ps` — multiple subprocess calls, slower but accurate.

**Windows (numeric PID):** `OpenProcess` + `GetExitCodeProcess` (platform_utils.py:133–150). Checks if exit code is `STILL_ACTIVE (259)`.

**Windows-specific issue:** `GetExitCodeProcess` returns `STILL_ACTIVE` as long as the process hasn't been waited on. But there's a known edge case: if a process happens to exit with exit code 259, it's falsely considered alive. This is a well-documented Windows API pitfall. Unlikely for `claude`/`node` processes, but worth noting.

**Windows (UUID, no console_pid):** Falls back to transcript mtime check — alive if modified within 2 hours. This is extremely coarse-grained:

- A session that crashed 1.5 hours ago still appears alive.
- A session actively running but producing no transcript output for 2+ hours (e.g., long-running tool) is considered dead.
- The 60-second grace period for newly registered sessions (server.py:352–355) helps with startup, but doesn't address the 2-hour window.

### W4. Prompt Delivery: `WriteConsoleInputW` vs Tmux

**Linux (tmux):** Three subprocess calls: `load-buffer` → `paste-buffer` → `send-keys Enter`. Text is loaded into tmux's buffer first (no character limit concern), then pasted.

**Windows (`win_send_keys.py`):** Uses `AttachConsole` + `WriteConsoleInputW` to inject keystrokes directly into the target console's input buffer.

**Windows-specific issues:**

- **`GetStdHandle` returns invalid handle after `FreeConsole` + `AttachConsole`**: This was the **root cause** of all prompt delivery failures on Windows. After `FreeConsole()`, cached standard handles become stale. `AttachConsole()` attaches to the target console but does not update these handles. `GetStdHandle(STD_INPUT_HANDLE)` returned an invalid handle, causing every `WriteConsoleInputW` call to silently fail. **Fixed:** replaced with `CreateFileW("CONIN$")` which opens a fresh handle to the attached console's input buffer.
- **`FreeConsole` + `AttachConsole` mutual exclusion**: The server spawns `win_send_keys.py` as a child process. If the target console is already being used by another `AttachConsole` call (from a concurrent prompt send), one of them will fail — `AttachConsole` only allows one external attachment at a time.
- **Character-by-character injection**: Each character generates a key-down + key-up `INPUT_RECORD` pair. For a 1000-character prompt, that's 2000+ input records. The console input buffer grows dynamically (per Microsoft Terminal source: `std::deque<INPUT_RECORD>`), so there is no overflow risk.
- **No escape handling**: Special characters (e.g., `\t`, control characters) are injected literally. If the prompt contains tab characters, they'll be interpreted as tab-completion by the shell, not as literal tabs. The tmux approach (`load-buffer`) doesn't have this issue because `paste-buffer` sends raw text.
- **`\r` vs `\n` for Enter**: `full_text = text.replace("\n", "\r") + "\r"`. Windows consoles expect `\r` (carriage return) as Enter. `\n` is converted to `\r` for multi-line prompt support.
- **10-second timeout**: `_send_prompt_windows` (platform_utils.py:320) has a 10-second timeout. If the subprocess hangs (e.g., `AttachConsole` blocks), the server's HTTP handler is blocked for 10 seconds (single-threaded server).

### W5. Path Encoding

`encode_project_path()` (platform_utils.py:153–171) converts filesystem paths to Claude Code's `~/.claude/projects/` directory names.

**Linux:** `/home/user/project` → `-home-user-project` (replace `/` with `-`).

**Windows:** `C:\Users\foo\project` → `C-Users-foo-project` (replace `\` with `/`, remove `:`, replace `/` with `-`).

**Issue:** The Linux auto-discovery code in `scan_existing_sessions()` (server.py:960–964) duplicates the encoding logic inline (`path.replace("/", "-")` with a leading `-`) instead of calling `encode_project_path()`. The two implementations currently produce identical results for Linux paths, but if `encode_project_path()` is ever updated (e.g., to handle edge cases), the auto-discovery path won't pick up the change. This duplication is an architectural maintenance concern.

**Windows issue — CONFIRMED:** The `encode_project_path` produces `-C-Users-foo-project` (removes colon, prepends `-`), but Claude Code actually produces `C--Users-foo-project` (replaces `:` with `-`, no leading `-`). Verified on Windows 11 with Claude Code v2.1.39:
- Actual directory: `C--Users-qidlin-source-repos-claude-code-webui`
- `encode_project_path()` output: `-C-Users-qidlin-source-repos-claude-code-webui`

**Current impact is limited:** Claude Code v2.1.39+ passes `transcript_path` directly in the SessionStart hook input data, so `find_transcript_path()` (which uses `encode_project_path`) is only called as a fallback. The `_scan_sessions_from_transcripts()` auto-discovery iterates all directories and does not use `encode_project_path`, so it is also unaffected.

### W6. `.request.json` Atomicity

**Linux:** `os.rename()` is atomic on the same filesystem (POSIX guarantee). A write-then-rename pattern would guarantee the server never sees a partial file.

**Windows:** `os.rename()` **fails** if the destination already exists (`FileExistsError`). You must use `os.replace()` instead for atomic replacement. But for new files (which `.request.json` is), `os.rename()` works — the file doesn't exist yet.

**Fixed:** The hook now writes to a `.tmp` file first, then calls `os.replace()` to atomically move it into place. `os.replace()` works on both POSIX and Windows (unlike `os.rename` which fails on Windows if the destination already exists).

### W7. Process Lifecycle

**Linux:** `SIGKILL` kills the process immediately. `atexit` doesn't fire. Orphaned `.request.json` files are left behind. `SIGTERM` triggers `atexit`.

**Windows:** `TerminateProcess` (the Windows equivalent of SIGKILL) kills the process immediately. `atexit` doesn't fire. But Windows also has a subtlety: closing the console window sends `CTRL_CLOSE_EVENT`, which Python may or may not handle depending on signal handlers. If it's not handled, the process is terminated after 5 seconds without running `atexit`.

**Impact:** Orphaned `.request.json` files are a cross-platform issue, but the trigger is different:
- Linux: `kill -9`, OOM killer
- Windows: `TerminateProcess`, closing the console window, system shutdown, task manager "End task"

The "End task" in Task Manager is by far the most common way Windows users kill processes, and it uses `TerminateProcess` — no `atexit` cleanup.

### Windows vs Linux Summary

| Aspect | Linux | Windows | Impact |
|--------|-------|---------|--------|
| **Transcript file read** | Always succeeds (advisory locking) | Can raise `IOError` (mandatory locking) | 1s freeze on retry; missed poll on double failure |
| **Concurrent read during write** | Read returns whatever bytes are on disk | Read may fail entirely with lock error | Windows has wider "invisible" windows |
| **`/compact` race** | Can read stale/mid-file data | Can't read at all (locked) → `new_data = b""` | Both bad, but symptoms differ |
| **Session auto-discovery** | Process-centric (tmux panes) | File-centric (transcript mtime) | Windows sessions lack `console_pid`; prompt delivery broken until hook fires |
| **Liveness detection** | `os.kill(0)` or tmux process tree | `GetExitCodeProcess` or 2-hour mtime window | Windows has much coarser granularity for auto-discovered sessions |
| **Prompt delivery** | tmux buffer paste (robust, no char limit) | `WriteConsoleInputW` keystroke injection | ~~Buffer overflow~~ debunked; `GetStdHandle` bug was root cause (**FIXED**) |
| **Prompt delivery blocking** | 5s timeout × 3 commands | 10s timeout for subprocess | Both block the HTTP server |
| **`.request.json` write** | Atomic via temp + `os.replace()` | Atomic via temp + `os.replace()` | **FIXED** — both platforms use write-then-replace |
| **Orphaned request files** | From SIGKILL, OOM | From TerminateProcess, console close, Task Manager | More common on Windows (Task Manager) |
| **Zombie cleanup accuracy** | Good (PID check or tmux process tree) | Poor for auto-discovered sessions (2-hour mtime) | Dead sessions linger much longer on Windows |
| **Pane/console eviction** | By `tmux_pane` match | By `console_pid` match (only hook-registered) | Auto-discovered Windows sessions can't be evicted |

---

## Confirmed Bugs

### Bug 1: Session stuck at "busy" when all tool_uses are resolved

**Location:** server.py:237–258

**Trigger:** The last assistant message has `stop_reason == "tool_use"`, and all tool_use blocks in that message have matching tool_results in subsequent user entries.

**Code path:**
1. Line 248: `_has_tool_result()` returns True → does not return "busy", continues.
2. Line 251: the full condition is `stop_reason == "end_turn" or (not tool_uses and stop_reason != "tool_use")`. First clause: `stop_reason == "end_turn"` is False. Second clause: `not tool_uses` is False (tool_uses is non-empty). Whole condition not met.
3. Falls through to line 258: returns `"busy"`.

**Expected:** Should return `"idle"`.

**Fix:** After the tool_uses loop (after line 249), if we reach line 251 without returning, the tool_use has been resolved. The condition at line 251 should also accept this case.

Note: the existing condition at line 251 is `stop_reason == "end_turn" or (not tool_uses and stop_reason != "tool_use")`. The second clause defensively handles the edge case where `stop_reason != "tool_use"` and there are no tool_uses — a state that shouldn't occur normally. The proposed fix below simplifies this and adds an explicit check for the all-resolved case:

```python
# All tool_uses resolved, or no tool_uses with end_turn
if stop_reason == "end_turn" or not tool_uses:
    return "idle", summary, user_prompt

# tool_uses exist but all resolved — also idle
if tool_uses and all(_has_tool_result(entries, tu.get("id", "")) for tu in tool_uses):
    return "idle", summary, user_prompt
```

Or more simply, change the existing logic so that when `_has_tool_result` returns True for the last tool_use, we don't just fall through — we explicitly go to the idle check.

### Bug 2: Dead code in user prompt extraction

**Location:** server.py:161–163

```python
if stripped.startswith("<") and not any(c in stripped for c in ["\n"] if stripped.count("<") > 3):
    # Check if it's mostly XML — strip tags and see what's left
    pass
text = content  # executes unconditionally
```

The `if` body is `pass`. The `text = content` assignment on line 164 runs regardless. These three lines have no effect.

### Bug 3: UTF-8 `errors="replace"` can cause offset drift

**Location:** server.py:94, 108

If invalid bytes exist in the transcript, `errors="replace"` substitutes each bad byte with U+FFFD (3 bytes in UTF-8). The `len(line.encode("utf-8"))` on line 108 counts the replacement character's bytes, not the original's. The offset advances by more than the actual file bytes.

**Practical impact:** Near-zero. Transcript files are written by Claude Code in clean UTF-8.

### Bug 4: Windows file lock retry blocks the entire server

**Location:** server.py:83–91

The `time.sleep(1)` during the IOError retry is called on the HTTP handler thread. Since `HTTPServer` is single-threaded, this blocks all HTTP request processing for 1 second. If multiple sessions have locked transcript files, the cumulative delay is N seconds.

### Bug 5: Windows auto-discovered sessions have broken prompt delivery and coarse liveness

**Location:** server.py:827–870, server.py:325–356

Auto-discovered sessions (from `_scan_sessions_from_transcripts`) register with `console_pid = ""`. This means:
- `send_prompt()` returns False immediately — the user can see the session but can't interact with it.
- Liveness check falls back to a 2-hour transcript mtime window, so dead sessions linger for hours.
- No pane/console eviction is possible.

This is by design (documented in server.py:830–831), but it means the Windows-without-hook experience is significantly degraded compared to Linux.

---

## Proposed Fixes

> **All fixes below have been applied.** See the commit that accompanies this document.

### Priority 1: Fix Bug 1 (stuck "busy" state) ✅

**Applied:** Added `_all_tool_uses_resolved()` helper. The idle check at the end of the tool_uses block now also returns idle when all tool_uses have matching tool_results.

### Priority 2: Defend against orphaned `.request.json` ✅

**Applied:** `_find_pending_request()` now checks `is_process_alive(hook_pid)`. If the hook process is dead, the orphaned `.request.json` is removed and skipped.

### Priority 3: Defend against `/compact` and `/clear` race ✅

**Applied:** `update_session_state()` now checks if `offset > file_size` before reading. If the file was rewritten shorter, offset and entries are reset immediately without waiting for the hook registration.

### Priority 4: Atomic `.request.json` writes ✅

**Applied:** The hook now writes to a `.tmp` file first, then calls `os.replace()` to atomically move it into place.

### Priority 5: Clean up dead code (Bug 2) ✅

**Applied:** Removed the no-op `if`/`pass` block in user prompt extraction.

### Priority 6: Non-blocking Windows file lock handling ✅

**Applied:** Replaced the `time.sleep(1)` retry with a simple `new_data = b""` fallback. The next poll cycle will retry without blocking the HTTP server.

### Priority 7: Future-proof the offset update ✅

**Applied:** Added a `THREADING NOTE` docstring to `update_session_state()` documenting the single-threaded safety assumption.

---

## Testing Plan

### Testability Refactoring

`_derive_state()` is almost a pure function — its only side effect is calling `_find_pending_request()` which scans the filesystem. To make it fully testable:

**Option A (minimal change):** Mock `_find_pending_request` in tests using `unittest.mock.patch`.

**Option B (cleaner):** Extract the pending-request lookup as a parameter:

```python
def _derive_state(sid, s, pending_request=None):
```

The caller (`update_session_state`) passes the result of `_find_pending_request(sid)`. Tests pass `None` or a fake request dict directly.

### Test Cases

Helpers for building transcript entries:

```python
def assistant_entry(content, stop_reason="end_turn"):
    return {"type": "assistant", "message": {"content": content, "stop_reason": stop_reason}}

def user_entry(content):
    return {"type": "user", "message": {"content": content}}

def tool_use(name, tool_id, input_data=None):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": input_data or {}}

def tool_result(tool_use_id, content="ok"):
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}

def text_block(text):
    return {"type": "text", "text": text}

def make_session(entries):
    return {"transcript_entries": entries, "last_summary": "", "last_user_prompt": ""}
```

#### State derivation — basic states

| # | Test name | Entries | Expected state |
|---|-----------|---------|---------------|
| 1 | Empty transcript | `[]` | `idle` |
| 2 | User message only, no assistant reply | `[user("hello")]` | `busy` |
| 3 | Assistant end_turn, no tools | `[user("hi"), assistant([text("hello")], "end_turn")]` | `idle` |
| 4 | User message after assistant | `[assistant([text("done")], "end_turn"), user("next")]` | `busy` |
| 5 | Unresolved tool_use | `[user("do it"), assistant([tool_use("Bash", "t1")], "tool_use")]` | `busy` |
| 6 | AskUserQuestion | `[user("help"), assistant([tool_use("AskUserQuestion", "t1", {"question": "which?"})], "tool_use")]` | `elicitation` |
| 7 | ExitPlanMode | `[user("plan"), assistant([tool_use("ExitPlanMode", "t1")], "tool_use")]` | `plan_review` |
| 8 | Pending request file exists, tool_use unresolved | (mock `_find_pending_request` to return a request) | `permission_prompt` |
| 9 | Pending request file exists, but tool_use already resolved (stale) | (mock returns request; entries have tool_result) | NOT `permission_prompt` |

#### State derivation — Bug 1 regression test

| # | Test name | Entries | Expected state |
|---|-----------|---------|---------------|
| 10 | All tool_uses resolved, stop_reason=tool_use | `[user("write"), assistant([tool_use("Write", "t1")], "tool_use"), user([tool_result("t1")])]` | `idle` (currently returns `busy`) |
| 11 | Multiple tool_uses, all resolved | `[user("do"), assistant([tool_use("Read", "t1"), tool_use("Write", "t2")], "tool_use"), user([tool_result("t1"), tool_result("t2")])]` | `idle` |

#### State derivation — multi-tool edge cases

| # | Test name | Entries | Expected state |
|---|-----------|---------|---------------|
| 12 | Multiple tool_uses, only last resolved | Same as 11 but without `tool_result("t1")` | `busy` pre-fix (Bug 1); `idle` post-fix (only last tool_use is checked — see B3) |
| 13 | AskUserQuestion is not the last tool_use | `[user("x"), assistant([tool_use("AskUserQuestion", "t1"), tool_use("Read", "t2")], "tool_use")]` | `busy` (not `elicitation` — documents current behavior) |

#### User prompt extraction

| # | Test name | User content | Expected prompt |
|---|-----------|-------------|----------------|
| 14 | Plain text | `"what is 2+2"` | `"what is 2+2"` |
| 15 | System XML stripped | `"<system-reminder>x</system-reminder>real question"` | `"real question"` |
| 16 | Only system XML, no real text | `"<system-reminder>x</system-reminder>"` | `""` (skipped, look at earlier user entry) |
| 17 | List content with text blocks | `[text_block("hello"), text_block("world")]` | `"hello world"` |
| 18 | List content with tool_results only | `[tool_result("t1")]` | `""` (skipped) |

#### Transcript parsing (`update_session_state`)

These tests need a real temporary file:

| # | Test name | Setup | Expected |
|---|-----------|-------|----------|
| 19 | Normal incremental read | Write 3 lines, poll, write 2 more, poll | 5 total entries |
| 20 | Partial last line | Write 2 complete lines + incomplete line, poll | 2 entries parsed; offset stops before incomplete line |
| 21 | File rewritten shorter (compact race) | Write 5 lines, poll (offset=X). Rewrite file with 2 lines (shorter). Poll again. | Offset > file size: should reset or read empty |
| 22 | Empty file | Poll on empty file | 0 entries |

#### Permission request file handling

| # | Test name | Setup | Expected |
|---|-----------|-------|----------|
| 23 | Half-written request file | Write partial JSON to `.request.json` | `_find_pending_request` returns None (skips on JSONDecodeError) |
| 24 | Orphaned request from dead PID | Write valid `.request.json` with `pid` of a dead process | Should be cleaned up (after fix) |
| 25 | Stale request resolved in transcript | `.request.json` exists; transcript has matching tool_result | `_cleanup_stale_request` called; state is NOT `permission_prompt` |

#### Prompt delivery

| # | Test name | Setup | Expected |
|---|-----------|-------|----------|
| 26 | Tmux pane doesn't exist | Session with invalid `tmux_pane` | `send_prompt` returns False |
| 27 | Windows session without console_pid | Session with `console_pid=""` | `send_prompt` returns False |

#### Windows-specific tests

| # | Test name | Setup | Expected |
|---|-----------|-------|----------|
| 28 | Transcript file locked by another process | Open file with exclusive lock, then call `update_session_state` | Skips gracefully without blocking (after fix: no sleep) |
| 29 | Auto-discovered Windows session liveness | Create session with mtime 1 hour ago, no `console_pid` | `_is_session_alive` returns True |
| 30 | Auto-discovered Windows session expired | Create session with mtime 3 hours ago, no `console_pid` | `_is_session_alive` returns False |
| 31 | `encode_project_path` Windows path | `C:\Users\foo\project` | `"-C-Users-foo-project"` |
| 32 | `encode_project_path` Linux path | `/home/user/project` | `"-home-user-project"` |
| 33 | `os.replace` used for atomic file writes | Write `.request.json` via temp + replace | File contents always complete (never partial) |
| 34 | Pane eviction by `console_pid` | Register two sessions with same `console_pid`, source=startup | First session evicted |
| 35 | Pane eviction skipped for auto-discovered sessions | Auto-discovered session (no `console_pid`) + hook-registered session | Both coexist (documents current behavior) |
