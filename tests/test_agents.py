"""Agents (docs/agents.md): conversations, their stateless turns, and the two
connectors, against a THROWAWAY brain.

    python -m pytest tests/test_agents.py

`claude` is replaced by a script that records its argv and prints a canned
stream-json run (a Grep, a Skill, a reply ending in the question marker), and
Ollama by a local HTTP server that answers with one tool call and then text.
Nothing leaves the machine and no model is called.

One brain and one conversation for the module: the classes follow the life of
that conversation and run in file order.
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from types import SimpleNamespace

import pytest

import agents
import documents
import graph
from _fakes import make_exe
from conftest import LocalServer

UTF8 = "utf-8"
MODE_VAR = "FAKE_CLAUDE_MODE"
CLAUDE_MODEL = "claude-sonnet-5-5"
ARGV_LOG = "claude-argv.jsonl"
FIRST_QUESTION = "What must I renew?"
REPLY_TEXT = "The permit is due Oct 5.\nShall I add a task?"
CLAIMED_COST = 0.0421
WAIT_S = 20
RECOVER_STATUS_WAIT_S = 10

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
system = ""
if "--append-system-prompt-file" in argv:
    with open(argv[argv.index("--append-system-prompt-file") + 1], encoding="utf-8") as f:
        system = f.read()
with open(os.path.join(os.environ["SECOND_BRAIN_HOME"], "claude-argv.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": argv, "stdin": sys.stdin.read(), "system": system}) + "\n")
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
if mode == "crash":
    sys.stderr.write("boom: no credentials\n"); sys.exit(2)
if mode == "slow":
    import time; time.sleep(60)
def p(o): print(json.dumps(o), flush=True)
p({"type": "system", "subtype": "init", "model": "claude-sonnet-5-5", "session_id": "s"})
p({"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 1000, "output_tokens": 20},
   "content": [{"type": "thinking", "thinking": "look it up"},
               {"type": "tool_use", "id": "t1", "name": "Grep", "input": {"pattern": "permit"}},
               {"type": "tool_use", "id": "t2", "name": "Skill", "input": {"skill": "recall"}}]}})
p({"type": "user", "message": {"content": [
   {"type": "tool_result", "tool_use_id": "t1", "content": "1 match"},
   {"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "denied"}], "is_error": True}]}})
p({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.0421,
   "usage": {"input_tokens": 1200, "cache_read_input_tokens": 300, "output_tokens": 80},
   "result": "The permit is due Oct 5.\nShall I add a task?\n[[needs-answer]]"})
'''


class FakeOllama(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/tags":
            return self._json({"models": [{"name": "tiny:1b"}, {"name": "notools:1b"}]})
        self._json({"error": "nope"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        FakeOllama.calls.append(body)
        if body.get("model") == "notools:1b" and body.get("tools"):
            return self._json({"error": "registry.ollama.ai/notools does not support tools"}, 400)
        if body.get("tools") and not any(m["role"] == "tool" for m in body["messages"]):
            tool = "add_task" if "message:\nmake a task" in body["messages"][-1]["content"] else "recall"
            args = {"title": "Renew permit"} if tool == "add_task" else {"query": "permit"}
            return self._json({"message": {"role": "assistant", "content": "",
                                           "tool_calls": [{"function": {"name": tool, "arguments": args}}]},
                               "prompt_eval_count": 50, "eval_count": 5})
        return self._json({"message": {"role": "assistant", "content": "<think>hm</think>Done."},
                           "prompt_eval_count": 70, "eval_count": 9})


# ------------------------------------------------------------------- helpers
def wait_status(con, rid, timeout=WAIT_S):
    end = time.time() + timeout
    while time.time() < end:
        r = con.execute("SELECT status FROM agent_msg WHERE id=?", (rid,)).fetchone()
        if r and r["status"] not in agents.PENDING:
            return r["status"]
        time.sleep(0.1)
    return "timeout"


def wait_until(predicate, timeout=RECOVER_STATUS_WAIT_S, step=0.05) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def reply_of(con, rid):
    return agents._msg_row(con.execute("SELECT * FROM agent_msg WHERE id=?", (rid,)).fetchone())


def last_call(home: Path) -> dict:
    return json.loads((home / ARGV_LOG).read_text(encoding=UTF8).splitlines()[-1])


@contextmanager
def claude_mode(mode: str) -> Iterator[None]:
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(MODE_VAR, mode)
        yield


# ------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def ctx(module_brain_home: Path) -> Iterator[SimpleNamespace]:
    """The brain, its fake claude and fake Ollama, and the connectors configured."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("MEMORY_LEDGER_OWNER", "Owner")
        mp.delenv(MODE_VAR, raising=False)
        bin_path = make_exe(module_brain_home / "fake-claude", FAKE_CLAUDE)
        srv = LocalServer(FakeOllama)
        FakeOllama.calls.clear()
        try:
            graph.api_create_node(graph.connect(), "task", "Renew the parking permit", "", {}, "open", "2026-10-05")
            yield SimpleNamespace(home=module_brain_home, bin=bin_path, url=srv.url, con=agents.db())
        finally:
            srv.stop()


# ------------------------------------------------------------------- config
class TestConfig:
    @pytest.fixture(scope="class")
    def saved(self, ctx) -> SimpleNamespace:
        res = agents.save_config({"connectors": [
            {"kind": "claude", "name": "Claude", "bin": str(ctx.bin), "default_model": CLAUDE_MODEL},
            {"kind": "ollama", "name": "Local", "url": ctx.url},
            {"kind": "ollama", "name": "Local"},
        ]})
        ids = [c["id"] for c in res["config"]["connectors"]]
        refused_kind = agents.save_config({"connectors": [{"kind": "chatgpt"}]})
        refused_url = agents.save_config({"connectors": [{"kind": "ollama", "url": "file:///etc"}]})
        # the config the rest of the suite runs on: the claude and the first ollama
        agents.save_config({"connectors": res["config"]["connectors"][:2]})
        payload = {c["id"]: c for c in agents.connectors_payload()}
        return SimpleNamespace(ids=ids, refused_kind=refused_kind, refused_url=refused_url, payload=payload)

    def test_connector_ids_are_unique(self, saved):
        assert saved.ids == ["claude", "local", "local-2"]

    def test_an_unknown_kind_is_refused(self, saved):
        assert not saved.refused_kind["ok"]

    def test_an_ollama_url_must_be_http(self, saved):
        assert not saved.refused_url["ok"]

    def test_an_ollama_connector_lists_its_hosts_models(self, saved):
        assert saved.payload["local"]["models"] == ["notools:1b", "tiny:1b"]

    def test_and_defaults_to_one_of_them(self, saved):
        assert saved.payload["local"]["default_model"] == "notools:1b"


# -------------------------------------------------------------- attachments
class TestAttachments:
    @pytest.fixture(scope="class")
    def sent(self, ctx) -> SimpleNamespace:
        con = ctx.con
        saved = documents.save_attachment("shot.png", base64.b64encode(b"\x89PNG").decode())
        bad = agents.send(con, "see", attachments=["../etc/passwd"], start=False)
        r0 = agents.send(con, "", attachments=[saved["path"]], start=False)
        turn = agents.build_turn(con, dict(con.execute("SELECT * FROM agent_msg WHERE id=?",
                                                       (r0["reply_id"],)).fetchone()))
        return SimpleNamespace(saved=saved, bad=bad, r0=r0, turn=turn)

    def test_an_attachment_outside_the_attachments_folder_is_refused(self, sent):
        assert not sent.bad["ok"]

    def test_attachments_alone_make_a_message_titled_by_the_file(self, sent):
        assert sent.r0["ok"] and sent.r0["conversation"]["title"] == "shot.png"

    def test_the_agent_is_told_where_the_file_is_on_disk(self, sent):
        assert str(documents.root("attachments") / sent.saved["path"]) in sent.turn["prompt"]


# ---------------------------------------------------------- claude, read-only
@pytest.fixture(scope="module")
def first(ctx) -> SimpleNamespace:
    """The read-only exchange every later stage builds on: the first message, its
    reply, and what `claude` was called with."""
    con = ctx.con
    r = agents.send(con, FIRST_QUESTION, start=False)
    out = agents.run_reply(r["reply_id"])
    call = last_call(ctx.home)
    allowed = next((a for a in call["argv"] if a.startswith("--allowedTools=")), "=").split("=", 1)[1].split(",")
    ctx.cid = r["conversation"]["id"]
    return SimpleNamespace(r=r, out=out, m=reply_of(con, r["reply_id"]), call=call,
                           argv=call["argv"], prompt=call["stdin"], system=call["system"],
                           allowed=allowed, cid=ctx.cid)


class TestClaudeReadOnly:
    def test_a_first_message_makes_the_conversation_titled_from_it(self, first):
        assert first.r["ok"] and first.r["conversation"]["title"] == FIRST_QUESTION

    def test_it_starts_read_only(self, first):
        assert first.r["conversation"]["full_tools"] is False

    def test_the_reply_runs(self, first):
        assert first.out == {"ok": True, "status": "question"}

    def test_the_question_marker_is_gone_from_the_text(self, first):
        assert first.m["text"] == REPLY_TEXT

    def test_tools_skills_and_thinking_are_logged(self, first):
        kinds = [(e["kind"], e["name"]) for e in first.m["events"]]
        assert kinds == [("thinking", "thinking"), ("tool", "Grep"), ("skill", "recall")]

    def test_a_tools_result_lands_on_its_call(self, first):
        assert first.m["events"][1]["output"] == "1 match"
        assert first.m["events"][2]["error"]

    def test_claudes_own_cost_wins_over_the_estimate(self, first):
        assert first.m["cost_usd"] == CLAIMED_COST and not first.m["cost_estimated"]

    def test_tokens_come_from_the_final_usage(self, first):
        assert (first.m["tokens_in"], first.m["tokens_out"]) == (1500, 80)

    def test_the_recalled_task_was_in_the_turns_context(self, first):
        assert any(c.startswith("task_renew-the-parking-permit") for c in first.m["context"]), first.m["context"]

    def test_read_only_cuts_the_tool_set_and_denies_the_rest(self, first):
        assert "--tools=Read,Grep,Glob,WebSearch,WebFetch,Skill,Bash" in first.argv
        assert "dontAsk" in first.argv
        assert "--dangerously-skip-permissions" not in first.argv

    def test_read_only_bash_is_the_brains_own_commands_only(self, first):
        assert [a for a in first.allowed if a.startswith("Bash")] == [f"Bash({c}:*)" for c in agents.BRAIN_CMDS]

    def test_read_only_connectors_read_and_never_write(self, first):
        writes = ("send", "create", "edit", "update", "delete", "trash", "executeWrite",
                  "executeDestructive", "comment")
        assert "mcp__atlassian__getJiraIssue" in first.allowed
        assert "mcp__claude_ai_Slack__slack_read_thread" in first.allowed
        assert not [a for a in first.allowed if any(w in a for w in writes)]

    def test_neither_prompt_is_on_the_command_line(self, first):
        argv = first.argv
        assert "--append-system-prompt-file" in argv and "-p" in argv
        assert argv[argv.index("-p") + 1].startswith("--")
        assert not any(FIRST_QUESTION in a for a in argv)

    def test_the_system_prompt_names_the_conversation_and_how_to_read_it(self, first):
        cid = first.r["conversation"]["id"]
        assert cid in first.system and f"{agents.THREAD_CMD} {cid}" in first.system

    def test_the_system_prompt_gives_the_brains_commands_to_read_and_write_without_asking(self, first):
        s = first.system
        assert f"{agents.GRAPH_CMD} match-rules" in s and f"{agents.GRAPH_CMD} add-node" in s
        assert "without asking" in s and "READ-ONLY" in s

    def test_the_prompt_carries_the_message_and_the_recalled_node(self, first):
        assert FIRST_QUESTION in first.prompt and "Renew the parking permit" in first.prompt

    def test_the_conversation_badge_is_a_question(self, ctx, first):
        assert agents.conv_state(ctx.con, first.r["conversation"]["id"]) == "question"

    def test_the_exchange_is_written_to_the_graph_under_the_conversations_node(self, ctx, first):
        con = ctx.con
        node = con.execute("SELECT * FROM nodes WHERE id=?", (first.m["node_id"],)).fetchone()
        assert node is not None and "Oct 5" in node["body"]
        conv = agents.get_conv(con, first.r["conversation"]["id"])
        edge = con.execute("SELECT 1 FROM edges WHERE src=? AND dst=? AND rel='part-of'",
                           (first.m["node_id"], conv["node_id"])).fetchone()
        assert edge is not None


# ------------------------------------------------------------- the context window
class TestContextWindow:
    @pytest.fixture(scope="class")
    def turns(self, ctx, first) -> SimpleNamespace:
        con, cid = ctx.con, ctx.cid
        r2 = agents.send(con, "yes", cid, full_tools=True, start=False)
        agents.run_reply(r2["reply_id"])
        with_history = last_call(ctx.home)
        agents.save_conversation(con, cid, context_turns=0)
        r3 = agents.send(con, "and?", cid, start=False)
        agents.run_reply(r3["reply_id"])
        return SimpleNamespace(with_history=with_history, without=last_call(ctx.home))

    def test_one_earlier_exchange_is_carried_by_default(self, turns):
        prompt = turns.with_history["stdin"]
        assert "Earlier in this conversation" in prompt and FIRST_QUESTION in prompt

    def test_tools_on_runs_without_prompts(self, turns):
        argv = turns.with_history["argv"]
        assert "--dangerously-skip-permissions" in argv
        assert "--tools=" not in " ".join(argv)

    def test_zero_carries_none(self, turns):
        assert "Earlier in this conversation" not in turns.without["stdin"]

    def test_the_window_is_clamped(self, ctx, turns):
        res = agents.save_conversation(ctx.con, ctx.cid, context_turns=99)
        assert res["conversation"]["context_turns"] == 10


# ------------------------------------------------------------------- failures
class TestFailures:
    @pytest.fixture(scope="class")
    def crashed(self, ctx, first) -> SimpleNamespace:
        con = ctx.con
        r4 = agents.send(con, "again", ctx.cid, start=False)
        with claude_mode("crash"):
            agents.run_reply(r4["reply_id"])
        return SimpleNamespace(r4=r4, m=reply_of(con, r4["reply_id"]))

    def test_a_crash_is_an_error_with_claudes_reason(self, crashed):
        assert crashed.m["status"] == "error" and "no credentials" in crashed.m["error"], crashed.m["error"]

    def test_the_badge_says_so(self, ctx, crashed):
        assert agents.conv_state(ctx.con, ctx.cid) == "error"

    def test_retry_queues_it_again(self, ctx, crashed):
        assert agents.retry(ctx.con, crashed.r4["reply_id"], start=False)["ok"]
        assert reply_of(ctx.con, crashed.r4["reply_id"])["status"] == "queued"

    def test_retry_keeps_the_failed_trace_and_marks_it(self, ctx, crashed):
        # (runs after the retry above: the reply is queued again with its old log in place)
        ev = reply_of(ctx.con, crashed.r4["reply_id"])["events"]
        assert ev and ev[-1]["name"] == "attempt failed" and ev[-1]["error"]

    def test_a_done_reply_cannot_be_retried(self, ctx, first):
        assert not agents.retry(ctx.con, first.r["reply_id"], start=False)["ok"]


# -------------------------------------------- switching mid-conversation, and ollama
class TestOllama:
    @pytest.fixture(scope="class")
    def switched(self, ctx, first) -> SimpleNamespace:
        con, cid = ctx.con, ctx.cid
        FakeOllama.calls.clear()
        res = agents.save_conversation(con, cid, connector="local", model="tiny:1b")
        unknown = agents.save_conversation(con, cid, connector="nope")
        r5 = agents.send(con, "what is due?", cid, full_tools=False, start=False)
        out = agents.run_reply(r5["reply_id"])
        first_call = FakeOllama.calls[0]
        return SimpleNamespace(res=res, unknown=unknown, out=out, m=reply_of(con, r5["reply_id"]),
                               first_call=first_call)

    def test_connector_and_model_switch(self, switched):
        assert switched.res["ok"] and switched.res["conversation"]["connector"] == "local"

    def test_an_unknown_connector_is_refused(self, switched):
        assert not switched.unknown["ok"]

    def test_ollama_answers_after_its_tool_call(self, switched):
        assert switched.out["ok"] and switched.m["text"] == "Done.", switched.m["text"]

    def test_the_tool_call_is_logged_with_its_result(self, switched):
        ev = switched.m["events"][0]
        assert ev["name"] == "recall" and "parking permit" in ev["output"].lower()

    def test_read_only_ollama_still_reads_and_writes_the_brain(self, switched):
        names = {t["function"]["name"] for t in switched.first_call["tools"]}
        assert names == {"recall", "search", "get_node", "read_thread", "add_note", "add_task"}

    def test_ollama_is_told_to_read_the_thread_with_its_tool(self, switched):
        assert "read_thread" in switched.first_call["messages"][0]["content"]

    def test_read_thread_reads_this_conversation_back(self, ctx, switched):
        assert FIRST_QUESTION in agents.ollama_tool_call("read_thread", {}, ctx.cid)

    def test_read_thread_keeps_the_last_n(self, ctx, switched):
        last2 = agents.ollama_tool_call("read_thread", {"limit": 2}, ctx.cid)
        assert "(2 messages)" in last2 and "Oct 5" not in last2, last2

    def test_a_local_model_costs_nothing_and_tokens_are_counted(self, switched):
        assert switched.m["cost_usd"] == 0 and switched.m["tokens_in"] == 120

    def test_each_reply_records_who_wrote_it(self, ctx, switched, first):
        assert switched.m["model"] == "tiny:1b"
        assert reply_of(ctx.con, first.r["reply_id"])["model"] == CLAUDE_MODEL

    def test_a_brain_write_works_while_read_only(self, switched):
        assert agents.ollama_tool_call("add_task", {"title": "x"})["ok"]

    def test_tools_on_ollama_writes_too(self, ctx, switched):
        FakeOllama.calls.clear()
        r6 = agents.send(ctx.con, "make a task", ctx.cid, full_tools=True, start=False)
        agents.run_reply(r6["reply_id"])
        found = graph.connect().execute(
            "SELECT 1 FROM nodes WHERE title='Renew permit' AND type='task'").fetchone()
        assert found is not None

    def test_a_model_without_tools_answers_without_them(self, ctx, switched):
        agents.save_conversation(ctx.con, ctx.cid, model="notools:1b")
        r7 = agents.send(ctx.con, "plain?", ctx.cid, start=False)
        out = agents.run_reply(r7["reply_id"])
        m = reply_of(ctx.con, r7["reply_id"])
        assert out["ok"] and m["events"][0]["name"] == "no tools", out


# --------------------------------------------------------------- list, hide, threads
class TestListHideThreads:
    @pytest.fixture(scope="class")
    def hidden(self, ctx, first):
        agents.save_conversation(ctx.con, ctx.cid, hidden=True)

    def test_a_hidden_conversation_is_still_listed_flagged(self, ctx, hidden):
        assert agents.conversations(ctx.con)[0]["hidden"] is True

    def test_the_thread_sums_its_cost(self, ctx, hidden):
        t = agents.thread(ctx.con, ctx.cid)
        assert round(t["conversation"]["cost_usd"], 4) == round(CLAIMED_COST * 3, 4), t["conversation"]["cost_usd"]

    def test_messages_alternate_the_reply_after_its_question(self, ctx, hidden):
        t = agents.thread(ctx.con, ctx.cid)
        assert [x["role"] for x in t["messages"][:2]] == ["user", "agent"]

    def test_the_thread_command_prints_the_thread(self, ctx, hidden, run_script):
        out = run_script(agents.SCRIPT, "thread", ctx.cid, home=ctx.home, env={"MEMORY_LEDGER_OWNER": "Owner"})
        assert out.returncode == 0, out.stdout + out.stderr
        assert "Owner:" in out.stdout and "Oct 5" in out.stdout
        assert agents.QUESTION_MARK not in out.stdout


# ------------------------------------------------ background dispatch and recovery
class TestBackgroundAndRecovery:
    @pytest.fixture(scope="class")
    def r8(self, ctx, first) -> dict:
        agents.save_conversation(ctx.con, ctx.cid, connector="claude", model=CLAUDE_MODEL)
        return agents.send(ctx.con, "async", ctx.cid)

    def _set(self, ctx, rid, **cols) -> None:
        sets = ", ".join(f"{k}=?" for k in cols)
        ctx.con.execute(f"UPDATE agent_msg SET {sets} WHERE id=?", (*cols.values(), rid))
        ctx.con.commit()

    def test_a_sent_message_runs_in_the_background(self, ctx, r8):
        assert wait_status(ctx.con, r8["reply_id"]) == "question"

    def test_a_reply_left_running_fails_on_restart(self, ctx, r8):
        self._set(ctx, r8["reply_id"], status="running", beat_at=0)
        assert agents.recover() == 1
        assert "interrupted" in reply_of(ctx.con, r8["reply_id"])["error"]

    def test_a_reply_whose_server_is_still_alive_survives_another_servers_start(self, ctx, r8):
        self._set(ctx, r8["reply_id"], status="running", owner_pid=os.getppid(), beat_at=0)
        assert agents.recover() == 0
        assert reply_of(ctx.con, r8["reply_id"])["status"] == "running"

    def test_a_reply_with_a_fresh_heartbeat_survives_even_if_its_pid_is_unseen(self, ctx, r8):
        self._set(ctx, r8["reply_id"], owner_pid=0, beat_at=time.time())
        assert agents.recover() == 0
        assert reply_of(ctx.con, r8["reply_id"])["status"] == "running"
        self._set(ctx, r8["reply_id"], status="error")

    def test_the_next_action_line_is_split_off_the_reply(self):
        assert agents.strip_next("Done.\n[[next: Run the tests]]") == ("Done.", "Run the tests")
        assert agents.strip_next("Plain") == ("Plain", "")

    def test_an_empty_message_is_refused(self, ctx, r8):
        assert not agents.send(ctx.con, "  ", ctx.cid, start=False)["ok"]


# ---------------------------------------------------------------------- cancel
class TestCancel:
    @pytest.fixture(scope="class")
    def queued(self, ctx, first) -> SimpleNamespace:
        r9 = agents.send(ctx.con, "queued only", ctx.cid, start=False)
        res = agents.cancel(ctx.con, r9["reply_id"])
        return SimpleNamespace(r9=r9, res=res, m=reply_of(ctx.con, r9["reply_id"]))

    def test_a_queued_reply_cancels_at_once_as_a_failure_retry_can_run(self, queued):
        assert queued.res["ok"] and queued.res["was"] == "queued", queued.res
        assert queued.m["status"] == "error" and queued.m["error"] == agents.CANCELLED

    def test_a_reply_that_ended_cannot_be_cancelled(self, ctx, queued):
        assert not agents.cancel(ctx.con, queued.r9["reply_id"])["ok"]

    def test_and_it_never_starts(self, ctx, queued):
        assert not agents.run_reply(queued.r9["reply_id"])["ok"]
        assert reply_of(ctx.con, queued.r9["reply_id"])["error"] == agents.CANCELLED

    def test_a_running_reply_cancels_its_session_killed_and_retry_runs_it_again(self, ctx, queued):
        con = ctx.con
        with claude_mode("slow"):
            r10 = agents.send(con, "take your time", ctx.cid)
            assert wait_until(lambda: r10["reply_id"] in agents._procs)
            t0 = time.time()
            res = agents.cancel(con, r10["reply_id"])
            assert res["ok"] and res["was"] == "running", res
            assert wait_status(con, r10["reply_id"]) == "error"
            assert time.time() - t0 < 5, res
            wait_until(lambda: not agents.running_count())
        # it stays cancelled after its thread ends
        m = reply_of(con, r10["reply_id"])
        assert m["error"] == agents.CANCELLED, m["error"]
        assert not agents._cancelled and not agents._procs
        # and Retry runs it again
        with claude_mode("ok"):
            agents.retry(con, r10["reply_id"])
            assert wait_status(con, r10["reply_id"]) == "question"
