"""Agent chats mirrored to the master, behind AGENTS_SYNC=1.

    python -m pytest tests/test_agents_sync.py

No server: only the local side (triggers, payloads, pull writes, claim) is
exercised, against a throwaway brain.
"""

from __future__ import annotations

import json
import time

import pytest

import agents
import graph

SEP = "\x1f"
TS = "2026-10-20T10:00:00.000000Z"


@pytest.fixture
def con(brain_home, monkeypatch):
    monkeypatch.setenv("AGENTS_SYNC", "1")
    c = agents.db()
    c.execute("INSERT INTO agent_conv(id,title,created_at,updated_at,draft,draft_at)"
              " VALUES('c1','t',?,?,'unsent',?)", (TS, TS, TS))
    c.execute("INSERT INTO agent_msg(id,conv_id,role,text,at) VALUES('q1','c1','user','hi',?)", (TS,))
    c.execute("INSERT INTO agent_msg(id,conv_id,role,text,at,reply_to,status,owner_pid,beat_at)"
              " VALUES('a1','c1','agent','',?,'q1','running',4242,?)", (TS, time.time()))
    c.commit()
    return c


def queued(c):
    return {r["ref"] for r in c.execute("SELECT ref FROM outbox WHERE kind='row'")}


class TestFlag:
    def test_off_by_default_nothing_is_mirrored(self, brain_home, monkeypatch):
        monkeypatch.delenv("AGENTS_SYNC", raising=False)
        c = agents.db()
        c.execute("INSERT INTO agent_msg(id,conv_id,role,text,at) VALUES('q','c','user','x',?)", (TS,))
        c.commit()
        assert "agent_msg" not in graph.mirror_tables()
        assert not [r for r in queued(c) if r.startswith("agent_")]

    def test_on_the_chat_tables_are_mirrored_and_queued(self, con):
        assert {"agent_conv", "agent_msg"} <= set(graph.mirror_tables())
        assert {f'agent_msg{SEP}["q1"]', f'agent_msg{SEP}["a1"]', f'agent_conv{SEP}["c1"]'} <= queued(con)


class TestPayload:
    def test_the_draft_never_leaves(self, con):
        d = json.loads(graph._row_payload(con, "agent_conv", '["c1"]')["data"])
        assert "draft" not in d and "draft_at" not in d and d["title"] == "t"

    def test_the_pid_never_leaves_but_the_heartbeat_and_reply_to_do(self, con):
        d = json.loads(graph._row_payload(con, "agent_msg", '["a1"]')["data"])
        assert "owner_pid" not in d and d["reply_to"] == "q1" and d["beat_at"] > 0
        assert d["status"] == "running"


class TestPull:
    def test_a_pulled_conv_keeps_the_local_draft(self, con):
        graph.put_row(con, "agent_conv", {"id": "c1", "title": "renamed", "created_at": TS,
                                          "updated_at": TS})
        r = con.execute("SELECT title, draft FROM agent_conv WHERE id='c1'").fetchone()
        assert (r["title"], r["draft"]) == ("renamed", "unsent")

    def test_a_new_message_from_the_other_machine_is_added(self, con):
        assert graph.put_row(con, "agent_msg", {"id": "q2", "conv_id": "c1", "role": "user",
                                                "text": "from b", "at": TS, "status": "done"})
        assert con.execute("SELECT text FROM agent_msg WHERE id='q2'").fetchone()[0] == "from b"

    def test_a_finished_reply_is_not_rolled_back_to_running(self, con):
        con.execute("UPDATE agent_msg SET status='done' WHERE id='a1'")
        assert not graph.put_row(con, "agent_msg", {"id": "a1", "conv_id": "c1", "role": "agent",
                                                    "at": TS, "status": "running"})
        assert con.execute("SELECT status FROM agent_msg WHERE id='a1'").fetchone()[0] == "done"

    def test_a_reply_running_here_is_not_overwritten_by_a_stale_copy(self, con):
        assert not graph.put_row(con, "agent_msg", {"id": "a1", "conv_id": "c1", "role": "agent",
                                                    "at": TS, "status": "queued"})


class TestProcessingFlag:
    def row(self, con, i):
        return con.execute("SELECT * FROM agent_msg WHERE id=?", (i,)).fetchone()

    def test_a_fresh_heartbeat_is_processing_even_with_a_foreign_pid(self, con):
        assert agents.is_processing(self.row(con, "a1"))

    def test_a_stale_heartbeat_is_not(self, con):
        con.execute("UPDATE agent_msg SET beat_at=? WHERE id='a1'", (time.time() - 120,))
        assert not agents.is_processing(self.row(con, "a1"))

    def test_a_finished_reply_is_not(self, con):
        con.execute("UPDATE agent_msg SET status='done' WHERE id='a1'")
        assert not agents.is_processing(self.row(con, "a1"))


class TestClaim:
    def claim(self, con, mid, monkeypatch):
        monkeypatch.setattr(agents, "_run_claimed", lambda *a: {"ok": True})
        return agents.run_reply(mid)

    def test_another_machines_live_reply_is_not_claimed(self, con, monkeypatch):
        r = self.claim(con, "a1", monkeypatch)
        assert r["ok"] is False
        assert con.execute("SELECT owner_pid FROM agent_msg WHERE id='a1'").fetchone()[0] == 4242

    def test_a_stale_one_is_taken_over(self, con, monkeypatch):
        con.execute("UPDATE agent_msg SET beat_at=? WHERE id='a1'", (time.time() - 120,))
        con.commit()
        assert self.claim(con, "a1", monkeypatch)["ok"] is True

    def test_a_queued_one_is_claimed_once(self, con, monkeypatch):
        con.execute("UPDATE agent_msg SET status='queued', beat_at=0 WHERE id='a1'")
        con.commit()
        assert self.claim(con, "a1", monkeypatch)["ok"] is True
        assert self.claim(con, "a1", monkeypatch)["ok"] is False
