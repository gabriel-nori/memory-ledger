"""Shared pytest fixtures for the brain's suites.

    python -m pytest tests

Every suite here runs the real scripts against a THROWAWAY brain and never
against the one in the owner's home. This file is where that is made true once:

- `pytest_configure` scrubs the environment before a single test module is
  imported (the Neo4j mirror variables, the integration tokens and the paths
  that would point a script at a real brain) and aims `$SECOND_BRAIN_HOME` at a
  directory that is deleted when the run ends;
- `brain_home` / `module_brain_home` give a test (or a whole module that builds
  on its own earlier state) its own empty brain, with the variable set;
- `brain_env` builds the environment for a child process;
- `fake_exe`, `http_server` and `serve_process` are the fakes and servers the
  suites talk to, written to behave the same on Linux, macOS and Windows.

Stdlib only, like the suites. Every text-mode call names its encoding:
`tests/test_encoding.py` walks this file too.
"""

from __future__ import annotations

import atexit
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
SCRIPTS = ROOT / "scripts"
SERVE = SCRIPTS / "serve.py"
GRAPH = SCRIPTS / "graph.py"

for _p in (str(SCRIPTS), str(TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _fakes import make_exe  # noqa: E402

HOME_VAR = "SECOND_BRAIN_HOME"
NO_AUTOSYNC_VAR = "SECOND_BRAIN_NO_AUTOSYNC"
OWNER_VAR = "MEMORY_LEDGER_OWNER"
OWNER_NAME = "Owner"
UTF8 = "utf-8"
LOCALHOST = "127.0.0.1"

# What would point a script at a real service or a real brain. A prefix removes
# every variable that starts with it; a name removes just that one.
SCRUB_PREFIXES = (
    "SECOND_BRAIN_NEO4J", "MEMORY_LEDGER_NEO4J",
    "MEMORY_LEDGER_SLACK_", "MEMORY_LEDGER_JIRA_", "MEMORY_LEDGER_GITHUB_",
    "MEMORY_LEDGER_B2B_",
)
SCRUB_NAMES = (
    "SECOND_BRAIN_DB", "MEMORY_LEDGER_DB", "MEMORY_LEDGER_HOME",
    "SECOND_BRAIN_OFF", "MEMORY_LEDGER_OFF",
)

# how long a server may take to come up: a first start builds its own venv
SERVER_START_TIMEOUT_S = 240
SERVER_STOP_TIMEOUT_S = 15

_run_home: Path | None = None


def _scrubbed(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items()
            if not k.startswith(SCRUB_PREFIXES) and k not in SCRUB_NAMES}


def pytest_configure(config: pytest.Config) -> None:
    """Runs before any test module is imported, so a module-level import of a
    script sees a scrubbed environment and a home that is not the owner's."""
    global _run_home
    for key in list(os.environ):
        if key.startswith(SCRUB_PREFIXES) or key in SCRUB_NAMES:
            del os.environ[key]
    _run_home = Path(tempfile.mkdtemp(prefix="second-brain-pytest-"))
    os.environ[HOME_VAR] = str(_run_home)
    os.environ[NO_AUTOSYNC_VAR] = "1"
    atexit.register(shutil.rmtree, _run_home, True)


# ------------------------------------------------------------------- homes
@pytest.fixture
def brain_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty brain for this test alone; `$SECOND_BRAIN_HOME` points at it."""
    home = tmp_path / "brain"
    home.mkdir()
    monkeypatch.setenv(HOME_VAR, str(home))
    monkeypatch.setenv(NO_AUTOSYNC_VAR, "1")
    return home


@pytest.fixture(scope="module")
def module_brain_home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """One brain for a whole module whose tests build on each other's state
    (the order in the file is the order they run in). Same variable, same scrub."""
    home = tmp_path_factory.mktemp("brain")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(HOME_VAR, str(home))
        mp.setenv(NO_AUTOSYNC_VAR, "1")
        yield home


@pytest.fixture
def owner_name(monkeypatch: pytest.MonkeyPatch) -> str:
    """The owner the suites that render a name expect: `Owner`."""
    monkeypatch.setenv(OWNER_VAR, OWNER_NAME)
    return OWNER_NAME


# ------------------------------------------------------------- child processes
@pytest.fixture
def brain_env() -> Callable[..., dict[str, str]]:
    """`brain_env(home, **extra)`: the environment for a child process over
    `home`. A value of None removes that variable. Never carries the Neo4j
    mirror or an integration token from the machine running the suite."""
    def build(home: Path | str | None = None, **extra: str | None) -> dict[str, str]:
        env = _scrubbed(dict(os.environ))
        if home is not None:
            env[HOME_VAR] = str(home)
        env[NO_AUTOSYNC_VAR] = "1"
        for key, value in extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env
    return build


@pytest.fixture
def run_script(brain_env: Callable[..., dict[str, str]]) -> Callable[..., subprocess.CompletedProcess]:
    """`run_script("graph.py", "add", ..., home=..., env={...}, input=...)`: run a
    script of the brain under this interpreter, text in and out as UTF-8 with
    undecodable bytes replaced (never a crash on the reader's side)."""
    def run(script: str | Path, *args: str, home: Path | str | None = None,
            env: dict[str, str | None] | None = None, input: str | None = None,
            cwd: Path | str | None = None, timeout: float = 120,
            check: bool = False) -> subprocess.CompletedProcess:
        path = Path(script)
        if not path.is_absolute():
            path = SCRIPTS / path
        return subprocess.run(
            [sys.executable, str(path), *map(str, args)],
            capture_output=True, text=True, encoding=UTF8, errors="replace",
            env=brain_env(home if home is not None else os.environ[HOME_VAR], **(env or {})),
            input=input, cwd=cwd, timeout=timeout, check=check)
    return run


@pytest.fixture
def free_port() -> Callable[[], int]:
    def pick() -> int:
        with socket.socket() as s:
            s.bind((LOCALHOST, 0))
            return s.getsockname()[1]
    return pick


@pytest.fixture
def fake_exe() -> Callable[[Path, str], Path]:
    """`fake_exe(path, source)`: a fake program that runs on every OS (a shebang
    script on POSIX, a `.py` with a `.cmd` shim on Windows); returns what goes
    in a config's `bin`. See tests/_fakes.py."""
    return make_exe


# ---------------------------------------------------------------------- HTTP
class LocalServer:
    """A `ThreadingHTTPServer` on a free port, serving on a daemon thread."""

    def __init__(self, handler: type[BaseHTTPRequestHandler]) -> None:
        self.server = ThreadingHTTPServer((LOCALHOST, 0), handler)
        self.port: int = self.server.server_address[1]
        self.url = f"http://{LOCALHOST}:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=SERVER_STOP_TIMEOUT_S)


@pytest.fixture
def http_server() -> Iterator[Callable[[type[BaseHTTPRequestHandler]], LocalServer]]:
    """`http_server(HandlerClass)`: a local server the code under test talks to
    instead of Slack, Jira, Ollama or a website. Stopped when the test ends."""
    started: list[LocalServer] = []

    def start(handler: type[BaseHTTPRequestHandler]) -> LocalServer:
        server = LocalServer(handler)
        started.append(server)
        return server

    yield start
    for server in started:
        server.stop()


class ServeProcess:
    """One real `scripts/serve.py` on a free port, over a given brain."""

    def __init__(self, home: Path, port: int, flags: tuple[str, ...],
                 env: dict[str, str], log: Path) -> None:
        from _platform import group_kwargs  # noqa: PLC0415 - needs sys.path set above
        self.home, self.port, self.log = home, port, log
        self.url = f"http://{LOCALHOST}:{port}"
        self._out = open(log, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVE), "--port", str(port), "--no-open", "--no-reload", *flags],
            env=env, stdout=self._out, stderr=subprocess.STDOUT, **group_kwargs())

    # Set once a server fails to come up: the next ones fail at once with the
    # same log instead of each waiting out SERVER_START_TIMEOUT_S.
    never_came_up: str | None = None

    def wait_ready(self, timeout: float = SERVER_START_TIMEOUT_S) -> None:
        if ServeProcess.never_came_up is not None:
            self.stop()
            raise RuntimeError("an earlier server never came up (not retrying):\n"
                               + ServeProcess.never_came_up)
        deadline = time.time() + timeout
        while time.time() < deadline and self.proc.poll() is None:
            try:
                socket.create_connection((LOCALHOST, self.port), timeout=1).close()
                return
            except OSError:
                time.sleep(0.25)
        log = self.log.read_text(encoding=UTF8, errors="replace")
        self.stop()
        ServeProcess.never_came_up = log
        raise RuntimeError("the server never came up:\n" + log)

    def stop(self) -> None:
        """Ctrl-C where the platform has one (the server shuts down cleanly),
        then the whole process group; Windows has no SIGINT for a child in its
        own group, so there it goes straight to the group."""
        from _platform import WINDOWS, kill_tree  # noqa: PLC0415
        if self.proc.poll() is None and not WINDOWS:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=SERVER_STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                pass
        kill_tree(self.proc)
        self.proc.wait()
        self._out.close()


@pytest.fixture
def serve_process(tmp_path: Path, free_port: Callable[[], int],
                  brain_env: Callable[..., dict[str, str]]) -> Iterator[Callable[..., ServeProcess]]:
    """`serve_process(home, *flags, env={...}, wait=True)`: start the real
    server over `home` (default: a fresh brain) and return it once it answers.
    Everything started is stopped when the test ends."""
    started: list[ServeProcess] = []

    def start(home: Path | None = None, *flags: str, env: dict[str, str | None] | None = None,
              wait: bool = True) -> ServeProcess:
        if home is None:
            home = tmp_path / f"serve-brain-{len(started)}"
            home.mkdir()
        proc = ServeProcess(home, free_port(), flags, brain_env(home, **(env or {})),
                            tmp_path / f"server-{len(started)}.log")
        started.append(proc)
        if wait:
            proc.wait_ready()
        return proc

    yield start
    for proc in started:
        proc.stop()
