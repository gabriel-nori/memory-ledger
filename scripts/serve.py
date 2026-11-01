#!/usr/bin/env python3
"""memory-ledger backend — the one command that serves the brain.

Reads the same brain the CLI uses (~/.second_brain/graph.db, or $SECOND_BRAIN_DB /
$SECOND_BRAIN_HOME) and serves two things over one port:

  * the JSON API (`/api/*`), a FastAPI app in `scripts/brain_api/`, whose
    OpenAPI document is generated at runtime and browsable at **/api/docs**;
  * the React single-page app built from `frontend/` into `frontend/dist`,
    which is not in git: this file builds it on first start.

    python3 scripts/serve.py [--port 8770] [--no-open] [--no-reload] [--no-write]

**This file is stdlib-only, and still the only thing a user runs.** The API
needs FastAPI and uvicorn, and the engine must not (CONTRIBUTING.md): a
dependency in graph.py is a dependency in every shell's startup path. So the
dependencies live in a venv this file owns — built on first run from
`scripts/requirements-server.txt` into a cache directory, keyed by a hash of that
file, and re-entered on every start with `os.execv`. `$MEMORY_LEDGER_VENV`
points it at a venv of your own instead. See `server_python`.

**npm is needed for the UI, never for the API.** The build output is not
committed; the server builds it on first start, and rebuilds it when it finds
the source NEWER than it (see `ensure_ui`). With no node on the machine the
server still starts and every endpoint answers — only the page is a 503 that
says why. `npm run dev` in `frontend/` is the real loop for UI work, and it
proxies /api straight back here.

It restarts itself when its own code changes, so a long-running server never
serves a page newer than the API behind it. See RELOAD below.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import _platform  # noqa: E402

# The SPA, and where it is built from. `dist/` is a build artifact and is NOT
# in git; `ensure_ui` builds it when it is missing and refreshes it when the
# source has moved ahead of it.
FRONTEND = ROOT / "frontend"
DIST = FRONTEND / "dist"
INDEX = DIST / "index.html"

# Changing any of these means the bundle on disk no longer describes the source,
# so it is rebuilt. Config counts: a vite.config.ts edit changes the output
# without touching a single component.
UI_CONFIG_FILES = ("package.json", "package-lock.json", "vite.config.ts",
                   "tsconfig.json", "index.html")

# How much newer a source file has to be before it means someone edited it.
# Files written together (a checkout, a copy, a restore from backup) can land
# within moments of the bundle and in any order; a margin keeps that from
# reading as an edit. A real edit lands minutes after a build, never
# microseconds.
STALE_GRACE_NS = 2_000_000_000

# What to say when the page could not be built. It names the reason and the one
# command that fixes it, because "the server is broken" is what a bare 404 says
# and it is never what happened here.
MISSING_UI = """<!doctype html>
<meta charset="utf-8">
<title>memory-ledger — the UI is not built</title>
<style>body{{font:15px/1.6 ui-sans-serif,system-ui,sans-serif;max-width:42rem;
margin:12vh auto;padding:0 1.5rem;background:#0d1117;color:#e6edf3}}
code{{background:#161b22;padding:2px 6px;border-radius:5px;color:#58a6ff}}
pre{{background:#161b22;padding:12px 14px;border-radius:8px;overflow:auto}}
a{{color:#58a6ff}} .why{{color:#8b949e}}</style>
<h1>The UI is not built</h1>
<p class="why">{reason}</p>
<p>The page is built from <code>frontend/</code> on first start and is not in
git, so building it needs Node.js. Install it and restart the server, or build
the page yourself:</p>
<pre>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</pre>
<p><b>The API is running and fully usable meanwhile:</b>
<a href="/api/docs">/api/docs</a>.</p>
"""

# Set by ensure_ui() when the build could not run, so the page can say WHY
# rather than only that something is missing.
UI_PROBLEM = ""

# ---------------------------------------------------------------------------
# BUILDING THE UI
#
# The frontend is a React app with a real toolchain, and its output is NOT in
# git: a generated artifact in git is a second copy of the source, and it drifts
# from it silently. So this function produces the bundle — on first start, when
# there is none — and rebuilds it whenever it finds a source file newer than it,
# so the person who changed the UI sees the change without a command to remember.
#
# npm is therefore needed for the UI, never for the API. A machine with no npm
# still starts the server and answers every endpoint; only the page is missing,
# and it says why. If a bundle is already on disk it keeps being served, and the
# terminal only notes that it could not refresh one it thought was stale.
# ---------------------------------------------------------------------------
def _ui_sources():
    """Every file whose change makes the current bundle stale."""
    for name in UI_CONFIG_FILES:
        path = FRONTEND / name
        if path.exists():
            yield path
    src = FRONTEND / "src"
    if src.is_dir():
        for path in src.rglob("*"):
            # Tests never reach the bundle, so a test edit is not a rebuild.
            if path.is_file() and ".test." not in path.name:
                yield path


def ui_stale() -> str:
    """Why the bundle needs rebuilding, or "" when it is current.

    mtime, not a content hash: this runs on every start, and hashing a few
    hundred files to save a one-second vite build would cost more than it saves.

    The comparison carries STALE_GRACE_NS, so files written together with the
    bundle read as current rather than as an edit to the UI.
    """
    if not INDEX.exists():
        return "no bundle yet"
    built = INDEX.stat().st_mtime_ns
    newest, newest_path = 0, None
    for path in _ui_sources():
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            continue
        if mtime > newest:
            newest, newest_path = mtime, path
    if newest > built + STALE_GRACE_NS:
        return f"{newest_path.name} changed since the last build"
    return ""


def _run(cmd, cwd, label) -> tuple[bool, str]:
    """Run one toolchain command, showing its output as it goes.

    Streamed rather than captured because `npm install` can take half a minute
    and a silent terminal during it reads as a hang.
    """
    print(f"  {label}…", flush=True)
    try:
        proc = subprocess.run(cmd, cwd=str(cwd))
    except FileNotFoundError:
        return False, f"{cmd[0]} is not installed"
    except Exception as e:                       # pragma: no cover - defensive
        return False, f"{label} could not start: {e}"
    if proc.returncode != 0:
        return False, f"{label} failed (exit {proc.returncode})"
    return True, ""


def ensure_ui(auto: bool = True, force: bool = False) -> str:
    """Build the bundle if it is missing, or rebuild it if the source moved.
    Returns "" if fine, else why not.

    Never raises and never exits: the API must start whether or not the UI can
    be built. When a bundle is already on disk, the failures below leave it in
    place and are reported as "possibly stale". With no `frontend/dist` at all
    (a fresh checkout on a machine without node) the page is a 503 that says
    why and how to fix it.
    """
    global UI_PROBLEM
    UI_PROBLEM = ""

    if not FRONTEND.is_dir():
        UI_PROBLEM = "the frontend/ directory is missing from this checkout"
        return UI_PROBLEM

    reason = "--rebuild was passed" if force else ui_stale()
    if not reason:
        return ""
    if not auto:
        UI_PROBLEM = f"{reason}, and --no-build was passed"
        return UI_PROBLEM

    _platform.use_portable_node()
    if not shutil.which("npm"):
        import _node
        print("  no npm on PATH: downloading a portable Node.js", flush=True)
        why = _node.portable_node()
        if why:
            print(f"  portable Node.js failed: {why}", flush=True)
        _platform.use_portable_node()
    npm = shutil.which("npm")
    if not npm:
        UI_PROBLEM = (f"{reason}, and npm is not on PATH. "
                      "The portable Node.js could not be downloaded either; install Node.js to build the page.")
        print(f"  UI not built: {UI_PROBLEM}", flush=True)
        return UI_PROBLEM

    print(f"building the UI ({reason})", flush=True)

    if not (FRONTEND / "node_modules").is_dir():
        # `npm ci` is the reproducible install and needs the lockfile; without
        # one it refuses outright, so fall back rather than fail the whole start.
        if (FRONTEND / "package-lock.json").exists():
            ok, err = _run([npm, "ci", "--no-audit", "--no-fund"],
                           FRONTEND, "installing dependencies (npm ci)")
        else:
            ok, err = _run([npm, "install", "--no-audit", "--no-fund"],
                           FRONTEND, "installing dependencies (npm install)")
        if not ok:
            UI_PROBLEM = err
            return UI_PROBLEM

    ok, err = _run([npm, "run", "build"], FRONTEND, "compiling")
    if not ok:
        UI_PROBLEM = err
        return UI_PROBLEM

    if not INDEX.exists():
        UI_PROBLEM = "the build reported success but wrote no index.html"
        return UI_PROBLEM

    print("  UI ready", flush=True)
    return ""
# ---------------------------------------------------------------------------
# THE SERVER'S ENVIRONMENT
#
# The API runs on FastAPI + uvicorn, and the promise is still one command with
# no install step. So this file builds the environment itself: a venv in a
# cache directory, named by a hash of the requirements file and the Python
# version, so bumping a pin (or the interpreter) builds a new one beside the old
# rather than mutating a working install mid-flight.
#
# The build happens in a temp directory that is renamed into place only after
# pip succeeded — a half-installed venv is never mistaken for a ready one, and
# two servers starting at once cannot both write into the same directory.
# ---------------------------------------------------------------------------
REQUIREMENTS = HERE / "requirements-server.txt"
READY = ".memory-ledger-ready"
RESPAWN_ENV = "SECOND_BRAIN_VIZ_RESPAWN"


def _venv_key() -> str:
    h = hashlib.sha256(REQUIREMENTS.read_bytes())
    h.update(f"{sys.version_info.major}.{sys.version_info.minor}".encode())
    return h.hexdigest()[:12]


def venv_dir() -> Path:
    override = os.environ.get("MEMORY_LEDGER_VENV", "").strip()
    if override:
        return Path(override).expanduser()
    return _platform.cache_dir() / "memory-ledger" / f"server-{_venv_key()}"


def _venv_python(d: Path) -> Path:
    return _platform.venv_python(d)


def _in(d: Path) -> bool:
    try:
        return Path(sys.prefix).resolve() == d.resolve()
    except OSError:
        return False


def _build_venv(d: Path) -> str:
    """Create `d` with the requirements installed. Returns "" or why not."""
    d.parent.mkdir(parents=True, exist_ok=True)
    tmp = d.parent / f"{d.name}.building-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"setting up the server's environment (one time) in {d}", flush=True)
    try:
        subprocess.run([sys.executable, "-m", "venv", str(tmp)], check=True)
    except (subprocess.CalledProcessError, OSError) as e:
        shutil.rmtree(tmp, ignore_errors=True)
        return (f"could not create a venv ({e}). On Debian/Ubuntu: "
                "apt install python3-venv")
    py = _venv_python(tmp)
    pip = [str(py), "-m", "pip", "install", "--disable-pip-version-check",
           "--no-input", "-q", "-r", str(REQUIREMENTS)]
    print("  installing fastapi and uvicorn…", flush=True)
    if subprocess.run(pip).returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        return ("pip could not install scripts/requirements-server.txt — this needs "
                "the network once. Or point $MEMORY_LEDGER_VENV at a venv that has "
                "fastapi and uvicorn.")
    (tmp / READY).write_text(_venv_key(), encoding="utf-8")
    try:
        tmp.rename(d)
    except OSError:
        # Another server finished first; theirs is as good as ours.
        shutil.rmtree(tmp, ignore_errors=True)
        if not (d / READY).exists():
            return f"could not move the new environment into {d}"
    print("  environment ready", flush=True)
    return ""


def server_python() -> Path:
    """The interpreter the server runs under, building its venv if needed.

    Exits with the reason when it cannot — there is no degraded mode to fall
    back to, and a traceback from pip is not an explanation."""
    d = venv_dir()
    if _in(d):
        return Path(sys.executable)
    if os.environ.get("MEMORY_LEDGER_VENV", "").strip():
        # Yours, so used as it is: never installed into behind your back.
        if not _venv_python(d).exists():
            sys.exit(f"memory-ledger: the server cannot start: "
                     f"$MEMORY_LEDGER_VENV={d} is not a venv")
        return _venv_python(d)
    if not (d / READY).exists():
        why = _build_venv(d)
        if why:
            sys.exit(f"memory-ledger: the server cannot start: {why}")
    return _venv_python(d)


def _enter_server_env() -> None:
    """Re-exec this file under the server's venv, unless already in it."""
    py = server_python()
    if _in(venv_dir()):
        return
    _platform.reexec([str(py), str(Path(__file__).resolve()), *sys.argv[1:]])


# ---------------------------------------------------------------------------
# RELOAD
#
# index.html is read from disk on every request, but this module, the API and
# the engine are loaded once at startup. A server left running for hours would
# therefore serve a CURRENT page against a STALE API — the page asks for a filter
# the process has never heard of and silently shows nothing. So the process
# notices its own code changed and re-execs.
#
# Polling mtimes beats a filesystem-watch dependency: a handful of files, once a
# second. And unlike uvicorn's own reloader, this one refuses to restart into
# code that does not compile — a typo mid-edit must not take the server down.
# ---------------------------------------------------------------------------
_RESTART = threading.Event()


def _watched() -> list[Path]:
    """This module, the API package, the engine modules it serves, the page."""
    out = [Path(__file__).resolve(), INDEX, REQUIREMENTS]
    out += sorted((HERE / "brain_api").glob("*.py"))
    out += [HERE / "graph.py", HERE / "brain_query.py", HERE / "doctor.py",
            HERE / "trigger.py", HERE / "_switch.py"]
    return out


def _stamp(paths) -> dict:
    out = {}
    for p in paths:
        try:
            out[str(p)] = p.stat().st_mtime_ns
        except OSError:
            out[str(p)] = None
    return out


def _replies_running() -> bool:
    try:
        import graph
        con = graph.connect()
        try:
            return con.execute("SELECT 1 FROM agent_msg WHERE status='running' "
                               "AND role='agent' LIMIT 1").fetchone() is not None
        finally:
            con.close()
    except Exception:
        return False


def _watch(server, interval=1.0) -> None:
    paths = _watched()
    seen = _stamp(paths)
    while not _RESTART.is_set():
        time.sleep(interval)
        now = _stamp(paths)
        changed = [p for p in now if now[p] != seen.get(p)]
        if not changed:
            continue
        seen = now
        broken = []
        for c in changed:
            if c.endswith(".py"):
                try:
                    compile(Path(c).read_text(encoding="utf-8"), c, "exec")
                except SyntaxError as e:
                    broken.append(f"{Path(c).name}:{e.lineno}: {e.msg}")
                except OSError:
                    pass
        if broken:
            print("  reload held — does not compile:", flush=True)
            for b in broken:
                print(f"    {b}", flush=True)
            continue
        # a re-exec kills the threads running agent replies, and the chat then
        # shows Retry: hold the restart until none is running (at most 30 min)
        waited = 0.0
        while _replies_running() and waited < 1800:
            if waited == 0:
                print("  restart waits for the running agent replies", flush=True)
            time.sleep(2.0)
            waited += 2.0
        print(f"  changed: {', '.join(Path(c).name for c in changed)} — restarting",
              flush=True)
        _RESTART.set()
        # uvicorn closes the listening socket on its way out, so the port is
        # free to rebind by the time main() re-execs.
        server.should_exit = True
        return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--no-reload", action="store_true",
                    help="do not restart when the server's own code changes")
    ap.add_argument("--no-write", action="store_true",
                    help="serve read-only — refuse every POST. Use this when "
                         "binding --host 0.0.0.0, since the write API is "
                         "unauthenticated and anyone on the network could edit "
                         "your brain.")
    ap.add_argument("--no-build", action="store_true",
                    help="never run npm. A bundle already on disk is served "
                         "as it is, even if your own source is newer than it; "
                         "with none, the page says so and the API still runs.")
    ap.add_argument("--rebuild", action="store_true",
                    help="rebuild the UI even when it looks current")
    ap.add_argument("--no-trigger", action="store_true",
                    help="do not start the Slack/Jira trigger watchers, even when "
                         "<home>/trigger.json enables them (docs/trigger.md). "
                         "--no-write implies this.")
    args = ap.parse_args()

    _enter_server_env()

    # keep the log readable when it is a file rather than a terminal
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, OSError):
            pass

    import uvicorn
    import graph
    from brain_api import state
    from brain_api.app import create_app

    state.WRITABLE = not args.no_write
    # A read-only server must not be acting on its owner's behalf.
    state.TRIGGERS = not (args.no_trigger or args.no_write)
    # nor pulling into, or pushing from, a brain it was told not to write
    state.SYNC = not args.no_write
    graph.connect().close()  # ensure the brain exists

    # Refresh BEFORE binding, so the browser opens on the page it will keep.
    # It never aborts the start, and it never DELETES the bundle to force the
    # issue: on a machine that cannot rebuild, removing it would trade a
    # possibly-stale page for no page at all.
    state.UI_PROBLEM = ensure_ui(auto=not args.no_build, force=args.rebuild)

    url = f"http://{args.host}:{args.port}/"
    # a restart re-execs with the same argv; don't open a new tab each time
    respawned = os.environ.pop(RESPAWN_ENV, "") == "1"
    if not respawned:
        print(f"memory-ledger → {url}")
        print(f"  brain: {graph.db_path()}")
        print(f"  api:   {url}api/docs")
        if UI_PROBLEM and INDEX.exists():
            print(f"  note:  serving the existing UI build; it may be stale ({UI_PROBLEM})")
        elif UI_PROBLEM:
            print(f"  note:  the UI is unavailable ({UI_PROBLEM}); the API is not")
        print("  Ctrl-C to stop.")
    else:
        print(f"  reloaded → {url}")

    server = uvicorn.Server(uvicorn.Config(
        create_app(DIST, MISSING_UI), host=args.host, port=args.port,
        log_level="warning", access_log=False))
    if not args.no_reload:
        threading.Thread(target=_watch, args=(server,), daemon=True).start()
    if not (args.no_open or respawned):
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.run()
    except KeyboardInterrupt:
        pass            # uvicorn re-raises the Ctrl-C it caught, once it is down
    if not _RESTART.is_set():
        print("\nstopped.")
        return
    os.environ[RESPAWN_ENV] = "1"
    _platform.reexec([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]])


if __name__ == "__main__":
    main()
