#!/usr/bin/env python3
"""Agents: conversations with a model from the brain UI (docs/agents.md).

Every turn is ONE stateless call. Nothing carries a session from one turn to the
next: what the model sees is the message, the last few exchanges the
conversation is set to carry (`context_turns`, 0 by choice), and what `recall`
pulls from the graph for that message. Each finished exchange is written back to
the graph as a `message` node under the conversation's `conversation` node, so
the next turn's recall finds it like any other memory, and so does every other
session.

Connectors are the person's own list in `<home>/agents.json`: each one has a
`kind` (today `claude` or `ollama`) and its own settings, so two Ollama hosts
are two connectors. A conversation can switch connector and model between
turns; each reply records which one wrote it.

Tools are per conversation. Off (the default), Claude runs with read-only
built-in tools and read-only connectors, and nothing allowed beyond them. On,
Claude runs like a trigger session (every tool, no prompts). Either way the
brain itself is read AND written: the graph's commands for Claude, the graph
tools (recall, search, get_node, read_thread, add_note, add_task) for Ollama.

    python3 scripts/agents.py status
    python3 scripts/agents.py run <message-id>     # run one queued reply, in this process
    python3 scripts/agents.py thread <conversation-id> [--limit N]   # the thread, as text
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _platform  # noqa: E402
import _textio  # noqa: E402
import documents  # noqa: E402
import graph  # noqa: E402
import voice  # noqa: E402

CONFIG_NAME = "agents.json"
KINDS = ("claude", "ollama")
STATUSES = ("queued", "running", "done", "question", "error")
PENDING = ("queued", "running")
# The line an agent ends with when it cannot go on without the owner. Hidden in
# the UI; its presence is what turns the conversation's badge into a "?".
QUESTION_MARK = "[[needs-answer]]"
NEXT_RE = re.compile(r"[ \t`]*\[\[next:\s*(.*?)\s*\]\][ \t`]*", re.S)
# What a reply cancelled by the owner fails with; Retry runs it again.
CANCELLED = "cancelled by you"
MAX_TURNS = 10            # the most earlier exchanges a turn may carry
TEXT_MAX = 20_000         # a message, either way
EVENT_MAX = 2_000         # one logged tool input or output
EVENTS_MAX = 200          # logged events per reply
# Recalled nodes of these types are bookkeeping, not memory worth handing a model.
RECALL_SKIP = {"conversation", "agent", "context-event", "capture", "node-type",
               "rule-snapshot", "usage-week"}

CLAUDE_MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5-20251001",
                 "claude-fable-5-1"]
# Read-only Claude: these built-ins and nothing else, every other call denied.
CLAUDE_READ_TOOLS = ["Read", "Grep", "Glob", "WebSearch", "WebFetch", "Skill"]
SCRIPT = Path(__file__).resolve()
# How an agent reads its own conversation back, and reads and writes the brain.
# These run in both tool modes: read-only limits what an agent does OUTSIDE the
# brain, never its memory (the offload rule, CONVENTIONS §0). Read-only Claude
# gets Bash for these commands and nothing else.
THREAD_CMD = f"python3 {SCRIPT} thread"
GRAPH_CMD = f"python3 {SCRIPT.parent / 'graph.py'}"
QUERY_CMD = f"python3 {SCRIPT.parent / 'brain_query.py'}"
BRAIN_CMDS = (THREAD_CMD, GRAPH_CMD, QUERY_CMD)
# Read-only Claude may also look things up through the person's connectors.
# Only these, by exact name: anything that posts, edits, labels or deletes is
# left out, and `dontAsk` denies it.
CLAUDE_READ_MCP = [
    "ToolSearch",
    *(f"mcp__atlassian__{t}" for t in (
        "atlassianUserInfo", "getAccessibleAtlassianResources", "getJiraIssue",
        "searchJiraIssuesUsingJql", "search", "searchConfluence", "getConfluenceContent",
        "getGraphContext", "getGraphObject", "getLoomVideo", "discover", "executeRead")),
    *(f"mcp__claude_ai_Slack__slack_{t}" for t in (
        "read_channel", "read_thread", "read_canvas", "read_file", "read_user_profile",
        "search_public", "search_public_and_private", "search_channels", "search_users",
        "list_channel_members", "get_reactions")),
    *(f"mcp__claude_ai_Gmail__{t}" for t in (
        "get_message", "get_thread", "search_threads", "list_labels", "list_drafts", "get_draft")),
    *(f"mcp__claude_ai_Google_Calendar__{t}" for t in (
        "list_calendars", "list_events", "get_event", "search_events", "suggest_time")),
    *(f"mcp__claude_ai_Google_Drive__{t}" for t in (
        "get_file_metadata", "list_recent_files", "read_file_content", "search_files")),
    *(f"mcp__claude_ai_Claude_Docs__{t}" for t in ("read", "query", "guide")),
    *(f"mcp__claude_ai_Sentry__{t}" for t in (
        "find_organizations", "find_projects", "get_sentry_resource", "search_events",
        "search_issues", "search_sentry_tools")),
]

DEFAULTS: dict[str, Any] = {
    "default_connector": "claude",
    "context_turns": 1,
    "recall_limit": 8,
    "recall_budget": 2600,
    "max_concurrent": 2,
    "connectors": [
        {"id": "claude", "name": "Claude", "kind": "claude", "enabled": True,
         "bin": "claude", "models": CLAUDE_MODELS, "default_model": "claude-sonnet-5-5",
         "timeout_s": 900},
        {"id": "ollama", "name": "Ollama", "kind": "ollama", "enabled": True,
         "url": "http://127.0.0.1:11434", "default_model": "", "timeout_s": 600,
         "max_rounds": 8},
    ],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_conv (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    connector     TEXT NOT NULL DEFAULT '',
    model         TEXT NOT NULL DEFAULT '',
    full_tools    INTEGER NOT NULL DEFAULT 0,
    context_turns INTEGER NOT NULL DEFAULT 1,
    hidden        INTEGER NOT NULL DEFAULT 0,
    draft         TEXT NOT NULL DEFAULT '',    -- the unsent message in the composer
    draft_at      TEXT NOT NULL DEFAULT '',    -- when the draft was last edited (the newest wins)
    node_id       TEXT NOT NULL DEFAULT '',    -- its `conversation` node in the graph
    verbosity     TEXT NOT NULL DEFAULT '',    -- voice.VERBOSITIES, or '' to follow the global one
    tts           TEXT NOT NULL DEFAULT ''     -- 'on' | 'off', or '' to follow the global one
);
CREATE TABLE IF NOT EXISTS agent_msg (
    id            TEXT PRIMARY KEY,
    conv_id       TEXT NOT NULL,
    role          TEXT NOT NULL,                -- user | agent
    text          TEXT NOT NULL DEFAULT '',
    at            TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'done',
    reply_to      TEXT NOT NULL DEFAULT '',     -- an agent reply: the user message it answers
    connector     TEXT NOT NULL DEFAULT '',
    model         TEXT NOT NULL DEFAULT '',
    full_tools    INTEGER NOT NULL DEFAULT 0,
    events        TEXT NOT NULL DEFAULT '[]',   -- tool, skill, subagent and thinking log
    context       TEXT NOT NULL DEFAULT '[]',   -- the recalled node ids the turn was given
    error         TEXT NOT NULL DEFAULT '',
    session_id    TEXT NOT NULL DEFAULT '',
    cost_usd      REAL,                         -- null: not known; 0: free (a local model)
    cost_estimated INTEGER NOT NULL DEFAULT 0,  -- 1 while it is still our estimate
    tokens_in     INTEGER NOT NULL DEFAULT 0,
    tokens_out    INTEGER NOT NULL DEFAULT 0,
    started_at    TEXT NOT NULL DEFAULT '',
    finished_at   TEXT NOT NULL DEFAULT '',
    node_id       TEXT NOT NULL DEFAULT '',     -- the exchange's `message` node
    owner_pid     INTEGER NOT NULL DEFAULT 0,   -- the server process running it
    beat_at       REAL NOT NULL DEFAULT 0,      -- epoch of the owner's last heartbeat
    next_action   TEXT NOT NULL DEFAULT ''      -- the agent's suggested next message
);
CREATE INDEX IF NOT EXISTS agent_msg_conv ON agent_msg(conv_id, at);
CREATE INDEX IF NOT EXISTS agent_msg_status ON agent_msg(status);
"""


class AgentError(Exception):
    """A connector could not be reached, or refused the call."""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(3)}"


# ---------------------------------------------------------------- config
def config_path() -> Path:
    return graph.home_dir() / CONFIG_NAME


def load_config() -> dict[str, Any]:
    """The defaults under `<home>/agents.json`. A file that does not parse is
    reported under `_problem` and the defaults stand."""
    cfg = json.loads(json.dumps(DEFAULTS))
    p = config_path()
    if not p.exists():
        return cfg
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("must hold a JSON object")
    except (OSError, ValueError) as e:
        cfg["_problem"] = f"{p} is not valid JSON: {e}"
        return cfg
    for k, v in raw.items():
        if k in DEFAULTS:
            cfg[k] = v
    if not isinstance(cfg.get("connectors"), list):
        cfg["connectors"] = json.loads(json.dumps(DEFAULTS["connectors"]))
    return cfg


def _clean_connector(c: Any, taken: set[str]) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(c, dict):
        return None, "a connector must be an object"
    kind = str(c.get("kind") or "").strip()
    if kind not in KINDS:
        return None, f"connector kind must be one of {', '.join(KINDS)}"
    name = str(c.get("name") or kind.title()).strip()[:60]
    cid = graph.slugify(str(c.get("id") or name))[:40]
    base, n = cid, 2
    while cid in taken:
        cid, n = f"{base}-{n}", n + 1
    taken.add(cid)
    out: dict[str, Any] = {"id": cid, "name": name, "kind": kind,
                           "enabled": bool(c.get("enabled", True)),
                           "default_model": str(c.get("default_model") or "").strip()[:120]}
    try:
        out["timeout_s"] = max(30, min(7200, int(c.get("timeout_s") or 900)))
    except (TypeError, ValueError):
        return None, "timeout_s must be a number"
    if kind == "claude":
        out["bin"] = str(c.get("bin") or "claude").strip()[:500]
        models = c.get("models") if isinstance(c.get("models"), list) else CLAUDE_MODELS
        out["models"] = [str(m).strip()[:120] for m in models if str(m).strip()][:20]
    else:
        url = str(c.get("url") or "http://127.0.0.1:11434").strip().rstrip("/")[:500]
        if not re.match(r"^https?://", url):
            return None, "an Ollama url must start with http:// or https://"
        out["url"] = url
        try:
            out["max_rounds"] = max(1, min(30, int(c.get("max_rounds") or 8)))
        except (TypeError, ValueError):
            return None, "max_rounds must be a number"
    return out, ""


def save_config(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge `patch` into the file. `connectors`, when given, replaces the list."""
    cfg = load_config()
    if cfg.get("_problem"):
        return {"ok": False, "error": cfg["_problem"]}
    if "connectors" in patch:
        if not isinstance(patch["connectors"], list) or len(patch["connectors"]) > 20:
            return {"ok": False, "error": "connectors must be a list of at most 20"}
        taken: set[str] = set()
        clean = []
        for c in patch["connectors"]:
            cc, err = _clean_connector(c, taken)
            if err:
                return {"ok": False, "error": err}
            clean.append(cc)
        cfg["connectors"] = clean
    for key, lo, hi in (("context_turns", 0, MAX_TURNS), ("recall_limit", 0, 30),
                        ("recall_budget", 200, 20000), ("max_concurrent", 1, 8)):
        if key in patch:
            try:
                cfg[key] = max(lo, min(hi, int(patch[key])))
            except (TypeError, ValueError):
                return {"ok": False, "error": f"{key} must be a number"}
    if "default_connector" in patch:
        cfg["default_connector"] = str(patch["default_connector"] or "")[:40]
    if cfg["default_connector"] not in {c["id"] for c in cfg["connectors"]}:
        cfg["default_connector"] = cfg["connectors"][0]["id"] if cfg["connectors"] else ""
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({k: v for k, v in cfg.items() if not k.startswith("_")},
                              indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(p)
    return {"ok": True, "config": cfg}


def connector(cfg: dict[str, Any], cid: str) -> dict[str, Any] | None:
    return next((c for c in cfg.get("connectors") or [] if c.get("id") == cid), None)


def ollama_models(c: dict[str, Any], timeout: float = 2.0) -> tuple[list[str], str]:
    """The models an Ollama host has pulled, and why not when it cannot say."""
    try:
        with urllib.request.urlopen(f"{c['url']}/api/tags", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8") or "{}")
        return sorted(m.get("name", "") for m in data.get("models") or [] if m.get("name")), ""
    except (OSError, ValueError) as e:
        return [], f"could not reach {c['url']}: {e}"


def connectors_payload(cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Every connector with the models it offers right now."""
    cfg = cfg or load_config()
    out = []
    for c in cfg.get("connectors") or []:
        row = {**c, "available": True, "problem": ""}
        if c["kind"] == "ollama":
            models, problem = ollama_models(c) if c.get("enabled") else ([], "")
            row["models"], row["problem"] = models, problem
            row["available"] = not problem
            if not row.get("default_model") and models:
                row["default_model"] = models[0]
        out.append(row)
    return out


def status_payload() -> dict[str, Any]:
    cfg = load_config()
    return {"ok": True, "config": {k: v for k, v in cfg.items() if k != "connectors"},
            "connectors": connectors_payload(cfg), "running": running_count(),
            "question_mark": QUESTION_MARK}


# ---------------------------------------------------------------- storage
def db() -> sqlite3.Connection:
    con = graph.connect()
    con.executescript(SCHEMA)
    have = {r["name"] for r in con.execute("PRAGMA table_info(agent_msg)")}
    for col, ddl in (("owner_pid", "INTEGER NOT NULL DEFAULT 0"),
                     ("beat_at", "REAL NOT NULL DEFAULT 0"),
                     ("next_action", "TEXT NOT NULL DEFAULT ''")):
        if col not in have:
            con.execute(f"ALTER TABLE agent_msg ADD COLUMN {col} {ddl}")
    have = {r["name"] for r in con.execute("PRAGMA table_info(agent_conv)")}
    for col in ("draft", "draft_at", "verbosity", "tts"):
        if col not in have:
            con.execute(f"ALTER TABLE agent_conv ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    if graph.agents_sync_on():
        graph._ensure_mirror_triggers(con)      # the tables exist only now
    con.commit()
    return con


def _conv_row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["full_tools"] = bool(d["full_tools"])
    d["hidden"] = bool(d["hidden"])
    return d


def _msg_row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    for k in ("events", "context"):
        try:
            d[k] = json.loads(d[k] or "[]")
        except ValueError:
            d[k] = []
    d["full_tools"] = bool(d["full_tools"])
    d["cost_estimated"] = bool(d["cost_estimated"])
    return d


def get_conv(con: sqlite3.Connection, cid: str) -> dict[str, Any] | None:
    r = con.execute("SELECT * FROM agent_conv WHERE id=?", (cid,)).fetchone()
    return _conv_row(r) if r else None


def conv_state(con: sqlite3.Connection, cid: str) -> str:
    """What the conversation's badge shows, from its latest reply: `pending`
    (a spinner), `question` (a ?), `error`, `done` (a check) or `empty`."""
    r = con.execute("SELECT status FROM agent_msg WHERE conv_id=? AND role='agent'"
                    " ORDER BY at DESC LIMIT 1", (cid,)).fetchone()
    if not r:
        return "empty"
    return "pending" if r["status"] in PENDING else r["status"]


def conversations(con: sqlite3.Connection) -> list[dict[str, Any]]:
    out = []
    for r in con.execute("SELECT * FROM agent_conv ORDER BY updated_at DESC"):
        c = _conv_row(r)
        last = con.execute("SELECT role, text, at FROM agent_msg WHERE conv_id=?"
                           " AND text<>'' ORDER BY at DESC LIMIT 1", (c["id"],)).fetchone()
        totals = con.execute("SELECT COUNT(*) n, SUM(cost_usd) cost FROM agent_msg"
                             " WHERE conv_id=?", (c["id"],)).fetchone()
        c["state"] = conv_state(con, c["id"])
        c["last"] = dict(last) if last else None
        c["messages"] = totals["n"]
        c["cost_usd"] = round(totals["cost"] or 0.0, 6)
        out.append(c)
    return out


def thread(con: sqlite3.Connection, cid: str) -> dict[str, Any] | None:
    c = get_conv(con, cid)
    if not c:
        return None
    msgs = [_msg_row(r) for r in con.execute(
        "SELECT * FROM agent_msg WHERE conv_id=? ORDER BY at", (cid,))]
    c["state"] = conv_state(con, cid)
    c["cost_usd"] = round(sum(m["cost_usd"] or 0 for m in msgs), 6)
    c["cost_estimated"] = any(m["cost_estimated"] for m in msgs)
    c["tokens_in"] = sum(m["tokens_in"] for m in msgs)
    c["tokens_out"] = sum(m["tokens_out"] for m in msgs)
    c["turn_end"] = turn_end(msgs)
    return {"ok": True, "conversation": c, "messages": msgs}


def turn_end(msgs: list[dict[str, Any]]) -> dict[str, str] | None:
    """How the latest turn ended: `{id, status}` with status ok | error | cancelled, or None
    while it still runs. The voice mode reopens the microphone on it, so it must always
    settle: a failed or cancelled reply is a turn end too."""
    last = next((m for m in reversed(msgs) if m["role"] == "agent"), None)
    if not last or last["status"] in PENDING:
        return None
    if last["status"] == "error":
        return {"id": last["id"], "status": "cancelled" if last["error"] == CANCELLED else "error"}
    return {"id": last["id"], "status": "ok"}


def thread_text(con: sqlite3.Connection, cid: str, limit: int = 0) -> str:
    """The thread as plain text, oldest first: what an agent reads to see
    earlier turns than the ones its prompt carries. `limit` keeps the last N
    messages."""
    c = get_conv(con, cid)
    if not c:
        return f"no conversation {cid}"
    rows = con.execute("SELECT role, text, model, status, at FROM agent_msg WHERE conv_id=?"
                       " ORDER BY at", (cid,)).fetchall()
    if limit > 0:
        rows = rows[-limit:]
    out = [f"Conversation {cid}: {c['title'] or 'Untitled'} ({len(rows)} messages)"]
    for r in rows:
        if r["role"] == "agent" and r["status"] in PENDING:
            continue                                   # the reply being written now
        who = "Owner" if r["role"] == "user" else f"Agent ({r['model']})"
        out.append(f"\n[{r['at']}] {who}:\n{strip_mark(r['text'] or '')[0]}")
    return "\n".join(out)


def _pick(cfg: dict[str, Any], connector_id: str, model: str) -> tuple[dict | None, str, str]:
    cid = connector_id or cfg.get("default_connector") or ""
    c = connector(cfg, cid)
    if not c:
        return None, "", f"no connector {cid!r}; add one in Settings → Agents"
    if not c.get("enabled"):
        return None, "", f"the {c['name']} connector is turned off"
    model = (model or c.get("default_model") or "").strip()
    if not model and c["kind"] == "ollama":
        models, problem = ollama_models(c)
        if problem:
            return None, "", problem
        model = models[0] if models else ""
    if not model:
        return None, "", f"pick a model for {c['name']}"
    return c, model[:120], ""


def save_conversation(con: sqlite3.Connection, cid: str = "", **changes: Any) -> dict[str, Any]:
    """Create a conversation (no `cid`) or change one: title, connector, model,
    full_tools, context_turns, hidden, verbosity, tts, draft (with `draft_at`; an older edit than the stored one is ignored)."""
    cfg = load_config()
    ts = now_iso()
    if not cid:
        c, model, err = _pick(cfg, str(changes.get("connector") or ""), str(changes.get("model") or ""))
        if err:
            return {"ok": False, "error": err}
        cid = _new_id("aconv")
        turns = changes.get("context_turns", cfg.get("context_turns", 1))
        con.execute("INSERT INTO agent_conv(id,title,created_at,updated_at,connector,model,"
                    "full_tools,context_turns) VALUES(?,?,?,?,?,?,?,?)",
                    (cid, str(changes.get("title") or "")[:200], ts, ts, c["id"], model,
                     int(bool(changes.get("full_tools"))), _turns(turns)))
        con.commit()
        return {"ok": True, "conversation": get_conv(con, cid), "created": True}
    cur = get_conv(con, cid)
    if not cur:
        return {"ok": False, "error": f"no conversation {cid}"}
    sets: dict[str, Any] = {}
    if "connector" in changes or "model" in changes:
        c, model, err = _pick(cfg, str(changes.get("connector") or cur["connector"]),
                              str(changes.get("model") if "model" in changes else ""))
        if err:
            return {"ok": False, "error": err}
        sets.update(connector=c["id"], model=model)
    if "title" in changes:
        sets["title"] = str(changes["title"] or "")[:200]
    if "full_tools" in changes:
        sets["full_tools"] = int(bool(changes["full_tools"]))
    if "context_turns" in changes:
        sets["context_turns"] = _turns(changes["context_turns"])
    if "hidden" in changes:
        sets["hidden"] = int(bool(changes["hidden"]))
    if "verbosity" in changes:
        sets["verbosity"] = changes["verbosity"] if changes["verbosity"] in voice.VERBOSITIES else ""
    if "tts" in changes:
        sets["tts"] = changes["tts"] if changes["tts"] in ("on", "off") else ""
    if "draft" in changes:
        at = str(changes.get("draft_at") or now_iso())[:40]
        if at >= cur["draft_at"]:
            sets.update(draft=str(changes["draft"] or "")[:TEXT_MAX], draft_at=at)
    if sets:
        con.execute(f"UPDATE agent_conv SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                    (*sets.values(), cid))
        con.commit()
        if "title" in sets and cur["node_id"]:
            graph.api_update_node(graph.connect(), cur["node_id"], title=_node_title(sets["title"]))
    return {"ok": True, "conversation": get_conv(con, cid), "created": False}


def _turns(v: Any) -> int:
    try:
        return max(0, min(MAX_TURNS, int(v)))
    except (TypeError, ValueError):
        return 1


def _node_title(title: str) -> str:
    return f"Agents chat: {title}"[:200]


ATTACH_LINK = re.compile(r"\[([^\]\n]+)\]\(attach:([^)\s]+)\)")


def attachment_lines(paths: list[str]) -> tuple[str, str]:
    """The message lines for files the owner attached, or the reason one is refused."""
    lines = []
    for rel in paths:
        try:
            target = documents.resolve(rel, "attachments")
        except ValueError as e:
            return "", f"attachment {rel!r}: {e}"
        name = re.sub(r"[\[\]()]", "", re.sub(r"^\d{8}-\d{6}-[0-9a-f]{4}-", "", target.name)) or "file"
        lines.append(f"[{name}](attach:{rel})")
    return "\n".join(lines), ""


def with_disk_paths(text: str) -> str:
    """The message as the agent reads it: each attachment link also names the
    file's place on disk, which a model with file tools can open."""
    def where(m: re.Match) -> str:
        try:
            return f"{m.group(1)} (attached file: {documents.resolve(m.group(2), 'attachments')})"
        except ValueError:
            return m.group(0)
    return ATTACH_LINK.sub(where, text)


def send(con: sqlite3.Connection, text: str, conv_id: str = "", *, connector_id: str = "",
         model: str = "", full_tools: bool | None = None, context_turns: int | None = None,
         attachments: list[str] | None = None, start: bool = True) -> dict[str, Any]:
    """Store the person's message and queue the reply. With no conversation,
    one is made, titled from the message. The connector, model and tools given
    here become the conversation's own from this turn on."""
    text = (text or "").strip()[:TEXT_MAX]
    if attachments:
        lines, err = attachment_lines(attachments)
        if err:
            return {"ok": False, "error": err}
        text = f"{text}\n\n{lines}".strip()
    if not text:
        return {"ok": False, "error": "missing text"}
    changes: dict[str, Any] = {}
    if connector_id:
        changes["connector"] = connector_id
    if model:
        changes["model"] = model
    if full_tools is not None:
        changes["full_tools"] = full_tools
    if context_turns is not None:
        changes["context_turns"] = context_turns
    if not conv_id:
        changes["title"] = _title_from(text)
    res = save_conversation(con, conv_id, **changes)
    if not res.get("ok"):
        return res
    conv = res["conversation"]
    if conv["connector"] and conv["model"] and not connector(load_config(), conv["connector"]):
        return {"ok": False, "error": f"no connector {conv['connector']!r}"}
    ts = now_iso()
    uid, aid = _new_id("amsg"), _new_id("amsg")
    con.execute("INSERT INTO agent_msg(id,conv_id,role,text,at,status) VALUES(?,?,?,?,?,?)",
                (uid, conv["id"], "user", text, ts, "done"))
    # One microsecond later, so the reply always sorts after its question.
    con.execute("INSERT INTO agent_msg(id,conv_id,role,text,at,status,reply_to,connector,model,"
                "full_tools) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (aid, conv["id"], "agent", "", _after(ts), "queued", uid, conv["connector"],
                 conv["model"], int(conv["full_tools"])))
    con.execute("UPDATE agent_conv SET updated_at=? WHERE id=?", (ts, conv["id"]))
    con.commit()
    if start:
        dispatch()
    return {"ok": True, "conversation": get_conv(con, conv["id"]), "message_id": uid,
            "reply_id": aid}


def retry(con: sqlite3.Connection, msg_id: str, start: bool = True) -> dict[str, Any]:
    """Run a failed reply again, with the conversation's current connector."""
    r = con.execute("SELECT * FROM agent_msg WHERE id=? AND role='agent'", (msg_id,)).fetchone()
    if not r:
        return {"ok": False, "error": f"no reply {msg_id}"}
    if r["status"] != "error":
        return {"ok": False, "error": f"reply {msg_id} is {r['status']}, not error"}
    conv = get_conv(con, r["conv_id"])
    con.execute("UPDATE agent_msg SET status='queued', error='', events='[]', text='',"
                " connector=?, model=?, full_tools=?, cost_usd=NULL, cost_estimated=0,"
                " tokens_in=0, tokens_out=0, started_at='', finished_at='' WHERE id=?",
                (conv["connector"], conv["model"], int(conv["full_tools"]), msg_id))
    con.commit()
    if start:
        dispatch()
    return {"ok": True, "reply_id": msg_id}


def cancel(con: sqlite3.Connection, msg_id: str) -> dict[str, Any]:
    """Stop a queued or running reply. It fails at once with `CANCELLED`, so
    the thread shows it and Retry runs it again; a running Claude session is
    killed, a running Ollama chat stops at its next round and its answer is
    dropped."""
    r = con.execute("SELECT * FROM agent_msg WHERE id=? AND role='agent'", (msg_id,)).fetchone()
    if not r:
        return {"ok": False, "error": f"no reply {msg_id}"}
    if r["status"] not in PENDING:
        return {"ok": False, "error": f"reply {msg_id} is {r['status']}, not running"}
    with _lock:
        if msg_id in _threads:                 # its thread drops whatever it ends with
            _cancelled.add(msg_id)
        proc = _procs.get(msg_id)
    if proc is not None and proc.poll() is None:
        _platform.kill_tree(proc)
    _update(con, msg_id, status="error", error=CANCELLED, finished_at=now_iso())
    return {"ok": True, "reply_id": msg_id, "was": r["status"]}


def _is_cancelled(msg_id: str) -> bool:
    with _lock:
        return msg_id in _cancelled


def _after(ts: str) -> str:
    dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(dt.timestamp() + 0.001, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def _title_from(text: str) -> str:
    line = re.sub(r"\s+", " ", ATTACH_LINK.sub(r"\1", text)).strip()
    return line if len(line) <= 60 else line[:57].rstrip() + "…"


def _update(con: sqlite3.Connection, msg_id: str, **cols: Any) -> None:
    if "events" in cols:
        cols["events"] = json.dumps(cols["events"][-EVENTS_MAX:], ensure_ascii=False)
    if "context" in cols:
        cols["context"] = json.dumps(cols["context"], ensure_ascii=False)
    con.execute(f"UPDATE agent_msg SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?",
                (*cols.values(), msg_id))
    con.commit()


# ---------------------------------------------------------------- context
def build_turn(con: sqlite3.Connection, reply: dict[str, Any]) -> dict[str, Any]:
    """What one stateless call is given: a system prompt, and a prompt holding
    the earlier exchanges the conversation carries, the recalled memory, and
    the message."""
    cfg = load_config()
    conv = get_conv(con, reply["conv_id"])
    q = con.execute("SELECT * FROM agent_msg WHERE id=?", (reply["reply_to"],)).fetchone()
    question = with_disk_paths(q["text"]) if q else ""
    turns = conv["context_turns"] if conv else 0
    history = []
    if turns:
        rows = con.execute(
            "SELECT u.text AS asked, a.text AS answered, a.model, a.node_id FROM agent_msg a"
            " JOIN agent_msg u ON u.id=a.reply_to WHERE a.conv_id=? AND a.role='agent'"
            " AND a.status IN ('done','question') AND a.at < ? ORDER BY a.at DESC LIMIT ?",
            (reply["conv_id"], reply["at"], turns)).fetchall()
        history = list(reversed([dict(r) for r in rows]))
    recalled: list[dict[str, Any]] = []
    if int(cfg.get("recall_limit") or 0):
        skip = {h["node_id"] for h in history if h.get("node_id")}
        if conv and conv.get("node_id"):
            skip.add(conv["node_id"])
        try:
            res = graph.api_recall(graph.connect(), question,
                                   limit=int(cfg["recall_limit"]) + 4,
                                   budget=int(cfg.get("recall_budget") or 2600), exclude=skip)
            recalled = [_without_next(r) for r in res.get("results") or []
                        if r.get("type") not in RECALL_SKIP][:int(cfg["recall_limit"])]
        except Exception as e:                       # recall is help, never a blocker
            recalled = [{"id": "", "type": "error", "title": f"recall failed: {e}"}]
    parts = []
    if history:
        parts.append("Earlier in this conversation:")
        for h in history:
            parts.append(f"\nOwner: {h['asked']}\n\nYou ({h['model']}): "
                         f"{strip_mark(strip_next(h['answered'])[0])[0]}")
        parts.append("")
    if recalled:
        parts.append("What the brain recalled for this message (ids are graph node ids):")
        parts += [json.dumps(r, ensure_ascii=False) for r in recalled if r.get("id")]
        parts.append("")
    parts.append("The owner's message:\n" + question)
    parts.append(f"\n(If your reply asks the owner anything they must answer before you "
                 f"can go on, its last line is exactly {QUESTION_MARK}.)")
    cid = reply.get("connector") or (conv or {}).get("connector") or ""
    kind = (connector(cfg, cid) or {}).get("kind", "claude")
    system = "\n".join(x for x in (system_prompt(conv, len(history), kind),
                                    voice.reply_language_instruction(question)) if x)
    return {"system": system, "prompt": "\n".join(parts),
            "context": [r["id"] for r in recalled if r.get("id")], "question": question}


def system_prompt(conv: dict[str, Any] | None, carried: int, kind: str = "claude") -> str:
    full = bool(conv and conv.get("full_tools"))
    owner = graph.owner() or "the owner"
    lines = [
        f"You are {graph.brain_name()}, {owner}'s second brain, answering in the Agents "
        "tab of the brain's own UI.",
        "Every turn is stateless. You see only the owner's message, "
        + (f"the last {carried} exchange(s) of this conversation, " if carried else "")
        + "and the memory the brain recalled for it. If that is not enough, look things "
          "up rather than guess.",
    ]
    if conv:
        how = ("call the read_thread tool" if kind == "ollama" else
               f"run `{THREAD_CMD} {conv['id']}` (add `--limit N` for the last N messages)")
        lines.append(
            f"This conversation's id is {conv['id']}"
            + (f", its graph node {conv['node_id']}" if conv.get("node_id") else "")
            + f". When the owner refers to something said earlier that you do not see, "
              f"{how} to read the whole thread rather than ask.")
    lines += [
        "Reply in the language of the owner's message. Be direct and short.",
        f"If you cannot go on without the owner (a choice only they can make, a fact you "
        f"cannot find, a confirmation before acting), ask, and make the last line of your "
        f"reply exactly {QUESTION_MARK}. Never add it otherwise.",
        "Every reply ends with a line of its own suggesting the owner's most likely next "
        "message as `[[next: <one short sentence, in the owner's voice>]]`. When you asked "
        f"a question, suggest the answer you expect (before the {QUESTION_MARK} line, which "
        "stays last). Leave it out only when nothing obvious follows.",
    ]
    if kind == "ollama":
        lines.append("The brain is yours to read and write in every turn: look things up "
                     "with recall, search and get_node, and store what is worth keeping "
                     "(a fact, a decision, a to-do) with add_note and add_task, without "
                     "asking first.")
    else:
        lines.append(
            "The brain is yours to read and write in every turn, whatever the tools mode, "
            "and without asking first. Run these exactly as written, never after a `cd`:\n"
            f"- `{GRAPH_CMD} match-rules \"<what you are about to do>\"`: the owner's rules "
            "that apply. Run it before acting.\n"
            f"- `{GRAPH_CMD} recall \"<topic>\"`, `search \"<text>\"`, `get-node --id <id>`: "
            "facts, people, projects, decisions.\n"
            f"- `{QUERY_CMD} open --mine`: the owner's open loops.\n"
            f"- `{GRAPH_CMD} add-node --type <note|task|decision|person|...> --title ... "
            "--body ...` and `add-edge`: store what is worth keeping from this turn (a fact, "
            "a decision, a to-do, a person). The exchange itself is stored for you.")
    if full:
        lines.append("Tools are ON for this conversation: you may also act outside the "
                     "brain (edit, run commands, post), within the brain's rules.")
    else:
        lines.append("Tools are READ-ONLY for this conversation: outside the brain, read "
                     "and look up (files, the web, Jira, Slack, mail, calendar, Drive) but "
                     "change nothing. If the owner asks for such a change, say what you "
                     "would do and that they can turn tools on for this conversation.")
    cfg = voice.load_config()
    tts_on = (conv or {}).get("tts") == "on" or (
        cfg["tts_enabled"] and (conv or {}).get("tts") != "off")
    narrate = voice.verbosity_instruction((conv or {}).get("verbosity", ""), cfg, tts_on)
    if narrate:
        lines.append(narrate)
    return "\n".join(lines)


def strip_next(text: str) -> tuple[str, str]:
    """The reply without the `[[next: ...]]` line, and the suggestion in it."""
    found = NEXT_RE.findall(text or "")
    return NEXT_RE.sub("", text or "").rstrip(), (found[-1].strip()[:300] if found else "")


def _without_next(node: dict[str, Any]) -> dict[str, Any]:
    """A recalled node with any `[[next: ...]]` removed, so a small model cannot copy an old suggestion."""
    return {k: NEXT_RE.sub("", v).strip() if isinstance(v, str) else v for k, v in node.items()}


def strip_mark(text: str) -> tuple[str, bool]:
    """The reply without the question marker, and whether it had one."""
    lines = (text or "").rstrip().splitlines()
    asked = False
    while lines and lines[-1].strip() in (QUESTION_MARK, f"`{QUESTION_MARK}`"):
        lines.pop()
        asked = True
    if not asked and QUESTION_MARK in (text or ""):
        asked = True
    return "\n".join(lines).replace(QUESTION_MARK, "").rstrip(), asked


def _clip(v: Any, n: int = EVENT_MAX) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + f"… ({len(s) - n} more chars)"


# ---------------------------------------------------------------- claude
def claude_command(c: dict[str, Any], model: str, full: bool, session_id: str,
                   system_file: Path) -> list[str]:
    """argv for one turn. Neither the prompt nor the system prompt is in it:
    the prompt goes in on stdin and the system prompt is read from
    `system_file`, so neither is capped by the command line (~32 KB on
    Windows) nor re-read by a shell on the way."""
    cmd = [_platform.resolve_bin(str(c.get("bin") or "claude")), "-p", "--output-format",
           "stream-json", "--verbose", "--model", model, "--session-id", session_id,
           "--append-system-prompt-file", str(system_file)]
    if full:
        cmd.append("--dangerously-skip-permissions")
    else:
        # The tool set itself is cut down, and anything outside it is denied
        # without a prompt: no edits, connectors for reading only, and Bash for
        # the brain's own commands only.
        tools = ",".join(CLAUDE_READ_TOOLS)
        allowed = [*CLAUDE_READ_TOOLS, *CLAUDE_READ_MCP, *(f"Bash({c}:*)" for c in BRAIN_CMDS)]
        cmd += [f"--tools={tools},Bash", f"--allowedTools={','.join(allowed)}",
                "--permission-mode", "dontAsk"]
    return cmd


def _usage_cost(model: str, usage: dict[str, Any]) -> float:
    try:
        import trigger
        p_in, p_out, p_cache = trigger._price(model)
    except Exception:
        p_in, p_out, p_cache = 5.0, 25.0, 0.5
    return (usage.get("input_tokens", 0) * p_in + usage.get("output_tokens", 0) * p_out
            + usage.get("cache_read_input_tokens", 0) * p_cache
            + usage.get("cache_creation_input_tokens", 0) * p_in * 1.25) / 1e6


class _Log:
    """The reply's event log, written through to the row at most every half
    second while it runs, so the UI can follow along."""

    def __init__(self, con: sqlite3.Connection, msg_id: str):
        self.con, self.msg_id = con, msg_id
        self.events: list[dict[str, Any]] = []
        self.by_id: dict[str, dict[str, Any]] = {}
        self.usage: dict[str, dict[str, Any]] = {}   # per assistant message id
        self.model = ""
        self.extra: dict[str, Any] = {}
        self._flushed = 0.0

    def add(self, ev: dict[str, Any]) -> dict[str, Any]:
        ev.setdefault("at", now_iso())
        self.events.append(ev)
        if ev.get("id"):
            self.by_id[ev["id"]] = ev
        return ev

    def tokens(self) -> tuple[int, int]:
        tin = sum(u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                  + u.get("cache_creation_input_tokens", 0) for u in self.usage.values())
        return tin, sum(u.get("output_tokens", 0) for u in self.usage.values())

    def cost(self) -> float:
        return sum(_usage_cost(self.model, u) for u in self.usage.values())

    def flush(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._flushed < 0.5:
            return
        self._flushed = time.monotonic()
        tin, tout = self.tokens()
        cols: dict[str, Any] = {"events": self.events, "tokens_in": tin, "tokens_out": tout,
                                **self.extra}
        if self.usage:
            cols.update(cost_usd=round(self.cost(), 6), cost_estimated=1)
        _update(self.con, self.msg_id, **cols)


def _tool_event(block: dict[str, Any], parent: str) -> dict[str, Any]:
    name, inp = block.get("name") or "tool", block.get("input") or {}
    kind, label = "tool", name
    if name == "Skill":
        kind, label = "skill", str(inp.get("skill") or inp.get("command") or "skill")
    elif name in ("Task", "Agent"):
        kind = "agent"
        label = str(inp.get("subagent_type") or inp.get("description") or "subagent")
    elif name.startswith("mcp__"):
        kind = "mcp"
    return {"id": block.get("id") or "", "kind": kind, "name": label, "tool": name,
            "input": _clip(inp), "output": "", "error": False, "parent": parent or ""}


def _result_text(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") if isinstance(b, dict) else str(b)
                         for b in content)
    return str(content or "")


def handle_stream_line(log: _Log, line: str) -> dict[str, Any] | None:
    """Fold one stream-json line into the log. Returns the `result` object
    when the line is the final one."""
    try:
        ev = json.loads(line)
    except ValueError:
        return None
    t = ev.get("type")
    parent = ev.get("parent_tool_use_id") or ""
    if t == "system" and ev.get("subtype") == "init":
        log.model = ev.get("model") or log.model
    elif t == "assistant":
        msg = ev.get("message") or {}
        if msg.get("id") and msg.get("usage"):
            log.usage[msg["id"]] = msg["usage"]
        for b in msg.get("content") or []:
            bt = b.get("type")
            if bt == "tool_use":
                log.add(_tool_event(b, parent))
            elif bt == "thinking" and (b.get("thinking") or "").strip():
                log.add({"kind": "thinking", "name": "thinking", "output": _clip(b["thinking"]),
                         "parent": parent})
            elif bt == "text" and (b.get("text") or "").strip():
                log.add({"kind": "text", "name": "said", "output": _clip(b["text"]),
                         "parent": parent})
    elif t == "user":
        for b in (ev.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                target = log.by_id.get(b.get("tool_use_id") or "")
                if target is not None:
                    target["output"] = _clip(_result_text(b.get("content")))
                    target["error"] = bool(b.get("is_error"))
    elif t == "result":
        return ev
    return None


def run_claude(con: sqlite3.Connection, reply: dict[str, Any], c: dict[str, Any],
               turn: dict[str, Any]) -> dict[str, Any]:
    session_id = str(uuid.uuid4())
    log = _Log(con, reply["id"])
    log.model = reply["model"]
    fd, name = tempfile.mkstemp(prefix="agent-system-", suffix=".md")
    system_file = Path(name)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(turn["system"])
    try:
        return _run_claude(con, reply, c, turn, log, session_id, system_file)
    finally:
        system_file.unlink(missing_ok=True)


def _run_claude(con: sqlite3.Connection, reply: dict[str, Any], c: dict[str, Any],
                turn: dict[str, Any], log: "_Log", session_id: str,
                system_file: Path) -> dict[str, Any]:
    cmd = claude_command(c, reply["model"], bool(reply["full_tools"]), session_id, system_file)
    _update(con, reply["id"], session_id=session_id)
    env = _textio.child_env({**os.environ, "MEMORY_LEDGER_AGENT_REPLY": reply["id"],
                             "MEMORY_LEDGER_AGENT_CONV": reply["conv_id"]})
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=env,
                                cwd=str(graph.home_dir()) if graph.home_dir().is_dir()
                                else str(Path.home()), bufsize=1, **_platform.group_kwargs())
    except OSError as e:
        raise AgentError(f"could not start {cmd[0]}: {e}") from None
    _platform.track(proc)
    try:
        # the prompt, then EOF: `claude -p` reads it all before it answers
        proc.stdin.write(turn["prompt"])   # type: ignore[union-attr]
        proc.stdin.close()                 # type: ignore[union-attr]
    except OSError:
        pass   # it died before reading; its exit and stderr say why, below
    with _lock:
        _procs[reply["id"]] = proc
    # the whole tree: claude's MCP servers and tools die with it
    timer = threading.Timer(int(c.get("timeout_s") or 900), _platform.kill_tree, (proc,))
    timer.start()
    result = None
    try:
        for line in proc.stdout:   # type: ignore[union-attr]
            res = handle_stream_line(log, line)
            if res is not None:
                result = res
            log.flush()
        proc.wait()
    finally:
        timed_out = not timer.is_alive() and proc.returncode not in (0, None)
        timer.cancel()
        _platform.untrack(proc)
        with _lock:
            _procs.pop(reply["id"], None)
    log.flush(force=True)
    if result is None:
        err = (proc.stderr.read() if proc.stderr else "").strip()[-600:]   # type: ignore[union-attr]
        if timed_out:
            raise AgentError(f"timed out after {c.get('timeout_s')}s")
        raise AgentError(err or f"claude exited {proc.returncode} without an answer")
    text = str(result.get("result") or "")
    tokens = log.tokens()
    u = result.get("usage") or {}
    if u:   # the final tally; the streamed ones are partial
        tokens = (int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
                  + int(u.get("cache_creation_input_tokens") or 0), int(u.get("output_tokens") or 0))
    out = {"text": text, "events": log.events, "tokens": tokens}
    if result.get("total_cost_usd") is not None:
        out["cost_usd"], out["cost_estimated"] = float(result["total_cost_usd"]), False
    else:
        out["cost_usd"], out["cost_estimated"] = log.cost(), True
    if result.get("is_error"):
        raise AgentError(text or str(result.get("subtype") or "claude reported an error"))
    return out


# ---------------------------------------------------------------- ollama
def _tool(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": props, "required": required}}}


_S = {"type": "string"}
OLLAMA_READ_TOOLS = [
    _tool("recall", "Associative recall over the owner's memory graph: the nodes most "
          "related to a query (people, tasks, decisions, notes).", {"query": _S}, ["query"]),
    _tool("search", "Find nodes whose title contains the text.", {"text": _S}, ["text"]),
    _tool("get_node", "Read one node in full, with its relations.", {"id": _S}, ["id"]),
    _tool("read_thread", "Read this conversation's earlier messages, oldest first. "
          "`limit` keeps the last N.", {"limit": {"type": "integer"}}, []),
]
OLLAMA_WRITE_TOOLS = [
    _tool("add_note", "Store a note in the owner's memory graph.",
          {"title": _S, "body": _S}, ["title"]),
    _tool("add_task", "Create an open task for the owner. `due` is YYYY-MM-DD.",
          {"title": _S, "body": _S, "due": _S}, ["title"]),
]


def ollama_tool_call(name: str, args: dict[str, Any], conv_id: str = "") -> Any:
    """One graph tool, run here. Writing to the brain is allowed in both tool
    modes; Ollama has no tools that reach outside it."""
    if name == "read_thread":
        try:
            limit = int(args.get("limit") or 0)
        except (TypeError, ValueError):
            limit = 0
        return thread_text(db(), conv_id, limit) if conv_id else {"error": "no conversation"}
    gcon = graph.connect()
    if name == "recall":
        res = graph.api_recall(gcon, str(args.get("query") or ""), limit=8)
        return res.get("results") or []
    if name == "search":
        term = str(args.get("text") or "").strip().lower()
        if len(term) < 2:
            return []
        return [dict(r) for r in gcon.execute(
            "SELECT id,type,title,status FROM nodes WHERE lower(title) LIKE ?"
            " ORDER BY length(title) LIMIT 15", (f"%{term}%",))]
    if name == "get_node":
        r = gcon.execute("SELECT * FROM nodes WHERE id=?", (str(args.get("id") or ""),)).fetchone()
        if not r:
            return {"error": "no such node"}
        d = graph._node_dict(r)
        d["body"] = (d.get("body") or "")[:4000]
        d["edges"] = [dict(e) for e in gcon.execute(
            "SELECT src,rel,dst FROM edges WHERE src=? OR dst=? LIMIT 40", (d["id"], d["id"]))]
        return d
    if name in ("add_note", "add_task"):
        title = str(args.get("title") or "").strip()[:300]
        if not title:
            return {"error": "missing title"}
        ntype = "note" if name == "add_note" else "task"
        res = graph.api_create_node(gcon, ntype, title, str(args.get("body") or "")[:4000],
                                    {"source": "agents"}, "open" if ntype == "task" else "",
                                    str(args.get("due") or "")[:10] if ntype == "task" else "")
        return {"ok": res.get("ok"), "id": (res.get("node") or {}).get("id"),
                "error": res.get("error")}
    return {"error": f"unknown tool {name}"}


def _ollama_chat(c: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(f"{c['url']}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=int(c.get("timeout_s") or 600)) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise AgentError(f"Ollama answered {e.code}: {detail}") from None
    except (OSError, ValueError) as e:
        raise AgentError(f"could not reach Ollama at {c['url']}: {e}") from None


def run_ollama(con: sqlite3.Connection, reply: dict[str, Any], c: dict[str, Any],
               turn: dict[str, Any]) -> dict[str, Any]:
    log = _Log(con, reply["id"])
    tools = OLLAMA_READ_TOOLS + OLLAMA_WRITE_TOOLS
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": turn["system"] + "\nUse the tools to look up the "
         "owner's memory graph when the recalled memory is not enough."},
        {"role": "user", "content": turn["prompt"]},
    ]
    tin = tout = 0
    use_tools = True
    for _ in range(int(c.get("max_rounds") or 8)):
        if _is_cancelled(reply["id"]):
            raise AgentError(CANCELLED)
        body = {"model": reply["model"], "messages": messages, "stream": False}
        if use_tools:
            body["tools"] = tools
        try:
            data = _ollama_chat(c, body)
        except AgentError as e:
            # A model without tool support says so; answer without them.
            if use_tools and "does not support tools" in str(e):
                use_tools = False
                log.add({"kind": "note", "name": "no tools", "output":
                         f"{reply['model']} does not support tools; answering without them"})
                continue
            raise
        tin += int(data.get("prompt_eval_count") or 0)
        tout += int(data.get("eval_count") or 0)
        msg = data.get("message") or {}
        if (msg.get("thinking") or "").strip():
            log.add({"kind": "thinking", "name": "thinking", "output": _clip(msg["thinking"])})
        calls = msg.get("tool_calls") or []
        content = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S).strip()
        if not calls:
            log.extra = {"tokens_in": tin, "tokens_out": tout}
            log.flush(force=True)
            return {"text": content, "events": log.events, "tokens": (tin, tout),
                    "cost_usd": 0.0, "cost_estimated": False}
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": calls})
        for call in calls:
            fn = call.get("function") or {}
            name, args = fn.get("name") or "", fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            ev = log.add({"kind": "tool", "name": name, "tool": name, "input": _clip(args),
                          "output": "", "error": False})
            try:
                res = ollama_tool_call(name, args, reply["conv_id"])
            except Exception as e:                    # a tool failure is the model's to read
                res = {"error": str(e)}
            ev["output"] = _clip(res)
            ev["error"] = isinstance(res, dict) and bool(res.get("error"))
            messages.append({"role": "tool", "tool_name": name,
                             "content": json.dumps(res, ensure_ascii=False, default=str)[:8000]})
        log.extra = {"tokens_in": tin, "tokens_out": tout}
        log.flush(force=True)
    raise AgentError(f"no answer after {c.get('max_rounds') or 8} tool rounds")


RUNNERS = {"claude": run_claude, "ollama": run_ollama}


# ---------------------------------------------------------------- running
def run_reply(msg_id: str) -> dict[str, Any]:
    """Run one queued (or just-claimed) reply to the end, in this thread."""
    con = db()
    r = con.execute("SELECT * FROM agent_msg WHERE id=?", (msg_id,)).fetchone()
    if not r:
        return {"ok": False, "error": f"no reply {msg_id}"}
    reply = _msg_row(r)
    # Claimed only while still pending: a cancel in between wins.
    # With AGENTS_SYNC a `running` reply may belong to another machine: it is
    # claimable only once its heartbeat has gone stale.
    if graph.agents_sync_on():
        free, args = "status='queued' OR (status='running' AND beat_at<?)", (time.time() - BEAT_STALE_S,)
    else:
        free, args = "status IN ('queued','running')", ()
    claimed = con.execute("UPDATE agent_msg SET status='running', started_at=?, owner_pid=?,"
                          f" beat_at=? WHERE id=? AND ({free})",
                          (now_iso(), os.getpid(), time.time(), msg_id, *args)).rowcount
    con.commit()
    if not claimed:
        return {"ok": False, "error": f"reply {msg_id} was cancelled"}
    stop = threading.Event()
    threading.Thread(target=_beat, args=(msg_id, stop), daemon=True).start()
    try:
        return _run_claimed(con, msg_id, reply)
    finally:
        stop.set()


def _beat(msg_id: str, stop: threading.Event) -> None:
    """While a reply runs, stamp it every BEAT_S so another server can tell it
    is alive without seeing its pid (a sandbox may not)."""
    con = graph.connect()
    while not stop.wait(BEAT_S):
        try:
            con.execute("UPDATE agent_msg SET beat_at=? WHERE id=? AND status='running'",
                        (time.time(), msg_id))
            con.commit()
        except sqlite3.Error:
            pass


def _run_claimed(con: sqlite3.Connection, msg_id: str, reply: dict[str, Any]) -> dict[str, Any]:
    cfg = load_config()
    c = connector(cfg, reply["connector"])
    try:
        if not c:
            raise AgentError(f"no connector {reply['connector']!r}")
        if not c.get("enabled"):
            raise AgentError(f"the {c['name']} connector is turned off")
        turn = build_turn(con, reply)
        _update(con, msg_id, context=turn["context"])
        res = RUNNERS[c["kind"]](con, reply, c, turn)
    except Exception as e:
        if _is_cancelled(msg_id):              # cancel already wrote the reply's end
            return {"ok": False, "error": CANCELLED}
        _update(con, msg_id, status="error", error=str(e)[:1000], finished_at=now_iso())
        return {"ok": False, "error": str(e)}
    if _is_cancelled(msg_id):                  # an answer that came in after the cancel
        return {"ok": False, "error": CANCELLED}
    text, nxt = strip_next(res["text"])
    text, asked = strip_mark(text)
    status = "question" if asked else "done"
    _update(con, msg_id, status=status, text=text[:TEXT_MAX],
            next_action=nxt, events=res["events"],
            cost_usd=round(res["cost_usd"], 6) if res["cost_usd"] is not None else None,
            cost_estimated=int(bool(res["cost_estimated"])), tokens_in=res["tokens"][0],
            tokens_out=res["tokens"][1], finished_at=now_iso())
    con.execute("UPDATE agent_conv SET updated_at=? WHERE id=?", (now_iso(), reply["conv_id"]))
    con.commit()
    try:
        remember(con, msg_id, turn["question"], text)
    except Exception:
        pass                                          # the reply stands without its node
    return {"ok": True, "status": status}


def remember(con: sqlite3.Connection, msg_id: str, question: str, answer: str) -> None:
    """Write the exchange into the graph, under the conversation's node, so
    recall finds it from here on."""
    reply = _msg_row(con.execute("SELECT * FROM agent_msg WHERE id=?", (msg_id,)).fetchone())
    conv = get_conv(con, reply["conv_id"])
    gcon = graph.connect()
    if not conv["node_id"]:
        res = graph.api_create_node(gcon, "conversation", _node_title(conv["title"]), "",
                                    {"source": "agents", "agent_conv": conv["id"],
                                     "started_at": conv["created_at"]}, "open")
        if not res.get("ok"):
            return
        conv["node_id"] = res["node"]["id"]
        con.execute("UPDATE agent_conv SET node_id=? WHERE id=?", (conv["node_id"], conv["id"]))
        con.commit()
    body = f"Owner: {question}\n\n{reply['model']}: {answer}"[:4000]
    res = graph.api_create_node(gcon, "message", f"Agents: {_title_from(question)}", body,
                                {"source": "agents", "agent_conv": conv["id"],
                                 "agent_reply": msg_id, "connector": reply["connector"],
                                 "model": reply["model"]}, "")
    if res.get("ok"):
        graph.api_link(gcon, res["node"]["id"], conv["node_id"], graph.AREA_REL)
        _update(con, msg_id, node_id=res["node"]["id"])


_lock = threading.Lock()
_threads: dict[str, threading.Thread] = {}
_procs: dict[str, subprocess.Popen] = {}     # a running Claude reply's session, to cancel it
_cancelled: set[str] = set()                 # replies the owner cancelled, until they end


def running_count() -> int:
    with _lock:
        return sum(1 for t in _threads.values() if t.is_alive())


def dispatch() -> list[str]:
    """Start queued replies, oldest first, up to `max_concurrent` at once. Each
    finished reply dispatches again, so the queue drains on its own."""
    limit = int(load_config().get("max_concurrent") or 2)
    started = []
    with _lock:
        for k in [k for k, t in _threads.items() if not t.is_alive()]:
            del _threads[k]
        free = limit - len(_threads)
        if free <= 0:
            return started
        con = db()
        rows = con.execute("SELECT id FROM agent_msg WHERE status='queued' ORDER BY at"
                           " LIMIT ?", (free,)).fetchall()
        for r in rows:
            if r["id"] in _threads:
                continue
            t = threading.Thread(target=_work, args=(r["id"],), daemon=True,
                                 name=f"agent-{r['id']}")
            _threads[r["id"]] = t
            started.append(r["id"])
            t.start()
    return started


def _work(msg_id: str) -> None:
    try:
        run_reply(msg_id)
    finally:
        with _lock:
            _threads.pop(msg_id, None)
            _cancelled.discard(msg_id)
        dispatch()


BEAT_S = 10          # heartbeat period of a running reply
BEAT_STALE_S = 30    # no heartbeat for this long: the owner is gone


def is_processing(row: Any) -> bool:
    """A reply still in flight: queued, or running with a live heartbeat. The
    heartbeat, not the pid, decides — a pid only means something on its own machine."""
    if row["status"] == "queued":
        return True
    return row["status"] == "running" and time.time() - (row["beat_at"] or 0) < BEAT_STALE_S


def _alive(pid: int) -> bool:
    return _platform.pid_alive(pid)


def recover() -> int:
    """At server start: a reply left `running` whose server process is gone
    fails with a reason (retry is one click), and the queue restarts. A reply
    whose owner is still alive (a recent heartbeat, or a live pid) is left alone."""
    con = db()
    n = 0
    for r in con.execute("SELECT id, owner_pid, beat_at FROM agent_msg"
                         " WHERE status='running'").fetchall():
        if time.time() - (r["beat_at"] or 0) < BEAT_STALE_S:
            continue
        if r["owner_pid"] and r["owner_pid"] != os.getpid() and _alive(r["owner_pid"]):
            continue
        n += con.execute("UPDATE agent_msg SET status='error', error='interrupted: the server "
                         "stopped while this ran', finished_at=? WHERE id=? AND status='running'",
                         (now_iso(), r["id"])).rowcount
    con.commit()
    dispatch()
    return n


# ---------------------------------------------------------------- cli
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    r = sub.add_parser("run")
    r.add_argument("id")
    t = sub.add_parser("thread", help="print a conversation's messages, oldest first")
    t.add_argument("id")
    t.add_argument("--limit", type=int, default=0, help="only the last N messages")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        graph.out(status_payload())
    elif a.cmd == "thread":
        print(thread_text(db(), a.id, a.limit))
    else:
        graph.out(run_reply(a.id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
