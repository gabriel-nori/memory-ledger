"""Gemini and Codex connectors (scripts/providers.py), against a THROWAWAY brain.

    python -m pytest tests/test_providers.py

The CLIs are replaced by a script that records its argv, env and stdin and
prints a canned answer. No model is called and nothing leaves the machine.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

import agents
import flags
import graph
import providers
from _fakes import make_exe

FAKE = '''#!/usr/bin/env python3
import json, os, sys
mode = os.environ.get("FAKE_CLI_MODE", "")
rec = {"argv": sys.argv[1:], "stdin": sys.stdin.read(),
       "key": os.environ.get("GEMINI_API_KEY", "") or os.environ.get("CODEX_API_KEY", "")}
open(os.environ["FAKE_CLI_LOG"], "a").write(json.dumps(rec) + "\\n")
if mode == "fail":
    sys.stderr.write("auth failed for " + rec["key"])
    sys.exit(1)
print("fake answer")
'''


@pytest.fixture
def cli(module_brain_home: Path, monkeypatch):
    log = module_brain_home / "cli.jsonl"
    log.unlink(missing_ok=True)
    monkeypatch.setenv("FAKE_CLI_LOG", str(log))
    monkeypatch.delenv("FAKE_CLI_MODE", raising=False)
    exe = make_exe(module_brain_home / "fake-cli", FAKE)
    monkeypatch.setattr(flags, "enabled", lambda n: True if n == "ai_providers" else False)

    def calls():
        return [json.loads(x) for x in log.read_text().splitlines()]
    return exe, calls


def _run(kind, exe, full=False, model=""):
    c = {"id": kind, "name": kind, "kind": kind, "bin": str(exe), "timeout_s": 60}
    con = agents.db()
    r = agents.send(con, "hello", start=False)
    reply = dict(con.execute("SELECT * FROM agent_msg WHERE id=?", (r["reply_id"],)).fetchone())
    reply.update(model=model, full_tools=int(full))
    return agents.run_cli(con, reply, c, {"system": "SYS", "prompt": "PROMPT"})


def test_the_new_kinds_need_the_flag(module_brain_home, monkeypatch):
    monkeypatch.setattr(flags, "enabled", lambda n: False)
    res = agents.save_config({"connectors": [{"kind": "gemini"}]})
    assert not res["ok"] and "gemini" not in res["error"]


def test_connectors_for_the_new_kinds_are_saved_when_the_flag_is_on(module_brain_home, cli):
    res = agents.save_config({"connectors": [{"kind": "gemini"}, {"kind": "codex"}]})
    assert res["ok"]
    gem, cod = res["config"]["connectors"]
    assert gem["default_model"] == "auto" and cod["default_model"] == ""


def test_gemini_is_read_only_and_gets_the_prompt_on_stdin(cli):
    exe, calls = cli
    out = _run("gemini", exe, model="flash")
    (call,) = calls()
    assert out["text"] == "fake answer"
    assert call["argv"] == ["-p", "", "-o", "text", "--approval-mode", "plan", "-m", "flash"]
    assert "SYS" in call["stdin"] and "PROMPT" in call["stdin"]


def test_codex_is_read_only_unless_tools_are_on(cli):
    exe, calls = cli
    _run("codex", exe)
    _run("codex", exe, full=True, model="m1")
    ro, rw = calls()
    assert ro["argv"] == ["exec", "--skip-git-repo-check", "--sandbox", "read-only", "-"]
    assert rw["argv"] == ["exec", "--skip-git-repo-check", "--sandbox", "workspace-write",
                          "-m", "m1", "-"]


def test_a_saved_key_rides_in_the_env_and_is_redacted_from_errors(cli, monkeypatch):
    exe, calls = cli
    providers.save_key("gemini", "sekret-123")
    try:
        _run("gemini", exe)
        assert calls()[0]["key"] == "sekret-123"
        monkeypatch.setenv("FAKE_CLI_MODE", "fail")
        with pytest.raises(agents.AgentError) as e:
            _run("gemini", exe)
        assert "sekret-123" not in str(e.value) and "***" in str(e.value)
    finally:
        providers.delete_key("gemini")


def test_no_saved_key_leaves_the_cli_to_sign_in_its_own_way(cli):
    exe, calls = cli
    _run("codex", exe)
    assert calls()[0]["key"] == ""


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_the_key_file_is_private_and_never_listed(module_brain_home):
    providers.save_key("codex", "k-abc")
    try:
        p = module_brain_home / providers.SECRETS_DIR / "codex.key"
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        row = next(r for r in providers.listing() if r["kind"] == "codex")
        assert row["has_key"] and "k-abc" not in json.dumps(providers.listing())
        assert "k-abc" not in json.dumps(agents.load_config())
    finally:
        providers.delete_key("codex")


def test_a_key_with_spaces_is_refused():
    with pytest.raises(ValueError):
        providers.save_key("gemini", "two words")
    with pytest.raises(ValueError):
        providers.save_key("ollama", "x")


def test_detect_reports_a_missing_cli():
    assert providers.detect("gemini", "definitely-not-installed-cli")["installed"] is False
    assert providers.install_command("gemini") == "npm install -g @google/gemini-cli"
