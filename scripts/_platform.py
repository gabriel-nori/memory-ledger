"""What differs between macOS, Linux and Windows, in one place.

Finding a program on PATH (`claude` is `claude.cmd` under npm on Windows),
choosing the interpreter for a script by its kind (bash from PATH, PowerShell
for a .ps1, cmd for a .bat), and nothing built as a string to hand a shell.
Killing a child with everything it started, and asking whether a pid is alive.

Stdlib only; safe to import from the engine, the hooks and the API.
"""

from __future__ import annotations

import atexit
import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

WINDOWS = os.name == "nt"


def resolve_bin(name: str) -> str:
    """The program to run for `name`: a path as given (with `~` expanded), or
    the first match on PATH. On Windows `shutil.which` follows PATHEXT, so
    `claude` finds `claude.cmd` or `claude.exe`. Falls back to `name` itself,
    so the error a caller reports names what was asked for."""
    name = str(name or "").strip()
    if not name:
        return name
    if os.sep in name or (os.altsep and os.altsep in name) or name.startswith("~"):
        return str(Path(name).expanduser())
    return shutil.which(name) or name


def bash() -> str | None:
    """A bash to run a shell script with: PATH first (Git Bash, WSL's, Homebrew's),
    then the usual places, then sh. None when there is no POSIX shell at all."""
    found = shutil.which("bash")
    if found:
        return found
    for cand in ("/bin/bash", "/usr/bin/bash", "/usr/local/bin/bash", "/opt/homebrew/bin/bash"):
        if Path(cand).exists():
            return cand
    return shutil.which("sh") or ("/bin/sh" if Path("/bin/sh").exists() else None)


def powershell() -> str | None:
    """PowerShell 7 (pwsh) when installed, else Windows PowerShell."""
    return shutil.which("pwsh") or shutil.which("powershell")


def owner_only(path: Path) -> bool:
    """Make a file holding a secret readable by its owner alone. POSIX: mode
    600. Windows ignores the mode bits, so there the inherited ACL is replaced
    by one that grants the current user full control and no one else
    anything (icacls). False when it could not be done."""
    path = Path(path)
    if not WINDOWS:
        try:
            path.chmod(0o600)
            return True
        except OSError:
            return False
    user = os.environ.get("USERNAME", "").strip()
    if not user:
        return False
    try:
        r = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL, timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def link_dir(link: Path, target: Path) -> str:
    """Make `link` reach the directory `target`: a relative symlink, else on
    Windows (no symlinks without developer mode or admin) a junction, which
    needs no privilege. Returns "symlink" or "junction"; raises OSError when
    neither can be made."""
    link, target = Path(link), Path(target)
    try:
        link.symlink_to(target.name if target.parent == link.parent else target,
                        target_is_directory=True)
        return "symlink"
    except OSError:
        if not WINDOWS:
            raise
    r = subprocess.run([os.environ.get("COMSPEC") or "cmd.exe", "/d", "/c", "mklink", "/J",
                        str(link), str(target.resolve())],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                       timeout=30)
    if r.returncode != 0:
        raise OSError(f"mklink /J failed: {r.stderr.decode('utf-8', errors='replace').strip()}")
    return "junction"


def is_link(path: Path) -> bool:
    """A symlink, or on Windows a junction."""
    path = Path(path)
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def cache_dir() -> Path:
    """Where rebuildable things go: $XDG_CACHE_HOME, else %LOCALAPPDATA% on
    Windows, else ~/.cache."""
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser()
    if WINDOWS and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"])
    return Path.home() / ".cache"


def venv_python(d: Path) -> Path:
    """The interpreter inside the venv at `d`."""
    return d / ("Scripts/python.exe" if WINDOWS else "bin/python")


def voice_venv() -> Path:
    """The venv `scripts/setup.py --voice` builds for faster-whisper (and Piper),
    kept out of the server's: the STT worker and Piper run under it."""
    return cache_dir() / "memory-ledger" / "voice"


def node_home() -> Path:
    """Where `scripts/setup.py` unpacks a portable Node.js when the machine has none."""
    return cache_dir() / "memory-ledger" / "node"


def node_bin_dir() -> Path | None:
    """The directory holding npm in the portable Node, or None when there is none."""
    root = node_home()
    for d in (root / "current", root / "current" / "bin"):
        if (d / ("npm.cmd" if WINDOWS else "npm")).exists():
            return d
    return None


def use_portable_node() -> None:
    """Put the portable Node first on PATH for this process and its children,
    unless a system npm is already there."""
    if shutil.which("npm"):
        return
    d = node_bin_dir()
    if d:
        os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")


def python3() -> list[str]:
    """argv prefix that runs a Python 3: this interpreter, else `py -3` on
    Windows, else python3 on PATH."""
    if sys.executable:
        return [sys.executable]
    if WINDOWS and shutil.which("py"):
        return [shutil.which("py") or "py", "-3"]
    return [shutil.which("python3") or shutil.which("python") or "python3"]


def interpreter(lang: str, path: Path) -> list[str] | None:
    """argv prefix that runs the script at `path`, whose language is `lang`
    (graph._script_lang). None when this machine has nothing that runs it."""
    suffix = path.suffix.lower()
    if lang == "powershell" or suffix == ".ps1":
        ps = powershell()
        return [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"] if ps else None
    if lang == "batch" or suffix in (".bat", ".cmd"):
        return [os.environ.get("COMSPEC") or "cmd.exe", "/d", "/c"] if WINDOWS else None
    if lang == "python":
        return python3()
    if lang == "shell":
        sh = bash()
        return [sh] if sh else None
    prog = {"javascript": "node", "ruby": "ruby", "perl": "perl"}.get(lang)
    if prog:
        found = shutil.which(prog)
        return [found] if found else None
    return None


# ---------------------------------------------------------------- processes
#
# Killing a process kills only that process. `claude` starts its own children
# (MCP servers, tools, subagents), and they would outlive it: so a child that
# may need killing is started as the leader of its own group, and killed with
# the group (killpg on POSIX, taskkill /T on Windows).

_CREATE_NEW_PROCESS_GROUP = 0x00000200
_tracked: set = set()
_tracked_lock = threading.Lock()


def group_kwargs() -> dict:
    """Popen keywords that make the child the leader of a new process group."""
    if WINDOWS:
        return {"creationflags": _CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill `proc` and everything it started. Safe on one that already exited."""
    if proc.poll() is not None:
        return
    if WINDOWS:
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            pgid = os.getpgid(proc.pid)
            # only a group of its own: never the caller's, which holds us too
            if pgid == proc.pid and pgid != os.getpgid(0):
                os.killpg(pgid, signal.SIGKILL)
        except (OSError, AttributeError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def track(proc: subprocess.Popen) -> subprocess.Popen:
    """Remember `proc` so its tree is killed if this process exits first: a
    child in its own group no longer gets the terminal's Ctrl-C."""
    with _tracked_lock:
        _tracked.add(proc)
    return proc


def untrack(proc: subprocess.Popen) -> None:
    with _tracked_lock:
        _tracked.discard(proc)


@atexit.register
def _kill_tracked() -> None:
    with _tracked_lock:
        procs = list(_tracked)
        _tracked.clear()
    for p in procs:
        kill_tree(p)


def pid_alive(pid: int) -> bool:
    """Whether a process with this id is running. Never signals it: on Windows
    `os.kill(pid, 0)` is TerminateProcess, not a probe."""
    if pid <= 0:
        return False
    if WINDOWS:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # 5 is ERROR_ACCESS_DENIED: it exists, it is just not ours to open
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259   # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
