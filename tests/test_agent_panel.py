"""Ledger Agent, the floating terminal box (scripts/agent_panel.py), against a
THROWAWAY brain. The AppKit box is not opened and nothing is compiled: these
cover what the launcher hands the box, the pinned SwiftTerm checkout it insists
on, and the server's detached launch.

    python -m pytest tests/test_agent_panel.py

The box's own size handling has a self-test in the app itself:
LEDGER_AGENT_SELFTEST=1 <home>/LedgerAgentTerminal.app/Contents/MacOS/LedgerAgentTerminal "" t /tmp "sleep 30"
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

import agent_panel


class TestCommand:
    def test_plain_claude(self):
        assert shlex.split(agent_panel.claude_command([]))[-1] == "claude"

    def test_the_box_hides_the_ledger_and_dora_status_segments(self):
        words = shlex.split(agent_panel.claude_command([]))
        assert words[:2] == ["exec", "env"]
        assert {"LEDGER_AGENT=1", "SHOW_LEDGER=0", "SHOW_DORA=0"} <= set(words[2:words.index("claude")])

    def test_arguments_reach_claude_quoted(self):
        cmd = agent_panel.claude_command(["--model", "claude-opus-5-5", "-p", "it's; rm -rf ~"])
        words = shlex.split(cmd)
        assert words[words.index("claude") + 1:] == ["--model", "claude-opus-5-5", "-p", "it's; rm -rf ~"]

    def test_the_box_gets_icon_subtitle_folder_command(self, tmp_path):
        argv = agent_panel.box_argv(Path("/x/box"), tmp_path, ["--continue"])
        assert argv[0] == "/x/box" and argv[3] == str(tmp_path)
        assert argv[4] == agent_panel.claude_command(["--continue"])

    def test_the_box_gets_the_default_icon_when_none_is_set(self, brain_home, tmp_path):
        assert agent_panel.box_argv(Path("/x/box"), tmp_path, [])[1] == str(agent_panel.ICON)

    def test_the_box_gets_the_uploaded_icon_when_one_is_set(self, brain_home, tmp_path):
        icon = brain_home / "agent-icon.png"
        icon.write_bytes(b"\x89PNG")
        agent_panel.graph.save_settings({"agent_icon": icon.name})
        assert agent_panel.box_argv(Path("/x/box"), tmp_path, [])[1] == str(icon)

    def test_a_missing_uploaded_icon_falls_back_to_the_default(self, brain_home, tmp_path):
        agent_panel.graph.save_settings({"agent_icon": "agent-icon.png"})
        assert agent_panel.box_argv(Path("/x/box"), tmp_path, [])[1] == str(agent_panel.ICON)

    def test_a_setting_cannot_point_outside_the_brain_home(self, brain_home, tmp_path):
        agent_panel.graph.save_settings({"agent_icon": "../../etc/x.png"})
        assert agent_panel.icon_path() == agent_panel.ICON

    def test_the_title_follows_the_brain_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_panel.graph, "brain_name", lambda: "Dora")
        assert agent_panel.box_argv(Path("/x/box"), tmp_path, [])[5] == "Dora agent"

    def test_it_opens_on_sonnet_by_default(self):
        assert agent_panel.with_default_model([]) == ["--model", "sonnet"]
        assert agent_panel.with_default_model(["--continue"]) == ["--model", "sonnet", "--continue"]

    def test_a_chosen_model_wins(self):
        for args in (["--model", "claude-opus-5-5"], ["--continue", "--model=opus"]):
            assert agent_panel.with_default_model(args) == args

    def test_the_cli_passes_sonnet_to_the_box(self, tmp_path, monkeypatch):
        seen = {}
        monkeypatch.setattr(agent_panel, "run", lambda cwd, args: seen.update(args=args) or 0)
        assert agent_panel.main(["--cwd", str(tmp_path)]) == 0
        assert seen["args"] == ["--model", "sonnet"]

    def test_home_is_shortened_in_the_subtitle(self):
        assert agent_panel.subtitle(Path.home() / "dev" / "x") == "~/dev/x"


class TestBuild:
    def test_a_checkout_at_another_commit_is_refused(self, brain_home):
        repo = agent_panel.build_dir() / f"SwiftTerm-{agent_panel.SWIFTTERM_TAG}"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "not swiftterm"], check=True)
        files, err = agent_panel.swiftterm_sources()
        assert files == [] and "expected 5d1440684414" in err

    def test_an_up_to_date_app_is_not_rebuilt(self, brain_home, monkeypatch):
        exe = agent_panel.app_dir() / "Contents" / "MacOS" / agent_panel.APP_NAME
        exe.parent.mkdir(parents=True)
        exe.write_text("built", encoding="utf-8")
        monkeypatch.setattr(agent_panel.shutil, "which", lambda _n: pytest.fail("tried to build"))
        assert agent_panel.panel_binary() == (exe, "")


class TestLaunch:
    def test_a_missing_folder_is_refused(self, brain_home):
        res = agent_panel.launch(str(brain_home / "nope"))
        assert not res["ok"] and "no folder" in res["error"]

    def test_it_starts_detached_and_says_it_is_building(self, brain_home, monkeypatch):
        seen = {}
        monkeypatch.setattr(agent_panel.sys, "platform", "darwin")
        monkeypatch.setattr(agent_panel, "running_box", lambda: None)
        monkeypatch.setattr(agent_panel.subprocess, "Popen", lambda argv, **kw: seen.update(argv=argv, **kw))
        res = agent_panel.launch(str(brain_home))
        assert res == {"ok": True, "cwd": str(brain_home), "building": True}
        assert seen["start_new_session"] and seen["argv"][-2:] == ["--cwd", str(brain_home)]

    def test_the_cli_refuses_a_missing_folder(self, brain_home):
        assert agent_panel.main(["--cwd", str(brain_home / "nope")]) == 1


class TestOneBox:
    """Opening the box while it is open never starts a second one."""

    BOX = "/Users/x/.memory-ledger/LedgerAgentTerminal.app/Contents/MacOS/LedgerAgentTerminal"

    def _ps(self, lines):
        return lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "\n".join(lines) + "\n", "")

    def test_the_open_box_is_found_by_its_bundle_path(self, monkeypatch):
        # A build of the same app, and an unrelated process, are not the box.
        monkeypatch.setattr(agent_panel, "_run", self._ps([
            "  101 swift-driver -module-name LedgerAgentTerminal main.swift",
            "  202 " + self.BOX + " /icon.svg ~/dev/x /Users/x/dev/x exec env claude",
            "  303 /bin/zsh -l",
        ]))
        assert agent_panel.running_box() == 202

    def test_no_box_means_none(self, monkeypatch):
        monkeypatch.setattr(agent_panel, "_run", self._ps(["  101 swift-driver -module-name LedgerAgentTerminal"]))
        assert agent_panel.running_box() is None

    def test_launch_says_already_open_and_starts_nothing(self, brain_home, monkeypatch):
        monkeypatch.setattr(agent_panel.sys, "platform", "darwin")
        monkeypatch.setattr(agent_panel, "running_box", lambda: 202)
        monkeypatch.setattr(agent_panel.subprocess, "Popen", lambda *a, **kw: pytest.fail("a second box was started"))
        assert agent_panel.launch(str(brain_home)) == {
            "ok": True, "cwd": str(brain_home), "building": False, "already_open": True}

    def test_the_cli_says_already_open_and_exits_0(self, brain_home, monkeypatch, capsys):
        monkeypatch.setattr(agent_panel.sys, "platform", "darwin")
        monkeypatch.setattr(agent_panel, "running_box", lambda: 202)
        monkeypatch.setattr(agent_panel, "panel_binary", lambda: pytest.fail("the box was built or opened"))
        assert agent_panel.main(["--cwd", str(brain_home)]) == 0
        assert "already open" in capsys.readouterr().err
