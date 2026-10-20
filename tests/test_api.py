"""The HTTP API, pinned from the outside (docs/api.md).

This suite is the specification of `scripts/serve.py` as a client sees it: the
routes, the envelope, the status codes, the refusals and the SPA fallback. It
starts the real entry point against a THROWAWAY brain and talks to it over a
socket, and never imports the server, so it describes behaviour, not whichever
framework happens to produce it. It was written against the stdlib server and
kept unchanged across the move to FastAPI; that is the point of it.

    python -m pytest tests/test_api.py

One server serves the whole module, so the tests are ordered: a test may rely
on what an earlier one left in the brain only where it says so, and everything
else builds its own nodes. The server brings its own dependencies (see
`scripts/serve.py`); this file needs none.
"""

from __future__ import annotations

import base64
import http.client
import itertools
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Iterator
from pathlib import Path

import pytest

from conftest import GRAPH, HOME_VAR, LOCALHOST, ROOT, SCRIPTS, UTF8, ServeProcess

POSIX = os.name != "nt"
DIST = ROOT / "frontend" / "dist"
API_TIMEOUT_S = 60
SERVER_UP_TIMEOUT_S = 240

# The whole route table. A route that appears or disappears on the server
# without appearing or disappearing here is a contract change nobody wrote down.
GET_ROUTES = [
    "/api/openapi.json", "/api/docs", "/api/graph", "/api/views", "/api/assignees",
    "/api/realms", "/api/node", "/api/people", "/api/capture", "/api/ui-prefs",
    "/api/backups", "/api/doctor", "/api/consolidate-preview",
    "/api/person-duplicates", "/api/kinds", "/api/skills", "/api/bundle",
    "/api/rule", "/api/rule-proposals", "/api/settings", "/api/timezones", "/api/status", "/api/meta",
    "/api/suggest-links", "/api/search", "/api/trigger", "/api/trigger-request", "/api/trigger-people", "/api/b2b",
    "/api/custom-triggers", "/api/custom-trigger",
    "/api/b2b-conversations", "/api/b2b-thread",
    "/api/agents", "/api/voice", "/api/agent-commands", "/api/agent-conversations", "/api/agent-thread", "/api/document", "/api/document-file",
    "/api/scripts", "/api/script", "/api/tasks", "/api/task", "/api/usage-report", "/api/usage-summary",
]
# Open on a --no-write server too: reading aloud changes nothing and calls no model.
OPEN_POST_ROUTES = ["/api/voice-speak"]
POST_ROUTES = [
    "/api/voice-config", "/api/voice-translate",
    "/api/set-due", "/api/add-task", "/api/complete", "/api/set-status",
    "/api/set-assignee", "/api/assign", "/api/merge-person", "/api/add-alias",
    "/api/update-node", "/api/link", "/api/unlink", "/api/propose-idea", "/api/settings",
    "/api/neo4j-config", "/api/neo4j-test", "/api/flush", "/api/prune",
    "/api/capture", "/api/consolidate", "/api/ui-prefs", "/api/backup", "/api/rename-realm",
    "/api/create-rule", "/api/propose-rules", "/api/accept-rule", "/api/reject-rule",
    "/api/create-skill", "/api/create-process",
    "/api/export-skills", "/api/ingest-skills", "/api/export-bundle",
    "/api/upload-bundle", "/api/import-bundle", "/api/delete-contracts",
    "/api/agent-send", "/api/agent-attach", "/api/agent-conversation", "/api/agent-retry", "/api/agent-cancel", "/api/agent-plugin", "/api/agent-config",
    "/api/trigger-gate", "/api/trigger-action", "/api/trigger-config", "/api/trigger-check",
    "/api/trigger-channel", "/api/trigger-person", "/api/trigger-decline", "/api/trigger-reprocess",
    "/api/trigger-cancel", "/api/trigger-scan",
    "/api/custom-trigger", "/api/custom-trigger-dry-run", "/api/custom-trigger-enable",
    "/api/b2b-config", "/api/b2b-identity", "/api/b2b-broker", "/api/b2b-broker-check",
    "/api/b2b-peer", "/api/b2b-allowed", "/api/b2b-send",
    "/api/export-scripts", "/api/upload-scripts", "/api/import-scripts", "/api/create-script",
]
VIEWS = ["agenda", "open", "today", "tomorrow", "week", "closed", "range"]

FULL_SKILL = {
    "when_to_use": "A second machine needs what this one learned.",
    "given": "The learned tree, and a destination directory.",
    "deliverables": "**The folder is self-contained.** Rules sit beside skills.",
    "judged_against": "Ingesting the folder alone restores both halves.",
    "guardrails": "**Must not overwrite a rule the other brain holds.**",
    "reply": "One line: how many moved, and where.",
}
FULL_RULE = {
    "the_rule": "An export carries the rules its skills honor.",
    "why": "A skill whose rules stayed behind is accountable to nothing.",
    "judged_against": "The exported folder has a rules/ entry per honored id.",
}

# routes used by more than one test
META = "/api/meta"
GRAPH_ROUTE = "/api/graph"
NODE = "/api/node"
ADD_TASK = "/api/add-task"
ASSIGN = "/api/assign"
UPDATE_NODE = "/api/update-node"
SETTINGS = "/api/settings"
SKILLS = "/api/skills"
RULE = "/api/rule"
UI_PREFS = "/api/ui-prefs"
TRIGGER = "/api/trigger"
TRIGGER_REQUEST = "/api/trigger-request"
TRIGGER_CONFIG = "/api/trigger-config"
TRIGGER_GATE = "/api/trigger-gate"
TRIGGER_CHANNEL = "/api/trigger-channel"
TRIGGER_PERSON = "/api/trigger-person"
CUSTOM_TRIGGER = "/api/custom-trigger"
B2B = "/api/b2b"
B2B_PEER = "/api/b2b-peer"
B2B_BROKER = "/api/b2b-broker"
B2B_ALLOWED = "/api/b2b-allowed"
B2B_IDENTITY = "/api/b2b-identity"
USAGE_REPORT = "/api/usage-report"
SCRIPTS_ROUTE = "/api/scripts"
SCRIPT = "/api/script"
EXPORT_SCRIPTS = "/api/export-scripts"
UPLOAD_SCRIPTS = "/api/upload-scripts"
IMPORT_SCRIPTS = "/api/import-scripts"
CREATE_SCRIPT = "/api/create-script"
BUNDLE = "/api/bundle"
CONSOLIDATE_PREVIEW = "/api/consolidate-preview"

# the words the server refuses with
MISSING_ID = "missing id"
MISSING_TITLE = "missing title"
MISSING_REQUEST_ID = "missing request_id"
TRUE_OR_FALSE = "true or false"
NOT_FOUND = "not found"
NOT_CONFIGURED = "neo4j not configured"
NO_FILE = "no file was sent"
NO_LONGER_STAGED = "no longer staged"
NOTHING_SELECTED = "nothing was selected"
REALM_SLUG = "realm must be lowercase"
NOT_A_SLUG = "Not A Slug"
COULD_NOT_REACH = "could not reach"
NO_RULE_PROPOSAL = "no rule proposal"
ALREADY_ACCEPTED = "already accepted"

GRACE = "Grace Hopper"
ZIP_MAGIC = b"PK"
JIRA_ACCOUNT = "62f3dd0ce50f2f2a39573991"
PEER_KEY = "yPN6TVrC4gH1oFJpWqrGSGtVkXO8wJ0uD7jM3rC8dFg"     # a real X25519 public key
SLACK_LINK = "https://x.slack.com/archives/C0ABC123/p1"
SLACK_CHANNEL = "C0ABC123"
HELLO_SCRIPT = '#!/usr/bin/env python3\n"""Say hello."""\nprint(1)\n'


# ================================================================== client
class Resp:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status, self.headers, self.raw = status, headers, body

    @property
    def ctype(self) -> str:
        return (self.headers.get("content-type") or "").split(";")[0].strip()

    def json(self) -> dict:
        try:
            return json.loads(self.raw)
        except ValueError:
            return {"__not_json__": self.raw[:200].decode(UTF8, "replace")}


class Api:
    """A running server, over a brain, and the ways a client talks to it."""

    def __init__(self, proc: ServeProcess) -> None:
        self.proc, self.home, self.port = proc, proc.home, proc.port

    def request(self, method: str, path: str, body=None, raw_body: bytes | None = None,
                timeout: float = API_TIMEOUT_S) -> Resp:
        c = http.client.HTTPConnection(LOCALHOST, self.port, timeout=timeout)
        headers = {}
        data = raw_body
        if body is not None:
            data = json.dumps(body).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            c.request(method, path, body=data, headers=headers)
            r = c.getresponse()
            return Resp(r.status, {k.lower(): v for k, v in r.getheaders()}, r.read())
        finally:
            c.close()

    def get(self, path: str, **q) -> Resp:
        if q:
            path += "?" + urllib.parse.urlencode(q)
        return self.request("GET", path)

    def post(self, path: str, body=None, **kw) -> Resp:
        return self.request("POST", path,
                            body={} if body is None and "raw_body" not in kw else body, **kw)

    def env(self) -> dict[str, str]:
        return {**os.environ, HOME_VAR: str(self.home)}

    def cli(self, *args: str) -> dict:
        p = subprocess.run([sys.executable, str(GRAPH), *args], capture_output=True,
                           text=True, encoding=UTF8, errors="replace", env=self.env())
        try:
            return json.loads(p.stdout)
        except ValueError:
            return {"ok": False, "raw": p.stdout[:300], "stderr": p.stderr[:300]}


def assert_refused(r: Resp, status: int = 400, needle: str = "") -> None:
    """The failure envelope: a status, ok:false, and an error worth showing."""
    j = r.json()
    assert r.status == status, f"{r.status} {r.raw[:160]!r}"
    assert j.get("ok") is False, r.raw[:160]
    assert isinstance(j.get("error"), str), r.raw[:160]
    assert needle in j["error"], r.raw[:160]


def free_port() -> int:
    with socket.socket() as s:
        s.bind((LOCALHOST, 0))
        return s.getsockname()[1]


def start_api(home: Path, log: Path, *flags: str) -> Iterator[Api]:
    env = {**os.environ, HOME_VAR: str(home)}
    proc = ServeProcess(home, free_port(), ("--no-build", *flags), env, log)
    try:
        proc.wait_ready()
        api = Api(proc)
        deadline = time.time() + SERVER_UP_TIMEOUT_S
        while api.get(META).status != 200 and time.time() < deadline:
            time.sleep(0.25)
        yield api
    finally:
        proc.stop()


@pytest.fixture(scope="module")
def api(module_brain_home: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Api]:
    """One real serve.py on a free port, over a fresh brain, for the module."""
    yield from start_api(module_brain_home, tmp_path_factory.mktemp("log") / "server.log")


@pytest.fixture(scope="module")
def readonly_api(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Api]:
    home = tmp_path_factory.mktemp("ro-brain")
    yield from start_api(home, tmp_path_factory.mktemp("rolog") / "server.log", "--no-write")


@pytest.fixture(scope="module")
def ada(api: Api) -> dict:
    """The person and the task the read tests look for."""
    person = api.cli("add-node", "--type", "person", "--title", "Ada Lovelace")
    task = api.cli("add-node", "--type", "task", "--title", "Ship the thing",
                   "--props", json.dumps({"assignee": "ada lovelace"}), "--due", "2026-09-30")
    return {"person": person, "task": task}


@pytest.fixture(scope="module")
def grace(api: Api) -> str:
    """The id of a person the write tests assign work to."""
    return api.cli("add-node", "--type", "person", "--title", GRACE)["node"]["id"]


@pytest.fixture
def task(api: Api, grace: str) -> dict:
    """A fresh task for one test: its id, the add-task answer and the person."""
    r = api.post(ADD_TASK, {
        "title": "Write the spec", "body": "b", "due": "2026-10-01",
        "assignee": "grace hopper", "priority": "P2",
        "links": [{"id": grace, "rel": "about"}, {"id": grace, "rel": "made-up"}, "junk"]})
    assert r.status == 200, r.raw[:160]
    j = r.json()
    return {"id": j["node"]["id"], "answer": j, "response": r, "person": grace}


# =========================================================== the envelope
def test_an_api_answer_is_json(api: Api) -> None:
    r = api.get(META)
    assert r.ctype == "application/json"
    assert r.json().get("ok") is True


def test_an_api_answer_is_never_cached(api: Api) -> None:
    """The map polls, and a cached poll is a frozen map."""
    assert api.get(META).headers.get("cache-control") == "no-store"


def test_the_app_is_never_cached_either(api: Api) -> None:
    assert api.get("/").headers.get("cache-control") == "no-store"


def test_an_unknown_get_under_api_is_a_json_404_not_the_app(api: Api) -> None:
    r = api.get("/api/no-such-view")
    assert_refused(r, 404, "unknown view: no-such-view")
    assert r.ctype == "application/json"


def test_an_unknown_post_is_a_json_404(api: Api) -> None:
    assert_refused(api.post("/api/no-such-route", {}), 404, NOT_FOUND)


def test_a_post_to_a_read_only_route_is_a_404_not_a_405(api: Api) -> None:
    assert_refused(api.post(GRAPH_ROUTE, {}), 404, NOT_FOUND)


def test_a_get_to_a_write_only_route_is_an_unknown_view(api: Api) -> None:
    assert_refused(api.get(ADD_TASK), 404, "unknown view: add-task")


@pytest.mark.parametrize("raw", [b"{not json", b"[1, 2]"], ids=["not-json", "not-an-object"])
def test_a_body_that_is_not_a_json_object_is_refused_not_a_crash(api: Api, raw: bytes) -> None:
    r = api.post(ADD_TASK, raw_body=raw)
    assert r.status == 400, r.raw[:80]
    assert r.json().get("ok") is False


def test_an_empty_body_is_an_empty_object_refused_on_its_merits(api: Api) -> None:
    assert_refused(api.post(ADD_TASK, raw_body=b""), 400, MISSING_TITLE)


def test_a_field_of_the_wrong_type_is_refused_never_stored_as_its_repr(api: Api) -> None:
    r = api.post(ADD_TASK, {"title": 123})
    assert r.status == 400, r.raw[:80]
    assert r.json().get("ok") is False


# ============================================================= the docs
@pytest.fixture(scope="module")
def openapi(api: Api) -> dict:
    return api.get("/api/openapi.json").json()


def test_the_openapi_document_is_served(api: Api) -> None:
    r = api.get("/api/openapi.json")
    assert r.status == 200
    assert r.ctype == "application/json"


def test_the_document_is_openapi_3(openapi: dict) -> None:
    assert str(openapi.get("openapi", "")).startswith("3.")


def test_every_read_route_is_in_the_document(openapi: dict) -> None:
    # The document and its viewer are served (checked below) but are not
    # endpoints OF the API, so the document does not have to list itself.
    self_describing = {"/api/openapi.json", "/api/docs"}
    paths = openapi.get("paths", {})
    missing = [p for p in GET_ROUTES
               if p not in self_describing and "get" not in paths.get(p, {})]
    assert not missing, ", ".join(missing)


def test_every_write_route_is_in_the_document(openapi: dict) -> None:
    paths = openapi.get("paths", {})
    missing = [p for p in POST_ROUTES + OPEN_POST_ROUTES if "post" not in paths.get(p, {})]
    assert not missing, ", ".join(missing)


def test_the_dashboard_views_are_the_one_templated_route(openapi: dict) -> None:
    paths = openapi.get("paths", {})
    assert "get" in paths.get("/api/{view}", {}), [p for p in paths if "{" in p]


def test_the_document_lists_nothing_this_suite_has_never_heard_of(openapi: dict) -> None:
    known = set(GET_ROUTES) | set(POST_ROUTES) | set(OPEN_POST_ROUTES) | {"/api/{view}"}
    extra = sorted(p for p in openapi.get("paths", {}) if p not in known)
    assert not extra, ", ".join(extra)


def test_a_browsable_ui_is_served_over_it(api: Api) -> None:
    r = api.get("/api/docs")
    assert r.status == 200
    assert r.ctype == "text/html"
    assert b"swagger" in r.raw.lower()


def test_the_ui_points_at_the_document_on_the_same_port(api: Api) -> None:
    assert b"/api/openapi.json" in api.get("/api/docs").raw


# ============================================================ the SPA
@pytest.fixture(scope="module")
def index_html() -> bytes:
    return (DIST / "index.html").read_bytes()


def test_root_is_the_built_index(api: Api, index_html: bytes) -> None:
    r = api.get("/")
    assert r.status == 200
    assert r.ctype == "text/html"
    assert r.raw == index_html


def test_a_deep_link_falls_back_to_the_index(api: Api, index_html: bytes) -> None:
    r = api.get("/dashboard/deep/link")
    assert r.status == 200
    assert r.raw == index_html


def test_a_built_asset_is_served_as_itself(api: Api) -> None:
    r = api.get("/assets/app.js")
    assert r.status == 200
    assert r.raw == (DIST / "assets" / "app.js").read_bytes()


def test_scripts_are_served_with_a_type_a_browser_will_execute(api: Api) -> None:
    assert api.get("/assets/app.js").ctype == "text/javascript"


def test_css_is_served_as_css(api: Api) -> None:
    assert api.get("/assets/app.css").ctype == "text/css"


@pytest.mark.parametrize("path", [
    "/../../scripts/graph.py", "/%2e%2e/%2e%2e/scripts/graph.py",
    "/assets/../../../scripts/graph.py"])
def test_a_path_never_reads_outside_the_build(api: Api, path: str) -> None:
    r = api.get(path)
    assert b"memory-ledger graph engine" not in r.raw
    assert r.status in (200, 404)


# ============================================================== reads
@pytest.fixture(scope="module")
def meta(api: Api, ada: dict) -> dict:
    return api.get(META).json()


@pytest.fixture(scope="module")
def graph(api: Api, ada: dict) -> dict:
    return api.get(GRAPH_ROUTE).json()


@pytest.mark.parametrize("key", [
    "rels", "statuses", "realms", "default_realm", "kinds", "no_kind_token", "types",
    "skill_sections", "rule_sections", "decl_labels", "rule_sources", "node_types",
    "writable", "boot"])
def test_meta_carries(meta: dict, key: str) -> None:
    assert key in meta


def test_meta_says_the_server_is_writable(meta: dict) -> None:
    assert meta.get("writable") is True


def test_the_relation_vocabulary_is_the_engines(meta: dict) -> None:
    assert "relates-to" in meta.get("rels", [])


def test_the_generated_skill_sections_are_flagged(meta: dict) -> None:
    assert {x["field"] for x in meta.get("skill_sections", []) if x.get("generated")} == {
        "rules_honored", "depends_on"}


def test_graph_has_nodes_and_edges(graph: dict) -> None:
    assert isinstance(graph.get("nodes"), list)
    assert isinstance(graph.get("edges"), list)


def test_a_graph_node_carries_what_the_map_draws(graph: dict) -> None:
    assert graph["nodes"]
    assert set(graph["nodes"][0]) >= {"id", "type", "title", "status", "due", "body",
                                      "realm", "kind", "props", "degree"}


def test_graph_meta_has_counts_and_the_boot_token(graph: dict) -> None:
    assert set(graph.get("meta", {})) >= {"nodes", "edges", "by_type", "by_realm", "by_kind",
                                          "boot", "generated", "source", "ego"}


def test_graph_reads_from_the_local_store_with_no_master_configured(graph: dict) -> None:
    assert graph["meta"].get("source") == "sqlite (local)"


def test_graph_has_a_version_token(graph: dict) -> None:
    assert str(graph.get("version", "")).startswith("sqlite-")


def test_graph_boot_matches_meta(graph: dict, meta: dict) -> None:
    assert graph["meta"]["boot"] == meta["boot"]


def test_the_views_are_listed(api: Api) -> None:
    assert api.get("/api/views").json().get("views") == VIEWS


@pytest.mark.parametrize("view", [v for v in VIEWS if v != "range"])
def test_a_view_answers(api: Api, view: str) -> None:
    r = api.get(f"/api/{view}")
    assert r.status == 200, r.raw[:80]
    assert r.json().get("ok") is True


def test_a_bare_range_is_refused_it_needs_both_ends(api: Api) -> None:
    assert_refused(api.get("/api/range"), 400, "range needs date_from and date_to")


def test_range_takes_from_and_to_and_finds_the_task_due_in_it(api: Api, ada: dict) -> None:
    r = api.get("/api/range", **{"from": "2026-01-01", "to": "2026-12-31"})
    assert r.status == 200, r.raw[:120]
    assert r.json().get("ok"), r.raw[:120]
    assert "Ship the thing" in r.raw.decode(UTF8), r.raw[:120]


def test_a_bad_query_to_a_view_is_a_400_envelope(api: Api) -> None:
    assert_refused(api.get("/api/range", **{"from": "2026-01-01", "to": "oops"}), 400)


def test_junk_and_unknown_view_params_are_ignored(api: Api) -> None:
    assert api.get("/api/open", mine="1", since_days="x", nonsense="1").status == 200


def test_assignees_are_open_task_counts_per_person(api: Api, ada: dict) -> None:
    a = api.get("/api/assignees").json()
    assert a.get("ok"), str(a)[:120]
    assert a.get("assignees", {}).get("ada lovelace") == 1, str(a)[:120]


def test_realms_come_with_the_default(api: Api) -> None:
    rl = api.get("/api/realms").json()
    assert rl.get("ok")
    assert "realms" in rl
    assert "default" in rl


def test_kinds_come_with_the_none_token(api: Api) -> None:
    k = api.get("/api/kinds").json()
    assert k.get("ok")
    assert "kinds" in k
    assert "none_token" in k


def test_people_lists_the_person_with_id_aliases_and_realm(api: Api, ada: dict) -> None:
    p = api.get("/api/people").json()
    found = [x for x in p.get("people", []) if x["title"] == "Ada Lovelace"]
    assert len(found) == 1, str(p)[:120]
    assert set(found[0]) == {"id", "title", "aliases", "realm"}


def test_node_detail_is_the_node_flat_with_its_edges(api: Api, graph: dict) -> None:
    task = next(n for n in graph["nodes"] if n["title"] == "Ship the thing")
    d = api.get(NODE, id=task["id"]).json()
    assert d.get("ok"), str(d)[:120]
    assert d.get("id") == task["id"]
    assert isinstance(d.get("edges"), list)


def test_an_unknown_node_is_ok_false_still_a_200(api: Api) -> None:
    r = api.get(NODE, id="nope_nothing")
    assert r.status == 200
    assert r.json().get("ok") is False


def test_no_id_at_all_is_ok_false_too(api: Api) -> None:
    r = api.get(NODE)
    assert r.status == 200
    assert r.json().get("ok") is False


def test_capture_state(api: Api) -> None:
    c = api.get("/api/capture").json()
    assert set(c) >= {"enabled", "marker", "marker_present", "env_off", "source"}, str(c)[:120]
    assert c["enabled"] is True


def test_ui_prefs(api: Api) -> None:
    u = api.get(UI_PREFS).json()
    assert u.get("ok")
    assert set(u) >= {"synced", "path", "prefs"}


def test_backups(api: Api) -> None:
    b = api.get("/api/backups").json()
    assert b.get("ok")
    assert set(b) >= {"dir", "keep_days", "backups"}


def test_doctor_the_call_worked_and_health_is_its_own_field(api: Api) -> None:
    doc = api.get("/api/doctor").json()
    assert doc.get("ok") is True, str(doc)[:120]
    assert isinstance(doc.get("healthy"), bool)


def test_consolidate_preview_is_a_dry_run(api: Api) -> None:
    cp = api.get(CONSOLIDATE_PREVIEW).json()
    assert cp.get("ok"), str(cp)[:120]
    assert cp.get("dry_run") is True
    assert cp.get("keep_days") == 2


def test_consolidate_preview_reads_keep_days(api: Api) -> None:
    assert api.get(CONSOLIDATE_PREVIEW, keep_days="5").json().get("keep_days") == 5


def test_consolidate_preview_clamps_keep_days(api: Api) -> None:
    assert api.get(CONSOLIDATE_PREVIEW, keep_days="9999").json().get("keep_days") == 365


def test_a_non_number_keep_days_is_ok_false_still_a_200(api: Api) -> None:
    r = api.get(CONSOLIDATE_PREVIEW, keep_days="abc")
    assert r.status == 200
    assert r.json().get("ok") is False


def test_person_duplicates(api: Api) -> None:
    assert isinstance(api.get("/api/person-duplicates").json().get("duplicates"), list)


def test_skills_carries_both_trees_and_the_catalogs(api: Api) -> None:
    sk = api.get(SKILLS).json()
    assert sk.get("ok")
    assert set(sk) >= {"trees", "rules", "processes", "realm", "node_types", "areas"}, \
        str(sorted(sk))[:160]


def test_an_unknown_rule_is_ok_false_with_rule_null(api: Api) -> None:
    r = api.get(RULE, id="rule_nothing")
    assert r.status == 200
    assert r.json() == {"ok": False, "rule": None}


def test_settings_are_stored_env_and_in_effect(api: Api) -> None:
    st = api.get(SETTINGS).json()
    assert st.get("ok")
    assert set(st) >= {"writable", "keys", "settings", "env", "path", "effective", "capture"}


def test_settings_effective_carries_the_resolved_values(api: Api) -> None:
    st = api.get(SETTINGS).json()
    assert set(st.get("effective", {})) >= {
        "tz", "tz_source", "system_tz", "now", "realm", "owner", "owner_source", "backend",
        "backends", "autosync", "map_poll_seconds", "dash_refresh_seconds"}


def test_timezones_come_from_the_stdlib(api: Api) -> None:
    tz = api.get("/api/timezones").json()
    assert tz.get("ok")
    assert "UTC" in tz.get("zones", [])


def test_status(api: Api) -> None:
    status = api.get("/api/status").json()
    assert status.get("ok")
    assert set(status) >= {"writable", "boot", "db", "home", "nodes", "edges",
                           "pending_ops", "loose_captures", "neo4j"}


def test_status_never_sends_the_password(api: Api) -> None:
    neo4j = api.get("/api/status").json().get("neo4j", {})
    assert "password" not in neo4j
    assert "password_set" in neo4j


def test_suggest_links(api: Api, ada: dict) -> None:
    sl = api.get("/api/suggest-links", text="Ship the thing").json()
    assert sl.get("ok")
    assert sl.get("count") == len(sl.get("suggestions", []))


def test_a_one_letter_search_is_empty_not_a_scan(api: Api) -> None:
    assert api.get("/api/search", q="S").json() == {"ok": True, "results": []}


def test_search_is_case_insensitive_on_the_title(api: Api, ada: dict) -> None:
    sr = api.get("/api/search", q="ship").json()
    assert any(x["title"] == "Ship the thing" for x in sr.get("results", []))


def test_search_returns_id_type_title_status(api: Api, ada: dict) -> None:
    sr = api.get("/api/search", q="ship").json()
    assert sr["results"]
    assert set(sr["results"][0]) == {"id", "type", "title", "status"}


def test_search_limit_is_clamped_to_at_least_one(api: Api, ada: dict) -> None:
    assert len(api.get("/api/search", q="th", limit="0").json()["results"]) <= 1


def test_a_missing_bundle_is_a_404(api: Api) -> None:
    assert_refused(api.get(BUNDLE, name="nothing.zip"), 404, "no such bundle")


def test_a_bundle_name_pointing_out_of_the_bundle_dir_serves_nothing(api: Api) -> None:
    assert_refused(api.get(BUNDLE, name="../graph.db"), 404)


# ============================================================= writes
def test_add_task_needs_a_title(api: Api) -> None:
    assert_refused(api.post(ADD_TASK, {}), 400, MISSING_TITLE)


def test_add_task_needs_a_known_type(api: Api) -> None:
    assert_refused(api.post(ADD_TASK, {"title": "x", "type": "note"}), 400,
                   "type must be task or subtask")


def test_add_task_writes_the_node(task: dict) -> None:
    assert task["response"].json().get("ok"), task["response"].raw[:120]


def test_a_task_is_marked_as_coming_from_the_web_ui(task: dict) -> None:
    assert task["answer"]["node"]["props"].get("source") == "web-ui"


def test_a_task_keeps_its_props(task: dict) -> None:
    assert task["answer"]["node"]["props"].get("priority") == "P2"


def test_a_task_is_open_with_its_due_date(task: dict) -> None:
    node = task["answer"]["node"]
    assert node["status"] == "open"
    assert node["due"].startswith("2026-10-01")


def test_the_kept_links_are_applied(task: dict) -> None:
    assert task["answer"].get("links") == [{"id": task["person"], "rel": "about"}]


def test_an_unknown_relation_is_reported_back_not_applied(task: dict) -> None:
    assert task["answer"].get("rejected", [{}])[0].get("error") == "unknown relation"


@pytest.mark.parametrize(("route", "body", "needle"), [
    ("/api/set-due", {}, MISSING_ID),
    ("/api/set-status", {"status": "open"}, MISSING_ID),
    ("/api/set-assignee", {}, MISSING_ID),
    (ASSIGN, {}, MISSING_ID),
    (UPDATE_NODE, {}, MISSING_ID),
    ("/api/complete", {}, MISSING_ID),
], ids=lambda v: v if isinstance(v, str) and v.startswith("/") else "")
def test_a_write_needs_an_id(api: Api, route: str, body: dict, needle: str) -> None:
    assert_refused(api.post(route, body), 400, needle)


def test_set_due(api: Api, task: dict) -> None:
    r = api.post("/api/set-due", {"id": task["id"], "due": "2026-11-02"})
    assert r.json() == {"ok": True, "id": task["id"], "due": r.json().get("due")}, r.raw[:120]
    assert r.json()["due"].startswith("2026-11-02")


def test_set_status_needs_a_known_status(api: Api, task: dict) -> None:
    assert_refused(api.post("/api/set-status", {"id": task["id"], "status": "nah"}), 400,
                   "status must be one of open, doing")


def test_set_status(api: Api, task: dict) -> None:
    r = api.post("/api/set-status", {"id": task["id"], "status": "waiting",
                                     "waiting_on": "Grace"})
    assert r.status == 200, r.raw[:120]
    assert r.json().get("ok")


def test_set_assignee(api: Api, task: dict) -> None:
    assert api.post("/api/set-assignee", {"id": task["id"], "assignee": "x"}).json().get("ok")


def test_assign_refuses_a_node_that_is_not_there(api: Api, task: dict) -> None:
    assert_refused(api.post(ASSIGN, {"id": task["id"], "person_id": "person_nope"}), 400,
                   "no node person_nope")


def test_assign_refuses_a_node_that_is_not_a_person(api: Api, task: dict) -> None:
    assert_refused(api.post(ASSIGN, {"id": task["id"], "person_id": task["id"]}), 400,
                   "not a person")


def test_assign_writes_the_edge_and_the_assignee_string(api: Api, task: dict) -> None:
    r = api.post(ASSIGN, {"id": task["id"], "person_id": task["person"]})
    assert r.status == 200, r.raw[:120]
    assert r.json().get("ok")
    d = api.get(NODE, id=task["id"]).json()
    assert any(e["rel"] == "assigned-to" for e in d.get("edges", []))
    assert d["props"]["assignee"] == GRACE


def test_an_empty_person_unassigns_and_drops_the_edge(api: Api, task: dict) -> None:
    api.post(ASSIGN, {"id": task["id"], "person_id": task["person"]})
    api.post(ASSIGN, {"id": task["id"], "person_id": ""})
    edges = api.get(NODE, id=task["id"]).json().get("edges", [])
    assert not any(e["rel"] == "assigned-to" for e in edges)


@pytest.mark.parametrize(("body", "needle"), [
    ({}, "nothing to update"),
    ({"title": "  "}, "title cannot be empty"),
    ({"priority": "urgent"}, "priority must be one of"),
    ({"due": "friday"}, "due must be YYYY-MM-DD"),
], ids=["nothing", "blank-title", "bad-priority", "bad-due"])
def test_update_node_refuses(api: Api, task: dict, body: dict, needle: str) -> None:
    assert_refused(api.post(UPDATE_NODE, {"id": task["id"], **body}), 400, needle)


def test_update_node_and_an_in_place_edit_moves_the_graph_version_token(
        api: Api, task: dict) -> None:
    before = api.get(GRAPH_ROUTE).json()["version"]
    time.sleep(1.1)
    r = api.post(UPDATE_NODE, {"id": task["id"], "title": "Write the real spec",
                               "priority": "", "due": "2026-12-01", "realm": ""})
    assert r.status == 200, r.raw[:120]
    assert r.json()["node"]["title"] == "Write the real spec"
    assert api.get(GRAPH_ROUTE).json()["version"] != before


def test_link_needs_both_ends(api: Api, task: dict) -> None:
    assert_refused(api.post("/api/link", {"src": task["id"]}), 400, "missing src or dst")


def test_link_needs_a_known_relation(api: Api, task: dict) -> None:
    assert_refused(api.post("/api/link", {"src": task["id"], "dst": task["person"],
                                          "rel": "loves"}), 400, "unknown relation: loves")


def test_link_and_unlink(api: Api, task: dict) -> None:
    body = {"src": task["id"], "dst": task["person"], "rel": "mentions"}
    assert api.post("/api/link", body).json().get("ok")
    assert api.post("/api/unlink", body).json().get("ok")


def test_complete(api: Api, task: dict) -> None:
    r = api.post("/api/complete", {"id": task["id"], "note": "done"})
    assert r.status == 200, r.raw[:120]
    assert r.json().get("ok")


# ------------------------------------------------------------- the idea write
def test_propose_idea_needs_a_title(api: Api) -> None:
    assert_refused(api.post("/api/propose-idea", {"detail": "d"}), 400, MISSING_TITLE)


def test_propose_idea_needs_a_realm_that_is_a_slug(api: Api) -> None:
    assert_refused(api.post("/api/propose-idea", {"title": "x", "realm": NOT_A_SLUG}), 400,
                   REALM_SLUG)


def test_propose_idea_writes_a_proposed_idea_in_the_configured_realm(api: Api) -> None:
    r = api.post("/api/propose-idea", {"title": "Cache the layout", "detail": "Why: speed."})
    j = r.json()
    assert r.status == 200, r.raw[:120]
    assert j.get("ok")
    node = j["node"]
    assert node["type"] == "idea", str(node)[:160]
    assert node["status"] == "proposed"
    assert node["body"] == "Why: speed."
    assert node["realm"] == api.get(SKILLS).json()["realm"]
    hist = api.cli("idea-history", "--id", node["id"])
    assert [h.get("to") for h in hist.get("history", [])] == ["proposed"], str(hist)[:200]


def test_propose_idea_keeps_an_explicit_realm(api: Api) -> None:
    r = api.post("/api/propose-idea", {"title": "Work idea", "realm": "work"})
    assert r.json()["node"]["realm"] == "work", r.raw[:120]


# ---------------------------------------------------------- the people writes
def test_add_alias_needs_both_halves(api: Api, grace: str) -> None:
    assert_refused(api.post("/api/add-alias", {"id": grace}), 400,
                   "id and alias are both required")


def test_add_alias(api: Api, grace: str) -> None:
    assert api.post("/api/add-alias", {"id": grace, "alias": "Prof"}).json().get("ok")


def test_merge_person_refuses_nonsense(api: Api) -> None:
    assert api.post("/api/merge-person", {"canonical": "", "duplicate": ""}).status == 400


def test_merge_person_and_a_merged_person_cannot_be_assigned_to(api: Api) -> None:
    a = api.cli("add-node", "--type", "person", "--title", "Alan Turing")["node"]["id"]
    b = api.cli("add-node", "--type", "person", "--title", "A. Turing",
                "--force-new")["node"]["id"]
    r = api.post("/api/merge-person", {"canonical": a, "duplicate": b})
    assert r.status == 200, r.raw[:160]
    assert r.json().get("ok")
    tid = api.post(ADD_TASK, {"title": "t"}).json()["node"]["id"]
    assert_refused(api.post(ASSIGN, {"id": tid, "person_id": b}), 400, "merged into")


# -------------------------------------------------------- the settings writes
@pytest.mark.parametrize(("body", "needle"), [
    ({}, "nothing to save"),
    ({"tz": "Mars/Olympus"}, "unknown timezone: Mars/Olympus"),
    ({"realm": "Big Realm"}, REALM_SLUG),
    ({"autosync": "yes"}, "autosync must be true or false"),
    ({"backend": "redis"}, "backend must be one of"),
    ({"map_poll_seconds": 0}, "map_poll_seconds must be between 1 and 600"),
    ({"dash_refresh_seconds": "soon"}, "dash_refresh_seconds must be a whole number"),
    ({"brain_name": "{x}"}, "no braces"),
    ({"prompt_recall": "maybe"}, "must be true or false"),
    ({"learned_rules": "maybe"}, "must be true or false"),
], ids=["nothing", "timezone", "realm", "switch", "backend", "poll-range", "poll-type",
        "brain-name-braces", "prompt-recall", "learned-rules"])
def test_a_bad_setting_is_refused(api: Api, body: dict, needle: str) -> None:
    assert_refused(api.post(SETTINGS, body), 400, needle)


def test_a_valid_save_answers_with_the_whole_settings_payload_and_took(api: Api) -> None:
    r = api.post(SETTINGS, {"tz": "Asia/Jerusalem", "realm": "personal",
                            "owner": GRACE, "ui_sync": True,
                            "map_poll_seconds": "5", "consolidate": None})
    j = r.json()
    assert r.status == 200, r.raw[:120]
    assert j.get("ok")
    assert "effective" in j
    eff = j["effective"]
    assert eff["tz"] == "Asia/Jerusalem", str(eff)[:200]
    assert eff["owner"] == GRACE
    assert eff["map_poll_seconds"] == 5
    assert eff["ui_sync"] is True


def test_the_brain_name_defaults_to_a_word_people_say(api: Api) -> None:
    api.post(SETTINGS, {"brain_name": ""})
    eff = api.get(SETTINGS).json()["effective"]
    assert eff["brain_name"] == "Brain"
    assert eff["brain_name_source"] == "default"


def test_the_brain_name_can_be_set_tidied_and_reset(api: Api) -> None:
    named = api.post(SETTINGS, {"brain_name": "  Jarvis  "}).json()
    assert named["effective"]["brain_name"] == "Jarvis", str(named.get("effective"))[:200]
    assert named["effective"]["brain_name_source"] == "settings"
    # the vocabulary carries it, for the page's title
    assert api.get(META).json()["brain_name"] == "Jarvis"
    api.post(SETTINGS, {"brain_name": ""})
    assert api.get(META).json()["brain_name"] == "Brain"


def test_a_realm_set_applies_to_the_next_write_not_the_next_restart(api: Api) -> None:
    api.post(SETTINGS, {"realm": "personal"})
    assert api.post(ADD_TASK, {"title": "realm probe"}).json()["node"]["realm"] == "personal"
    # and to an idea proposed from the map
    assert api.post("/api/propose-idea", {"title": "realm probe"}).json()["node"]["realm"] == \
        "personal"


@pytest.mark.parametrize(("body", "needle"), [
    ({"old": "personal"}, "both the old and the new realm name"),
    ({"old": "personal", "new": "Big Realm"}, REALM_SLUG),
    ({"old": "nope", "new": "x"}, "no realm named"),
], ids=["needs-both-names", "not-a-slug", "no-such-realm"])
def test_a_realm_rename_is_refused(api: Api, body: dict, needle: str) -> None:
    api.post(SETTINGS, {"realm": "personal"})
    api.post(ADD_TASK, {"title": "keeps the realm alive"})
    assert_refused(api.post("/api/rename-realm", body), 400, needle)


def test_renaming_the_realm_moves_its_nodes_merges_into_an_existing_one_and_goes_back(
        api: Api) -> None:
    api.post(SETTINGS, {"realm": "personal"})
    api.post(ADD_TASK, {"title": "to be renamed"})
    api.post(ADD_TASK, {"title": "to be renamed too"})
    r = api.post("/api/rename-realm", {"old": "personal", "new": "life"})
    j = r.json()
    assert r.status == 200, r.raw[:200]
    assert j.get("moved", 0) >= 2, "renaming the default realm moves its nodes"
    assert j.get("default") == "life", "and follows it in the settings"
    assert j.get("backup", {}).get("ok")
    assert not j.get("merged_into_existing")
    assert api.post(ADD_TASK, {"title": "renamed probe"}).json()["node"]["realm"] == "life", \
        "new work lands under the new name"
    assert "personal" not in api.get("/api/realms").json()["realms"], \
        "nothing is left under the old one"

    api.post(ADD_TASK, {"title": "other realm", "realm": "scratch"})
    j = api.post("/api/rename-realm", {"old": "scratch", "new": "life"}).json()
    assert j.get("ok"), str(j)[:200]
    assert j.get("merged_into_existing") is True, "renaming onto an existing realm merges"
    assert "default" not in j

    back = api.post("/api/rename-realm", {"old": "life", "new": "personal"}).json()
    assert back.get("default") == "personal"


def test_the_ui_prefs_are_stored_and_read_back(api: Api) -> None:
    assert api.post(UI_PREFS, {"prefs": {"tab": "map"}}).json().get("synced") is True
    assert api.get(UI_PREFS).json()["prefs"].get("tab") == "map"


def test_capture_needs_a_boolean(api: Api) -> None:
    assert_refused(api.post("/api/capture", {"enabled": "off"}), 400,
                   "enabled must be true or false")


def test_capture_can_be_switched_off_and_back_on(api: Api) -> None:
    off = api.post("/api/capture", {"enabled": False}).json()
    assert off.get("ok"), str(off)[:120]
    assert off["capture"]["enabled"] is False
    assert off["capture"]["source"] == "marker"
    on = api.post("/api/capture", {"enabled": True}).json()
    assert on.get("ok")
    assert on["capture"]["enabled"] is True


def test_neo4j_wants_an_http_url(api: Api) -> None:
    assert_refused(api.post("/api/neo4j-config", {"url": "bolt://x"}), 400,
                   "url must be the neo4j HTTP endpoint")


def test_neo4j_wants_a_password(api: Api) -> None:
    assert_refused(api.post("/api/neo4j-config", {"url": "http://127.0.0.1:1"}), 400,
                   "password required")


def test_testing_an_unconfigured_master_is_refused(api: Api) -> None:
    assert_refused(api.post("/api/neo4j-test", {}), 400, NOT_CONFIGURED)


def test_an_empty_neo4j_url_clears_it(api: Api) -> None:
    r = api.post("/api/neo4j-config", {"url": ""})
    assert r.status == 200, r.raw[:120]
    assert r.json().get("cleared") is True


def test_flush_with_no_master_is_refused_and_still_reports_what_was_pending(api: Api) -> None:
    r = api.post("/api/flush", {})
    assert_refused(r, 400, NOT_CONFIGURED)
    assert "was_pending" in r.json(), r.raw[:120]


def test_backup(api: Api) -> None:
    b = api.post("/api/backup", {"label": "spec"}).json()
    assert b.get("ok"), str(b)[:120]
    assert set(b) >= {"created", "removed", "backups"}


@pytest.mark.parametrize("route", ["/api/prune", "/api/consolidate"])
def test_prune_and_consolidate_refuse_without_confirm(api: Api, route: str) -> None:
    r = api.post(route, {})
    assert_refused(r, 400, "Preview it first")
    assert r.json().get("needs_confirmation") is True


@pytest.mark.parametrize("route", ["/api/prune", "/api/consolidate"])
def test_prune_and_consolidate_refuse_a_bad_keep_days(api: Api, route: str) -> None:
    assert_refused(api.post(route, {"confirm": True, "keep_days": "x"}), 400,
                   "keep_days must be a whole number")


def test_consolidate_runs_when_confirmed(api: Api) -> None:
    r = api.post("/api/consolidate", {"confirm": True, "keep_days": 30})
    assert r.status == 200, r.raw[:160]
    assert r.json().get("ok")


# ------------------------------------------------------------ prompt recall
def test_prompt_recall_is_off_by_default_and_says_where_that_came_from(api: Api) -> None:
    eff = api.get(SETTINGS).json()["effective"]
    assert eff["prompt_recall"] is False, str(eff)[:160]
    assert eff["prompt_recall_source"] == "default"


def test_prompt_recall_can_be_turned_on_and_off_through_the_settings_route(api: Api) -> None:
    r = api.post(SETTINGS, {"prompt_recall": True})
    assert r.status == 200, r.raw[:160]
    assert r.json()["effective"]["prompt_recall"] is True
    assert r.json()["effective"]["prompt_recall_source"] == "settings"
    r = api.post(SETTINGS, {"prompt_recall": False})
    assert r.status == 200, r.raw[:160]
    assert r.json()["effective"]["prompt_recall"] is False


# ------------------------------------------------------------- agents sync
def test_agents_sync_is_off_by_default_and_follows_the_settings_route(api: Api) -> None:
    eff = api.get(SETTINGS).json()["effective"]
    assert eff["agents_sync"] is False and eff["agents_sync_source"] == "default"
    r = api.post(SETTINGS, {"agents_sync": True})
    assert r.status == 200, r.raw[:160]
    assert r.json()["effective"]["agents_sync"] is True
    assert r.json()["effective"]["agents_sync_source"] == "settings"
    r = api.post(SETTINGS, {"agents_sync": False})
    assert r.json()["effective"]["agents_sync"] is False


# ----------------------------------------------------------- learned rules
def test_learned_rules_proposals_over_the_api(api: Api) -> None:
    """The switch, the detector, and a proposal's life: one story, in order."""
    r = api.get("/api/rule-proposals")
    assert r.status == 200, r.raw[:160]
    assert r.json().get("ok")
    assert r.json()["count"] == 0, "proposals list empty"
    assert r.json().get("budget"), "with the read budget"
    eff = api.get(SETTINGS).json()["effective"]
    assert eff["learned_rules"] is False, str(eff)[:160]
    assert eff["learned_rules_source"] == "default"
    r = api.post("/api/propose-rules")
    assert r.status == 200, r.raw[:160]
    assert r.json().get("enabled") is False, "running the detector while off does nothing"
    assert r.json().get("ran") is False
    for sess in ("api-a", "api-b"):
        api.cli("add-node", "--type", "capture", "--title", "never mock the database",
                "--body", "No, never mock the database in integration tests",
                "--props", json.dumps({"role": "user", "session_id": sess}))
    r = api.post(SETTINGS, {"learned_rules": True})
    assert r.status == 200, r.raw[:160]
    assert r.json()["effective"]["learned_rules"] is True
    r = api.post("/api/propose-rules")
    assert r.status == 200, r.raw[:200]
    assert len(r.json()["proposed"]) == 1, "now it proposes the repeated correction"
    pid = r.json()["proposed"][0]["id"]
    assert [p["id"] for p in api.get("/api/rule-proposals").json()["proposals"]] == [pid]
    assert_refused(api.post("/api/accept-rule", {"id": "nope"}), 400, NO_RULE_PROPOSAL)
    assert_refused(api.post("/api/reject-rule", {"id": "nope"}), 400, NO_RULE_PROPOSAL)
    r = api.post("/api/accept-rule", {"id": pid, "title": "Do not mock the database"})
    assert r.status == 200, r.raw[:160]
    assert r.json().get("source") == "learned", "accept makes a learned rule"
    assert r.json().get("precedence") == 50, "at 50"
    assert api.get(RULE, id=r.json()["rule"]).json()["rule"]["props"]["source"] == "learned", \
        "and it is an ordinary rule to read"
    assert_refused(api.post("/api/accept-rule", {"id": pid}), 400, ALREADY_ACCEPTED)
    assert_refused(api.post("/api/reject-rule", {"id": pid}), 400, ALREADY_ACCEPTED)


# ----------------------------------------------- skills, rules, processes, bundles
@pytest.fixture(scope="module")
def rule_id(api: Api) -> str:
    r = api.post("/api/create-rule", {"title": "Spec rule", "sections": FULL_RULE,
                                      "scenario": "write a spec"})
    assert r.status == 200, r.raw[:120]
    assert r.json().get("ok")
    return r.json()["node"]["id"]


def test_a_rule_needs_a_title(api: Api) -> None:
    assert_refused(api.post("/api/create-rule", {}), 400, "a rule needs a title")


def test_a_rule_needs_a_known_source(api: Api) -> None:
    assert_refused(api.post("/api/create-rule", {"title": "x", "source": "made-up"}), 400,
                   "unknown source: made-up")


def test_a_rule_needs_a_way_to_check_it(api: Api) -> None:
    assert_refused(api.post("/api/create-rule", {
        "title": "Half", "sections": {"the_rule": "Do it."}}), 400, "JUDGED AGAINST")


def test_a_created_rule_can_be_read_in_full(api: Api, rule_id: str) -> None:
    assert api.get(RULE, id=rule_id).json()["rule"]["id"] == rule_id


def test_a_skill_needs_a_title(api: Api) -> None:
    assert_refused(api.post("/api/create-skill", {}), 400, "a skill needs a title")


def test_create_skill_with_an_inline_rule_and_declarations_as_list_or_string(
        api: Api, rule_id: str) -> None:
    r = api.post("/api/create-skill", {
        "name": "spec-sweep", "title": "Spec sweep", "description": "d",
        "sections": FULL_SKILL, "reads": ["task", "note"], "writes": "note",
        "rules": [rule_id], "new_rules": [{"title": "Inline rule", "sections": FULL_RULE,
                                           "scenario": "inline"}, "junk"]})
    j = r.json()
    assert r.status == 200, r.raw[:160]
    assert j.get("ok")
    assert len(j.get("created_rules", [])) == 1
    assert j["node"]["props"].get("reads") == "task, note"


def test_a_broken_inline_rule_aborts_the_skill_and_nothing_is_written(api: Api) -> None:
    r = api.post("/api/create-skill", {"name": "nope", "title": "Nope",
                                       "sections": FULL_SKILL, "new_rules": [{}]})
    assert_refused(r, 400)
    assert not api.get(NODE, id="skill_nope").json()["ok"]


def test_a_process_realm_must_be_a_slug(api: Api) -> None:
    assert_refused(api.post("/api/create-process", {"title": "p", "realm": NOT_A_SLUG}), 400,
                   REALM_SLUG)


def test_create_process(api: Api) -> None:
    r = api.post("/api/create-process", {"title": "Spec process", "body": "Step one.",
                                         "trigger": "when asked"})
    assert r.status == 200, r.raw[:120]
    assert r.json().get("ok")


def test_an_export_to_a_bad_kind_is_refused(api: Api) -> None:
    assert_refused(api.post("/api/export-skills", {"kind": "nonsense"}), 400)


def test_export_and_ingest_skills_round_trip(api: Api) -> None:
    exp = api.post("/api/export-skills", {"kind": "learned"}).json()
    assert exp.get("ok"), str(exp)[:120]
    assert exp.get("skills") == 1
    ing = api.post("/api/ingest-skills", {"dir": exp.get("dir", "")}).json()
    assert ing.get("ok"), str(ing)[:120]
    assert ing.get("count", 0) >= 1


def test_a_bundle_goes_out_comes_back_and_is_imported_and_deleted(api: Api) -> None:
    """Export, download, upload, import, delete: the one life of a bundle."""
    exp = api.post("/api/export-bundle", {"skills": ["skill_spec-sweep"], "rules": []}).json()
    assert exp.get("ok"), str(exp)[:160]
    assert exp.get("download", "").startswith("/api/bundle?name=")
    r = api.request("GET", exp["download"])
    assert r.status == 200
    assert r.raw[:2] == ZIP_MAGIC, "the bundle downloads as a zip"
    assert r.ctype == "application/zip"
    assert "attachment" in r.headers.get("content-disposition", "")

    assert_refused(api.post("/api/upload-bundle", {}), 400, NO_FILE)
    assert_refused(api.post("/api/upload-bundle", {"data": "not base64 at all!!"}), 400,
                   "not valid base64")
    up = api.post("/api/upload-bundle", {"filename": "b.zip",
                                         "data": base64.b64encode(r.raw).decode()}).json()
    assert up.get("ok"), str(up)[:160]
    assert up.get("token"), "upload inspects before writing"
    assert up.get("filename") == "b.zip"
    assert_refused(api.post("/api/import-bundle", {
        "token": "../../graph", "select": {"x": "update"}}), 400, NO_LONGER_STAGED)
    assert_refused(api.post("/api/import-bundle", {
        "token": up["token"], "select": {"x": "explode"}}), 400, NOTHING_SELECTED)
    imp = api.post("/api/import-bundle", {
        "token": up["token"], "select": {"skill_spec-sweep": "replace"}}).json()
    assert imp.get("ok"), str(imp)[:160]

    d = api.post("/api/delete-contracts", {"ids": ["skill_spec-sweep"]}).json()
    assert d.get("ok"), str(d)[:160]
    assert d.get("count") == 1


# ================================================================ scripts
def mode_is_executable(path: Path) -> bool:
    return bool(os.stat(path).st_mode & 0o111)


@pytest.fixture
def scripts_dir(api: Api) -> Path:
    """The brain's scripts directory, reset to the two files and the noise the
    tests expect (a dotfile and a directory the listing must skip)."""
    import shutil  # noqa: PLC0415
    d = Path(api.home) / "scripts"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir()
    (d / "hello.py").write_text(HELLO_SCRIPT, encoding=UTF8)
    (d / "hello.py").chmod(0o755)
    (d / "notes.sh").write_text("# Jot a note.\necho hi\n", encoding=UTF8)
    (d / ".hidden").write_text("x", encoding=UTF8)
    (d / "__pycache__").mkdir()
    return d


def test_no_scripts_directory_is_an_empty_list_not_an_error(api: Api) -> None:
    import shutil  # noqa: PLC0415
    shutil.rmtree(Path(api.home) / "scripts", ignore_errors=True)
    ls = api.get(SCRIPTS_ROUTE).json()
    assert ls.get("ok"), str(ls)[:160]
    assert ls.get("count") == 0
    assert ls.get("exists") is False
    assert_refused(api.post(EXPORT_SCRIPTS, {}), 400, "there are no scripts")


def test_scripts_lists_files_not_dotfiles_or_directories(api: Api, scripts_dir: Path) -> None:
    ls = api.get(SCRIPTS_ROUTE).json()
    assert [x["name"] for x in ls["scripts"]] == ["hello.py", "notes.sh"], str(ls)[:200]


def test_a_script_has_the_first_line_that_says_what_it_is_and_its_mode(
        api: Api, scripts_dir: Path) -> None:
    ls = api.get(SCRIPTS_ROUTE).json()
    hello = ls["scripts"][0]
    assert hello["summary"] == "Say hello.", str(hello)[:160]
    assert ls["scripts"][1]["summary"] == "Jot a note."
    if POSIX:
        assert hello["executable"] is True


def test_one_script_reads_in_full(api: Api, scripts_dir: Path) -> None:
    assert "print(1)" in api.get(SCRIPT, name="hello.py").json().get("script", {}).get("text", "")


def test_a_script_name_pointing_out_of_the_directory_reads_nothing(
        api: Api, scripts_dir: Path) -> None:
    assert api.get(SCRIPT, name="../graph.db").json().get("ok") is False


def test_export_scripts_downloads_a_zip(api: Api, scripts_dir: Path) -> None:
    exp = api.post(EXPORT_SCRIPTS, {"names": ["hello.py"]}).json()
    assert exp.get("ok"), str(exp)[:160]
    assert exp.get("scripts") == ["hello.py"]
    assert exp.get("download", "").startswith("/api/bundle?name=scripts-")
    r = api.request("GET", exp["download"])
    assert r.status == 200
    assert r.raw[:2] == ZIP_MAGIC


def test_exporting_an_unknown_script_is_refused(api: Api, scripts_dir: Path) -> None:
    assert_refused(api.post(EXPORT_SCRIPTS, {"names": ["nope.py"]}), 400, "no such script")


def test_an_upload_of_scripts_with_no_file_is_refused(api: Api) -> None:
    assert_refused(api.post(UPLOAD_SCRIPTS, {}), 400, NO_FILE)


def test_scripts_upload_import_replaces_keeping_a_backup(api: Api, scripts_dir: Path) -> None:
    exp = api.post(EXPORT_SCRIPTS, {"names": ["hello.py"]}).json()
    zipped = api.request("GET", exp["download"]).raw
    (scripts_dir / "hello.py").write_text("changed\n", encoding=UTF8)
    up = api.post(UPLOAD_SCRIPTS, {"filename": "s.zip",
                                   "data": base64.b64encode(zipped).decode()}).json()
    assert up.get("ok"), str(up)[:200]
    assert up.get("token")
    assert up["items"][0]["exists"]
    assert up["items"][0]["identical"] is False
    assert (scripts_dir / "hello.py").read_text(encoding=UTF8) == "changed\n", \
        "upload inspects before writing"

    assert_refused(api.post(IMPORT_SCRIPTS, {
        "token": up["token"], "select": {"x": "explode"}}), 400, NOTHING_SELECTED)
    imp = api.post(IMPORT_SCRIPTS, {"token": up["token"],
                                    "select": {"hello.py": "replace"}}).json()
    assert imp.get("ok"), str(imp)[:200]
    assert imp["applied"][0]["action"] == "replaced"
    assert "print(1)" in (scripts_dir / "hello.py").read_text(encoding=UTF8)
    assert (Path(imp["backup"]) / "hello.py").read_text(encoding=UTF8) == "changed\n"
    if POSIX:
        assert mode_is_executable(scripts_dir / "hello.py"), "the executable bit came back"
    assert_refused(api.post(IMPORT_SCRIPTS, {
        "token": up["token"], "select": {"hello.py": "keep"}}), 400, NO_LONGER_STAGED)


def test_a_loose_file_with_an_unsafe_name_is_refused(api: Api) -> None:
    loose = base64.b64encode(b"#!/bin/sh\n# Loose one.\necho\n").decode()
    assert_refused(api.post(UPLOAD_SCRIPTS, {"filename": ".profile", "data": loose}), 400,
                   "cannot be a script name")


def test_a_loose_file_stages_as_one_script_and_lands_as_new(
        api: Api, scripts_dir: Path) -> None:
    loose = base64.b64encode(b"#!/bin/sh\n# Loose one.\necho\n").decode()
    up = api.post(UPLOAD_SCRIPTS, {"filename": "loose.sh", "data": loose}).json()
    assert up.get("ok"), str(up)[:160]
    assert [i["name"] for i in up["items"]] == ["loose.sh"]
    imp = api.post(IMPORT_SCRIPTS, {"token": up["token"],
                                    "select": {"loose.sh": "keep"}}).json()
    assert imp.get("ok")
    assert imp["applied"][0]["action"] == "created"
    assert (scripts_dir / "loose.sh").is_file()


def test_create_script_writes_an_executable_file(api: Api, scripts_dir: Path) -> None:
    made = api.post(CREATE_SCRIPT, {"name": "made.sh",
                                    "text": "#!/bin/sh\n# Made here.\n"}).json()
    assert made.get("ok"), str(made)[:200]
    assert made["action"] == "created"
    assert made["script"]["summary"] == "Made here."
    if POSIX:
        assert mode_is_executable(scripts_dir / "made.sh")


def test_create_script_will_not_overwrite_one_unasked(api: Api, scripts_dir: Path) -> None:
    api.post(CREATE_SCRIPT, {"name": "made.sh", "text": "#!/bin/sh\n# Made here.\n"})
    assert_refused(api.post(CREATE_SCRIPT, {"name": "made.sh", "text": "x"}), 400,
                   "already exists")


def test_create_script_will_not_write_outside_the_directory(api: Api, scripts_dir: Path) -> None:
    assert_refused(api.post(CREATE_SCRIPT, {"name": "../evil.sh", "text": "x"}), 400,
                   "bare filename")


def test_replacing_a_script_keeps_a_backup(api: Api, scripts_dir: Path) -> None:
    api.post(CREATE_SCRIPT, {"name": "made.sh", "text": "#!/bin/sh\n# Made here.\n"})
    again = api.post(CREATE_SCRIPT, {"name": "made.sh", "text": "#!/bin/sh\n",
                                     "replace": True, "executable": False}).json()
    assert again.get("ok"), str(again)[:200]
    assert again["action"] == "replaced"
    assert (Path(again["backup"]) / "made.sh").read_text(encoding=UTF8).startswith(
        "#!/bin/sh\n# Made")
    if POSIX:
        assert not mode_is_executable(scripts_dir / "made.sh")


# ================================================================ triggers
_event_ids = itertools.count(1)


@pytest.fixture
def trigger_rid(api: Api) -> str:
    """A mention, logged the way a watcher would log it: through the engine."""
    snippet = (
        "import sys, json; sys.path.insert(0, %r); import trigger; "
        "ev = {'source': 'slack', 'event_id': 'C1:%d.0', 'scope': 'slack:C1:%d.0', "
        "'requester_id': 'U1', 'is_owner': True, 'text': 'hello', 'meta': {}}; "
        "print(json.dumps(trigger.intake(ev, trigger.load_config())))"
        % (str(SCRIPTS), next(_event_ids), next(_event_ids)))
    p = subprocess.run([sys.executable, "-c", snippet], capture_output=True, text=True,
                       encoding=UTF8, errors="replace", env=api.env())
    rid = json.loads(p.stdout or "{}").get("request_id", "")
    assert rid, p.stderr[-200:]
    return rid


def test_trigger_status_answers_with_no_trigger_json_and_says_so(api: Api) -> None:
    t = api.get(TRIGGER).json()
    assert t.get("ok")
    assert t["config"]["enabled"] is False
    assert t["config"]["exists"] is False


def test_without_a_config_the_watchers_do_not_start_and_say_why(api: Api) -> None:
    t = api.get(TRIGGER).json()
    assert t["runtime"]["running"] is False, str(t.get("runtime"))[:160]
    assert "does not exist" in t["runtime"]["reason"]


def test_trigger_request_returns_it(api: Api, trigger_rid: str) -> None:
    d = api.get(TRIGGER_REQUEST, id=trigger_rid).json()
    assert d.get("ok")
    assert d["request"]["id"] == trigger_rid
    assert d["actions"] == []


def test_an_unknown_request_is_a_successful_read_of_nothing(api: Api) -> None:
    assert api.get(TRIGGER_REQUEST, id="nope").json().get("ok") is False


def test_trigger_status_lists_the_request(api: Api, trigger_rid: str) -> None:
    assert any(r["id"] == trigger_rid for r in api.get(TRIGGER).json()["recent"])


def test_tasks_lists_it_as_a_card_with_the_counts(api: Api, trigger_rid: str) -> None:
    t = api.get("/api/tasks").json()
    assert t.get("ok")
    assert any(r["id"] == trigger_rid and r["name"] == "mention" for r in t["tasks"])
    assert t["total"] >= 1


def test_task_returns_it_in_full_with_its_transcript_state(api: Api, trigger_rid: str) -> None:
    d = api.get("/api/task", id=trigger_rid).json()
    assert d.get("ok")
    assert d["task"]["text"] == "hello"
    assert d["transcript"]["found"] is False


def test_an_unknown_task_is_a_successful_read_of_nothing(api: Api) -> None:
    assert api.get("/api/task", id="nope").json().get("ok") is False


def test_trigger_action_appends_and_the_request_shows_it(api: Api, trigger_rid: str) -> None:
    a = api.post("/api/trigger-action", {"request_id": trigger_rid, "kind": "reply",
                                         "target": "https://x", "detail": "answered"})
    assert a.status == 200, a.raw[:120]
    assert a.json().get("ok")
    kinds = [x["kind"] for x in api.get(TRIGGER_REQUEST, id=trigger_rid).json()["actions"]]
    assert kinds == ["reply"]


@pytest.mark.parametrize(("body", "needle"), [
    ({"kind": "x"}, MISSING_REQUEST_ID),
    ({"request_id": "nope", "kind": "x"}, "no request nope"),
    ({"__rid__": True}, "missing kind"),
], ids=["needs-a-request", "request-must-exist", "needs-a-kind"])
def test_trigger_action_refuses(api: Api, trigger_rid: str, body: dict, needle: str) -> None:
    if body.pop("__rid__", False):
        body["request_id"] = trigger_rid
    assert_refused(api.post("/api/trigger-action", body), 400, needle)


def test_trigger_gate_opens_and_closes_a_conversation(api: Api) -> None:
    def scopes() -> list[str]:
        return [x["scope"] for x in api.get(TRIGGER).json()["enabled_scopes"]]

    g = api.post(TRIGGER_GATE, {"scope": "jira:PROJ-1", "enabled": True})
    assert g.status == 200
    assert g.json().get("enabled") is True
    assert "jira:PROJ-1" in scopes(), "which status then lists"
    api.post(TRIGGER_GATE, {"scope": "jira:PROJ-1", "enabled": False})
    assert "jira:PROJ-1" not in scopes(), "and closes it again"


def test_the_gate_enabled_must_be_a_boolean(api: Api) -> None:
    assert_refused(api.post(TRIGGER_GATE, {"scope": "jira:PROJ-1", "enabled": "yes"}), 400,
                   TRUE_OR_FALSE)


def test_a_malformed_scope_is_refused(api: Api) -> None:
    assert_refused(api.post(TRIGGER_GATE, {"scope": "bogus", "enabled": True}), 400,
                   "scope must look like")


def test_a_github_pr_is_a_conversation_the_gate_keeps(api: Api) -> None:
    assert api.post(TRIGGER_GATE, {"scope": "github:o/r#7", "enabled": True}).json().get("ok")


@pytest.fixture(scope="module")
def trigger_services(api: Api) -> None:
    """Jira and GitHub configured, as the checks below expect."""
    api.post(TRIGGER_CONFIG, {"jira": {"enabled": True, "base_url": "https://j.example",
                                       "email": "a@b.c", "token": "secret-tok"}})
    api.post(TRIGGER_CONFIG, {"github": {"enabled": True, "token": "ghp_secret-gh",
                                         "events": {"ci_failed": {"on": True}}}})


def test_trigger_config_saves_and_never_echoes_a_token(api: Api) -> None:
    c = api.post(TRIGGER_CONFIG, {"jira": {"enabled": True, "base_url": "https://j.example",
                                           "email": "a@b.c", "token": "secret-tok"}})
    assert c.status == 200, c.raw[:200]
    assert c.json()["config"]["jira"]["enabled"] is True
    assert b"secret-tok" not in c.raw
    assert c.json()["config"]["secrets"]["jira.token"]["set"] is True


def test_a_config_save_restarts_the_watchers_on_the_new_config(api: Api) -> None:
    c = api.post(TRIGGER_CONFIG, {"jira": {"enabled": True, "base_url": "https://j.example",
                                           "email": "a@b.c", "token": "secret-tok"}})
    assert c.json().get("watching") is True
    assert c.json()["runtime"]["reason"] == "triggers are switched off"


def test_trigger_status_reads_the_config_back(api: Api, trigger_services: None) -> None:
    assert api.get(TRIGGER).json()["config"]["jira"]["base_url"] == "https://j.example"


def test_a_bad_trigger_config_value_is_refused(api: Api) -> None:
    assert_refused(api.post(TRIGGER_CONFIG, {"jira": {"poll_seconds": 2}}), 400, "from 15")


def test_a_trigger_config_that_is_not_an_object_is_refused(api: Api) -> None:
    assert api.post(TRIGGER_CONFIG, raw_body=b"[1]").status == 400


def test_trigger_config_takes_the_github_section_and_its_events(api: Api) -> None:
    c = api.post(TRIGGER_CONFIG, {"github": {"enabled": True, "token": "ghp_secret-gh",
                                             "events": {"ci_failed": {"on": True}}}})
    assert c.status == 200, c.raw[:200]
    assert c.json()["config"]["github"]["events"]["ci_failed"]["on"] is True
    assert b"ghp_secret-gh" not in c.raw


def test_trigger_config_carries_access_events_and_commands_for_slack_and_jira(api: Api) -> None:
    c = api.post(TRIGGER_CONFIG, {
        "refuse_without_model": False, "refusals": {"not_allowed": "No."},
        "slack": {"allowed_users": ["U1ABC"], "events": {"joined_channel": {"on": True}},
                  "commands": [{"name": "rota", "do": "who is on call for {arg_1}"}]},
        "jira": {"watch_jql": "project = OPS", "events": {"transitioned": {"on": True}}}})
    cfg = c.json().get("config") or {}
    assert c.status == 200, c.raw[:300]
    assert cfg["refuse_without_model"] is False
    assert cfg["refusals"]["not_allowed"] == "No."
    assert cfg["slack"]["allowed_users"] == ["U1ABC"]
    assert cfg["slack"]["events"]["joined_channel"]["on"] is True
    assert cfg["slack"]["commands"][0]["name"] == "rota"
    assert cfg["jira"]["watch_jql"] == "project = OPS"
    assert cfg["jira"]["events"]["transitioned"]["on"] is True


def test_a_command_whose_script_is_missing_is_refused(api: Api) -> None:
    assert_refused(api.post(TRIGGER_CONFIG, {"slack": {"commands": [
        {"name": "x", "action": "script", "script": {"name": "nope.sh"}}]}}),
        400, "no script nope.sh")


def test_trigger_check_knows_github(api: Api, trigger_services: None) -> None:
    k = api.post("/api/trigger-check", {"only": "github", "offline": True}).json()
    assert k.get("ok"), str(k)[:200]
    assert k["target"] == "github"
    assert k["ready"]


def test_trigger_check_checks_one_service(api: Api, trigger_services: None) -> None:
    k = api.post("/api/trigger-check", {"only": "jira", "offline": True}).json()
    assert k.get("ok"), str(k)[:200]
    assert k["target"] == "jira"
    assert k["ready"] is True


def test_trigger_check_names_what_is_missing(api: Api) -> None:
    k = api.post("/api/trigger-check", {"only": "slack", "offline": True}).json()
    assert k["ready"] is False
    assert any("app_token" in p for p in k["problems"])


def test_trigger_check_refuses_an_unknown_service(api: Api) -> None:
    assert_refused(api.post("/api/trigger-check", {"only": "fax"}), 400, "only must be one of")


def test_trigger_channel_opens_a_channel_from_a_link_and_closing_keeps_the_row(api: Api) -> None:
    ch = api.post(TRIGGER_CHANNEL, {"channel": SLACK_LINK, "name": "#ops", "enabled": True})
    assert ch.status == 200, ch.raw[:200]
    assert [c["channel_id"] for c in ch.json()["channels"]["active"]] == [SLACK_CHANNEL]
    ch = api.post(TRIGGER_CHANNEL, {"channel": SLACK_CHANNEL, "enabled": False}).json()
    assert ch.get("ok")
    assert [c["channel_id"] for c in ch["channels"]["removed"]] == [SLACK_CHANNEL], \
        "closing it keeps the row, marked removed"
    assert api.get(TRIGGER).json()["channels"]["removed"][0]["name"] == "ops", \
        "and status lists it that way"


def test_a_channel_must_be_an_id_or_a_link(api: Api) -> None:
    assert_refused(api.post(TRIGGER_CHANNEL, {"channel": "#general", "enabled": True}), 400,
                   "its id")


def test_channel_enabled_must_be_a_boolean(api: Api) -> None:
    assert_refused(api.post(TRIGGER_CHANNEL, {"channel": SLACK_CHANNEL}), 400, TRUE_OR_FALSE)


def test_trigger_person_allows_a_jira_person_and_taking_them_off_keeps_the_row(
        api: Api) -> None:
    pp = api.post(TRIGGER_PERSON, {"source": "jira", "user_id": JIRA_ACCOUNT,
                                   "name": "Isadora", "enabled": True})
    assert pp.status == 200, pp.raw[:200]
    assert [x["name"] for x in pp.json()["people"]["active"]] == ["Isadora"]
    pp = api.post(TRIGGER_PERSON, {"user_id": JIRA_ACCOUNT, "enabled": False}).json()
    assert pp.get("ok")
    assert pp["people"]["removed"][0]["user_id"] == JIRA_ACCOUNT, \
        "taking them off keeps the row, marked removed, Jira by default"
    assert api.get(TRIGGER).json()["people"]["removed"][0]["name"] == "Isadora", \
        "and status lists them that way"


def test_a_person_needs_an_id_their_source_could_have_issued(api: Api) -> None:
    assert_refused(api.post(TRIGGER_PERSON, {"user_id": "Isadora Martini", "enabled": True}),
                   400, "account id")


def test_person_enabled_must_be_a_boolean_here_too(api: Api) -> None:
    assert_refused(api.post(TRIGGER_PERSON, {"user_id": JIRA_ACCOUNT}), 400, TRUE_OR_FALSE)


def test_finding_people_needs_jira_set_up(api: Api) -> None:
    assert api.get("/api/trigger-people", q="isa").json()["ok"] is False


def test_a_declined_request_is_declined_and_closed_to_reprocess_and_cancel(
        api: Api, trigger_rid: str) -> None:
    dcl = api.post("/api/trigger-decline", {"request_id": trigger_rid,
                                            "reason": "It is personal.", "rule": "rule_x"})
    assert dcl.status == 200, dcl.raw[:200]
    assert dcl.json()["status"] == "declined"
    assert dcl.json()["notice"].endswith("It is personal.")
    assert_refused(api.post("/api/trigger-reprocess", {"request_id": trigger_rid}), 400,
                   "can be reprocessed")
    assert_refused(api.post("/api/trigger-cancel", {"request_id": trigger_rid}), 400,
                   "can be cancelled")


def test_a_decline_needs_a_reason(api: Api, trigger_rid: str) -> None:
    assert_refused(api.post("/api/trigger-decline", {"request_id": trigger_rid}), 400,
                   "missing reason")


@pytest.mark.parametrize("route", ["/api/trigger-reprocess", "/api/trigger-cancel"])
def test_reprocess_and_cancel_need_a_request(api: Api, route: str) -> None:
    assert_refused(api.post(route, {}), 400, MISSING_REQUEST_ID)


def test_wake_phrases_scan(api: Api) -> None:
    """Off first, then saved through the config, then dry run and a stranger."""
    assert_refused(api.post("/api/trigger-scan", {"text": "Brain will handle it"}), 400,
                   "wake phrases are off")
    saved = api.post(TRIGGER_CONFIG, {"enabled": True, "meeting": {
        "enabled": True, "phrases": [{"phrase": "{brain} will handle"}]}}).json()
    try:
        assert saved.get("ok"), str(saved)[:200]
        assert saved["config"]["meeting"]["phrases"][0]["phrase"] == "{brain} will handle"
        dry = api.post("/api/trigger-scan", {
            "text": "Ana: who sends it?\nGrace Hopper: Brain will handle it",
            "dry_run": True}).json()
        assert dry.get("ok"), str(dry)[:300]
        assert dry["matched"] == 1, "a dry run says what matches"
        assert dry["matches"][0]["is_owner"], "and who said it"
        assert dry["matches"][0]["runs"]
        stranger = api.post("/api/trigger-scan", {"text": "Ana: brain will handle the rest",
                                                  "title": "probe"}).json()
        assert stranger.get("ok"), str(stranger)[:300]
        assert len(stranger["refused"]) == 1, "a stranger's phrase is logged refused"
        assert not stranger["queued"], "and runs nothing"
    finally:
        api.post(TRIGGER_CONFIG, {"enabled": False, "meeting": {"enabled": False,
                                                                 "phrases": []}})


# ======================================================== custom triggers
@pytest.fixture(scope="module")
def trigger_text(api: Api) -> str:
    """The template, aimed at a closed port on this machine: nothing leaves it,
    and on_error turns the refusal into an event."""
    lst = api.get("/api/custom-triggers").json()
    return (lst["template"].replace("my-trigger", "site")
            .replace("https://example.com/health", "http://127.0.0.1:9/health"))


def test_custom_triggers_list_with_none_yet_a_template_and_says_they_do_not_run(
        api: Api) -> None:
    lst = api.get("/api/custom-triggers").json()
    assert lst.get("ok"), str(lst)[:200]
    assert lst["triggers"] == []
    assert "type: emit" in lst["template"]
    assert lst["active"] is False


def test_a_trigger_that_does_not_validate_is_not_saved(api: Api, trigger_text: str) -> None:
    bad = api.post(CUSTOM_TRIGGER, {"id": "site", "text": trigger_text.replace("5m", "5s")})
    assert_refused(bad, 400, "at least 30s")
    assert api.get(CUSTOM_TRIGGER, id="site").json().get("ok") is False


def test_check_only_validates_and_writes_nothing(api: Api, trigger_text: str) -> None:
    chk = api.post(CUSTOM_TRIGGER, {"id": "site", "text": trigger_text, "check_only": True})
    assert chk.status == 200
    assert chk.json()["valid"]
    invalid = api.post(CUSTOM_TRIGGER, {"id": "site", "text": "id: site\n", "check_only": True})
    assert not invalid.json()["valid"]
    assert api.get("/api/custom-triggers").json()["triggers"] == []


def test_a_valid_trigger_is_saved_as_written_and_can_be_driven(
        api: Api, trigger_text: str) -> None:
    """Save, read back, dry run, enable and break: one trigger's life."""
    r = api.post(CUSTOM_TRIGGER, {"id": "site", "text": trigger_text})
    assert r.status == 200, r.raw[:200]
    assert r.json()["created"] is True
    d = api.get(CUSTOM_TRIGGER, id="site").json()
    assert d.get("ok"), str(d)[:200]
    assert d["text"].strip() == trigger_text.strip(), "its detail has the file"
    assert d["errors"] == [], "the verdict"
    assert d["runs"] == [], "no runs yet"
    assert d["checkpoint"] == ""
    assert_refused(api.post(CUSTOM_TRIGGER, {"id": "../x", "text": trigger_text}), 400,
                   "not a trigger id")
    dry = api.post("/api/custom-trigger-dry-run", {"id": "site"}).json()
    assert dry.get("dry_run") is True, str(dry)[:200]
    assert api.get(CUSTOM_TRIGGER, id="site").json()["runs"] == [], \
        "a dry run leaves no run behind"
    on = api.post("/api/custom-trigger-enable", {"id": "site", "enabled": True})
    assert on.status == 200
    assert "enabled: true" in api.get(CUSTOM_TRIGGER, id="site").json()["text"], \
        "enable rewrites the file"
    assert_refused(api.post("/api/custom-trigger-enable", {"id": "site", "enabled": "yes"}), 400)
    (Path(api.home) / "triggers" / "broken.yaml").write_text(
        "id: broken\ninterval: 1s\n", encoding=UTF8)
    assert_refused(api.post("/api/custom-trigger-enable", {"id": "broken", "enabled": True}),
                   400, "fix it before")


# ===================================================================== b2b
def test_b2b_routes(api: Api) -> None:
    """Identity, brokers, peers, send (docs/b2b.md): one connection's life, in order."""
    t = api.get(B2B).json()
    assert t.get("ok"), "status answers with no b2b.json"
    assert t["config"]["active"] is False
    assert t["peers"] == []
    assert t["config"]["brokers"] == []
    assert "runtime" in t

    i = api.post(B2B_IDENTITY, {})
    me = i.json().get("user_id", "")
    assert i.status == 200, i.raw[:200]
    assert len(me) == 43, "b2b-identity makes a key pair; the id is the public key"
    key = json.loads((Path(api.home) / "b2b.json").read_text(encoding=UTF8))["private_key"]
    t = api.get(B2B)
    assert key.encode() not in t.raw, "and the private key never comes back"
    assert t.json()["config"]["private_key"]["set"] is True
    assert t.json()["config"]["user_id"] == me
    assert_refused(api.post(B2B_IDENTITY, {}), 400, "--force")

    b = api.post(B2B_BROKER, {"url": "http://127.0.0.1:9", "name": "Home"})
    assert b.status == 200, b.raw[:200]
    assert b.json()["config"]["brokers"][0]["id"] == "home"
    b = api.post(B2B_BROKER, {"url": "http://127.0.0.1:8", "name": "Team"})
    assert b.status == 200
    assert len(b.json()["config"]["brokers"]) == 2, "and a second"
    assert_refused(api.post(B2B_BROKER, {"url": "ftp://x"}), 400)
    b = api.post(B2B_BROKER, {"id": "team", "enabled": False})
    assert b.status == 200
    assert b.json()["config"]["brokers"][1]["enabled"] is False
    assert_refused(api.post("/api/b2b-broker-check", {"id": "home"}), 400, COULD_NOT_REACH)
    c = api.post("/api/b2b-config", {"poll_seconds": 120})
    assert c.status == 200, c.raw[:200]
    assert c.json()["config"]["poll_seconds"] == 120
    assert_refused(api.post("/api/b2b-config", {"poll_seconds": 1}), 400, "poll_seconds")

    assert_refused(api.post(B2B_PEER, {"peer_id": PEER_KEY, "enabled": True}), 400,
                   "which broker")
    p = api.post(B2B_PEER, {"peer_id": PEER_KEY, "name": "Ana", "broker_id": "home",
                            "enabled": True})
    assert p.status == 200, p.raw[:200]
    assert p.json()["peers"][0]["name"] == "Ana"
    assert p.json()["peers"][0]["broker_id"] == "home"
    assert [a["user_id"] for a in p.json()["allowed"]] == [PEER_KEY], \
        "connecting a peer allows it to send back"
    assert p.json()["allowed"][0]["is_peer"]
    assert_refused(api.post(B2B_PEER, {"peer_id": PEER_KEY}), 400, TRUE_OR_FALSE)

    a = api.post(B2B_ALLOWED, {"user_id": PEER_KEY, "enabled": False})
    assert a.status == 200, a.raw[:200]
    assert a.json()["allowed"][0]["removed_at"], "b2b-allowed blocks a sender, keeping the row"
    a = api.post(B2B_ALLOWED, {"user_id": PEER_KEY, "name": "Ana", "enabled": True})
    assert a.status == 200
    assert not a.json()["allowed"][0]["removed_at"], "and allows them again"
    assert_refused(api.post(B2B_ALLOWED, {"user_id": "nope", "enabled": True}), 400,
                   "public key")
    assert_refused(api.post(B2B_ALLOWED, {"user_id": PEER_KEY}), 400, TRUE_OR_FALSE)
    assert [x["user_id"] for x in api.get(B2B).json()["allowed"]] == [PEER_KEY], \
        "the status lists who is allowed"
    assert_refused(api.post(B2B_PEER, {"peer_id": me, "broker_id": "home", "enabled": True}),
                   400)

    assert_refused(api.post("/api/b2b-send", {"to": "Ana", "text": "hi"}), 400,
                   COULD_NOT_REACH)
    assert_refused(api.post("/api/b2b-send", {"to": "nobody", "text": "hi"}), 400,
                   "not a connected peer")
    assert_refused(api.post("/api/b2b-send", {"to": "Ana", "text": "hi", "files": ["../x"]}),
                   400, "not a file saved by")
    assert th_ok(api.get(f"/api/b2b-thread?peer={PEER_KEY}&limit=5&before=2020-01-01"))
    assert_refused(api.post("/api/b2b-send", {"to": "Ana", "text": "hi", "human": True}), 400,
                   COULD_NOT_REACH)
    cv = api.get("/api/b2b-conversations").json()
    assert cv.get("ok"), str(cv)[:300]
    assert [(c["peer_id"], c["name"], c["connected"], c["messages"], c["last"])
            for c in cv["conversations"]] == [(PEER_KEY, "Ana", True, 0, None)]
    th = api.get(f"/api/b2b-thread?peer={PEER_KEY}").json()
    assert th.get("ok")
    assert th["messages"] == []
    assert th["peer_id"] == PEER_KEY
    assert_refused(api.get("/api/b2b-thread"), 200, "missing peer")
    p = api.post(B2B_PEER, {"peer_id": PEER_KEY, "enabled": False})
    assert p.status == 200
    assert p.json()["peers"][0]["removed_at"], "b2b-peer disconnects, keeping the row"
    b = api.post(B2B_BROKER, {"id": "team", "remove": True})
    assert b.status == 200
    assert len(b.json()["config"]["brokers"]) == 1, "a broker is removed"


def th_ok(resp) -> bool:
    body = resp.json()
    return body.get("ok") and body["messages"] == [] and body["has_more"] is False


# ============================================================ usage report
USAGE_METRICS = {
    "costUSD": 12.5, "sessions": 3, "turns": 100, "typedPrompts": 4, "activeDays": 2,
    "byModel": {"claude-opus-5": 12.5}, "composition": {"cacheRead": 8.0, "output": 4.5},
    "rateLimits": []}
USAGE_WEEKS = (("2026-W01", "2025-12-29", "2026-01-04"), ("2026-W02", "2026-01-05", "2026-01-11"))


def test_with_no_usage_weeks_the_report_is_an_empty_state_not_an_error(api: Api) -> None:
    r = api.get(USAGE_REPORT).json()
    assert r.get("ok") is True
    assert r["weeks"] == []
    assert r["selected"] == []
    assert "No usage weeks in the brain" in r["html"]


@pytest.fixture(scope="module")
def usage_weeks(api: Api) -> None:
    for tag, frm, to in USAGE_WEEKS:
        made = api.cli("add-node", "--type", "usage-week", "--id", f"usage-week_{tag}",
                       "--title", f"Usage {tag}", "--props",
                       json.dumps({"week": tag, "from": frm, "to": to,
                                   "metrics": USAGE_METRICS}))
        assert made.get("ok") is True, f"a {tag} node is stored: {str(made)[:160]}"


def test_the_report_lists_every_stored_week_and_renders_them_all(
        api: Api, usage_weeks: None) -> None:
    r = api.get(USAGE_REPORT).json()
    assert [w["week"] for w in r["weeks"]] == ["2026-W01", "2026-W02"]
    assert r["selected"] == ["2026-W01", "2026-W02"]
    assert "$25.00" in r["html"]


def test_the_same_weeks_give_the_same_bytes(api: Api, usage_weeks: None) -> None:
    assert api.get(USAGE_REPORT).json()["html"] == api.get(USAGE_REPORT).json()["html"]


def test_weeks_n_keeps_the_newest_n(api: Api, usage_weeks: None) -> None:
    one = api.get(USAGE_REPORT, weeks=1).json()
    assert one["selected"] == ["2026-W02"]
    assert "$12.50" in one["html"]


def test_from_and_to_is_an_inclusive_range(api: Api, usage_weeks: None) -> None:
    rng = api.get(USAGE_REPORT, **{"from": "2026-W01", "to": "2026-W01"}).json()
    assert rng["selected"] == ["2026-W01"]


def test_a_malformed_week_tag_is_refused_with_the_reason(api: Api, usage_weeks: None) -> None:
    bad = api.get(USAGE_REPORT, **{"from": "last week"}).json()
    assert bad.get("ok") is False
    assert "ISO week" in bad.get("error", "")


# ========================================================== read-only mode
# `--no-write` refuses every write, and says so.
def test_read_only_meta_says_so(readonly_api: Api) -> None:
    assert readonly_api.get(META).json().get("writable") is False


def test_read_only_settings_says_so(readonly_api: Api) -> None:
    assert readonly_api.get(SETTINGS).json().get("writable") is False


@pytest.mark.parametrize("route", POST_ROUTES)
def test_read_only_refuses_every_write_route_with_a_403(readonly_api: Api, route: str) -> None:
    assert_refused(readonly_api.post(route, {}), 403, "--no-write")


def test_read_only_an_unknown_post_is_still_a_404(readonly_api: Api) -> None:
    assert_refused(readonly_api.post("/api/nope", {}), 404)


def test_read_only_reads_still_work(readonly_api: Api) -> None:
    assert readonly_api.get(GRAPH_ROUTE).status == 200


def test_a_read_only_server_never_acts_no_trigger_watchers(readonly_api: Api) -> None:
    assert readonly_api.get(TRIGGER).json().get("watching") is False
