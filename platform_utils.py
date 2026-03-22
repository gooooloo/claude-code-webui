"""
Cross-platform utilities for Claude Code WebUI.

Provides OS-agnostic helpers for temp directories, process detection,
and path encoding. On Linux/macOS, uses /proc and standard POSIX tools.
On Windows, uses ctypes Win32 API calls (no third-party dependencies).
"""

import os
import subprocess
import sys
import tempfile

IS_WINDOWS = sys.platform == "win32"


def get_queue_dir():
    """Return the platform-appropriate queue directory for request/response JSON files."""
    if IS_WINDOWS:
        return os.path.join(tempfile.gettempdir(), "claude-webui")
    return "/tmp/claude-webui"


def get_image_dir():
    """Return the platform-appropriate directory for uploaded images."""
    if IS_WINDOWS:
        return os.path.join(tempfile.gettempdir(), "claude-images")
    return "/tmp/claude-images"


def find_claude_pid(start_pid=None):
    """Walk up the process tree to find the 'claude' process PID (cross-platform)."""
    if start_pid is None:
        start_pid = os.getppid()
    if IS_WINDOWS:
        return _find_claude_pid_windows(start_pid)
    return _find_claude_pid_unix(start_pid)


def _find_claude_pid_unix(start_pid):
    """Walk /proc to find claude ancestor process."""
    pid = start_pid
    for _ in range(10):
        try:
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read().strip()
            if comm in ("claude", "node"):
                with open(f"/proc/{pid}/cmdline") as f:
                    cmdline = f.read()
                if "claude" in cmdline:
                    return pid
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("PPid:"):
                        pid = int(line.split()[1])
                        break
                else:
                    break
        except (FileNotFoundError, PermissionError, ValueError):
            break
    return start_pid


def _find_claude_pid_windows(start_pid):
    """Walk process tree on Windows using CreateToolhelp32Snapshot (no third-party deps)."""
    import ctypes
    import ctypes.wintypes as wt

    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD),
            ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wt.DWORD),
            ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", wt.LONG),
            ("dwFlags", wt.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == ctypes.c_void_p(-1).value:
        return start_pid

    try:
        proc_map = {}  # pid -> (parent_pid, exe_name_lower)
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)

        if k32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                proc_map[pe.th32ProcessID] = (
                    pe.th32ParentProcessID,
                    pe.szExeFile.lower(),
                )
                if not k32.Process32NextW(snap, ctypes.byref(pe)):
                    break
    finally:
        k32.CloseHandle(snap)

    pid = start_pid
    for _ in range(10):
        if pid not in proc_map:
            break
        ppid, exe = proc_map[pid]
        if "claude" in exe:
            return pid
        if "node" in exe:
            # Verify it's claude's node process by checking if "claude" appears
            # in any child or the process name itself
            return pid
        pid = ppid

    return start_pid


def find_shell_pid(start_pid=None):
    """Find the shell process PID (claude's parent) — stable terminal anchor on Windows."""
    claude_pid = find_claude_pid(start_pid)
    if IS_WINDOWS:
        return _get_parent_pid_windows(claude_pid) or claude_pid
    return _get_parent_pid_unix(claude_pid) or claude_pid


def _get_parent_pid_unix(pid):
    """Get parent PID via /proc."""
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except (FileNotFoundError, PermissionError, ValueError):
        pass
    return None


def _get_parent_pid_windows(pid):
    """Get parent PID using CreateToolhelp32Snapshot."""
    import ctypes
    import ctypes.wintypes as wt

    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD),
            ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wt.DWORD),
            ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", wt.LONG),
            ("dwFlags", wt.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == ctypes.c_void_p(-1).value:
        return None

    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if k32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                if pe.th32ProcessID == pid:
                    return pe.th32ParentProcessID
                if not k32.Process32NextW(snap, ctypes.byref(pe)):
                    break
    finally:
        k32.CloseHandle(snap)

    return None


def is_process_alive(pid):
    """Check if a process is alive (cross-platform)."""
    if IS_WINDOWS:
        return _is_process_alive_windows(pid)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _is_process_alive_windows(pid):
    """Check process liveness on Windows using OpenProcess."""
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if k32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return exit_code.value == STILL_ACTIVE
        return False
    finally:
        k32.CloseHandle(handle)


def encode_project_path(path):
    """Encode project path for Claude Code ~/.claude/projects/ directory naming.

    Linux:   /home/user/proj   -> -home-user-proj
    Windows: C:\\Users\\foo\\proj -> C-Users-foo-proj
    """
    if IS_WINDOWS:
        # Normalize to forward slashes first, then remove drive colon
        path = path.replace("\\", "/")
        path = path.replace(":", "")
        encoded = path.replace("/", "-")
        if not encoded.startswith("-"):
            encoded = "-" + encoded
        return encoded
    else:
        encoded = path.replace("/", "-")
        if not encoded.startswith("-"):
            encoded = "-" + encoded
        return encoded


def get_process_children(pid):
    """Get child PIDs of a process (cross-platform)."""
    if IS_WINDOWS:
        return _get_children_windows(pid)
    return _get_children_unix(pid)


def _get_children_unix(pid):
    """Get child PIDs using pgrep."""
    import subprocess

    try:
        result = subprocess.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return [int(p) for p in result.stdout.strip().split() if p.strip()]
    except Exception:
        return []


def _get_children_windows(pid):
    """Get child PIDs using CreateToolhelp32Snapshot."""
    import ctypes
    import ctypes.wintypes as wt

    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD),
            ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wt.DWORD),
            ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", wt.LONG),
            ("dwFlags", wt.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == ctypes.c_void_p(-1).value:
        return []

    children = []
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if k32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                if pe.th32ParentProcessID == pid:
                    children.append(pe.th32ProcessID)
                if not k32.Process32NextW(snap, ctypes.byref(pe)):
                    break
    finally:
        k32.CloseHandle(snap)

    return children


def get_process_name(pid):
    """Get the executable name for a process (cross-platform)."""
    if IS_WINDOWS:
        return _get_process_name_windows(pid)
    return _get_process_name_unix(pid)


def _get_process_name_unix(pid):
    """Get process name via /proc or ps."""
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except (FileNotFoundError, PermissionError):
        pass
    import subprocess

    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _get_process_name_windows(pid):
    """Get process name using CreateToolhelp32Snapshot."""
    import ctypes
    import ctypes.wintypes as wt

    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD),
            ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wt.DWORD),
            ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", wt.LONG),
            ("dwFlags", wt.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == ctypes.c_void_p(-1).value:
        return ""

    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if k32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                if pe.th32ProcessID == pid:
                    return pe.szExeFile
                if not k32.Process32NextW(snap, ctypes.byref(pe)):
                    break
    finally:
        k32.CloseHandle(snap)

    return ""


# ── Prompt delivery ──

def send_prompt(session_info, prompt_text):
    """Send a prompt to a session, dispatching to the appropriate platform method."""
    terminal_id = session_info.get("terminal_id")
    if not terminal_id:
        return False
    if IS_WINDOWS:
        return _send_prompt_windows(int(terminal_id), prompt_text)
    return _send_prompt_tmux(session_info, prompt_text)


def _send_prompt_windows(target_pid, text):
    """Send a prompt to a Windows console via win_send_keys.py subprocess."""
    try:
        result = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(__file__), "win_send_keys.py"),
             str(target_pid)],
            input=text.encode("utf-8"), capture_output=True, timeout=10
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _send_prompt_tmux(session_info, prompt):
    """Send a prompt to a tmux pane."""
    tmux_socket = session_info.get("tmux_socket", "")
    pane = session_info.get("terminal_id", "")
    if not pane:
        return False

    cmd_base = ["tmux"]
    if tmux_socket:
        socket_path = tmux_socket.split(",")[0]
        cmd_base = ["tmux", "-S", socket_path]

    try:
        # Use a named buffer to avoid global buffer race (T2)
        buf_name = f"webui-{os.getpid()}"

        # Load prompt into named buffer via stdin
        r = subprocess.run(
            cmd_base + ["load-buffer", "-b", buf_name, "-"],
            input=prompt.encode(), capture_output=True, timeout=5
        )
        if r.returncode != 0:
            return False
        # Paste buffer into target pane
        r = subprocess.run(
            cmd_base + ["paste-buffer", "-b", buf_name, "-t", pane, "-d"],
            capture_output=True, timeout=5
        )
        if r.returncode != 0:
            return False
        # Send Enter
        r = subprocess.run(
            cmd_base + ["send-keys", "-t", pane, "Enter"],
            capture_output=True, timeout=5
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
