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
        return [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
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


def test_ping_reports_the_answer_and_masks_the_key(cli, monkeypatch):
    exe, calls = cli
    providers.save_key("gemini", "sekret-123")
    try:
        assert providers.ping("gemini", str(exe)) == {"ok": True, "answer": "fake answer"}
        assert "ok" in calls()[-1]["stdin"]
        monkeypatch.setenv("FAKE_CLI_MODE", "fail")
        res = providers.ping("gemini", str(exe))
        assert not res["ok"] and "sekret-123" not in res["error"] and "***" in res["error"]
    finally:
        providers.delete_key("gemini")


def test_ping_needs_an_installed_cli():
    res = providers.ping("codex", "definitely-not-installed-cli")
    assert not res["ok"] and "not installed" in res["error"]


def test_install_runs_npm_and_reports_a_failure(module_brain_home, monkeypatch):
    log = module_brain_home / "npm.log"
    npm = make_exe(module_brain_home / "fake-npm",
                   '#!/usr/bin/env python3\nimport os, sys\n'
                   f'open({str(log)!r}, "w").write(" ".join(sys.argv[1:]))\n'
                   'sys.stderr.write("EACCES"); sys.exit(int(os.environ.get("NPM_EXIT", "0")))\n')
    real = providers._platform.resolve_bin
    monkeypatch.setattr(providers._platform, "resolve_bin",
                        lambda n: str(npm) if n == "npm" else real(n))
    ok = providers.install("gemini")
    assert ok["ok"] and log.read_text(encoding="utf-8") == "install -g @google/gemini-cli"
    monkeypatch.setenv("NPM_EXIT", "1")
    bad = providers.install("gemini")
    assert not bad["ok"] and "EACCES" in bad["error"]
    assert providers.install("ollama")["ok"] is False


def test_the_wizard_routes(module_brain_home, monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from brain_api.app import create_app
    client = TestClient(create_app(tmp_path, "no ui"))
    monkeypatch.setattr(flags, "enabled", lambda n: False)
    kinds = [p["kind"] for p in client.get("/api/agent-providers").json()["providers"]]
    assert kinds == ["claude", "ollama"]                       # Gemini and Codex wait for the flag
    assert client.post("/api/agent-provider-key", json={"kind": "gemini", "key": "k"}).status_code == 400
    monkeypatch.setattr(flags, "enabled", lambda n: n == "ai_providers")
    kinds = [p["kind"] for p in client.get("/api/agent-providers").json()["providers"]]
    assert "gemini" in kinds and "codex" in kinds
    r = client.post("/api/agent-provider-key", json={"kind": "codex", "key": "key-xyz"})
    row = next(p for p in r.json()["providers"] if p["kind"] == "codex")
    assert row["has_key"] and "key-xyz" not in r.text
    r = client.post("/api/agent-provider-key", json={"kind": "codex", "remove": True})
    assert not next(p for p in r.json()["providers"] if p["kind"] == "codex")["has_key"]
    r = client.post("/api/agent-provider-install", json={"kind": "codex"})
    assert r.status_code == 400 and "confirm" in r.json()["error"]
    assert r.json()["command"] == "npm install -g @openai/codex"
    r = client.post("/api/agent-config", json={"delegation": {"enabled": True, "targets": ["codex"]}})
    assert r.json()["config"]["delegation"]["targets"] == ["codex"]
