"""Sub-agent tracking (flag `subagent_tracking`): each Task call is a row of its own,
under the row that made it, and what it did is in its own log.

    python -m pytest tests/test_subagent_tracking.py
"""

from __future__ import annotations

import json

import pytest

import agents
import flags


@pytest.fixture
def con(brain_home):
    c = agents.db()
    c.execute("INSERT INTO agent_conv(id,created_at,updated_at) VALUES('c1','t','t')")
    c.execute("INSERT INTO agent_msg(id,conv_id,role,at,status,connector,model) "
              "VALUES('r1','c1','agent','2026-01-01','running','claude','m')")
    c.commit()
    return c


def line(**kw) -> str:
    return json.dumps(kw)


def assistant(blocks, parent=None, mid="m1", usage=None):
    return line(type="assistant", parent_tool_use_id=parent,
                message={"id": mid, "content": blocks, "usage": usage or {}})


def task(tid, desc):
    return {"type": "tool_use", "id": tid, "name": "Task",
            "input": {"description": desc, "prompt": f"do {desc}", "subagent_type": "Explore"}}


def result(tid, text, error=False):
    return line(type="user", message={"content": [
        {"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": error}]})


def run(con, lines):
    log = agents._Log(con, "r1")
    for ln in lines:
        agents.handle_stream_line(log, ln)
    log.flush(force=True)
    return log


STREAM = [
    assistant([task("t1", "find it")]),
    assistant([{"type": "text", "text": "looking"}], parent="t1", mid="m2",
              usage={"input_tokens": 10, "output_tokens": 5}),
    assistant([task("t2", "dig deeper")], parent="t1", mid="m3"),
    assistant([{"type": "text", "text": "deep"}], parent="t2", mid="m4"),
    result("t2", "found deep"),
    result("t1", "found it"),
]


def test_off_by_default_subagents_stay_events_of_the_reply(con):
    run(con, STREAM)
    assert con.execute("SELECT COUNT(*) FROM agent_msg WHERE role='subagent'").fetchone()[0] == 0
    assert "subagents" not in agents.thread(con, "c1")


def test_on_each_subagent_is_a_row_under_its_parent(con):
    flags.set_flag("subagent_tracking", True)
    log = run(con, STREAM)
    rows = {r["tool_use_id"]: r for r in con.execute(
        "SELECT * FROM agent_msg WHERE role='subagent'")}
    assert set(rows) == {"t1", "t2"}
    assert rows["t1"]["parent_msg_id"] == "r1" and rows["t1"]["reply_to"] == "r1"
    assert rows["t2"]["parent_msg_id"] == rows["t1"]["id"] and rows["t2"]["reply_to"] == "r1"
    assert rows["t1"]["status"] == rows["t2"]["status"] == "done"
    assert rows["t1"]["text"] == "found it" and rows["t1"]["prompt"] == "do find it"
    assert rows["t1"]["tokens_out"] == 5
    # the reply's log keeps the call, not what the subagent did
    assert [e["kind"] for e in log.events] == ["agent"]
    assert [e["output"] for e in json.loads(rows["t1"]["events"]) if e["kind"] == "text"] == ["looking"]


def test_the_thread_lists_them_apart_from_its_messages(con):
    flags.set_flag("subagent_tracking", True)
    run(con, STREAM)
    t = agents.thread(con, "c1")
    assert [m["id"] for m in t["messages"]] == ["r1"]
    assert len(t["subagents"]) == 2


def test_an_error_result_marks_the_subagent_and_a_cancel_closes_the_open_ones(con):
    flags.set_flag("subagent_tracking", True)
    run(con, [assistant([task("t1", "a")]), result("t1", "boom", error=True),
              assistant([task("t3", "b")], mid="m9")])
    st = {r["tool_use_id"]: r["status"] for r in con.execute(
        "SELECT * FROM agent_msg WHERE role='subagent'")}
    assert st == {"t1": "error", "t3": "running"}
    agents.cancel(con, "r1")
    assert con.execute("SELECT status FROM agent_msg WHERE tool_use_id='t3'").fetchone()[0] == "error"
