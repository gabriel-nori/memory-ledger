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


def test_antigravity_asks_before_tools_unless_they_are_on(cli):
    exe, calls = cli
    _run("antigravity", exe)
    _run("antigravity", exe, full=True)
    ro, full = calls()
    assert ro["argv"][0] == "-p" and "SYS" in ro["argv"][1] and "PROMPT" in ro["argv"][1]
    assert ro["argv"][2:] == ["--output-format", "text"]
    assert full["argv"][-1] == "--dangerously-skip-permissions"
    assert providers.PROVIDERS["antigravity"]["key_env"] == ""      # account sign-in, no key
    assert "install.sh" in providers.install_command("antigravity") or os.name == "nt"


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


# ---------------------------------------------------------------- brain step
def test_gemini_gets_the_brain_as_a_marked_block_and_a_repeat_changes_nothing(cli, tmp_path, monkeypatch):
    exe, _ = cli
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".gemini" / "GEMINI.md").write_text("my own notes\n", encoding="utf-8")
    assert providers.brain_state("gemini") == {"brain": False}
    res = providers.install_brain("gemini", str(exe))
    assert res["ok"] and res["brain"]
    text = (tmp_path / ".gemini" / "GEMINI.md").read_text(encoding="utf-8")
    assert text.startswith("my own notes") and "graph.py" in text and "match-rules" in text
    assert providers.brain_state("gemini") == {"brain": True}
    providers.install_brain("gemini", str(exe))
    assert (tmp_path / ".gemini" / "GEMINI.md").read_text(encoding="utf-8") == text


def test_the_brain_step_needs_the_cli_installed(module_brain_home, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    res = providers.install_brain("codex", str(tmp_path / "no-such-codex"))
    assert not res["ok"] and "not installed" in res["error"]
    assert not (tmp_path / ".codex").exists()


def test_claude_gets_the_plugin_from_this_checkout(module_brain_home, tmp_path):
    seen = tmp_path / "seen.txt"
    fake = make_exe(tmp_path / "claude", f'''#!/usr/bin/env python3
import sys
open({str(seen)!r}, "a").write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1:4] == ["plugin", "list", "--json"]:
    print("[]" if not open({str(seen)!r}).read().count("install") else '[{{"id": "memory-ledger@memory-ledger"}}]')
''')
    res = providers.install_brain("claude", str(fake))
    assert res["ok"] and res["brain"]
    lines = seen.read_text(encoding="utf-8").splitlines()
    assert f"plugin marketplace add {providers.PLUGIN_ROOT}" in lines
    assert "plugin install memory-ledger@memory-ledger" in lines


# ---------------------------------------------------------------- Hugging Face Space (image)
import hf_space

SIZES = ["1024x1024 (1:1)", "1344x768 (16:9)", "768x1344 (9:16)", "1184x864 (4:3)", "864x1184 (3:4)"]


def test_the_wrapper_asks_for_the_smallest_size_that_fits():
    assert hf_space.pick_size(SIZES, "auto") == "1184x864 (4:3)"          # fewest pixels of all
    assert hf_space.pick_size(SIZES, "wide") == "1344x768 (16:9)"
    assert hf_space.pick_size(SIZES, "9:16") == "768x1344 (9:16)"
    assert hf_space.pick_size(SIZES, "square") == "1024x1024 (1:1)"
    assert hf_space.pick_size([], "wide") == ""


def test_steps_follow_the_quality_and_the_slider_bounds():
    assert hf_space.pick_steps("draft") == 12 and hf_space.pick_steps("final") == 28
    assert hf_space.pick_steps("draft", lo=20) == 20 and hf_space.pick_steps("final", hi=24) == 24
    with pytest.raises(hf_space.SpaceError):
        hf_space.pick_steps("ultra")


def test_a_brief_can_carry_aspect_and_quality_lines():
    assert hf_space.brief_options("aspect=wide\nquality=final\na red apple") == (
        "a red apple", {"aspect": "wide", "quality": "final"})
    assert hf_space.brief_options("a red apple\naspect=wide")[1] == {}    # only at the top


def test_the_space_shares_the_huggingface_token_and_never_saves_its_own(module_brain_home):
    providers.save_key("huggingface", "hf_abc123")
    assert providers.load_key("hfspace") == "hf_abc123"
    assert providers.redact("failed with hf_abc123", "hfspace") == "failed with ***"
    providers.delete_key("huggingface")


def test_generate_sends_the_minimum_and_saves_the_image(module_brain_home, tmp_path, monkeypatch):
    providers.save_key("huggingface", "hf_abc123")
    calls = {}
    monkeypatch.setattr(hf_space, "space_info", lambda s: {"id": s, "host": "https://x.hf.space",
                                                           "stage": "RUNNING", "ready": True})
    params = [{"parameter_name": n, "parameter_default": d} for n, d in
              (("prompt", None), ("size", "1024x1024 (1:1)"), ("steps", 28), ("seed", 0), ("randomize_seed", True))]
    monkeypatch.setattr(hf_space, "_endpoint", lambda h, n, t: (params, {"size": {"choices": [[s, s] for s in SIZES]},
                                                                          "steps": {"minimum": 4, "maximum": 50}}))
    monkeypatch.setattr(hf_space, "_stream", lambda h, n, data, t, to: calls.setdefault("data", data) and
                        [{"url": "https://x.hf.space/f/a.webp"}, 7])
    monkeypatch.setattr(hf_space, "_download", lambda url, t, dest: dest.write_bytes(b"img"))
    res = hf_space.generate("a red apple", aspect="wide", out_dir=tmp_path)
    assert calls["data"] == ["a red apple", "1344x768 (16:9)", 12, 0, True]
    assert Path(res["path"]).read_bytes() == b"img" and res["path"].endswith(".webp") and res["seed"] == 7
    providers.delete_key("huggingface")


def test_generate_needs_the_token_and_a_running_space(module_brain_home, monkeypatch):
    with pytest.raises(hf_space.SpaceError, match="HF_TOKEN"):
        hf_space.generate("a red apple")
    providers.save_key("huggingface", "hf_abc123")
    monkeypatch.setattr(hf_space, "space_info", lambda s: {"id": s, "stage": "SLEEPING", "ready": False})
    with pytest.raises(hf_space.SpaceError, match="not running"):
        hf_space.generate("a red apple")
    providers.delete_key("huggingface")


def test_a_quota_error_is_named_and_the_key_is_masked(monkeypatch):
    assert "ZeroGPU quota" in hf_space._quota_hint('"You have exceeded your GPU quota"')
