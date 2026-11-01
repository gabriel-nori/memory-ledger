#!/usr/bin/env python3
"""Start, stop and install the desktop launcher for the memory-ledger server.

    python3 scripts/launcher.py start      # default: bring everything up, open the browser
    python3 scripts/launcher.py stop       # shut it down
    python3 scripts/launcher.py status
    python3 scripts/launcher.py install    # put a launcher on the Desktop

`start` runs `serve.py` detached (it owns the brain, the UI build and the voice
workers), waits for `/api/health` (the first start builds the venv and can take
minutes), then opens the page. Already running: it only opens the page. The pid
and the log live in `<home>/run/` and `<home>/logs/serve.log`.

The Desktop launcher is a `.command` (macOS), a `.lnk` to `pythonw` (Windows) or
a `.desktop` (Linux): each runs `launcher.py start` with the Python that ran
`install`, so it does not depend on PATH.

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import _platform  # noqa: E402
import _switch  # noqa: E402

PORT = int(os.environ.get("MEMORY_LEDGER_PORT", "8770"))
URL = f"http://127.0.0.1:{PORT}/"
START_TIMEOUT_S = 600
NAME = "Memory Ledger"


def run_dir() -> Path:
    d = _switch.home_dir() / "run"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pid_file() -> Path:
    return run_dir() / "serve.pid"


def healthy(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(URL + "api/health", timeout=timeout) as r:
            return r.status == 200 and json.loads(r.read().decode("utf-8")).get("ok") is True
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _pid() -> int:
    try:
        return int(pid_file().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _python() -> str:
    """The interpreter for serve.py: pythonw would hide the console but cannot
    print, so the server always runs under python with its output in the log."""
    exe = Path(sys.executable)
    if _platform.WINDOWS and exe.name.lower() == "pythonw.exe":
        exe = exe.with_name("python.exe")
    return str(exe)


def start(open_browser: bool = True) -> int:
    if healthy():
        print(f"{NAME} is already running at {URL}")
    else:
        log_dir = _switch.home_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log = open(log_dir / "serve.log", "ab")
        kw = _platform.group_kwargs()
        if _platform.WINDOWS:
            kw["creationflags"] |= 0x08000000          # CREATE_NO_WINDOW
        proc = subprocess.Popen([_python(), str(HERE / "serve.py"), "--no-open", "--port", str(PORT)],
                                cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=log, stderr=log, **kw)
        pid_file().write_text(str(proc.pid), encoding="utf-8")
        print(f"starting {NAME} (first start builds its environment; log: {log_dir / 'serve.log'})", flush=True)
        deadline = time.monotonic() + START_TIMEOUT_S
        while not healthy():
            if proc.poll() is not None:
                print(f"the server exited with {proc.returncode}; see {log_dir / 'serve.log'}", file=sys.stderr)
                return 1
            if time.monotonic() > deadline:
                print(f"the server did not answer in {START_TIMEOUT_S}s; see {log_dir / 'serve.log'}", file=sys.stderr)
                return 1
            time.sleep(1)
    if open_browser:
        webbrowser.open(URL)
    return 0


def stop() -> int:
    pid = _pid()
    if not pid or not _platform.pid_alive(pid):
        print(f"{NAME} is not running")
        pid_file().unlink(missing_ok=True)
        return 0
    if _platform.WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=15)
    else:
        try:                                            # serve.py leads its own session: the group is ours
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not _platform.pid_alive(pid):
            break
        time.sleep(0.2)
    pid_file().unlink(missing_ok=True)
    print(f"{NAME} stopped")
    return 0


# ------------------------------------------------------------ desktop launcher
def desktop_dir() -> Path:
    if _platform.WINDOWS:
        # the real Desktop can be redirected (OneDrive); ask the shell
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "[Environment]::GetFolderPath('Desktop')"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip())
    elif sys.platform != "darwin":
        r = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", stdin=subprocess.DEVNULL) if shutil.which("xdg-user-dir") else None
        if r and r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip())
    return Path.home() / "Desktop"


def install() -> tuple[bool, str]:
    """Write the Desktop launcher. Returns (ok, path or reason)."""
    d = desktop_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"{d}: {e}"
    py, script = _python(), str(Path(__file__).resolve())
    try:
        if sys.platform == "darwin":
            path = d / f"{NAME}.command"
            path.write_text(f'#!/bin/bash\nexec "{py}" "{script}" start\n', encoding="utf-8")
            path.chmod(0o755)
        elif _platform.WINDOWS:
            path = d / f"{NAME}.lnk"
            pyw = Path(py).with_name("pythonw.exe")
            target = pyw if pyw.exists() else Path(py)
            ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($args[0]);"
                  "$s.TargetPath=$args[1];$s.Arguments=$args[2];$s.WorkingDirectory=$args[3];$s.Save()")
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps, str(path), str(target),
                                f'"{script}" start', str(ROOT)],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
            if r.returncode != 0:
                return False, (r.stderr or r.stdout).strip()[-300:]
        else:
            path = d / "memory-ledger.desktop"
            path.write_text("[Desktop Entry]\nType=Application\n"
                            f"Name={NAME}\nExec=\"{py}\" \"{script}\" start\nTerminal=false\n",
                            encoding="utf-8")
            path.chmod(0o755)
    except OSError as e:
        return False, str(e)
    return True, str(path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="launcher", description=__doc__.split("\n\n")[0])
    ap.add_argument("action", nargs="?", default="start", choices=["start", "stop", "status", "install"])
    ap.add_argument("--no-open", action="store_true", help="start: do not open the browser")
    a = ap.parse_args(argv)
    if a.action == "start":
        return start(not a.no_open)
    if a.action == "stop":
        return stop()
    if a.action == "status":
        up = healthy()
        print(f"{NAME}: {'running at ' + URL if up else 'stopped'}")
        return 0 if up else 1
    ok, detail = install()
    print(detail, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
