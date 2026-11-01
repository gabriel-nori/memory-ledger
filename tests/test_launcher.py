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
