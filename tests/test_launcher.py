"""scripts/launcher.py: the start/stop cycle against a throwaway home, and the Desktop launcher.

    python -m pytest tests/test_launcher.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import launcher


def test_install_writes_a_launcher_that_runs_start(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "desktop_dir", lambda: tmp_path / "Desktop")
    if launcher._platform.WINDOWS:
        pytest.skip("the .lnk is made through PowerShell and COM")
    ok, path = launcher.install()
    assert ok, path
    text = Path(path).read_text("utf-8")
    assert "launcher.py" in text and "start" in text and sys.executable in text
    assert Path(path).stat().st_mode & 0o100


def test_stop_when_nothing_runs_is_a_no_op(brain_home, capsys):
    assert launcher.stop() == 0
    assert "not running" in capsys.readouterr().out


def test_healthy_is_false_on_a_closed_port(monkeypatch):
    monkeypatch.setattr(launcher, "URL", "http://127.0.0.1:9/")
    assert launcher.healthy(timeout=0.5) is False


def test_stop_leaves_a_reused_pid_alone(brain_home, capsys, monkeypatch):
    import os
    launcher.pid_file().write_text(str(os.getpid()), encoding="utf-8")   # alive, but not serve.py
    monkeypatch.setattr(launcher._platform, "pid_command", lambda pid: "/bin/sleep 100")
    sent = []
    monkeypatch.setattr(launcher.os, "killpg", lambda *a: sent.append(a), raising=False)
    monkeypatch.setattr(launcher.os, "kill", lambda *a: sent.append(a))
    assert launcher.stop() == 0
    assert [a for a in sent if a[1] != 0] == [] and "not running" in capsys.readouterr().out
    assert not launcher.pid_file().exists()


def test_system_packages_per_os():
    have = lambda names: (lambda n: f"/bin/{n}" if n in names else None)
    assert launcher.system_packages("darwin", have({"brew"}))[0] == ["brew", "install", "ffmpeg"]
    assert launcher.system_packages("darwin", have({"brew", "ffmpeg"})) == ([], "")
    cmd, note = launcher.system_packages("darwin", have(set()))
    assert cmd == [] and "brew.sh" in note
    cmd, _ = launcher.system_packages("linux", have({"dnf"}))
    assert cmd[-3:] == ["dnf", "install", "-y", "espeak-ng"][-3:] and "espeak-ng" in cmd
    assert launcher.system_packages("linux", have({"espeak-ng"})) == ([], "")
    assert launcher.system_packages("win32", have(set())) == ([], "")


def test_offer_voice_asks_once_and_runs_setup(brain_home, monkeypatch):
    monkeypatch.setattr(launcher, "voice_ready", lambda: False)
    monkeypatch.setattr(launcher, "system_packages", lambda: (["pkg", "install"], ""))
    calls = []
    fake = lambda cmd, **kw: calls.append(cmd) or type("R", (), {"returncode": 0})()
    assert launcher.offer_voice(ask=lambda _: "y", run=fake, interactive=True) == "installed"
    assert calls[0] == ["pkg", "install"] and calls[1][1:] == [str(launcher.HERE / "setup.py"), "--voice", "--skip-ui"]
    assert launcher.offer_voice(ask=lambda _: "y", run=fake, interactive=True) == "skipped"


def test_offer_voice_remembers_a_no_and_skips_when_not_a_tty(brain_home, monkeypatch):
    monkeypatch.setattr(launcher, "voice_ready", lambda: False)
    assert launcher.offer_voice(interactive=False) == "not interactive"
    assert launcher.offer_voice(ask=lambda _: "n", interactive=True) == "declined"
    assert launcher.offer_voice(ask=lambda _: "y", interactive=True) == "skipped"
