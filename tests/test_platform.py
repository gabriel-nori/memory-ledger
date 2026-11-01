"""What differs between macOS, Linux and Windows (scripts/_platform.py), against a THROWAWAY home.

    python -m pytest tests/test_platform.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import _platform
import graph
import trigger

UTF8 = "utf-8"
NO_SUCH_PROGRAM = "no-such-program-xyz"
SLEEP_FOREVER = "import time; time.sleep(60)"
INJECTION = "mundo; echo injected"
KILL_WAIT_STEPS = 50
KILL_WAIT_STEP_S = 0.1


# ------------------------------------------------------- finding programs
def test_a_path_is_used_as_given_with_tilde_expanded():
    assert _platform.resolve_bin("~/bin/claude") == str(Path("~/bin/claude").expanduser())


def test_a_bare_name_is_looked_up_on_path():
    assert _platform.resolve_bin("python3") == (shutil.which("python3") or "python3")


def test_a_name_that_is_nowhere_comes_back_as_is_so_the_error_names_it():
    assert _platform.resolve_bin(NO_SUCH_PROGRAM) == NO_SUCH_PROGRAM


def test_the_trigger_engine_resolves_claude_the_same_way():
    assert trigger.claude_bin({"claude": {"bin": NO_SUCH_PROGRAM}}) == NO_SUCH_PROGRAM


# ------------------------------------------------------------ interpreters
def test_a_py_runs_with_this_python():
    assert _platform.interpreter("python", Path("x.py")) == [sys.executable]


def test_a_sh_runs_with_the_bash_on_path():
    sh = _platform.interpreter("shell", Path("x.sh"))
    assert sh is not None and Path(sh[0]).name in ("bash", "bash.exe", "sh"), sh


def test_a_ps1_runs_with_powershell_by_file_without_the_profile_or_not_at_all():
    ps = _platform.interpreter("powershell", Path("x.ps1"))
    if not _platform.powershell():
        assert ps is None
    else:
        assert ps[-1] == "-File" and "-NoProfile" in ps


def test_a_bat_runs_with_cmd_on_windows_only():
    assert (_platform.interpreter("batch", Path("x.bat")) is None) != _platform.WINDOWS


def test_an_unknown_language_has_no_interpreter():
    assert _platform.interpreter("text", Path("x.txt")) is None


@pytest.mark.parametrize("name, lang", [("a.ps1", "powershell"), ("a.bat", "batch"), ("a.cmd", "batch")])
def test_graph_knows_ps1_and_bat(name, lang):
    assert graph._script_lang(name, "") == lang


# ----------------------------------- a script that is not executable runs
@pytest.fixture
def hello_script(brain_home: Path) -> Path:
    graph.scripts_dir().mkdir(parents=True, exist_ok=True)
    script = graph.scripts_dir() / "hello.sh"
    script.write_text('echo "olá $1"\n', encoding=UTF8)
    script.chmod(0o644)
    return script


def test_its_argv_is_the_interpreter_and_the_path_as_separate_items(hello_script):
    argv, err = trigger.script_argv("hello.sh")
    assert not err, err
    assert argv[-1] == str(hello_script)
    assert len(argv) == 2


@pytest.mark.skipif(_platform.interpreter("shell", Path("x.sh")) is None, reason="no shell interpreter")
def test_an_argument_is_data_never_shell_text(hello_script):
    argv, _ = trigger.script_argv("hello.sh")
    out = subprocess.run([*argv, INJECTION], capture_output=True, text=True,
                         encoding=UTF8, errors="replace").stdout
    assert out.strip() == f"olá {INJECTION}"


# --------------------------------------------------------------- processes
def test_this_process_is_alive():
    assert _platform.pid_alive(os.getpid())


def test_a_pid_that_exited_is_not_alive_and_asking_signals_nothing():
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    assert not _platform.pid_alive(dead.pid)


@pytest.mark.parametrize("pid", [0, -1])
def test_a_pid_that_cannot_exist_is_not_alive(pid):
    assert not _platform.pid_alive(pid)


def test_kill_tree_takes_the_grandchildren_with_it():
    # a parent that starts a grandchild and prints its pid, then sleeps
    tree = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess, sys, time\n"
         f"c = subprocess.Popen([sys.executable, '-c', {SLEEP_FOREVER!r}])\n"
         "print(c.pid, flush=True)\n"
         "time.sleep(60)"],
        stdout=subprocess.PIPE, text=True, encoding=UTF8, errors="replace",
        **_platform.group_kwargs())
    try:
        grandchild = int(tree.stdout.readline())
        _platform.kill_tree(tree)
        tree.wait(timeout=10)
        for _ in range(KILL_WAIT_STEPS):
            if not _platform.pid_alive(grandchild):
                break
            time.sleep(KILL_WAIT_STEP_S)
        assert not _platform.pid_alive(grandchild)
        # safe on a process that already exited, and never our own group
        _platform.kill_tree(tree)
        assert tree.poll() is not None
        assert _platform.pid_alive(os.getpid())
    finally:
        tree.stdout.close()
        if tree.poll() is None:
            tree.kill()


# --------------------------------------------------------- secrets and links
def test_a_secret_file_is_made_owner_only(brain_home):
    secret = brain_home / "secret.json"
    secret.write_text("{}", encoding=UTF8)
    assert _platform.owner_only(secret)
    if not _platform.WINDOWS:
        assert (secret.stat().st_mode & 0o777) == 0o600, oct(secret.stat().st_mode)


@pytest.fixture
def linked(brain_home: Path):
    (brain_home / "legacy").mkdir()
    how = _platform.link_dir(brain_home / "canonical", brain_home / "legacy")
    (brain_home / "legacy" / "x").write_text("1", encoding=UTF8)
    return brain_home, how


def test_a_directory_link_reaches_its_target(linked):
    home, _ = linked
    assert (home / "canonical" / "x").read_text(encoding=UTF8) == "1"


def test_it_is_a_link_symlink_or_junction(linked):
    home, how = linked
    assert _platform.is_link(home / "canonical")
    assert how in ("symlink", "junction")


def test_a_relative_symlink_when_the_two_are_siblings(linked):
    home, how = linked
    if how != "symlink":
        pytest.skip("not a symlink on this platform")
    assert os.readlink(home / "canonical") == "legacy"


# ------------------------------------------------------------- re-exec
def test_reexec_keeps_the_pid_that_was_started_and_hands_back_its_exit_code(tmp_path: Path):
    """On Windows `os.execv` exits the old process at once while the new one runs
    on, so a caller that waits on the pid sees "done" for a server still alive."""
    script = tmp_path / "again.py"
    script.write_text(
        "import sys; sys.path.insert(0, %r)\nimport _platform\n"
        "if len(sys.argv) > 1: sys.exit(7)\n"
        "_platform.reexec([sys.executable, __file__, 'second'])\n" % str(Path(_platform.__file__).parent),
        encoding=UTF8)
    started = time.time()
    done = subprocess.run([sys.executable, str(script)], check=False, timeout=60)
    assert done.returncode == 7
    assert time.time() - started < 60
