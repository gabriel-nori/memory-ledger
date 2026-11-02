"""Agents handing tasks to another provider (scripts/delegation.py), against a THROWAWAY brain.

    python -m pytest tests/test_delegation.py

The target CLI is a script that records its stdin and prints a canned answer;
no model is called.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import agents
import delegation
import flags
import graph
from _fakes import make_exe

FAKE = '''#!/usr/bin/env python3
import os, sys
open(os.environ["FAKE_CLI_LOG"], "a").write(sys.stdin.read() + "\\n---\\n")
print("delegated answer")
'''


@pytest.fixture
def setup(module_brain_home: Path, monkeypatch):
    log = module_brain_home / "deleg.log"
    log.unlink(missing_ok=True)
    monkeypatch.setenv("FAKE_CLI_LOG", str(log))
    exe = make_exe(module_brain_home / "fake-gemini", FAKE)
    monkeypatch.setattr(flags, "enabled", lambda n: n == "ai_providers")
    res = agents.save_config({
        "connectors": [{"kind": "claude", "default_model": "claude-sonnet-5-5"}, {"kind": "gemini", "bin": str(exe)}],
        "delegation": {"enabled": True, "targets": ["gemini"], "max_per_turn": 2,
                       "rules": "long web research goes to Gemini"}})
    assert res["ok"]
    con = agents.db()
    r = agents.send(con, "hi", connector_id="claude", start=False)
    assert r["ok"], r
    return con, r["reply_id"], log


def test_it_runs_the_task_on_the_target_and_stores_a_row(setup):
    con, rid, log = setup
    res = delegation.delegate("gemini", "summarise the Paris metro map", rid)
    assert res == {"ok": True, "connector": "gemini", "text": "delegated answer"}
    assert "summarise the Paris metro map" in log.read_text(encoding="utf-8")
    row = con.execute("SELECT * FROM agent_msg WHERE parent_msg_id=?", (rid,)).fetchone()
    assert row["role"] == "subagent" and row["status"] == "done" and row["full_tools"] == 0
    assert row["label"].startswith("delegated")


def test_the_delegate_sees_only_the_brief(setup):
    _, rid, log = setup
    delegation.delegate("gemini", "just this", rid)
    seen = log.read_text(encoding="utf-8")
    assert "just this" in seen and "second brain" not in seen and "match-rules" not in seen


def test_off_by_default_and_needs_the_flag(setup, monkeypatch):
    con, rid, _ = setup
    agents.save_config({"delegation": {"enabled": False}})
    assert "off" in delegation.delegate("gemini", "x", rid)["error"]
    agents.save_config({"delegation": {"enabled": True, "targets": ["gemini"]}})
    monkeypatch.setattr(flags, "enabled", lambda n: False)
    assert "ai_providers" in delegation.delegate("gemini", "x", rid)["error"]


def test_a_target_not_allowed_is_refused(setup):
    _, rid, _ = setup
    agents.save_config({"delegation": {"enabled": True, "targets": []}})
    assert "not allowed" in delegation.delegate("gemini", "x", rid)["error"]
    assert "no enabled connector" in delegation.delegate("nope", "x", rid)["error"]


def test_no_delegating_to_your_own_provider(setup):
    _, rid, _ = setup
    agents.save_config({"delegation": {"enabled": True, "targets": ["claude", "gemini"]}})
    assert "your own" in delegation.delegate("claude", "x", rid)["error"]


def test_depth_is_one(setup):
    con, rid, _ = setup
    delegation.delegate("gemini", "first", rid)
    sub = con.execute("SELECT id FROM agent_msg WHERE parent_msg_id=?", (rid,)).fetchone()["id"]
    assert "depth 1" in delegation.delegate("gemini", "again", sub)["error"]


def test_the_cap_per_turn(setup):
    _, rid, _ = setup
    assert delegation.delegate("gemini", "one", rid)["ok"]
    assert delegation.delegate("gemini", "two", rid)["ok"]
    assert "at most 2" in delegation.delegate("gemini", "three", rid)["error"]


def test_a_brief_naming_a_personal_node_is_blocked(setup):
    _, rid, log = setup
    con = graph.connect()
    node = graph.api_create_node(con, "note", "Private thing", "x", realm="personal")
    nid = node.get("id") or node["node"]["id"]
    res = delegation.delegate("gemini", f"look at {nid} and summarise", rid)
    assert not res["ok"] and "blocked realm" in res["error"]
    assert not log.exists() or nid not in log.read_text(encoding="utf-8")


def test_the_rules_reach_the_system_prompt_only_when_delegation_is_active(setup, monkeypatch):
    cfg = agents.load_config()
    text = delegation.prompt_section(cfg, "CMD", "claude")
    assert "CMD <connector>" in text and "gemini" in text and "long web research" in text
    assert delegation.prompt_section(cfg, "CMD", "gemini") == ""      # no target besides itself
    monkeypatch.setattr(flags, "enabled", lambda n: False)
    assert delegation.prompt_section(cfg, "CMD", "claude") == ""


def test_the_block_is_cleaned(module_brain_home):
    d = delegation.clean({"enabled": "yes", "targets": ["a", " "], "max_per_turn": 99,
                          "timeout_s": "x", "blocked_realms": "no"})
    assert d["enabled"] is False and d["targets"] == ["a"] and d["max_per_turn"] == 10
    assert d["timeout_s"] == 600 and d["blocked_realms"] == ["personal"]
    json.dumps(d)
