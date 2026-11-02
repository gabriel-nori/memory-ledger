"""The trigger engine (docs/trigger.md): gate, audit log, loops, sources, dispatch.

    python -m pytest tests/test_trigger.py

Stdlib and pytest only. It runs `scripts/trigger.py` in process against a
THROWAWAY brain per test (the `brain_home` fixture of conftest), with Slack and
Jira replaced by a fake transport (`trigger.http_json` is the only function that
talks HTTP) and `claude` replaced by a script that prints what a headless
session would. Nothing leaves the machine.
"""

from __future__ import annotations

import copy
import json
import logging
import sys
import threading
import time
import urllib.request
from http.client import IncompleteRead
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

import trigger  # noqa: E402 - conftest has put scripts/ on sys.path

UTF8 = "utf-8"
REAL_HTTP_JSON = trigger.http_json

BOT = "UBOT"
TAG = f"<@{BOT}>"
OWNER_ID = "UOWNER"
OTHER_ID = "UOTHER"
CHANNEL = "C1"
OWNER_SIGNATURE = "_sent by Owner's second brain_"
SIGNATURE_TEXT = "sent by Owner's second brain"
NOT_HERE = "Not here."
SLACK_PERMALINK = "https://x.slack.com/p"
JIRA_BASE = "https://j.example"
JIRA_OWNER = "acc-owner"
JIRA_MARKER = "@cérebro"
GITHUB_REPO_URL = "https://github.com/o/r"
SECRET_JIRA_TOKEN = "sekrit-9f2"
CLAUDE_CALLS_LOG = "claude-calls.jsonl"

FAKE_CLAUDE_SOURCE = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
log = os.path.join(os.environ["SECOND_BRAIN_HOME"], "claude-calls.jsonl")
prompt = "" if "--version" in args else sys.stdin.buffer.read().decode("utf-8")
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps({"args": args, "prompt": prompt, "rid": os.environ.get("MEMORY_LEDGER_REQUEST_ID"),
                        "src": os.environ.get("MEMORY_LEDGER_TRIGGER_SOURCE")}) + "\\n")
if "--version" in args:
    print("9.9.9 (fake)"); sys.exit(0)
if prompt.startswith("Rewrite NOTICE"):
    # A translation: mark it, so a test can see it happened and to what.
    notice = prompt.split("<<<NOTICE\\n", 1)[1].split("\\nNOTICE>>>", 1)[0]
    print(json.dumps({"result": "[translated] " + notice})); sys.exit(0)
if prompt.startswith("Post one Slack message"):
    where = "DM" if "FORCE-DM" in prompt else "THREAD"
    print(json.dumps({"result": "posted it\\n" + where})); sys.exit(0)
if "FAIL-ME" in prompt:
    print(json.dumps({"is_error": True, "result": "it broke"})); sys.exit(1)
print(json.dumps({"result": "replied in thread", "total_cost_usd": 0.0123}))
"""

ECHO_SCRIPT = """#!/usr/bin/env python3
import json, os, sys
print(json.dumps({"argv": sys.argv[1:], "user": os.environ.get("BRAIN_TRIGGER_USER"),
                  "command": os.environ.get("BRAIN_COMMAND"),
                  "args": json.loads(os.environ.get("BRAIN_ARGS_JSON") or "[]"),
                  "rid": os.environ.get("MEMORY_LEDGER_REQUEST_ID")}))
"""

SLOW_SCRIPT = "#!/usr/bin/env python3\nimport time\ntime.sleep(60)\nprint('finished')\n"

BASE_CONFIG = {
    "enabled": True,
    # The notice tests below are about translating; the default, refusing with
    # no model call at all, has its own (the access tests).
    "refuse_without_model": False,
    "signature": "_sent by {owner}'s second brain_",
    "claude": {"bin": "", "timeout_s": 60, "mcp_config": "", "extra_args": ["--model", "x"]},
    "slack": {"enabled": True, "owner_user_id": OWNER_ID, "bot_token": "xoxb-t",
              "app_token": "xapp-t", "user_token": "xoxp-t"},
    "jira": {"enabled": True, "base_url": JIRA_BASE, "email": "o@example.com",
             "token": "jt", "owner_account_id": JIRA_OWNER, "marker": JIRA_MARKER},
    "github": {"enabled": True, "token": "ghp_t", "login": "owner", "marker": "/brain",
               "events": {"ci_failed": {"on": True}, "merged": {"on": True},
                          "pushed": {"on": True}}},
}


# ================================================================== helpers
def write_cfg(config: dict) -> dict:
    trigger.config_path().parent.mkdir(parents=True, exist_ok=True)
    trigger.config_path().write_text(json.dumps(config), encoding=UTF8)
    return trigger.load_config()


def read_config_file() -> dict:
    return json.loads(trigger.config_path().read_text(encoding=UTF8))


class FakeHTTP:
    """Answers by (method, url-without-query); records every call."""

    def __init__(self):
        self.calls, self.routes = [], {}

    def on(self, method, url, fn):
        self.routes[(method, url)] = fn

    def __call__(self, method, url, *, headers=None, body=None, form=None, timeout=30):
        self.calls.append({"method": method, "url": url, "body": body, "form": form})
        fn = self.routes.get((method, url.split("?")[0]))
        if fn is None:
            raise trigger.TriggerError(f"no fake for {method} {url}")
        return fn(url=url, body=body, form=form)


def install_http(monkeypatch: pytest.MonkeyPatch) -> FakeHTTP:
    fake = FakeHTTP()
    monkeypatch.setattr(trigger, "http_json", fake)
    trigger._SLACK_BOT_ID.clear()
    s = trigger.SLACK_API
    fake.on("POST", s + "auth.test", lambda **_: {"ok": True, "user_id": BOT})
    fake.on("POST", s + "chat.getPermalink",
            lambda form, **_: {"ok": True, "permalink": f"{SLACK_PERMALINK}{form['message_ts']}"})
    fake.on("POST", s + "users.info",
            lambda form, **_: {"ok": True, "user": {"profile": {"real_name": f"Name {form['user']}"}}})
    fake.on("POST", s + "chat.postMessage", lambda **_: {"ok": True})
    return fake


def calls_to(fake: FakeHTTP, suffix: str) -> list[dict]:
    return [c for c in fake.calls if c["url"].split("?")[0].endswith(suffix)]


def slack_msg(ts, user, text, thread=None):
    m = {"type": "app_mention", "ts": ts, "user": user, "text": text, "channel": CHANNEL}
    if thread:
        m["thread_ts"] = thread
    return m


def mention(cfg, ts, user, text, channel=CHANNEL, thread=None):
    """A Slack event for a message that tags the bot, as the engine builds it."""
    return trigger.slack_event(slack_msg(ts, user, text, thread), channel, cfg, bot_user_id=BOT)


def claude_calls(home: Path) -> list[dict]:
    log = home / CLAUDE_CALLS_LOG
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding=UTF8).splitlines()]


def write_script(name: str, text: str, executable: bool = True) -> None:
    d = trigger.graph.scripts_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text, encoding=UTF8)
    (d / name).chmod(0o755 if executable else 0o644)


def adf(*parts):
    content = []
    for p in parts:
        content.append({"type": "mention", "attrs": {"id": p[1], "text": p[2]}}
                       if isinstance(p, tuple) else {"type": "text", "text": p})
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": content}]}


class Dispatcher:
    """Stands in for the worker pool: collects the request ids it is handed.
    (Not a list: the engine treats a falsy dispatcher as none given.)"""

    def __init__(self):
        self.ran: list[str] = []

    def __call__(self, request_id):
        self.ran.append(request_id)


# ================================================================= fixtures
@pytest.fixture(autouse=True)
def _clean_slack_cache():
    trigger._SLACK_BOT_ID.clear()
    yield
    trigger._SLACK_BOT_ID.clear()


@pytest.fixture
def home(brain_home: Path, owner_name: str) -> Path:
    return brain_home


@pytest.fixture
def base_config(home: Path, fake_exe) -> dict:
    config = copy.deepcopy(BASE_CONFIG)
    config["claude"]["bin"] = str(fake_exe(home / "fake-claude", FAKE_CLAUDE_SOURCE))
    return config


@pytest.fixture
def cfg(base_config: dict) -> dict:
    return write_cfg(base_config)


@pytest.fixture
def http(monkeypatch) -> FakeHTTP:
    return install_http(monkeypatch)


@pytest.fixture
def dispatch() -> Dispatcher:
    return Dispatcher()


@pytest.fixture
def con(home: Path):
    connection = trigger.db()
    yield connection
    connection.close()


# ==================================================================== config
def test_no_config_file_means_disabled_and_says_why(home):
    cfg = trigger.load_config()
    assert not trigger.source_enabled(cfg, "slack")
    assert "does not exist" in cfg["_problem"]


def test_a_file_that_does_not_parse_turns_nothing_on(home):
    trigger.config_path().write_text("{not json", encoding=UTF8)
    cfg = trigger.load_config()
    assert "not valid JSON" in cfg["_problem"]
    assert not trigger.source_enabled(cfg, "jira")


def test_the_file_merges_over_the_defaults(cfg):
    assert cfg["jira"]["poll_seconds"] == 60
    assert cfg["jira"]["marker"] == JIRA_MARKER


def test_an_env_var_outranks_the_file(cfg, monkeypatch):
    monkeypatch.setenv("MEMORY_LEDGER_SLACK_BOT_TOKEN", "xoxb-env")
    assert trigger.secret(cfg, "slack", "bot_token") == "xoxb-env"


def test_the_signature_names_the_owner(cfg):
    assert trigger.signature(cfg) == OWNER_SIGNATURE


def test_the_summary_never_carries_a_token(cfg):
    summary = trigger.config_summary(cfg)
    dumped = json.dumps(summary)
    assert "xoxb-t" not in dumped
    assert 'jt"' not in dumped
    assert summary["secrets"]["slack.bot_token"]["set"] is True


# ============================================================ http transport
class Resp:
    def __init__(self, raw):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        if isinstance(self.raw, Exception):
            raise self.raw
        return self.raw


@pytest.fixture
def urlopen_script(monkeypatch) -> list:
    """What each successive `urlopen` returns or raises."""
    script: list = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=30: Resp(script.pop(0)))
    return script


def test_a_get_cut_off_mid_body_is_read_again_once(urlopen_script):
    urlopen_script[:] = [IncompleteRead(b"[{"), b'[{"accountId": "a"}]']
    assert REAL_HTTP_JSON("GET", "https://x/y") == [{"accountId": "a"}]
    assert not urlopen_script


def test_a_get_cut_off_twice_is_a_trigger_error_never_a_crash(urlopen_script):
    urlopen_script[:] = [IncompleteRead(b""), ConnectionResetError()]
    with pytest.raises(trigger.TriggerError) as err:
        REAL_HTTP_JSON("GET", "https://x/y?q=1")
    assert "connection broke" in str(err.value)
    assert "q=1" not in str(err.value)


def test_a_post_is_never_sent_twice(urlopen_script):
    urlopen_script[:] = [IncompleteRead(b""), b"{}"]
    with pytest.raises(trigger.TriggerError) as err:
        REAL_HTTP_JSON("POST", "https://x/y", body={})
    assert "connection broke" in str(err.value)
    assert len(urlopen_script) == 1


# ====================================================================== gate
def test_a_message_mentioning_the_bot_is_a_mention(cfg, http):
    ev = mention(cfg, "100.1", OTHER_ID, f"{TAG} do the thing")
    assert ev
    assert ev["scope"] == "slack:C1:100.1"


def test_a_mention_resolves_its_permalink_and_requester_name(cfg, http):
    ev = mention(cfg, "100.1", OTHER_ID, f"{TAG} do the thing")
    assert ev["permalink"] == f"{SLACK_PERMALINK}100.1"
    assert ev["requester_name"] == f"Name {OTHER_ID}"


def test_a_message_not_mentioning_the_bot_is_not_a_mention(cfg, http):
    assert mention(cfg, "1.0", "U", "hello") is None


@pytest.fixture
def refused(cfg, http, dispatch, con):
    ev = mention(cfg, "100.1", OTHER_ID, f"{TAG} do the thing")
    res = trigger.handle(ev, cfg, dispatch, trigger.slack_notify(cfg))
    return SimpleNamespace(ev=ev, res=res, request=trigger.get_request(con, res["request_id"]))


def test_someone_else_not_enabled_is_refused_and_nothing_runs(refused, dispatch):
    assert refused.res["action"] == "refused", refused.res
    assert not dispatch.ran


def test_the_refusal_is_posted_once_in_the_thread(refused, http):
    posts = calls_to(http, "chat.postMessage")
    assert len(posts) == 1
    assert posts[0]["form"]["thread_ts"] == "100.1"


def test_the_refusal_is_translated_and_the_signature_left_as_it_is(refused, http):
    text = calls_to(http, "chat.postMessage")[0]["form"]["text"]
    assert text.startswith("[translated] I'm not enabled")
    assert text.endswith("\n" + OWNER_SIGNATURE)


def test_the_refusal_is_logged_anyway_with_the_notice_as_an_action(refused, con):
    assert refused.request["status"] == "refused"
    assert refused.request["reason"] == "not-enabled"
    assert [a["kind"] for a in trigger.actions_for(con, refused.request["id"])] == ["refusal-notice"]


def test_a_second_refusal_in_the_same_thread_is_logged_but_not_reposted(refused, cfg, http, dispatch):
    ev2 = mention(cfg, "100.2", OTHER_ID, f"{TAG} again", thread="100.1")
    res = trigger.handle(ev2, cfg, dispatch, trigger.slack_notify(cfg))
    assert res["action"] == "refused"
    assert len(calls_to(http, "chat.postMessage")) == 1


@pytest.fixture
def opened_by_owner(cfg, http, dispatch, con):
    own = mention(cfg, "100.3", OWNER_ID, f"{TAG} summarise", thread="100.1")
    return trigger.handle(own, cfg, dispatch)


def test_the_owner_runs_anywhere(opened_by_owner, dispatch):
    assert opened_by_owner["action"] == "run"
    assert dispatch.ran == [opened_by_owner["request_id"]]


def test_the_owner_opens_the_conversation(opened_by_owner, con):
    assert trigger.gate_enabled(con, "slack:C1:100.1")


def test_someone_else_in_an_opened_conversation_runs(opened_by_owner, cfg, dispatch):
    ev = mention(cfg, "100.4", OTHER_ID, f"{TAG} now?", thread="100.1")
    assert trigger.handle(ev, cfg, dispatch)["action"] == "run"
    assert len(dispatch.ran) == 2


def test_another_thread_in_the_channel_is_still_closed(opened_by_owner, cfg, dispatch):
    other = mention(cfg, "200.1", OTHER_ID, f"{TAG} hi")
    assert trigger.handle(other, cfg, dispatch)["action"] == "refused"


def test_opening_the_whole_channel_opens_every_thread_in_it(cfg, http, dispatch, con):
    trigger.set_gate(con, "slack:C1", True, by="test")
    chan = mention(cfg, "300.1", OTHER_ID, f"{TAG} hi")
    assert trigger.handle(chan, cfg, dispatch)["action"] == "run"


def test_a_malformed_scope_is_refused(con):
    assert trigger.set_gate(con, "slack C1; drop", True)["ok"] is False


# ===================================================================== loops
def test_a_message_carrying_the_signature_is_ignored(cfg, http, dispatch):
    ev = mention(cfg, "400.1", OWNER_ID, f"{TAG} done.\n{OWNER_SIGNATURE}")
    assert trigger.handle(ev, cfg, dispatch)["reason"] == "own reply"
    assert not dispatch.ran


def test_the_same_event_twice_runs_once(cfg, http, dispatch):
    ev = mention(cfg, "400.2", OWNER_ID, f"{TAG} go")
    first = trigger.handle(ev, cfg, dispatch)
    second = trigger.handle(dict(ev), cfg, dispatch)
    assert first["action"] == "run"
    assert second == {"action": "ignore", "reason": "seen"}
    assert len(dispatch.ran) == 1


def test_a_bots_message_is_not_a_mention(cfg, http):
    bot_msg = {**slack_msg("400.3", "UX", f"{TAG} hi"), "bot_id": "B1"}
    assert trigger.slack_event(bot_msg, CHANNEL, cfg, bot_user_id=BOT) is None


# ============================================================= audit + node
@pytest.fixture
def audited(cfg, http, dispatch, con):
    ev = mention(cfg, "500.1", OWNER_ID, f"{TAG} move PROJ-1 to review")
    rid = trigger.handle(ev, cfg, dispatch)["request_id"]
    return SimpleNamespace(ev=ev, rid=rid, request=trigger.get_request(con, rid))


def log_two_actions(con, audited):
    first = trigger.log_action(con, audited.rid, "jira-transition", "PROJ-1", "moved to In Review")
    second = trigger.log_action(con, audited.rid, "reply", audited.ev["permalink"], "told them")
    return first, second


def test_the_request_id_is_a_uuid(audited):
    assert len(audited.rid) == 36
    assert audited.rid.count("-") == 4


def test_who_where_when_and_what_are_recorded(audited):
    req = audited.request
    assert req["requester_id"] == OWNER_ID
    assert req["scope"] == "slack:C1:500.1"
    assert req["received_at"]
    assert "PROJ-1" in req["text"]
    assert req["permalink"]


def test_the_request_is_mirrored_as_a_request_node(audited, con):
    node = con.execute("SELECT * FROM nodes WHERE id=?", (audited.request["node_id"],)).fetchone()
    assert node is not None
    assert node["type"] == "request"


def test_actions_append_under_the_request(audited, con):
    first, second = log_two_actions(con, audited)
    assert first["ok"]
    assert second["ok"]
    assert [x["kind"] for x in trigger.actions_for(con, audited.rid)] == ["jira-transition", "reply"]


def test_actions_are_copied_onto_the_node(audited, con):
    log_two_actions(con, audited)
    row = con.execute("SELECT props FROM nodes WHERE id=?", (audited.request["node_id"],)).fetchone()
    assert [x["kind"] for x in json.loads(row["props"])["actions"]] == ["jira-transition", "reply"]


def test_an_action_for_no_request_is_refused(con):
    assert trigger.log_action(con, "nope", "reply")["ok"] is False


def test_an_action_with_no_kind_is_refused(audited, con):
    assert trigger.log_action(con, audited.rid, "")["ok"] is False


def test_the_cli_logs_an_action_from_the_request_id_variable(audited, home, run_script):
    proc = run_script("trigger.py", "action", "--kind", "slack-message", "--detail", "cli",
                      home=home, env={"MEMORY_LEDGER_REQUEST_ID": audited.rid})
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["ok"]


# ================================================================== dispatch
@pytest.fixture
def session(cfg, http, dispatch, con, home):
    """One request from the owner, handled and run through the fake claude."""
    ev = mention(cfg, "600.1", OWNER_ID, f"{TAG} summarise this")
    rid = trigger.handle(ev, cfg, dispatch)["request_id"]
    res = trigger.run_request(rid, cfg)
    last = claude_calls(home)[-1]
    return SimpleNamespace(ev=ev, rid=rid, res=res, request=trigger.get_request(con, rid),
                           last=last, args=last["args"], prompt=last["prompt"])


def test_a_session_that_succeeds_is_done(session):
    assert session.res["status"] == "done"
    assert session.request["status"] == "done"


def test_its_result_and_cost_are_recorded(session):
    assert session.request["result"] == "replied in thread"
    assert session.request["cost_usd"] == pytest.approx(0.0123)


def test_the_call_is_headless_json_with_permissions_skipped(session):
    assert session.args[0] == "-p"
    assert "--dangerously-skip-permissions" in session.args
    assert session.args[session.args.index("--output-format") + 1] == "json"


def test_extra_args_ride_along(session):
    assert session.args[-2:] == ["--model", "x"]


def test_the_request_id_and_source_reach_the_sessions_env(session):
    assert session.last["rid"] == session.rid
    assert session.last["src"] == "slack"


def test_the_prompt_goes_in_on_stdin_not_on_the_command_line(session):
    assert "summarise this" not in " ".join(session.args)
    assert session.args[1] == "--output-format"


def test_the_prompt_carries_the_text_the_permalink_and_the_source(session):
    assert "summarise this" in session.prompt
    assert session.ev["permalink"] in session.prompt
    assert "source: slack" in session.prompt


def test_the_prompt_names_the_respond_skill_and_the_signature(session):
    assert "`respond` skill" in session.prompt
    assert OWNER_SIGNATURE in session.prompt


def test_a_request_that_is_not_queued_is_not_run_twice(session, cfg):
    assert trigger.run_request(session.rid, cfg)["ok"] is False


def test_a_session_that_fails_is_failed_with_its_reason(cfg, http, dispatch, con):
    ev = mention(cfg, "600.2", OWNER_ID, f"{TAG} FAIL-ME")
    rid = trigger.handle(ev, cfg, dispatch)["request_id"]
    assert trigger.run_request(rid, cfg)["status"] == "failed"
    assert trigger.get_request(con, rid)["result"] == "it broke"


def test_mcp_config_is_expanded_and_strict_when_asked(cfg):
    mcfg = copy.deepcopy(cfg)
    mcfg["claude"].update(mcp_config="~/mcp.json", strict_mcp_config=True)
    cmd = trigger.build_command(mcfg)
    assert f"--mcp-config={Path('~/mcp.json').expanduser()}" in cmd
    assert "--strict-mcp-config" in cmd


def test_a_claude_that_cannot_start_is_failed_not_a_crash(cfg, http, dispatch, home):
    bcfg = copy.deepcopy(cfg)
    bcfg["claude"]["bin"] = str(home / "no-such-claude")
    ev = mention(cfg, "600.3", OWNER_ID, f"{TAG} x")
    rid = trigger.handle(ev, cfg, dispatch)["request_id"]
    assert trigger.run_request(rid, bcfg)["status"] == "failed"


@pytest.fixture
def recovered(cfg, http, dispatch, con):
    queued = trigger.handle(mention(cfg, "600.4", OWNER_ID, f"{TAG} y"), cfg, dispatch)["request_id"]
    running = trigger.handle(mention(cfg, "600.5", OWNER_ID, f"{TAG} z"), cfg, dispatch)["request_id"]
    trigger._set_request(con, running, status="running")
    again: list[str] = []
    res = trigger.recover(cfg, again.append)
    return SimpleNamespace(queued=queued, running=running, again=again, res=res)


def test_on_start_a_running_request_is_interrupted(recovered, con):
    assert trigger.get_request(con, recovered.running)["status"] == "interrupted"
    assert recovered.res["interrupted"] >= 1


def test_on_start_a_queued_request_is_dispatched(recovered):
    assert recovered.queued in recovered.again


# =================================================================== logging
@pytest.fixture
def trigger_log(home):
    """The trigger logger, set up under the test's home and torn down after."""
    def reset():
        for handler in list(trigger.log.handlers):
            trigger.log.removeHandler(handler)
            handler.close()
        trigger.log._brain_path = None
    reset()
    yield
    reset()


@pytest.fixture
def log_lines(trigger_log):
    lines: list[str] = []

    class Keep(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    path = trigger.setup_logging()
    trigger.log.addHandler(Keep())
    return SimpleNamespace(lines=lines, path=path)


@pytest.fixture
def logged_request(cfg, http, dispatch, log_lines):
    ev = mention(cfg, "650.1", OWNER_ID, f"{TAG} log me")
    rid = trigger.handle(ev, cfg, dispatch)["request_id"]
    return SimpleNamespace(ev=ev, rid=rid)


def test_setting_logging_up_twice_does_not_double_the_handlers(trigger_log):
    path = trigger.setup_logging()
    assert trigger.setup_logging() == path
    assert len(trigger.log.handlers) == 2


def test_a_new_request_is_logged_with_its_source_requester_and_decision(logged_request, log_lines):
    rid = logged_request.rid
    assert any(rid in m and "new request" in m and "slack" in m and "(owner)" in m
               and "log me" in m and "queued" in m for m in log_lines.lines), log_lines.lines[-1:]


def test_its_task_is_logged_when_it_starts_and_ends_with_the_cost(logged_request, log_lines, cfg):
    rid = logged_request.rid
    trigger.run_request(rid, cfg)
    assert any(f"task {rid} started" in m for m in log_lines.lines), log_lines.lines[-2:]
    assert any(f"task {rid} done" in m and "$0.01" in m for m in log_lines.lines), log_lines.lines[-2:]


def test_a_mention_seen_before_is_not_logged_again(logged_request, log_lines, cfg, dispatch):
    seen = len(log_lines.lines)
    trigger.handle(logged_request.ev, cfg, dispatch)
    assert len(log_lines.lines) == seen


def test_a_refusal_is_logged_as_one(logged_request, log_lines, cfg, dispatch):
    ev = mention(cfg, "650.2", "USTRANGER", f"{TAG} hi", channel="C9")
    trigger.handle(ev, cfg, dispatch)
    assert "refused" in log_lines.lines[-1], log_lines.lines[-1:]


def test_it_all_lands_in_the_homes_trigger_log(logged_request, log_lines, home):
    assert "new request" in log_lines.path.read_text(encoding=UTF8)
    assert log_lines.path.parent == home / "logs"


def test_the_start_line_names_the_sources_the_pool_and_the_log(cfg, log_lines):
    from brain_api import trigger_runtime
    summary = trigger_runtime._summary(cfg, 3)
    assert "slack" in summary
    assert "jira (poll 60s)" in summary
    assert "3 sessions" in summary
    assert str(log_lines.path) in summary


# ============================================================= run commands
@pytest.fixture
def slack_request(cfg, http, dispatch, con):
    ev = mention(cfg, "1700000000.000200", OWNER_ID, f"{TAG} sum it up")
    req = trigger.get_request(con, trigger.handle(ev, cfg, dispatch)["request_id"])
    return SimpleNamespace(ev=ev, request=req, variables=trigger.request_variables(req, cfg))


@pytest.mark.parametrize("name, expected", [
    ("trigger_source", "slack"),
    ("trigger_kind", "mention"),
    ("trigger_user", f"Name {OWNER_ID}"),
    ("trigger_user_id", OWNER_ID),
    ("trigger_user_is_owner", "true"),
    ("trigger_content", f"{TAG} sum it up"),
    ("slack_channel", CHANNEL),
    ("slack_thread", "1700000000.000200"),
    ("github_repo", ""),
])
def test_slack_variables_name_source_kind_user_content_channel_and_thread(slack_request, name, expected):
    assert slack_request.variables[name] == expected


def test_the_slack_variables_carry_the_link(slack_request):
    assert slack_request.variables["trigger_link"] == slack_request.ev["permalink"]


def test_the_trigger_time_is_the_messages_own_not_when_it_was_read(slack_request):
    assert slack_request.variables["trigger_time"] == "2023-11-14T22:13:20+00:00"
    assert slack_request.variables["trigger_received"]


def test_every_catalogued_variable_has_a_value_even_if_empty(slack_request):
    assert set(slack_request.variables) == set(trigger.VARIABLES)


def test_no_run_command_means_no_instructions_block(slack_request, cfg):
    assert "<<<INSTRUCTIONS" not in trigger.build_prompt(slack_request.request, cfg)


@pytest.fixture
def run_command_request(cfg, http, dispatch, con):
    pcfg = copy.deepcopy(cfg)
    pcfg["slack"]["prompt"] = ("{trigger_source} sent this task. Pick it up and reply to "
                               "{trigger_user} in {trigger_where}. Braces {like this} stay.")
    ev = mention(pcfg, "1700000000.000201", OWNER_ID, f"{TAG} sum it up")
    req = trigger.get_request(con, trigger.handle(ev, pcfg, dispatch)["request_id"])
    return SimpleNamespace(request=req, prompt=trigger.build_prompt(req, cfg))


def test_the_run_command_is_kept_on_the_request_as_it_was_when_it_fired(run_command_request):
    meta = run_command_request.request["meta"]
    assert meta["do"].startswith("{trigger_source} sent this task")
    assert meta["action"] == {"type": "claude"}


def test_a_run_command_is_rendered_into_the_prompt_ahead_of_the_guardrails(run_command_request):
    prompt = run_command_request.prompt
    assert "slack sent this task. Pick it up and reply to Name UOWNER in Slack channel C1" in prompt
    assert "Braces {like this} stay." in prompt
    assert prompt.index("<<<INSTRUCTIONS") < prompt.index(" decline --request ")


def test_the_message_is_still_quoted_verbatim_below_the_run_command(run_command_request):
    assert f"<<<MESSAGE\n{TAG} sum it up" in run_command_request.prompt


def test_jira_variables_carry_the_issue_its_summary_and_the_comments_time_in_utc(cfg, http, dispatch, con):
    issue = {"key": "K-7", "fields": {"summary": "Fix the export"}}
    comment = {"id": "9", "body": f"{JIRA_MARKER} please", "created": "2026-09-23T11:18:00.000-0300",
               "author": {"accountId": JIRA_OWNER, "displayName": "Owner"}}
    jev = trigger.jira_event(issue, comment, cfg, base=JIRA_BASE, owner_id=JIRA_OWNER)
    jreq = trigger.get_request(con, trigger.handle(jev, cfg, dispatch)["request_id"])
    v = trigger.request_variables(jreq, cfg)
    assert v["jira_issue"] == "K-7"
    assert v["trigger_title"] == "Fix the export"
    assert v["trigger_time"] == "2026-09-23T14:18:00+00:00"


def test_saving_an_unknown_variable_is_refused_naming_it_and_the_real_ones(cfg):
    res = trigger.save_config({"slack": {"prompt": "hi {trigger_usr}"}})
    assert not res["ok"]
    assert "{trigger_usr}" in res["error"]
    assert "{trigger_user}" in res["error"]


def test_an_unknown_variable_in_a_github_events_command_is_refused_too(cfg):
    res = trigger.save_config({"github": {"events": {"merged": {"do": "{github_repo} {oops}"}}}})
    assert not res["ok"]
    assert "{oops}" in res["error"]


def test_a_good_run_command_saves_and_the_page_gets_the_catalogue(cfg):
    res = trigger.save_config({"jira": {"prompt": "{trigger_user} on {jira_issue}"}})
    assert res["ok"]
    assert res["config"]["jira"]["prompt"] == "{trigger_user} on {jira_issue}"
    variables = res["config"]["variables"]
    assert any(x["name"] == "trigger_content" and x["global"] for x in variables)
    assert any(x["name"] == "github_repo" and x["sources"] == ["github"] for x in variables)


# ============================================================= slack catch-up
@pytest.fixture
def catch_up(cfg, http, dispatch, con):
    first = trigger.slack_catch_up(cfg, dispatch, now=1000.0)
    first_checkpoint = trigger.get_checkpoint(con, "slack")
    s = trigger.SLACK_API
    history = {"ok": True, "messages": [
        {"ts": "1001.0", "user": OWNER_ID, "text": f"{TAG} new top-level"},
        {"ts": "900.0", "user": OWNER_ID, "text": f"{TAG} older than the checkpoint"},
        {"ts": "800.0", "user": "UX", "text": "an old thread", "reply_count": 2,
         "latest_reply": "1002.0"},
        {"ts": "1003.0", "user": OWNER_ID, "text": "no mention here"}]}
    replies = {"ok": True, "messages": [
        {"ts": "800.0", "user": "UX", "text": "an old thread"},
        {"ts": "1002.0", "user": OWNER_ID, "text": f"{TAG} in an old thread", "thread_ts": "800.0"}]}
    http.on("POST", s + "users.conversations", lambda **_: {"ok": True, "channels": [{"id": CHANNEL}]})
    http.on("POST", s + "conversations.history", lambda **_: history)
    http.on("POST", s + "conversations.replies", lambda **_: replies)
    trigger.slack_catch_up(cfg, dispatch, now=2000.0)
    return SimpleNamespace(first=first, first_checkpoint=first_checkpoint)


def test_a_first_run_answers_no_backlog_and_starts_from_now(catch_up):
    assert catch_up.first.get("first_run")
    assert catch_up.first_checkpoint == "1000.000000"


def test_new_mentions_run_including_a_reply_inside_an_older_thread(catch_up, dispatch, con):
    got = sorted(trigger.get_request(con, r)["event_id"] for r in dispatch.ran)
    assert got == ["C1:1001.0", "C1:1002.0"]


def test_the_thread_reply_keeps_its_thread_as_the_conversation(catch_up, dispatch, con):
    assert any(trigger.get_request(con, r)["scope"] == "slack:C1:800.0" for r in dispatch.ran)


def test_the_checkpoint_moves_to_the_start_of_the_pass(catch_up, con):
    assert trigger.get_checkpoint(con, "slack") == "2000.000000"


def test_history_reached_back_for_threads_not_only_to_the_checkpoint(catch_up, http):
    assert float(calls_to(http, "conversations.history")[-1]["form"]["oldest"]) < 1000.0


def test_a_second_pass_over_the_same_messages_runs_nothing_new(catch_up, cfg, dispatch):
    trigger.slack_catch_up(cfg, dispatch, now=3000.0)
    assert len(dispatch.ran) == 2


def test_the_checkpoint_never_moves_backwards(catch_up, cfg, dispatch, con):
    trigger.slack_catch_up(cfg, dispatch, now=3000.0)
    trigger.set_checkpoint(con, "slack", "10.0")
    assert trigger.get_checkpoint(con, "slack") == "3000.000000"


# ================================================================== channels
CHANNEL_A = "C0ABC123"
CHANNEL_B = "C0DEF456"


@pytest.fixture
def channels(cfg, http, con):
    http.on("POST", trigger.SLACK_API + "conversations.info",
            lambda form, **_: {"ok": True, "channel": {"name": f"name-{form['channel']}"}})
    return con


@pytest.fixture
def removed_channel(cfg, channels):
    trigger.add_channel(channels, CHANNEL_A, by="test", cfg=cfg)
    return trigger.remove_channel(channels, CHANNEL_A, by="test")


@pytest.mark.parametrize("given, expected", [
    ("c0abc123", CHANNEL_A),
    (f"https://x.slack.com/archives/{CHANNEL_A}/p1700000000", CHANNEL_A),
    ("#general", ""),
], ids=["an id", "a link to it", "anything else"])
def test_parse_channel(given, expected):
    assert trigger.parse_channel(given) == expected


def test_adding_a_channel_opens_it_with_its_name_looked_up_from_slack(cfg, channels):
    res = trigger.add_channel(channels, CHANNEL_A, by="test", cfg=cfg)
    assert res["ok"]
    assert res["channel"]["name"] == f"name-{CHANNEL_A}"
    assert trigger.gate_enabled(channels, f"slack:{CHANNEL_A}")


def test_a_name_given_by_hand_wins_without_the_hash(cfg, channels):
    trigger.add_channel(channels, CHANNEL_B, name="#ops", by="test", cfg=cfg)
    assert any(c["name"] == "ops" for c in trigger.list_channels(channels)["active"])


def test_removing_a_channel_closes_it(removed_channel, channels):
    assert removed_channel["ok"]
    assert not trigger.gate_enabled(channels, f"slack:{CHANNEL_A}")


def test_a_removed_channel_keeps_its_row_marked_removed_with_who_and_when(removed_channel, channels):
    removed = trigger.list_channels(channels)["removed"]
    assert [c["channel_id"] for c in removed] == [CHANNEL_A]
    assert removed[0]["removed_by"] == "test"
    assert removed[0]["removed_at"]


def test_removing_a_channel_that_is_not_open_says_so(removed_channel, channels):
    assert trigger.remove_channel(channels, CHANNEL_A)["ok"] is False


def test_re_adding_restores_the_same_row_rather_than_making_a_second(removed_channel, cfg, channels):
    res = trigger.add_channel(channels, CHANNEL_A, by="again", cfg=cfg)
    listing = trigger.list_channels(channels)
    assert res["ok"]
    assert CHANNEL_A in [c["channel_id"] for c in listing["active"]]
    assert not listing["removed"]
    count = channels.execute("SELECT count(*) FROM trigger_channel WHERE channel_id=?",
                             (CHANNEL_A,)).fetchone()[0]
    assert count == 1


def test_a_channel_wide_gate_is_the_list_set_gate_writes_it(cfg, channels):
    trigger.add_channel(channels, CHANNEL_B, name="#ops", by="test", cfg=cfg)
    assert trigger.set_gate(channels, f"slack:{CHANNEL_B}", False, by="t")["ok"]
    assert CHANNEL_B in [c["channel_id"] for c in trigger.list_channels(channels)["removed"]]


def test_status_carries_the_channel_list(cfg, channels):
    trigger.add_channel(channels, CHANNEL_A, by="test", cfg=cfg)
    assert CHANNEL_A in [c["channel_id"] for c in trigger.status_payload()["channels"]["active"]]


# =================================================================== notices
def notice_request(cfg, ts, user=OTHER_ID, text=f"{TAG} por favor faz isso"):
    ev = mention(cfg, ts, user, text, channel="C9")
    return trigger.intake(ev, cfg)["request_id"]      # no notify: posted by hand


def test_with_a_user_token_the_notice_is_translated_and_posted_in_the_thread(cfg, http, con):
    rid = notice_request(cfg, "700.1")
    res = trigger.deliver_notice(rid, NOT_HERE, cfg)
    post = calls_to(http, "chat.postMessage")[-1]["form"]
    assert res["how"] == "thread"
    assert post["text"] == f"[translated] {NOT_HERE}\n{OWNER_SIGNATURE}"
    assert trigger.actions_for(con, rid)[-1]["kind"] == "refusal-notice"


@pytest.fixture
def dm_notice(cfg, monkeypatch, con):
    """The owner's token cannot reach channel C9; opening a DM works."""
    fake = install_http(monkeypatch)
    sent: list[dict] = []

    def post_msg(form, **_):
        sent.append(form)
        if form["channel"] == "C9":
            return {"ok": False, "error": "channel_not_found"}
        return {"ok": True}
    fake.on("POST", trigger.SLACK_API + "chat.postMessage", post_msg)
    fake.on("POST", trigger.SLACK_API + "conversations.open",
            lambda form, **_: {"ok": True, "channel": {"id": f"D-{form['users']}"}})
    rid = notice_request(cfg, "700.2")
    return SimpleNamespace(fake=fake, sent=sent, rid=rid, res=trigger.deliver_notice(rid, NOT_HERE, cfg))


def test_when_the_owner_cannot_reach_the_channel_the_requester_gets_a_dm(dm_notice):
    assert dm_notice.res["how"] == "dm"
    assert dm_notice.sent[-1]["channel"] == f"D-{OTHER_ID}"


def test_the_dm_links_what_they_wrote_and_the_signature_stays_last(dm_notice):
    text = dm_notice.sent[-1]["text"]
    assert f"{SLACK_PERMALINK}700.2" in text
    assert text.endswith(OWNER_SIGNATURE)


def test_the_dm_is_logged_as_a_dm(dm_notice, con):
    assert trigger.actions_for(con, dm_notice.rid)[-1]["kind"] == "refusal-notice-dm"


def test_any_other_failure_is_a_failure_not_a_dm(dm_notice, cfg, con):
    dm_notice.fake.on("POST", trigger.SLACK_API + "chat.postMessage",
                      lambda form, **_: {"ok": False, "error": "ratelimited"})
    rid = notice_request(cfg, "700.3")
    res = trigger.deliver_notice(rid, NOT_HERE, cfg)
    assert res["ok"] is False
    assert trigger.actions_for(con, rid)[-1]["kind"] == "refusal-notice-failed"


@pytest.fixture
def no_user_token(cfg, http, home):
    config = copy.deepcopy(cfg)
    config["slack"].pop("user_token")
    rid = notice_request(config, "700.4")
    before = len(claude_calls(home))
    res = trigger.deliver_notice(rid, NOT_HERE, config)
    return SimpleNamespace(config=config, rid=rid, res=res, before=before, calls=claude_calls(home))


def test_without_a_user_token_one_session_goes_through_the_slack_mcp_no_direct_post(no_user_token, http):
    assert no_user_token.res["ok"]
    assert len(no_user_token.calls) == no_user_token.before + 1
    assert not calls_to(http, "chat.postMessage")


def test_the_session_is_told_to_translate_sign_untranslated_and_dm_when_it_cannot_post(no_user_token):
    prompt = no_user_token.calls[-1]["prompt"]
    assert "language the ORIGINAL MESSAGE" in prompt
    assert "untranslated" in prompt
    assert f"direct message to the user {OTHER_ID}" in prompt
    assert "por favor" in prompt


def test_where_the_session_posted_is_logged(no_user_token, con):
    assert "thread (via the Slack MCP)" in trigger.actions_for(con, no_user_token.rid)[-1]["detail"]


def test_a_session_that_had_to_dm_says_so_and_it_is_logged_as_a_dm(no_user_token, con):
    rid = notice_request(no_user_token.config, "700.5", text=f"{TAG} FORCE-DM please")
    trigger.deliver_notice(rid, NOT_HERE, no_user_token.config)
    assert trigger.actions_for(con, rid)[-1]["kind"] == "refusal-notice-dm"


def test_with_translation_off_the_template_is_posted_as_written_and_no_model_is_called(cfg, http, home):
    plain = copy.deepcopy(cfg)
    plain["translate_notices"] = False
    rid = notice_request(plain, "700.6")
    before = len(claude_calls(home))
    trigger.deliver_notice(rid, NOT_HERE, plain)
    assert calls_to(http, "chat.postMessage")[-1]["form"]["text"].startswith(NOT_HERE)
    assert len(claude_calls(home)) == before


def test_the_old_single_refusal_key_still_counts_as_the_not_enabled_one(home):
    assert trigger.refusal_text({"refusal": "Nope, {owner}."}, "not_enabled") == "Nope, Owner."


def test_the_reason_is_filled_and_an_empty_one_leaves_no_gap(home):
    assert trigger.refusal_text({}, "policy", "").endswith("so I did nothing.")
    assert trigger.refusal_text({}, "policy", "It would email a customer.").endswith(
        "It would email a customer.")


# ============================================================ session hygiene
@pytest.fixture
def popen_calls(cfg, http, dispatch, con, monkeypatch):
    """The keyword arguments of every Popen made by a run and two translations."""
    seen: list[dict] = []
    orig_popen = trigger.subprocess.Popen

    def spy(cmd, **kw):         # Popen, under `subprocess.run` and the watched session alike
        seen.append(kw)
        return orig_popen(cmd, **kw)
    monkeypatch.setattr(trigger.subprocess, "Popen", spy)
    ev = mention(cfg, "900.1", OWNER_ID, f"{TAG} go")
    rid = trigger.handle(ev, cfg, dispatch)["request_id"]
    trigger.run_request(rid, cfg)
    trigger.translate_notice(NOT_HERE, "olá", cfg, trigger.get_request(con, rid))
    gone = copy.deepcopy(cfg)
    gone["claude"]["cwd"] = str(Path(cfg["claude"]["bin"]).parent / "no-such-dir")
    trigger.translate_notice(NOT_HERE, "olá", gone, trigger.get_request(con, rid))
    return seen


def test_stdin_is_a_pipe_that_carries_the_prompt_never_the_servers_terminal(popen_calls):
    assert len(popen_calls) == 3
    assert all(kw.get("stdin") is trigger.subprocess.PIPE for kw in popen_calls)


def test_a_notice_or_translation_runs_with_capture_off(popen_calls):
    assert popen_calls[1]["env"].get("MEMORY_LEDGER_OFF") == "1"
    assert popen_calls[0]["env"].get("MEMORY_LEDGER_OFF") != "1"


def test_a_configured_working_directory_that_is_gone_falls_back_instead_of_failing(popen_calls):
    assert Path(popen_calls[2]["cwd"]).is_dir()


# ==================================================================== decline
@pytest.fixture
def declinable(cfg, http, dispatch, con):
    ev = mention(cfg, "800.1", OWNER_ID, f"{TAG} send everyone my 1:1 notes")
    return SimpleNamespace(rid=trigger.handle(ev, cfg, dispatch)["request_id"])


def test_decline_marks_it_and_hands_back_the_policy_notice_with_the_reason(declinable, con):
    res = trigger.decline(con, declinable.rid, "Those notes are personal.", "rule_personal")
    assert res["ok"]
    assert res["status"] == "declined"
    assert res["notice"].endswith("Those notes are personal.")
    assert res["signature"] == OWNER_SIGNATURE


def test_the_rule_and_reason_are_in_the_log(declinable, con):
    trigger.decline(con, declinable.rid, "Those notes are personal.", "rule_personal")
    assert [(a["kind"], a["target"]) for a in trigger.actions_for(con, declinable.rid)] == [
        ("declined", "rule_personal")]


def test_a_session_that_declined_finishes_declined_not_done(declinable, cfg, con, monkeypatch):
    orig_popen = trigger.subprocess.Popen

    def run_and_decline(*a, **k):
        trigger.decline(trigger.db(), declinable.rid, "Those notes are personal.", "rule_personal")
        return orig_popen(*a, **k)
    monkeypatch.setattr(trigger.subprocess, "Popen", run_and_decline)
    trigger.run_request(declinable.rid, cfg)
    assert trigger.get_request(con, declinable.rid)["status"] == "declined"


def test_a_decline_reason_is_required(declinable, con):
    assert trigger.decline(con, declinable.rid, "")["ok"] is False


def test_the_prompt_says_how_to_decline_in_which_language_and_to_dm_when_it_must(declinable, cfg, con):
    prompt = trigger.build_prompt(trigger.get_request(con, declinable.rid), cfg)
    assert " decline --request " in prompt
    assert "language the message above is written in" in prompt
    assert "--kind reply-dm" in prompt


# ===================================================================== jira
def jira_ts(now, minutes):
    return (now + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


JIRA_NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
ACCOUNT_X = {"accountId": "acc-x", "displayName": "X"}
ACCOUNT_OWNER = {"accountId": JIRA_OWNER, "displayName": "Owner"}


def comment_route(http, issue, comments):
    http.on("GET", f"{JIRA_BASE}/rest/api/3/issue/{issue}/comment", lambda **_: comments)


def test_adf_reads_as_text_and_mentions_as_their_display_names():
    doc = adf(("m", JIRA_OWNER, "@Owner"), " @Cerebro please")
    assert trigger.adf_text(doc).strip() == "@Owner @Cerebro please"


def test_the_marker_is_case_and_accent_blind():
    assert trigger.has_marker("hey @CEREBRO", JIRA_MARKER)
    assert not trigger.has_marker("hey", "@x")


@pytest.fixture
def jira_env(cfg, http, dispatch, con):
    return SimpleNamespace(jira=trigger.Jira(cfg), con=con, now=JIRA_NOW)


def test_a_first_jira_poll_answers_no_backlog(jira_env, cfg, dispatch):
    res = trigger.jira_poll(cfg, dispatch, now=jira_env.now, jira=jira_env.jira)
    assert res.get("first_run")
    assert not dispatch.ran


@pytest.fixture
def jira_polled(jira_env, cfg, http, dispatch, con):
    """A first poll, then ten minutes later one that finds five comments on PROJ-7."""
    now = jira_env.now
    trigger.jira_poll(cfg, dispatch, now=now, jira=jira_env.jira)
    comments = {"comments": [
        {"id": "5", "created": jira_ts(now, 9), "author": ACCOUNT_OWNER,
         "body": adf("done.\n", SIGNATURE_TEXT)},
        {"id": "4", "created": jira_ts(now, 8), "author": ACCOUNT_X,
         "body": adf(f"{JIRA_MARKER} no owner mention")},
        {"id": "3", "created": jira_ts(now, 7), "author": ACCOUNT_X,
         "body": adf(("m", JIRA_OWNER, "@Owner"), f" {JIRA_MARKER} please triage")},
        {"id": "2", "created": jira_ts(now, 6), "author": ACCOUNT_OWNER,
         "body": adf("@Cerebro move to review")},
        {"id": "1", "created": jira_ts(now, -600), "author": {"accountId": JIRA_OWNER},
         "body": adf(f"{JIRA_MARKER} ancient")}]}
    searched: list[dict] = []
    http.on("POST", JIRA_BASE + "/rest/api/3/search/jql",
            lambda body, **_: searched.append(body) or {"issues": [
                {"key": "PROJ-7", "fields": {"summary": "A bug"}}], "isLast": True})
    comment_route(http, "PROJ-7", comments)
    http.on("POST", JIRA_BASE + "/rest/api/3/issue/PROJ-7/comment", lambda **_: {"id": "99"})
    trigger.jira_poll(cfg, dispatch, now=now + timedelta(minutes=10), jira=jira_env.jira)
    return SimpleNamespace(
        searched=searched,
        events=sorted(trigger.get_request(con, r)["event_id"] for r in dispatch.ran),
        requests={r["event_id"]: r for r in trigger.list_requests(con, 100)})


def test_the_jira_search_is_relative_so_the_profiles_timezone_cannot_shift_it(jira_polled):
    assert jira_polled.searched[-1]["jql"].startswith("updated >= -12m"), jira_polled.searched[-1]["jql"]


def test_the_owners_own_marked_comment_runs(jira_polled):
    assert "PROJ-7:2" in jira_polled.events


def test_a_colleagues_reply_after_the_owner_opened_the_issue_runs_in_the_same_pass(jira_polled):
    assert "PROJ-7:3" in jira_polled.events
    assert jira_polled.requests["PROJ-7:3"]["status"] == "queued"


def test_the_owners_comment_opens_the_issue(jira_polled, con):
    assert trigger.gate_enabled(con, "jira:PROJ-7")


def test_a_marker_without_a_mention_of_the_owner_is_not_a_mention(jira_polled):
    assert "PROJ-7:4" not in jira_polled.requests


def test_the_brains_own_signed_comment_is_not_a_mention(jira_polled):
    assert "PROJ-7:5" not in jira_polled.requests


def test_comments_older_than_the_checkpoint_window_are_not_read(jira_polled):
    assert "PROJ-7:1" not in jira_polled.requests


def test_the_permalink_focuses_the_comment(jira_polled):
    assert jira_polled.requests["PROJ-7:2"]["permalink"] == f"{JIRA_BASE}/browse/PROJ-7?focusedCommentId=2"


@pytest.fixture
def jira_refusal(jira_env, cfg, http, dispatch, con):
    """Someone else mentions the owner on an issue nobody opened."""
    now = jira_env.now
    trigger.jira_poll(cfg, dispatch, now=now, jira=jira_env.jira)
    comments = {"comments": [
        {"id": "11", "created": jira_ts(now, 15), "author": ACCOUNT_X,
         "body": adf(("m", JIRA_OWNER, "@Owner"), f" {JIRA_MARKER} please")}]}
    http.on("POST", JIRA_BASE + "/rest/api/3/search/jql",
            lambda body, **_: {"issues": [{"key": "PROJ-8"}], "isLast": True})
    comment_route(http, "PROJ-8", comments)
    posted: list[dict] = []
    http.on("POST", JIRA_BASE + "/rest/api/3/issue/PROJ-8/comment",
            lambda body, **_: posted.append(body) or {"id": "12"})
    trigger.jira_poll(cfg, dispatch, now=now + timedelta(minutes=16), jira=jira_env.jira)
    request = {x["event_id"]: x for x in trigger.list_requests(con, 100)}["PROJ-8:11"]
    return SimpleNamespace(request=request, posted=posted)


def test_someone_else_on_an_issue_nobody_opened_is_refused(jira_refusal):
    assert jira_refusal.request["status"] == "refused"


def test_the_refusal_is_one_comment_signed_in_italics_with_no_marker_in_it(jira_refusal):
    assert len(jira_refusal.posted) == 1
    sig = json.dumps(jira_refusal.posted[0])
    assert '"em"' in sig
    assert SIGNATURE_TEXT in sig
    assert "rebro" not in sig


def test_a_refused_request_can_be_reprocessed_the_owner_vouching_for_that_one(jira_refusal, con):
    res = trigger.reprocess(con, jira_refusal.request["id"])
    assert res["ok"]
    assert res["was"] == "refused"
    assert trigger.get_request(con, jira_refusal.request["id"])["status"] == "queued"


def test_a_person_is_allowed_by_their_account_id(con, cfg):
    assert trigger.allow_person(con, "jira", "acc-x", "X", by="test", cfg=cfg)["ok"]


def test_an_id_jira_could_not_have_issued_is_refused(con):
    assert trigger.allow_person(con, "jira", "x y", by="test")["ok"] is False


def test_only_jira_keeps_a_people_list(con):
    assert trigger.allow_person(con, "slack", "U0ABC")["ok"] is False


def test_the_owner_is_never_on_the_people_list(con):
    cfg = {"jira": {"owner_account_id": JIRA_OWNER}}
    assert trigger.allow_person(con, "jira", JIRA_OWNER, cfg=cfg)["ok"] is False


def test_an_allowed_persons_mention_runs_on_an_issue_nobody_opened_with_no_refusal(
        jira_env, cfg, http, dispatch, con):
    now = jira_env.now
    trigger.jira_poll(cfg, dispatch, now=now, jira=jira_env.jira)
    trigger.allow_person(con, "jira", "acc-x", "X", by="test", cfg=cfg)
    comments = {"comments": [
        {"id": "21", "created": jira_ts(now, 20), "author": ACCOUNT_X,
         "body": adf(("m", JIRA_OWNER, "@Owner"), f" {JIRA_MARKER} please")}]}
    http.on("POST", JIRA_BASE + "/rest/api/3/search/jql",
            lambda body, **_: {"issues": [{"key": "PROJ-9"}], "isLast": True})
    comment_route(http, "PROJ-9", comments)
    posted: list[dict] = []
    http.on("POST", JIRA_BASE + "/rest/api/3/issue/PROJ-9/comment",
            lambda body, **_: posted.append(body) or {"id": "22"})
    trigger.jira_poll(cfg, dispatch, now=now + timedelta(minutes=21), jira=jira_env.jira)
    request = {x["event_id"]: x for x in trigger.list_requests(con, 100)}["PROJ-9:21"]
    assert request["status"] == "queued"
    assert request["reason"] == "allowed user"
    assert not posted
    assert not trigger.gate_enabled(con, "jira:PROJ-9")


@pytest.fixture
def disallowed(con, cfg):
    trigger.allow_person(con, "jira", "acc-x", "X", by="test", cfg=cfg)
    return trigger.disallow_person(con, "jira", "acc-x", by="test")


def test_taking_a_person_off_keeps_the_row_marked_removed_with_who(disallowed, con):
    listing = trigger.list_people(con, "jira")
    assert disallowed["ok"]
    assert not listing["active"]
    assert listing["removed"][0]["removed_by"] == "test"
    assert not trigger.person_allowed(con, "jira", "acc-x")


def test_taking_off_someone_not_on_the_list_says_so(disallowed, con):
    assert trigger.disallow_person(con, "jira", "acc-x")["ok"] is False


def test_allowing_them_again_restores_the_same_row(disallowed, con):
    assert trigger.allow_person(con, "jira", "acc-x", by="again")["ok"]
    assert con.execute("SELECT count(*) FROM trigger_person").fetchone()[0] == 1
    assert trigger.list_people(con)["active"][0]["name"] == "X"


def test_status_carries_the_people_list(disallowed, con):
    trigger.allow_person(con, "jira", "acc-x", by="again")
    assert trigger.status_payload()["people"]["active"][0]["user_id"] == "acc-x"


@pytest.fixture
def user_search(cfg, http):
    http.on("GET", JIRA_BASE + "/rest/api/3/user/search",
            lambda **_: [{"accountId": "acc-i", "displayName": "Isa", "active": True,
                          "accountType": "atlassian"},
                         {"accountId": "acc-app", "displayName": "Bot", "accountType": "app"},
                         {"accountId": "acc-old", "displayName": "Gone", "active": False}])


def test_finding_people_returns_active_humans_only(user_search, cfg):
    found = trigger.find_people(cfg, "jira", "isa")
    assert found["ok"], found
    assert [x["user_id"] for x in found["people"]] == ["acc-i"]


def test_a_one_letter_search_is_refused(user_search, cfg):
    assert trigger.find_people(cfg, "jira", "i")["ok"] is False


def test_without_a_base_url_the_poll_says_what_is_missing(cfg, dispatch):
    nourl = {**cfg, "jira": {**cfg["jira"], "base_url": ""}}
    assert "jira.base_url" in trigger.jira_poll(nourl, dispatch)["error"]


# =================================================================== github
GH_NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def gh_ts(minutes):
    return (GH_NOW + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def gh_item(api, n, author, assoc="MEMBER", body="", created=-600, merged=None):
    return {"number": n, "title": f"PR {n}", "html_url": f"{GITHUB_REPO_URL}/pull/{n}",
            "repository_url": f"{api}/repos/o/r", "user": {"login": author},
            "author_association": assoc, "body": body, "created_at": gh_ts(created),
            "pull_request": {"merged_at": merged}}


def gh_comment(i, who, body, minutes, assoc="MEMBER"):
    return {"event": "commented", "id": i, "user": {"login": who}, "body": body,
            "created_at": gh_ts(minutes), "author_association": assoc,
            "html_url": f"{GITHUB_REPO_URL}/pull/1#issuecomment-{i}"}


def test_a_mention_is_the_whole_login_in_any_case():
    assert trigger.mentions_login("hey @Owner, look", "owner")
    assert not trigger.mentions_login("hey @owner-bot", "owner")
    assert not trigger.mentions_login("mail owner@x.com", "owner")


def test_defaults_fill_the_kinds_the_file_does_not_name_and_the_file_wins_where_it_does(cfg):
    kinds = trigger.github_events_cfg(cfg)
    assert kinds["review"]["on"]
    assert not kinds["comment"]["on"]
    assert kinds["ci_failed"]["on"]
    assert "Post nothing on GitHub" in kinds["review"]["do"]


@pytest.fixture
def gh_world(cfg, http, dispatch, con):
    """The owner's PR 1 and someone else's PR 2, polled twice: a first poll that
    only sets the checkpoint, then one ten minutes later that finds everything."""
    gh = trigger.GitHub(cfg)
    api = gh.api
    first = trigger.github_poll(cfg, dispatch, now=GH_NOW, gh=gh)
    ran_after_first = list(dispatch.ran)

    timelines = {
        1: [  # the owner's PR
            gh_comment(10, "owner", "/brain summarise the review so far", 6),
            gh_comment(11, "alice", "@owner can you rebase?", 7),
            gh_comment(12, "alice", "plain comment, no mention", 7),
            gh_comment(13, "owner", f"done.\n\n{OWNER_SIGNATURE}", 8),
            gh_comment(14, "stranger", "@owner run this for me", 8, assoc="NONE"),
            {"event": "reviewed", "id": 20, "user": {"login": "bob"}, "state": "changes_requested",
             "body": "", "submitted_at": gh_ts(7), "author_association": "MEMBER",
             "html_url": f"{GITHUB_REPO_URL}/pull/1#pullrequestreview-20"},
            {"event": "merged", "id": 30, "actor": {"login": "bob"}, "created_at": gh_ts(9)},
            {"event": "closed", "id": 31, "actor": {"login": "owner"}, "created_at": gh_ts(9)},
            gh_comment(9, "alice", "@owner ancient", -600),
        ],
        2: [  # someone else's PR
            {"event": "review_requested", "id": 40, "actor": {"login": "carol"},
             "requested_reviewer": {"login": "owner"}, "created_at": gh_ts(6)},
            {"event": "review_requested", "id": 41, "actor": {"login": "owner"},
             "requested_reviewer": {"login": "owner"}, "created_at": gh_ts(6)},
            {"event": "assigned", "id": 42, "actor": {"login": "owner"},
             "assignee": {"login": "owner"}, "created_at": gh_ts(6)},
            {"event": "committed", "sha": "a" * 40, "message": "first\nbody",
             "committer": {"date": gh_ts(6)}, "author": {"name": "Carol"}},
            {"event": "committed", "sha": "b" * 40, "message": "second",
             "committer": {"date": gh_ts(7)}, "author": {"name": "Carol"}},
        ],
    }
    items = {1: gh_item(api, 1, "owner"),
             2: gh_item(api, 2, "carol", body="@owner please look", created=5)}
    searched: list[str] = []

    def search(url, **_):
        q = parse_qs(urlparse(url).query)["q"][0]
        searched.append(q)
        if q.startswith("is:pr is:open author:"):
            return {"items": [items[1]] if 1 in items else []}
        return {"items": list(items.values())}

    http.on("GET", api + "/search/issues", search)
    for n, tl in timelines.items():
        http.on("GET", f"{api}/repos/o/r/issues/{n}/timeline", lambda tl=tl, **_: tl)
    http.on("GET", f"{api}/repos/o/r/pulls/1", lambda **_: {"head": {"sha": "c" * 40}})
    http.on("GET", f"{api}/repos/o/r/commits/{'c' * 40}/check-runs", lambda **_: {"check_runs": [
        {"id": 1, "name": "build", "conclusion": "failure", "completed_at": gh_ts(8),
         "html_url": f"{GITHUB_REPO_URL}/runs/1"},
        {"id": 2, "name": "lint", "conclusion": "success", "completed_at": gh_ts(8)},
        {"id": 3, "name": "e2e", "conclusion": "cancelled", "completed_at": gh_ts(8)}]})
    posted: list[dict] = []
    http.on("POST", f"{api}/repos/o/r/issues/1/comments", lambda body, **_: posted.append(body) or {})

    res = trigger.github_poll(cfg, dispatch, now=GH_NOW + timedelta(minutes=10), gh=gh)
    requests = {r["event_id"]: r for r in trigger.list_requests(con, 100)}
    return SimpleNamespace(gh=gh, api=api, first=first, ran_after_first=ran_after_first, res=res,
                           items=items, timelines=timelines, searched=searched, posted=posted,
                           requests=requests)


def test_a_first_github_poll_answers_no_backlog(gh_world):
    assert gh_world.first.get("first_run")
    assert not gh_world.ran_after_first


def test_the_github_poll_ran(gh_world):
    assert gh_world.res.get("ok"), gh_world.res


def test_the_search_is_since_the_checkpoint_and_asks_for_review_requests_and_reviewed_prs(gh_world):
    searched = gh_world.searched
    assert any(q.startswith("involves:owner updated:>=2026-09-23T11:58:00Z") for q in searched), searched
    assert any("review-requested:owner" in q for q in searched), searched
    assert any("reviewed-by:owner" in q for q in searched), searched


def test_the_owners_own_comment_with_the_marker_runs_and_opens_the_pr(gh_world, con):
    assert gh_world.requests["o/r#1:c10"]["status"] == "queued"
    assert trigger.gate_enabled(con, "github:o/r#1")


def test_the_owners_mention_carries_the_mentions_run_command(gh_world, cfg, con):
    own = trigger.get_request(con, gh_world.requests["o/r#1:c10"]["id"])
    assert own["meta"].get("kind") == "request"
    assert own["meta"].get("do") == trigger.github_events_cfg(cfg)["mention"]["do"]


def test_a_teammates_mention_runs(gh_world):
    assert gh_world.requests["o/r#1:c11"]["status"] == "queued"


def test_a_comment_with_no_mention_does_not_run_while_comment_is_off(gh_world):
    assert "o/r#1:c12" not in gh_world.requests


def test_the_brains_own_signed_github_comment_is_not_a_mention(gh_world):
    assert "o/r#1:c13" not in gh_world.requests


def test_a_strangers_mention_runs_only_because_the_owner_opened_this_pr(gh_world):
    assert gh_world.requests["o/r#1:c14"]["reason"] == "enabled"


def test_a_review_on_the_owners_pr_runs_as_a_subscription(gh_world):
    assert gh_world.requests["o/r#1:r20"]["reason"] == "subscribed: review"


def test_the_merge_runs(gh_world):
    assert "o/r#1:m30" in gh_world.requests


def test_nothing_older_than_the_window_runs(gh_world):
    assert "o/r#1:c9" not in gh_world.requests


def test_a_review_request_for_the_owner_runs(gh_world):
    assert "o/r#2:rr40" in gh_world.requests


def test_a_review_request_the_owner_made_for_themselves_runs_what_they_fire_they_act_on(gh_world):
    request = gh_world.requests["o/r#2:rr41"]
    assert request["reason"] == "subscribed: review_requested"
    assert "added yourself as a reviewer" in request["text"]


def test_assigned_and_closed_stay_off_until_switched_on(gh_world):
    assert not {"o/r#2:a42", "o/r#1:x31"} & set(gh_world.requests)


def test_switched_on_assigning_themselves_and_closing_their_own_pr_count_trusted(gh_world, cfg):
    on = copy.deepcopy(cfg)
    on["github"]["events"] = {**(on["github"].get("events") or {}),
                              "assigned": {"on": True}, "closed": {"on": True}}
    evs = {e["event_id"]: e for n in (1, 2) for e in trigger.github_item_events(
        gh_world.gh, gh_world.items[n], GH_NOW - timedelta(hours=1), on, "owner")}
    assert evs["o/r#2:a42"]["subscribed"], list(evs)
    assert evs["o/r#1:x31"]["subscribed"], list(evs)
    assert "You assigned yourself" in evs["o/r#2:a42"]["text"]


def test_a_description_that_mentions_the_owner_runs(gh_world):
    assert "o/r#2:body" in gh_world.requests


def test_several_commits_are_one_push_named_by_the_newest(gh_world):
    assert "o/r#2:pbbbbbbbbbbbb" in gh_world.requests
    assert not any(":paaaa" in i for i in gh_world.requests)


def test_failed_checks_are_one_event_per_commit_and_cancelled_is_not_a_failure(gh_world):
    ci = gh_world.requests["o/r#1:ci" + "c" * 12]
    assert "build" in ci["text"]
    assert "e2e" not in ci["text"]


def test_events_are_handled_oldest_first_across_prs(gh_world, dispatch, con):
    order = [trigger.get_request(con, r)["event_id"] for r in dispatch.ran]
    assert order.index("o/r#1:c10") < order.index("o/r#1:c11")


def test_a_subscriptions_prompt_carries_its_instruction_and_says_not_to_answer_on_github(gh_world, cfg, con):
    req = trigger.get_request(con, gh_world.requests["o/r#1:r20"]["id"])
    prompt = trigger.build_prompt(req, cfg)
    assert "github_event: review" in prompt
    assert "<<<INSTRUCTIONS\nRead the review" in prompt
    assert "not a\nmessage to answer" not in prompt
    assert "subscribed to" in prompt


def test_a_mentions_prompt_replies_with_a_comment(gh_world, cfg, con):
    prompt = trigger.build_prompt(trigger.get_request(con, gh_world.requests["o/r#1:c11"]["id"]), cfg)
    assert "add a comment on that GitHub pull request" in prompt


@pytest.fixture
def gh_review(gh_world, cfg, con):
    req = trigger.get_request(con, gh_world.requests["o/r#1:r20"]["id"])
    return SimpleNamespace(request=req, variables=trigger.request_variables(req, cfg))


def test_a_github_requests_variables_say_who_what_where_and_the_title(gh_review):
    v = gh_review.variables
    assert v["trigger_source"] == "github"
    assert v["trigger_kind"] == "review"
    assert v["trigger_user"]
    assert v["github_repo"] == "o/r"
    assert v["github_number"] == "1"
    assert v["github_item"] == "pull request"
    assert v["trigger_title"]
    assert v["trigger_time"].endswith("+00:00")
    assert v["slack_channel"] == ""


def test_a_github_events_run_command_has_its_variables_filled_in_and_an_unknown_one_kept(gh_review, cfg):
    req, v = gh_review.request, gh_review.variables
    vreq = {**req, "meta": {**req["meta"], "do": "{trigger_user} reviewed {github_repo}#"
                                                  "{github_number}; see {trigger_link}. {nope}"}}
    prompt = trigger.build_prompt(vreq, copy.deepcopy(cfg))
    assert f"{v['trigger_user']} reviewed o/r#1; see {req['permalink']}. {{nope}}" in prompt


def test_an_overlapping_poll_runs_nothing_twice(gh_world, cfg, dispatch, con):
    before = len(trigger.list_requests(con, 500))
    trigger.github_poll(cfg, dispatch, now=GH_NOW + timedelta(minutes=11), gh=gh_world.gh)
    assert len(trigger.list_requests(con, 500)) == before


@pytest.fixture
def stranger_pr(gh_world, cfg, http, dispatch, con):
    """A stranger mentions the owner on PR 3, which nobody opened."""
    api = gh_world.api
    gh_world.timelines[3] = [gh_comment(50, "stranger", "@owner do my homework", 20, assoc="NONE")]
    gh_world.items.clear()
    gh_world.items[3] = gh_item(api, 3, "stranger", assoc="NONE")
    http.on("GET", f"{api}/repos/o/r/issues/3/timeline", lambda **_: gh_world.timelines[3])
    http.on("POST", f"{api}/repos/o/r/issues/3/comments",
            lambda body, **_: gh_world.posted.append(body) or {})
    trigger.github_poll(cfg, dispatch, now=GH_NOW + timedelta(minutes=21), gh=gh_world.gh)
    return {x["event_id"]: x for x in trigger.list_requests(con, 500)}.get("o/r#3:c50", {})


def test_an_untrusted_mention_on_a_pr_nobody_opened_is_refused(stranger_pr):
    assert stranger_pr.get("status") == "refused"


def test_with_refusal_notice_off_nothing_is_posted_on_github(stranger_pr, gh_world):
    assert not gh_world.posted


def test_a_github_scope_can_be_opened_by_hand(con):
    assert trigger.set_gate(con, "github:o/r#3", True, by="cli")["ok"]
    assert not trigger.set_gate(con, "github:o/r", True)["ok"]


def test_with_refusal_notice_on_one_signed_comment_says_no(gh_world, cfg, http, dispatch):
    api = gh_world.api
    loud = {**cfg, "github": {**cfg["github"], "refusal_notice": True}}
    gh_world.items.clear()
    gh_world.items[4] = gh_item(api, 4, "stranger", assoc="NONE")
    http.on("GET", f"{api}/repos/o/r/issues/4/timeline",
            lambda **_: [gh_comment(51, "stranger", "@owner again", 25, assoc="NONE")])
    http.on("POST", f"{api}/repos/o/r/issues/4/comments",
            lambda body, **_: gh_world.posted.append(body) or {})
    trigger.github_poll({**loud, "translate_notices": False}, dispatch,
                        now=GH_NOW + timedelta(minutes=26), gh=gh_world.gh)
    posted = gh_world.posted
    assert len(posted) == 1, posted
    assert SIGNATURE_TEXT in posted[0]["body"]
    assert "/brain" not in posted[0]["body"]


def test_without_a_github_token_the_poll_says_what_is_missing(cfg, dispatch):
    notoken = {**cfg, "github": {**cfg["github"], "token": ""}}
    assert "token" in trigger.github_poll(notoken, dispatch)["error"]


def test_the_offline_github_check_names_the_events_that_trigger(cfg):
    res = trigger.check(cfg, offline=True, only="github")
    assert res["ready"], res
    assert any("ci_failed" in n for n in res["notes"]), res


# ===================================================================== check
def test_a_complete_config_checks_out(cfg, http):
    res = trigger.check(cfg, offline=True)
    assert res["ok"]
    assert res["ready"], res["problems"]


def test_a_missing_token_and_owner_are_named(cfg, http):
    bad = copy.deepcopy(cfg)
    bad["slack"].pop("app_token")
    bad["slack"]["owner_user_id"] = ""
    res = trigger.check(bad, offline=True)
    assert not res["ready"]
    assert any("app_token" in p for p in res["problems"])
    assert any("owner_user_id" in p for p in res["problems"])


@pytest.fixture
def jira_off(cfg):
    off = copy.deepcopy(cfg)
    off["jira"]["enabled"] = False
    off["jira"]["base_url"] = ""
    return off


def test_one_service_can_be_checked_while_it_is_still_switched_off(jira_off):
    res = trigger.check(jira_off, offline=True, only="jira")
    assert res["target"] == "jira"
    assert any("jira.base_url" in p for p in res["problems"])


def test_checking_one_service_does_not_report_on_the_others(jira_off):
    res = trigger.check(jira_off, offline=True, only="slack")
    assert res["ready"]
    assert not any("jira" in p for p in res["problems"])


def test_an_unknown_check_target_is_refused(cfg, http):
    assert trigger.check(cfg, only="email")["ok"] is False


def test_online_the_slack_check_proves_each_token_against_slack(cfg, http):
    s = trigger.SLACK_API
    http.on("POST", s + "apps.connections.open", lambda **_: {"ok": True, "url": "wss://x"})
    http.on("POST", s + "auth.test", lambda form=None, **_: {"ok": True, "user_id": BOT, "user": "me"})
    res = trigger.check(cfg, only="slack")
    assert res["ready"], res["notes"]
    assert sum("token" in n for n in res["notes"]) >= 3, res["notes"]


def test_the_check_names_the_token_slack_rejected(cfg, http):
    s = trigger.SLACK_API
    http.on("POST", s + "auth.test", lambda form=None, **_: {"ok": True, "user_id": BOT, "user": "me"})
    http.on("POST", s + "apps.connections.open", lambda **_: {"ok": False, "error": "invalid_auth"})
    res = trigger.check(cfg, only="slack")
    assert not res["ready"]
    assert any(p.startswith("app token") for p in res["problems"])


# ==================================================================== saving
@pytest.fixture
def first_save(home):
    return trigger.save_config({"enabled": True, "jira": {
        "enabled": True, "base_url": "https://j.example/", "token": SECRET_JIRA_TOKEN,
        "poll_seconds": 30}})


def test_a_first_save_creates_the_file(first_save):
    assert first_save["ok"], first_save
    assert trigger.config_path().exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX file modes")
def test_the_saved_file_is_mode_600_because_it_holds_tokens(first_save):
    assert (trigger.config_path().stat().st_mode & 0o777) == 0o600


def test_the_summary_says_a_token_is_stored_never_what_it_is(first_save):
    summary = first_save["config"]
    assert summary["secrets"]["jira.token"]["set"]
    assert SECRET_JIRA_TOKEN not in json.dumps(summary)


def test_a_trailing_slash_on_the_base_url_is_dropped(first_save):
    assert first_save["config"]["jira"]["base_url"] == "https://j.example"


@pytest.fixture
def partially_saved(first_save):
    trigger.save_config({"jira": {"token": "", "marker": "@brain"}})
    return read_config_file()


def test_an_empty_secret_keeps_the_stored_one(partially_saved):
    assert partially_saved["jira"]["token"] == SECRET_JIRA_TOKEN


def test_the_other_keys_survive_a_partial_save(partially_saved):
    assert partially_saved["jira"]["poll_seconds"] == 30
    assert partially_saved["enabled"] is True
    assert partially_saved["jira"]["marker"] == "@brain"


def test_clear_removes_a_secret(first_save):
    trigger.save_config({"clear": ["jira.token"]})
    assert "token" not in read_config_file()["jira"]


def test_a_secret_from_the_environment_is_reported_as_such(first_save, monkeypatch):
    monkeypatch.setenv("MEMORY_LEDGER_JIRA_TOKEN", "from-env")
    assert trigger.config_summary(trigger.load_config())["secrets"]["jira.token"]["source"] == "env"


@pytest.mark.parametrize("patch, needle", [
    ({"enabled": "yes"}, "true or false"),
    ({"jira": {"poll_seconds": 1}}, "from 15 to 3600"),
    ({"jira": {"base_url": "ftp://x"}}, "http(s) URL"),
    ({"slack": {"bot_token": "xoxp-wrong"}}, "should start with xoxb-"),
    ({"refusals": {"policy": 5}}, "must be text"),
    ({"clear": ["jira.email"]}, "is not a secret"),
    ({"github": {"token": "xoxb-nope"}}, "should start with ghp_"),
    ({"github": {"events": {"deploy": {"on": True}}}}, "is not an event"),
    ({"github": {"events": {"review": {"on": "yes"}}}}, "true or false"),
    ({"github": {"repos": ["o/r", "bad repo"]}}, "not valid"),
    ({"github": {"poll_seconds": 5}}, "from 30 to 3600"),
], ids=lambda value: value if isinstance(value, str) else "")
def test_a_bad_save_is_refused(home, patch, needle):
    res = trigger.save_config(patch)
    assert res["ok"] is False, res
    assert needle in res["error"], res


def test_both_notices_and_the_translation_switch_are_saved(home):
    trigger.save_config({"refusals": {"policy": "Não posso: {reason}"}, "translate_notices": False})
    raw = read_config_file()
    assert raw["refusals"]["policy"] == "Não posso: {reason}"
    assert raw["translate_notices"] is False


@pytest.fixture
def github_saved(home):
    trigger.save_config({"github": {"token": "github_pat_abc", "repos": "o/r, acme",
                                    "events": {"review": {"on": False},
                                               "ci_failed": {"on": True, "do": "  fix it  "}}}})
    trigger.save_config({"github": {"events": {"review": {"do": "tell me"}}}})


def test_github_events_merge_per_kind_and_per_field(github_saved):
    raw = read_config_file()
    ev = trigger.config_summary(trigger.load_config())["github"]["events"]
    assert raw["github"]["events"]["review"] == {"on": False, "do": "tell me"}
    assert (ev["ci_failed"]["on"], ev["ci_failed"]["do"], ev["ci_failed"]["action"]) == (
        True, "fix it", "claude")
    assert ev["mention"]["on"] is True
    assert raw["github"]["repos"] == ["o/r", "acme"]


def test_a_fine_grained_token_is_accepted_and_never_shown(github_saved):
    assert "github_pat_abc" not in json.dumps(trigger.config_summary(trigger.load_config()))


def test_an_unknown_key_is_not_written(home):
    trigger.save_config({"bogus": 1, "slack": {"bogus": 2}})
    raw = read_config_file()
    assert "bogus" not in raw
    assert "bogus" not in raw.get("slack", {})


# =============================================== access + refusals, no model
@pytest.fixture
def access_cfg(cfg):
    base = copy.deepcopy(cfg)
    base.pop("refuse_without_model")                       # the default: on
    base["slack"].update(allowed_users=["UFRIEND"], blocked_users=["UPEST"])
    return base


def test_not_accepted_is_refused_and_the_notice_goes_out_as_written_through_the_api(
        access_cfg, http, dispatch):
    ev = mention(access_cfg, "800.1", OTHER_ID, f"{TAG} please", channel="C8")
    res = trigger.handle(ev, access_cfg, dispatch, trigger.slack_notify(access_cfg))
    post = calls_to(http, "chat.postMessage")
    assert res["action"] == "refused", res
    assert not dispatch.ran
    assert len(post) == 1
    assert post[0]["form"]["text"].startswith("I'm not enabled")


def test_a_refusal_calls_no_model(access_cfg, http, dispatch, home):
    before = len(claude_calls(home))
    ev = mention(access_cfg, "800.1", OTHER_ID, f"{TAG} please", channel="C8")
    trigger.handle(ev, access_cfg, dispatch, trigger.slack_notify(access_cfg))
    assert len(claude_calls(home)) == before


def test_with_no_user_token_there_is_no_model_free_way_to_post_only_logged_no_session(
        access_cfg, http, dispatch, con, home):
    no_user = copy.deepcopy(access_cfg)
    no_user["slack"].pop("user_token")
    before = len(claude_calls(home))
    ev = mention(no_user, "800.2", OTHER_ID, f"{TAG} please", channel="C7")
    res = trigger.handle(ev, no_user, dispatch, trigger.slack_notify(no_user))
    assert res["action"] == "refused"
    assert len(claude_calls(home)) == before
    assert not calls_to(http, "chat.postMessage")
    assert trigger.actions_for(con, res["request_id"])[-1]["kind"] == "refusal-notice-skipped"


@pytest.fixture
def allowed_run(access_cfg, http, dispatch, con):
    ev = mention(access_cfg, "800.3", "UFRIEND", f"{TAG} hi", channel="C8")
    return trigger.handle(ev, access_cfg, dispatch)


def test_an_allowed_user_runs_in_a_conversation_nobody_opened(allowed_run, dispatch, con):
    assert allowed_run["action"] == "run"
    assert dispatch.ran == [allowed_run["request_id"]]
    assert trigger.get_request(con, allowed_run["request_id"])["reason"] == "allowed user"


def test_an_allowed_user_opens_nothing_for_anyone_else(allowed_run, con):
    assert not trigger.gate_enabled(con, "slack:C8:800.3")


def test_a_blocked_user_is_refused_even_in_an_open_channel_with_the_not_allowed_notice(
        access_cfg, http, dispatch, con):
    trigger.set_gate(con, "slack:C8", True, by="test")
    ev = mention(access_cfg, "800.4", "UPEST", f"{TAG} hi", channel="C8")
    res = trigger.handle(ev, access_cfg, dispatch, trigger.slack_notify(access_cfg))
    post = calls_to(http, "chat.postMessage")
    assert res["action"] == "refused"
    assert not dispatch.ran
    assert post
    assert post[0]["form"]["text"].startswith("You can't ask me that here")
    assert trigger.get_request(con, res["request_id"])["reason"] == "blocked"


def test_the_owner_is_never_blocked(access_cfg, http, dispatch):
    blocking = {**access_cfg, "slack": {**access_cfg["slack"], "blocked_users": [OWNER_ID]}}
    ev = mention(blocking, "800.5", OWNER_ID, f"{TAG} go", channel="C8")
    assert trigger.handle(ev, access_cfg, dispatch)["action"] == "run"


def test_the_access_lists_save_upper_cased(access_cfg):
    assert trigger.save_config({"slack": {"allowed_users": "u1abc, U2DEF"}})["ok"]
    assert trigger.load_config()["slack"]["allowed_users"] == ["U1ABC", "U2DEF"]


def test_the_access_lists_are_checked_on_save(access_cfg):
    assert not trigger.save_config({"slack": {"blocked_users": ["not a user"]}})["ok"]


# ==================================================================== cancel
@pytest.fixture
def slow_cfg(cfg, http):
    write_script("slow.py", SLOW_SCRIPT)
    config = copy.deepcopy(cfg)
    config["slack"]["commands"] = [{"name": "slow", "action": "script", "script": {"name": "slow.py"}}]
    return config


@pytest.fixture
def cancelled(slow_cfg, dispatch, con):
    ev = mention(slow_cfg, "950.1", OWNER_ID, f"{TAG} slow", channel="C9")
    rid = trigger.handle(ev, slow_cfg, dispatch)["request_id"]
    return SimpleNamespace(rid=rid, res=trigger.cancel(con, rid, by="web-ui"))


def test_a_queued_task_cancels_at_once_and_is_logged(cancelled, con):
    assert cancelled.res["ok"], cancelled.res
    assert cancelled.res["was"] == "queued"
    assert trigger.get_request(con, cancelled.rid)["status"] == "cancelled"
    assert trigger.actions_for(con, cancelled.rid)[-1]["kind"] == "cancel"


def test_a_cancelled_task_never_runs(cancelled, slow_cfg, con):
    assert trigger.run_request(cancelled.rid, slow_cfg)["ok"] is False
    assert trigger.get_request(con, cancelled.rid)["status"] == "cancelled"


def test_an_ended_task_cannot_be_cancelled(cancelled, con):
    assert not trigger.cancel(con, cancelled.rid)["ok"]


def test_a_cancelled_task_can_be_reprocessed(cancelled, con):
    assert trigger.reprocess(con, cancelled.rid)["ok"]
    assert trigger.get_request(con, cancelled.rid)["status"] == "queued"


def test_a_running_script_is_killed_within_a_poll_and_stays_cancelled(slow_cfg, dispatch, con):
    ev = mention(slow_cfg, "950.1", OWNER_ID, f"{TAG} slow", channel="C9")
    rid = trigger.handle(ev, slow_cfg, dispatch)["request_id"]
    out: dict = {}
    worker = threading.Thread(target=lambda: out.update(trigger.run_request(rid, slow_cfg)))
    worker.start()
    deadline = time.time() + 10
    while time.time() < deadline and trigger.get_request(con, rid)["status"] != "running":
        time.sleep(0.05)
    started = time.time()
    res = trigger.cancel(con, rid)
    worker.join(10)
    req = trigger.get_request(con, rid)
    assert res["ok"], res
    assert res["was"] == "running"
    assert not worker.is_alive()
    assert time.time() - started < 5
    assert out.get("status") == "cancelled", out
    assert req["status"] == "cancelled"
    assert "finished" not in (req["result"] or "")


# =================================================================== commands
@pytest.fixture
def cmd_cfg(cfg, http):
    write_script("echo.py", ECHO_SCRIPT)
    write_script("plain.py", ECHO_SCRIPT, executable=False)
    config = copy.deepcopy(cfg)
    config["slack"]["commands"] = [
        {"name": "deploy", "action": "script", "script": {"name": "echo.py"}},
        {"name": "ship", "action": "script", "anyone": True,
         "script": {"name": "plain.py", "args": ["--env={arg_1}", "{trigger_user_id}"]}},
        {"name": "ticket", "pattern": r"^ticket (?P<key>[A-Z]+-\d+)", "anyone": True,
         "action": "http", "http": {"method": "POST", "url": "https://hook.example/t/{key}?who={trigger_user}",
                                    "headers": {"X-Who": "{trigger_user}"},
                                    "body": '{"text": "{trigger_content}", "key": "{key}"}'}},
        {"name": "ask", "anyone": True, "do": "Answer {arg_1} briefly."},
        {"name": "off", "on": False, "action": "script", "script": {"name": "echo.py"}},
    ]
    return config


def test_a_command_is_its_name_then_braces_split_on_commas_and_trimmed(cmd_cfg):
    cmd, v = trigger.match_command(cmd_cfg, f"{TAG} deploy {{staging, v1.2 ,  now}}", BOT)
    assert cmd["name"] == "deploy"
    assert v["args"] == "staging, v1.2 ,  now"
    assert [v["arg_1"], v["arg_2"], v["arg_3"], v["arg_4"]] == ["staging", "v1.2", "now", ""]
    assert v["args_list"] == ["staging", "v1.2", "now"]


def test_command_case_does_not_matter(cmd_cfg):
    assert trigger.match_command(cmd_cfg, f"{TAG} DEPLOY", BOT)[0]["name"] == "deploy"


def test_a_longer_word_is_not_the_command(cmd_cfg):
    assert trigger.match_command(cmd_cfg, f"{TAG} deployment plan", BOT)[0] is None


def test_an_off_command_never_matches(cmd_cfg):
    assert trigger.match_command(cmd_cfg, f"{TAG} off {{x}}", BOT)[0] is None


def test_a_patterns_named_groups_are_variables(cmd_cfg):
    cmd, v = trigger.match_command(cmd_cfg, f"{TAG} ticket PROJ-12 please", BOT)
    assert cmd["name"] == "ticket"
    assert v["key"] == "PROJ-12"


@pytest.fixture
def deploy_run(cmd_cfg, dispatch, con):
    ev = mention(cmd_cfg, "900.1", OWNER_ID, f"{TAG} deploy {{staging; rm -rf ~, $(id)}}", channel="C5")
    res = trigger.handle(ev, cmd_cfg, dispatch)
    out = trigger.run_request(res["request_id"], cmd_cfg)
    req = trigger.get_request(con, res["request_id"])
    return SimpleNamespace(ev=ev, rid=res["request_id"], out=out, request=req, got=json.loads(req["result"]))


def test_a_command_is_its_own_kind_carries_its_action_and_opens_nothing(deploy_run):
    meta = deploy_run.ev["meta"]
    assert meta["kind"] == "command"
    assert meta["command"] == "deploy"
    assert meta["action"]["type"] == "script"
    assert deploy_run.ev["opens"] is False


def test_the_owners_command_runs_the_script_with_no_session(deploy_run, con):
    assert deploy_run.out["status"] == "done", deploy_run.out
    assert not trigger.gate_enabled(con, "slack:C5:900.1")


def test_command_arguments_arrive_as_argv_items_never_through_a_shell(deploy_run):
    assert deploy_run.got["argv"] == ["staging; rm -rf ~", "$(id)"], deploy_run.got
    assert deploy_run.got["args"] == deploy_run.got["argv"]


def test_the_script_gets_every_variable_in_the_environment_and_the_request_id(deploy_run):
    assert deploy_run.got["user"] == f"Name {OWNER_ID}"
    assert deploy_run.got["command"] == "deploy"
    assert deploy_run.got["rid"] == deploy_run.rid


def test_the_run_is_logged_as_an_action(deploy_run, con):
    assert [a["kind"] for a in trigger.actions_for(con, deploy_run.rid)] == ["script"]


def test_someone_else_may_not_run_an_owner_only_command_even_in_an_open_channel(cmd_cfg, dispatch, con):
    trigger.set_gate(con, "slack:C5", True, by="test")
    ev = mention(cmd_cfg, "900.2", OTHER_ID, f"{TAG} deploy {{prod}}", channel="C5")
    res = trigger.handle(ev, cmd_cfg, dispatch, trigger.slack_notify(cmd_cfg))
    assert res["action"] == "refused"
    assert trigger.get_request(con, res["request_id"])["reason"] == "owner-only command"


@pytest.fixture
def ship_run(cmd_cfg, dispatch, con):
    trigger.set_gate(con, "slack:C5", True, by="test")
    ev = mention(cmd_cfg, "900.3", OTHER_ID, f"{TAG} ship {{prod}}", channel="C5")
    res = trigger.handle(ev, cmd_cfg, dispatch)
    out = trigger.run_request(res["request_id"], cmd_cfg)
    return SimpleNamespace(out=out, got=json.loads(trigger.get_request(con, res["request_id"])["result"]))


def test_an_anyone_command_runs_for_someone_the_gate_lets_in_and_a_file_that_is_not_executable_runs_through_its_interpreter(
        ship_run):
    assert ship_run.out["status"] == "done", ship_run.out


def test_a_commands_own_arguments_are_templates_one_argv_item_each(ship_run):
    assert ship_run.got["argv"] == ["--env=prod", OTHER_ID], ship_run.got


@pytest.fixture
def http_command(cmd_cfg, dispatch, monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(
        trigger, "http_raw",
        lambda method, url, headers=None, data=None, timeout=30: (
            sent.append({"method": method, "url": url, "headers": headers, "data": data}) or (201, "ok")))
    ev = mention(cmd_cfg, "900.4", OWNER_ID, f'{TAG} ticket PROJ-9 "quoted" & more', channel="C5")
    res = trigger.handle(ev, cmd_cfg, dispatch)
    return SimpleNamespace(sent=sent, out=trigger.run_request(res["request_id"], cmd_cfg))


def test_an_http_command_calls_the_api(http_command):
    assert http_command.out["status"] == "done", http_command.out
    assert http_command.sent[-1]["method"] == "POST"


def test_http_command_values_are_percent_encoded_in_the_url(http_command):
    assert http_command.sent[-1]["url"] == "https://hook.example/t/PROJ-9?who=Name%20UOWNER"


def test_http_command_values_are_json_escaped_in_a_json_body_so_a_quote_cannot_end_the_string(http_command):
    body = json.loads(http_command.sent[-1]["data"].decode(UTF8))
    assert body == {"text": f'{TAG} ticket PROJ-9 "quoted" & more', "key": "PROJ-9"}
    assert http_command.sent[-1]["headers"]["Content-Type"].startswith("application/json")


def test_an_http_500_is_a_failure(cmd_cfg, dispatch, monkeypatch):
    monkeypatch.setattr(trigger, "http_raw",
                        lambda method, url, headers=None, data=None, timeout=30: (500, "boom"))
    ev = mention(cmd_cfg, "900.5", OWNER_ID, f"{TAG} ticket PROJ-10", channel="C5")
    out = trigger.run_request(trigger.handle(ev, cmd_cfg, dispatch)["request_id"], cmd_cfg)
    assert out["status"] == "failed"


def test_a_claude_commands_run_command_fills_in_its_arguments_and_the_session_is_told(
        cmd_cfg, dispatch, con):
    ev = mention(cmd_cfg, "900.6", OWNER_ID, f"{TAG} ask {{the rota}}", channel="C5")
    req = trigger.get_request(con, trigger.handle(ev, cmd_cfg, dispatch)["request_id"])
    prompt = trigger.build_prompt(req, cmd_cfg)
    assert "Answer the rota briefly." in prompt
    assert "command: ask" in prompt
    assert 'command_args: ["the rota"]' in prompt
    assert "slack_event: command" in prompt


@pytest.mark.parametrize("patch, needle", [
    ({"slack": {"commands": [{"name": "bad name"}]}}, "is not a name"),
    ({"slack": {"commands": [{"name": "a"}, {"name": "A"}]}}, "two commands"),
    ({"slack": {"commands": [{"name": "a", "pattern": "("}]}}, "not a regular expression"),
    ({"slack": {"commands": [{"name": "a", "pattern": "(?P<owner>x)"}]}}, "already a variable"),
    ({"slack": {"commands": [{"name": "a", "do": "{nope}"}]}}, "{nope}"),
    ({"slack": {"commands": [{"name": "a", "action": "script",
                              "script": {"name": "missing.sh"}}]}}, "no script missing.sh"),
    ({"slack": {"commands": [{"name": "a", "action": "http"}]}}, "needs a URL"),
    ({"slack": {"commands": [{"name": "a", "action": "http",
                              "http": {"url": "{trigger_link}"}}]}}, "must start with http"),
    ({"slack": {"commands": [{"name": "a", "action": "http",
                              "http": {"url": "https://x", "headers": {"X": "a\nb"}}}]}}, "one line"),
    ({"slack": {"commands": [{"name": "a", "action": "script",
                              "script": {"name": "../x"}}]}}, "bare filename"),
    ({"jira": {"events": {"transitioned": {"on": True, "action": "shell"}}}}, "must be one of"),
], ids=lambda value: value if isinstance(value, str) else "")
def test_a_bad_command_save_is_refused(cfg, patch, needle):
    res = trigger.save_config(patch)
    assert res["ok"] is False, res
    assert needle in res["error"], res


def test_a_patterns_groups_may_be_used_in_its_own_run_command(cfg):
    res = trigger.save_config({"slack": {"commands": [
        {"name": "t", "pattern": r"^t (?P<key>\w+)", "do": "look at {key} for {trigger_user}"}]}})
    assert res["ok"], str(res)[:200]
    assert res["config"]["slack"]["commands"][0]["do"] == "look at {key} for {trigger_user}"


# ================================================================ slack member
@pytest.fixture
def member_cfg(cfg, http):
    http.on("POST", trigger.SLACK_API + "conversations.info",
            lambda form, **_: {"ok": True, "channel": {"name": "ops"}})
    config = copy.deepcopy(cfg)
    config["slack"]["events"] = {"joined_channel": {"on": True},
                                 "left_channel": {"on": True, "action": "script",
                                                  "script": {"name": "echo.py"}}}
    return config


JOINED = {"type": "member_joined_channel", "user": BOT, "channel": "C77",
          "inviter": OWNER_ID, "event_ts": "1700000100.000001"}


def test_a_channel_member_event_switched_off_is_nothing(cfg, http):
    assert trigger.slack_member_event(JOINED, copy.deepcopy(cfg), bot_user_id=BOT) is None


def test_another_member_joining_is_nothing(member_cfg):
    assert trigger.slack_member_event({**JOINED, "user": "USOMEONE"}, member_cfg, bot_user_id=BOT) is None


@pytest.fixture
def bot_added(member_cfg, dispatch, con):
    ev = trigger.slack_member_event(JOINED, member_cfg, bot_user_id=BOT)
    res = trigger.handle(ev, member_cfg, dispatch)
    req = trigger.get_request(con, res["request_id"])
    return SimpleNamespace(res=res, request=req, variables=trigger.request_variables(req, member_cfg))


def test_the_bot_added_is_a_subscribed_run_with_who_and_where(bot_added):
    v = bot_added.variables
    assert bot_added.res["action"] == "run"
    assert bot_added.request["reason"] == "subscribed: joined_channel"
    assert v["trigger_kind"] == "joined_channel"
    assert v["slack_channel_name"] == "ops"
    assert v["trigger_user"] == f"Name {OWNER_ID}"
    assert v["trigger_user_is_owner"] == "true"


def test_even_when_the_owner_added_the_bot_it_opens_nothing(bot_added, con):
    assert not trigger.gate_enabled(con, "slack:C77")


def test_the_session_is_told_it_is_not_answering_anyone(bot_added, member_cfg):
    prompt = trigger.build_prompt(bot_added.request, member_cfg)
    assert "slack_event: joined_channel" in prompt
    assert "none by default" in prompt
    assert "added me to #ops" in prompt


def test_the_bot_removed_runs_its_script(member_cfg, dispatch):
    write_script("echo.py", ECHO_SCRIPT)
    ev = trigger.slack_member_event({"type": "member_left_channel", "user": BOT, "channel": "C77",
                                     "event_ts": "1700000200.1"}, member_cfg, bot_user_id=BOT)
    res = trigger.handle(ev, member_cfg, dispatch)
    out = trigger.run_request(res["request_id"], member_cfg)
    assert ev["meta"]["kind"] == "left_channel"
    assert out["status"] == "done", out


# =============================================================== jira changes
@pytest.fixture
def jira_changes(cfg, http, dispatch, con):
    """Watched issue OPS-3 with a transition, an assignment, an unassignment,
    one change older than the window, and a colleague's and the owner's comments."""
    jcfg = copy.deepcopy(cfg)
    jcfg["jira"]["events"] = {k: {"on": True} for k in trigger.JIRA_CHANGE_KINDS}
    jira = trigger.Jira(jcfg)
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    trigger.set_checkpoint(con, "jira", now.isoformat(), only_forward=False)
    issue = {"key": "OPS-3", "fields": {"summary": "Rotate keys"}, "changelog": {"histories": [
        {"id": "70", "created": jira_ts(now, 4), "author": ACCOUNT_OWNER, "items": [
            {"field": "status", "fromString": "To Do", "toString": "In Progress"},
            {"field": "assignee", "from": None, "fromString": None, "to": JIRA_OWNER,
             "toString": "Owner"}]},
        {"id": "71", "created": jira_ts(now, 6), "author": ACCOUNT_X, "items": [
            {"field": "assignee", "from": JIRA_OWNER, "fromString": "Owner", "to": None,
             "toString": None}]},
        {"id": "60", "created": jira_ts(now, -900), "author": ACCOUNT_X, "items": [
            {"field": "status", "fromString": "A", "toString": "B"}]}]}}
    searched: list[dict] = []

    def search(body, **_):
        searched.append(body)
        if "currentUser()" in body["jql"]:
            return {"issues": [issue], "isLast": True}
        return {"issues": [], "isLast": True}
    http.on("POST", JIRA_BASE + "/rest/api/3/search/jql", search)
    comment_route(http, "OPS-3", {"comments": [
        {"id": "31", "created": jira_ts(now, 7), "author": ACCOUNT_X,
         "body": adf("can you rotate the staging one too?")},
        {"id": "30", "created": jira_ts(now, 5), "author": ACCOUNT_OWNER, "body": adf("on it")}]})
    trigger.jira_poll(jcfg, dispatch, now=now + timedelta(minutes=10), jira=jira)
    requests = [trigger.get_request(con, r) for r in dispatch.ran]
    return SimpleNamespace(cfg=jcfg, jira=jira, now=now, searched=searched, requests=requests,
                           kinds=[r["meta"]["kind"] for r in requests],
                           variables={r["meta"]["kind"]: trigger.request_variables(r, jcfg) for r in requests})


def test_changes_are_read_on_the_watched_issues_with_the_changelog(jira_changes):
    watch = [b for b in jira_changes.searched if "currentUser()" in b["jql"]]
    assert watch, jira_changes.searched
    assert watch[-1].get("expand") == "changelog"
    assert "AND (assignee = currentUser()" in watch[-1]["jql"]


def test_a_transition_an_assignment_an_unassignment_and_a_colleagues_comment_arrive_oldest_first(jira_changes):
    assert jira_changes.kinds == ["transitioned", "assigned", "unassigned", "comment"]


def test_the_transitions_from_and_to(jira_changes):
    v = jira_changes.variables["transitioned"]
    assert v["jira_from"] == "To Do"
    assert v["jira_to"] == "In Progress"
    assert v["trigger_user_is_owner"] == "true"


def test_whom_it_was_assigned_to_and_who_had_it(jira_changes):
    assigned, unassigned = jira_changes.variables["assigned"], jira_changes.variables["unassigned"]
    assert assigned["jira_assignee"] == "Owner"
    assert assigned["jira_assignee_id"] == JIRA_OWNER
    assert unassigned["jira_previous_assignee"] == "Owner"
    assert unassigned["jira_assignee"] == ""


def test_the_owners_own_plain_comment_is_conversation_not_a_trigger(jira_changes):
    assert all(r["event_id"] != "OPS-3:comment:30" for r in jira_changes.requests)


def test_a_change_the_owner_made_counts_and_none_of_it_opens_the_issue(jira_changes, con):
    assert jira_changes.requests[0]["reason"] == "subscribed: transitioned"
    assert not trigger.gate_enabled(con, "jira:OPS-3")


def test_a_change_older_than_the_window_is_not_read(jira_changes):
    assert all(":h60:" not in r["event_id"] for r in jira_changes.requests)


def test_the_session_gets_the_event_and_its_run_command_and_posts_nothing_by_default(jira_changes):
    prompt = trigger.build_prompt(jira_changes.requests[0], jira_changes.cfg)
    assert "jira_event: transitioned" in prompt
    assert "moved from To Do to In Progress" in prompt
    assert "post on Jira only if it says to" in prompt


def test_an_overlapping_jira_poll_runs_nothing_twice(jira_changes):
    again: list[str] = []
    trigger.jira_poll(jira_changes.cfg, again.append, now=jira_changes.now + timedelta(minutes=11),
                      jira=jira_changes.jira)
    assert not again


# ==================================================================== slack DMs
DM_CHANNEL = "D1"
DM_SCOPE = f"slack:{DM_CHANNEL}"


def dm(ts, user, text, thread=None):
    m = {"type": "message", "channel_type": "im", "channel": DM_CHANNEL, "ts": ts, "user": user,
         "text": text}
    if thread:
        m["thread_ts"] = thread
    return m


def dm_event(cfg, ts, user, text, channel=DM_CHANNEL):
    return trigger.slack_event(dm(ts, user, text), channel, cfg, bot_user_id=BOT)


@pytest.fixture
def dm_cfg(cfg, http):
    config = copy.deepcopy(cfg)
    config.pop("refuse_without_model")
    config["slack"]["dms"] = True
    return config


def test_a_tag_with_a_label_as_slack_writes_it_outside_the_bots_channels_is_a_tag():
    assert trigger.mentions_bot("<@UBOT|brain> test", BOT)
    assert not trigger.mentions_bot("<@UBOTX> test", BOT)


def test_a_dm_is_one_conversation_the_whole_dm(dm_cfg):
    ev = dm_event(dm_cfg, "950.1", "UANA", "<@UBOT|brain> what is on my plate?")
    assert ev["scope"] == "slack:D1"
    assert ev["parent_scope"] == ""
    assert ev["meta"]["channel_type"] == "im"


def test_someone_elses_tag_in_a_dm_nobody_opened_is_refused_with_the_notice_in_the_dm_not_a_thread(
        dm_cfg, http, dispatch):
    ev = dm_event(dm_cfg, "950.1", "UANA", "<@UBOT|brain> what is on my plate?")
    res = trigger.handle(ev, dm_cfg, dispatch, trigger.slack_notify(dm_cfg))
    post = calls_to(http, "chat.postMessage")
    assert res["action"] == "refused"
    assert post
    assert post[-1]["form"]["channel"] == DM_CHANNEL
    assert "thread_ts" not in post[-1]["form"]


def test_a_dms_link_was_asked_for_with_the_user_token_because_the_bot_is_not_in_the_dm(dm_cfg, http):
    dm_event(dm_cfg, "950.1", "UANA", "<@UBOT|brain> what is on my plate?")
    assert calls_to(http, "chat.getPermalink")[-1]["form"]["channel"] == DM_CHANNEL


@pytest.fixture
def dm_opened(dm_cfg, dispatch, con):
    own = dm_event(dm_cfg, "950.2", OWNER_ID, f"{TAG} help Ana with this")
    owner_res = trigger.handle(own, dm_cfg, dispatch)
    again = dm_event(dm_cfg, "950.3", "UANA", f"{TAG} and the rota?")
    return SimpleNamespace(owner_res=owner_res, res=trigger.handle(again, dm_cfg, dispatch))


def test_the_owners_tag_in_a_dm_runs_and_opens_that_dm(dm_opened, con):
    assert dm_opened.owner_res["action"] == "run"
    assert trigger.gate_enabled(con, DM_SCOPE)


def test_then_the_other_persons_next_tag_not_in_any_thread_runs(dm_opened):
    assert dm_opened.res["action"] == "run"


def test_the_session_replies_in_the_dm_as_a_new_message_not_a_thread(dm_opened, dm_cfg, con):
    prompt = trigger.build_prompt(trigger.get_request(con, dm_opened.res["request_id"]), dm_cfg)
    assert "reply in that direct message, as the owner, as a new message" in prompt


@pytest.fixture
def dms_open_cfg(dm_cfg):
    config = copy.deepcopy(dm_cfg)
    config["slack"]["dms_open"] = True
    return config


def test_with_dms_open_anyone_in_a_dm_with_the_owner_gets_in(dms_open_cfg, dispatch, con):
    ev = dm_event(dms_open_cfg, "950.4", "UBO", f"{TAG} hi", channel="D2")
    res = trigger.handle(ev, dms_open_cfg, dispatch)
    assert res["action"] == "run"
    assert trigger.get_request(con, res["request_id"])["reason"] == "direct message"
    assert not trigger.gate_enabled(con, "slack:D2")


def test_dms_open_does_not_let_in_someone_blocked(dms_open_cfg, dispatch):
    blocked = copy.deepcopy(dms_open_cfg)
    blocked["slack"]["blocked_users"] = ["UBO"]
    ev = dm_event(blocked, "950.5", "UBO", f"{TAG} hi", channel="D2")
    assert trigger.handle(ev, blocked, dispatch)["action"] == "refused"


def test_a_dm_without_the_tag_is_nothing(dms_open_cfg):
    assert dm_event(dms_open_cfg, "950.6", "UBO", "hello", channel="D2") is None


def test_dms_without_a_user_token_is_a_problem_the_check_names(dm_cfg):
    no_user = copy.deepcopy(dm_cfg)
    no_user["slack"].pop("user_token")
    chk = trigger.check(trigger._merge(trigger.DEFAULTS, no_user), offline=True, only="slack")
    assert any("slack.dms is on but there is no user_token" in x for x in chk["problems"]), chk


# ===================================================================== tasks
@pytest.fixture
def claude_config_dir(home, monkeypatch) -> Path:
    projects = home / "claude-config" / "projects"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(projects.parent))
    return projects


@pytest.fixture
def task(cfg, http, dispatch, con, home, claude_config_dir):
    """One request run through the fake claude, with the call it made."""
    ev = mention(cfg, "950.1", OWNER_ID, f"{TAG} watch me run")
    rid = trigger.handle(ev, cfg, dispatch)["request_id"]
    trigger.run_request(rid, cfg)
    req = trigger.get_request(con, rid)
    return SimpleNamespace(rid=rid, request=req, args=claude_calls(home)[-1]["args"],
                           projects=claude_config_dir, cfg=cfg,
                           folder=claude_config_dir / trigger._project_slug(req["cwd"]))


TRANSCRIPT = [
    {"type": "user", "timestamp": "t0", "message": {"content": "You were summoned"}},
    {"type": "assistant", "timestamp": "t1", "message": {"content": [
        {"type": "text", "text": "Reading it."},
        {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "ls"}}]}},
    {"type": "user", "timestamp": "t2", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "tu1", "content": "a.txt", "is_error": False}]}},
    {"type": "assistant", "timestamp": "t3", "isSidechain": True, "message": {"content": [
        {"type": "text", "text": "a subagent, not this session"}]}},
    {"type": "assistant", "timestamp": "t4", "message": {"content": [
        {"type": "tool_use", "id": "tu2", "name": "Read", "input": {"file": "x"}}]}},
]


@pytest.fixture
def transcribed(task):
    """The session's transcript, where Claude Code writes it."""
    task.folder.mkdir(parents=True, exist_ok=True)
    (task.folder / f"{task.request['session_id']}.jsonl").write_text(
        "\n".join(json.dumps(x) for x in TRANSCRIPT) + "\nnot json\n", encoding=UTF8)
    return task


def test_the_session_id_is_chosen_up_front_and_passed_to_claude(task):
    assert task.request["session_id"]
    assert task.args[task.args.index("--session-id") + 1] == task.request["session_id"]


def test_the_command_and_working_directory_are_recorded(task):
    assert json.loads(task.request["command"])[1:] == task.args
    assert Path(task.request["cwd"]).is_dir()


def test_the_detail_carries_the_command_the_whole_message_and_the_actions(transcribed, cfg):
    d = trigger.task_detail(transcribed.rid)
    assert d["ok"]
    assert d["task"]["command"][0] == cfg["claude"]["bin"]
    assert d["task"]["text"].endswith("watch me run")
    assert isinstance(d["actions"], list)


def test_the_transcript_is_read_prompt_what_it_said_tool_calls_with_results(transcribed):
    t = trigger.task_detail(transcribed.rid)["transcript"]
    assert t["found"]
    assert t["tool_calls"] == 2
    assert [e["kind"] for e in t["events"]] == ["text", "text", "tool", "tool"]
    assert t["events"][0]["role"] == "prompt"
    assert t["events"][2]["result"] == "a.txt"
    assert t["events"][3]["result"] is None


def test_a_subagents_lines_and_a_broken_line_are_skipped(transcribed):
    t = trigger.task_detail(transcribed.rid)["transcript"]
    assert not any("subagent" in (e.get("text") or "") for e in t["events"])


@pytest.fixture
def running_task(transcribed, con):
    trigger._set_request(con, transcribed.rid, status="running")
    payload = trigger.tasks_payload(50)
    transcribed.payload = payload
    transcribed.row = next(x for x in payload["tasks"] if x["id"] == transcribed.rid)
    return transcribed


def test_the_task_list_names_each_task_and_counts_by_status(running_task):
    assert running_task.payload["ok"]
    assert running_task.row["name"] == "mention"
    assert running_task.row["source"] == "slack"
    assert running_task.payload["counts"].get("running", 0) >= 1
    assert "command" not in running_task.row


def test_a_running_task_carries_its_progress_tool_calls_and_the_latest_one(running_task):
    progress = running_task.row["progress"]
    assert progress["tool_calls"] == 2
    assert progress["last_tool"] == "Read"
    assert '"file"' in progress["last_input"]


@pytest.fixture
def live_session(running_task, con):
    """What the brain's hooks write while the session runs."""
    g = trigger.graph
    sid = running_task.request["session_id"]
    conv = g.api_conversation(con, sid)["node"]["id"]
    main_ = g.api_agent_spawn(con, conv, "main", session_id=sid)["node"]["id"]
    running_task.sub = g.api_agent_spawn(con, main_, "pr-reviewer", body="review it",
                                         session_id=sid)["node"]["id"]
    g.api_create_node(con, "capture", "answered", "Done, replied in thread.",
                      {"role": "assistant", "session_id": sid, "captured_at": "2099-01-01T00:00:00+00:00"})
    trigger.log_action(con, running_task.rid, "reply", "https://slack.example/r", "answered")
    running_task.activity = trigger.task_detail(running_task.rid)["activity"]
    return running_task


def test_the_detail_carries_the_sessions_live_activity_from_the_database(live_session):
    act = live_session.activity
    assert act["conversation"]["status"] == "open"
    assert len(act["agents"]) == 2
    assert act["subagents_running"] == 1
    assert act["actions"] == 1
    assert act["last_message"] == "Done, replied in thread."


def test_its_timeline_has_the_session_the_subagent_the_action_and_the_answer_in_order(live_session):
    kinds = [(e["kind"], e["title"]) for e in live_session.activity["timeline"]]
    assert kinds[0] == ("session", "session started")
    assert ("agent", "subagent pr-reviewer started") in kinds
    assert ("action", "reply") in kinds
    assert kinds[-1] == ("message", "answered")


def test_the_cards_progress_follows_the_database_as_the_hooks_write(live_session, con):
    trigger.graph.api_update_node(con, live_session.sub, status="done",
                                  props_patch={"outcome": "completed"})
    row = next(x for x in trigger.tasks_payload(50)["tasks"] if x["id"] == live_session.rid)
    assert row["progress"]["subagents"] == 1
    assert row["progress"]["subagents_running"] == 0
    assert row["progress"]["last_action"]["kind"] == "reply"


@pytest.fixture
def legacy_task(transcribed, cfg, http, dispatch, con):
    """A request from before session ids were kept, its transcript named by something else."""
    trigger._set_request(con, transcribed.rid, status="done")
    old = trigger.handle(mention(cfg, "950.2", OWNER_ID, f"{TAG} old"), cfg, dispatch)["request_id"]
    trigger._set_request(con, old, status="done", started_at=trigger.now_iso())
    legacy = transcribed.projects / trigger._project_slug(trigger.session_cwd(trigger.load_config()))
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "legacy-session.jsonl").write_text(json.dumps(
        {"type": "user", "timestamp": "t0",
         "message": {"content": f"request_id: {old}\nsource: slack"}}) + "\n", encoding=UTF8)
    return old


def test_an_older_tasks_transcript_is_found_by_its_request_id_and_remembered(legacy_task, con):
    assert trigger.task_detail(legacy_task)["transcript"]["found"]
    assert trigger.get_request(con, legacy_task)["session_id"] == "legacy-session"


def test_an_unknown_task_is_refused(home):
    assert trigger.task_detail("nope")["ok"] is False


@pytest.fixture
def priced_transcript(task):
    """opus 5: 1k in $0.005 + 1k out $0.025 + 1M read $0.50 + 1k 1h-write $0.01; haiku 1M in $1."""
    usage = {"input_tokens": 1000, "output_tokens": 1000, "cache_read_input_tokens": 1_000_000,
             "cache_creation_input_tokens": 1000,
             "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 1000}}
    message = {"type": "assistant", "message": {"id": "msg1", "model": "claude-opus-5", "usage": usage}}
    task.folder.mkdir(parents=True, exist_ok=True)
    path = task.folder / "priced.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in (message, message)) + "\n",
                    encoding=UTF8)                      # one message, two blocks
    (task.folder / "priced" / "subagents").mkdir(parents=True, exist_ok=True)
    (task.folder / "priced" / "subagents" / "agent-1.jsonl").write_text(json.dumps(
        {"type": "assistant", "message": {"id": "msg2", "model": "claude-haiku-4-5",
                                          "usage": {"input_tokens": 1_000_000}}}) + "\n", encoding=UTF8)
    task.priced_path, task.message = path, message
    return task


def test_a_sessions_cost_is_estimated_from_its_token_usage_and_its_subagents_each_message_once(
        priced_transcript):
    assert trigger.estimate_cost(priced_transcript.priced_path) == pytest.approx(1.54, abs=1e-9)


def test_no_transcript_means_no_estimate(priced_transcript):
    assert trigger.estimate_cost(priced_transcript.folder / "missing.jsonl") is None


def test_a_timed_out_task_without_a_cost_gets_one_estimated_and_it_is_kept(priced_transcript, con):
    task = priced_transcript
    with (task.folder / f"{task.request['session_id']}.jsonl").open("a", encoding=UTF8) as f:
        f.write(json.dumps(task.message) + "\n")
    trigger._set_request(con, task.rid, status="timeout", cost_usd=None, cost_estimated=0)
    row = next(x for x in trigger.tasks_payload(50)["tasks"] if x["id"] == task.rid)
    assert row["cost_estimated"] is True
    assert row["cost_usd"] is not None
    assert trigger.get_request(con, task.rid)["cost_estimated"] == 1


# ============================================================== wake phrases
PHRASE_BRAIN = "Jarvis"
MEETING_LINK = "https://docs.example/d/1"
MEETING_TITLE = "Acme sync"
MEETING_TRANSCRIPT = (
    "Ana Lima: can someone send the deck to Acme?\n"
    "Owner Doe: Jarvis, will handle this — send them the v2 deck.\n"
    "Ana Lima: jarvis take note, the review moved to Friday\n"
    "Bo Chen: Jarvis will handle the invoices\n"
    "Owner Doe: jarvis never mind\n")


@pytest.fixture
def jarvis(monkeypatch):
    monkeypatch.setenv("MEMORY_LEDGER_BRAIN_NAME", PHRASE_BRAIN)


@pytest.fixture
def meeting_cfg(cfg, jarvis):
    config = copy.deepcopy(cfg)
    config["meeting"] = {"enabled": True, "phrases": [
        {"phrase": "{brain} will handle"},
        {"phrase": "{brain} take note", "anyone": True,
         "do": "Note it: {phrase_rest} (from {phrase_speaker} in {meeting_title})"},
        {"phrase": "{brain} never mind", "on": False},
    ]}
    return config


def test_the_phrase_words_are_said_with_the_brains_name(jarvis):
    assert trigger.phrase_words("{brain} will handle") == ["jarvis", "will", "handle"]


def test_case_accents_and_punctuation_between_the_words_do_not_matter(jarvis):
    words = ["jarvis", "will", "handle"]
    assert trigger.find_phrase(words, "OK — JÁRVIS, will handle this.") is not None


def test_a_longer_word_is_not_the_phrase(jarvis):
    assert trigger.find_phrase(["jarvis", "will", "handle"], "jarvis will handled it") is None


def test_a_transcript_is_speakers_turns_and_a_bare_line_carries_on_the_last_one(jarvis):
    turns = trigger.utterances("[00:01] Ana Lima: can someone send the deck?\n"
                               "Owner Doe: Jarvis will handle this,\n"
                               "send them the v2.\nsee https://x.example: here")
    assert turns[0] == ("Ana Lima", "can someone send the deck?")
    assert turns[1] == ("Owner Doe", "Jarvis will handle this,")
    assert turns[2] == ("Owner Doe", "send them the v2.")
    assert turns[3][0] == "Owner Doe", turns


def test_a_gemini_export_skips_notes_above_the_transcript_and_a_heading_ends_a_turn(jarvis):
    gemini = trigger.utterances(
        "### **Details**\n"
        "**Integration Concepts and Next Steps**: Owner Doe said Jarvis will handle it.\n"
        "-----\n"
        "# **Transcript**\n"
        "### **00:28:37**\n"
        "**Owner Doe:** the wizard, I'll let Jarvis will handle this and she\n"
        "**Ana Lima**: Mhm.\n"
        "will take the requirements.\n"
        "### **00:30:10**\n"
        "orphan line after a timestamp heading\n")
    assert gemini == [("Owner Doe", "the wizard, I'll let Jarvis will handle this and she"),
                      ("Ana Lima", "Mhm."), ("Ana Lima", "will take the requirements."),
                      ("", "orphan line after a timestamp heading")]


def test_two_phrases_fitting_one_line_are_one_ask_the_most_specific_takes_it(cfg, jarvis):
    overlap = copy.deepcopy(cfg)
    overlap["meeting"] = {"enabled": True, "phrases": [
        {"phrase": "{brain} handle this"}, {"phrase": "I'll let {brain} handle this"}]}
    one = trigger.match_phrases(overlap, "Owner Doe: so I'll let Jarvis handle this one")
    assert len(one) == 1
    assert one[0]["phrase"]["phrase"] == "I'll let {brain} handle this"


def test_the_owner_in_full_or_by_first_name_is_the_owner_someone_else_is_not(home):
    assert trigger.is_owner_name("Owner Doe")
    assert trigger.is_owner_name("owner")
    assert not trigger.is_owner_name("Ana Lima")
    assert not trigger.is_owner_name("")
    assert trigger.is_owner_name("Alex", aliases=["Alex"])


def test_switched_off_a_scan_starts_nothing(meeting_cfg):
    off = copy.deepcopy(meeting_cfg)
    off["meeting"]["enabled"] = False
    assert not trigger.scan(off, MEETING_TRANSCRIPT)["ok"]


@pytest.fixture
def scanned(meeting_cfg, http, dispatch, con):
    res = trigger.scan(meeting_cfg, MEETING_TRANSCRIPT, title=MEETING_TITLE, link=MEETING_LINK,
                       dispatch=dispatch)
    return SimpleNamespace(res=res, cfg=meeting_cfg)


def test_the_owners_phrase_runs_anyones_runs_for_an_anyone_phrase_a_strangers_is_refused(scanned, dispatch):
    res = scanned.res
    status = {m["speaker"]: m["status"] for m in res["matches"]}
    assert res["ok"], res
    assert res["matched"] == 3
    assert status == {"Owner Doe": "queued", "Ana Lima": "queued", "Bo Chen": "refused"}
    assert dispatch.ran == res["queued"]
    assert len(res["refused"]) == 1


def test_a_switched_off_phrase_never_matches(scanned):
    assert not any("never mind" in m["said"] for m in scanned.res["matches"])


def test_the_same_transcript_again_runs_nothing_twice(scanned, dispatch):
    again = trigger.scan(scanned.cfg, MEETING_TRANSCRIPT, title=MEETING_TITLE, link=MEETING_LINK,
                         dispatch=dispatch)
    assert again["seen"] == 3
    assert not again["queued"]
    assert len(dispatch.ran) == 2


def test_a_wake_request_is_logged_before_it_runs_source_meeting_the_line_said_and_who(scanned, con):
    req = trigger.get_request(con, scanned.res["queued"][0])
    assert req["source"] == "meeting"
    assert req["is_owner"]
    assert req["requester_name"] == "Owner Doe"
    assert req["text"].startswith("Jarvis, will handle this")
    assert req["permalink"] == MEETING_LINK
    assert req["status"] == "queued"


def test_the_session_hears_the_phrase_the_lines_around_it_its_own_name_and_a_default_task(scanned, con):
    req = trigger.get_request(con, scanned.res["queued"][0])
    prompt = trigger.build_prompt(req, scanned.cfg)
    assert "source: meeting" in prompt, prompt[:600]
    assert "wake_phrase: jarvis will handle" in prompt
    assert "Ana Lima: can someone send the deck to Acme?" in prompt
    assert "you_are: Jarvis, Owner's second brain" in prompt
    assert "summoned by a wake phrase" in prompt
    assert "what they put on Jarvis, and do it now" in prompt


def test_a_phrases_own_run_command_gets_its_variables(scanned, con):
    note = trigger.get_request(con, scanned.res["queued"][1])
    rendered = trigger.render(trigger.run_command(note, scanned.cfg),
                              trigger.request_variables(note, scanned.cfg))
    assert rendered == "Note it: the review moved to Friday (from Ana Lima in Acme sync)"


def test_brain_is_a_variable_everywhere(scanned, con):
    req = trigger.get_request(con, scanned.res["queued"][0])
    assert trigger.request_variables(req, scanned.cfg)["brain"] == PHRASE_BRAIN


def test_a_wake_request_runs_like_any_other_request(scanned, con):
    done = trigger.run_request(scanned.res["queued"][0], scanned.cfg)
    assert done.get("ok"), str(done)[:300]
    assert trigger.get_request(con, scanned.res["queued"][0])["status"] == "done"


def test_the_signature_can_carry_the_brains_name(jarvis, home):
    assert trigger.signature({"signature": "_{brain}, {owner}'s second brain_"}) == (
        "_Jarvis, Owner's second brain_")


# ============================================================== save phrases
@pytest.mark.parametrize("phrases, why", [
    ([{"phrase": "{brain}"}], "two words"),
    ([{"phrase": "  "}], "empty"),
    ([{"phrase": "{brain} go {x}"}], "only use {brain}"),
    ([{"phrase": "a b"}, {"phrase": "A  B"}], "twice"),
    ([{"phrase": "a b", "do": "{nope}"}], "no variable"),
    ([{"phrase": "a b", "action": "http"}], "needs a URL"),
    ({"phrase": "a b"}, "must be a list"),
], ids=lambda value: value if isinstance(value, str) else "")
def test_a_bad_wake_phrase_is_refused(home, phrases, why):
    res = trigger.save_config({"meeting": {"phrases": phrases}})
    assert not res["ok"], res
    assert why in res["error"], res


def test_a_good_wake_phrase_is_kept_its_spaces_tidied(home):
    res = trigger.save_config({"meeting": {"enabled": True, "phrases": [
        {"phrase": "  {brain}   will handle ", "anyone": False, "do": "Do {phrase_rest}"}]}})
    assert res["ok"], str(res)[:300]
    assert res["config"]["meeting"]["phrases"][0]["phrase"] == "{brain} will handle"
    assert res["config"]["meeting"]["enabled"] is True


def test_http_raw_invalid_url_is_a_trigger_error():
    with pytest.raises(trigger.TriggerError):
        trigger.http_raw("GET", "http://127.0.0.1/a\x00b", timeout=3)


# ============================================================ agent connector
GEMINI_ECHO = '''#!/usr/bin/env python3
import sys
sys.stdout.write("gemini did: " + sys.stdin.read()[:40])
'''


@pytest.fixture
def gemini(home: Path, fake_exe, monkeypatch):
    import agents
    import flags
    monkeypatch.setattr(flags, "enabled", lambda n: n == "ai_providers")
    exe = fake_exe(home / "fake-gemini", GEMINI_ECHO)
    res = agents.save_config({"connectors": [{"kind": "gemini", "bin": str(exe)}]})
    assert res["ok"]
    return res["config"]["connectors"][0]["id"]


def _agent_cfg(cfg, agent):
    out = copy.deepcopy(cfg)
    out["slack"]["events"]["mention"]["agent"] = agent
    return out


def test_a_trigger_can_name_the_agent_it_runs_on(cfg, gemini, http, dispatch, con):
    acfg = write_cfg(_agent_cfg(cfg, gemini))
    ev = mention(acfg, "700.1", OWNER_ID, f"{TAG} hello")
    rid = trigger.handle(ev, acfg, dispatch)["request_id"]
    assert trigger.get_request(con, rid)["meta"]["agent"] == gemini
    assert trigger.run_request(rid, acfg)["status"] == "done"
    assert trigger.get_request(con, rid)["result"].startswith("gemini did:")


def test_no_agent_named_runs_the_triggers_own_claude(cfg, http, dispatch, con, home):
    ev = mention(cfg, "700.2", OWNER_ID, f"{TAG} hello")
    rid = trigger.handle(ev, cfg, dispatch)["request_id"]
    assert "agent" not in trigger.get_request(con, rid)["meta"]
    assert trigger.run_request(rid, cfg)["ok"]
    assert claude_calls(home)


def test_an_agent_that_is_gone_fails_the_run_with_the_reason(cfg, http, dispatch, con):
    acfg = write_cfg(_agent_cfg(cfg, "no-such"))
    ev = mention(acfg, "700.3", OWNER_ID, f"{TAG} hello")
    rid = trigger.handle(ev, acfg, dispatch)["request_id"]
    assert trigger.run_request(rid, acfg)["status"] == "failed"
    assert "no agent connector" in trigger.get_request(con, rid)["reason"]


def test_saving_an_agent_that_does_not_exist_is_refused(cfg):
    row: dict = {}
    assert trigger._clean_action({"agent": "Bad Id!"}, row, "x")
    assert not trigger._clean_action({"agent": "gemini"}, row, "x") and row["agent"] == "gemini"
    assert "no agent connector" in trigger._action_complete({"action": "claude", "agent": "nope"}, "x")
