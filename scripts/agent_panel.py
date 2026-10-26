#!/usr/bin/env python3
"""Ledger Agent: a floating terminal box, in front of whatever you are doing,
running the `claude` CLI (docs/agents.md, "Ledger Agent"). Its title follows
the brain's name: a brain called Dora opens "Dora agent" (`agent_name()`).

One box at a time: opening it while it is open says so and does nothing.
It is a real terminal, so it behaves like Claude Code anywhere: Ctrl-C stops a
reply, Esc interrupts, `/model`, `/clear` (a clean session), `/resume`, slash
commands and permission prompts all work. Claude starts in the folder the box was
opened from (or `--cwd`); anything after `--` goes to `claude` as is. It always
opens on Sonnet (`--model sonnet`, the same as typing `/model sonnet`) unless
those arguments pick a model themselves.

The box is a tiny AppKit app (agent_terminal.swift) compiled together with
SwiftTerm's sources — a pinned commit, cloned once — into
`<home>/LedgerAgentTerminal.app`. The first run builds it (~1-2 min, needs
`xcode-select --install` and network); later runs open at once.

    python3 scripts/agent_panel.py                       # claude, here
    python3 scripts/agent_panel.py --cwd ~/dev/x         # claude, in ~/dev/x
    python3 scripts/agent_panel.py -- --model claude-opus-5-5 --continue
"""

from __future__ import annotations

import argparse
import fcntl
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph  # noqa: E402

HERE = Path(__file__).resolve().parent
SRC = HERE / "agent_terminal.swift"
ICON = HERE.parent / "frontend" / "public" / "favicon.svg"
APP_NAME = "LedgerAgentTerminal"
DEFAULT_MODEL = "sonnet"   # what the box opens on unless `-- --model …` says otherwise
SWIFTTERM_URL = "https://github.com/migueldeicaza/SwiftTerm"
SWIFTTERM_TAG = "v1.20.0"
SWIFTTERM_COMMIT = "5d14406844143538cd8f8851d2d8a67c1fe443e5"   # what the tag pointed at when pinned
# SwiftTerm's package plugin generates this at build time; swiftc alone needs it written.
BUILD_INFO = f'''public enum SwiftTermBuildInfo {{
    public static let branch: String? = nil
    public static let tag: String? = "{SWIFTTERM_TAG}"
    public static let commit: String? = "{SWIFTTERM_COMMIT}"
    public static let hasUncommittedChanges: Bool? = false
    public static let version: String = "{SWIFTTERM_TAG.lstrip('v')}"
}}
'''
PLIST = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>com.memory-ledger.agent-terminal</string>
<key>CFBundleName</key><string>{APP_NAME}</string>
<key>CFBundleExecutable</key><string>{APP_NAME}</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSUIElement</key><true/>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
"""


def _say(*a: Any) -> None:
    print(*a, file=sys.stderr, flush=True)


def _run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


# ---------------------------------------------------------------- building
def build_dir() -> Path:
    return graph.home_dir() / "build"


def app_dir() -> Path:
    return graph.home_dir() / f"{APP_NAME}.app"


def swiftterm_sources() -> tuple[list[Path], str]:
    """SwiftTerm's macOS sources at the pinned commit, cloned once. A checkout
    that is not at that commit is refused, never built."""
    repo = build_dir() / f"SwiftTerm-{SWIFTTERM_TAG}"
    if not (repo / ".git").is_dir():
        shutil.rmtree(repo, ignore_errors=True)
        repo.parent.mkdir(parents=True, exist_ok=True)
        _say(f"Fetching SwiftTerm {SWIFTTERM_TAG} (once)…")
        r = _run(["git", "clone", "-q", "--depth", "1", "--branch", SWIFTTERM_TAG, SWIFTTERM_URL, str(repo)])
        if r.returncode != 0:
            shutil.rmtree(repo, ignore_errors=True)
            return [], f"could not fetch SwiftTerm: {(r.stderr or '').strip()[-300:]}"
    head = _run(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()
    if head != SWIFTTERM_COMMIT:
        return [], f"SwiftTerm checkout is at {head[:12]}, expected {SWIFTTERM_COMMIT[:12]}; delete {repo}"
    files = sorted(p for p in (repo / "Sources" / "SwiftTerm").rglob("*.swift") if "iOS" not in p.parts)
    return files, ""


def panel_binary() -> tuple[Path | None, str]:
    """The compiled box, rebuilt when the Swift source is newer than it. One
    build at a time: a second caller waits for the first."""
    exe = app_dir() / "Contents" / "MacOS" / APP_NAME
    if exe.exists() and exe.stat().st_mtime >= SRC.stat().st_mtime:
        return exe, ""
    if not shutil.which("swiftc"):
        return None, "needs the Xcode command line tools: xcode-select --install"
    build_dir().mkdir(parents=True, exist_ok=True)
    with open(build_dir() / "agent-terminal.lock", "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if exe.exists() and exe.stat().st_mtime >= SRC.stat().st_mtime:   # built while we waited
            return exe, ""
        sources, err = swiftterm_sources()
        if err:
            return None, err
        work = build_dir() / "agent-terminal"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        shutil.copy(SRC, work / "main.swift")       # top-level code must live in main.swift
        (work / "BuildInfo.swift").write_text(BUILD_INFO, encoding="utf-8")
        _say(f"Building the {agent_name()} box (first run, ~1-2 min)…")
        tmp = work / APP_NAME
        r = _run(["swiftc", "-O", "-module-name", APP_NAME, *map(str, sources),
                  str(work / "main.swift"), str(work / "BuildInfo.swift"), "-o", str(tmp)])
        if r.returncode != 0:
            return None, "could not build the box: " + (r.stderr or "").strip()[-800:]
        exe.parent.mkdir(parents=True, exist_ok=True)
        (app_dir() / "Contents" / "Info.plist").write_text(PLIST, encoding="utf-8")
        shutil.move(str(tmp), str(exe))              # whole file at once: never a half-written app
        _run(["codesign", "-s", "-", "-f", str(app_dir())])
        return exe, ""


# ---------------------------------------------------------------- running
# Only the box's claude sees these, so only its status line changes: the shared
# statusline script drops its 🧬 ledger and 🐬 Dora segments, and any other
# status line can check LEDGER_AGENT=1 to do the same.
BOX_ENV = {"LEDGER_AGENT": "1", "SHOW_LEDGER": "0", "SHOW_DORA": "0"}


def claude_command(claude_args: list[str]) -> str:
    """What the box's login shell runs: `exec env … claude …`, every argument quoted."""
    env = [f"{k}={v}" for k, v in BOX_ENV.items()]
    return " ".join(["exec", "env", *env, "claude", *map(shlex.quote, claude_args)])


def with_default_model(claude_args: list[str]) -> list[str]:
    """Open on DEFAULT_MODEL, unless the arguments already choose a model."""
    if any(a == "--model" or a.startswith("--model=") for a in claude_args):
        return claude_args
    return ["--model", DEFAULT_MODEL, *claude_args]


def agent_name() -> str:
    """What the box is called: the brain's name (the `brain_name` setting) plus
    "agent", so a brain called Dora opens "Dora agent"."""
    return f"{graph.brain_name()} agent"


def subtitle(cwd: Path) -> str:
    home = str(Path.home())
    return "~" + str(cwd)[len(home):] if str(cwd).startswith(home) else str(cwd)


def icon_path() -> Path:
    """The box's icon: the one uploaded in Settings (the `agent_icon` setting),
    else the default favicon."""
    return graph.agent_icon_path() or ICON


def box_argv(exe: Path, cwd: Path, claude_args: list[str]) -> list[str]:
    icon = icon_path()
    return [str(exe), str(icon) if icon.exists() else "", subtitle(cwd), str(cwd),
            claude_command(claude_args), agent_name()]


def running_box() -> int | None:
    """The pid of an open box, or None. Read from `ps`, not `pgrep -f`: on macOS
    pgrep cannot read this app's arguments and answers "not running" while the
    box is right there, which is how a second box used to open on every call."""
    needle = f"{APP_NAME}.app/Contents/MacOS/{APP_NAME}"
    r = _run(["ps", "-axo", "pid=,command="])
    for line in r.stdout.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if cmd.split(" ", 1)[0].endswith(needle) and pid.isdigit():   # the executable, not its arguments
            return int(pid)
    return None


def run(cwd: Path, claude_args: list[str]) -> int:
    if sys.platform != "darwin":
        _say(f"The {agent_name()} box needs macOS.")
        return 1
    if running_box():
        _say(f"The {agent_name()} box is already open.")
        return 0
    exe, err = panel_binary()
    if exe is None:
        _say(err)
        return 1
    # Waits for the box: Ctrl-C here closes it too.
    try:
        return subprocess.call(box_argv(exe, cwd, claude_args), cwd=str(cwd))
    except KeyboardInterrupt:
        return 130


def launch(cwd: str = "") -> dict[str, Any]:
    """Open the box from the brain UI, detached: it outlives the tab, and the
    server never waits on it — not even on the first build."""
    if sys.platform != "darwin":
        return {"ok": False, "error": f"the {agent_name()} box needs macOS"}
    folder = Path(cwd).expanduser() if cwd else HERE.parent
    if not folder.is_dir():
        return {"ok": False, "error": f"no folder {folder}"}
    if running_box():
        return {"ok": True, "cwd": str(folder), "building": False, "already_open": True}
    graph.home_dir().mkdir(parents=True, exist_ok=True)
    log = graph.home_dir() / "agent-terminal.log"
    with open(log, "a", encoding="utf-8") as out:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--cwd", str(folder)],
                         stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         start_new_session=True, cwd=str(folder))
    exe = app_dir() / "Contents" / "MacOS" / APP_NAME
    built = exe.exists() and exe.stat().st_mtime >= SRC.stat().st_mtime   # same test as panel_binary()
    return {"ok": True, "cwd": str(folder), "building": not built}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 epilog="Anything after `--` goes to claude, e.g. -- --model claude-opus-5-5")
    ap.add_argument("--cwd", default="", help="the folder claude starts in (default: this one)")
    ap.add_argument("--build", action="store_true", help="only build the box, then exit")
    a, rest = ap.parse_known_args(argv)
    if rest[:1] == ["--"]:
        rest = rest[1:]
    if a.build:
        exe, err = panel_binary()
        _say(str(exe) if exe else err)
        return 0 if exe else 1
    cwd = Path(a.cwd).expanduser().resolve() if a.cwd else Path.cwd()
    if not cwd.is_dir():
        _say(f"No folder {cwd}")
        return 1
    return run(cwd, with_default_model(rest))


if __name__ == "__main__":
    sys.exit(main())
