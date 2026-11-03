"""Custom triggers (docs/trigger.md#custom-triggers): validation, the block
engine, the isolated runner, a run end to end, the CLI and the scheduler.

    python -m pytest tests/test_custom_trigger.py

Every test gets a throwaway brain (`brain_home`). The HTTP the triggers call is
a server on 127.0.0.1 started by the `site` fixture; nothing leaves the machine.
The triggers are written JSON-shaped (JSON is YAML), so most of the suite runs
without PyYAML; the checks that need real YAML use `pytest.importorskip`.
"""

from __future__ import annotations

import copy
import io
import json
import socket
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

import _trigger_block_runner as blocks
import custom_trigger as ct
import graph
import trigger

SENTINEL = "s3cr3t-SENTINEL-value-42"
SITE_ID = "site-health"
TOKEN = "TOKEN"
UTF8 = "utf-8"
FIXED_NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)
LISTING_BODY = '{"items": [{"id": 1}, {"id": 2}]}'
FAKE_CLAUDE_SOURCE = (
    "#!/usr/bin/env python3\n"
    "import json, sys\n"
    "sys.stdin.read()\n"
    "print(json.dumps({'result': 'ok', 'total_cost_usd': 0.0}))\n")

CFG = {"enabled": True,
       "custom": {"enabled": True, "secrets": {TOKEN: SENTINEL}, "max_events_per_run": 20},
       "claude": {"bin": "unused"}}

GOOD_EMIT = {"id": "e", "type": "emit", "event": {"id": "x"}}

SITE_HEALTH = {
    "id": SITE_ID, "name": "Site health", "enabled": False, "interval": "5m",
    "do": "Tell the owner {event_title}.",
    "config": {"url": "http://placeholder/health"},
    "secrets": [TOKEN],
    "blocks": [
        {"id": "check", "type": "http_request", "url": "{{config.url}}",
         "headers": {"Authorization": "Bearer {{secrets.TOKEN}}"}},
        {"id": "down", "type": "condition", "if": "prev.status >= 400",
         "then": "continue", "else": "stop"},
        {"id": "alert", "type": "emit",
         "event": {"id": "site-health:{{prev.status}}", "title": "site down: {{prev.status}}",
                   "payload": {"status": "{{prev.status}}"}}},
    ],
}


# ---------------------------------------------------------------- helpers
def with_blocks(*bl, **extra) -> dict:
    return {"id": "t", "interval": "1m", "blocks": list(bl), **extra}


def emit_block(**event) -> dict:
    return {"id": "e", "type": "emit", "event": event}


def site_health(url: str) -> dict:
    defn = copy.deepcopy(SITE_HEALTH)
    defn["config"] = {"url": url}
    return defn


def write_trigger(defn: dict, name: str = "") -> str:
    ct.triggers_dir().mkdir(parents=True, exist_ok=True)
    tid = name or defn["id"]
    (ct.triggers_dir() / f"{tid}.yaml").write_text(json.dumps(defn, indent=2), encoding=UTF8)
    return tid


def write_raw(tid: str, text: str) -> Path:
    ct.triggers_dir().mkdir(parents=True, exist_ok=True)
    p = ct.triggers_dir() / f"{tid}.yaml"
    p.write_text(text, encoding=UTF8)
    return p


def write_script(name: str, source: str) -> None:
    graph.scripts_dir().mkdir(parents=True, exist_ok=True)
    (graph.scripts_dir() / name).write_text(source, encoding=UTF8)


def counts(con) -> dict:
    return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("trigger_request", "trigger_checkpoint", "custom_trigger_run",
                      "custom_trigger_state", "nodes")}


def request_count(con, tid: str) -> int:
    return con.execute("SELECT COUNT(*) FROM trigger_request WHERE source=?",
                       (ct.SOURCE_PREFIX + tid,)).fetchone()[0]


def free_closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Site:
    """What the local web server answers, and what it was asked."""

    def __init__(self) -> None:
        self.status = 503
        self.body = '{"ok": false, "items": [{"id": 1}, {"id": 2}]}'
        self.seen_headers: list[dict] = []
        self.url = ""


@pytest.fixture
def site(http_server):
    state = Site()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            state.seen_headers.append(dict(self.headers))
            data = state.body.encode()
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    state.url = http_server(Handler).url + "/health"
    return state


@pytest.fixture
def cfg(brain_home, tmp_path, fake_exe) -> dict:
    """The config with a working fake `claude`, also written to trigger.json."""
    conf = copy.deepcopy(CFG)
    conf["claude"]["bin"] = str(fake_exe(tmp_path / "fake-claude", FAKE_CLAUDE_SOURCE))
    trigger.config_path().write_text(json.dumps(conf), encoding=UTF8)
    return conf


@pytest.fixture
def con(brain_home):
    c = ct.db()
    yield c
    c.close()


class FakeIO(blocks.IO):
    def __init__(self, answers=(), script=None, scripts=None):
        super().__init__(http=self._http, now=lambda: FIXED_NOW, script=script, scripts=scripts)
        self.answers, self.calls = list(answers), []

    def _http(self, method, url, headers=None, data=None, timeout=30):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "data": data, "timeout": timeout})
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


# ================================================================ validate
def test_the_spec_example_is_valid_without_warnings(brain_home):
    v = ct.validate(SITE_HEALTH, CFG, expect_id=SITE_ID)
    assert v["ok"], v
    assert v["warnings"] == []


# (label, definition, text that must be in one of the errors)
REJECTED = [
    ("unsafe id", {**with_blocks(GOOD_EMIT), "id": "../x"}, "id must be"),
    ("id of the wrong kind", {**with_blocks(GOOD_EMIT), "id": 7}, "id must be"),
    ("interval under 30s", {**with_blocks(GOOD_EMIT), "interval": "10s"}, "at least 30s"),
    ("negative interval", {**with_blocks(GOOD_EMIT), "interval": -5}, "at least 30s"),
    ("interval that does not parse", {**with_blocks(GOOD_EMIT), "interval": "soon"},
     "interval must look like"),
    ("no interval", {"id": "t", "blocks": [GOOD_EMIT]}, "interval must look like"),
    ("mode other than poll", {**with_blocks(GOOD_EMIT), "mode": "webhook"}, "reserved"),
    ("enabled that is not a bool", {**with_blocks(GOOD_EMIT), "enabled": "yes"},
     "enabled is true or false"),
    ("do that is not text", {**with_blocks(GOOD_EMIT), "do": 5}, "do is text"),
    ("do that is too long", {**with_blocks(GOOD_EMIT), "do": "x" * (trigger.RUN_COMMAND_MAX + 1)},
     "do is text"),
    ("do with an unknown variable", {**with_blocks(GOOD_EMIT), "do": "tell {nobody}"}, "nobody"),
    ("config that is not a mapping", {**with_blocks(GOOD_EMIT), "config": [1]},
     "config is a mapping"),
    ("secrets that is not a list", {**with_blocks(GOOD_EMIT), "secrets": "TOKEN"},
     "secrets is a list of NAMES"),
    ("secret in lower case", {**with_blocks(GOOD_EMIT), "secrets": ["token"]},
     "secrets is a list of NAMES"),
    ("secret trigger.json does not have", {**with_blocks(GOOD_EMIT), "secrets": ["MISSING"]},
     "secret MISSING"),
    ("limits that is not a mapping", {**with_blocks(GOOD_EMIT), "limits": 3},
     "limits is a mapping"),
    ("timeout out of range", {**with_blocks(GOOD_EMIT), "limits": {"timeout_s": 7201}},
     "limits.timeout_s"),
    ("timeout that is a bool", {**with_blocks(GOOD_EMIT), "limits": {"timeout_s": True}},
     "limits.timeout_s"),
    ("memory out of range", {**with_blocks(GOOD_EMIT), "limits": {"memory_mb": 8}},
     "limits.memory_mb"),
    ("network that is not a bool", {**with_blocks(GOOD_EMIT), "limits": {"network": "no"}},
     "limits.network is true or false"),
    ("retry that is not a mapping", {**with_blocks(GOOD_EMIT), "retry": 3},
     "retry is a mapping"),
    ("retry attempts out of range", {**with_blocks(GOOD_EMIT), "retry": {"attempts": 9}},
     "retry.attempts"),
    ("retry backoff out of range", {**with_blocks(GOOD_EMIT), "retry": {"backoff_s": 61}},
     "retry.backoff_s"),
    ("no blocks", {"id": "t", "interval": "1m"}, "blocks is a list with at least one block"),
    ("empty blocks", with_blocks(), "blocks is a list with at least one block"),
    ("a block that is not a mapping", with_blocks("emit"), "must be a mapping"),
    ("a block id that is not valid", with_blocks({"id": "bad id", "type": "stop"}),
     "id must be letters"),
    ("a block id used twice", with_blocks(GOOD_EMIT, dict(GOOD_EMIT)), "used twice"),
    ("an unknown block type", with_blocks({"id": "a", "type": "loop"}, GOOD_EMIT),
     "type must be one of"),
    ("http_request with no url", with_blocks({"id": "a", "type": "http_request"}, GOOD_EMIT),
     "needs url"),
    ("http_request with a bad method",
     with_blocks({"id": "a", "type": "http_request", "url": "http://x", "method": "FETCH"},
                 GOOD_EMIT), "method must be one of"),
    ("http_request with a bad on_error",
     with_blocks({"id": "a", "type": "http_request", "url": "http://x", "on_error": "retry"},
                 GOOD_EMIT), "on_error is stop or continue"),
    ("http_request with network off",
     with_blocks({"id": "h", "type": "http_request", "url": "http://x"}, GOOD_EMIT,
                 limits={"network": False}), "network is false"),
    ("condition with no if", with_blocks({"id": "c", "type": "condition"}, GOOD_EMIT),
     "needs if"),
    ("condition with a then that is not continue or stop",
     with_blocks({"id": "c", "type": "condition", "if": "true", "then": "jump"}, GOOD_EMIT),
     "then is continue or stop"),
    ("condition with an else that is not continue or stop",
     with_blocks({"id": "c", "type": "condition", "if": "true", "else": "jump"}, GOOD_EMIT),
     "else is continue or stop"),
    ("condition that does not parse",
     with_blocks({"id": "c", "type": "condition", "if": "a =="}, GOOD_EMIT),
     "condition does not parse"),
    ("condition that calls something",
     with_blocks({"id": "c", "type": "condition", "if": "__import__('os').system('x')"},
                 GOOD_EMIT), "may not use Call"),
    ("condition that reads a dunder",
     with_blocks({"id": "c", "type": "condition", "if": "prev.__class__ == 1"}, GOOD_EMIT),
     "may not start with _"),
    ("condition with a computed index",
     with_blocks({"id": "p", "type": "stop"},
                 {"id": "c", "type": "condition", "if": "prev[config.i] == 1"}, GOOD_EMIT),
     "must be a literal"),
    ("transform with nothing to do", with_blocks({"id": "x", "type": "transform"}, GOOD_EMIT),
     "needs set, pick or drop"),
    ("transform with a set that is not a mapping",
     with_blocks({"id": "x", "type": "transform", "set": ["a"]}, GOOD_EMIT),
     "set is a mapping"),
    ("transform with a pick that is not a list of names",
     with_blocks({"id": "x", "type": "transform", "pick": "a"}, GOOD_EMIT),
     "pick is a list of field names"),
    ("transform with a drop of non-text names",
     with_blocks({"id": "x", "type": "transform", "drop": [1]}, GOOD_EMIT),
     "drop is a list of field names"),
    ("emit with no event", with_blocks({"id": "e", "type": "emit"}), "needs event"),
    ("emit with an event that is not a mapping",
     with_blocks({"id": "e", "type": "emit", "event": "x"}), "event is a mapping"),
    ("script with no script", with_blocks({"id": "s", "type": "script"}, GOOD_EMIT),
     "needs script"),
    ("script that is not there",
     with_blocks({"id": "s", "type": "script", "script": "nope.py"}, GOOD_EMIT),
     "no script nope.py"),
    ("script with a path instead of a name",
     with_blocks({"id": "s", "type": "script", "script": "../x.py"}, GOOD_EMIT),
     "is not a script name"),
    ("prev in the first block", with_blocks(emit_block(id="{{prev.x}}")),
     "first block has no prev"),
    ("a reference with an unknown root", with_blocks(emit_block(id="{{env.HOME}}")),
     "a reference starts with"),
    ("a reference that is not a path", with_blocks(emit_block(id="{{ 1x }}")),
     "is not a path"),
    ("config.x that config does not have",
     with_blocks(emit_block(id="{{config.nope}}"), config={"url": "x"}),
     "config.nope is not in config"),
    ("a secret used but not declared", with_blocks(emit_block(id="{{secrets.TOKEN}}")),
     "not one of the trigger's secrets"),
    ("secrets with no name", with_blocks(emit_block(id="{{secrets}}")),
     "not one of the trigger's secrets"),
    ("item outside an emit with each", with_blocks(emit_block(id="{{item.id}}")),
     "only there inside an emit with each"),
    ("item inside the each itself",
     with_blocks({"id": "e", "type": "emit", "each": "{{item.list}}", "event": {"id": "x"}}),
     "only there inside an emit with each"),
    ("a condition that reads an unknown root",
     with_blocks({"id": "p", "type": "stop"},
                 {"id": "c", "type": "condition", "if": "foo.bar == 1"}, GOOD_EMIT),
     "a reference starts with"),
]


@pytest.mark.parametrize("defn, needle", [pytest.param(d, n, id=label) for label, d, n in REJECTED])
def test_validate_rejects(brain_home, defn, needle):
    r = ct.validate(defn, CFG)
    assert not r["ok"]
    assert any(needle in e for e in r["errors"]), r["errors"]


def test_validate_rejects_a_definition_that_is_not_a_mapping(brain_home):
    r = ct.validate(["x"], CFG)
    assert r == {"ok": False, "errors": ["a trigger is a mapping"], "warnings": []}


def test_validate_rejects_a_file_name_that_differs_from_the_id(brain_home):
    r = ct.validate(with_blocks(GOOD_EMIT), CFG, expect_id="other")
    assert not r["ok"]
    assert any("other.yaml" in e for e in r["errors"])


def test_validate_rejects_a_script_that_is_not_python(brain_home):
    write_script("hello.sh", "echo hi\n")
    r = ct.validate(with_blocks({"id": "s", "type": "script", "script": "hello.sh"}, GOOD_EMIT),
                    CFG)
    assert not r["ok"]
    assert any("Python only" in e for e in r["errors"])


def test_validate_reads_the_config_from_disk_when_none_is_given(cfg):
    defn = with_blocks(GOOD_EMIT, secrets=[TOKEN])
    assert ct.validate(defn)["ok"]


def test_an_emit_with_no_event_id_is_a_warning(brain_home):
    r = ct.validate(with_blocks(emit_block(title="x")), CFG)
    assert r["ok"]
    assert any("stable id" in w for w in r["warnings"])


def test_no_emit_at_the_end_is_a_warning(brain_home):
    r = ct.validate(with_blocks({"id": "h", "type": "http_request", "url": "http://x"}), CFG)
    assert r["ok"]
    assert any("always ends skipped" in w for w in r["warnings"])


def test_a_trailing_stop_does_not_hide_the_emit_before_it(brain_home):
    r = ct.validate(with_blocks(GOOD_EMIT, {"id": "s", "type": "stop"}), CFG)
    assert r["ok"]
    assert r["warnings"] == []


def test_network_off_with_a_script_block_warns_that_it_is_not_enforced(brain_home):
    write_script("ok.py", "result = 1\n")
    r = ct.validate(with_blocks({"id": "s", "type": "script", "script": "ok.py"}, GOOD_EMIT,
                                limits={"network": False}), CFG)
    assert r["ok"]
    assert any("can still open a connection" in w for w in r["warnings"])


@pytest.mark.parametrize("platform, warns", [("linux", False), ("darwin", True), ("win32", True)])
def test_memory_limit_warning_follows_the_platform(brain_home, monkeypatch, platform, warns):
    monkeypatch.setattr(sys, "platform", platform)
    r = ct.validate(with_blocks(GOOD_EMIT, limits={"memory_mb": 256}), CFG)
    assert r["ok"]
    assert any("memory_mb is not enforced" in w for w in r["warnings"]) is warns


# ================================================================ small helpers
@pytest.mark.parametrize("value, seconds", [
    ("30s", 30), ("5m", 300), ("1h", 3600), ("1d", 86400), ("90", 90), (" 2 m ", 120),
    (45, 45), (0, 0), (-5, -5),
    (True, None), (False, None), ("soon", None), ("5x", None), ("", None), (None, None),
    ("1.5m", None), ([], None),
])
def test_parse_interval(value, seconds):
    assert ct.parse_interval(value) == seconds


@pytest.mark.parametrize("tid, ok", [
    ("site-health", True), ("a_b", True), ("a.b", False), ("../x", False), ("", False),
    (None, False), (5, False), ("a/b", False),
])
def test_id_ok(tid, ok):
    assert ct.id_ok(tid) is ok


def test_path_of_is_none_for_an_unsafe_id(brain_home):
    assert ct.path_of("../x") is None
    assert ct.path_of("ok").name == "ok.yaml"


def test_limits_and_retry_defaults_and_overrides():
    assert ct.limits_of({}) == {"timeout_s": 60, "memory_mb": 256, "network": True}
    assert ct.limits_of({"limits": {"timeout_s": 5, "network": False}}) == {
        "timeout_s": 5, "memory_mb": 256, "network": False}
    assert ct.limits_of({"limits": "bad"})["timeout_s"] == 60
    assert ct.retry_of({}) == {"attempts": 1, "backoff_s": 5}
    assert ct.retry_of({"retry": {"attempts": 3}}) == {"attempts": 3, "backoff_s": 5}
    assert ct.retry_of({"retry": []}) == {"attempts": 1, "backoff_s": 5}


def test_config_accessors():
    assert ct.custom_cfg({}) == {}
    assert ct.custom_cfg({"custom": "x"}) == {}
    assert ct.secret_values({"custom": {"secrets": {"A": 1, "B": "x"}}}) == {"A": "1", "B": "x"}
    assert ct.secret_values({"custom": {"secrets": ["A"]}}) == {}
    assert ct.max_events({}) == ct.MAX_EVENTS_DEFAULT
    assert ct.max_events({"custom": {"max_events_per_run": 7}}) == 7
    assert ct.max_events({"custom": {"max_events_per_run": 0}}) == ct.MAX_EVENTS_DEFAULT
    assert ct.max_events({"custom": {"max_events_per_run": "many"}}) == ct.MAX_EVENTS_DEFAULT
    assert ct.max_events({"custom": {"max_events_per_run": -4}}) == 1


@pytest.mark.parametrize("value, expected", [
    ("a SECRET b", "a *** b"),
    ({"SECRET": ["x SECRET"]}, {"***": ["x ***"]}),
    (["SECRET", 3, None], ["***", 3, None]),
    (42, 42),
])
def test_redact(value, expected):
    assert ct.redact(value, ["SECRET"]) == expected


def test_redact_with_no_secrets_changes_nothing():
    assert ct.redact("SECRET", []) == "SECRET"


def test_event_of_is_a_subscribed_request_that_opens_nothing(brain_home):
    ev = ct.event_of({"id": "t", "do": "act"},
                     {"id": "i" * 600, "title": "T", "payload": {"a": 1}, "permalink": "http://p"},
                     "run-1")
    assert ev["source"] == "custom:t"
    assert len(ev["event_id"]) == 500
    assert ev["subscribed"] is True
    assert ev["is_owner"] is False
    assert ev["permalink"] == "http://p"
    assert ev["meta"]["trigger_id"] == "t"
    assert ev["meta"]["payload"] == {"a": 1}
    assert ev["meta"]["do"] == "act"
    assert ev["meta"]["run_id"] == "run-1"


# ================================================================ files
def test_load_errors(brain_home):
    assert "not a trigger id" in ct.load("../x")[1]
    assert "no trigger" in ct.load("missing")[1]
    write_raw("list", "[1, 2]")
    assert "must hold a mapping" in ct.load("list")[1]
    write_raw("junk", "{not json, not yaml: [")
    assert ct.load("junk")[0] is None
    assert ct.load("junk")[1]


def test_list_ids_ignores_names_that_are_not_ids(brain_home):
    assert ct.list_ids() == []
    write_raw("b", "{}")
    write_raw("a", "{}")
    write_raw("not.an.id", "{}")
    assert ct.list_ids() == ["a", "b"]


def test_parse_text_without_pyyaml_reads_json_only(monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    assert ct.parse_text('{"a": 1}') == {"a": 1}
    with pytest.raises(ValueError, match="PyYAML is not installed"):
        ct.parse_text("a: 1")


def test_parse_text_with_pyyaml():
    pytest.importorskip("yaml")
    assert ct.parse_text("a: 1\nb: [x]") == {"a": 1, "b": ["x"]}
    with pytest.raises(ValueError, match="not valid YAML"):
        ct.parse_text("a: [1")


def test_real_yaml_trigger_loads(brain_home):
    pytest.importorskip("yaml")
    write_raw("yamly", "id: yamly\ninterval: 5m\nblocks:\n  - id: e\n    type: emit\n"
                       "    event:\n      id: fixed\n")
    defn, err = ct.load("yamly")
    assert not err
    assert defn["blocks"][0]["event"]["id"] == "fixed"


def test_enable_on_a_json_shaped_file(brain_home):
    write_trigger(SITE_HEALTH)
    assert ct.set_enabled(SITE_ID, True) == {"ok": True, "id": SITE_ID, "enabled": True}
    assert ct.load(SITE_ID)[0]["enabled"] is True
    assert ct.set_enabled(SITE_ID, False)["enabled"] is False
    assert ct.load(SITE_ID)[0]["enabled"] is False


def test_enable_rewrites_only_the_enabled_line(brain_home):
    pytest.importorskip("yaml")
    p = write_raw("commented", "# keep me\nid: commented\nenabled: false  # off for now\n"
                               "interval: 1m\nblocks: []\n")
    ct.set_enabled("commented", True)
    text = p.read_text(encoding=UTF8)
    assert text.startswith("# keep me\nid: commented\nenabled: true\n")
    assert text.endswith("interval: 1m\nblocks: []\n")


def test_enable_adds_the_line_after_the_id(brain_home):
    pytest.importorskip("yaml")
    p = write_raw("plain", "# top\nid: plain\ninterval: 1m\n")
    ct.set_enabled("plain", True)
    assert p.read_text(encoding=UTF8) == "# top\nid: plain\nenabled: true\ninterval: 1m\n"


def test_enable_with_no_id_line_puts_it_first(brain_home):
    pytest.importorskip("yaml")
    p = write_raw("noid", "interval: 1m\n")
    ct.set_enabled("noid", False)
    assert p.read_text(encoding=UTF8) == "enabled: false\ninterval: 1m\n"


def test_enable_a_trigger_that_does_not_load_says_why(brain_home):
    res = ct.set_enabled("missing", True)
    assert res["ok"] is False
    assert "no trigger" in res["error"]


# ================================================================ engine
SCOPE = {"prev": {"a": {"b": [10, {"c": "deep"}]}, "n": 3, "s": "x y"}, "config": {"u": "x"}}


@pytest.mark.parametrize("path, parts", [
    ("a.b[0]", ["a", "b", 0]),
    ('a["c d"].e', ["a", "c d", "e"]),
    ("a['x'][-1]", ["a", "x", -1]),
    ("  a  ", ["a"]),
    ("a-b", ["a-b"]),
])
def test_parse_path(path, parts):
    assert blocks.parse_path(path) == parts


@pytest.mark.parametrize("path", ["", "   ", None, "1x", "a..b", "a b", "[", "a.", "ab[x]"])
def test_parse_path_refuses_what_is_not_a_path(path):
    assert blocks.parse_path(path) is None


@pytest.mark.parametrize("path, expected", [
    ("prev.a.b[1].c", "deep"),
    ("prev.a.b[-2]", 10),
    ("prev.a.zz.c", None),
    ("prev.a.b[9]", None),
    ("prev.n.x", None),
    ("prev.a.b.c", None),
    ("not a path", None),
    ("", None),
])
def test_lookup(path, expected):
    assert blocks.lookup(SCOPE, path) == expected


def test_refs_are_found_recursively():
    assert blocks.refs({"a": "{{ x.y }}", "b": ["{{z}} and {{w}}", 3], "c": None}) == [
        "x.y", "z", "w"]


@pytest.mark.parametrize("template, expected", [
    ("{{prev.a.b}}", [10, {"c": "deep"}]),
    ("{{prev.n}}", 3),
    ("n={{prev.n}}!", "n=3!"),
    ("[{{prev.no}}]", "[]"),
    ("{{prev.a.b[1]}}!", '{"c": "deep"}!'),
    (["{{prev.n}}", "x{{prev.n}}"], [3, "x3"]),
    ({"k": "{{prev.n}}"}, {"k": 3}),
    (5, 5),
    (None, None),
])
def test_resolve(template, expected):
    assert blocks.resolve(template, SCOPE) == expected


def test_resolve_writes_booleans_as_words_in_text():
    assert blocks.resolve("ok={{prev.t}} no={{prev.f}}", {"prev": {"t": True, "f": False}}) \
        == "ok=true no=false"


@pytest.mark.parametrize("expr", [
    "prev.n >= 3 and config.u == 'x'",
    "not ('y' in [config.u, 'z'])",
    "{{prev.n}} == 3",
    "prev.n == 3 or prev.none == 1",
    "1 < prev.n < 4",
    "prev.n is not None",
    "prev.none is None",
    "prev.none == null",
    "prev.n != 4 and true",
    "-prev.n == -3",
    "prev.a.b[0] == 10",
    "prev['n'] == 3",
    "'x' in prev.s",
    "prev.n in (3, 4)",
])
def test_evaluate_true(expr):
    assert blocks.evaluate(expr, SCOPE) is True


@pytest.mark.parametrize("expr", [
    "prev.none > 400",
    "'a' in prev.none",
    "prev.n > 3",
    "1 < prev.n < 3",
    "prev.a.b[9] == 1",
    "prev.n.x == 1",
    "-prev.s == 1",
    "false",
    "prev.n == 3 and prev.none == 1",
    "-prev.n == 3",
])
def test_evaluate_false(expr):
    assert blocks.evaluate(expr, SCOPE) is False


@pytest.mark.parametrize("expr, needle", [
    ("(lambda: 1)()", "may not use"),
    ("", "empty condition"),
    ("   ", "empty condition"),
    ("a ==", "does not parse"),
    ("f(1)", "may not use Call"),
    ("prev.__class__", "may not start with _"),
    ("prev[config.i]", "must be a literal"),
    ("prev + 1", "may not use"),
    ("[x for x in prev]", "may not use"),
])
def test_conditions_outside_the_small_language_are_refused(expr, needle):
    with pytest.raises(blocks.BlockError, match=needle):
        blocks.evaluate(expr, SCOPE)


def test_condition_roots_skip_literals():
    assert blocks.condition_roots("prev.a == null and config.u == true") == {"prev", "config"}


# a http_request that 503s, then the condition, then the emit
def test_503_emits_one_event_and_sends_the_secret():
    io503 = FakeIO([(503, "down")])
    r = blocks.run_blocks(SITE_HEALTH, {"secrets": {TOKEN: "t"}}, io503)
    assert r["status"] == "ok"
    assert [e["id"] for e in r["events"]] == ["site-health:503"]
    assert io503.calls[0]["headers"]["Authorization"] == "Bearer t"
    assert [entry["block"] for entry in r["log"]] == ["check", "down", "alert"]


def test_200_ends_skipped():
    r = blocks.run_blocks(SITE_HEALTH, {}, FakeIO([(200, "ok")]))
    assert r["status"] == "skipped"
    assert not r["events"]
    assert r["log"][-1] == {"block": "down", "status": "stop", "detail": "False -> stop"}


def test_a_network_failure_without_on_error_is_an_error():
    r = blocks.run_blocks(SITE_HEALTH, {}, FakeIO([blocks.BlockError("refused")]))
    assert r["status"] == "error"
    assert "block check: refused" in r["error"]
    assert r["events"] == []
    assert r["checkpoint"] is None


def test_on_error_continue_leaves_prev_status_none_and_the_flow_goes_on():
    tolerant = copy.deepcopy(SITE_HEALTH)
    tolerant["blocks"][0]["on_error"] = "continue"
    tolerant["blocks"][1]["if"] = "prev.status == None or prev.status >= 400"
    r = blocks.run_blocks(tolerant, {}, FakeIO([blocks.BlockError("refused")]))
    assert r["status"] == "ok"
    assert r["events"][0]["id"] == "site-health:"
    assert "failed, continuing: refused" in r["log"][0]["detail"]


def test_a_list_in_emit_is_one_event_per_item_and_moves_the_checkpoint():
    listing = with_blocks(
        {"id": "get", "type": "http_request", "url": "http://x"},
        {"id": "shape", "type": "transform", "set": {"items": "{{prev.json.items}}"},
         "pick": ["status"], "drop": ["status"]},
        {"id": "each", "type": "emit", "each": "{{prev.items}}",
         "event": {"id": "item-{{item.id}}", "title": "new {{item.id}}", "payload": "{{item}}"},
         "checkpoint": "{{prev.items[-1].id}}"})
    r = blocks.run_blocks(listing, {}, FakeIO([(200, LISTING_BODY)]))
    assert [e["id"] for e in r["events"]] == ["item-1", "item-2"]
    assert r["events"][1]["payload"] == {"id": 2}
    assert r["events"][1]["title"] == "new 2"
    assert r["checkpoint"] == "2"
    r = blocks.run_blocks(listing, {"max_events": 1}, FakeIO([(200, LISTING_BODY)]))
    assert r["status"] == "error"
    assert "limit is 1" in r["error"]
    assert r["events"] == []


def test_stop_ends_the_run_before_emit():
    r = blocks.run_blocks(with_blocks({"id": "s", "type": "stop"}, GOOD_EMIT), {})
    assert r["status"] == "skipped"
    assert r["log"] == [{"block": "s", "status": "stop", "detail": ""}]


def test_an_unknown_block_type_is_an_error():
    r = blocks.run_blocks(with_blocks({"id": "z", "type": "loop"}), {})
    assert r["status"] == "error"
    assert "unknown block type 'loop'" in r["error"]


def test_no_blocks_is_skipped():
    assert blocks.run_blocks({"id": "t"}, {})["status"] == "skipped"


@pytest.mark.parametrize("hit_flow, expected", [
    ({"then": "stop"}, "skipped"),
    ({"then": "continue"}, "ok"),
    ({}, "ok"),
])
def test_condition_flow_when_the_test_holds(hit_flow, expected):
    defn = with_blocks({"id": "c", "type": "condition", "if": "true", **hit_flow}, GOOD_EMIT)
    assert blocks.run_blocks(defn, {})["status"] == expected


@pytest.mark.parametrize("miss_flow, expected", [
    ({"else": "continue"}, "ok"),
    ({"else": "stop"}, "skipped"),
    ({}, "skipped"),
])
def test_condition_flow_when_the_test_fails(miss_flow, expected):
    defn = with_blocks({"id": "c", "type": "condition", "if": "false", **miss_flow}, GOOD_EMIT)
    assert blocks.run_blocks(defn, {})["status"] == expected


def test_a_bad_condition_is_a_block_error():
    r = blocks.run_blocks(with_blocks({"id": "c", "type": "condition", "if": "f()"}), {})
    assert r["status"] == "error"
    assert "block c:" in r["error"]


# ---- http_request
def http_block(**kw) -> dict:
    return {"id": "h", "type": "http_request", "url": "http://x/y", **kw}


def test_http_post_with_a_mapping_body_is_sent_as_json():
    io = FakeIO([(201, '{"id": 5}')])
    r = blocks.run_blocks(with_blocks(
        http_block(method="post", body={"a": "{{config.v}}"}, headers={"X-A": "  a   b "}),
        GOOD_EMIT, config={"v": 9}), {}, io)
    call = io.calls[0]
    assert r["status"] == "ok"
    assert call["method"] == "POST"
    assert json.loads(call["data"]) == {"a": 9}
    assert call["headers"]["Content-Type"] == "application/json; charset=utf-8"
    assert call["headers"]["X-A"] == "a b"


def test_http_post_keeps_the_owners_content_type_and_text_bodies():
    io = FakeIO([(200, "")])
    blocks.run_blocks(with_blocks(
        http_block(method="PUT", body="plain {{config.v}}", headers={"content-type": "text/x"}),
        config={"v": 1}), {}, io)
    assert io.calls[0]["data"] == b"plain 1"
    assert io.calls[0]["headers"] == {"content-type": "text/x"}
    io = FakeIO([(200, "")])
    blocks.run_blocks(with_blocks(http_block(method="POST", body={"a": 1},
                                             headers={"Content-Type": "application/x"})), {}, io)
    assert list(io.calls[0]["headers"]) == ["Content-Type"]


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_http_get_and_head_send_no_body(method):
    io = FakeIO([(200, "")])
    blocks.run_blocks(with_blocks(http_block(method=method, body={"a": 1})), {}, io)
    assert io.calls[0]["data"] is None


@pytest.mark.parametrize("asked, used", [(None, 30), (5, 5), (500, 30), (0, 30), (-3, 1)])
def test_http_timeout_is_clamped(asked, used):
    io = FakeIO([(200, "")])
    blocks.run_blocks(with_blocks(http_block(**({} if asked is None else {"timeout_s": asked}))),
                      {}, io)
    assert io.calls[0]["timeout"] == used


def test_http_prev_has_status_ok_body_and_json():
    scripted = FakeIO([(404, '{"a": 1}')])
    seen = {}

    def capture(path, env):
        seen.update(env["prev"])
        return {"result": None}

    scripted.script, scripted.scripts = capture, {"s.py": "s.py"}
    blocks.run_blocks(with_blocks(http_block(), {"id": "s", "type": "script", "script": "s.py"}),
                      {}, scripted)
    assert seen == {"status": 404, "ok": False, "body": '{"a": 1}', "json": {"a": 1}}


@pytest.mark.parametrize("body, parsed", [("not json", None), ("{broken", None), ("", None),
                                          ("  [1, 2]", [1, 2])])
def test_http_json_is_only_what_parses(body, parsed):
    io = FakeIO([(200, body)])
    seen = {}

    def capture(path, env):
        seen.update(env["prev"])
        return {}

    io.script, io.scripts = capture, {"s.py": "s.py"}
    blocks.run_blocks(with_blocks(http_block(), {"id": "s", "type": "script", "script": "s.py"}),
                      {}, io)
    assert seen["json"] == parsed


@pytest.mark.parametrize("url", ["ftp://x", "not a url", "{{config.none}}"])
def test_http_refuses_a_url_that_is_not_http(url):
    r = blocks.run_blocks(with_blocks(http_block(url=url)), {}, FakeIO([(200, "")]))
    assert r["status"] == "error"
    assert "not an http(s) URL" in r["error"]


def test_http_with_network_off_is_an_error_and_makes_no_call():
    io = FakeIO([(200, "")])
    r = blocks.run_blocks(with_blocks(http_block(), limits={"network": False}), {}, io)
    assert r["status"] == "error"
    assert "network is off" in r["error"]
    assert io.calls == []


def test_http_raw_against_a_real_server(site):
    site.status, site.body = 200, "hello"
    assert blocks.http_raw("GET", site.url) == (200, "hello")
    site.status, site.body = 404, "gone"
    assert blocks.http_raw("GET", site.url, headers={"X-T": "1"}) == (404, "gone")
    assert site.seen_headers[-1]["X-T"] == "1"


def test_http_raw_failures_are_block_errors_without_the_query(brain_home):
    port = free_closed_port()
    with pytest.raises(blocks.BlockError) as e:
        blocks.http_raw("GET", f"http://127.0.0.1:{port}/p?k={SENTINEL}", timeout=3)
    assert SENTINEL not in str(e.value)


def test_http_raw_invalid_url_is_a_block_error(brain_home):
    with pytest.raises(blocks.BlockError):
        blocks.http_raw("GET", "http://127.0.0.1/a\x00b", timeout=3)


# ---- transform
def run_transform(prev, **block):
    seen = {}

    def capture(path, env):
        seen.update(prev=env["prev"])
        return {}

    io = FakeIO([(200, json.dumps(prev) if prev is not None else "")], script=capture,
                scripts={"s.py": "s.py"})
    blocks.run_blocks(with_blocks(
        http_block(), {"id": "t", "type": "transform", **block},
        {"id": "s", "type": "script", "script": "s.py"}), {}, io)
    return seen["prev"]


def test_transform_pick_drop_and_set_apply_in_that_order():
    out = run_transform({"a": 1}, pick=["status", "ok", "zzz"], drop=["ok"],
                        set={"n": "{{prev.status}}", "lit": 5})
    assert out == {"status": 200, "zzz": None, "n": 200, "lit": 5}


def test_transform_set_only_adds_to_prev():
    out = run_transform(None, set={"k": "v"})
    assert out["k"] == "v"
    assert out["status"] == 200


def test_transform_wraps_a_prev_that_is_not_a_mapping():
    seen = {}
    defn = with_blocks({"id": "s0", "type": "script", "script": "s.py"},
                       {"id": "t", "type": "transform", "set": {"x": 1}},
                       {"id": "s", "type": "script", "script": "s.py"})
    calls = []

    def script(path, env):
        calls.append(env["prev"])
        return {"result": 7} if len(calls) == 1 else {}

    blocks.run_blocks(defn, {}, FakeIO(script=script, scripts={"s.py": "s.py"}))
    seen["prev"] = calls[-1]
    assert seen["prev"] == {"value": 7, "x": 1}


def test_transform_with_no_prev_starts_empty():
    calls = []

    def script(path, env):
        calls.append(env["prev"])
        return {}

    blocks.run_blocks(with_blocks({"id": "t", "type": "transform", "set": {"x": 1}},
                                  {"id": "s", "type": "script", "script": "s.py"}),
                      {}, FakeIO(script=script, scripts={"s.py": "s.py"}))
    assert calls == [{"x": 1}]


# ---- emit
def test_emit_defaults_the_id_and_the_title():
    r = blocks.run_blocks(with_blocks({"id": "e", "type": "emit", "event": {}}), {})
    assert r["events"] == [{"id": "t:" + r["events"][0]["id"].split(":", 1)[1],
                            "title": "t fired", "permalink": "", "payload": {}}]
    assert r["events"][0]["id"].startswith("t:")


def test_emit_uses_the_context_clock_for_the_default_id():
    r = blocks.run_blocks(with_blocks({"id": "e", "type": "emit", "event": {"title": "x"}}),
                          {"now": "2026-10-12T00:00:00+00:00"})
    assert r["events"][0]["id"] == "t:2026-10-12T00:00:00+00:00"


def test_emit_uses_the_io_clock_when_the_context_has_none():
    r = blocks.run_blocks(with_blocks({"id": "e", "type": "emit", "event": {}}), {}, FakeIO())
    assert r["events"][0]["id"] == "t:2026-10-12T00:00:00+00:00"


def test_emit_truncates_long_fields_and_keeps_the_permalink():
    r = blocks.run_blocks(with_blocks(emit_block(id="i" * 900, title="t" * 900,
                                                 permalink="p" * 2000)), {})
    ev = r["events"][0]
    assert (len(ev["id"]), len(ev["title"]), len(ev["permalink"])) == (500, 500, 1000)


@pytest.mark.parametrize("each, ids", [
    ("{{config.missing}}", []),
    ("{{config.one}}", ["i-5"]),
    ("{{config.many}}", ["i-1", "i-2"]),
])
def test_emit_each_accepts_a_list_a_scalar_or_nothing(each, ids):
    defn = with_blocks({"id": "e", "type": "emit", "each": each,
                        "event": {"id": "i-{{item}}"}},
                       config={"one": 5, "many": [1, 2]})
    r = blocks.run_blocks(defn, {})
    assert [e["id"] for e in r["events"]] == ids
    assert r["status"] == ("ok" if ids else "skipped")


def test_emit_checkpoint_is_text_and_may_be_empty():
    r = blocks.run_blocks(with_blocks({**emit_block(id="x"), "checkpoint": "{{config.n}}"},
                                      config={"n": 12}), {})
    assert r["checkpoint"] == "12"
    r = blocks.run_blocks(with_blocks(emit_block(id="x")), {})
    assert r["checkpoint"] is None


def test_the_checkpoint_in_the_context_is_in_scope():
    r = blocks.run_blocks(with_blocks(emit_block(id="c-{{checkpoint}}")), {"checkpoint": "7"})
    assert r["events"][0]["id"] == "c-7"


def test_the_event_limit_defaults_to_twenty():
    defn = with_blocks({"id": "e", "type": "emit", "each": "{{config.many}}",
                        "event": {"id": "i-{{item}}"}}, config={"many": list(range(21))})
    r = blocks.run_blocks(defn, {})
    assert r["status"] == "error"
    assert "limit is 20" in r["error"]


# ---- script
def test_a_script_block_sets_prev_and_may_move_the_checkpoint():
    calls = []

    def script(path, env):
        calls.append((path, env["checkpoint"]))
        return {"result": {"n": 5}, "checkpoint": "c-" + env["checkpoint"]}

    defn = with_blocks({"id": "s", "type": "script", "script": "cursor.py"},
                       emit_block(id="n{{prev.n}}-{{checkpoint}}"))
    r = blocks.run_blocks(defn, {"checkpoint": "7"},
                          FakeIO(script=script, scripts={"cursor.py": "/abs/cursor.py"}))
    assert calls == [("/abs/cursor.py", "7")]
    assert r["events"][0]["id"] == "n5-c-7"
    assert r["checkpoint"] == "c-7"


def test_a_script_that_asks_to_stop_ends_the_run():
    defn = with_blocks({"id": "s", "type": "script", "script": "s.py"}, GOOD_EMIT)
    r = blocks.run_blocks(defn, {}, FakeIO(script=lambda p, e: {"result": 1, "stop": True},
                                           scripts={"s.py": "s.py"}))
    assert r["status"] == "skipped"
    assert r["log"][0]["status"] == "stop"


def test_a_script_nobody_gave_a_path_for_is_an_error():
    r = blocks.run_blocks(with_blocks({"id": "s", "type": "script", "script": "x.py"}), {})
    assert r["status"] == "error"
    assert "no script 'x.py'" in r["error"]


def test_a_script_that_raises_is_an_error_with_its_type():
    def script(path, env):
        raise KeyError("boom")

    r = blocks.run_blocks(with_blocks({"id": "s", "type": "script", "script": "s.py"}), {},
                          FakeIO(script=script, scripts={"s.py": "s.py"}))
    assert r["status"] == "error"
    assert "KeyError" in r["error"]


def test_run_script_returns_the_result_and_what_changed(tmp_path, capsys):
    path = tmp_path / "s.py"
    path.write_text("print('noise')\nresult = [prev, checkpoint, secrets['A']]\n"
                    "checkpoint = 'moved'\nstop = True\n", encoding=UTF8)
    out = blocks.run_script(str(path), {"prev": 1, "config": {}, "checkpoint": "c",
                                        "now": "n", "secrets": {"A": "a"}})
    assert out == {"result": [1, "c", "a"], "stop": True, "checkpoint": "moved"}
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "noise" in captured.err


def test_run_script_leaves_the_checkpoint_out_when_unchanged(tmp_path):
    path = tmp_path / "s.py"
    path.write_text("result = 1\n", encoding=UTF8)
    out = blocks.run_script(str(path), {"prev": None, "config": {}, "checkpoint": "c",
                                        "now": "n", "secrets": {}})
    assert out == {"result": 1, "stop": False}


def test_limit_memory_off_says_nothing():
    assert blocks._limit_memory(0) == ""


def test_limit_memory_says_why_when_it_cannot(monkeypatch):
    resource = pytest.importorskip("resource")

    def refuse(*a):
        raise OSError("no")

    monkeypatch.setattr(resource, "setrlimit", refuse)
    assert "not enforced here: no" in blocks._limit_memory(64)


def test_main_runs_one_context(monkeypatch, capsys):
    ctx = {"trigger": with_blocks(emit_block(id="m")), "memory_mb": 0}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(ctx)))
    assert blocks.main() == 0
    res = json.loads(capsys.readouterr().out)
    assert res["status"] == "ok"
    assert res["events"][0]["id"] == "m"


def test_main_refuses_a_context_that_is_not_json(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert blocks.main() == 2
    assert "bad context" in json.loads(capsys.readouterr().out)["error"]


def test_main_notes_when_the_memory_limit_is_not_enforced(monkeypatch, capsys):
    monkeypatch.setattr(blocks, "_limit_memory", lambda mb: "memory_mb is not enforced here")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"trigger": with_blocks(emit_block(id="m")), "memory_mb": 64})))
    blocks.main()
    res = json.loads(capsys.readouterr().out)
    assert res["log"][0] == {"block": "", "status": "warning",
                             "detail": "memory_mb is not enforced here"}


# ================================================================ isolation
def iso_ctx(defn, cfg_=CFG, **extra):
    return {**ct.context(defn, cfg_, ""), **extra}


def test_context_hands_over_only_the_declared_secrets_and_known_scripts(brain_home):
    write_script("ok.py", "result = 1\n")
    defn = with_blocks({"id": "s", "type": "script", "script": "ok.py"},
                       {"id": "t", "type": "script", "script": "nope.py"},
                       secrets=[TOKEN, "OTHER"])
    c = ct.context(defn, {"custom": {"secrets": {TOKEN: "v", "UNDECLARED": "z"}}}, None)
    assert c["secrets"] == {TOKEN: "v"}
    assert list(c["scripts"]) == ["ok.py"]
    assert c["checkpoint"] == ""
    assert c["max_events"] == ct.MAX_EVENTS_DEFAULT
    assert c["memory_mb"] == 256


def test_the_runner_calls_real_http_and_emits(site):
    r = ct.run_isolated(iso_ctx(site_health(site.url)), 30)
    assert r["status"] == "ok"
    assert r["events"][0]["id"] == "site-health:503"
    assert site.seen_headers[0]["Authorization"] == f"Bearer {SENTINEL}"


def test_a_timeout_kills_the_runner(brain_home):
    write_script("slow.py", "import time\ntime.sleep(30)\nresult = 1\n")
    defn = with_blocks({"id": "s", "type": "script", "script": "slow.py"}, GOOD_EMIT)
    t0 = datetime.now()
    r = ct.run_isolated(iso_ctx(defn), 2)
    took = (datetime.now() - t0).total_seconds()
    assert r["status"] == "error"
    assert "killed after 2s" in r["error"]
    assert took < 15


def test_more_than_the_output_cap_is_an_error(brain_home):
    write_script("loud.py", "import sys\nsys.__stdout__.write('x' * 2_000_000)\nresult = 1\n")
    r = ct.run_isolated(iso_ctx(with_blocks({"id": "s", "type": "script",
                                             "script": "loud.py"})), 30)
    assert r["status"] == "error"
    assert "more than" in r["error"]


@pytest.mark.parametrize("payload, needle", [
    ("not json", "not JSON"),
    ('{"x": 1}', "has no status"),
    ('[1]', "has no status"),
    ('{"status": "weird"}', "has no status"),
])
def test_an_answer_that_is_not_the_runners_is_an_error(brain_home, payload, needle):
    write_script("garbage.py", "import os, sys\n"
                               f"sys.__stdout__.write({payload!r})\n"
                               "sys.__stdout__.flush()\nos._exit(0)\n")
    r = ct.run_isolated(iso_ctx(with_blocks({"id": "s", "type": "script",
                                             "script": "garbage.py"})), 30)
    assert r["status"] == "error"
    assert needle in r["error"]


def test_a_runner_that_exits_non_zero_is_an_error_with_its_stderr(brain_home):
    write_script("die.py", "import sys\nsys.stderr.write('bye now')\nsys.exit(3)\n")
    r = ct.run_isolated(iso_ctx(with_blocks({"id": "s", "type": "script",
                                             "script": "die.py"})), 30)
    assert r["status"] == "error"
    assert "exit 3" in r["error"]
    assert "bye now" in r["error"]


def test_a_runner_that_cannot_start_is_an_error(brain_home, monkeypatch):
    def refuse(*a, **k):
        raise OSError("no such interpreter")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    r = ct.run_isolated(iso_ctx(with_blocks(GOOD_EMIT)), 5)
    assert r["status"] == "error"
    assert "could not start the runner" in r["error"]


def test_the_child_does_not_see_the_parents_environment(brain_home, monkeypatch):
    monkeypatch.setenv("LEAK_ME", "parent-only")
    write_script("env.py", "import os\nresult = {'env': sorted(os.environ)}\n")
    r = ct.run_isolated(iso_ctx(with_blocks(
        {"id": "s", "type": "script", "script": "env.py"},
        emit_block(id="x", payload="{{prev.env}}"))), 30)
    assert r["status"] == "ok", r
    env = r["events"][0]["payload"]
    assert "LEAK_ME" not in env
    assert "SECOND_BRAIN_HOME" not in env


def test_child_env_is_a_path_and_nothing_else_off_windows(monkeypatch):
    monkeypatch.setattr(ct._platform, "WINDOWS", False)
    assert set(ct.child_env()) == {"PATH"}


def test_child_env_carries_what_windows_needs(monkeypatch):
    monkeypatch.setattr(ct._platform, "WINDOWS", True)
    monkeypatch.setenv("SYSTEMROOT", "C:\\Windows")
    monkeypatch.setenv("LEAK_ME", "x")
    env = ct.child_env()
    assert env["SYSTEMROOT"] == "C:\\Windows"
    assert "LEAK_ME" not in env


def test_a_script_gets_prev_and_checkpoint_and_may_move_the_checkpoint(brain_home):
    write_script("cursor.py", "result = {'n': 5}\ncheckpoint = 'c-' + (checkpoint or '0')\n")
    r = ct.run_isolated(iso_ctx(with_blocks(
        {"id": "s", "type": "script", "script": "cursor.py"},
        emit_block(id="n{{prev.n}}")), checkpoint="7"), 30)
    assert r["status"] == "ok"
    assert r["checkpoint"] == "c-7"
    assert r["events"][0]["id"] == "n5"


def test_a_secret_in_an_error_is_blotted_out(brain_home):
    write_script("leak.py", "raise RuntimeError('token is ' + secrets['TOKEN'])\n")
    defn = with_blocks({"id": "s", "type": "script", "script": "leak.py"}, secrets=[TOKEN])
    raw = ct.run_isolated(iso_ctx(defn), 30)
    assert SENTINEL in json.dumps(raw)      # it did reach the runner's answer
    r = ct.redact(raw, [SENTINEL])
    assert SENTINEL not in json.dumps(r)
    assert "***" in json.dumps(r)


def test_the_runner_file_reads_a_bad_context_from_the_command_line(brain_home):
    p = subprocess.run([sys.executable, "-I", str(ct.RUNNER)], input="nope",
                       capture_output=True, text=True, encoding=UTF8)
    assert p.returncode == 2
    assert "bad context" in json.loads(p.stdout)["error"]


# ================================================================ execute
def test_dry_run_says_what_it_would_create_and_writes_nothing(cfg, con, site):
    write_trigger(site_health(site.url))
    before = counts(con)
    dry = ct.execute(SITE_ID, cfg=cfg, dry_run=True)
    assert dry["ok"] and dry["dry_run"]
    assert [e["event_id"] for e in dry["events"]] == ["site-health:503"]
    assert dry["checkpoint"] == {"now": "", "would_be": None}
    assert dry["warnings"] == []
    assert counts(con) == before


def test_a_dry_run_of_a_broken_trigger_writes_no_state(cfg, con):
    res = ct.execute("missing", cfg=cfg, dry_run=True)
    assert res["status"] == "invalid"
    assert ct.get_state(con, "missing") == {}


@pytest.mark.parametrize("make", ["missing", "invalid"])
def test_a_trigger_that_cannot_run_is_noted_invalid(cfg, con, make):
    if make == "invalid":
        write_trigger({"id": "bad", "interval": "1s", "blocks": []}, "bad")
    tid = "missing" if make == "missing" else "bad"
    res = ct.execute(tid, cfg=cfg)
    assert res["ok"] is False
    assert res["status"] == "invalid"
    assert res["error"] if make == "missing" else res["errors"]
    assert ct.get_state(con, tid)["last_status"] == "invalid"
    assert ct.runs(con, tid) == []


def test_a_scheduled_run_inside_the_interval_is_not_due(cfg, con, site):
    write_trigger(site_health(site.url))
    assert ct.execute(SITE_ID, cfg=cfg, scheduled=True)["status"] == "ok"
    assert ct.execute(SITE_ID, cfg=cfg, scheduled=True) == {"ok": True, "id": SITE_ID,
                                                            "status": "not_due"}
    assert ct.execute(SITE_ID, cfg=cfg)["status"] == "ok"   # by hand it always runs
    assert len(ct.runs(con, SITE_ID)) == 2


def test_a_run_creates_one_task_and_the_same_event_creates_nothing_twice(cfg, con, site):
    write_trigger(site_health(site.url))
    ran = []
    r = ct.execute(SITE_ID, cfg=cfg, dispatch=ran.append)
    assert r["ok"] and len(r["created"]) == 1
    assert ran == r["created"]
    req = trigger.get_request(con, r["created"][0])
    assert req["status"] == "queued"
    assert req["source"] == "custom:site-health"
    assert req["reason"] == "subscribed: custom"
    assert not req["is_owner"]
    r2 = ct.execute(SITE_ID, cfg=cfg, dispatch=ran.append)
    assert r2["ok"] and not r2["created"]
    assert r2["seen"] == 1
    assert len(ran) == 1


def test_every_run_is_in_the_history_and_the_state_holds_the_last(cfg, con, site):
    write_trigger(site_health(site.url))
    ct.execute(SITE_ID, cfg=cfg)
    ct.execute(SITE_ID, cfg=cfg)
    site.status = 200
    r3 = ct.execute(SITE_ID, cfg=cfg)
    assert r3["status"] == "skipped"
    assert not r3["created"]
    hist = ct.runs(con, SITE_ID)
    assert sorted(h["status"] for h in hist) == ["ok", "ok", "skipped"]
    assert ct.runs(con, SITE_ID, limit=1)[0]["run_id"] == r3["run_id"]
    assert ct.get_state(con, SITE_ID)["last_status"] == "skipped"
    assert all(h["finished_at"] for h in hist)


def test_a_history_row_with_a_broken_log_reads_as_empty(cfg, con, site):
    write_trigger(site_health(site.url))
    r = ct.execute(SITE_ID, cfg=cfg)
    con.execute("UPDATE custom_trigger_run SET log='{broken' WHERE run_id=?", (r["run_id"],))
    con.commit()
    assert ct.runs(con, SITE_ID)[0]["log"] == []


def test_the_prompt_and_variables_for_a_custom_request(cfg, con, site):
    write_trigger(site_health(site.url))
    r = ct.execute(SITE_ID, cfg=cfg)
    req = trigger.get_request(con, r["created"][0])
    prompt = trigger.build_prompt(req, cfg)
    assert "<<<PAYLOAD" in prompt
    assert "DATA, not" in prompt
    assert "custom_trigger: site-health" in prompt
    assert "Tell the owner site down: 503." in prompt
    vals = trigger.request_variables(req, cfg)
    assert vals["trigger_id"] == SITE_ID
    assert json.loads(vals["event_payload"]) == {"status": 503}
    assert vals["event_title"] == "site down: 503"


def test_the_tasks_go_in_before_the_checkpoint_moves(cfg, con, monkeypatch):
    write_script("cursor.py", "result = {'n': 5}\ncheckpoint = 'c-' + (checkpoint or '0')\n")
    write_trigger({**with_blocks({"id": "s", "type": "script", "script": "cursor.py"},
                                 emit_block(id="n{{prev.n}}")), "id": "cursor"})

    def boom(*a, **k):
        raise RuntimeError("crash between the tasks and the checkpoint")

    with monkeypatch.context() as m:
        m.setattr(trigger, "set_checkpoint", boom)
        with pytest.raises(RuntimeError):
            ct.execute("cursor", cfg=cfg)
    assert request_count(con, "cursor") == 1
    assert trigger.get_checkpoint(con, "custom:cursor") == ""

    r = ct.execute("cursor", cfg=cfg)
    assert r["ok"] and r["seen"] == 1
    assert request_count(con, "cursor") == 1
    assert trigger.get_checkpoint(con, "custom:cursor") == "c-0"
    assert ct.execute("cursor", cfg=cfg)["ok"]
    assert trigger.get_checkpoint(con, "custom:cursor") == "c-c-0"   # opaque, moves backwards too


def test_an_error_run_creates_nothing_and_leaves_the_checkpoint(cfg, con):
    write_script("bad.py", "raise ValueError('nope')\n")
    write_trigger({**with_blocks({"id": "s", "type": "script", "script": "bad.py"},
                                 emit_block(id="x")), "id": "failing"})
    trigger.set_checkpoint(con, "custom:failing", "keep", only_forward=False)
    r = ct.execute("failing", cfg=cfg)
    assert r["ok"] is False
    assert r["status"] == "error"
    assert "ValueError" in r["error"]
    assert r["created"] == []
    assert trigger.get_checkpoint(con, "custom:failing") == "keep"
    assert ct.get_state(con, "failing")["last_status"] == "error"
    assert "ValueError" in ct.get_state(con, "failing")["last_error"]


def test_too_many_events_is_an_error_that_creates_no_task(cfg, con):
    cfg["custom"]["max_events_per_run"] = 1
    write_trigger({"id": "many", "interval": "1m", "config": {"l": [1, 2]},
                   "blocks": [{"id": "e", "type": "emit", "each": "{{config.l}}",
                               "event": {"id": "i{{item}}"}}]})
    r = ct.execute("many", cfg=cfg)
    assert r["status"] == "error"
    assert "limit is 1" in r["error"]
    assert request_count(con, "many") == 0


def test_a_failing_run_is_retried_and_each_attempt_is_logged(cfg, con):
    flaky = {**with_blocks({"id": "h", "type": "http_request",
                            "url": f"http://127.0.0.1:{free_closed_port()}/x"}, GOOD_EMIT,
                           retry={"attempts": 3, "backoff_s": 7}), "id": "flaky"}
    write_trigger(flaky)
    sleeps = []
    r = ct.execute("flaky", cfg=cfg, sleep=sleeps.append)
    assert r["status"] == "error"
    assert r["attempts"] == 3
    assert sleeps == [7, 7]
    log = ct.runs(con, "flaky")[0]["log"]
    assert [a["attempt"] for a in log] == [1, 2, 3]
    assert all(a["status"] == "error" for a in log)


def test_a_retry_that_succeeds_stops_retrying(cfg, con, site):
    defn = site_health(site.url)
    defn["retry"] = {"attempts": 3, "backoff_s": 0}
    write_trigger(defn)
    sleeps = []
    r = ct.execute(SITE_ID, cfg=cfg, sleep=sleeps.append)
    assert r["attempts"] == 1
    assert sleeps == []


def test_the_secret_is_in_no_output_table_or_node(cfg, con, site, capsys):
    site.status = 200
    echo = {**with_blocks({"id": "h", "type": "http_request",
                           "url": site.url + "?k={{secrets.TOKEN}}",
                           "headers": {"Authorization": "Bearer {{secrets.TOKEN}}"}},
                          {"id": "e", "type": "emit",
                           "event": {"id": "echo", "title": "{{secrets.TOKEN}}",
                                     "payload": {"h": "{{secrets.TOKEN}}"}}},
                          secrets=[TOKEN]), "id": "echo"}
    write_trigger(echo)
    for action in ("dry-run", "run", "runs", "show"):
        trigger.main(["custom", action, "echo"])
    dump = capsys.readouterr().out
    for table in ("trigger_request", "custom_trigger_run", "custom_trigger_state",
                  "trigger_checkpoint", "trigger_action", "nodes"):
        dump += json.dumps([dict(r) for r in con.execute(f"SELECT * FROM {table}")], default=str)
    assert SENTINEL not in dump
    assert "***" in dump
    # but it was sent: the server saw it in the header
    assert any(SENTINEL in (h.get("Authorization") or "") for h in site.seen_headers)


def test_run_now_refuses_a_trigger_that_is_already_running(cfg, con, site):
    write_trigger(site_health(site.url))
    lock = ct._lock_for(SITE_ID)
    assert lock.acquire(blocking=False)
    try:
        busy = ct.run_now(SITE_ID, cfg=cfg)
    finally:
        lock.release()
    assert busy["status"] == "busy"
    assert busy["ok"] is False
    assert ct.run_now(SITE_ID, cfg=cfg)["status"] == "ok"


def test_summary(cfg, con):
    write_trigger({**SITE_HEALTH, "enabled": True})
    s = ct.summary(SITE_ID, con)
    assert (s["ok"], s["name"], s["enabled"], s["interval"], s["blocks"]) == (
        True, "Site health", True, "5m", 3)
    broken = ct.summary("missing", con)
    assert broken["ok"] is False
    assert "no trigger" in broken["error"]
    write_trigger({"id": "bare", "interval": "1m"})
    assert ct.summary("bare")["name"] == "bare"
    assert ct.summary("bare")["enabled"] is False


# ================================================================ the page
def test_the_template_validates(cfg):
    pytest.importorskip("yaml")
    text = ct.template("my-new")
    assert text.startswith("id: my-new\n")
    res = ct.check_text("my-new", text, cfg)
    assert res["ok"], res
    assert ct.template("../bad").startswith("id: my-trigger\n")


def test_check_text_refuses_what_it_cannot_read(cfg):
    assert ct.check_text("../x", "{}", cfg)["ok"] is False
    too_long = ct.check_text("t", "x" * (ct.TEXT_MAX + 1), cfg)
    assert too_long["ok"] is False
    assert "at most" in too_long["errors"][0]
    assert ct.check_text("t", "{not json: [", cfg)["ok"] is False
    assert ct.check_text("t", "", cfg)["ok"] is False


def test_save_text_writes_only_what_validates(cfg):
    good = json.dumps(with_blocks(emit_block(id="x")) | {"id": "page"})
    res = ct.save_text("page", good, cfg)
    assert res["saved"] is True
    assert res["created"] is True
    assert ct.raw_text("page") == good + "\n"
    again = ct.save_text("page", good + "\n", cfg)
    assert again["saved"] and again["created"] is False
    bad = ct.save_text("page", "{}", cfg)
    assert bad["saved"] is False
    assert bad["error"].startswith("not saved: ")
    assert ct.raw_text("page") == good + "\n"


def test_raw_text_of_a_missing_or_unsafe_trigger_is_empty(brain_home):
    assert ct.raw_text("missing") == ""
    assert ct.raw_text("../x") == ""


def test_detail_of_a_trigger_and_of_a_missing_one(cfg, con, site):
    assert ct.detail("missing", con) == {"ok": False, "error": "no trigger missing"}
    write_trigger(site_health(site.url))
    ct.execute(SITE_ID, cfg=cfg)
    d = ct.detail(SITE_ID, con)
    assert d["ok"] and d["id"] == SITE_ID
    assert d["state"]["last_status"] == "ok"
    assert len(d["runs"]) == 1
    assert d["checkpoint"] == ""
    assert "text" in d and "errors" in d


# ================================================================ cli
def cli(capsys, *argv) -> tuple[int, dict]:
    code = trigger.main(["custom", *argv])
    return code, json.loads(capsys.readouterr().out)


def test_list_shows_every_trigger(cfg, capsys):
    write_trigger(SITE_HEALTH)
    write_trigger(with_blocks(GOOD_EMIT) | {"id": "second"})
    code, listed = cli(capsys, "list")
    assert code == 0
    assert {t["id"] for t in listed["triggers"]} == {SITE_ID, "second"}
    assert listed["dir"] == str(ct.triggers_dir())


def test_list_of_no_triggers_is_empty(cfg, capsys):
    code, listed = cli(capsys, "list")
    assert (code, listed["triggers"]) == (0, [])


def test_validate_exits_one_on_a_bad_trigger_and_zero_on_a_good_one(cfg, capsys):
    write_trigger({"id": "broken", "interval": "1s", "blocks": []})
    write_trigger(SITE_HEALTH)
    code, res = cli(capsys, "validate", "broken")
    assert code == 1 and res["ok"] is False
    code, res = cli(capsys, "validate", SITE_ID)
    assert code == 0 and res["ok"] is True
    code, res = cli(capsys, "validate", "missing")
    assert code == 1
    assert "no trigger" in res["errors"][0]


def test_every_action_but_list_needs_an_id(cfg, capsys):
    code, res = cli(capsys, "validate")
    assert code == 1
    assert "needs a trigger id" in res["error"]


def test_an_unknown_action_is_refused_by_the_parser(cfg):
    with pytest.raises(SystemExit):
        trigger.main(["custom", "explode", "x"])


def test_show_prints_the_definition_state_and_verdict(cfg, capsys):
    write_trigger(SITE_HEALTH)
    code, res = cli(capsys, "show", SITE_ID)
    assert code == 0
    assert res["trigger"]["id"] == SITE_ID
    assert res["ok"] is True
    assert res["checkpoint"] == ""
    code, res = cli(capsys, "show", "missing")
    assert code == 1 and "no trigger" in res["error"]


def test_enable_refuses_a_broken_trigger_and_disable_does_not(cfg, capsys):
    write_trigger({"id": "broken", "interval": "1s", "blocks": []})
    code, res = cli(capsys, "enable", "broken")
    assert code == 1
    assert res["error"] == "fix it before enabling it"
    code, res = cli(capsys, "enable", "missing")
    assert code == 1
    code, res = cli(capsys, "disable", "broken")
    assert code == 0 and res["enabled"] is False


def test_enable_and_disable_a_good_trigger(cfg, capsys):
    write_trigger(SITE_HEALTH)
    assert cli(capsys, "enable", SITE_ID)[1]["enabled"] is True
    assert ct.load(SITE_ID)[0]["enabled"] is True
    assert cli(capsys, "disable", SITE_ID)[1]["enabled"] is False
    assert ct.load(SITE_ID)[0]["enabled"] is False


def test_dry_run_works_while_custom_triggers_are_off(cfg, site, capsys):
    trigger.config_path().write_text(json.dumps(
        {**cfg, "custom": {**cfg["custom"], "enabled": False}}), encoding=UTF8)
    write_trigger(site_health(site.url))
    code, res = cli(capsys, "dry-run", SITE_ID)
    assert code == 0 and res["dry_run"] is True


def test_run_refuses_while_custom_triggers_are_off(cfg, site, capsys):
    trigger.config_path().write_text(json.dumps(
        {**cfg, "custom": {**cfg["custom"], "enabled": False}}), encoding=UTF8)
    write_trigger(site_health(site.url))
    code, res = cli(capsys, "run", SITE_ID)
    assert code == 1
    assert "not enabled" in res["error"]


def test_run_creates_a_task_runs_it_and_lists_it_in_runs(cfg, con, site, capsys):
    write_trigger(site_health(site.url))
    code, res = cli(capsys, "run", SITE_ID)
    assert code == 0
    assert len(res["created"]) == 1
    assert len(res["ran"]) == 1
    assert res["ran"][0]["status"] == "done"
    code, res = cli(capsys, "runs", SITE_ID, "--limit", "1")
    assert code == 0
    assert [r["status"] for r in res["runs"]] == ["ok"]


def test_run_that_hits_a_running_trigger_exits_one(cfg, site, capsys):
    write_trigger(site_health(site.url))
    lock = ct._lock_for(SITE_ID)
    lock.acquire()
    try:
        code, res = cli(capsys, "run", SITE_ID)
    finally:
        lock.release()
    assert code == 1
    assert res["status"] == "busy"


# ================================================================ scheduler
def due_trigger(url: str, **extra) -> dict:
    defn = site_health(url) | {"id": "sched", "enabled": True} | extra
    defn["blocks"][2]["event"]["id"] = "sched:{{now}}"
    return defn


def test_an_enabled_trigger_that_never_ran_is_due_and_runs(cfg, con, site):
    write_trigger(due_trigger(site.url))
    ran = []
    res = ct.run_due(cfg, ran.append, con=con)
    assert [r["id"] for r in res] == ["sched"]
    assert len(ran) == 1


def test_a_disabled_trigger_does_not_run(cfg, con, site):
    write_trigger(due_trigger(site.url, enabled=False))
    assert ct.run_due(cfg, lambda rid: None, con=con) == []
    assert ct.get_state(con, "sched") == {}


def test_a_trigger_without_enabled_is_off(cfg, con, site):
    defn = due_trigger(site.url)
    del defn["enabled"]
    write_trigger(defn)
    assert ct.run_due(cfg, lambda rid: None, con=con) == []


def test_a_file_that_does_not_parse_is_noted_and_the_loop_goes_on(cfg, con, site):
    write_trigger(due_trigger(site.url))
    write_raw("bad", "{not json, not yaml: [")
    res = ct.run_due(cfg, lambda rid: None, con=con)
    assert [r["id"] for r in res] == ["sched"]
    assert ct.get_state(con, "bad")["last_status"] == "invalid"


def test_not_due_again_inside_its_interval_and_due_once_it_has_passed(cfg, con, site):
    write_trigger(due_trigger(site.url))
    ran = []
    ct.run_due(cfg, ran.append, con=con)
    assert ct.run_due(cfg, ran.append, con=con) == []
    assert len(ran) == 1
    long_ago = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat(timespec="seconds")
    con.execute("UPDATE custom_trigger_state SET last_run_at=? WHERE trigger_id='sched'",
                (long_ago,))
    con.commit()
    assert ct.is_due(con, ct.load("sched")[0])


def test_is_due_with_an_explicit_clock(cfg, con):
    defn = {"id": "d", "interval": "1m"}
    assert ct.is_due(con, defn) is True
    ct._set_state(con, "d", last_run_at="2026-10-12T00:00:00+00:00")
    start = datetime(2026, 10, 12, tzinfo=timezone.utc)
    assert ct.is_due(con, defn, start + timedelta(seconds=59)) is False
    assert ct.is_due(con, defn, start + timedelta(seconds=60)) is True


def test_a_trigger_already_running_is_not_started_twice(cfg, con, site):
    write_trigger(due_trigger(site.url))
    lock = ct._lock_for("sched")
    lock.acquire()
    try:
        res = ct.run_due(cfg, lambda rid: None, con=con)
        busy = ct.run_now("sched", cfg=cfg, con=con)
    finally:
        lock.release()
    assert [r["status"] for r in res] == ["busy"]
    assert busy["status"] == "busy"
    assert [r["status"] for r in ct.run_due(cfg, lambda rid: None, con=con)] == ["ok"]


def test_one_triggers_crash_does_not_stop_the_others(cfg, con, site, monkeypatch):
    write_trigger(due_trigger(site.url) | {"id": "a-first"})
    write_trigger(due_trigger(site.url) | {"id": "b-second"})
    real = ct.execute

    def flaky(tid, **kw):
        if tid == "a-first":
            raise RuntimeError("kaboom")
        return real(tid, **kw)

    monkeypatch.setattr(ct, "execute", flaky)
    res = ct.run_due(cfg, lambda rid: None, con=con)
    assert [(r["id"], r["status"]) for r in res] == [("a-first", "error"), ("b-second", "ok")]
    assert res[0]["error"] == "kaboom"
    st = ct.get_state(con, "a-first")
    assert st["last_status"] == "error"
    assert "crashed: kaboom" in st["last_error"]


def test_the_runtime_loop_runs_a_pass_and_notes_its_errors(cfg, monkeypatch):
    from brain_api import trigger_runtime as rt
    stop = threading.Event()
    calls = []

    def one_pass(conf, dispatch, con=None):
        calls.append(1)
        stop.set()
        return [{"id": "x", "status": "error", "error": "boom", "created": []}]

    monkeypatch.setattr(ct, "run_due", one_pass)
    rt._custom_loop(cfg, stop, tick=0.01)
    st = rt.status()["custom"]
    assert calls == [1]
    assert st["state"] == "scheduling"
    assert "x: boom" in st["last_error"]


def test_the_runtime_loop_survives_a_pass_that_raises(cfg, monkeypatch):
    from brain_api import trigger_runtime as rt
    stop = threading.Event()

    def explode(conf, dispatch, con=None):
        stop.set()
        raise RuntimeError("pass failed")

    monkeypatch.setattr(ct, "run_due", explode)
    rt._custom_loop(cfg, stop, tick=0.01)
    st = rt.status()["custom"]
    assert st["state"] == "erroring"
    assert "pass failed" in st["last_error"]
