"""GET /api/* — the reads the page and the map make.

Most of these answer 200 even when `ok` is false (an unknown node, a keep_days
that is not a number): the read worked, and what it found is "nothing". The
exceptions are the dashboard views (400 on a bad query, 404 on an unknown view)
and the bundle download (404).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, JSONResponse

import agent_commands
import agents
import b2b
import documents
import brain_query
import custom_trigger
import graph
import trigger
import usage_ingest
import usage_report

from . import commands, payloads, state, trigger_runtime

router = APIRouter(prefix="/api")


@router.get("/graph", tags=["graph"], summary="The whole graph, for the map",
            description="Nodes, edges, and the facet counts the map builds its filters "
                        "from. Read from the neo4j master when one is reachable, else "
                        "from the local sqlite mirror.")
def get_graph():
    return payloads.graph_payload()


@router.get("/views", tags=["dashboard"], summary="The names of the date views")
def views():
    return {"ok": True, "views": brain_query.VIEWS}


@router.get("/assignees", tags=["vocabulary"],
            summary="Every assignee token on an open task, with counts")
def assignees():
    return {"ok": True, "assignees": brain_query.assignees("auto")}


@router.get("/realms", tags=["vocabulary"],
            summary="Every realm in the brain, with two counts each")
def realms():
    # open vocabulary — the UI reads, never ships a list
    return {"ok": True, "realms": brain_query.realms("auto"),
            "default": graph.DEFAULT_REALM}


@router.get("/node", tags=["graph"], summary="One node in full, plus its named neighbours")
def node(id: str = Query("", description="The node id.")):
    return brain_query.node_detail(id)


@router.get("/people", tags=["people"], summary="Person nodes, for the assignee picker")
def people():
    rows = graph.person_rows(graph.connect())
    return {"ok": True, "people": [
        {"id": d["id"], "title": d["title"], "aliases": graph.person_aliases(d)[1:],
         "realm": d.get("realm", "")}
        for d in sorted(rows, key=lambda x: x["title"].lower())]}


@router.get("/capture", tags=["settings"], summary="Is the ledger recording?",
            description="$MEMORY_LEDGER_OFF is per-process and OUTRANKS the "
                        "`<home>/.disabled` marker, so `env_off` tells a client whether "
                        "offering to turn capture back on would be a lie.")
def capture():
    return payloads.capture_payload()


@router.get("/ui-prefs", tags=["settings"],
            summary="The browser's own view state, if it is being synced",
            description="NOT settings: the browser owns it, and storing it here is "
                        "opt-in (`ui_sync`). The blob is opaque on purpose.")
def ui_prefs():
    return {"ok": True, "synced": graph.flag("ui_sync"),
            "path": str(graph.ui_prefs_path()), "prefs": graph.ui_prefs()}


@router.get("/backups", tags=["settings"],
            summary="The snapshots consolidation leaves behind",
            description="Point-in-time copies of the whole graph. Restore one by "
                        "copying it back over graph.db.")
def backups():
    return {"ok": True, "dir": str(graph.backups_dir()),
            "keep_days": graph.BACKUP_KEEP_DAYS, "backups": graph.list_backups()}


@router.get("/doctor", tags=["settings"], summary="The health check, as JSON",
            description="The same report `scripts/doctor.py` prints. `ok` means the "
                        "CALL succeeded; `healthy` is the verdict.")
def doctor():
    return payloads.doctor_payload()


@router.get("/consolidate-preview", tags=["settings"],
            summary="What consolidation WOULD do",
            description="A read: it computes the plan and writes nothing, from the same "
                        "`graph.consolidation_plan` the run itself uses.")
def consolidate_preview(keep_days: str = Query(
        "", description="Leave the last N days of captures loose. Default 2.")):
    return payloads.consolidate_preview(keep_days)


@router.get("/person-duplicates", tags=["people"],
            summary="People who look like the same person",
            description="Read-only: it proposes, and `POST /api/merge-person` acts.")
def person_duplicates():
    return {"ok": True, "duplicates": graph.person_duplicates(graph.connect())}


@router.get("/kinds", tags=["vocabulary"],
            summary="Every props.kind in the brain, with the same two counts")
def kinds():
    return {"ok": True, "kinds": brain_query.kinds("auto"),
            "none_token": brain_query.NO_KIND}


@router.get("/skills", tags=["contracts"],
            summary="The two skill trees and the rule catalogue",
            description="Built from the edges `sync-skill-tree` wrote, never re-derived "
                        "from the files here: two readers of one declaration cannot "
                        "disagree, two parsers of it eventually do.")
def skills():
    con = graph.connect()
    return {"ok": True, "trees": graph.skill_trees(con),
            "rules": graph.rule_catalog(con),
            # processes are realm-scoped: every realm is sent and the page
            # narrows, defaulting to the realm new ones land in
            "processes": graph.process_catalog(con), "realm": graph.current_realm(),
            "node_types": graph.node_type_catalog(con), "areas": graph.area_titles(con)}


@router.get("/bundle", tags=["contracts"], summary="Download an exported bundle",
            response_class=FileResponse,
            responses={200: {"content": {"application/zip": {}}},
                       404: {"description": "No such bundle."}},
            description="`name` is reduced to a bare filename, so it can only name a "
                        "file in `<home>/bundles`.")
def bundle(name: str = Query("", description="The bundle filename.")):
    path = commands.bundle_path(name)
    if not path:
        return JSONResponse({"ok": False, "error": "no such bundle"}, status_code=404)
    return FileResponse(path, media_type="application/zip", filename=path.name)


@router.get("/scripts", tags=["scripts"], summary="The local scripts in <home>/scripts",
            description="Name, size, executable bit, language and the first line that "
                        "says what each one is. The brain runs one only when a trigger links it.")
def scripts():
    return graph.list_scripts()


@router.get("/script", tags=["scripts"], summary="One local script's text",
            description="`name` must be a bare filename in `<home>/scripts`.")
def script(name: str = Query("", description="The script filename.")):
    return graph.read_script(name)


@router.get("/rule", tags=["contracts"], summary="One rule in full")
def rule(id: str = Query("", description="The rule id.")):
    row = graph.connect().execute("SELECT * FROM nodes WHERE id=? AND type=?",
                                  (id, graph.RULE_TYPE)).fetchone()
    return {"ok": bool(row), "rule": graph._node_dict(row) if row else None}


@router.get("/rule-proposals", tags=["contracts"],
            summary="Learned-rule proposals awaiting a decision",
            description="What the learned-rules detector found, never applied: a proposal "
                        "is not a rule until accepted. Budgeted. Readable while the "
                        "detector is off.")
def rule_proposals(status: str = Query("proposed", description="proposed | accepted | "
                                                               "rejected | all."),
                   budget: int = Query(graph.READ_BUDGET, ge=200, le=20000)):
    if status not in ("proposed", "accepted", "rejected", "all"):
        status = "proposed"
    return graph.api_rule_proposals(graph.connect(), status=status, budget=budget)


@router.get("/settings", tags=["settings"],
            summary="Stored preferences, and what is actually in effect",
            description="They differ whenever an environment variable overrides the "
                        "file, so `env` names every key the shell has taken over.")
def settings():
    return payloads.settings_payload()


@router.get("/timezones", tags=["settings"], summary="Every IANA zone name, from the stdlib")
def timezones():
    return {"ok": True, "zones": payloads.timezones()}


@router.get("/status", tags=["settings"],
            summary="Where the data lives and whether the master is reachable",
            description="The neo4j password is NEVER sent to the browser; this reports "
                        "`password_set` only.")
def status():
    return payloads.status_payload()


@router.get("/trigger", tags=["trigger"],
            summary="Trigger watchers, config and the newest requests",
            description="What `<home>/trigger.json` configures (never the tokens, only "
                        "whether each is set), what the watchers in this process are "
                        "doing, the request counts by status, the conversations enabled, "
                        "and the newest requests.")
def trigger_status(limit: int = Query(10, ge=1, le=200)):
    return {**trigger.status_payload(limit), "runtime": trigger_runtime.status(),
            "watching": state.TRIGGERS}


@router.get("/b2b", tags=["b2b"], summary="B2B: identity, brokers, peers and messages",
            description="What `<home>/b2b.json` configures (the user id, never the private "
                        "key, only whether one is set), the brokers, the peers, the "
                        "messages sent, the received ones by status, and what the poller "
                        "in this process is doing (docs/b2b.md).")
def b2b_status():
    return {**b2b.status_payload(), "runtime": trigger_runtime.status().get("b2b", {}),
            "watching": state.TRIGGERS}


@router.get("/b2b-conversations", tags=["b2b"],
            summary="B2B: everyone this brain messaged or heard from",
            description="One row per brain, newest conversation first: its name, whether "
                        "it is a connected peer (so it can be written to) and allowed, how "
                        "many messages there are, and the last one. Connected peers with no "
                        "messages yet come last.")
def b2b_conversations():
    return {"ok": True, "conversations": b2b.conversations(b2b.db())}


@router.get("/b2b-thread", tags=["b2b"], summary="B2B: the messages with one brain",
            description="One page of a conversation, oldest first: the latest 20, or the 20 "
                        "sent before `before` (a time, the `next_before` of the page after). "
                        "`has_more` says older ones exist. Each says when it was sent, whether "
                        "a person wrote it (`human`) or a session or script did, whether it is "
                        "`sent`, `staged` (waiting for the peer's box to have room) or "
                        "`failed`, its `files`, and, for a received one, the request it became.")
def b2b_thread(peer: str = Query("", description="The peer's user id."),
               limit: int = Query(b2b.THREAD_PAGE, ge=1, le=200),
               before: str = Query("", description="Only messages sent before this time.")):
    if not peer:
        return {"ok": False, "error": "missing peer"}
    return {"ok": True, "peer_id": peer, **b2b.thread_page(b2b.db(), peer, limit, before)}


@router.get("/agents", tags=["agents"], summary="Agents: the connectors and their models",
            description="What `<home>/agents.json` configures, with the models each "
                        "connector offers now (an Ollama host is asked for its own).")
def agents_status():
    return agents.status_payload()


@router.get("/agent-commands", tags=["agents"], summary="Agents: the slash commands and the plugins",
            description="The skills and plugin commands the composer offers after a `/`, read "
                        "from disk (no model call), and every installed plugin with whether it "
                        "is on. `writes` is a guess from the name and description.")
def agent_commands_list():
    return agent_commands.payload(graph.home_dir())


@router.get("/agent-conversations", tags=["agents"], summary="Agents: every conversation",
            description="Newest first, hidden ones included (`hidden`). `state` is what "
                        "its badge shows: `pending`, `question`, `done`, `error` or `empty`.")
def agent_conversations():
    return {"ok": True, "conversations": agents.conversations(agents.db())}


@router.get("/agent-thread", tags=["agents"], summary="Agents: one conversation's messages",
            description="Oldest first. A reply carries its event log (tools, skills, "
                        "subagents, thinking), the recalled node ids it was given, its "
                        "tokens and its cost, live while it runs.")
def agent_thread(id: str = Query("", description="The conversation id.")):
    if not id:
        return {"ok": False, "error": "missing id"}
    return agents.thread(agents.db(), id) or {"ok": False, "error": f"no conversation {id}"}


@router.get("/custom-triggers", tags=["trigger"],
            summary="Custom triggers: every file in <home>/triggers, with its last run",
            description="Whether custom triggers run here (`active`: the master switch and "
                        "`custom.enabled`), and one row per trigger. A starting template "
                        "for a new one is in `template`.")
def custom_triggers():
    cfg = trigger.load_config()
    con = custom_trigger.db()
    return {"ok": True, "dir": str(custom_trigger.triggers_dir()),
            "active": trigger.source_enabled(cfg, "custom"),
            "secrets": sorted(custom_trigger.secret_values(cfg)),
            "triggers": [custom_trigger.summary(t, con) for t in custom_trigger.list_ids()],
            "template": custom_trigger.template()}


@router.get("/custom-trigger", tags=["trigger"],
            summary="One custom trigger: its file, verdict, state, checkpoint and runs")
def custom_trigger_detail(id: str = Query("", description="The trigger's id."),
                          limit: int = Query(20, ge=1, le=200)):
    return custom_trigger.detail(id, limit=limit)


@router.get("/trigger-request", tags=["trigger"],
            summary="One request and every action taken for it")
def trigger_request(id: str = Query("", description="The request id (UUID).")):
    return trigger.request_detail(id)


@router.get("/trigger-people", tags=["trigger"],
            summary="Find people on a source by name or email, to allow one",
            description="Asks Jira with the stored token. Reads only: allowing someone is "
                        "`POST /api/trigger-person`.")
def trigger_people(q: str = Query("", description="Part of a name or an email."),
                   source: str = Query("jira", description="Only `jira` for now.")):
    return trigger.find_people(trigger.load_config(), source, q)


@router.get("/tasks", tags=["trigger"],
            summary="Every trigger task, newest first, for the Tasks panel",
            description="One row per request: who fired it, from where, what it is "
                        "called, its status and timings. A running task carries "
                        "`progress` (tool calls so far and the latest one), read from "
                        "its session transcript. `text` and `result` are clipped; "
                        "`/api/task` has them whole.")
def tasks(limit: int = Query(200, ge=1, le=trigger.TASKS_MAX)):
    return trigger.tasks_payload(limit)


@router.get("/task", tags=["trigger"],
            summary="One task in full: trigger, command, output, actions, tool calls",
            description="The transcript is budgeted: the newest "
                        f"{trigger.TRANSCRIPT_EVENTS_MAX} events, each field clipped to "
                        f"{trigger.TRANSCRIPT_FIELD_MAX} characters (`truncated`, `of`).")
def task(id: str = Query("", description="The request id (UUID).")):
    return trigger.task_detail(id)


@router.get("/usage-report", tags=["usage"],
            summary="The usage report, rendered: one self-contained HTML page",
            description="Drawn from the brain's `usage-week` nodes (one per ISO week) by "
                        "`scripts/usage_report.py`, with no model call: the same weeks give "
                        "the same page, byte for byte. `weeks` lists every stored week for "
                        "a picker, `selected` the ones in the page. With no week stored the "
                        "page is an empty state, not an error. A malformed week tag is "
                        "`ok: false`.")
def usage_report_page(
        weeks: Optional[int] = Query(None, ge=1, le=520,
                                     description="Only the newest N weeks (after from/to)."),
        from_: str = Query("", alias="from", description="First ISO week, inclusive (YYYY-Www)."),
        to: str = Query("", description="Last ISO week, inclusive (YYYY-Www).")):
    try:
        return usage_report.report(graph.db_path(), weeks, from_ or None, to or None)
    except ValueError as e:
        return {"ok": False, "error": str(e)}


@router.get("/usage-summary", tags=["usage"],
            summary="Estimated spend for the sidebar meter: 5 hours, today, week, month, year, total",
            description="Dollar estimates priced from the local Claude Code logs by "
                        "`scripts/usage_ingest.py` (list price per token, not what a "
                        "subscription charges), summed from the `usage-week` nodes. No model "
                        "call. Asking also starts a detached ingest when the last one is "
                        "over ten minutes old, so the numbers trail the logs by a few minutes.")
def usage_summary():
    usage_ingest.refresh()
    now = datetime.now(usage_ingest.get_tz(None)).replace(tzinfo=None)
    return usage_report.usage_summary(usage_report.load_weeks(graph.db_path()), now)


@router.get("/meta", tags=["vocabulary"], summary="The vocabulary, so no client hard-codes it",
            description="`rels` and `statuses` are CLOSED and enforced on write. "
                        "`realms`, `kinds` and `types` are OPEN: whatever this brain "
                        "actually contains.")
def meta():
    return {
        "ok": True, "rels": graph.EDGE_RELS, "statuses": graph.TASK_STATUSES,
        "realms": brain_query.realms("auto"), "default_realm": graph.DEFAULT_REALM,
        "kinds": brain_query.kinds("auto"), "no_kind_token": brain_query.NO_KIND,
        "types": brain_query.node_types("auto"),
        # The contract shapes (§5.1, §5b.1) come from the engine too. A form that
        # hard-coded the eight headings would be a second definition of the
        # shape, and the one nothing checks.
        "skill_sections": [
            {"field": f, "heading": h, "required": r,
             "generated": f in ("rules_honored", "depends_on")}
            for f, h, r in graph.SKILL_SECTIONS],
        "rule_sections": [{"field": f, "heading": h, "required": r}
                          for f, h, r in graph.RULE_SECTIONS],
        "decl_labels": [{"field": f, "label": lab} for f, lab in graph.DECL_LABELS],
        "rule_sources": sorted(graph.RULE_PRECEDENCE),
        # the declarable vocabulary, not just the types in use: a new skill
        # legitimately writes a type this brain has never held
        "node_types": graph.NODE_TYPES,
        "brain_name": graph.brain_name(), "owner": graph.owner(),
        "writable": state.WRITABLE, "boot": state.BOOT,
    }


@router.get("/suggest-links", tags=["graph"], summary="What a piece of text mentions, and how",
            description="Scans the text for nodes this brain already holds and reads "
                        "the wording around each mention to pick a relation. A "
                        "suggestion engine, not an oracle — let a person confirm each.")
def suggest_links(text: str = Query("", description="The text to scan."),
                  exclude: str = Query("", description="A node id to leave out.")):
    res = graph.suggest_links(graph.connect(), text, 8, exclude)
    return {"ok": True, "count": len(res), "suggestions": res}


@router.get("/search", tags=["graph"], summary="Find a node by title, for the relation picker",
            description="Under two characters it answers with an empty list rather than "
                        "scanning the brain.")
def search(q: str = Query("", description="Title substring. Two characters minimum."),
           limit: str = Query("", description="1–30, default 12.")):
    term = q.strip()
    if len(term) < 2:
        return {"ok": True, "results": []}
    n = max(1, min(30, int(limit))) if limit.isdigit() else 12
    rows = graph.connect().execute(
        "SELECT id,type,title,status FROM nodes"
        " WHERE lower(title) LIKE ? ORDER BY length(title) LIMIT ?",
        (f"%{term.lower()}%", n)).fetchall()
    return {"ok": True, "results": [dict(r) for r in rows]}


@router.get("/document", tags=["agents"], summary="One document the agents wrote, as text",
            description="`path` is relative to `<home>/exports` (the target of a `doc:` link "
                        "in a reply). Markdown, text and HTML come back as `text`; a PDF or an "
                        "image has none and is fetched from `/api/document-file`. A path outside "
                        "`exports`, an unknown type or a file over 5 MB is `ok: false`.")
def document(path: str = Query("", description="Path relative to the area."),
             area: str = Query("exports", description="`exports` (what agents write) or "
                                                      "`attachments` (what the owner attached).")):
    return documents.read(path, area)


@router.get("/document-file", tags=["agents"], summary="One document, raw",
            description="The bytes of a document, for an iframe (HTML, PDF). HTML is served "
                        "with a CSP `sandbox` so it can run its own script but cannot reach "
                        "this server's API.")
def document_file(path: str = Query("", description="Path relative to the area."),
                  area: str = Query("exports", description="`exports` or `attachments`.")):
    try:
        target = documents.resolve(path, area)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
    return FileResponse(target, media_type=documents.media_type(target),
                        headers={"Content-Security-Policy": "sandbox allow-scripts",
                                 "X-Content-Type-Options": "nosniff"})


# ------------------------------------------------------------ date views
# Registered LAST: `/api/{view}` would otherwise shadow every route above.
@router.get("/{view}", tags=["dashboard"], summary="Run one date view",
            responses={400: {"description": "The query was refused."},
                       404: {"description": "No view by that name."}},
            description="The same query layer the CLI uses, over HTTP. `realm` and "
                        "`kind` take comma lists; omitting one means every value.")
def dashboard(
    view: str,
    mine: Optional[str] = Query(None, description="Only rows that need action from the "
                                                  "brain's owner. `0`/`false`/`no` = off."),
    assignee: Optional[str] = Query(None, description="Comma list of assignee tokens. "
                                                      "`unassigned` is one of them."),
    realm: Optional[str] = Query(None, description="Comma list of realms."),
    kind: Optional[str] = Query(None, description="Comma list of props.kind values. "
                                                  "`(none)` selects nodes carrying none."),
    tz: Optional[str] = Query(None, description="IANA name overriding the configured "
                                                "timezone for this call."),
    backend: Optional[str] = Query(None, description="sqlite | neo4j | auto (default)."),
    status: Optional[str] = Query(None, description="range only: any | open | done."),
    date_from: Optional[str] = Query(None, alias="from",
                                     description="range only: ISO date, inclusive."),
    date_to: Optional[str] = Query(None, alias="to",
                                   description="range only: ISO date, inclusive."),
    since_days: Optional[str] = Query(None, description="closed only: how far back."),
):
    if view not in brain_query.VIEWS:
        return JSONResponse({"ok": False, "error": f"unknown view: {view}"},
                            status_code=404)
    # default to 'auto' so the dashboard reflects the SAME store as the map
    # (neo4j master when reachable, local sqlite mirror when offline)
    opts = {"backend": "auto"}
    for key, val in (("assignee", assignee), ("realm", realm), ("kind", kind),
                     ("tz", tz), ("backend", backend), ("status", status),
                     ("date_from", date_from), ("date_to", date_to)):
        if val:
            opts[key] = val
    if mine:
        opts["mine"] = mine not in ("0", "false", "no")
    if since_days and since_days.isdigit():
        opts["since_days"] = int(since_days)
    try:
        return brain_query.run_json(view, **opts)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
