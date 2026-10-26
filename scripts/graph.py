#!/usr/bin/env python3
"""memory-ledger graph engine.

A tiny, dependency-free graph database over SQLite. Everything the plugin
remembers is a NODE (a person, task, requirement, decision, note, writing
sample, reminder...) and everything that connects them is an EDGE
(relates-to, blocks, part-of, assigned-to, about, depends-on, mentions...).

Skills never touch SQL directly; they call these subcommands and read the
JSON that comes back. Machine output is always JSON on stdout.

DB location: $SECOND_BRAIN_DB, else <plugin>/memory/graph.db
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import unicodedata
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Where the ledger lives and whether it is on come from _switch, a module that
# imports nothing heavier than pathlib. They live there rather than here so a
# SessionStart hook can ask "am I enabled?" for ~12 ms instead of importing this
# engine for ~55 ms — see scripts/_switch.py. Re-exported so `graph._env`,
# `graph.home_dir`, `graph.disabled` and `graph.enabled` keep resolving for every
# caller that already reads them through the engine.
from _switch import (  # noqa: E402,F401
    _env, home_dir, disabled, enabled, DISABLE_MARKER, _OFF_TRUTHY,
    HOME_DIRNAME, HOME_CANDIDATES, adopt_canonical_home, legacy_homes,
    flag, flag_source, BOOL_SETTINGS,
    settings as _switch_settings,
)
import decay as _decay  # noqa: E402  (per-type half-lives; pure, imports no engine)


# Default section (realm) for new nodes; override with $SECOND_BRAIN_REALM or
# --realm, or set it once in <home>/settings.json. Assigned below, after the
# settings reader it depends on — see `settings()`.
#: The realm a fresh brain writes into until one is configured. Nothing treats
#: the name as special: `rename-realm` renames it, and writing a node with
#: `--realm <new>` is all it takes to add another.
REALM_FALLBACK = "default"
DEFAULT_REALM = REALM_FALLBACK

# ---------------------------------------------------------------------------
# vocabulary (soft — not enforced, just documented + used for defaults)
# ---------------------------------------------------------------------------
NODE_TYPES = [
    "person", "task", "subtask", "requirement", "decision", "note",
    "project", "reminder", "writing-sample", "association", "meeting",
    "jira-issue", "resource", "skill", "area", "quote", "parked",
    # §3 also names these; they were missing here, so anything deriving the
    # vocabulary from the code (the skill tree, the UI facets) could not see them.
    "team", "channel", "rule", "spec", "process", "pr", "email", "message",
    # traceability (§2k): the agent tree, what was considered, and what was
    # discarded under a condition that may later stop holding.
    "conversation", "agent", "idea", "transition", "gauge", "context-event",
    # the skill tree (§5a): one node per node TYPE, so two skills touching the
    # same type are visibly coupled even when neither one names the other.
    "node-type",
    # triggers (docs/trigger.md): one per Slack/Jira mention that summoned the
    # brain, so "who asked for that?" is a recall, not an archaeology dig.
    "request",
    # usage (scripts/usage_report.py): one ISO week of Claude Code usage metrics,
    # written by the person's own ingest and drawn by the Usage section.
    "usage-week",
    # learned rules: a detector's finding, awaiting accept/reject. Never a `rule`,
    # so match-rules cannot return it before a person says yes.
    "rule-proposal",
]

# Default character budget for read paths that would otherwise return whole
# bodies. `search` used to be uncapped and returned ~9k chars where `recall`
# answered the same cue in ~1.5k — the single largest avoidable read cost.
READ_BUDGET = 2600

# An AREA is a domain hub: quotes, health, process... Content links to it with
# `part-of`, so a domain is one edge walk away instead of a full-text guess.
# Area ids are `hub_<domain>` by convention (hub_quote, hub_health, hub_process).
AREA_TYPE = "area"
AREA_REL = "part-of"

# --- The GENOME: the brain's copy of how its own Claude layer is built ---------
# The plugin ships a base layer; the DB is where it is kept, mutated and re-read.
# `rule` nodes hold the conventions (scenario-matched, so a request loads only the
# 1-3 that apply instead of the whole CONVENTIONS.md); `spec` nodes hold the
# rebuild blueprint (hooks, storage, API surface, restore steps) so ANY AI can
# reconstitute the interaction layer from the graph alone if the plugin vanishes.
RULE_TYPE = "rule"
SPEC_TYPE = "spec"
AREA_CONVENTION = "hub_convention"
AREA_GENOME = "hub_genome"
# A user-authored rule outranks the plugin's shipped default of the same key.
RULE_PRECEDENCE = {"user": 100, "learned": 50, "plugin": 0}
# Sections that bind on EVERY request, so match-rules appends them regardless of
# the cue. Keep this set tiny: each one is a fixed cost paid on every lookup.
ALWAYS_APPLY_SECTIONS = {"0", "1"}
EDGE_RELS = [
    "relates-to", "blocks", "blocked-by", "part-of", "has-subtask",
    "assigned-to", "about", "depends-on", "causes", "caused-by",
    "mentions", "authored-by", "example-of", "follows-up",
    # neuroplasticity / learned skills
    "applies-to", "learned-from", "reinforces", "triggered-by",
    # traceability (§2k): `part-of` carries the agent tree and message->agent,
    # `about` a transition to its idea, `blocked-by` an idea to the gauge that
    # held it back, `caused-by` a transition to the turn that caused it.
    "supersedes", "captured-from",
    # the skill tree (§5a). `requires` is hard; `waits-for` is optional-ordered
    # (starting before it makes this skill's output wrong); `enriched-by` is
    # optional-enriching (consume it if it landed, never wait). `reads`/`writes`
    # point at a `node-type`, which is where the real coupling shows.
    "requires", "waits-for", "enriched-by", "reads", "writes",
    # a skill -> the rule it is accountable for (§5.2). The body naming a rule id
    # is prose a regex has to guess at; this is the same claim as an edge, so the
    # map can walk it and a skill's accountability survives a reworded paragraph.
    "honors",
]

# The skill -> rule edge, named once so the UI, the exporter and the tree all
# reach for the same string.
HONORS_REL = "honors"

# Skill-tree edges are machine-generated from the declarations, so sync owns them
# and prunes the ones that stopped being declared. A hand-made edge never carries
# this prop and is therefore never touched.
TREE_EDGE_PROP = {"src": "skill-tree"}
TREE_HUBS = {
    "plugin": ("hub_skills_builtin", "Built-in skills — the plugin's contract corpus"),
    "learned": ("hub_skills_user", "User skills — learned in this brain at runtime"),
}

# statuses that count as "archived" — kept forever, hidden from active views
DONE_STATUSES = ("done", "cancelled", "dropped")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path() -> Path:
    """The graph database file. $SECOND_BRAIN_DB overrides the exact file;
    otherwise it lives at <home>/graph.db."""
    env = _env("DB")
    if env:
        return Path(env).expanduser()
    return home_dir() / "graph.db"


# ---------------------------------------------------------------------------
# backups — a point in time to come back to
#
# Consolidation is non-destructive by construction (§2a): it adds a digest and
# links the captures to it, and every original stays. That makes it safe, not
# reversible — undoing a run means unpicking a digest and a props key from every
# capture it touched, which is exactly the kind of work nobody wants to do at
# the moment they realise they want it. So a run takes a snapshot of the whole
# file first, and `scripts/restore.py <snapshot>` puts it back (checked first,
# and the brain it replaces is kept).
#
# Snapshots expire after BACKUP_KEEP_DAYS. That IS a delete, and it is not the
# delete §2a forbids: §2a is about the graph's own content — a task is archived,
# never removed — while this is retention on a copy, whose whole purpose is to
# be temporary. An unbounded backup directory silently doubles the brain's disk
# footprint every time you press a button.
# ---------------------------------------------------------------------------
BACKUP_KEEP_DAYS = 5


def backups_dir() -> Path:
    return home_dir() / "backups"


def backup_db(label: str = "manual") -> dict:
    """Snapshot the graph into <home>/backups/<label>-<stamp>.db.

    Uses sqlite3's own backup API rather than copying the file, because a plain
    copy of a live database can catch it mid-transaction and produce a snapshot
    that will not open — the one failure mode a backup may not have."""
    src = db_path()
    if not src.exists():
        return {"ok": False, "error": f"no database at {src}"}
    d = backups_dir()
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = d / f"{slugify(label) or 'manual'}-{stamp}.db"
    import sqlite3           # lazy, like connect() — see that function
    con = sqlite3.connect(str(src))
    try:
        target = sqlite3.connect(str(dest))
        try:
            con.backup(target)
        finally:
            target.close()
    finally:
        con.close()
    return {"ok": True, "path": str(dest), "bytes": dest.stat().st_size,
            "created": now(), "expires_in_days": BACKUP_KEEP_DAYS}


def list_backups() -> list:
    """Every snapshot on disk, newest first, each saying how long it has left."""
    d = backups_dir()
    if not d.is_dir():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=BACKUP_KEEP_DAYS)
    rows = []
    for f in d.glob("*.db"):
        try:
            st = f.stat()
        except OSError:
            continue
        made = datetime.fromtimestamp(st.st_mtime, timezone.utc)
        rows.append({"name": f.name, "path": str(f), "bytes": st.st_size,
                     "created": made.isoformat(timespec="seconds"),
                     "expires": (made + timedelta(days=BACKUP_KEEP_DAYS)
                                 ).isoformat(timespec="seconds"),
                     "expired": made < cutoff})
    return sorted(rows, key=lambda r: r["created"], reverse=True)


def prune_backups(keep_days: int = BACKUP_KEEP_DAYS) -> list:
    """Delete snapshots past their retention window. Returns what it removed,
    so the caller can say so rather than tidying up behind the user's back."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    removed = []
    for f in backups_dir().glob("*.db") if backups_dir().is_dir() else []:
        try:
            if datetime.fromtimestamp(f.stat().st_mtime, timezone.utc) < cutoff:
                size = f.stat().st_size
                f.unlink()
                removed.append({"name": f.name, "bytes": size})
        except OSError:
            continue
    return removed


def learned_skills_dir() -> Path:
    """Where runtime-learned user skills live (one folder per skill, each with
    a SKILL.md). This is the plastic layer — the brain writes new abilities here
    and records in the graph when to fire them."""
    return home_dir() / "learned_skills"


# ---------------------------------------------------------------------------
# settings: preferences that outlive one shell
#
# Env vars are per-process, so `export SECOND_BRAIN_TZ=…` only holds for the
# terminal that ran it — a cron, a hook, or the web server never sees it. This
# file is the machine-wide answer, and the web UI's Settings page edits it.
# Precedence everywhere: explicit flag > env var > this file > built-in default.
# ---------------------------------------------------------------------------
SETTING_KEYS = ["tz", "realm", "owner", "brain_name", "backend", "autosync", "consolidate",
                "map_poll_seconds", "dash_refresh_seconds", "ui_sync",
                "learned_rules", "prompt_recall", "agents_sync", "agent_icon"]

# `backend` is where READS come from by default (writes always hit SQLite first
# and mirror onward, whatever this says). `auto` is the honest setting for a
# container that can be stopped: neo4j when it answers, the local mirror when it
# does not. `scripts/neo4j_local.py up` writes it.
BACKENDS = ("sqlite", "neo4j", "auto")

# BOOL_SETTINGS, flag() and flag_source() live in _switch, imported above — the
# SessionStart path reads them before it is willing to import this module.

# The fallback owner, used only when neither the env var nor the file names one.
# It is a name rather than "" because norm_assignee() has to map SOMETHING to
# "me", and a blank owner would quietly make every task somebody else's.
DEFAULT_OWNER = "Owner"

# What the brain answers to: the word a wake phrase starts with ("Brain will
# handle this"), `{brain}` in a signature or a run command, and the name the
# page shows. A word people say out loud, so it defaults to one.
DEFAULT_BRAIN_NAME = "Brain"


def settings_path() -> Path:
    return home_dir() / "settings.json"


# The brain's icon: one image the person uploads in Settings, kept in the brain
# home as `agent-icon.<ext>`. The `agent_icon` setting holds just that filename.
# The browser tab, the sidebar and the Ledger Agent box all read it from here.
AGENT_ICON_TYPES = {"png": "image/png", "jpg": "image/jpeg", "ico": "image/x-icon",
                    "svg": "image/svg+xml"}
AGENT_ICON_MAX_BYTES = 512 * 1024


def agent_icon_path() -> Path | None:
    """The uploaded icon's file, or None when none is set (or it has gone
    missing), so every caller falls back to the default favicon."""
    name = setting("agent_icon", "")
    if not isinstance(name, str) or Path(name).name != name \
            or name.rpartition(".")[2] not in AGENT_ICON_TYPES:
        return None
    p = home_dir() / name
    return p if p.is_file() else None


#: Reading the preferences file is _switch's job — flag() there resolves the
#: boolean settings before this module is importable, and one file must not have
#: two readers that can disagree about a corrupt one. The write side stays here.
settings = _switch_settings


def save_settings(patch: dict) -> dict:
    """Merge `patch` into the file and return the whole result. A value of ""
    or None REMOVES the key, which is how you go back to the env/system default
    rather than pinning an empty string."""
    cur = settings()
    for k, v in (patch or {}).items():
        if k not in SETTING_KEYS:
            continue
        # `False` is a value, not an absence. Testing `v in (None, "")` alone
        # would delete every switch the moment someone turned one off, and the
        # key would read back as its default — an off switch that turns itself
        # on is the worst kind.
        if v is None or (v == "" and not isinstance(v, bool)):
            cur.pop(k, None)
        elif k in BOOL_SETTINGS:
            cur[k] = bool(v)
        else:
            cur[k] = v
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cur, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return cur


# ---------------------------------------------------------------------------
# UI preferences — the browser's own state, optionally kept in the brain
#
# These are NOT settings. A setting (tz, realm, owner) changes what the engine
# computes, so it has to live server-side or the CLI and the cron disagree with
# the page. Which dashboard tab you had open changes nothing, and forcing it
# through the same file would make every idle click a disk write on a value no
# other reader cares about.
#
# So the browser owns them in localStorage, and syncing them here is opt-in
# (`ui_sync`). Turned on, this file is what a second machine — or the same
# machine in another browser — starts from instead of the defaults.
#
# It is one opaque blob on purpose: the shape belongs to the frontend, and a
# server that validates the keys becomes a second place to edit every time the
# UI grows a toggle. What IS enforced is that it is an object and that it is
# small, because neither is the frontend's business to get right.
# ---------------------------------------------------------------------------
UI_PREFS_MAX_BYTES = 64 * 1024


def ui_prefs_path() -> Path:
    return home_dir() / "ui_prefs.json"


def ui_prefs() -> dict:
    try:
        val = json.loads(ui_prefs_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return val if isinstance(val, dict) else {}


def save_ui_prefs(prefs: dict) -> dict:
    """Replace the stored blob. Replace rather than merge: the browser holds the
    whole thing, and a merge would resurrect a preference the user just cleared."""
    if not isinstance(prefs, dict):
        return {"ok": False, "error": "preferences must be an object"}
    body = json.dumps(prefs, indent=2, sort_keys=True) + "\n"
    if len(body.encode("utf-8")) > UI_PREFS_MAX_BYTES:
        return {"ok": False,
                "error": f"preferences exceed {UI_PREFS_MAX_BYTES} bytes"}
    p = ui_prefs_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return {"ok": True, "path": str(p), "keys": sorted(prefs)}


def setting(name: str, default: str = "") -> str:
    """One setting, env first. Same order as everything else in the engine."""
    val = _env(name.upper())
    if val:
        return val
    val = settings().get(name)
    return str(val) if val not in (None, "") else default


def owner() -> str:
    """Who "me" is. Resolved, never hardcoded: $MEMORY_LEDGER_OWNER, then the
    `owner` setting, then DEFAULT_OWNER. Everything that decides whether a task
    is on YOUR plate — norm_assignee, is_mine_actionable, a new session's
    props.owner — goes through this."""
    return setting("owner", DEFAULT_OWNER)


def brain_name() -> str:
    """What the brain is called: $MEMORY_LEDGER_BRAIN_NAME, then the
    `brain_name` setting, then DEFAULT_BRAIN_NAME."""
    return setting("brain_name", DEFAULT_BRAIN_NAME)


#: Alias for the handful of api_* helpers that take an `owner=` argument and so
#: shadow the resolver's name inside their own scope.
_resolve_owner = owner


DEFAULT_REALM = setting("realm", REALM_FALLBACK)


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40] or "node"


def connect():
    import sqlite3
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists() or path.stat().st_size == 0
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(con)
    if fresh:
        # A brain made from scratch starts with the built-in skills, as if
        # `ingest-skills` had been run (SECOND_BRAIN_NO_SKILL_SEED=1 skips it).
        ensure_builtin_skills(con)
    return con


# The cue text a node is matched on, over and above its prose. `match-rules` reads
# `props.scenario`, `match-skills` reads `props.triggers` / `props.description` —
# authors write those fields precisely so a rule fires on words its body never uses.
# FTS5 external-content tables can only index real columns of the content table, so
# the JSON is surfaced as one VIRTUAL generated column and indexed like any other.
# Without it every one of those cue words is dead weight (fixed 2026-07-31).
_CUES_SQL = ("ifnull(json_extract(props,'$.scenario'),'') || ' ' || "
             "ifnull(json_extract(props,'$.triggers'),'') || ' ' || "
             "ifnull(json_extract(props,'$.description'),'')")


def _generated_cols_ok() -> bool:
    """Generated columns need SQLite 3.31 (2020). Older builds keep the
    two-column index — cue matching degrades to prose, it does not break."""
    import sqlite3
    return sqlite3.sqlite_version_info >= (3, 31)


def _ensure_fts(con) -> None:
    """Create the FTS index and its sync triggers, migrating older brains.

    Older databases have a two-column index (title, body) and triggers to match.
    When the shape changes, the index and triggers are dropped and rebuilt from the
    content table — cheap, since `nodes` is the source of truth and a personal brain
    is small. Falls back to the two-column shape on SQLite older than 3.31, which
    cannot add a generated column.
    """
    # table_xinfo, NOT table_info: the latter omits VIRTUAL generated columns, so
    # `cues` would look missing on every connect and the ALTER would fail second run.
    cols = [r[1] for r in con.execute("PRAGMA table_xinfo(nodes)").fetchall()]
    if _generated_cols_ok() and "cues" not in cols:
        con.execute("ALTER TABLE nodes ADD COLUMN cues TEXT "
                    f"GENERATED ALWAYS AS ({_CUES_SQL}) VIRTUAL")
        cols.append("cues")

    want = ["title", "body"] + (["cues"] if "cues" in cols else [])
    have = [r[1] for r in con.execute("PRAGMA table_info(nodes_fts)").fetchall()]
    if have and have != want:                      # stale shape — rebuild it
        con.executescript("DROP TRIGGER IF EXISTS nodes_ai;"
                          "DROP TRIGGER IF EXISTS nodes_ad;"
                          "DROP TRIGGER IF EXISTS nodes_au;"
                          "DROP TABLE IF EXISTS nodes_fts;")
        have = []
    cl = ", ".join(want)
    if not have:
        con.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5("
                    f"{cl}, content='nodes', content_rowid='rowid')")
        con.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('rebuild')")

    # Triggers keep the index in step with writes. Generated columns are readable
    # through NEW/OLD, so the same column list works here.
    new = ", ".join(f"new.{c}" for c in want)
    old = ", ".join(f"old.{c}" for c in want)
    con.executescript(f"""
        CREATE TRIGGER IF NOT EXISTS nodes_ai AFTER INSERT ON nodes BEGIN
            INSERT INTO nodes_fts(rowid, {cl}) VALUES (new.rowid, {new});
        END;
        CREATE TRIGGER IF NOT EXISTS nodes_ad AFTER DELETE ON nodes BEGIN
            INSERT INTO nodes_fts(nodes_fts, rowid, {cl})
            VALUES ('delete', old.rowid, {old});
        END;
        CREATE TRIGGER IF NOT EXISTS nodes_au AFTER UPDATE ON nodes BEGIN
            INSERT INTO nodes_fts(nodes_fts, rowid, {cl})
            VALUES ('delete', old.rowid, {old});
            INSERT INTO nodes_fts(rowid, {cl}) VALUES (new.rowid, {new});
        END;
    """)


def _ensure_schema(con) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS nodes (
            id       TEXT PRIMARY KEY,
            type     TEXT NOT NULL,
            title    TEXT NOT NULL,
            body     TEXT DEFAULT '',
            props    TEXT DEFAULT '{}',   -- JSON blob
            status   TEXT DEFAULT '',     -- e.g. open|doing|done|blocked
            due      TEXT DEFAULT '',     -- ISO date/time, optional
            created  TEXT NOT NULL,
            updated  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS edges (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            src     TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
            dst     TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
            rel     TEXT NOT NULL,
            props   TEXT DEFAULT '{}',
            created TEXT NOT NULL,
            UNIQUE(src, dst, rel)
        );
        CREATE INDEX IF NOT EXISTS idx_nodes_type   ON nodes(type);
        CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status);
        CREATE INDEX IF NOT EXISTS idx_nodes_due    ON nodes(due);
        CREATE INDEX IF NOT EXISTS idx_edges_src    ON edges(src);
        CREATE INDEX IF NOT EXISTS idx_edges_dst    ON edges(dst);
        """
    )
    _ensure_fts(con)
    # --- realm (section) column + neo4j sync outbox ---
    cols = [r[1] for r in con.execute("PRAGMA table_info(nodes)").fetchall()]
    if "realm" not in cols:
        con.execute("ALTER TABLE nodes ADD COLUMN realm TEXT DEFAULT ''")
        con.execute("UPDATE nodes SET realm=? WHERE realm IS NULL OR realm=''",
                    (DEFAULT_REALM,))
    con.execute(
        "CREATE TABLE IF NOT EXISTS outbox ("
        "seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, ref TEXT, created TEXT)")
    # Hebbian tally: how often two nodes have co-activated in recall. Mirrored to
    # the master as rows, like every table a restore needs (see MIRROR_TABLES);
    # an EDGE is only created once a pair repeats (see
    # HEBBIAN_THRESHOLD), so a single lexical coincidence cannot wire the graph.
    con.execute(
        "CREATE TABLE IF NOT EXISTS coactivation ("
        "a TEXT, b TEXT, n INTEGER DEFAULT 0, last TEXT, PRIMARY KEY(a,b))")
    _ensure_blocked_index(con)
    # two-way sync bookkeeping, local to this file and never mirrored: who this
    # machine is, how far it has pulled, the file shas it last agreed on
    con.execute("CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS mirror_mute (on_ INTEGER)")
    _ensure_mirror_triggers(con)
    con.commit()


# --- the recheck index (§2k) -------------------------------------------------
# What is consulted BY TRIGGER lives in an indexed table; what is read in
# sequence to reconstruct lives in the log. The log here is the `transition`
# NODES (immutable, synced to the master); this table is their PROJECTION —
# derived, local, and rebuildable at any time with `recheck --rebuild`. Nothing
# writes it directly: `idea-discard` appends the transition and then projects.
#
# One row per CONDITION, so a multi-condition discard is several rows sharing a
# `source_ref`; the recheck treats a source_ref group as a conjunction.
def _ensure_blocked_index(con) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS blocked_item (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            item_type   TEXT NOT NULL,      -- 'idea' today, anything tomorrow
            item_ref    TEXT NOT NULL,      -- the node held back
            gauge_key   TEXT NOT NULL,      -- catalog key (see `gauges`)
            op          TEXT NOT NULL,
            required    TEXT NOT NULL DEFAULT '',
            source_ref  TEXT NOT NULL,      -- transition node that wrote it
            created     TEXT NOT NULL,
            resolved_at TEXT NOT NULL DEFAULT '',
            UNIQUE(item_ref, gauge_key, op, required, source_ref)
        );
        CREATE INDEX IF NOT EXISTS idx_blocked_gauge
            ON blocked_item(gauge_key, resolved_at);
        CREATE INDEX IF NOT EXISTS idx_blocked_item ON blocked_item(item_ref);
        CREATE INDEX IF NOT EXISTS idx_blocked_src  ON blocked_item(source_ref);
        """
    )


# ---------------------------------------------------------------------------
# neo4j master + offline outbox
# ---------------------------------------------------------------------------
# neo4j is the unified master store. Every mutation also writes to the local
# SQLite mirror (durable + offline) and is queued in `outbox`; we push to neo4j
# immediately, and if neo4j is down the queue drains automatically on the next
# successful op or an explicit `flush`.

def _neo4j_cfg() -> dict:
    cfg = {}
    f = home_dir() / "neo4j.json"
    if f.exists():
        try:
            cfg = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    return {
        "url": _env("NEO4J_URL", cfg.get("url", "")),
        "db": _env("NEO4J_DB", cfg.get("db", "neo4j")),
        "user": _env("NEO4J_USER", cfg.get("user", "neo4j")),
        "password": _env("NEO4J_PASSWORD", cfg.get("password", "")),
    }


def _neo4j_call(statements, timeout=30):
    """POST Cypher to neo4j's HTTP tx endpoint. Returns (ok, error_or_None).

    NOTE: the timeout is client-side only. A request that times out here may
    still commit on the server, so callers must treat a timeout as "unknown",
    not as "did not happen" — every statement we send is idempotent for exactly
    that reason (MERGE, not CREATE).
    """
    cfg = _neo4j_cfg()
    if not cfg["url"] or not cfg["password"]:
        return False, "neo4j not configured"
    endpoint = cfg["url"].rstrip("/") + f"/db/{cfg['db']}/tx/commit"
    auth = base64.b64encode(f"{cfg['user']}:{cfg['password']}".encode()).decode()
    req = urllib.request.Request(
        endpoint, data=json.dumps({"statements": statements}).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "Authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        errs = body.get("errors") or []
        return (not errs), (errs[0].get("message") if errs else None)
    except Exception as e:
        return False, str(e)


def _neo4j_query(statement, params=None, timeout=60):
    """Run one read statement. Returns (rows, error_or_None); each row is the list
    of returned columns. `_neo4j_call` only reports ok — restore needs the data."""
    cfg = _neo4j_cfg()
    if not cfg["url"] or not cfg["password"]:
        return [], "neo4j not configured"
    endpoint = cfg["url"].rstrip("/") + f"/db/{cfg['db']}/tx/commit"
    auth = base64.b64encode(f"{cfg['user']}:{cfg['password']}".encode()).decode()
    req = urllib.request.Request(
        endpoint, data=json.dumps({"statements": [
            {"statement": statement, "parameters": params or {}}]}).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "Authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
    except Exception as e:
        return [], str(e)
    if body.get("errors"):
        return [], body["errors"][0].get("message")
    return [r["row"] for r in body["results"][0]["data"]], None


_NODE_ID_CONSTRAINT = (
    "CREATE CONSTRAINT node_id_unique IF NOT EXISTS "
    "FOR (n:Node) REQUIRE n.id IS UNIQUE")
_MIRROR_ROW_CONSTRAINT = (
    "CREATE CONSTRAINT mirror_row_uid IF NOT EXISTS "
    "FOR (x:MirrorRow) REQUIRE x.uid IS UNIQUE")
_MIRROR_FILE_CONSTRAINT = (
    "CREATE CONSTRAINT mirror_file_path IF NOT EXISTS "
    "FOR (x:MirrorFile) REQUIRE x.path IS UNIQUE")
_TOMBSTONE_CONSTRAINT = (
    "CREATE CONSTRAINT tombstone_uid IF NOT EXISTS "
    "FOR (t:Tombstone) REQUIRE t.uid IS UNIQUE")


def _ensure_neo4j_schema() -> tuple:
    """Make every mirrored key unique on the master.

    Without this, `MERGE (x:Node {id:...})` is not safe under concurrency: two
    flushes racing on the same id each fail to find it and each create one. That
    is how 22 ids ended up duplicated (one of them three times). The constraint
    makes MERGE actually idempotent, which the whole sync design assumes.
    Cheap and idempotent, so callers can just run it."""
    return _neo4j_call([{"statement": s} for s in (
        _NODE_ID_CONSTRAINT, _MIRROR_ROW_CONSTRAINT, _MIRROR_FILE_CONSTRAINT,
        _TOMBSTONE_CONSTRAINT)],
        timeout=30)


def _neo4j_up() -> bool:
    cfg = _neo4j_cfg()
    if not cfg["url"]:
        return False
    try:
        urllib.request.urlopen(cfg["url"].rstrip("/") + "/", timeout=3).read()
        return True
    except Exception:
        return False


def _clean_props(props: dict) -> dict:
    """neo4j props must be primitives or arrays of ONE primitive type (a mixed
    list fails the whole transaction) — JSON-encode anything else."""
    out = {}
    for k, v in (props or {}).items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif (isinstance(v, list) and all(isinstance(x, (str, int, float, bool)) for x in v)
              and len({type(x) for x in v}) <= 1):
            out[k] = v
        else:
            out[k] = json.dumps(v, ensure_ascii=False)
    return out


EDGE_SEP = "\x1f"

# The columns a node carries itself. A prop with one of these names used to be
# overwritten on the master by the column of the same name (113 nodes had one),
# so props now travel twice: flattened for queries, and verbatim in `_props` so
# a restore gets back exactly what was written.
NODE_FIELDS = ("id", "type", "title", "body", "status", "due", "realm",
               "created", "updated")

# Tables mirrored beside nodes and edges, each with its primary key. They reach
# the master as :MirrorRow {uid, table, key, data} — the row as JSON — because
# the master is a copy to restore from, not a second schema to keep in step.
# `blocked_item` is left out on purpose: it is a projection of the transition
# nodes, rebuilt by `recheck --rebuild`.
MIRROR_TABLES = {
    "coactivation": ("a", "b"),
    "trigger_request": ("id",),
    "trigger_action": ("id",),
    "trigger_gate": ("scope",),
    "trigger_channel": ("channel_id",),
    "trigger_person": ("source", "user_id"),
    "trigger_checkpoint": ("source",),
}

# Agent chats ride along only with AGENTS_SYNC=1 (off by default). Ids are unique
# per message and a message is only appended to, so two machines cannot collide;
# `draft` stays local (see AGENT_LOCAL_COLS) and `owner_pid` means nothing off
# the machine that ran the reply, so neither is sent.
AGENT_MIRROR_TABLES = {"agent_conv": ("id",), "agent_msg": ("id",)}
AGENT_LOCAL_COLS = {"agent_conv": ("draft", "draft_at"), "agent_msg": ("owner_pid",)}


def agents_sync_on() -> bool:
    """AGENTS_SYNC=1 forces it on for a process; otherwise the `agents_sync`
    setting (Settings page) decides, off by default."""
    return os.environ.get("AGENTS_SYNC") == "1" or flag("agents_sync")


def mirror_tables() -> dict:
    """The tables mirrored now: the fixed ones, plus the agent chat when on."""
    return {**MIRROR_TABLES, **AGENT_MIRROR_TABLES} if agents_sync_on() else MIRROR_TABLES


def put_row(con, table: str, data: dict) -> bool:
    """Write a row pulled from the master. An upsert, so columns that never
    leave this machine (the draft) keep their local value."""
    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    d = {k: v for k, v in data.items() if k in cols}
    pk = mirror_tables().get(table)
    if not d or not pk:
        return False
    if table == "agent_msg":
        r = con.execute("SELECT status, beat_at FROM agent_msg WHERE id=?",
                        (d.get("id"),)).fetchone()
        # a reply this machine finished, or is running, is not rolled back
        if r and (r["status"] in ("done", "error", "cancelled")
                  and d.get("status") in ("queued", "running")
                  or r["status"] == "running" and d.get("status") != "done"):
            return False
    sets = ", ".join(f"{k}=excluded.{k}" for k in d if k not in pk) or f"{pk[0]}={pk[0]}"
    con.execute(f"INSERT INTO {table}({', '.join(d)}) VALUES({', '.join('?' * len(d))}) "
                f"ON CONFLICT({', '.join(pk)}) DO UPDATE SET {sets}", list(d.values()))
    return True


# Every statement that changes the master takes the next number from one clock
# node and stamps it (`_seq`) with the machine that wrote it (`_origin`). The
# clock is write-locked until its transaction commits, so numbers commit in
# order, and "everything after N" is exactly what another machine has not pulled
# yet. Deletes leave a :Tombstone with the same stamp, so a delete can be pulled
# too — a node that is simply gone cannot be told from one never pushed.
_CLOCK = ("MERGE (c:SyncClock {id:'main'}) SET c.seq = coalesce(c.seq, 0) + 1 "
          "WITH c.seq AS seq ")
# Never overwrite a newer copy: another machine may have pushed a later version
# of the node than this one holds, and a whole-node SET would erase it.
_NODE_UPSERT = (_CLOCK + "UNWIND $nodes AS n MERGE (x:Node {id:n.id}) "
                "WITH x, n, seq WHERE x.updated IS NULL OR x.updated <= n.updated "
                "SET x = n, x._seq = seq, x._origin = $origin")
_EDGE_UPSERT = (_CLOCK + "UNWIND $edges AS e MATCH (a:Node {id:e.src}),(b:Node {id:e.dst}) "
                "MERGE (a)-[r:REL {rel:e.rel}]->(b) "
                "SET r = e.p, r._seq = seq, r._origin = $origin")
_NODE_DEL = (_CLOCK + "UNWIND $ids AS i OPTIONAL MATCH (x:Node {id:i}) DETACH DELETE x "
             "WITH DISTINCT i, seq MERGE (t:Tombstone {uid:'node' + $sep + i}) "
             "SET t.kind='node', t.ref=i, t._seq=seq, t._origin=$origin")
_EDGE_DEL = (_CLOCK + "UNWIND $edges AS e "
             "OPTIONAL MATCH (:Node {id:e.src})-[r:REL {rel:e.rel}]->(:Node {id:e.dst}) "
             "DELETE r WITH DISTINCT e, seq MERGE (t:Tombstone {uid:'edge' + $sep + e.ref}) "
             "SET t.kind='edge', t.ref=e.ref, t._seq=seq, t._origin=$origin")
_ROW_UPSERT = (_CLOCK + "UNWIND $rows AS w MERGE (x:MirrorRow {uid:w.uid}) "
               "SET x.table=w.table, x.key=w.key, x.data=w.data, "
               "x._seq = seq, x._origin = $origin")
_ROW_DEL = (_CLOCK + "UNWIND $uids AS u OPTIONAL MATCH (x:MirrorRow {uid:u}) DELETE x "
            "WITH DISTINCT u, seq MERGE (t:Tombstone {uid:'row' + $sep + u}) "
            "SET t.kind='row', t.ref=u, t._seq=seq, t._origin=$origin")
_FILE_UPSERT = (_CLOCK + "UNWIND $files AS f MERGE (x:MirrorFile {path:f.path}) "
                "SET x.content=f.content, x.sha=f.sha, x._seq = seq, x._origin = $origin")

# Stamps the master adds; not part of what was pushed.
MIRROR_STAMPS = ("_seq", "_origin")


def _ensure_mirror_triggers(con) -> None:
    """Queue every change to a mirrored table in the outbox, whoever makes it.

    `_after_write` only sees writes that go through it, and several paths write
    SQL directly (rule ingest, bulk imports, `_touch`, the trigger engine's own
    tables). A trigger catches all of them, so nothing reaches the local file
    without also being on its way to the master. Only the key is queued; the
    flush reads the row's state at push time, so a burst of changes to one row
    costs one push. `_touch` now queues too, but it still never pushes: the
    reinforcement rides along with the next real write's flush.

    A row in `mirror_mute` silences them: a pull writes what came FROM the
    master, and queueing it would send every pulled change straight back."""
    ts = "strftime('%Y-%m-%dT%H:%M:%S+00:00','now')"
    sep = "char(31)"
    when = "WHEN NOT EXISTS (SELECT 1 FROM mirror_mute)"
    # triggers from before the mute existed are replaced, not kept beside it
    old = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'mirror\\_%' "
        "ESCAPE '\\' AND sql NOT LIKE '%mirror_mute%'")]
    for name in old:
        con.execute(f"DROP TRIGGER IF EXISTS {name}")
    stmts = []
    for op in ("insert", "update", "delete"):
        refs = {"insert": ["new"], "update": ["old", "new"], "delete": ["old"]}[op]
        node_vals = ", ".join(f"('node', {r}.id, {ts})" for r in refs)
        edge_vals = ", ".join(
            f"('edge', {r}.src || {sep} || {r}.dst || {sep} || {r}.rel, {ts})"
            for r in refs)
        stmts.append(f"CREATE TRIGGER IF NOT EXISTS mirror_nodes_{op} AFTER {op.upper()} "
                     f"ON nodes {when} BEGIN INSERT INTO outbox(kind,ref,created) "
                     f"VALUES {node_vals}; END")
        stmts.append(f"CREATE TRIGGER IF NOT EXISTS mirror_edges_{op} AFTER {op.upper()} "
                     f"ON edges {when} BEGIN INSERT INTO outbox(kind,ref,created) "
                     f"VALUES {edge_vals}; END")
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, pk in mirror_tables().items():
        if table not in have:
            continue          # created later by its owner, who calls this again
        for op in ("insert", "update", "delete"):
            refs = {"insert": ["new"], "update": ["old", "new"], "delete": ["old"]}[op]
            vals = ", ".join(
                f"('row', '{table}' || {sep} || json_array({', '.join(f'{r}.{c}' for c in pk)}), {ts})"
                for r in refs)
            stmts.append(f"CREATE TRIGGER IF NOT EXISTS mirror_{table}_{op} AFTER "
                         f"{op.upper()} ON {table} {when} BEGIN "
                         f"INSERT INTO outbox(kind,ref,created) VALUES {vals}; END")
    for s in stmts:
        con.execute(s)


def _enqueue(con, kind, ref):
    con.execute("INSERT INTO outbox(kind,ref,created) VALUES(?,?,?)", (kind, ref, now()))


def _mirror_map(raw_props, fixed: dict) -> dict:
    """The property map a node or edge is written with: props flattened (for
    queries) where they don't clash with a fixed field, the fixed fields, and the
    props verbatim in `_props` (for restore)."""
    try:
        props = json.loads(raw_props or "{}")
    except (json.JSONDecodeError, TypeError):
        props = {}
    if not isinstance(props, dict):
        props = {}
    m = {k: v for k, v in _clean_props(props).items()
         if k not in fixed and k not in NODE_FIELDS and not k.startswith("_")}
    m.update(fixed)
    m["_props"] = raw_props if raw_props is not None else "{}"
    return m


def _node_payload(con, nid):
    r = con.execute("SELECT * FROM nodes WHERE id=?", (nid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    m = _mirror_map(d.get("props"), {
        "id": d["id"], "type": d["type"], "title": d["title"], "body": d["body"],
        "status": d["status"], "due": d["due"], "created": d["created"],
        "updated": d["updated"], "realm": d.get("realm") or DEFAULT_REALM})
    if d.get("realm") != m["realm"]:
        m["_realm"] = d.get("realm")      # the stored value, when it isn't the one shown
    return m


def _edge_payload(con, src, dst, rel):
    r = con.execute("SELECT props, created FROM edges WHERE src=? AND dst=? AND rel=?",
                    (src, dst, rel)).fetchone()
    if not r:
        return None
    return {"src": src, "dst": dst, "rel": rel,
            "p": _mirror_map(r["props"], {"rel": rel, "created": r["created"]})}


def _row_payload(con, table, key):
    """One mirrored row, or None if it is gone. `key` is the JSON array the
    trigger wrote, which is also the row's identity on the master."""
    pk = mirror_tables().get(table)
    if not pk:
        return None
    try:
        vals = json.loads(key)
    except json.JSONDecodeError:
        return None
    where = " AND ".join(f"{c}=?" for c in pk)
    try:
        r = con.execute(f"SELECT * FROM {table} WHERE {where}", vals).fetchone()
    except Exception:
        return None
    if not r:
        return None
    d = {k: v for k, v in dict(r).items() if k not in AGENT_LOCAL_COLS.get(table, ())}
    return {"uid": table + EDGE_SEP + key, "table": table, "key": key,
            "data": json.dumps(d, ensure_ascii=False)}


FLUSH_BATCH = 300      # ops per transaction; ~2s against the master at this size
FLUSH_TIMEOUT = 120    # generous: a timeout here wedges the queue, see _flush


def _flush_batch(con, rows):
    """Push one batch of outbox rows. Returns (ok, error, counts).

    Each queued ref is pushed as its CURRENT local state: present locally means
    upsert, gone means delete, whatever kind the op was queued as. Reading the
    kind instead got delete-then-recreate backwards (the upsert went first and
    the delete won), and the local file is the truth the master mirrors."""
    node_ids, edge_refs, row_refs = set(), set(), set()
    for r in rows:
        kind = r["kind"]
        if kind in ("node", "del_node"):
            node_ids.add(r["ref"])
        elif kind in ("edge", "del_edge"):
            edge_refs.add(r["ref"])
        elif kind in ("row", "del_row"):
            row_refs.add(r["ref"])
    nodes, del_ids = [], []
    for i in node_ids:
        p = _node_payload(con, i)
        if p:
            nodes.append(p)
        else:
            del_ids.append(i)
    edges, dedges = [], []
    for ref in edge_refs:
        parts = ref.split(EDGE_SEP)
        if len(parts) != 3:
            continue
        p = _edge_payload(con, *parts)
        if p:
            edges.append(p)
        else:
            dedges.append({**dict(zip(("src", "dst", "rel"), parts)), "ref": ref})
    up_rows, del_rows = [], []
    for ref in row_refs:
        table, _, key = ref.partition(EDGE_SEP)
        p = _row_payload(con, table, key)
        if p:
            up_rows.append(p)
        elif table in mirror_tables():
            del_rows.append(ref)
    counts = {"nodes": len(nodes), "edges": len(edges),
              "del_nodes": len(del_ids), "del_edges": len(dedges),
              "rows": len(up_rows), "del_rows": len(del_rows)}
    stmts = []
    if nodes:
        stmts.append({"statement": _NODE_UPSERT, "parameters": {"nodes": nodes}})
    if edges:
        stmts.append({"statement": _EDGE_UPSERT, "parameters": {"edges": edges}})
    if dedges:
        stmts.append({"statement": _EDGE_DEL, "parameters": {"edges": dedges}})
    if del_ids:
        stmts.append({"statement": _NODE_DEL, "parameters": {"ids": del_ids}})
    if up_rows:
        stmts.append({"statement": _ROW_UPSERT, "parameters": {"rows": up_rows}})
    if del_rows:
        stmts.append({"statement": _ROW_DEL, "parameters": {"uids": del_rows}})
    if not stmts:
        return True, None, counts          # nothing left to push; safe to dequeue
    _ensure_schema_once()
    ok, err = _neo4j_call(_stamped(con, stmts), timeout=FLUSH_TIMEOUT)
    return ok, err, counts


def _stamped(con, stmts):
    """Give every write the parameters its stamp needs."""
    origin = machine_id(con)
    for s in stmts:
        s.setdefault("parameters", {}).update(origin=origin, sep=EDGE_SEP)
    return stmts


def _sync_get(con, key, default=""):
    r = con.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def _sync_set(con, key, value) -> None:
    con.execute("INSERT INTO sync_state(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def machine_id(con) -> str:
    """Who this brain file is on the master, made once. A pull skips changes it
    wrote itself: it already holds them, and applying its own echo could only
    ever roll something back."""
    mid = _sync_get(con, "machine_id")
    if not mid:
        import socket
        host = slugify(socket.gethostname().split(".")[0])[:24] or "machine"
        mid = f"{host}-{secrets.token_hex(3)}"
        _sync_set(con, "machine_id", mid)
        con.commit()
    return mid


_SCHEMA_CHECKED = False


def _ensure_schema_once() -> None:
    """The constraints, once per process, before the first push. Best effort: a
    failure here is retried by the next process, and the push itself reports."""
    global _SCHEMA_CHECKED
    if not _SCHEMA_CHECKED:
        ok, _ = _ensure_neo4j_schema()
        _SCHEMA_CHECKED = ok


def _flush(con):
    """Drain the outbox to neo4j in bounded batches. Upserts current state of
    queued nodes/edges and applies queued deletes.

    Batching is load-bearing, not tidiness. The outbox used to be sent as ONE
    transaction: a daily consolidate pass can enqueue ~1,700 ops, that payload
    needed ~14s against the master, and the client gave up at 6s. Nothing was
    dequeued on failure, so every later write made the batch bigger and slower
    — a one-way ratchet that the queue could never escape. Bounded batches mean
    a backlog always drains, just over more round-trips.

    Each batch is deleted from the queue only after its push succeeds, so an
    interruption costs at most a replay of one batch (every statement is an
    idempotent MERGE/DELETE, so replay is safe)."""
    pushed = {"nodes": 0, "edges": 0, "del_nodes": 0, "del_edges": 0,
              "rows": 0, "del_rows": 0}
    batches = 0
    while True:
        rows = con.execute(
            "SELECT seq,kind,ref FROM outbox ORDER BY seq LIMIT ?", (FLUSH_BATCH,)).fetchall()
        if not rows:
            break
        ok, err, counts = _flush_batch(con, rows)
        if not ok:
            pending = con.execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"]
            out = {"ok": False, "error": err, "pending": pending, "batches": batches}
            if batches:
                out["pushed"] = pushed        # partial progress is real progress
            return out
        con.executemany("DELETE FROM outbox WHERE seq=?", [(r["seq"],) for r in rows])
        con.commit()
        for k in pushed:
            pushed[k] += counts[k]
        batches += 1
    return {"ok": True, "pending": 0, "batches": batches,
            "pushed": pushed if batches else 0}


def _after_write(con, nodes=(), edges=(), del_nodes=(), del_edges=()):
    """Queue changes for neo4j and (unless suppressed) push immediately."""
    for nid in nodes:
        _enqueue(con, "node", nid)
    for e in edges:
        _enqueue(con, "edge", EDGE_SEP.join(e))
    for nid in del_nodes:
        _enqueue(con, "del_node", nid)
    for e in del_edges:
        _enqueue(con, "del_edge", EDGE_SEP.join(e))
    con.commit()
    # flag() folds in $MEMORY_LEDGER_NO_AUTOSYNC, so a bulk load that exports it
    # still queues without pushing — it just is no longer the ONLY way to say so.
    if not flag("autosync"):
        return None
    return _flush(con)


def _node_dict(row) -> dict:
    d = dict(row)
    d.pop("cues", None)   # generated FTS input, not part of the node (see _ensure_fts)
    try:
        d["props"] = json.loads(d.get("props") or "{}")
    except (json.JSONDecodeError, TypeError):
        d["props"] = {}
    return d


def _gen_id(con, node_type: str, title: str) -> str:
    base = f"{slugify(node_type)}_{slugify(title)}"
    for _ in range(50):
        cand = f"{base}_{secrets.token_hex(2)}"
        exists = con.execute("SELECT 1 FROM nodes WHERE id=?", (cand,)).fetchone()
        if not exists:
            return cand
    return f"{base}_{secrets.token_hex(6)}"


def out(obj) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")


def _parse_props(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {"value": val}
    except json.JSONDecodeError:
        return {"note": raw}


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_init(args):
    # Adopt first: on a machine that predates the rename this points
    # ~/.memory-ledger at the existing ~/.second_brain, so the connect() below
    # opens the brain that is already there instead of starting a second one.
    home = adopt_canonical_home()
    con = connect()
    skills = ensure_builtin_skills(con)
    con.close()
    out({"ok": True, "db": str(db_path()), "home": home, "skills": skills})


def cmd_backups(args):
    """List the snapshots, take one, or expire the old ones.

    Snapshots exist so consolidation is undoable (see backup_db). They are
    reachable from here as well as the web UI, because a recovery path that
    only exists in a browser is not a recovery path."""
    res = {"ok": True, "dir": str(backups_dir()), "keep_days": BACKUP_KEEP_DAYS}
    if args.new:
        res["created"] = backup_db(args.label or "manual")
    if args.prune:
        res["removed"] = prune_backups()
    res["backups"] = list_backups()
    out(res)


def cmd_disable(args):
    """Turn the ledger off machine-wide: write `<home>/.disabled`. Idempotent —
    calling this twice is a no-op the second time. Every capture hook and the
    bootstrap check `disabled()` first, so this is enough to stop capture
    without uninstalling anything or deleting data."""
    home_dir().mkdir(parents=True, exist_ok=True)
    marker = home_dir() / DISABLE_MARKER
    marker.touch(exist_ok=True)
    res = {"ok": True, "disabled": True, "marker": str(marker)}
    if _env("OFF").strip().lower() in _OFF_TRUTHY:
        res["note"] = ("$MEMORY_LEDGER_OFF/$SECOND_BRAIN_OFF is also set in this "
                        "shell — that was already forcing the ledger off.")
    out(res)


def cmd_enable(args):
    """Turn the ledger back on: remove `<home>/.disabled` if present. Idempotent
    — calling this when already enabled is a no-op. NOTE: if
    $MEMORY_LEDGER_OFF/$SECOND_BRAIN_OFF is set in the current shell, this
    command cannot turn capture back on for that shell — the env var wins over
    the marker file every time `disabled()` is checked (see its docstring)."""
    marker = home_dir() / DISABLE_MARKER
    existed = marker.exists()
    if existed:
        marker.unlink()
    res = {"ok": True, "disabled": False, "marker_removed": existed}
    if _env("OFF").strip().lower() in _OFF_TRUTHY:
        res["warning"] = ("$MEMORY_LEDGER_OFF/$SECOND_BRAIN_OFF is set in this "
                           "shell — the ledger is STILL OFF here despite removing "
                           "the marker. Unset that env var (or fix it wherever it "
                           "is exported: rc file, cron, launchd) to actually "
                           "resume capture in this shell.")
    out(res)


def cmd_add_node(args):
    con = connect()
    area = getattr(args, "area", None)
    aid = None
    if area:
        # Resolve BEFORE creating, so a typo'd area never leaves an orphan node
        # sitting outside every domain.
        aid, err = _resolve_area(con, area)
        if err:
            out(err); return
    # A person goes through the identity guard, because this is the door the
    # ingest skills come through — the same colleague arrives under a nickname one
    # week and a full name the next, and two nodes is how their tasks get split.
    # --force-new is the escape hatch for two people who really do share a name.
    if args.type == "person" and not getattr(args, "force_new", False) and not args.id:
        res = api_create_person(con, args.title, args.body,
                                _parse_props(args.props),
                                getattr(args, "realm", None) or "")
    else:
        res = api_create_node(con, args.type, args.title, args.body,
                              _parse_props(args.props), args.status, args.due,
                              getattr(args, "realm", None), args.id)
    if aid and res.get("ok"):
        api_link(con, res["node"]["id"], aid, AREA_REL)
        res["area"] = aid
    out(res)


def cmd_people(args):
    con = connect()
    people = person_rows(con, include_superseded=getattr(args, "all", False))
    out({"ok": True, "count": len(people), "people": [
        {"id": d["id"], "title": d["title"], "realm": d.get("realm", ""),
         "aliases": person_aliases(d)[1:], "edges": _person_degree(con, d["id"]),
         "superseded_by": (d["props"] or {}).get("superseded_by", "")}
        for d in people]})


def cmd_who(args):
    """Which person node does this spelling mean? The read side of the guard."""
    out(resolve_person(connect(), args.name))


def cmd_person_dupes(args):
    con = connect()
    groups = person_duplicates(con)
    out({"ok": True, "groups": len(groups), "duplicates": groups,
         "hint": "graph.py merge-person --into <canonical> --from <duplicate>"})


def cmd_merge_person(args):
    out(api_merge_person(connect(), args.into, args.from_id))


def cmd_add_alias(args):
    out(api_add_alias(connect(), args.id, args.alias))


def cmd_update_node(args):
    con = connect()
    out(api_update_node(con, args.id, title=args.title, body=args.body,
                        status=args.status, due=args.due,
                        realm=getattr(args, "realm", None),
                        props_patch=(_parse_props(args.props)
                                     if args.props is not None else None)))


def cmd_del_node(args):
    con = connect()
    cur = con.execute("DELETE FROM nodes WHERE id=?", (args.id,))
    con.commit()
    sync = _after_write(con, del_nodes=[args.id])
    out({"ok": cur.rowcount > 0, "deleted": args.id, "sync": sync})


def cmd_add_edge(args):
    con = connect()
    out(api_link(con, args.src, args.dst, args.rel, _parse_props(args.props)))


def cmd_del_edge(args):
    con = connect()
    cur = con.execute(
        "DELETE FROM edges WHERE src=? AND dst=? AND rel=?",
        (args.src, args.dst, args.rel),
    )
    con.commit()
    sync = _after_write(con, del_edges=[(args.src, args.dst, args.rel)])
    out({"ok": cur.rowcount > 0, "sync": sync})


def _edges_of(con, nid):
    rows = con.execute(
        "SELECT e.src,e.dst,e.rel,e.props,"
        " s.title AS src_title, s.type AS src_type,"
        " d.title AS dst_title, d.type AS dst_type"
        " FROM edges e JOIN nodes s ON s.id=e.src JOIN nodes d ON d.id=e.dst"
        " WHERE e.src=? OR e.dst=?",
        (nid, nid),
    ).fetchall()
    return [dict(r) for r in rows]


def cmd_get_node(args):
    con = connect()
    row = con.execute("SELECT * FROM nodes WHERE id=?", (args.id,)).fetchone()
    if not row:
        out({"ok": False, "error": f"no node {args.id}"}); return
    out({"ok": True, "node": _node_dict(row), "edges": _edges_of(con, args.id)})


def cmd_search(args):
    con = connect()
    q = (args.query or "").strip()
    params, where = [], []
    if q:
        # quote each term for FTS to tolerate punctuation / partial words
        terms = " ".join(f'"{t}"*' for t in re.findall(r"\w+", q))
        rows = con.execute(
            "SELECT n.* FROM nodes_fts f JOIN nodes n ON n.rowid=f.rowid"
            " WHERE nodes_fts MATCH ? ORDER BY rank",
            (terms,),
        ).fetchall() if terms else []
    else:
        rows = con.execute("SELECT * FROM nodes ORDER BY updated DESC").fetchall()
    results = [_node_dict(r) for r in rows]
    if args.type:
        results = [r for r in results if r["type"] == args.type]
    results = results[: args.limit]
    if getattr(args, "full", False):
        out({"ok": True, "count": len(results), "results": results}); return
    # BUDGETED BY DEFAULT. Uncapped, this returned whole bodies — ~9,000 chars
    # where `recall` answered the same cue in ~1,500. Bodies are trimmed to share
    # the budget; read one in full with `get-node --id`.
    budget = int(getattr(args, "budget", None) or READ_BUDGET)
    # Only the props a caller routinely decides on. Emitting every prop is what
    # made the budget non-binding: props alone can outweigh the body.
    KEEP_PROPS = ("priority", "assignee", "waiting_on", "jira", "pr", "state",
                  "due", "section", "source", "attributed_to")
    slim, used, truncated = [], 0, 0
    for i, r in enumerate(results):
        room = max(160, (budget - used) // max(1, len(results) - i))
        body = r["body"] or ""
        entry = {"id": r["id"], "type": r["type"], "title": r["title"]}
        for k in ("status", "due"):
            if r.get(k):
                entry[k] = r[k]
        p = r["props"] or {}
        keep = {k: p[k] for k in KEEP_PROPS if p.get(k)}
        if keep:
            entry["props"] = keep
        entry["body"] = body[:room]
        if len(body) > room:
            entry["truncated"] = True; truncated += 1
        used += len(json.dumps(entry))
        slim.append(entry)
        if used >= budget:
            break
    out({"ok": True, "count": len(slim), "of": len(results), "chars": used,
         "budget": budget, "truncated": truncated,
         "hint": "full body: get-node --id · everything: --full",
         "results": slim})


def cmd_list(args):
    con = connect()
    where, params = [], []
    area = getattr(args, "area", None)
    if area:
        aid, err = _resolve_area(con, area)
        if err:
            out(err); return
        where.append(
            "id IN (SELECT src FROM edges WHERE dst=? AND rel=?)")
        params.extend([aid, AREA_REL])
    if args.type:
        where.append("type=?"); params.append(args.type)
    if args.status:
        where.append("status=?"); params.append(args.status)
    elif not args.all:
        # hide the archive from active views unless explicitly asked
        placeholders = ",".join("?" * len(DONE_STATUSES))
        where.append(f"status NOT IN ({placeholders})")
        params.extend(DONE_STATUSES)
    if args.due_before:
        where.append("due!='' AND due<=?"); params.append(args.due_before)
    # External task boards (§2c): `--untracked` is what a board pulls, and
    # `--tracked-by NAME` is what it already holds. Both are JSON reads on
    # props, so they filter in SQL and the LIMIT still means what it says.
    if getattr(args, "untracked", False):
        where.append("COALESCE(json_extract(props, '$.tracked_by.system'), '') = ''")
    if getattr(args, "tracked_by", None):
        where.append("json_extract(props, '$.tracked_by.system') = ?")
        params.append(args.tracked_by)
    sql = "SELECT * FROM nodes"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY CASE WHEN due='' THEN 1 ELSE 0 END, due, updated DESC"
    sql += f" LIMIT {int(args.limit)}"
    rows = con.execute(sql, params).fetchall()
    out({"ok": True, "count": len(rows), "results": [_node_dict(r) for r in rows]})


def _parse_dt(s):
    """Parse an ISO-8601 due string to an aware UTC datetime. Naive values are
    assumed UTC. Compare these, never the raw strings — mixed tz offsets make
    lexical comparison wrong (07:20-03:00 is LATER than 08:00+00:00)."""
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def cmd_due(args):
    con = connect()
    now_dt = datetime.now(timezone.utc)
    horizon_dt = now_dt + timedelta(days=args.within_days)
    rows = con.execute(
        "SELECT * FROM nodes WHERE due!='' "
        "AND status NOT IN ('done','cancelled')",
    ).fetchall()
    items = []
    for r in rows:
        d = _node_dict(r)
        due_dt = _parse_dt(d["due"])
        if due_dt is None or due_dt > horizon_dt:
            continue
        d["overdue"] = due_dt < now_dt
        d["_sort"] = due_dt
        items.append(d)
    items.sort(key=lambda x: x.pop("_sort"))
    out({"ok": True, "now": now_dt.isoformat(timespec="seconds"),
         "count": len(items), "results": items})


def _walk(con, start, depth, rel_filter):
    seen = {start}
    frontier = [start]
    collected_edges = []
    for _ in range(depth):
        nxt = []
        for nid in frontier:
            for e in _edges_of(con, nid):
                if rel_filter and e["rel"] != rel_filter:
                    continue
                collected_edges.append(e)
                other = e["dst"] if e["src"] == nid else e["src"]
                if other not in seen:
                    seen.add(other); nxt.append(other)
        frontier = nxt
        if not frontier:
            break
    nodes = []
    for nid in seen:
        row = con.execute("SELECT * FROM nodes WHERE id=?", (nid,)).fetchone()
        if row:
            nodes.append(_node_dict(row))
    # de-dup edges
    uniq = {(e["src"], e["dst"], e["rel"]): e for e in collected_edges}
    return nodes, list(uniq.values())


def cmd_neighbors(args):
    con = connect()
    if not con.execute("SELECT 1 FROM nodes WHERE id=?", (args.id,)).fetchone():
        out({"ok": False, "error": f"no node {args.id}"}); return
    nodes, edges = _walk(con, args.id, args.depth, args.rel)
    out({"ok": True, "center": args.id, "nodes": nodes, "edges": edges})


def cmd_stats(args):
    con = connect()
    by_type = {r["type"]: r["c"] for r in con.execute(
        "SELECT type, COUNT(*) c FROM nodes GROUP BY type").fetchall()}
    ntot = con.execute("SELECT COUNT(*) c FROM nodes").fetchone()["c"]
    etot = con.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"]
    out({"ok": True, "nodes": ntot, "edges": etot, "by_type": by_type,
         "db": str(db_path())})


def cmd_export(args):
    con = connect()
    nodes = [_node_dict(r) for r in con.execute(
        "SELECT * FROM nodes ORDER BY type,created").fetchall()]
    edges = [dict(r) for r in con.execute(
        "SELECT src,dst,rel,props,created FROM edges").fetchall()]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = Path(args.out).expanduser() if args.out else (
        db_path().parent / "exports" / stamp)
    base.mkdir(parents=True, exist_ok=True)

    if args.format in ("json", "both"):
        (base / "nodes.json").write_text(
            json.dumps(nodes, indent=2, ensure_ascii=False), encoding="utf-8")
        (base / "edges.json").write_text(
            json.dumps(edges, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.format in ("md", "both"):
        adj = {}
        for e in edges:
            adj.setdefault(e["src"], []).append((e["rel"], e["dst"]))
            adj.setdefault(e["dst"], []).append(("(" + e["rel"] + ")", e["src"]))
        lines = ["# memory-ledger memory export", f"_generated {now()}_", ""]
        for n in nodes:
            lines.append(f"## {n['title']}  `{n['id']}`")
            meta = [f"type: {n['type']}"]
            if n["status"]:
                meta.append(f"status: {n['status']}")
            if n["due"]:
                meta.append(f"due: {n['due']}")
            lines.append("- " + " · ".join(meta))
            if n["body"]:
                lines.append("")
                lines.append(n["body"])
            links = adj.get(n["id"], [])
            if links:
                lines.append("")
                lines.append("Links: " + ", ".join(
                    f"{rel} [[{dst}]]" for rel, dst in links))
            lines.append("")
        (base / "memory.md").write_text("\n".join(lines), encoding="utf-8")
    out({"ok": True, "exported_to": str(base), "nodes": len(nodes),
         "edges": len(edges), "format": args.format})


# ---------------------------------------------------------------------------
# completion / archive  (done work is moved, never deleted)
# ---------------------------------------------------------------------------
def cmd_complete(args):
    con = connect()
    out(api_complete(con, args.id, args.note))


def cmd_done(args):
    con = connect()
    rows = con.execute(
        "SELECT * FROM nodes WHERE status='done' ORDER BY updated DESC LIMIT ?",
        (int(args.limit),)).fetchall()
    out({"ok": True, "count": len(rows), "results": [_node_dict(r) for r in rows]})


# ---------------------------------------------------------------------------
# learned skills  (neuroplasticity: the brain grows + strengthens abilities)
# ---------------------------------------------------------------------------
_SKILL_TEMPLATE = """---
name: {name}
description: {title}
---

# {title}

**When to use (triggers):** {when}

{body}

---
_Learned skill — created at runtime by memory-ledger on {date}. The graph node
`{node_id}` tracks how often it fires and reinforces the trigger over time._
"""


def cmd_add_skill(args):
    """Create a runtime-learned skill: write learned_skills/<name>/SKILL.md AND
    a `skill` node whose body/triggers make it findable by `match-skills`."""
    con = connect()
    name = slugify(args.name)
    when = args.when or ""
    body = args.body or ""
    node_id = args.id or f"skill_{name}"
    # body carries the triggers so full-text match surfaces the skill by situation
    node_body = f"WHEN TO USE: {when}\n\n{body}"
    skill_path = learned_skills_dir() / name / "SKILL.md"

    existing = con.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
    ts = now()
    if existing:
        cur = _node_dict(existing)
        props = {**cur["props"], "triggers": when, "path": str(skill_path),
                 "kind": cur["props"].get("kind") or "learned",
                 **_skill_contract_props(args),
                 "updated_at": ts}
        con.execute("UPDATE nodes SET title=?, body=?, props=?, updated=? WHERE id=?",
                    (args.title, node_body, json.dumps(props), ts, node_id))
    else:
        props = {"triggers": when, "path": str(skill_path), "kind": "learned",
                 **_skill_contract_props(args),
                 "usage_count": 0, "confidence": 1.0, "learned_at": ts}
        con.execute(
            "INSERT INTO nodes(id,type,title,body,props,status,due,created,updated)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (node_id, "skill", args.title, node_body, json.dumps(props),
             "active", "", ts, ts))
    con.commit()
    sync = _after_write(con, nodes=[node_id])

    # Node-first: a learned process lives in the DB (portable, synced everywhere).
    # A markdown file is written ONLY when explicitly asked for (--with-file) — the
    # DB is the source of truth, the file is just an optional local export.
    wrote_file = False
    if args.with_file:
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(_SKILL_TEMPLATE.format(
            name=name, title=args.title, when=when or "(describe when to use)",
            body=body or "(steps)", date=ts, node_id=node_id), encoding="utf-8")
        wrote_file = True

    row = con.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
    out({"ok": True, "skill": _node_dict(row), "stored": "db",
         "skill_file": str(skill_path) if wrote_file else None})


def cmd_match_skills(args):
    """Given a situation, return learned skills whose triggers/body match, most
    reinforced first — this is the brain 'recognizing' a situation it has
    learned to handle."""
    con = connect()
    q = (args.query or "").strip()
    if q:
        # OR the cue words (ignore tiny stopwords) so a PARTIAL cue can trigger
        # recall — a situation need only overlap the skill's triggers, not match
        # every word. This is the associative-recall behaviour of the brain.
        words = [t for t in re.findall(r"\w+", q.lower()) if len(t) > 2]
        terms = " OR ".join(f'"{t}"*' for t in words)
        rows = con.execute(
            "SELECT n.* FROM nodes_fts f JOIN nodes n ON n.rowid=f.rowid"
            " WHERE nodes_fts MATCH ? AND n.type='skill' ORDER BY rank",
            (terms,)).fetchall() if terms else []
        results = [_node_dict(r) for r in rows]  # already in FTS relevance order
        # Rank by RELEVANCE first (FTS order), with reinforcement as a bounded
        # booster: each prior use nudges a skill up, capped, so a strongly-learned
        # skill edges ahead of equally-relevant peers but a weak match can never
        # outrank a strong one. (Before: pure usage sort buried the best match.)
        WEIGHT, CAP = 0.5, 6
        for i, r in enumerate(results):
            uses = int(r["props"].get("usage_count", 0) or 0)
            r["_score"] = i - min(uses, CAP) * WEIGHT
        results.sort(key=lambda r: r["_score"])
        for r in results:
            r.pop("_score", None)
    else:
        # no cue -> nothing to be relevant to; surface the most-reinforced skills
        rows = con.execute(
            "SELECT * FROM nodes WHERE type='skill' ORDER BY updated DESC").fetchall()
        results = [_node_dict(r) for r in rows]
        results.sort(key=lambda r: r["props"].get("usage_count", 0), reverse=True)
    results = results[: args.limit]
    # TOKEN COST: this runs on EVERY routed request (CONVENTIONS §3), and the full
    # procedure bodies made it ~8.1k chars. The caller only needs to pick a skill
    # here — it reads the procedure with `get-node --id` once it has. So return a
    # cue-sized row by default; `--full` restores the old payload.
    if not getattr(args, "full", False):
        slim = []
        for r in results:
            p = r["props"]
            slim.append({"id": r["id"], "title": r["title"],
                         "triggers": (p.get("triggers") or "")[:220],
                         "uses": int(p.get("usage_count", 0) or 0),
                         "strength": _strength(p),
                         "read_procedure": f"get-node --id {r['id']}"})
        results = slim
    con2 = connect()
    _touch(con2, [r["id"] for r in results])   # recognising a skill reinforces it
    out({"ok": True, "count": len(results), "results": results})


def _plugin_skills_dir() -> Path:
    """The plugin's authored skills live beside the engine (../skills)."""
    return Path(__file__).resolve().parent.parent / "skills"


def _parse_frontmatter(text: str):
    """Split a `---`-delimited YAML-ish frontmatter block from the body. Returns
    (meta, body). Values are single-line `key: value` pairs (enough for skills)."""
    meta, body = {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line and not line.lstrip().startswith("#"):
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        body = m.group(2)
    return meta, body


def _extract_triggers(meta: dict, body: str) -> str:
    """Best-effort 'when to use' cue for a skill: an explicit When-to-use line,
    else a 'Use ...' clause in the description, else the whole description."""
    m = re.search(r"(?im)^\**\s*when to use[^\n:]*:\**\s*(.+)$", body)
    if m and m.group(1).strip():
        return m.group(1).strip()
    m = re.search(r"(?im)^\s*##+\s*when to use\s*\n+(.+?)(?:\n\s*\n|\Z)", body, re.DOTALL)
    if m and m.group(1).strip():
        return " ".join(m.group(1).split())
    desc = meta.get("description", "")
    m = re.search(r"(Use (?:for|when|this)[^.]*\.)", desc)
    return (m.group(1) if m else desc).strip()


def _ingest_skill_file(con, path: Path, kind: str, realm: str):
    """Upsert one SKILL.md as a `skill` node — triggers + full procedure land in
    the DB (portable, synced), so the skill is no longer isolated in a file."""
    text = path.read_text(encoding="utf-8", errors="replace")
    meta, body = _parse_frontmatter(text)
    name = slugify(meta.get("name") or path.parent.name or path.stem)
    # `name` is a slug in every SKILL.md that has one, so using it as the title
    # renamed a skill to its own id on the way back in. An export writes the real
    # title out separately; a file without one keeps the old fallback.
    title = meta.get("title") or meta.get("name") or name
    triggers = _extract_triggers(meta, body)
    node_id = f"skill_{name}"
    node_body = f"WHEN TO USE: {triggers}\n\n{body.strip()}"
    ts = now()
    kind = meta.get("kind") or kind
    # A learned skill has no file the tree can re-read at sync time (only a
    # built-in does, §5a), so its declarations have to land in props HERE or the
    # import arrives with its dependencies silently dropped.
    extra = {}
    if kind != "plugin":
        decl = _parse_depends_on(body)
        for key, _label in DECL_LABELS:
            toks = decl.get(key.replace("_", "-") if key in ("waits_for", "enriched_by")
                            else key, [])
            extra[key] = ", ".join(decl_tokens(toks))
    existing = con.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
    if existing:
        cur = _node_dict(existing)
        props = {**cur["props"], "triggers": triggers, "kind": kind,
                 "source_path": str(path), "description": meta.get("description", ""),
                 **extra, "ingested_at": ts}
        con.execute("UPDATE nodes SET title=?, body=?, props=?, realm=?, updated=? WHERE id=?",
                    (title, node_body, json.dumps(props),
                     cur.get("realm") or meta.get("realm") or realm, ts, node_id))
        action = "updated"
    else:
        props = {"triggers": triggers, "kind": kind, "source_path": str(path),
                 "description": meta.get("description", ""), "usage_count": 0,
                 "confidence": 1.0, "learned_at": ts, "ingested_at": ts, **extra}
        con.execute(
            "INSERT INTO nodes(id,type,title,body,props,status,due,realm,created,updated)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (node_id, "skill", title, node_body, json.dumps(props),
             "active", "", meta.get("realm") or realm, ts, ts))
        action = "created"
    # `rules:` in the frontmatter is the portable half of the honors edge. A rule
    # that did not travel is reported rather than invented: a skill pointing at a
    # rule id this brain has never held would read as accountable and enforce
    # nothing.
    missing = []
    for rid in decl_tokens(meta.get("rules", "")):
        if con.execute("SELECT 1 FROM nodes WHERE id=? AND type=?",
                       (rid, RULE_TYPE)).fetchone():
            api_link(con, node_id, rid, HONORS_REL)
        else:
            missing.append(rid)
    return node_id, action, missing


def _skill_contract_props(args) -> dict:
    """The §5a declarations a user skill carries in props, since it has no file."""
    keys = ("requires", "waits_for", "enriched_by", "reads", "writes")
    return {k: getattr(args, k) for k in keys if getattr(args, k, None)}


def _parse_depends_on(text: str) -> dict:
    """Read a SKILL.md `## Depends on` block into the declarations §5a defines.

    The file is the source of truth for a built-in skill: the same block the
    contract checker validates is the one the tree is built from, so the document,
    the skill and the graph cannot drift apart."""
    m = re.search(r"^## Depends on\s*$(.*?)(?=^## )", text, re.M | re.S)
    if not m:
        return {}
    block = m.group(1)
    kinds = {"Requires": "requires", "Optional (ordered)": "waits-for",
             "Optional (enriching)": "enriched-by",
             "Reads": "reads", "Writes": "writes"}
    found = {}
    for line in block.splitlines():
        lm = re.match(r"\s*-\s*\*\*([^:*]+):\*\*(.*)", line)
        if not lm:
            continue
        label, rest = lm.group(1).strip(), lm.group(2)
        rel = kinds.get(label)
        if not rel:
            continue
        if re.match(r"\s*none\b", rest, re.I):
            found.setdefault(rel, [])
            continue
        found.setdefault(rel, []).extend(re.findall(r"`([^`]+)`", rest))
    return found


def _skill_declarations(con, row) -> tuple:
    """(kind, declarations) for one skill node.

    A built-in skill declares in its SKILL.md and the file wins, because the
    checker gates that file at commit time. A user skill has no file, so it
    declares in props (`add-skill --requires/--reads/--writes`)."""
    props = json.loads(row["props"] or "{}")
    kind = props.get("kind")
    path = props.get("source_path") or props.get("path") or ""
    if not kind:
        kind = "plugin" if "/skills/" in path and path.endswith("SKILL.md") else "learned"
    decl = {}
    if kind == "plugin" and path and Path(path).exists():
        try:
            decl = _parse_depends_on(Path(path).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            decl = {}
    if not decl:
        decl = {rel: [t.strip() for t in str(props.get(key, "")).split(",") if t.strip()]
                for rel, key in (("requires", "requires"), ("waits-for", "waits_for"),
                                 ("enriched-by", "enriched_by"), ("reads", "reads"),
                                 ("writes", "writes"))}
    return kind, decl


def skill_trees(con, with_rules: bool = True) -> list:
    """The two skill trees as the UI draws them (CONVENTIONS §5a).

    Built from the SAME edges `sync-skill-tree` wrote, never from a second pass
    over the files, so the page cannot show a tree the graph does not hold. Run
    the sync first if a declaration changed."""
    skills = {r["id"]: _node_dict(r) for r in con.execute(
        "SELECT * FROM nodes WHERE type='skill' ORDER BY title").fetchall()}
    edges = [dict(r) for r in con.execute(
        "SELECT src,dst,rel,props FROM edges WHERE src IN "
        "(SELECT id FROM nodes WHERE type='skill')").fetchall()]
    tree_edges = [e for e in edges
                  if json.loads(e["props"] or "{}").get("src") == "skill-tree"]

    # A skill's domain is the area it is filed under, minus the two tree hubs —
    # those say which tree it is in, which is already a facet of its own.
    area_of = {}
    for r in con.execute(
            "SELECT e.src, e.dst FROM edges e JOIN nodes n ON n.id=e.src"
            " WHERE n.type='skill' AND e.rel='part-of' AND e.dst LIKE 'hub_%'"):
        if not r["dst"].startswith("hub_skills_"):
            area_of.setdefault(r["src"], set()).add(r["dst"])

    hub_of, dep, io = {}, {}, {}
    for e in tree_edges:
        if e["rel"] == "part-of" and e["dst"].startswith("hub_skills_"):
            hub_of[e["src"]] = e["dst"]
        elif e["rel"] in ("requires", "waits-for", "enriched-by"):
            dep.setdefault(e["src"], []).append((e["rel"], e["dst"]))
        elif e["rel"] in ("reads", "writes"):
            io.setdefault(e["src"], []).append((e["rel"], e["dst"][len("nodetype_"):]))

    def entry(sid):
        n = skills[sid]
        p = n["props"]
        d = sorted(dep.get(sid, []))
        row = {
            "id": sid, "title": n["title"],
            "description": p.get("description") or "",
            "triggers": p.get("triggers") or "",
            "kind": p.get("kind") or "learned",
            "uses": int(p.get("usage_count", 0) or 0),
            "requires": [x for r, x in d if r == "requires"],
            "waits_for": [x for r, x in d if r == "waits-for"],
            "enriched_by": [x for r, x in d if r == "enriched-by"],
            "reads": sorted({t for r, t in io.get(sid, []) if r == "reads"}),
            "writes": sorted({t for r, t in io.get(sid, []) if r == "writes"}),
            "reads_other": p.get("reads_other", ""),
            "writes_other": p.get("writes_other", ""),
            "domains": sorted(area_of.get(sid, [])),
            "declared": False, "rules": [],
        }
        if with_rules:
            # Two different questions, both worth answering. DECLARED is what the
            # skill says it is accountable for. MATCHED is what match-rules will
            # actually hand it at runtime, from THIS brain — which is where a
            # person's own rules show up against a skill that never named them.
            seen = [{"id": r["id"], "title": r["title"],
                      "source": r["props"].get("source", ""),
                      "non_negotiable": bool(r["props"].get("non_negotiable")),
                      "linked": r["linked"], "why": "declared"}
                     for r in honored_rules(con, sid)]
            row["declared"] = bool(seen)
            cue = " ".join(filter(None, [n["title"], row["triggers"][:400]]))
            for r in rules_for(con, cue, 6):
                if r["id"] in {x["id"] for x in seen}:
                    continue
                nn = bool(r["props"].get("non_negotiable"))
                # A non-negotiable is appended to EVERY match, so calling it
                # "matched" here would be a lie that buries the handful of rules
                # this skill actually pulled. It gets its own bucket.
                seen.append({"id": r["id"], "title": r["title"],
                             "source": r["props"].get("source", ""),
                             "non_negotiable": nn,
                             "why": "always" if nn else "matched"})
            row["rules"] = seen
        return row

    trees = []
    for kind, (hub_id, hub_title) in TREE_HUBS.items():
        members = [s for s in skills if hub_of.get(s) == hub_id]
        rows = {s: entry(s) for s in members}
        # Nest a skill under the one it REQUIRES. Requires is the only hard edge,
        # so it is the only one that makes a hierarchy rather than a cross-link;
        # waits-for and enriched-by stay listed on the card, where they read as
        # what they are — relationships, not parentage.
        children = {}
        for sid, row in rows.items():
            parent = next((r for r in row["requires"] if r in rows), None)
            children.setdefault(parent, []).append(sid)
        def branch(sid):
            node = rows[sid]
            node["children"] = [branch(c) for c in
                                sorted(children.get(sid, []),
                                       key=lambda x: rows[x]["title"].lower())]
            return node
        roots = sorted(children.get(None, []), key=lambda x: rows[x]["title"].lower())
        trees.append({"kind": kind, "hub": hub_id, "title": hub_title,
                      "count": len(members), "skills": [branch(r) for r in roots]})
    return trees


RULE_SNAPSHOT_TYPE = "rule-snapshot"
RULE_SNAPSHOT_REALM = "rules-backup"


SKILL_CONTRACT_SECTIONS = ["## When to use", "## Given", "## Rules honored",
                           "## Depends on", "## Deliverables", "## Judged against",
                           "## Guardrails", "## Reply"]
RULE_CONTRACT_SECTIONS = ["THE RULE", "JUDGED AGAINST"]


def cmd_audit_contracts(args):
    """Check the contracts that live in the DB, which no file checker can see.

    scripts/check-contracts.sh gates the built-in corpus at commit time by reading
    `skills/*/SKILL.md`. A learned skill and a user rule have no file, so this is
    their equivalent: it reads the node bodies. Reports, never rewrites, because
    the missing half of a contract is a judgment call and filling it in
    mechanically would produce exactly the unfalsifiable clause §5b exists to
    prevent."""
    con = connect()
    gaps = {"skills": [], "rules": []}

    for row in con.execute(
            "SELECT * FROM nodes WHERE type='skill' ORDER BY id"):
        n = _node_dict(row)
        if args.kind and n["props"].get("kind") != args.kind:
            continue
        missing = [h for h in SKILL_CONTRACT_SECTIONS if h not in (n["body"] or "")]
        if missing:
            gaps["skills"].append({"id": n["id"], "kind": n["props"].get("kind", ""),
                                   "missing": missing})

    for row in con.execute(
            "SELECT * FROM nodes WHERE type=? AND status!='superseded' ORDER BY id",
            (RULE_TYPE,)):
        n = _node_dict(row)
        src = n["props"].get("source", "")
        # Shipped rules are regenerated from CONVENTIONS.md, so their contract is
        # that document and a body edit here would be overwritten (§5b.3).
        if src == "plugin" or (args.source and src not in args.source):
            continue
        body = n["body"] or ""
        missing = [h for h in RULE_CONTRACT_SECTIONS if h not in body]
        if not n["props"].get("scenario"):
            missing.append("scenario (GOVERNS is rendered from it, §5b.2)")
        if missing:
            gaps["rules"].append({"id": n["id"], "source": src, "missing": missing})

    total = len(gaps["skills"]) + len(gaps["rules"])
    out({"ok": total == 0, "incomplete": total,
         "skills_missing": len(gaps["skills"]), "rules_missing": len(gaps["rules"]),
         "gaps": gaps if not args.count_only else None,
         "hint": "a contract is completed by hand — see CONVENTIONS §5.1 and §5b.1"})


def cmd_snapshot_rules(args):
    """Copy every rule VERBATIM into its own realm before anything rewrites them.

    Taken before the §5b conversion, and worth taking before any bulk edit of the
    genome. A snapshot is a `rule-snapshot` node, NOT a `rule`, so `match-rules`
    can never return one and a backup cannot start behaving like a convention.
    It lives in the `rules-backup` realm, which keeps it out of every default view
    while leaving it searchable and diffable.

    Idempotent by content: re-running stores nothing for a rule whose body has not
    changed since its last snapshot under this label, and stores a new generation
    for one that has. Nothing is ever overwritten, because the point of a backup
    that can be overwritten is unclear."""
    con = connect()
    label = args.label or "pre-contract"
    rows = con.execute(
        "SELECT * FROM nodes WHERE type=? AND status!='superseded' ORDER BY id",
        (RULE_TYPE,)).fetchall()
    made, skipped, gens = [], [], []
    for row in rows:
        n = _node_dict(row)
        if args.source and n["props"].get("source") not in args.source:
            continue
        digest = hashlib.sha256((n["body"] or "").encode("utf-8")).hexdigest()[:12]
        base = f"rulesnap_{slugify(label)[:24]}_{n['id']}"
        prior = con.execute(
            "SELECT id, props FROM nodes WHERE type=? AND id LIKE ? ORDER BY id",
            (RULE_SNAPSHOT_TYPE, base + "%")).fetchall()
        if any(json.loads(r["props"] or "{}").get("digest") == digest for r in prior):
            skipped.append(n["id"]); continue
        gen = len(prior) + 1
        sid = base if gen == 1 else f"{base}__g{gen}"
        res = api_create_node(
            con, RULE_SNAPSHOT_TYPE, f"[{label}] {n['title']}", n["body"] or "",
            {"of": n["id"], "label": label, "digest": digest, "generation": gen,
             "taken_at": now(),
             # The whole node is captured, not just the prose: a rule's scenario is
             # what makes it fire, so a backup without it could not restore behavior.
             "orig_props": n["props"], "orig_title": n["title"],
             "orig_realm": n.get("realm", "")},
            "active", "", RULE_SNAPSHOT_REALM, sid)
        if res.get("ok"):
            api_link(con, sid, n["id"], "about")
            (gens if gen > 1 else made).append(sid)
    out({"ok": True, "label": label, "realm": RULE_SNAPSHOT_REALM,
         "rules_seen": len(rows), "snapshotted": len(made),
         "new_generations": len(gens), "unchanged": len(skipped),
         "restore_hint": "read one with get-node --id <snapshot id>; its orig_props "
                         "carries the scenario the rule fires on"})


def rule_catalog(con) -> list:
    """Every live rule, flattened for the UI's filter (CONVENTIONS §2g).

    Carries `scenario` as well as the body because that is the field match-rules
    actually fires on, so filtering by trigger has to see it: a rule whose body
    never says "slack" can still be the rule that fires when you type it."""
    areas = {}
    for r in con.execute(
            "SELECT e.src, e.dst FROM edges e JOIN nodes n ON n.id=e.src"
            " WHERE n.type=? AND e.rel='part-of' AND e.dst LIKE 'hub_%'", (RULE_TYPE,)):
        areas.setdefault(r["src"], set()).add(r["dst"])
    out_rows = []
    for row in con.execute(
            "SELECT * FROM nodes WHERE type=? AND status!='superseded' ORDER BY title",
            (RULE_TYPE,)):
        n = _node_dict(row)
        p = n["props"]
        out_rows.append({
            "id": n["id"], "title": n["title"],
            "source": p.get("source", ""), "section": str(p.get("section", "")),
            "non_negotiable": bool(p.get("non_negotiable")),
            "scenario": p.get("scenario", ""),
            "domains": sorted(areas.get(n["id"], [])),
            "chars": len(n["body"] or ""),
        })
    return out_rows


def node_type_catalog(con) -> list:
    """Every node type a skill can declare, with enough to choose one by.

    A picker that offers bare type names asks the person to already know what
    `gauge` or `transition` means. The count says whether this brain uses it at
    all, and the readers/writers say what would become coupled to a skill that
    declares it — which is the actual question §5a's reads/writes answer."""
    described = {r["title"]: r["body"] for r in con.execute(
        "SELECT title, body FROM nodes WHERE type='node-type'")}
    counts = {r["type"]: r["c"] for r in con.execute(
        "SELECT type, COUNT(*) c FROM nodes GROUP BY type")}
    touch = {}
    for r in con.execute(
            "SELECT e.rel, e.dst, n.title FROM edges e JOIN nodes n ON n.id=e.src"
            " WHERE e.rel IN ('reads','writes') AND e.dst LIKE 'nodetype_%'"):
        t = r["dst"][len("nodetype_"):]
        touch.setdefault(t, {}).setdefault(r["rel"], set()).add(r["title"])
    # NODE_TYPES is the declarable vocabulary, but §3 calls it soft and the live
    # graph legitimately holds types outside it. Offer both, or the picker would
    # be the one place that pretends the vocabulary is closed.
    names = list(dict.fromkeys(list(NODE_TYPES) + sorted(counts)))
    return [{"type": t, "body": described.get(t, ""), "nodes": counts.get(t, 0),
             "read_by": sorted(touch.get(t, {}).get("reads", [])),
             "written_by": sorted(touch.get(t, {}).get("writes", [])),
             "declarable": t in NODE_TYPES}
            for t in names]


def area_titles(con) -> dict:
    """hub id -> its title, so the UI labels a domain filter with the person's own
    words instead of a slug it made up."""
    return {r["id"]: r["title"] for r in con.execute(
        "SELECT id, title FROM nodes WHERE type=? AND id LIKE 'hub_%'", (AREA_TYPE,))}


# ---------------------------------------------------------------------------
# processes: the ways of working the brain has been told, beside the contracts
#
# A process is a node of type `process`, part-of `hub_process` (§2e). Unlike a
# skill or a rule it is REALM-scoped: a team's refinement process belongs to the
# work realm it was written in, so a new one lands in the brain's configured
# realm and the UI reads every realm, defaulting its filter to the current one.
# ---------------------------------------------------------------------------

PROCESS_TYPE = "process"
PROCESS_AREA = "hub_process"
# props a process carries as named frontmatter; anything else rides as `props:`
PROCESS_FIELDS = ("trigger", "established", "category")


def current_realm() -> str:
    """The realm new work lands in, read NOW rather than at import: the server
    is long-lived and the Settings page can change the realm under it."""
    return setting("realm", DEFAULT_REALM)


def process_catalog(con) -> list:
    """Every live process, in every realm — the filter narrows, not the query."""
    areas = {}
    for r in con.execute(
            "SELECT e.src, e.dst FROM edges e JOIN nodes n ON n.id=e.src"
            " WHERE n.type=? AND e.rel='part-of' AND e.dst LIKE 'hub_%'",
            (PROCESS_TYPE,)):
        areas.setdefault(r["src"], set()).add(r["dst"])
    degree = {r["id"]: r["c"] for r in con.execute(
        "SELECT n.id, COUNT(e.src) c FROM nodes n JOIN edges e"
        " ON (e.src=n.id OR e.dst=n.id) WHERE n.type=? GROUP BY n.id",
        (PROCESS_TYPE,))}
    rows = []
    for row in con.execute(
            "SELECT * FROM nodes WHERE type=? AND status NOT IN ('superseded','archived')"
            " ORDER BY title", (PROCESS_TYPE,)):
        n = _node_dict(row)
        p = n["props"]
        rows.append({
            "id": n["id"], "title": n["title"], "body": n["body"] or "",
            "realm": n.get("realm") or "",
            "trigger": p.get("trigger", ""), "established": p.get("established", ""),
            "category": p.get("category", ""),
            "domains": sorted(areas.get(n["id"], [])),
            "edges": degree.get(n["id"], 0),
            "updated": n.get("updated", ""),
        })
    return rows


def api_add_process(con, *, title, body="", trigger="", realm="", node_id="",
                    props=None) -> dict:
    """Create (or, given an existing id, refresh) a process in a realm.

    Always part-of `hub_process`, so it is findable as the domain rather than by
    guessing full-text (§2e)."""
    title = (title or "").strip()
    if not title:
        return {"ok": False, "error": "a process needs a title"}
    realm = slugify(realm) if realm else current_realm()
    extra = {k: v for k, v in (props or {}).items() if v not in (None, "")}
    extra.setdefault("category", "process")
    if trigger:
        extra["trigger"] = trigger.strip()
    nid = node_id.strip() if node_id else ""
    if nid and not nid.startswith(PROCESS_TYPE + "_"):
        nid = f"{PROCESS_TYPE}_{slugify(nid)}"
    existing = con.execute("SELECT type FROM nodes WHERE id=?", (nid,)).fetchone() if nid else None
    if existing and existing["type"] != PROCESS_TYPE:
        return {"ok": False, "error": f"{nid} exists and is a {existing['type']}, not a process"}
    if existing:
        res = api_update_node(con, nid, title=title, body=body or "", realm=realm,
                              props_patch=extra)
        res["action"] = "updated"
    else:
        res = api_create_node(con, PROCESS_TYPE, title, body or "", extra, "", "",
                              realm, nid)
        res["action"] = "created"
    if res.get("ok"):
        _ensure_area(con, PROCESS_AREA, DEFAULT_REALM)
        if not con.execute("SELECT 1 FROM edges WHERE src=? AND dst=? AND rel=?",
                           (res["node"]["id"], PROCESS_AREA, AREA_REL)).fetchone():
            api_link(con, res["node"]["id"], PROCESS_AREA, AREA_REL)
    return res


def cmd_add_process(args):
    out(api_add_process(connect(), title=args.title, body=args.body or "",
                        trigger=args.trigger or "", realm=args.realm or "",
                        node_id=args.id or ""))


def api_retype(con, ids, new_type: str, dry_run=False) -> dict:
    """Change the type of existing nodes, keeping id, edges and everything else.

    The id is kept on purpose: edges, `[[links]]` in other bodies and the mirror
    all address a node by id, so a new id would orphan every reference. What the
    node used to be is written into `props.migrated_from` (with the date), which
    makes the change reversible by running it again with the old type."""
    new_type = (new_type or "").strip()
    if not new_type:
        return {"ok": False, "error": "missing type"}
    changed, same, absent = [], [], []
    for nid in dict.fromkeys(str(i).strip() for i in ids if str(i).strip()):
        row = con.execute("SELECT id,type,title,props FROM nodes WHERE id=?",
                          (nid,)).fetchone()
        if not row:
            absent.append(nid)
            continue
        if row["type"] == new_type:
            same.append(nid)
            continue
        changed.append({"id": nid, "from": row["type"], "title": row["title"]})
        if dry_run:
            continue
        props = json.loads(row["props"] or "{}")
        props["migrated_from"] = row["type"]
        props["migrated_at"] = now()
        if new_type == PROCESS_TYPE:
            props.setdefault("category", "process")
        con.execute("UPDATE nodes SET type=?, props=?, updated=? WHERE id=?",
                    (new_type, json.dumps(props), now(), nid))
    con.commit()
    sync = None
    if changed and not dry_run:
        sync = _after_write(con, nodes=[c["id"] for c in changed])
    return {"ok": True, "dry_run": dry_run, "type": new_type,
            "count": len(changed), "changed": changed,
            "already": same, "not_found": absent, "sync": sync}


def cmd_retype(args):
    ids = list(args.id or [])
    if args.ids_file:
        ids += [l.strip() for l in Path(args.ids_file).expanduser()
                .read_text(encoding="utf-8").splitlines()
                if l.strip() and not l.lstrip().startswith("#")]
    out(api_retype(connect(), ids, args.type, args.dry_run))


def process_markdown(con, process_id: str) -> str:
    """One process as a portable .md: frontmatter plus the body, verbatim."""
    row = con.execute("SELECT * FROM nodes WHERE id=? AND type=?",
                      (process_id, PROCESS_TYPE)).fetchone()
    if not row:
        return ""
    n = _node_dict(row)
    p = n["props"]
    fm = [f"id: {n['id']}", f"title: {_fm_value(n['title'])}",
          f"realm: {n.get('realm', '')}"]
    fm += [f"{k}: {_fm_value(p[k])}" for k in PROCESS_FIELDS if p.get(k)]
    # the rest of props travels too, as one JSON line the frontmatter parser keeps
    # whole; usage counters are this brain's history, not the process
    rest = {k: v for k, v in p.items() if k not in PROCESS_FIELDS
            and k not in ("access_count", "last_accessed", "strength", "origin_realm")}
    if rest:
        fm.append("props: " + json.dumps(rest, ensure_ascii=False, sort_keys=True))
    return "---\n" + "\n".join(fm) + "\n---\n\n" + (n["body"] or "").strip() + "\n"


def _import_process_text(con, pid: str, text: str, mode: str, realm: str) -> str:
    """Land one process from a bundle in `realm`. Returns what was done."""
    meta, body = _parse_frontmatter(text)
    pid = meta.get("id") or pid
    if not meta.get("title"):
        return "skipped (no title)"
    row = con.execute("SELECT type FROM nodes WHERE id=?", (pid,)).fetchone()
    if row and row["type"] != PROCESS_TYPE:
        return f"failed: {pid} exists here as a {row['type']}"
    if row and mode == "ignore":
        return "kept"
    if row and mode == "replace":
        con.execute("DELETE FROM nodes WHERE id=?", (pid,))
        con.commit()
    props = {}
    try:
        props = json.loads(meta.get("props") or "{}")
        if not isinstance(props, dict):
            props = {}
    except ValueError:
        props = {}
    for k in PROCESS_FIELDS:
        if meta.get(k):
            props[k] = meta[k]
    if meta.get("realm") and meta["realm"] != realm:
        # where it came from is worth keeping: it says whose way of working it is
        props["origin_realm"] = meta["realm"]
    res = api_add_process(con, title=meta["title"], body=body.strip(),
                          realm=realm, node_id=pid, props=props)
    if not res.get("ok"):
        return f"failed: {res.get('error')}"
    return "replaced" if (row and mode == "replace") else res["action"]


def sync_skill_tree(con, realm="shared", no_node_types=False) -> dict:
    """Project the skill contracts into the graph as two trees (CONVENTIONS §5a).

    Built-in skills hang off `hub_skills_builtin` and declare in their SKILL.md;
    user skills hang off `hub_skills_user` and declare in props. Idempotent, and
    a true sync: edges this run previously wrote that are no longer declared are
    removed, so the graph cannot keep asserting a dependency the corpus dropped.
    Hand-made edges carry no marker and are never touched.

    Callable rather than a command body, because creating a skill has to project
    it immediately — a skill the page cannot draw until someone remembers to run
    a CLI command is a skill the page draws wrong."""
    ts = now()
    realm = realm or "shared"
    want, hubs, types_seen, unresolved = set(), {}, set(), {}
    trees = {"plugin": [], "learned": []}

    rows = con.execute("SELECT * FROM nodes WHERE type='skill' ORDER BY id").fetchall()
    if not rows:
        return {"ok": False, "error": "no skill nodes — run ingest-skills first"}

    for kind, (hub_id, hub_title) in TREE_HUBS.items():
        if not con.execute("SELECT 1 FROM nodes WHERE id=?", (hub_id,)).fetchone():
            api_create_node(con, AREA_TYPE, hub_title, "", {"domain": hub_id[4:]},
                            "", "", realm, hub_id)
        hubs[kind] = hub_id

    known_types = set(NODE_TYPES)
    for row in rows:
        sid = row["id"]
        kind, decl = _skill_declarations(con, row)
        trees[kind if kind in trees else "learned"].append(sid)
        want.add((sid, hubs.get(kind, hubs["learned"]), "part-of"))

        for rel in ("requires", "waits-for", "enriched-by"):
            for token in decl.get(rel, []):
                dep = token if token.startswith("skill_") else f"skill_{slugify(token)}"
                if con.execute("SELECT 1 FROM nodes WHERE id=? AND type='skill'",
                               (dep,)).fetchone() and dep != sid:
                    want.add((sid, dep, rel))
                else:
                    unresolved.setdefault(sid, []).append(f"{rel}:{token}")

        for rel in ("reads", "writes"):
            others = []
            for token in decl.get(rel, []):
                t = token.strip().lstrip("`").rstrip("`")
                if t in known_types and not no_node_types:
                    tid = f"nodetype_{t}"
                    if tid not in types_seen:
                        if not con.execute("SELECT 1 FROM nodes WHERE id=?", (tid,)).fetchone():
                            api_create_node(
                                con, "node-type", t,
                                f"A node type in the memory graph. Its edges show every "
                                f"skill that reads or writes it (CONVENTIONS §5a).",
                                {"node_type": t}, "", "", realm, tid)
                        types_seen.add(tid)
                    want.add((sid, tid, rel))
                else:
                    others.append(t)
            if others:
                props = json.loads(row["props"] or "{}")
                props[f"{rel.replace('-', '_')}_other"] = ", ".join(others)
                con.execute("UPDATE nodes SET props=?, updated=? WHERE id=?",
                            (json.dumps(props), ts, sid))

    have = {(r["src"], r["dst"], r["rel"]) for r in con.execute(
        "SELECT src,dst,rel,props FROM edges").fetchall()
        if json.loads(r["props"] or "{}").get("src") == "skill-tree"}

    added = [e for e in sorted(want) if e not in have]
    for src, dst, rel in added:
        api_link(con, src, dst, rel, dict(TREE_EDGE_PROP))
    pruned = [e for e in sorted(have) if e not in want]
    for src, dst, rel in pruned:
        con.execute("DELETE FROM edges WHERE src=? AND dst=? AND rel=?", (src, dst, rel))
    con.commit()
    sync = _after_write(con, del_edges=pruned) if pruned else None

    return {"ok": True,
            "trees": {"builtin": {"hub": hubs["plugin"], "skills": len(trees["plugin"])},
                      "user": {"hub": hubs["learned"], "skills": len(trees["learned"])}},
            "node_types": len(types_seen), "edges": len(want),
            "added": len(added), "pruned": len(pruned),
            "unresolved": unresolved or None, "sync": sync}


def cmd_sync_skill_tree(args):
    out(sync_skill_tree(connect(), getattr(args, "realm", None) or "shared",
                        getattr(args, "no_node_types", False)))


def cmd_ingest_skills(args):
    """Pull markdown skills into the brain as nodes so processes live in the DB
    (and sync everywhere), not as isolated files. Idempotent: re-run any time to
    refresh. Scans --dir (repeatable); defaults to the plugin skills + the
    runtime learned_skills tree."""
    out(ingest_skills(connect(), args.dir, args.kind, args.realm, args.all_md, args.no_rules))


def ingest_skills(con, dirs=None, kind=None, realm="shared", all_md=False,
                  no_rules=False) -> dict:
    """The work of `ingest-skills`, on an open connection. See cmd_ingest_skills."""
    dirs = [Path(d).expanduser() for d in (dirs or [])]
    if not dirs:
        dirs = [_plugin_skills_dir(), learned_skills_dir()]
    kind_for = {str(_plugin_skills_dir()): "plugin",
                str(learned_skills_dir()): "learned"}
    default_kind = kind
    results, touched, rules = [], [], []
    for d in dirs:
        if not d.exists():
            continue
        kind = kind_for.get(str(d), default_kind or "plugin")
        # Rules first, always: a skill file names the rules it honors, and a rule
        # that is not in the brain yet when the skill lands cannot be linked to.
        if not no_rules:
            for rpath in sorted((d / "rules").glob("*.md")):
                rid, action = _ingest_rule_file(con, rpath)
                rules.append({"id": rid, "action": action, "path": str(rpath)})
                if action == "created":
                    touched.append(rid)
        for path in sorted(d.rglob("SKILL.md")) + (
                sorted(d.rglob("*.md")) if all_md else []):
            if path.name != "SKILL.md" and not all_md:
                continue
            if path.parent.name == "rules":
                continue
            nid, action, missing = _ingest_skill_file(con, path, kind, realm)
            touched.append(nid)
            row = {"id": nid, "action": action, "kind": kind, "path": str(path)}
            if missing:
                row["rules_not_found"] = missing
            results.append(row)
    con.commit()
    sync = _after_write(con, nodes=touched) if touched else None
    tree = sync_skill_tree(con, realm) if touched else None
    return {"ok": True, "count": len(results), "ingested": results,
            "rules": rules, "tree": tree, "sync": sync}


def missing_builtin_skills(con) -> list:
    """Built-in skills (the plugin's skills/*/SKILL.md) with no `skill` node yet."""
    want = []
    for path in sorted(_plugin_skills_dir().glob("*/SKILL.md")):
        try:
            meta, _ = _parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        want.append(f"skill_{slugify(meta.get('name') or path.parent.name)}")
    if not want:
        return []
    have = {r[0] for r in con.execute(
        f"SELECT id FROM nodes WHERE id IN ({','.join('?' * len(want))})", want)}
    return [w for w in want if w not in have]


def ensure_builtin_skills(con) -> dict:
    """Ingest the built-in skills when any is missing from the brain; a no-op
    otherwise. A brain made from scratch has none until this runs: connect()
    calls it on the database it creates, `init` and `doctor` on every run.
    A skill the owner archived is still a node, so it is not brought back.
    SECOND_BRAIN_NO_SKILL_SEED=1 turns it off everywhere (a restore about to
    overwrite the file, a test that counts nodes in an empty brain)."""
    if os.environ.get("SECOND_BRAIN_NO_SKILL_SEED") == "1":
        return {"ok": True, "ingested": 0, "missing": [], "skipped": "SECOND_BRAIN_NO_SKILL_SEED=1"}
    missing = missing_builtin_skills(con)
    if not missing:
        return {"ok": True, "ingested": 0, "missing": []}
    try:
        res = ingest_skills(con)
    except Exception as e:   # a broken SKILL.md must not stop the brain opening
        return {"ok": False, "ingested": 0, "missing": missing, "error": str(e)[:300]}
    return {"ok": True, "ingested": len(missing), "missing": missing_builtin_skills(con),
            "count": res.get("count")}


# ---------------------------------------------------------------------------
# contracts as data: build one, parse one back, check one is complete
#
# §5.1 and §5b.1 describe the two shapes in prose, and until now the only thing
# that could produce them was a person typing the headings. That made every
# non-prose author — the web UI, an export, an import from another machine — a
# second definition of the same shape, free to drift. These functions ARE the
# shape: one renderer, one parser, one validator, and every author goes through
# them.
# ---------------------------------------------------------------------------

# (field, heading, required). `rules_honored` and `depends_on` are rendered from
# the links and declarations rather than typed, so they are never required input.
SKILL_SECTIONS = [
    ("when_to_use",    "## When to use",           True),
    ("given",          "## Given",                 True),
    ("rules_honored",  "## Rules honored",         False),
    ("depends_on",     "## Depends on",            False),
    ("deliverables",   "## Deliverables",          True),
    ("judged_against", "## Judged against",        True),
    ("guardrails",     "## Guardrails - must not", True),
    ("reply",          "## Reply (focus contract)", True),
]

# Only THE RULE and JUDGED AGAINST are required (§5b.1): a rule with no checkable
# outcome is the thing the shape exists to prevent. EXCEPTIONS is always emitted
# even when empty, because an unstated exception gets invented under pressure.
RULE_SECTIONS = [
    ("the_rule",       "THE RULE",      True),
    ("why",            "WHY",           False),
    ("how_to_apply",   "HOW TO APPLY",  False),
    ("exceptions",     "EXCEPTIONS",    False),
    ("judged_against", "JUDGED AGAINST", True),
    ("related",        "RELATED",       False),
]

# label in a `## Depends on` block  <->  the declaration key (§5a)
DECL_LABELS = [("requires", "Requires"), ("waits_for", "Optional (ordered)"),
               ("enriched_by", "Optional (enriching)"),
               ("reads", "Reads"), ("writes", "Writes")]


def decl_tokens(value) -> list:
    """A declaration field from any author: a list, or a comma/newline string."""
    if isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = re.split(r"[,\n]", str(value or ""))
    seen, keep = set(), []
    for t in items:
        t = str(t).strip().strip("`").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            keep.append(t)
    return keep


def render_depends_on(decl: dict) -> str:
    """The `## Depends on` block, in exactly the shape `_parse_depends_on` reads.

    `none` is written out for an empty field rather than the line being dropped:
    §5a says silence is never a declaration, and a round-trip through a file has
    to be able to tell "declared nothing" from "did not say"."""
    lines = []
    for key, label in DECL_LABELS:
        toks = decl_tokens((decl or {}).get(key))
        lines.append(f"- **{label}:** " +
                     (", ".join(f"`{t}`" for t in toks) if toks else "none."))
    return "\n".join(lines)


def render_rules_honored(rules: list, cue: str, extra: str = "") -> str:
    """The `## Rules honored` block: the match-rules call, then the rules this
    skill is accountable for, by id.

    The ids are written into the prose AND carried as `honors` edges. That is
    not duplication for its own sake: the body is what a person reads when the
    skill fires, the edge is what the graph can walk, and a skill whose
    accountability only existed in one of the two would be invisible to the
    other."""
    call = cue or "what you are about to do"
    out_lines = [
        "Ask the genome for the rules that apply before acting (CONVENTIONS §5.2):",
        "", "```",
        f'python3 "$CLAUDE_PLUGIN_ROOT/scripts/graph.py" match-rules "{call}"',
        "```", "",
    ]
    if rules:
        out_lines.append("Accountable for:")
        out_lines += [f"- `{r['id']}`: {r['title']}" for r in rules]
    else:
        out_lines.append("Accountable for: whatever `match-rules` returns for that "
                         "cue. No rule is named here yet.")
    if (extra or "").strip():
        out_lines += ["", extra.strip()]
    return "\n".join(out_lines)


def build_skill_contract(sections: dict, decl=None, rules=None, cue="") -> str:
    """The eight sections of §5.1, in order, as one node body."""
    sections = sections or {}
    parts = []
    for field, heading, _req in SKILL_SECTIONS:
        if field == "depends_on":
            text = render_depends_on(decl or {})
        elif field == "rules_honored":
            text = render_rules_honored(rules or [], cue,
                                        sections.get("rules_honored", ""))
        else:
            text = str(sections.get(field) or "").strip()
        # An empty section is LEFT OUT, never written as a bare heading. The
        # audit asks whether a heading appears in the body, so an empty one
        # would report a contract as complete and hide exactly the gap the
        # audit exists to find.
        if not text:
            continue
        parts.append(f"{heading}\n\n{text}")
    return "\n\n".join(parts) + "\n"


def build_rule_contract(sections: dict) -> str:
    """The rule contract of §5b.1 as one node body. GOVERNS is NOT written: it is
    rendered from props.scenario by whoever displays the rule (§5b.2)."""
    sections = sections or {}
    parts = []
    for field, heading, _req in RULE_SECTIONS:
        text = str(sections.get(field) or "").strip()
        if field == "exceptions" and not text:
            text = "none"
        # Same reason as build_skill_contract: a heading with nothing under it
        # reads as a filled section to every checker, including this project's.
        if not text:
            continue
        parts.append(f"{heading}\n\n{text}")
    return "\n\n".join(parts) + "\n"


def _norm_heading(line: str) -> str:
    """A heading reduced to what identifies it.

    Every dash becomes a hyphen, because the corpus writes
    `## Guardrails — must not` with an em dash and the generated form uses a
    plain one. Comparing them literally silently dropped that whole section on
    the way back in, and a lost guardrail is the one loss §5.3 calls
    unacceptable."""
    s = re.sub(r"[\u2010-\u2015\u2212\-]+", "-", (line or "").strip().lower())
    return re.sub(r"\s+", " ", s).strip(" .:")


def _split_sections(body: str, headings: list, level: str) -> dict:
    """Cut a contract body back into its sections. `level` is '## ' for a skill
    (markdown headings) or '' for a rule (bare uppercase lines)."""
    heads = {}
    for field, heading, _req in headings:
        norm = _norm_heading(heading)
        heads[norm] = field
        # `## Guardrails` on its own is the same section as
        # `## Guardrails - must not`, and the audit already treats it that way.
        heads.setdefault(norm.split(" - ")[0].strip(), field)
    found, cur = {}, None
    for line in (body or "").splitlines():
        stripped = line.strip()
        is_heading = stripped.startswith("##") if level else stripped.isupper()
        hit = heads.get(_norm_heading(line)) if is_heading else None
        if hit:
            cur = hit
            found.setdefault(cur, [])
            continue
        if cur is not None:
            found[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in found.items()}


def parse_skill_contract(body: str) -> dict:
    return _split_sections(body, SKILL_SECTIONS, "## ")


def parse_rule_contract(body: str) -> dict:
    return _split_sections(body, RULE_SECTIONS, "")


def missing_skill_sections(sections: dict) -> list:
    return [h for f, h, req in SKILL_SECTIONS
            if req and not str((sections or {}).get(f) or "").strip()]


def missing_rule_sections(sections: dict) -> list:
    return [h for f, h, req in RULE_SECTIONS
            if req and not str((sections or {}).get(f) or "").strip()]


# ---------------------------------------------------------------------------
# authoring a rule / a skill, from anywhere
#
# The CLI used to be the only author, so `cmd_add_rule` did the parsing, the
# validation and the writing in one function. The web UI needs the same write
# without the argparse namespace around it, so the write lives here and the
# command is a thin caller. One author, one set of invariants.
# ---------------------------------------------------------------------------

def api_add_rule(con, *, title, body="", sections=None, scenario="", source="user",
                 section="", non_negotiable=False, overrides="", realm="",
                 node_id="", require_contract=True):
    """Author or override a convention rule.

    `sections` (the §5b.1 fields) is the structured way in; `body` is the raw
    way, kept for callers that already hold a written contract. Either produces
    the same node, and the contract is checked before the write rather than
    reported afterwards by `audit-contracts`: a rule authored through a form has
    no excuse to arrive incomplete."""
    title = (title or "").strip()
    if not title:
        return {"ok": False, "error": "a rule needs a title"}
    if sections:
        body = build_rule_contract(sections)
    body = (body or "").strip()
    if require_contract:
        gaps = missing_rule_sections(sections or parse_rule_contract(body))
        if gaps:
            return {"ok": False, "error": "incomplete rule contract (§5b.1): "
                                          "missing " + ", ".join(gaps)}
    if source not in RULE_PRECEDENCE:
        return {"ok": False, "error": f"unknown source: {source}"}
    aid = _ensure_area(con, AREA_CONVENTION, realm or "shared")
    props = {"section": section or "user",
             "scenario": (scenario or title).strip(),
             "source": source,
             "precedence": RULE_PRECEDENCE[source],
             "authored_at": now()}
    if non_negotiable:
        props["non_negotiable"] = True
    if overrides:
        props["overrides"] = overrides
    nid = node_id or f"rule_{source}_{slugify(title)[:48]}"
    if con.execute("SELECT 1 FROM nodes WHERE id=?", (nid,)).fetchone():
        return {"ok": False, "error": f"{nid} already exists — edit it, or give "
                                      f"the new rule a different title"}
    res = api_create_node(con, RULE_TYPE, title, body, props,
                          "active", "", realm or "shared", nid)
    if not res.get("ok"):
        return res
    api_link(con, nid, aid, AREA_REL)
    if overrides:
        # Retire the shipped rule rather than deleting it — history stays, and
        # match-rules stops surfacing both sides of a contradiction.
        api_update_node(con, overrides, status="superseded",
                        props_patch={"superseded_by": nid})
        api_link(con, nid, overrides, "follows-up")
    res["area"] = aid
    return res


def api_add_skill(con, *, name, title, description="", triggers="", sections=None,
                  body="", decl=None, rules=(), realm="", node_id="",
                  kind="learned", require_contract=True, sync_tree=True):
    """Author a learned skill: the node, its `honors` edges, and its place in the
    user tree.

    A skill created here is complete the moment it is written — contract checked,
    rules linked, tree edges projected — because a skill that needs a follow-up
    CLI run before the page can show it is a skill the page will show wrong."""
    name = slugify(name or title)
    title = (title or "").strip()
    if not name or not title:
        return {"ok": False, "error": "a skill needs a name and a title"}
    triggers = (triggers or (sections or {}).get("when_to_use", "")).strip()
    rule_rows = []
    for rid in dict.fromkeys(rules or ()):
        row = con.execute("SELECT id,title FROM nodes WHERE id=? AND type=?",
                          (rid, RULE_TYPE)).fetchone()
        if not row:
            return {"ok": False, "error": f"no rule {rid}"}
        rule_rows.append({"id": row["id"], "title": row["title"]})
    if sections:
        if require_contract:
            gaps = missing_skill_sections(sections)
            if gaps:
                return {"ok": False, "error": "incomplete skill contract (§5.1): "
                                              "missing " + ", ".join(gaps)}
        body = build_skill_contract(sections, decl, rule_rows, triggers[:120])
    body = (body or "").strip()
    nid = node_id or f"skill_{name}"
    if con.execute("SELECT 1 FROM nodes WHERE id=?", (nid,)).fetchone():
        return {"ok": False, "error": f"{nid} already exists — edit it, or give "
                                      f"the new skill a different name"}
    props = {"triggers": triggers, "kind": kind, "description": description,
             "usage_count": 0, "confidence": 1.0, "learned_at": now()}
    for key, _label in DECL_LABELS:
        toks = decl_tokens((decl or {}).get(key))
        if toks:
            props[key] = ", ".join(toks)
    res = api_create_node(con, "skill", title, f"WHEN TO USE: {triggers}\n\n{body}",
                          props, "active", "", realm or "shared", nid)
    if not res.get("ok"):
        return res
    for r in rule_rows:
        api_link(con, nid, r["id"], HONORS_REL)
    res["rules"] = [r["id"] for r in rule_rows]
    if sync_tree:
        res["tree"] = sync_skill_tree(con, realm=realm or "shared")
    return res


def honored_rules(con, skill_id: str) -> list:
    """The rules a skill declares itself accountable for, edges first.

    Both halves are read, because the corpus predates the edge: a built-in skill
    names its rules in the `## Rules honored` prose and has no `honors` edge
    until someone adds one, and a skill authored in the UI has the edge from the
    first save. Reading only one of the two would hide half the corpus."""
    seen, out_rows = set(), []
    for r in con.execute(
            "SELECT n.id, n.title, n.props FROM edges e JOIN nodes n ON n.id=e.dst"
            " WHERE e.src=? AND e.rel=? AND n.type=? ORDER BY n.title",
            (skill_id, HONORS_REL, RULE_TYPE)):
        seen.add(r["id"])
        out_rows.append({"id": r["id"], "title": r["title"],
                         "props": json.loads(r["props"] or "{}"), "linked": True})
    row = con.execute("SELECT body FROM nodes WHERE id=?", (skill_id,)).fetchone()
    for rid in dict.fromkeys(re.findall(r"rule_[a-z0-9_-]+", (row["body"] if row else "") or "")):
        if rid in seen:
            continue
        hit = con.execute("SELECT id,title,props FROM nodes WHERE id=? AND type=?",
                          (rid, RULE_TYPE)).fetchone()
        if hit:
            seen.add(rid)
            out_rows.append({"id": hit["id"], "title": hit["title"],
                             "props": json.loads(hit["props"] or "{}"),
                             "linked": False})
    return out_rows


# ---------------------------------------------------------------------------
# markdown <-> graph, so a skill can travel between machines
#
# The DB is the source of truth and the neo4j mirror is how two machines share
# it. Markdown is the third route, and the one that works with no server: export
# a skill folder, commit it or copy it, ingest it on the other side. The export
# is the same contract the node holds, so a round trip changes nothing.
# ---------------------------------------------------------------------------

def _fm_value(v) -> str:
    """One frontmatter line's value. The parser is line-based single values, so
    anything multi-line is folded to spaces rather than silently truncated."""
    return " ".join(str(v or "").split())


def rule_markdown(con, rule_id: str) -> str:
    row = con.execute("SELECT * FROM nodes WHERE id=? AND type=?",
                      (rule_id, RULE_TYPE)).fetchone()
    if not row:
        return ""
    n = _node_dict(row)
    p = n["props"]
    fm = [f"id: {n['id']}", f"title: {_fm_value(n['title'])}",
          f"scenario: {_fm_value(p.get('scenario'))}",
          f"source: {p.get('source', 'user')}",
          f"section: {p.get('section', 'user')}",
          f"realm: {n.get('realm', '')}",
          f"non_negotiable: {'true' if p.get('non_negotiable') else 'false'}"]
    if p.get("overrides"):
        fm.append(f"overrides: {p['overrides']}")
    return "---\n" + "\n".join(fm) + "\n---\n\n" + (n["body"] or "").strip() + "\n"


def skill_markdown(con, skill_id: str) -> tuple:
    """(markdown, [rule ids]) for one skill node, as a portable SKILL.md."""
    row = con.execute("SELECT * FROM nodes WHERE id=? AND type='skill'",
                      (skill_id,)).fetchone()
    if not row:
        return "", []
    n = _node_dict(row)
    p = n["props"]
    _kind, decl = _skill_declarations(con, row)
    rules = honored_rules(con, skill_id)
    name = slugify(p.get("name") or n["id"][len("skill_"):] or n["title"])
    # the body already carries the contract; strip the WHEN TO USE preamble the
    # node stores for full-text matching, since the contract states it properly
    body = re.sub(r"^WHEN TO USE:.*?\n\n", "", n["body"] or "", flags=re.S)
    if "## When to use" not in body:
        body = build_skill_contract({"when_to_use": p.get("triggers", ""),
                                     "deliverables": body}, decl, rules,
                                    (p.get("triggers") or "")[:120])
    fm = [f"name: {name}",
          f"title: {_fm_value(n['title'])}",
          f"description: {_fm_value(p.get('description') or n['title'])}",
          f"kind: {p.get('kind', 'learned')}",
          f"realm: {n.get('realm', '')}",
          f"triggers: {_fm_value(p.get('triggers'))}"]
    if rules:
        fm.append("rules: " + ", ".join(r["id"] for r in rules))
    md = ("---\n" + "\n".join(fm) + "\n---\n\n"
          f"# {n['title']}\n\n" + body.strip() + "\n")
    return md, [r["id"] for r in rules]


def _ingest_rule_file(con, path: Path) -> tuple:
    """Upsert one exported rule file. A rule already in this brain is NEVER
    overwritten by an incoming file: the other machine's copy may be older, and
    silently losing an edit is worse than skipping an import you can redo."""
    meta, body = _parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    rid = meta.get("id") or f"rule_{meta.get('source', 'user')}_{slugify(meta.get('title', ''))[:48]}"
    if not meta.get("title"):
        return rid, "skipped (no title)"
    if con.execute("SELECT 1 FROM nodes WHERE id=?", (rid,)).fetchone():
        return rid, "kept"
    res = api_add_rule(
        con, title=meta["title"], body=body,
        scenario=meta.get("scenario", ""),
        source=meta.get("source", "user") if meta.get("source") in RULE_PRECEDENCE else "user",
        section=meta.get("section", ""),
        non_negotiable=str(meta.get("non_negotiable", "")).lower() == "true",
        realm=meta.get("realm", ""), node_id=rid,
        # an imported rule is reported, not refused: the gap belongs to whoever
        # authored it on the other machine, and dropping it here loses the rule
        require_contract=False)
    return rid, ("created" if res.get("ok") else f"failed: {res.get('error')}")


def cmd_export_skills(args):
    """Write skill nodes back out as markdown, so a brain can be moved by file.

    Rules a skill honors travel with it, into a shared `rules/` folder beside the
    skill folders: a skill whose rules stayed behind arrives on the other machine
    accountable to nothing, which is the failure this whole shape exists to
    prevent."""
    con = connect()
    dest = Path(args.dir).expanduser() if args.dir else learned_skills_dir()
    kinds = None if args.kind == "all" else {args.kind or "learned"}
    written, rule_ids = [], set()
    for row in con.execute("SELECT * FROM nodes WHERE type='skill' ORDER BY id"):
        p = json.loads(row["props"] or "{}")
        if kinds and (p.get("kind") or "learned") not in kinds:
            continue
        md, rules = skill_markdown(con, row["id"])
        name = slugify(row["id"][len("skill_"):] or row["title"])
        path = dest / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(md, encoding="utf-8")
        written.append({"id": row["id"], "path": str(path), "rules": len(rules)})
        rule_ids.update(rules)
    rules_written = []
    if not args.no_rules:
        for rid in sorted(rule_ids):
            md = rule_markdown(con, rid)
            path = dest / "rules" / f"{rid}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(md, encoding="utf-8")
            rules_written.append(str(path))
    out({"ok": True, "dir": str(dest), "skills": len(written),
         "rules": len(rules_written), "exported": written,
         "rule_files": rules_written,
         "hint": "copy this directory to the other machine and run ingest-skills"})


# ---------------------------------------------------------------------------
# bundles: a selection of skills and rules, as one zip you can hand to someone
#
# `export-skills` moves a WHOLE tree to a directory, which is the right shape
# for mirroring two machines and the wrong one for "send me your three review
# skills". A bundle is the selective route: pick the items, get one file.
#
# The zip holds a directory per kind (`skills/`, `rules/`) and an IMPORT PLAN.
# The plan is the reason this is not just a zip of the export directory: the
# order files land in decides whether the dependency tree survives. A skill
# read before the rule it honors links to nothing, and a skill read before the
# skill it requires lands with a dangling `requires`. The plan states the one
# order in which every link can be made, so the importer never has to guess and
# two importers cannot guess differently.
# ---------------------------------------------------------------------------

BUNDLE_FORMAT = "memory-ledger-bundle"
BUNDLE_VERSION = 1
BUNDLE_PLAN = "import-plan.json"
# one directory per kind, named for the kind — the whole layout of a bundle
BUNDLE_DIRS = {"skill": "skills", "rule": "rules", "process": "processes"}


def bundles_dir() -> Path:
    """Where exported bundles land, and where an uploaded one is staged. Inside
    the brain's home rather than a system temp dir: an export is a file the
    person is meant to find again after the browser has forgotten it."""
    return home_dir() / "bundles"


def _bundle_deps(decl: dict) -> dict:
    """The skill ids one skill declares, per relation, normalised to node ids."""
    out_deps = {}
    for rel, key in (("requires", "requires"), ("waits-for", "waits_for"),
                     ("enriched-by", "enriched_by")):
        ids = []
        for token in decl_tokens(decl.get(rel) or decl.get(key)):
            ids.append(token if token.startswith("skill_") else f"skill_{slugify(token)}")
        out_deps[key] = ids
    return out_deps


def _bundle_order(items: dict) -> tuple:
    """Every path in the order it can be imported, and the cycles that forced a
    guess.

    Rules first, unconditionally: a rule depends on nothing and a skill that
    lands before the rule it honors links to nothing. Then the skills, each
    after the skills it declares. A cycle cannot be ordered — it is reported
    rather than hidden, because an import that silently picked one arbitrary
    entry point is an import nobody can reproduce."""
    rules = sorted(p for p, it in items.items() if it["kind"] == "rule")
    # a process depends on nothing either; it sits between the two so a bundle
    # reads rules, then ways of working, then the skills that act on them
    procs = sorted(p for p, it in items.items() if it["kind"] == "process")
    skills = {it["id"]: p for p, it in items.items() if it["kind"] == "skill"}
    order, state, cycles = list(rules) + procs, {}, []

    def visit(sid, trail):
        if state.get(sid) == "done":
            return
        if state.get(sid) == "open":                 # back edge: a real cycle
            cycles.append(trail[trail.index(sid):] + [sid])
            return
        state[sid] = "open"
        for dep in items[skills[sid]]["depends_on"]:
            if dep in skills:
                visit(dep, trail + [sid])
            # a dependency outside the bundle is the exporter's finding, not the
            # importer's — it is already reported as `missing_skills`
        state[sid] = "done"
        order.append(skills[sid])

    for sid in sorted(skills):
        visit(sid, [])
    return order, cycles


def build_bundle(con, skill_ids=(), rule_ids=(), dest: Path = None,
                 process_ids=()) -> dict:
    """Write the selected skills, rules and processes to one zip and return the
    report.

    Rules a selected skill honors travel with it even when they were not
    picked, for the reason §5a gives the directory export: a skill that arrives
    without its rules is accountable to nothing, which is the failure the whole
    shape exists to prevent. A skill it REQUIRES is not pulled in the same way —
    that would quietly export something the person did not choose — but every
    one that did not travel is named in the report, so the gap is visible before
    the zip is sent rather than after it is imported."""
    import zipfile

    picked_skills = [s for s in dict.fromkeys(skill_ids) if s]
    picked_rules = [r for r in dict.fromkeys(rule_ids) if r]
    items, files, missing_rules = {}, {}, []

    in_bundle_rules = set()
    for sid in picked_skills:
        row = con.execute("SELECT * FROM nodes WHERE id=? AND type='skill'",
                          (sid,)).fetchone()
        if not row:
            return {"ok": False, "error": f"no skill {sid}"}
        props = json.loads(row["props"] or "{}")
        md, rules = skill_markdown(con, sid)
        _kind, decl = _skill_declarations(con, row)
        name = slugify(props.get("name") or sid[len("skill_"):] or row["title"])
        path = f"{BUNDLE_DIRS['skill']}/{name}/SKILL.md"
        deps = _bundle_deps(decl)
        files[path] = md
        items[path] = {
            "kind": "skill", "id": sid, "name": name, "title": row["title"],
            "description": props.get("description") or props.get("triggers", ""),
            "skill_kind": props.get("kind", "learned"),
            "picked": True, "rules": rules,
            "depends_on": deps["requires"],
            "waits_for": deps["waits_for"], "enriched_by": deps["enriched_by"]}
        in_bundle_rules.update(rules)

    for rid in dict.fromkeys(list(picked_rules) + sorted(in_bundle_rules)):
        row = con.execute("SELECT * FROM nodes WHERE id=? AND type=?",
                          (rid, RULE_TYPE)).fetchone()
        if not row:
            missing_rules.append(rid)
            continue
        props = json.loads(row["props"] or "{}")
        path = f"{BUNDLE_DIRS['rule']}/{rid}.md"
        files[path] = rule_markdown(con, rid)
        items[path] = {
            "kind": "rule", "id": rid, "title": row["title"],
            "description": props.get("scenario", ""),
            "source": props.get("source", "user"),
            "non_negotiable": bool(props.get("non_negotiable")),
            "picked": rid in picked_rules, "depends_on": []}

    for pid in dict.fromkeys(p for p in process_ids if p):
        row = con.execute("SELECT * FROM nodes WHERE id=? AND type=?",
                          (pid, PROCESS_TYPE)).fetchone()
        if not row:
            return {"ok": False, "error": f"no process {pid}"}
        props = json.loads(row["props"] or "{}")
        path = f"{BUNDLE_DIRS['process']}/{slugify(pid)}.md"
        files[path] = process_markdown(con, pid)
        items[path] = {
            "kind": "process", "id": pid, "title": row["title"],
            "description": props.get("trigger", ""),
            "realm": row["realm"] or "", "picked": True, "depends_on": []}

    if not files:
        return {"ok": False, "error": "nothing selected to export"}

    have = {it["id"] for it in items.values() if it["kind"] == "skill"}
    missing_skills = sorted({d for it in items.values() if it["kind"] == "skill"
                             for d in it["depends_on"] if d not in have})
    order, cycles = _bundle_order(items)

    plan = {"format": BUNDLE_FORMAT, "version": BUNDLE_VERSION,
            "created": now(), "realm": DEFAULT_REALM,
            "counts": {"skills": sum(1 for i in items.values() if i["kind"] == "skill"),
                       "rules": sum(1 for i in items.values() if i["kind"] == "rule"),
                       "processes": sum(1 for i in items.values()
                                        if i["kind"] == "process")},
            # THE point of the file: read these paths in this order and every
            # link the bundle carries can be made as it lands
            "order": order,
            "items": items,
            "missing_skills": missing_skills,
            "missing_rules": sorted(missing_rules),
            "cycles": cycles}

    dest = Path(dest).expanduser() if dest else (
        bundles_dir() / f"skills-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip")
    if dest.suffix != ".zip":
        dest = dest.with_suffix(".zip")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(BUNDLE_PLAN, json.dumps(plan, indent=2, sort_keys=True) + "\n")
        for path in order:
            z.writestr(path, files[path])

    return {"ok": True, "path": str(dest), "name": dest.name,
            "bytes": dest.stat().st_size, "plan": plan,
            "skills": plan["counts"]["skills"], "rules": plan["counts"]["rules"],
            "processes": plan["counts"]["processes"],
            "missing_skills": missing_skills, "missing_rules": sorted(missing_rules),
            "cycles": cycles}


def cmd_export_bundle(args):
    con = connect()
    skills, rules = list(args.skill or []), list(args.rule or [])
    procs = list(args.process or [])
    if args.spec:
        spec = json.loads(Path(args.spec).expanduser().read_text(encoding="utf-8"))
        skills += [str(s) for s in spec.get("skills", [])]
        rules += [str(r) for r in spec.get("rules", [])]
        procs += [str(p) for p in spec.get("processes", [])]
    out(build_bundle(con, skills, rules, Path(args.out).expanduser() if args.out else None,
                     procs))


def _safe_member(name: str) -> bool:
    """Is this a member name a bundle is allowed to contain?

    A zip is an untrusted file and its PLAN is untrusted too — the plan names
    the members to read, so a hand-made bundle can list anything it likes there.
    A name is only accepted when it sits inside one of the bundle's own kind
    directories and walks nowhere: no absolute path, no `..` segment, no drive
    or home prefix. Everything else is dropped before it is read, so no later
    step can be handed a name that points outside the archive."""
    if not name or name.startswith(("/", "\\", "~")) or ":" in name:
        return False
    parts = name.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    return parts[0] in set(BUNDLE_DIRS.values())


def _read_bundle(path: Path) -> tuple:
    """(plan, {path: text}) out of a bundle zip, or (None, error).

    Every member is checked against the plan before it is read. A zip is an
    untrusted file: a member named `../../.ssh/authorized_keys` is a real attack
    on any importer that joins member names onto a directory, and nothing here
    ever touches the filesystem with a name that came out of the archive."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if BUNDLE_PLAN not in names:
                return None, f"not a bundle: no {BUNDLE_PLAN} inside"
            plan = json.loads(z.read(BUNDLE_PLAN).decode("utf-8"))
            if plan.get("format") != BUNDLE_FORMAT:
                return None, f"unknown bundle format: {plan.get('format')!r}"
            texts = {}
            for member in plan.get("order", []):
                if member not in plan.get("items", {}) or member not in names:
                    continue
                if not _safe_member(member):
                    continue
                texts[member] = z.read(member).decode("utf-8", errors="replace")
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return None, f"could not read the bundle: {exc}"
    return (plan, texts), None


def inspect_bundle(con, path: Path) -> dict:
    """What is in a bundle and what it would collide with, changing nothing.

    The importer's dialog is built from this: the person chooses per item, and
    cannot choose sensibly without being told which items this brain already
    holds — an `update` and a `replace` differ only for those."""
    got, err = _read_bundle(path)
    if err:
        return {"ok": False, "error": err}
    plan, texts = got
    rows = []
    for member in plan.get("order", []):
        it = dict(plan.get("items", {}).get(member) or {})
        if not it.get("id"):
            continue
        it["path"] = member
        it["readable"] = member in texts
        row = con.execute("SELECT title, props, realm FROM nodes WHERE id=?",
                          (it["id"],)).fetchone()
        it["exists"] = bool(row)
        if row:
            it["existing_title"] = row["title"]
            it["existing_realm"] = row["realm"] or ""
            props = json.loads(row["props"] or "{}")
            it["existing_kind"] = props.get("kind") or props.get("source", "")
        rows.append(it)
    return {"ok": True, "path": str(path), "plan": plan, "items": rows,
            "counts": plan.get("counts", {}),
            "missing_skills": plan.get("missing_skills", []),
            "cycles": plan.get("cycles", [])}


def cmd_inspect_bundle(args):
    out(inspect_bundle(connect(), Path(args.zip).expanduser()))


def _import_rule_text(con, rid: str, text: str, mode: str) -> str:
    """Land one rule from a bundle. Returns what was done, in one word."""
    meta, body = _parse_frontmatter(text)
    rid = meta.get("id") or rid
    if not meta.get("title"):
        return "skipped (no title)"
    exists = bool(con.execute("SELECT 1 FROM nodes WHERE id=?", (rid,)).fetchone())
    if exists and mode == "ignore":
        return "kept"
    if exists and mode == "replace":
        con.execute("DELETE FROM nodes WHERE id=?", (rid,))
        con.commit()
    res = api_add_rule(
        con, title=meta["title"], body=body,
        scenario=meta.get("scenario", ""),
        source=meta.get("source", "user") if meta.get("source") in RULE_PRECEDENCE else "user",
        section=meta.get("section", ""),
        non_negotiable=str(meta.get("non_negotiable", "")).lower() == "true",
        overrides=meta.get("overrides", ""),
        realm=meta.get("realm", ""), node_id=rid,
        # the gap belongs to whoever authored it on the other machine, and
        # dropping the rule here loses it (§5a)
        require_contract=False)
    if not res.get("ok"):
        return f"failed: {res.get('error')}"
    return "replaced" if (exists and mode == "replace") else (
        "updated" if exists else "created")


def import_bundle(con, path: Path, select=None, default_mode="update",
                  realm="shared", process_realm="") -> dict:
    """Apply a bundle, in the order its plan states.

    Skills and rules are realm-independent contracts and land in `realm`
    (shared). A process belongs to a realm, so it lands in `process_realm` —
    the importer's current realm unless the person picked another — and keeps
    the realm it left as `origin_realm`.

    `select` is {node id: mode}, where mode is update | replace | ignore. An id
    the caller never mentioned is imported when it is NEW and left alone when it
    already exists: a bundle names dependencies the person did not tick, and
    creating those is what makes the tree link, while overwriting something they
    did not ask about is not a decision an importer gets to make."""
    got, err = _read_bundle(path)
    if err:
        return {"ok": False, "error": err}
    plan, texts = got
    select = {str(k): str(v) for k, v in (select or {}).items()}
    applied, touched = [], []

    for member in plan.get("order", []):
        it = plan.get("items", {}).get(member) or {}
        nid, text = it.get("id"), texts.get(member)
        if not nid or text is None:
            continue
        exists = bool(con.execute("SELECT 1 FROM nodes WHERE id=?", (nid,)).fetchone())
        mode = select.get(nid) or (default_mode if nid in select else
                                   ("update" if not exists else "ignore"))
        if mode == "ignore" and exists:
            applied.append({"id": nid, "kind": it.get("kind"), "action": "ignored"})
            continue
        if it.get("kind") == "rule":
            action = _import_rule_text(con, nid, text, mode)
        elif it.get("kind") == "process":
            action = _import_process_text(con, nid, text, mode,
                                          slugify(process_realm) if process_realm
                                          else current_realm())
        else:
            if exists and mode == "replace":
                con.execute("DELETE FROM nodes WHERE id=?", (nid,))
                con.commit()
            # written to the learned tree, then read back by the same parser the
            # directory ingest uses — one reader of a SKILL.md, not two
            # slugified, never used raw: `name` is a value out of the bundle's
            # plan, and a plan that says `../../..` would otherwise choose where
            # this file lands
            name = slugify(it.get("name") or nid[len("skill_"):])
            dest = learned_skills_dir() / name / "SKILL.md"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
            _nid, action, missing = _ingest_skill_file(
                con, dest, it.get("skill_kind") or "learned", realm)
            if exists and mode == "replace":
                action = "replaced"
            if missing:
                applied.append({"id": nid, "kind": "skill", "action": action,
                                "rules_not_found": missing})
                touched.append(nid)
                continue
        applied.append({"id": nid, "kind": it.get("kind"), "action": action})
        if not str(action).startswith(("failed", "skipped", "kept", "ignored")):
            touched.append(nid)

    con.commit()
    sync = _after_write(con, nodes=touched) if touched else None
    tree = sync_skill_tree(con, realm) if touched else None
    return {"ok": True, "count": len(applied), "applied": applied,
            "tree": tree, "sync": sync,
            "missing_skills": plan.get("missing_skills", [])}


def cmd_import_bundle(args):
    select = {}
    if args.spec:
        spec = json.loads(Path(args.spec).expanduser().read_text(encoding="utf-8"))
        select = spec.get("select") or spec
    for pair in (args.select or []):
        nid, _, mode = pair.partition(":")
        select[nid.strip()] = (mode.strip() or "update")
    out(import_bundle(connect(), Path(args.zip).expanduser(), select,
                      args.mode, getattr(args, "realm", None) or "shared",
                      getattr(args, "process_realm", None) or ""))


# ----------------------------------------------------- local scripts (§5c)
# `<home>/scripts` holds one person's automation — the files that are NOT
# agnostic enough to ship in the plugin (see the home README). They are files,
# not nodes: the brain lists them, zips them and lands a zip back, and never
# runs one.
SCRIPTS_FORMAT = "second-brain/scripts"
SCRIPTS_VERSION = 1
SCRIPTS_PLAN = "scripts-plan.json"
SCRIPTS_MEMBER_DIR = "scripts"
# a script is source code; anything this size is a data file that wandered in,
# and every byte of it is read into memory to hash and zip
SCRIPT_MAX_BYTES = 2 * 1024 * 1024
SCRIPT_MODES = ("replace", "keep")
_SCRIPT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}")
_SCRIPT_LANG = {".py": "python", ".sh": "shell", ".bash": "shell", ".zsh": "shell",
                ".js": "javascript", ".mjs": "javascript", ".ts": "typescript",
                ".rb": "ruby", ".pl": "perl", ".sql": "sql", ".applescript": "applescript",
                ".ps1": "powershell", ".bat": "batch", ".cmd": "batch"}


def scripts_dir() -> Path:
    """Where the person's local scripts live: `<home>/scripts`, flat."""
    return home_dir() / "scripts"


def script_name_ok(name: str) -> bool:
    """A bare filename the scripts directory may hold.

    The name reaches a filesystem join from a query string or a zip member, so
    it is matched against a strict pattern — no separator, no leading dot, no
    `..` — rather than cleaned up and trusted."""
    return bool(name and _SCRIPT_NAME.fullmatch(name) and ".." not in name)


def _script_summary(text: str) -> str:
    """The first line a person wrote to say what the file is: a docstring or
    a leading comment, past the shebang and the encoding line."""
    lines = text.splitlines()[:40]
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line or line.startswith("#!") or (line.startswith("#") and "coding" in line[:25]):
            continue
        if line.startswith(("from __future__", "set -", "'use strict'", '"use strict"')):
            continue
        for q in ('"""', "'''"):
            if line.startswith(q):
                rest = line[3:].strip().rstrip(q).strip()
                if rest:
                    return rest[:200]
                nxt = next((l.strip() for l in lines[i + 1:] if l.strip()), "")
                return nxt.rstrip(q).strip()[:200]
        if line.startswith(("#", "//", "--")):
            return line.lstrip("#/- ").strip()[:200]
        return ""
    return ""


def _script_lang(name: str, text: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in _SCRIPT_LANG:
        return _SCRIPT_LANG[suffix]
    first = text.split("\n", 1)[0]
    if first.startswith("#!"):
        for word, lang in (("python", "python"), ("bash", "shell"), ("zsh", "shell"),
                           ("/sh", "shell"), ("node", "javascript"), ("ruby", "ruby"),
                           ("pwsh", "powershell")):
            if word in first:
                return lang
    return "text"


def _script_row(name: str, blob: bytes, executable: bool) -> dict:
    text = blob.decode("utf-8", errors="replace")
    return {"name": name, "bytes": len(blob), "executable": executable,
            "sha256": hashlib.sha256(blob).hexdigest(),
            "language": _script_lang(name, text), "summary": _script_summary(text)}


def list_scripts() -> dict:
    """Every script in `<home>/scripts`. Subdirectories and dotfiles are not
    scripts (a `__pycache__`, an editor's swap file) and are left out."""
    d = scripts_dir()
    rows, skipped = [], []
    if d.is_dir():
        for p in sorted(d.iterdir(), key=lambda x: x.name.lower()):
            if not p.is_file() or p.is_symlink() or not script_name_ok(p.name):
                continue
            st = p.stat()
            if st.st_size > SCRIPT_MAX_BYTES:
                skipped.append({"name": p.name, "why": "larger than "
                                f"{SCRIPT_MAX_BYTES // (1024 * 1024)}MB"})
                continue
            row = _script_row(p.name, p.read_bytes(), bool(st.st_mode & 0o111))
            row["modified"] = datetime.fromtimestamp(
                st.st_mtime, timezone.utc).isoformat(timespec="seconds")
            rows.append(row)
    return {"ok": True, "dir": str(d), "exists": d.is_dir(), "count": len(rows),
            "scripts": rows, "skipped": skipped}


def read_script(name: str) -> dict:
    """One script's text, for reading in the page. Never executed."""
    if not script_name_ok(name):
        return {"ok": False, "error": "no such script"}
    p = scripts_dir() / name
    if not p.is_file() or p.is_symlink():
        return {"ok": False, "error": "no such script"}
    if p.stat().st_size > SCRIPT_MAX_BYTES:
        return {"ok": False, "error": "that script is too large to show"}
    blob = p.read_bytes()
    row = _script_row(name, blob, bool(p.stat().st_mode & 0o111))
    row["text"] = blob.decode("utf-8", errors="replace")
    return {"ok": True, "script": row}


def _write_scripts_zip(dest: Path, files: dict) -> dict:
    """`files` is {name: (bytes, executable)}. Returns the plan written."""
    import zipfile
    plan = {"format": SCRIPTS_FORMAT, "version": SCRIPTS_VERSION, "created": now(),
            "items": {n: {k: v for k, v in _script_row(n, b, x).items() if k != "name"}
                      for n, (b, x) in files.items()}}
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(SCRIPTS_PLAN, json.dumps(plan, indent=2, sort_keys=True) + "\n")
        for n, (blob, executable) in files.items():
            info = zipfile.ZipInfo(f"{SCRIPTS_MEMBER_DIR}/{n}",
                                   date_time=datetime.now().timetuple()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            # the unix mode rides in the high bits, so an unzip -X keeps it too
            info.external_attr = ((0o100755 if executable else 0o100644) & 0xFFFF) << 16
            z.writestr(info, blob)
    return plan


def export_scripts(names=(), dest: Path = None) -> dict:
    """Zip the named scripts (all of them when none are named) into
    `<home>/bundles/scripts-<stamp>.zip`, keeping each one's executable bit."""
    have = {r["name"]: r for r in list_scripts()["scripts"]}
    picked = [n for n in dict.fromkeys(names) if n] or sorted(have)
    missing = [n for n in picked if n not in have]
    if missing:
        return {"ok": False, "error": f"no such script: {', '.join(missing)}"}
    if not picked:
        return {"ok": False, "error": f"there are no scripts in {scripts_dir()}"}
    files = {n: ((scripts_dir() / n).read_bytes(), have[n]["executable"]) for n in picked}
    dest = Path(dest).expanduser() if dest else (
        bundles_dir() / f"scripts-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip")
    if dest.suffix != ".zip":
        dest = dest.with_suffix(".zip")
    _write_scripts_zip(dest, files)
    return {"ok": True, "path": str(dest), "name": dest.name,
            "bytes": dest.stat().st_size, "count": len(files), "scripts": sorted(files)}


def wrap_script(blob: bytes, name: str, dest: Path) -> dict:
    """Stage ONE loose file as a one-script zip, so a single file and an
    exported zip go through the same inspect and import."""
    name = Path(name or "").name
    if not script_name_ok(name):
        return {"ok": False, "error": "that filename cannot be a script name "
                "(letters, digits, dot, dash and underscore; no leading dot)"}
    if len(blob) > SCRIPT_MAX_BYTES:
        return {"ok": False, "error": "that file is larger than "
                f"{SCRIPT_MAX_BYTES // (1024 * 1024)}MB"}
    text = blob[:200].decode("utf-8", errors="replace")
    _write_scripts_zip(dest, {name: (blob, text.startswith("#!"))})
    return {"ok": True}


def _read_scripts_zip(path: Path) -> tuple:
    """({name: (bytes, executable)}, None) out of a scripts zip, or (None, error).

    A zip is untrusted: only members named `scripts/<safe name>` are read, and
    the name that reaches the filesystem is the pattern-checked bare filename,
    never the member path."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if SCRIPTS_PLAN not in names:
                if BUNDLE_PLAN in names:
                    return None, ("that is a skills bundle, not scripts — import it "
                                  "from Bundles")
                return None, f"not a scripts export: no {SCRIPTS_PLAN} inside"
            plan = json.loads(z.read(SCRIPTS_PLAN).decode("utf-8"))
            if plan.get("format") != SCRIPTS_FORMAT:
                return None, f"unknown scripts format: {plan.get('format')!r}"
            files = {}
            for info in z.infolist():
                head, _, name = info.filename.partition("/")
                if head != SCRIPTS_MEMBER_DIR or not script_name_ok(name):
                    continue
                if info.file_size > SCRIPT_MAX_BYTES:
                    continue
                mode = (info.external_attr >> 16) & 0o777
                meta = (plan.get("items") or {}).get(name) or {}
                executable = bool(mode & 0o111) if mode else bool(meta.get("executable"))
                files[name] = (z.read(info), executable)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return None, f"could not read the zip: {exc}"
    if not files:
        return None, "that zip carries no scripts"
    return files, None


def inspect_scripts(path: Path) -> dict:
    """What a scripts zip holds and which of it collides with a local file,
    changing nothing. A collision whose bytes match is `identical`, and needs
    no choice."""
    files, err = _read_scripts_zip(path)
    if err:
        return {"ok": False, "error": err}
    rows = []
    for name in sorted(files, key=str.lower):
        blob, executable = files[name]
        row = _script_row(name, blob, executable)
        local = scripts_dir() / name
        row["exists"] = local.is_file()
        if row["exists"]:
            mine = local.read_bytes()
            row["identical"] = hashlib.sha256(mine).hexdigest() == row["sha256"]
            row["existing_bytes"] = len(mine)
        rows.append(row)
    return {"ok": True, "path": str(path), "dir": str(scripts_dir()),
            "count": len(rows), "items": rows}


def import_scripts(path: Path, select: dict) -> dict:
    """Land the selected scripts in `<home>/scripts`.

    `select` maps a name to `replace` or `keep`. A name not in `select` is not
    imported. A new file is written for either mode; an existing one is
    overwritten only for `replace`, and its old bytes are copied to
    `<home>/backups/scripts-<stamp>/` first — a script is code someone wrote,
    and an import is not allowed to be the only copy of what it replaced."""
    files, err = _read_scripts_zip(path)
    if err:
        return {"ok": False, "error": err}
    select = {k: v for k, v in (select or {}).items() if v in SCRIPT_MODES}
    if not select:
        return {"ok": False, "error": "nothing was selected to import"}
    d = scripts_dir()
    d.mkdir(parents=True, exist_ok=True)
    backup = backups_dir() / f"scripts-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    applied, absent = [], []
    for name, mode in select.items():
        if name not in files:
            absent.append(name)
            continue
        blob, executable = files[name]
        target = d / name
        if target.is_symlink():
            applied.append({"name": name, "action": "kept",
                            "why": "the local file is a symlink"})
            continue
        exists = target.is_file()
        if exists and target.read_bytes() == blob:
            applied.append({"name": name, "action": "unchanged"})
            continue
        if exists and mode != "replace":
            applied.append({"name": name, "action": "kept"})
            continue
        if exists:
            backup.mkdir(parents=True, exist_ok=True)
            (backup / name).write_bytes(target.read_bytes())
        tmp = d / f".{name}.{secrets.token_hex(4)}.tmp"
        tmp.write_bytes(blob)
        tmp.chmod(0o755 if executable else 0o644)
        os.replace(tmp, target)
        applied.append({"name": name, "action": "replaced" if exists else "created"})
    res = {"ok": True, "dir": str(d), "applied": applied, "not_in_zip": absent}
    if backup.is_dir():
        res["backup"] = str(backup)
    return res


def write_script(name: str, text: str, executable: bool = True, replace: bool = False) -> dict:
    """Write one script into `<home>/scripts`: what a trigger's "new script"
    does, so the person need not leave the page to make the file a trigger
    runs. An existing file is replaced only when asked, after its old bytes go
    to `<home>/backups/scripts-<stamp>/`, like an import."""
    if not script_name_ok(name or ""):
        return {"ok": False, "error": "a script name is a bare filename: letters, digits, "
                                      ". _ -, no leading dot"}
    blob = (text or "").encode("utf-8")
    if len(blob) > SCRIPT_MAX_BYTES:
        return {"ok": False, "error": f"larger than {SCRIPT_MAX_BYTES // (1024 * 1024)}MB"}
    d = scripts_dir()
    d.mkdir(parents=True, exist_ok=True)
    target = d / name
    if target.is_symlink():
        return {"ok": False, "error": f"{name} is a symlink; change it on disk"}
    exists = target.is_file()
    if exists and not replace:
        return {"ok": False, "exists": True, "error": f"{name} already exists"}
    res = {"ok": True, "dir": str(d), "name": name, "action": "replaced" if exists else "created"}
    if exists:
        backup = backups_dir() / f"scripts-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        backup.mkdir(parents=True, exist_ok=True)
        (backup / name).write_bytes(target.read_bytes())
        res["backup"] = str(backup)
    tmp = d / f".{name}.{secrets.token_hex(4)}.tmp"
    tmp.write_bytes(blob)
    tmp.chmod(0o755 if executable else 0o644)
    os.replace(tmp, target)
    res["script"] = _script_row(name, blob, executable)
    return res


def cmd_scripts(args):
    out(list_scripts())


def cmd_create_script(args):
    text = Path(args.file).expanduser().read_text(encoding="utf-8") if args.file \
        else sys.stdin.read()
    out(write_script(args.name, text, executable=not args.no_exec, replace=args.replace))


def cmd_export_scripts(args):
    out(export_scripts(list(args.name or []),
                       Path(args.out).expanduser() if args.out else None))


def cmd_inspect_scripts(args):
    out(inspect_scripts(Path(args.zip).expanduser()))


def cmd_import_scripts(args):
    select = {}
    if args.spec:
        spec = json.loads(Path(args.spec).expanduser().read_text(encoding="utf-8"))
        select = spec.get("select") or spec
    if args.all:
        files, err = _read_scripts_zip(Path(args.zip).expanduser())
        if err:
            out({"ok": False, "error": err})
            return
        select = {n: args.mode for n in files} | select
    for pair in (args.select or []):
        name, _, mode = pair.partition(":")
        select[name.strip()] = mode.strip() or args.mode
    out(import_scripts(Path(args.zip).expanduser(), select))


def delete_contracts(con, ids=(), force=False) -> dict:
    """Delete skill and rule nodes outright.

    §2a's "archive, never delete" is about WORK — a finished task is history
    worth keeping. A contract is not history: a skill nobody should fire again
    has to actually stop firing, and a rule left in the corpus keeps binding
    every request. So this deletes, and the edges go with it (ON DELETE CASCADE).

    A built-in is refused without `force`, because deleting one only empties the
    DB row: the file it was ingested from is still on disk and the next
    `ingest-skills` brings it straight back. That is a delete that looks like it
    worked and did not, which is worse than a refusal."""
    deleted, refused, absent = [], [], []
    for nid in dict.fromkeys(str(i) for i in ids if i):
        row = con.execute("SELECT id,type,title,props FROM nodes WHERE id=?",
                          (nid,)).fetchone()
        if not row or row["type"] not in ("skill", RULE_TYPE, PROCESS_TYPE):
            absent.append(nid)
            continue
        props = json.loads(row["props"] or "{}")
        builtin = (props.get("kind") == "plugin" if row["type"] == "skill"
                   else props.get("source") == "plugin" if row["type"] == RULE_TYPE
                   else False)
        if builtin and not force:
            refused.append({"id": nid, "title": row["title"],
                            "why": "built-in: it ships as a file and would come "
                                   "back on the next ingest-skills"})
            continue
        con.execute("DELETE FROM nodes WHERE id=?", (nid,))
        deleted.append({"id": nid, "type": row["type"], "title": row["title"],
                        "builtin": builtin})
    con.commit()
    sync = _after_write(con, del_nodes=[d["id"] for d in deleted]) if deleted else None
    tree = sync_skill_tree(con, "shared") if deleted else None
    return {"ok": not refused or bool(deleted), "deleted": deleted,
            "count": len(deleted), "refused": refused, "not_found": absent,
            "tree": tree, "sync": sync}


def cmd_del_contracts(args):
    ids = list(args.id or [])
    if args.spec:
        spec = json.loads(Path(args.spec).expanduser().read_text(encoding="utf-8"))
        ids += [str(x) for x in (spec.get("ids") or
                                 spec.get("skills", []) + spec.get("rules", []))]
    out(delete_contracts(connect(), ids, args.force))


def _plugin_root() -> Path:
    """The plugin checkout that ships the base layer (engine lives in ./scripts)."""
    return Path(__file__).resolve().parent.parent


def _conventions_path() -> Path:
    return _plugin_root() / "CONVENTIONS.md"


_SECTION_RE = re.compile(r"^##\s+(?P<num>[0-9]+[a-z]?)\.\s+(?P<title>.+?)\s*$", re.M)


def _parse_conventions_sections(text: str) -> list:
    """Split CONVENTIONS.md into its `## N. Title` sections.

    Section granularity is deliberate. The doc is already organised by concern, so
    one node per section is lossless and honest, and it is the unit that pays off at
    read time: a request loads the 1-3 sections that apply instead of 400+ lines.
    Finer rules are then added by hand with `add-rule` — that is the mutable part.
    """
    out_sections = []
    matches = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end].strip()
        # Trailing horizontal rules are separators, not content.
        body = re.sub(r"\n-{3,}\s*$", "", body).strip()
        if not body:
            continue
        out_sections.append({"section": m.group("num"),
                             "title": m.group("title").strip(),
                             "body": body})
    return out_sections


def _scenario_terms(title: str, body: str) -> str:
    """Cue words for scenario matching: the heading, plus the bolded lead-ins the
    doc uses to name each rule. Cheap, and refinable by the user afterwards."""
    bolds = re.findall(r"\*\*(.+?)\*\*", body)
    cues = [title] + [b.strip(" .:—-") for b in bolds[:14]]
    seen, keep = set(), []
    for c in cues:
        c = re.sub(r"[`*]", "", c).strip()
        if 2 < len(c) < 90 and c.lower() not in seen:
            seen.add(c.lower()); keep.append(c)
    return " · ".join(keep)


def cmd_ingest_conventions(args):
    """Load CONVENTIONS.md into the brain as `rule` nodes, one per section.

    Idempotent and NON-DESTRUCTIVE: a rule the user has authored or edited is never
    overwritten by the shipped text — it is reported as `kept`. That is what lets
    the plugin ship a base layer while the user redefines the brain on top of it.
    """
    con = connect()
    path = Path(args.file).expanduser() if args.file else _conventions_path()
    if not path.exists():
        out({"ok": False, "error": f"no conventions file at {path}"}); return
    realm = getattr(args, "realm", None) or "shared"
    aid = _ensure_area(con, AREA_CONVENTION, realm)
    sections = _parse_conventions_sections(path.read_text(encoding="utf-8", errors="replace"))
    ts = now()
    results, touched = [], []
    for sec in sections:
        nid = f"rule_s{sec['section']}_{slugify(sec['title'])[:48]}"
        existing = con.execute("SELECT * FROM nodes WHERE id=?", (nid,)).fetchone()
        scenario = _scenario_terms(sec["title"], sec["body"])
        # Only the UNIVERSALLY applicable sections are forced into every answer.
        # The doc labels three sections "(non-negotiable)", but §4 (keep docs in
        # sync) is conditional — it binds when you change behaviour, not on every
        # request — so title-matching alone over-forces. Curate explicitly instead.
        forced = sec["section"] in ALWAYS_APPLY_SECTIONS
        if existing:
            cur = _node_dict(existing)
            src = cur["props"].get("source", "plugin")
            if src != "plugin" or cur["props"].get("user_edited"):
                results.append({"id": nid, "action": "kept", "source": src})
                continue
            props = {**cur["props"], "section": sec["section"], "scenario": scenario,
                     "source": "plugin", "precedence": RULE_PRECEDENCE["plugin"],
                     "ingested_at": ts, "source_path": str(path)}
            if forced:
                props["non_negotiable"] = True
            con.execute("UPDATE nodes SET title=?, body=?, props=?, updated=? WHERE id=?",
                        (sec["title"], sec["body"], json.dumps(props), ts, nid))
            results.append({"id": nid, "action": "updated", "source": "plugin"})
        else:
            props = {"section": sec["section"], "scenario": scenario,
                     "source": "plugin", "precedence": RULE_PRECEDENCE["plugin"],
                     "ingested_at": ts, "source_path": str(path)}
            if forced:
                props["non_negotiable"] = True
            con.execute(
                "INSERT INTO nodes(id,type,title,body,props,status,due,realm,created,updated)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (nid, RULE_TYPE, sec["title"], sec["body"], json.dumps(props),
                 "active", "", realm, ts, ts))
            results.append({"id": nid, "action": "created", "source": "plugin"})
        touched.append(nid)
        con.execute("INSERT OR IGNORE INTO edges(src,dst,rel,props,created)"
                    " VALUES(?,?,?,?,?)", (nid, aid, AREA_REL, "{}", ts))
    con.commit()
    sync = _after_write(con, nodes=touched) if touched else None
    out({"ok": True, "count": len(results), "file": str(path),
         "area": aid, "rules": results, "sync": sync})


def cmd_add_rule(args):
    """Author or override a convention rule. This is the neuroplastic half: a
    user rule outranks the plugin default it names via --overrides.

    The write itself is `api_add_rule`, shared with the web UI, so a rule typed
    into a form and a rule typed into a shell are the same node."""
    sections = {f: getattr(args, f, "") for f, _h, _r in RULE_SECTIONS}
    out(api_add_rule(
        connect(), title=args.title, body=args.body or "",
        sections=sections if any(sections.values()) else None,
        scenario=args.scenario or "", source=args.source or "user",
        section=args.section or "", non_negotiable=bool(args.non_negotiable),
        overrides=args.overrides or "", realm=getattr(args, "realm", None) or "shared",
        node_id=args.id or "",
        # a --body handed in whole is the author's business; only the structured
        # form is held to §5b.1 here, since that is the path that can fill it in
        require_contract=bool(getattr(args, "strict", False))))


def rules_for(con, query: str, limit: int = 8) -> list:
    """The rules that apply to a situation, in the order they should be read.

    Pure FTS + SQL, no model call. Extracted from cmd_match_rules so anything that
    needs to know which rules govern an action — the CLI, the web UI's skill
    view — asks the same function and cannot disagree with it."""
    q = (query or "").strip()
    hits = []
    if q:
        words = [t for t in re.findall(r"\w+", q.lower()) if len(t) > 2]
        terms = " OR ".join(f'"{t}"*' for t in words)
        if terms:
            hits = con.execute(
                "SELECT n.* FROM nodes_fts f JOIN nodes n ON n.rowid=f.rowid"
                " WHERE nodes_fts MATCH ? AND n.type=? AND n.status!='superseded'"
                " ORDER BY rank", (terms, RULE_TYPE)).fetchall()
    matched = [_node_dict(r) for r in hits]  # already in FTS relevance order
    # Precedence is a BOUNDED BOOSTER on relevance, never a re-sort: a user rule
    # edges ahead of an equally-relevant plugin default, but a barely-relevant user
    # rule must not outrank a rule that actually matches. (Same shape as
    # match-skills' reinforcement booster.)
    for i, r in enumerate(matched):
        prec = int(r["props"].get("precedence", 0) or 0)
        r["_score"] = i - (1.5 if prec >= RULE_PRECEDENCE["user"] else
                           0.75 if prec >= RULE_PRECEDENCE["learned"] else 0)
    matched.sort(key=lambda r: r["_score"])
    for r in matched:
        r.pop("_score", None)
    matched = matched[:limit]
    # Universally-applicable rules are APPENDED, never counted against the limit —
    # otherwise a handful of them fills every slot and the rule you actually asked
    # for never appears (which is exactly what happened before this).
    seen = {r["id"] for r in matched}
    forced = [_node_dict(r) for r in con.execute(
        "SELECT * FROM nodes WHERE type=? AND status!='superseded'"
        " AND json_extract(props,'$.non_negotiable')=1"
        " ORDER BY json_extract(props,'$.section')", (RULE_TYPE,)).fetchall()
        if r["id"] not in seen]
    return matched + forced


def cmd_match_rules(args):
    """Which conventions apply to THIS situation — the token-light replacement for
    reading all of CONVENTIONS.md. Non-negotiable rules are always included, then
    relevance order, then the payload is trimmed to a character budget so this can
    run on every request."""
    con = connect()
    results = rules_for(con, args.query, args.limit)
    if getattr(args, "full", False):
        out({"ok": True, "count": len(results), "results": results}); return
    budget = int(args.budget or 2600)
    slim, used = [], 0
    for r in results:
        p = r["props"]
        room = max(240, (budget - used) // max(1, len(results) - len(slim)))
        entry = {"id": r["id"], "section": p.get("section", ""),
                 "title": r["title"], "source": p.get("source", ""),
                 "rule": r["body"][:room]}
        if p.get("non_negotiable"):
            entry["non_negotiable"] = True
        if len(r["body"]) > room:
            entry["truncated"] = True
        used += len(entry["rule"])
        slim.append(entry)
        if used >= budget:
            break
    out({"ok": True, "count": len(slim), "budget": budget, "chars": used,
         "hint": "read a full rule with get-node --id", "results": slim})


# ---------------------------------------------------------------------------
# learned rules — a detector that PROPOSES, never applies
#
# The `learned` tier (precedence 50) existed with nothing to fill it. This fills
# it, under three constraints the rest of the brain already lives by:
#
#   token-free   the pass is regex + token sets over `capture` nodes the hooks
#                already wrote. No model, so it can run unattended.
#   opt-in       `learned_rules` is OFF until the person turns it on. Off, the
#                detector does nothing and nothing runs automatically.
#   proposes     a finding is a `rule-proposal` node, NOT a `rule`, so match-rules
#                cannot return it (same trick as `rule-snapshot`, §5b.3). Only
#                `accept-rule` turns one into a rule. Nothing is deleted: a
#                rejected proposal stays, and is what stops its cluster coming
#                back.
# ---------------------------------------------------------------------------
PROPOSAL_TYPE = "rule-proposal"
LEARNED_RULES_SETTING = "learned_rules"
LR_MIN_COUNT = 2            # times a correction must recur ...
LR_MIN_SESSIONS = 2         # ... across at least this many distinct sessions
LR_WINDOW_DAYS = 30         # how far back the pass reads
LR_MAX_PROMPT = 400         # a correction is short; a long prompt is a task
LR_MIN_TOKENS = 3           # fewer content words than this is too vague to cluster
LR_SIMILARITY = 0.6         # Jaccard over content tokens
LR_MAX_EVIDENCE = 8
LR_OFF_HINT = ("learned rules are OFF. Turn them on with "
               "`graph.py learned-rules on` (or the Settings page); "
               "set it back with `learned-rules off`.")

# A correction opens a clause with one of these. Matched on the accent-stripped,
# lower-cased text, at the START of a sentence/clause only: "no" inside a
# sentence is almost never a correction, and precision beats recall here.
_LR_NEG = (r"no\b(?=[\s,.!:;-])|nope\b|don'?t\b|do not\b|dont\b|never\b|stop\b|"
           r"please don'?t\b|not like (?:that|this)\b|nao\b|nunca\b|pare de\b|"
           r"para de\b|jamais\b")
_LR_POS = r"always\b|sempre\b"
_LR_ANY = (r"i (?:said|told you|asked)\b|instead\b|rather than\b|"
           r"eu (?:disse|falei|pedi)\b|em vez\b|ao inves\b")
_LR_CUE = re.compile(r"^(?:(?P<neg>%s)|(?P<pos>%s)|(?P<any>%s))" % (_LR_NEG, _LR_POS, _LR_ANY))
_LR_LEAD = re.compile(r"^(?:ok|okay|hey|hi|oi|ola|well|so|but|mas|and|e|entao|please|por favor)\b[\s,]*")
_LR_SPLIT = re.compile(r"(?<=[.!?;\n])\s+|\n+")
_LR_STOP = frozenset((
    "the a an and or to of in on for with that this it is are be as at by from "
    "you your me my i we do does did not no dont never always stop instead said "
    "just please again should would could can will when then than so if but "
    "use using used make made let get got have has had was were been its "
    "um uma uns umas o os as de do da dos das em no na nos nas para por com que "
    "se eu voce vc meu minha nao nunca sempre pare isso isto esse essa ele ela "
    "mas ou e ja so mais muito faz fazer faca usa usar").split())


def _lr_fold(text: str) -> str:
    """Lower-case and strip accents, so `não` and `nao` are one cue."""
    t = unicodedata.normalize("NFKD", (text or "").lower().replace("’", "'"))
    return "".join(c for c in t if not unicodedata.combining(c))


def learned_rules_enabled() -> bool:
    return flag(LEARNED_RULES_SETTING)


def correction_of(prompt: str):
    """The correction a prompt makes, or None.

    Returns `{"clause", "tokens", "polarity"}`. `tokens` is a frozenset of
    content words plus a polarity marker, so `never use X` and `always use X`
    do not cluster. Conservative by construction: a prompt that is long, a
    question, or holds a code fence is not a correction; the cue must open a
    clause; and a clause with too few content words is too vague to be a rule."""
    text = (prompt or "").strip()
    if not text or len(text) > LR_MAX_PROMPT or "```" in text:
        return None
    for clause in _LR_SPLIT.split(text):
        clause = clause.strip()
        if not clause or clause.endswith("?"):
            continue
        folded = _LR_LEAD.sub("", _lr_fold(clause))
        m = _LR_CUE.match(folded)
        if not m:
            continue
        polarity = "neg" if m.group("neg") else "pos" if m.group("pos") else "any"
        words = re.findall(r"[a-z0-9_]{3,}", folded[m.end():])
        toks = {w for w in words if w not in _LR_STOP}
        if len(toks) < LR_MIN_TOKENS:
            continue
        if polarity != "any":
            toks.add("__" + polarity)
        return {"clause": clause[:240], "tokens": frozenset(toks), "polarity": polarity}
    return None


def _lr_jaccard(a, b) -> float:
    a, b = set(a), set(b)
    # opposite polarity is never the same rule, however many words they share
    if "__neg" in a and "__pos" in b or "__pos" in a and "__neg" in b:
        return 0.0
    return len(a & b) / len(a | b) if a and b else 0.0


def detect_corrections(con, *, days: int = LR_WINDOW_DAYS, min_count: int = LR_MIN_COUNT,
                       min_sessions: int = LR_MIN_SESSIONS) -> dict:
    """Cluster the corrections in the recent user prompts. READ-ONLY.

    Greedy single pass in creation order (deterministic): a correction joins the
    first cluster whose founding member it resembles (Jaccard >= LR_SIMILARITY),
    else founds one. A cluster qualifies at `min_count` occurrences across
    `min_sessions` sessions."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
    rows = con.execute(
        "SELECT id,body,props,created FROM nodes WHERE type='capture' AND created>=?"
        " AND json_extract(props,'$.role')='user' ORDER BY created,id", (cutoff,)).fetchall()
    clusters, scanned = [], 0
    for r in rows:
        scanned += 1
        c = correction_of(r["body"])
        if not c:
            continue
        props = json.loads(r["props"] or "{}")
        item = {"id": r["id"], "session": props.get("session_id") or "",
                "clause": c["clause"], "tokens": c["tokens"], "created": r["created"]}
        for cl in clusters:
            if _lr_jaccard(cl["tokens"], c["tokens"]) >= LR_SIMILARITY:
                cl["items"].append(item)
                break
        else:
            clusters.append({"tokens": c["tokens"], "items": [item]})
    found = []
    for cl in clusters:
        items = cl["items"]
        sessions = sorted({i["session"] for i in items if i["session"]})
        if len(items) < min_count or len(sessions) < min_sessions:
            continue
        best = min(items, key=lambda i: (len(i["clause"]), i["created"]))
        found.append({"tokens": sorted(cl["tokens"]), "text": best["clause"],
                      "count": len(items), "sessions": sessions, "items": items})
    found.sort(key=lambda f: (-f["count"], f["text"]))
    return {"scanned": scanned, "days": int(days), "clusters": found}


def _lr_proposals(con, status: str = "") -> list:
    sql, params = "SELECT * FROM nodes WHERE type=?", [PROPOSAL_TYPE]
    if status:
        sql += " AND status=?"; params.append(status)
    return [_node_dict(r) for r in con.execute(sql + " ORDER BY created,id", params).fetchall()]


def _lr_link_evidence(con, src: str, evidence: list) -> None:
    for e in evidence:
        api_link(con, src, e["id"], "learned-from")


def api_propose_rules(con, *, dry_run: bool = False, days: int = LR_WINDOW_DAYS,
                      min_count: int = LR_MIN_COUNT,
                      min_sessions: int = LR_MIN_SESSIONS) -> dict:
    """Run the detector. A no-op, said out loud, while the setting is off."""
    if not learned_rules_enabled():
        return {"ok": True, "enabled": False, "ran": False, "hint": LR_OFF_HINT}
    det = detect_corrections(con, days=days, min_count=min_count, min_sessions=min_sessions)
    known = _lr_proposals(con)
    created, refreshed, skipped = [], [], []
    for cl in det["clusters"]:
        # An existing proposal in ANY state claims the cluster, by similarity
        # rather than by id: a rejected cluster must not come back because one
        # more prompt nudged its wording.
        twin = next((p for p in known
                     if _lr_jaccard(p["props"].get("tokens") or [], cl["tokens"])
                     >= LR_SIMILARITY), None)
        evidence = [{"id": i["id"], "session": i["session"], "snippet": i["clause"][:120]}
                    for i in cl["items"][-LR_MAX_EVIDENCE:]]
        if twin:
            if twin["status"] != "proposed":
                skipped.append({"id": twin["id"], "status": twin["status"]})
                continue
            if twin["props"].get("count") == cl["count"]:
                continue
            refreshed.append(twin["id"])
            if not dry_run:
                api_update_node(con, twin["id"], props_patch={
                    "count": cl["count"], "sessions": cl["sessions"],
                    "evidence": evidence, "refreshed_at": now()})
                _lr_link_evidence(con, twin["id"], evidence)
            continue
        pid = "proposal_" + hashlib.sha1(" ".join(cl["tokens"]).encode()).hexdigest()[:10]
        entry = {"id": pid, "text": cl["text"], "count": cl["count"],
                 "sessions": len(cl["sessions"]), "evidence": evidence}
        created.append(entry)
        if dry_run:
            continue
        res = api_create_node(
            con, PROPOSAL_TYPE, "Learned: " + cl["text"][:70], cl["text"],
            {"tokens": cl["tokens"], "count": cl["count"], "sessions": cl["sessions"],
             "evidence": evidence, "detected_at": now(), "source": "learned"},
            "proposed", "", "shared", pid)
        if res.get("ok"):
            _lr_link_evidence(con, pid, evidence)
            known.append(res["node"])
        else:
            entry["error"] = res.get("error")
    return {"ok": True, "enabled": True, "ran": True, "dry_run": bool(dry_run),
            "scanned_prompts": det["scanned"], "window_days": det["days"],
            "min_count": min_count, "min_sessions": min_sessions,
            "proposed": created, "refreshed": refreshed, "skipped": skipped,
            "hint": "review with `rule-proposals`; apply with `accept-rule --id`"}


def api_rule_proposals(con, *, status: str = "proposed", budget: int = READ_BUDGET,
                       limit: int = 50) -> dict:
    """List proposals, budgeted (CONTRIBUTING rule 4). Works while the setting is
    off: reviewing what is already there is not running the detector."""
    rows = _lr_proposals(con, "" if status == "all" else status)
    rows.sort(key=lambda p: (-(p["props"].get("count") or 0), p["id"]))
    slim, used = [], 0
    for p in rows[:limit]:
        pr = p["props"]
        entry = {"id": p["id"], "status": p["status"], "title": p["title"],
                 "text": p["body"][:300], "count": pr.get("count", 0),
                 "sessions": len(pr.get("sessions") or []),
                 "evidence": [{"id": e["id"], "snippet": e["snippet"]}
                              for e in (pr.get("evidence") or [])[:3]]}
        size = len(json.dumps(entry))
        if slim and used + size > budget:
            break
        used += size
        slim.append(entry)
    return {"ok": True, "enabled": learned_rules_enabled(), "status": status,
            "count": len(slim), "of": len(rows), "budget": budget, "chars": used,
            "truncated": len(slim) < len(rows),
            "hint": "accept-rule --id X [--title T --text T] | reject-rule --id X; "
                    "get-node --id X for the whole evidence",
            "proposals": slim}


def api_accept_rule(con, pid: str, *, title: str = "", text: str = "") -> dict:
    """Turn a proposal into an ACTIVE rule: source `learned`, precedence 50,
    linked to the proposal and to the prompts that taught it."""
    row = con.execute("SELECT * FROM nodes WHERE id=? AND type=?", (pid, PROPOSAL_TYPE)).fetchone()
    if not row:
        return {"ok": False, "error": f"no rule proposal {pid}"}
    p = _node_dict(row)
    if p["status"] != "proposed":
        return {"ok": False, "error": f"{pid} is already {p['status']}"}
    pr = p["props"]
    rule_text = (text or p["body"]).strip()
    rule_title = (title or p["title"]).strip()
    if not rule_text or not rule_title:
        return {"ok": False, "error": "a rule needs a title and a text"}
    n, s = pr.get("count", 0), len(pr.get("sessions") or [])
    res = api_add_rule(
        con, title=rule_title, source="learned", realm="shared",
        scenario=" ".join(t for t in pr.get("tokens", []) if not t.startswith("__"))
                 + " " + rule_title,
        sections={"the_rule": rule_text,
                  "why": f"Corrected {n} times across {s} sessions, then accepted "
                         f"from proposal {pid}.",
                  "judged_against": "The same correction is not needed again in a "
                                    "later session."},
        node_id=f"rule_learned_{slugify(rule_title)[:40]}_{pid[-4:]}")
    if not res.get("ok"):
        return res
    rid = res["node"]["id"]
    api_update_node(con, rid, props_patch={"proposal": pid, "evidence_count": n})
    api_link(con, rid, pid, "learned-from")
    _lr_link_evidence(con, rid, pr.get("evidence") or [])
    api_update_node(con, pid, status="accepted",
                    props_patch={"accepted_rule": rid, "accepted_at": now()})
    return {"ok": True, "rule": rid, "source": "learned",
            "precedence": RULE_PRECEDENCE["learned"], "proposal": pid}


def api_reject_rule(con, pid: str, *, reason: str = "") -> dict:
    """Mark a proposal rejected. It is KEPT, and it is what stops the cluster
    being proposed again."""
    row = con.execute("SELECT status FROM nodes WHERE id=? AND type=?",
                      (pid, PROPOSAL_TYPE)).fetchone()
    if not row:
        return {"ok": False, "error": f"no rule proposal {pid}"}
    if row["status"] != "proposed":
        return {"ok": False, "error": f"{pid} is already {row['status']}"}
    patch = {"rejected_at": now()}
    if reason:
        patch["reject_reason"] = reason
    api_update_node(con, pid, status="rejected", props_patch=patch)
    return {"ok": True, "proposal": pid, "status": "rejected", "kept": True}


def learned_rules_status(con) -> dict:
    counts = {}
    for p in _lr_proposals(con):
        counts[p["status"]] = counts.get(p["status"], 0) + 1
    on = learned_rules_enabled()
    return {"ok": True, "enabled": on,
            "source": flag_source(LEARNED_RULES_SETTING), "proposals": counts,
            "min_count": LR_MIN_COUNT, "min_sessions": LR_MIN_SESSIONS,
            "window_days": LR_WINDOW_DAYS,
            "automatic": "once a day at SessionStart, detached, only while on",
            "hint": None if on else LR_OFF_HINT}


def cmd_propose_rules(args):
    out(api_propose_rules(connect(), dry_run=args.dry_run, days=args.days,
                          min_count=args.min_count, min_sessions=args.min_sessions))


def cmd_rule_proposals(args):
    out(api_rule_proposals(connect(), status=args.status, budget=args.budget,
                           limit=args.limit))


def cmd_accept_rule(args):
    out(api_accept_rule(connect(), args.id, title=args.title or "", text=args.text or ""))


def cmd_reject_rule(args):
    out(api_reject_rule(connect(), args.id, reason=args.reason or ""))


def cmd_learned_rules(args):
    if args.action in ("on", "off"):
        save_settings({LEARNED_RULES_SETTING: args.action == "on"})
    out(learned_rules_status(connect()))


def cmd_use_skill(args):
    """Reinforce a skill after it fires — strengthen the pathway (usage_count up,
    confidence nudged toward 1, last_used stamped)."""
    con = connect()
    row = con.execute("SELECT * FROM nodes WHERE id=? AND type='skill'",
                      (args.id,)).fetchone()
    if not row:
        out({"ok": False, "error": f"no skill {args.id}"}); return
    cur = _node_dict(row)
    p = cur["props"]
    p["usage_count"] = int(p.get("usage_count", 0)) + 1
    p["last_used"] = now()
    p["confidence"] = round(min(1.0, float(p.get("confidence", 0.5)) + 0.1), 3)
    con.execute("UPDATE nodes SET props=?, updated=? WHERE id=?",
                (json.dumps(p), now(), args.id))
    con.commit()
    _after_write(con, nodes=[args.id])
    row = con.execute("SELECT * FROM nodes WHERE id=?", (args.id,)).fetchone()
    out({"ok": True, "skill": _node_dict(row)})


# ---------------------------------------------------------------------------
# link recognition — "what is this text already connected to?"
# ---------------------------------------------------------------------------
# Given free text (a task title you are typing), find the nodes it mentions and
# infer HOW they relate from the wording around each mention. Pure regex + a
# scan of the node table: no model, no tokens. Same engine the web UI calls as
# you type, so the UI needs no intelligence of its own.
#
# Cue -> relation, most specific first. The relation always reads
# "<the text> --rel--> <the mentioned node>", so the reciprocal side is implied
# (brain_query._relations derives badges from both ends).
_LINK_CUES = [
    ("blocked-by",  r"(?:is\s+)?blocked\s+(?:by|on)|waiting\s+(?:on|for)"
                    r"|held\s+up\s+by|gated\s+(?:on|by)|stuck\s+(?:on|behind)"
                    r"|pending\b|behind\b"),
    ("blocks",      r"(?:this\s+)?blocks\b|is\s+blocking\b|blocking\b|unblocks\b"),
    ("caused-by",   r"caused\s+by|because\s+of|due\s+to|regression\s+(?:from|of)"
                    r"|introduced\s+(?:by|in)|broken\s+by|broke\s+in|fallout\s+(?:from|of)"),
    ("causes",      r"causes?\b|causing\b|breaks?\b|breaking\b|leads?\s+to"
                    r"|results?\s+in|regresses\b"),
    ("depends-on",  r"depends?\s+on|depending\s+on|requires?\b|needs?\b|relies\s+on"),
    ("has-subtask", r"has\s+subtasks?|includes?\b|contains?\b|breaks?\s+down\s+into"),
    ("part-of",     r"part\s+of|subtask\s+of|belongs\s+to|under\b|within\b|inside\b"),
    ("follows-up",  r"follow(?:s|ing)?[\s-]?ups?\s+(?:on|with)|follow\s+up\s+(?:on|with)"
                    r"|after\b|once\b"),
    ("assigned-to", r"assign(?:ed)?\s+to|delegate[dsn]?\s+to|hand(?:ed)?\s+off\s+to"
                    r"|owner\s*[:=]"),
    ("about",       r"about\b|regarding\b|\bre\s*:|context\s+for"),
    ("mentions",    r"\bwith\b|\bcc\b|\bfyi\b"),
]
_LINK_CUES = [(rel, re.compile(r"(?:" + pat + r")[\s:,\-–—]*$")) for rel, pat in _LINK_CUES]

# how far back from a mention we look for a cue word
_CUE_WINDOW = 40

# Filler that commonly sits between the cue and the mention — "blocked by *PR*
# #7366", "waiting on *the ticket* ABC-2651". Stripped from the tail of the
# window before cue matching. Deliberately contains NO prepositions or verbs:
# cues like "depends on" / "caused by" / "part of" end in one, and eating it
# would stop them matching.
_MENTION_FILLER = re.compile(
    r"(?:\b(?:pr|mr|pull\s+request|ticket|issue|task|bug|card|node|epic|story|item"
    r"|the|a|an)\b|[\s:,\-–—]+)+$")


def _mention_keys(d: dict) -> set:
    """Distinctive strings that, appearing in free text, mean THIS node.

    Deliberately narrow: a whole title (only if long enough to be unambiguous),
    plus the identifiers people actually type — ticket keys (ABC-4521), PR/issue
    numbers (#7366), channel handles (#team-devs), and a person's first name.
    """
    title = (d.get("title") or "").strip().lower()
    if not title:
        return set()
    keys = set()
    if len(title) >= 6:
        keys.add(title)
    keys.update(re.findall(r"\b[a-z]{2,5}-\d{1,6}\b", title))   # ABC-4521, MS-33
    keys.update(re.findall(r"#\d{2,6}", title))                  # #7366
    if title.startswith("#") and len(title) > 3:                 # #team-devs
        keys.add(title)
    if d.get("type") == "person":
        for part in re.split(r"\s+", title):
            if len(part) >= 3:
                keys.add(part)                                   # first/last name
    return keys


def _find_mention(text_low: str, key: str) -> int:
    """Index of `key` in text as a whole token, or -1. Boundary-aware so 'abc-4'
    does not match inside 'abc-4521' and a name does not match mid-word."""
    m = re.search(r"(?<![\w#-])" + re.escape(key) + r"(?![\w-])", text_low)
    return m.start() if m else -1


def suggest_links(con, text: str, limit: int = 8, exclude: str = "") -> list:
    """Nodes this text mentions, each with the relation its wording implies."""
    text = (text or "").strip()
    if len(text) < 3:
        return []
    low = text.lower()
    rows = con.execute("SELECT id,type,title,status FROM nodes").fetchall()
    hits = {}
    for row in rows:
        d = dict(row)
        if d["id"] == exclude:
            continue
        best = -1
        matched = ""
        for key in _mention_keys(d):
            idx = _find_mention(low, key)
            # prefer the longest key that matches (most specific mention)
            if idx >= 0 and len(key) > len(matched):
                best, matched = idx, key
        if best < 0:
            continue
        before = _MENTION_FILLER.sub("", low[max(0, best - _CUE_WINDOW):best])
        after = low[best + len(matched):best + len(matched) + 12]
        rel, cue = "", ""
        for candidate, pat in _LINK_CUES:
            m = pat.search(before)
            if m:
                rel, cue = candidate, m.group(0).strip(" :,-–—")
                break
        if not rel:
            # no cue: a person is mentioned, anything else merely relates
            rel = "mentions" if d["type"] == "person" else "relates-to"
        # "needs X first" / "requires X first" is a block, not a dependency
        if rel == "depends-on" and re.match(r"\s+first\b", after):
            rel, cue = "blocked-by", (cue + " … first").strip()
        hit = {"id": d["id"], "type": d["type"], "title": d["title"],
               "status": d["status"] or "", "rel": rel, "cue": cue,
               "matched": matched, "at": best,
               # how much of this node's OWN title the mention accounts for —
               # "#7366" is most of the PR's title but incidental in a task that
               # merely quotes it
               "cover": len(matched) / max(1, len(d["title"] or ""))}
        prev = hits.get(d["id"])
        if prev is None or len(matched) > len(prev["matched"]):
            hits[d["id"]] = hit

    # One node per distinct mention. Several nodes can match the SAME text at the
    # SAME position (a task whose title quotes "PR #7366" collides with the PR).
    # Keep the one the identifier most belongs to, so the suggestion list is not
    # padded with everything that ever referenced the ticket.
    by_span = {}
    for h in hits.values():
        key = (h["matched"], h["at"])
        best_so_far = by_span.get(key)
        if best_so_far is None or h["cover"] > best_so_far["cover"]:
            by_span[key] = h
    out_ = sorted(by_span.values(), key=lambda h: (h["at"], -len(h["matched"])))
    for h in out_:
        h.pop("cover", None)
    return out_[:limit]


def cmd_suggest_links(args):
    con = connect()
    res = suggest_links(con, args.text, int(args.limit), args.exclude or "")
    out({"ok": True, "count": len(res), "text": args.text, "suggestions": res})


# ---------------------------------------------------------------------------
# person identity — one human, one node
# ---------------------------------------------------------------------------
# The same colleague arrives through many doors: a Slack display name, an email
# local part, a first name in a meeting transcript, a surname in a Jira comment.
# Left alone that produces "Mari", "Maria" and "Maria Castillo" as three people,
# which quietly splits their tasks three ways.
#
# Two tiers, because the cost of the two mistakes is not the same. Merging two
# real people is much worse than leaving a duplicate: it silently reassigns work.
# So CERTAIN is narrow enough to act on unattended, LIKELY only ever resolves when
# exactly one candidate fits, and anything ambiguous is handed back for a human to
# decide rather than guessed at.
#
# Nothing here rewrites a name that was already written down (§ the rename rule):
# a merge leaves the duplicate node in place as a resolver pointing at the
# canonical one, and props.assignee keeps whatever string it was given. Identity
# is resolved when READ, so old captures stay honest about their wording.
_NAME_NOISE = {"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "the"}


def norm_name(raw: str) -> str:
    """Casefold, strip accents, drop punctuation, collapse whitespace.
    'Maria  CASTILLO' / 'maria castillo' / 'María Castillo' all agree."""
    s = unicodedata.normalize("NFKD", (raw or "").strip())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^\w\s@.]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def name_parts(raw: str) -> list:
    """A name reduced to comparable parts. An email becomes its local part split
    on the usual separators, so maria.castillo@example.com -> [maria, castillo]."""
    s = norm_name(raw)
    if "@" in s:
        s = s.split("@", 1)[0]
    parts = [p for p in re.split(r"[\s._-]+", s) if len(p) >= 2 and p not in _NAME_NOISE]
    return parts


def name_match(a: str, b: str) -> str:
    """How confidently two names denote one person: 'certain' | 'likely' | ''.

    The load-bearing rule is the last one: two names that BOTH carry a surname and
    disagree on it are different people, however much the first name matches.
    Without it 'Daniel Reyes' and 'Daniel Moore' collapse into one.
    """
    pa, pb = name_parts(a), name_parts(b)
    if not pa or not pb:
        return ""
    if pa == pb:
        return "certain"
    sa, sb = set(pa), set(pb)
    # one side is a bare single name — "Mari", or "Castillo"
    if len(pa) == 1 or len(pb) == 1:
        short = (pa if len(pa) == 1 else pb)[0]
        long_ = pb if len(pa) == 1 else pa
        if short in set(long_):
            return "likely"                      # a first name or a surname on its own
        # a nickname clipped off the front: mari -> maria, dan -> daniel
        if len(short) >= 3 and any(p.startswith(short) for p in long_):
            return "likely"
        return ""
    # both sides carry at least two parts
    if sa == sb:
        return "certain"                         # "Castillo, Maria" — same parts, reordered
    if pa[0] == pb[0] and pa[-1] == pb[-1]:
        return "certain"                         # middle name added or dropped
    # Deliberately NOT matching on a shared surname plus a matching first initial.
    # It reads as a rule for "M. Castillo", but that already reduces to a single
    # token and is caught above; the only thing it actually catches is
    # "Daniel Reyes" vs "Daniela Reyes" — two different people whose work would
    # then be silently pooled. A duplicate node is much cheaper than that.
    return ""


def person_aliases(d: dict) -> list:
    """Every spelling a person node answers to: pinned aliases plus its title.
    props.aliases is the human's override for what the heuristic cannot infer —
    a nickname sharing no letters with the name, a maiden name, a handle."""
    raw = (d.get("props") or {}).get("aliases") or []
    if isinstance(raw, str):
        raw = [p for p in re.split(r"\s*[,;]\s*", raw) if p.strip()]
    names = [d.get("title") or ""]
    names += [str(x) for x in raw if str(x).strip()]
    email = (d.get("props") or {}).get("email") or ""
    if email:
        names.append(str(email))
    return [n for n in names if n.strip()]


def person_rows(con, include_superseded=False) -> list:
    """Every person node, newest write last. Superseded nodes are resolvers left
    behind by a merge — real history, but not people to offer in a picker."""
    rows = con.execute(
        "SELECT * FROM nodes WHERE type='person' ORDER BY created"
    ).fetchall()
    out_ = []
    for r in rows:
        d = _node_dict(r)
        if not include_superseded and (d["props"] or {}).get("superseded_by"):
            continue
        out_.append(d)
    return out_


def resolve_person(con, name: str) -> dict:
    """Find the ONE person node `name` denotes.

    Returns {"ok": True, "id": ...} on a confident single answer;
    {"ok": False, "candidates": [...]} when the name is ambiguous or unknown, so
    the caller can ask instead of guessing. A merged-away name still resolves,
    via the alias the merge recorded on the canonical node.
    """
    if not (name or "").strip():
        return {"ok": False, "error": "missing name", "candidates": []}
    certain, likely = [], []
    for d in person_rows(con):
        best = ""
        for alias in person_aliases(d):
            m = name_match(name, alias)
            if m == "certain":
                best = "certain"
                break
            if m == "likely":
                best = best or "likely"
        if best == "certain":
            certain.append(d)
        elif best == "likely":
            likely.append(d)
    pool = certain or likely
    brief = [{"id": d["id"], "title": d["title"],
              "aliases": person_aliases(d)[1:]} for d in pool]
    if len(pool) == 1:
        return {"ok": True, "id": pool[0]["id"], "title": pool[0]["title"],
                "confidence": "certain" if certain else "likely"}
    if not pool:
        return {"ok": False, "error": f"no person matches {name!r}", "candidates": []}
    return {"ok": False, "error": f"{name!r} is ambiguous — {len(pool)} people match",
            "candidates": brief}


def person_duplicates(con) -> list:
    """Groups of person nodes that look like one human. Read-only: this reports,
    it never merges, because the ambiguous cases need a human."""
    people = person_rows(con)
    seen, groups = set(), []
    for i, a in enumerate(people):
        if a["id"] in seen:
            continue
        group = [a]
        for b in people[i + 1:]:
            if b["id"] in seen:
                continue
            if _best_match(a, b):
                group.append(b)
        if len(group) > 1:
            for g in group:
                seen.add(g["id"])
            # the most connected node is the best canonical: it carries the history,
            # so merging into it moves the fewest edges
            ranked = sorted(group, key=lambda d: -_person_degree(con, d["id"]))
            keep = ranked[0]
            groups.append({
                "canonical": {"id": keep["id"], "title": keep["title"],
                              "edges": _person_degree(con, keep["id"])},
                # scored against the CANONICAL, not against whichever node happened
                # to anchor the scan — ranking reorders the group, so anything else
                # mislabels the pair that is actually going to be merged
                "duplicates": [{"id": d["id"], "title": d["title"],
                                "edges": _person_degree(con, d["id"]),
                                "confidence": _best_match(keep, d) or "likely"}
                               for d in ranked[1:]],
            })
    return groups


def _best_match(a: dict, b: dict) -> str:
    """Strongest verdict over every pair of spellings the two nodes answer to."""
    best = ""
    for na in person_aliases(a):
        for nb in person_aliases(b):
            m = name_match(na, nb)
            if m == "certain":
                return "certain"
            if m == "likely":
                best = "likely"
    return best


def _person_degree(con, nid: str) -> int:
    row = con.execute(
        "SELECT (SELECT COUNT(*) FROM edges WHERE src=?) +"
        "       (SELECT COUNT(*) FROM edges WHERE dst=?) AS n", (nid, nid)).fetchone()
    return (dict(row) or {}).get("n", 0) or 0


def api_add_alias(con, person_id: str, alias: str) -> dict:
    """Pin a spelling onto a person, for what the heuristic cannot infer."""
    row = con.execute("SELECT * FROM nodes WHERE id=?", (person_id,)).fetchone()
    if not row:
        return {"ok": False, "error": f"no node {person_id}"}
    d = _node_dict(row)
    alias = (alias or "").strip()
    if not alias:
        return {"ok": False, "error": "missing alias"}
    have = (d["props"] or {}).get("aliases") or []
    if isinstance(have, str):
        have = [p for p in re.split(r"\s*[,;]\s*", have) if p.strip()]
    if norm_name(alias) == norm_name(d["title"]) or \
       any(norm_name(alias) == norm_name(x) for x in have):
        return {"ok": True, "node": d, "added": False}   # already answers to it
    res = api_update_node(con, person_id, props_patch={"aliases": have + [alias]})
    if res.get("ok"):
        res["added"] = True
    return res


def api_merge_person(con, canonical_id: str, duplicate_id: str) -> dict:
    """Fold a duplicate person into the canonical one, without losing the wording.

    Edges move to the canonical node, the duplicate's name is pinned there as an
    alias, and the duplicate stays in the graph as a RESOLVER: superseded_by plus
    an edge pointing at its replacement. Nothing is deleted and no already-written
    name is rewritten, so an old capture that said "Mari" still says "Mari" and still
    resolves to the right human.
    """
    if canonical_id == duplicate_id:
        return {"ok": False, "error": "cannot merge a node into itself"}
    keep = con.execute("SELECT * FROM nodes WHERE id=?", (canonical_id,)).fetchone()
    drop = con.execute("SELECT * FROM nodes WHERE id=?", (duplicate_id,)).fetchone()
    if not keep:
        return {"ok": False, "error": f"no node {canonical_id}"}
    if not drop:
        return {"ok": False, "error": f"no node {duplicate_id}"}
    keep_d, drop_d = _node_dict(keep), _node_dict(drop)
    for d in (keep_d, drop_d):
        if d["type"] != "person":
            return {"ok": False, "error": f"{d['id']} is a {d['type']}, not a person"}
    if (drop_d["props"] or {}).get("superseded_by"):
        return {"ok": False, "error": f"{duplicate_id} is already merged away"}

    moved, dropped = 0, 0
    for e in con.execute("SELECT src,dst,rel FROM edges WHERE src=? OR dst=?",
                         (duplicate_id, duplicate_id)).fetchall():
        src, dst, rel = e["src"], e["dst"], e["rel"]
        nsrc = canonical_id if src == duplicate_id else src
        ndst = canonical_id if dst == duplicate_id else dst
        con.execute("DELETE FROM edges WHERE src=? AND dst=? AND rel=?", (src, dst, rel))
        if nsrc == ndst:
            dropped += 1            # the merge made this edge a self-loop
            continue
        try:
            con.execute("INSERT INTO edges(src,dst,rel,props,created) VALUES(?,?,?,?,?)",
                        (nsrc, ndst, rel, "{}", now()))
            moved += 1
        except Exception:
            dropped += 1            # canonical already had it — UNIQUE(src,dst,rel)
    con.commit()

    api_add_alias(con, canonical_id, drop_d["title"])
    for extra in person_aliases(drop_d)[1:]:
        api_add_alias(con, canonical_id, extra)
    api_update_node(con, duplicate_id,
                    props_patch={"superseded_by": canonical_id,
                                 "superseded_at": now()})
    api_link(con, duplicate_id, canonical_id, "relates-to")
    _after_write(con, nodes=[canonical_id, duplicate_id])
    row = con.execute("SELECT * FROM nodes WHERE id=?", (canonical_id,)).fetchone()
    return {"ok": True, "canonical": _node_dict(row),
            "merged": duplicate_id, "edges_moved": moved,
            "edges_deduped": dropped}


def api_create_person(con, title: str, body="", props=None, realm="") -> dict:
    """Create a person, unless one already exists under any spelling.

    The guard is the whole point: it returns the EXISTING node instead of a second
    one, so the graph converges on one node per human even when the name arrives
    differently each time. An ambiguous name is refused with its candidates rather
    than resolved by coin flip.
    """
    title = (title or "").strip()
    if not title:
        return {"ok": False, "error": "missing title"}
    hit = resolve_person(con, title)
    if hit.get("ok"):
        row = con.execute("SELECT * FROM nodes WHERE id=?", (hit["id"],)).fetchone()
        res = {"ok": True, "node": _node_dict(row), "created": False,
               "matched": hit.get("confidence")}
        # the incoming spelling is new information — record it, do not overwrite
        if norm_name(title) != norm_name(res["node"]["title"]):
            api_add_alias(con, hit["id"], title)
            row = con.execute("SELECT * FROM nodes WHERE id=?", (hit["id"],)).fetchone()
            res["node"] = _node_dict(row)
            res["alias_added"] = title
        return res
    if hit.get("candidates"):
        return {"ok": False, "error": hit["error"], "candidates": hit["candidates"],
                "hint": "merge them or pass an explicit alias"}
    res = api_create_node(con, "person", title, body, props or {}, realm=realm)
    if res.get("ok"):
        res["created"] = True
    return res


# ---------------------------------------------------------------------------
# write API — one implementation, shared by the CLI and the web UI
# ---------------------------------------------------------------------------
# These return plain dicts (no printing) so scripts/serve.py can call them directly
# and stay in lockstep with the CLI: same validation, same neo4j sync.
TASK_STATUSES = ["open", "doing", "blocked", "waiting", "done", "cancelled"]


def api_create_node(con, node_type, title, body="", props=None, status="",
                    due="", realm="", node_id=""):
    title = (title or "").strip()
    if not title:
        return {"ok": False, "error": "missing title"}
    # NODE_TYPES is a SOFT vocabulary (§3) — the live graph legitimately holds
    # channel/team/process/capture/pr nodes that are not in it. Callers that
    # need a closed set (e.g. the web UI) enforce it at their own boundary.
    if not (node_type or "").strip():
        return {"ok": False, "error": "missing type"}
    nid = node_id or _gen_id(con, node_type, title)
    ts = now()
    con.execute(
        "INSERT INTO nodes(id,type,title,body,props,status,due,realm,created,updated)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (nid, node_type, title, body or "", json.dumps(props or {}),
         status or "", due or "", realm or DEFAULT_REALM, ts, ts))
    con.commit()
    sync = _after_write(con, nodes=[nid])
    row = con.execute("SELECT * FROM nodes WHERE id=?", (nid,)).fetchone()
    return {"ok": True, "node": _node_dict(row), "sync": sync}


def api_update_node(con, node_id, *, title=None, body=None, status=None,
                    due=None, realm=None, props_patch=None):
    row = con.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
    if not row:
        return {"ok": False, "error": f"no node {node_id}"}
    cur = _node_dict(row)
    props = cur["props"]
    if props_patch:
        props = {**props, **props_patch}
    con.execute(
        "UPDATE nodes SET title=?,body=?,props=?,status=?,due=?,realm=?,updated=? WHERE id=?",
        (cur["title"] if title is None else title,
         cur["body"] if body is None else body,
         json.dumps(props),
         cur["status"] if status is None else status,
         cur["due"] if due is None else due,
         realm or cur.get("realm") or DEFAULT_REALM,
         now(), node_id))
    con.commit()
    sync = _after_write(con, nodes=[node_id])
    row = con.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
    return {"ok": True, "node": _node_dict(row), "sync": sync}


def api_complete(con, node_id, note=""):
    """Archive, never delete (CONVENTIONS §2a): status=done + completed_at."""
    row = con.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
    if not row:
        return {"ok": False, "error": f"no node {node_id}"}
    patch = {"completed_at": now()}
    if note:
        patch["completion_note"] = note
    res = api_update_node(con, node_id, status="done", props_patch=patch)
    if res.get("ok"):
        res["archived"] = True
    return res


def api_set_status(con, node_id, status, waiting_on=None):
    """Move a node's state. Mirrors graph set-state: stamps state_since so
    age_days stays honest, and mirrors onto status for tasks."""
    if status not in TASK_STATUSES:
        return {"ok": False, "error": f"unknown status: {status}"}
    if status == "done":
        return api_complete(con, node_id)
    patch = {"state": status, "state_since": now()}
    if waiting_on is not None:
        patch["waiting_on"] = waiting_on
    return api_update_node(con, node_id, status=status, props_patch=patch)


def api_link(con, src, dst, rel, props=None):
    # EDGE_RELS is also soft — hooks legitimately write `captured-from`. Closed
    # sets are enforced by the caller (see scripts/serve.py).
    if not (rel or "").strip():
        return {"ok": False, "error": "missing relation"}
    if src == dst:
        return {"ok": False, "error": "cannot link a node to itself"}
    for end in (src, dst):
        if not con.execute("SELECT 1 FROM nodes WHERE id=?", (end,)).fetchone():
            return {"ok": False, "error": f"no node {end}"}
    try:
        con.execute("INSERT INTO edges(src,dst,rel,props,created) VALUES(?,?,?,?,?)",
                    (src, dst, rel, json.dumps(props or {}), now()))
        con.commit()
    except Exception:
        pass   # UNIQUE(src,dst,rel) -> idempotent
    sync = _after_write(con, edges=[(src, dst, rel)])
    return {"ok": True, "edge": {"src": src, "dst": dst, "rel": rel}, "sync": sync}


def api_unlink(con, src, dst, rel):
    cur = con.execute("DELETE FROM edges WHERE src=? AND dst=? AND rel=?",
                      (src, dst, rel))
    con.commit()
    sync = _after_write(con, del_edges=[(src, dst, rel)])
    return {"ok": cur.rowcount > 0, "removed": cur.rowcount,
            "edge": {"src": src, "dst": dst, "rel": rel}, "sync": sync}


# ---------------------------------------------------------------------------
# memory dynamics — strength, decay, spreading activation, Hebbian wiring
# ---------------------------------------------------------------------------
# A brain does not keep every trace at equal weight, and it does not retrieve by
# string match. Three properties we mirror here, all in pure SQL/regex (no model,
# so recall stays free):
#
#   strength   every node carries access_count/last_accessed; strength RISES with
#              use and DECAYS with disuse, so what has proven useful ranks first.
#   activation recall seeds on full-text, then spreads across edges with distance
#              decay — asking about a ticket also surfaces its PR, its blocker
#              and the person it is assigned to.
#   Hebbian    nodes that activate together get wired together, so the graph
#              densifies itself instead of relying on manual linking.
#
# Everything below is also a TOKEN optimisation: better ranking means the caller
# needs fewer results, and payloads are slimmed to a snippet instead of whole
# bodies (see _slim). match-skills alone drops ~8.1k chars -> ~0.6k.

ACTIVATION_DECAY = 0.45        # activation retained per edge hop
ACTIVATION_DEPTH = 2           # how far activation spreads
STRENGTH_HALF_LIFE = 21.0      # days for an untouched memory to halve
RECALL_BUDGET = 1600           # default char budget for a recall payload
SNIPPET = 150                  # chars of body context returned per hit
HEBBIAN_TOP = 3                # how many co-activated nodes may wire together
HEBBIAN_MAX_NEW = 2            # cap new edges per recall, so it never hairballs
HEBBIAN_THRESHOLD = 3          # co-activations required before an edge forms
# Hubs, and things that should be linked by intent rather than correlation:
# a person/channel co-activates with everything, and skills carry deliberate
# applies-to / learned-from edges (CONVENTIONS §2b).
HEBBIAN_SKIP_TYPES = {"skill", "channel", "person", "capture"}


def _days_since(iso: str) -> float:
    if not iso:
        return 999.0
    try:
        then = datetime.fromisoformat(iso)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - then).total_seconds() / 86400.0)
    except Exception:
        return 999.0


def _strength(props: dict) -> float:
    """0..1 memory strength. Repetition raises the ceiling, disuse decays toward
    it — the forgetting curve, not a hard expiry. Nothing is ever deleted."""
    uses = int(props.get("access_count", 0) or 0)
    ceiling = min(1.0, 0.25 + 0.18 * (uses ** 0.5))
    age = _days_since(props.get("last_accessed") or props.get("updated") or "")
    return round(ceiling * (0.5 ** (age / STRENGTH_HALF_LIFE)), 4)


def _touch(con, ids) -> None:
    """Reinforce on READ — reading is what strengthens a memory. It never PUSHES
    to neo4j, because a read storm must not become a write storm; the mirror
    trigger queues the node, and it rides along with the next real write's flush."""
    ts = now()
    for nid in set(i for i in ids if i):
        row = con.execute("SELECT props FROM nodes WHERE id=?", (nid,)).fetchone()
        if not row:
            continue
        try:
            p = json.loads(row["props"] or "{}")
        except Exception:
            p = {}
        p["access_count"] = int(p.get("access_count", 0) or 0) + 1
        p["last_accessed"] = ts
        p["strength"] = _strength(p)
        con.execute("UPDATE nodes SET props=? WHERE id=?", (json.dumps(p), nid))
    con.commit()


def _excerpt(body: str, terms, width: int = SNIPPET) -> str:
    """A snippet around the first matching term — not the whole body. This is
    where most of the token saving comes from."""
    body = re.sub(r"\s+", " ", body or "").strip()
    if len(body) <= width:
        return body
    low = body.lower()
    at = -1
    for t in terms or []:
        at = low.find(t.lower())
        if at >= 0:
            break
    if at < 0:
        return body[:width].rstrip() + "…"
    start = max(0, at - width // 3)
    return ("…" if start else "") + body[start:start + width].rstrip() + "…"


def _slim(d: dict, terms=(), snippet=True) -> dict:
    """Compact form of a node for machine reading: identity, state, and a snippet
    — never the full body or the whole props blob."""
    p = d.get("props") or {}
    out_ = {"id": d["id"], "type": d["type"], "title": d["title"]}
    for k in ("status", "due", "realm"):
        if d.get(k):
            out_[k] = d[k]
    for k in ("priority", "assignee", "waiting_on", "delegated"):
        if p.get(k):
            out_[k] = p[k]
    if snippet and d.get("body"):
        out_["snippet"] = _excerpt(d["body"], terms)
    return out_


# Function words carry no cue value, and each one widens the activation sweep for
# nothing. Ships English + pt-BR; add the languages your brain is driven in.
_CUE_STOP = {
    "the","and","for","was","were","are","did","does","what","when","who","why","how",
    "with","from","that","this","these","those","have","has","had","will","would",
    "can","could","should","about","into","over","you","your","yours","our","ours",
    "his","her","its","their","them","they","not","but","any","all","some","more",
    "just","only","then","than","also","been","being","get","got","let","ask","asked",
    "que","com","para","por","dos","das","uma","uns","umas","nao","não","sim","meu",
    "minha","seu","sua","dele","dela","isso","esse","essa","aquilo","como","quando",
    "quem","onde","porque","porqu","tem","ter","tinha","fez","fazer","foi","era",
    "pra","pro","nos","nas","aos","mais","mas","ele","ela","eles","elas","voce","você",
}


# Associated nodes with more edges than HUB_FREE_DEGREE are divided by
# sqrt(degree/HUB_FREE_DEGREE), on top of the log penalty — the owner's own node
# stops polluting generic recalls.
HUB_FREE_DEGREE = 8
# Associated rows scoring under this fraction of the top row are dropped.
RECALL_TAIL_CUT = 0.15

# Longest first. A stem is only taken when >= 4 chars remain, so short words and
# acronyms are left alone. The stem feeds a PREFIX query (`"deploy"*`), so
# `deploys`/`deploying`/`deployed` all land on `deploy…` — and it never has to
# equal a real word, only to be a safe prefix of the inflections.
_STEM_SUFFIXES = (
    "mente", "ções", "coes", "ção", "cao", "ando", "endo", "indo",
    "ings", "ing", "ied", "ies", "ed", "es", "s", "ões", "oes", "ais", "eis", "is",
)


def _stem(w: str) -> str:
    for suf in _STEM_SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[: -len(suf)]
    return w


def _fts_terms(q: str):
    words = [t for t in re.findall(r"\w+", (q or "").lower()) if len(t) > 2]
    kept = [t for t in words if t not in _CUE_STOP]
    return kept or words          # never end up with an empty cue


def _fts_query(words) -> str:
    """OR of prefix terms over the stemmed words (deduplicated, order kept)."""
    stems = list(dict.fromkeys(_stem(w) for w in words))
    return " OR ".join(f'"{w}"*' for w in stems)


def cmd_recall(args):
    """Associative recall: seed on full-text, spread activation across edges,
    modulate by memory strength, return a SLIM ranked payload inside a char
    budget. One call replaces search + neighbors, and costs fewer tokens."""
    out(api_recall(connect(), args.query or "", limit=args.limit, depth=args.depth,
                   budget=args.budget, include_all=args.all,
                   include_captures=args.include_captures, wire=args.wire))


def api_recall(con, q: str, *, limit=8, depth=None, budget=None, include_all=False,
               include_captures=False, wire=True, exclude=()) -> dict:
    """`recall` as a function: the same ranking, returned instead of printed.
    `exclude` drops ids from the results (a caller that already holds them)."""
    depth = ACTIVATION_DEPTH if depth is None else depth
    budget = RECALL_BUDGET if budget is None else budget
    exclude = set(exclude or ())
    words = _fts_terms(q)
    seeds = {}
    if words:
        terms = _fts_query(words)
        try:
            rows = con.execute(
                "SELECT n.* FROM nodes_fts f JOIN nodes n ON n.rowid=f.rowid"
                " WHERE nodes_fts MATCH ? ORDER BY rank LIMIT 40", (terms,)).fetchall()
        except Exception:
            rows = []
        for i, r in enumerate(rows):
            seeds[r["id"]] = 1.0 / (1.0 + i)

    # Spreading activation, summing convergent paths (evidence adds up).
    #
    # Activation is normalised by FAN-OUT: a node divides what it passes among its
    # neighbours instead of handing each one a full share. Without this, hubs win
    # everything — the owner's `person` node has ~80 edges, so it collects
    # activation from every seed and then re-broadcasts it, and a query for a
    # ticket comes back as a list of people. Dividing by degree keeps a hub's
    # individual links weak while a sparse, specific link stays strong.
    act = dict(seeds)
    frontier = dict(seeds)
    origin = {k: k for k in seeds}        # node -> seed that fed it most
    best_in = {}                          # node -> strongest single share received
    for _ in range(max(0, int(depth))):
        nxt = {}
        for nid, a in frontier.items():
            nbrs = con.execute(
                "SELECT dst AS other FROM edges WHERE src=?"
                " UNION ALL SELECT src AS other FROM edges WHERE dst=?",
                (nid, nid)).fetchall()
            if not nbrs:
                continue
            share = a * ACTIVATION_DECAY / float(len(nbrs))
            if share < 0.005:
                continue
            for e in nbrs:
                nxt[e["other"]] = nxt.get(e["other"], 0.0) + share
                act[e["other"]] = act.get(e["other"], 0.0) + share
                if share > best_in.get(e["other"], 0.0):
                    best_in[e["other"]] = share
                    origin[e["other"]] = origin.get(nid, nid)
        frontier = nxt
        if not frontier:
            break
    if not act:
        return {"ok": True, "query": q, "count": 0, "results": []}

    ids = list(act)
    ph = ",".join("?" * len(ids))
    nodes = {r["id"]: _node_dict(r) for r in
             con.execute(f"SELECT * FROM nodes WHERE id IN ({ph})", ids).fetchall()}

    # degree of everything we might return, for the ubiquity penalty below
    deg = {r["id"]: r["n"] for r in con.execute(
        f"SELECT id, (SELECT COUNT(*) FROM edges e WHERE e.src=nodes.id OR e.dst=nodes.id)"
        f" AS n FROM nodes WHERE id IN ({ph})", ids).fetchall()}

    import math
    _decay_table = _decay_half_lives()
    scored = []
    for nid, a in act.items():
        d = nodes.get(nid)
        if not d or nid in exclude:
            continue
        if d["type"] == "capture" and not include_captures:
            continue          # the day's digest covers these (see `consolidate`)
        if d["type"] == "context-event":
            continue          # bookkeeping, never prose — read it with `context-at`
        if not include_all and (d.get("status") or "").lower() in DONE_STATUSES:
            continue
        s = _strength(d["props"])
        score = a * (0.55 + 0.45 * s)
        score *= _decay.rank_weight(d, _decay_table)   # per-type half-life (decay.py)
        # A node linked to everything (the owner's own person node, a hub channel)
        # is structurally uninformative — it answers every query and distinguishes
        # none. Penalise ubiquity, but only when the node was merely ASSOCIATED;
        # a lexical hit earned its rank.
        if nid not in seeds:
            score /= (1.0 + math.log2(1.0 + deg.get(nid, 0)))
            score /= max(1.0, math.sqrt(deg.get(nid, 0) / HUB_FREE_DEGREE))
        row = _slim(d, words)
        row["score"] = round(score, 4)
        if nid in seeds:
            row["why"] = "matched"
        else:
            src = nodes.get(origin.get(nid, ""))
            if src is None and origin.get(nid):
                r = con.execute("SELECT title FROM nodes WHERE id=?",
                                (origin[nid],)).fetchone()
                src = {"title": r["title"]} if r else None
            row["why"] = (f'linked to "{(src["title"] or "")[:48]}"' if src
                          else "associated")
        scored.append((score, row))
    scored.sort(key=lambda x: -x[0])
    # Tail cut: past a gap, the rest is noise that only costs budget. Matched rows
    # are never cut — a lexical hit earned its place.
    if scored:
        floor = scored[0][0] * RECALL_TAIL_CUT
        scored = [(sc, r) for sc, r in scored
                  if sc >= floor or r["why"] == "matched"]

    results, used = [], 0
    for _, row in scored:
        cost = len(json.dumps(row, ensure_ascii=False))
        if len(results) >= int(limit) or (used + cost) > int(budget):
            break
        results.append(row); used += cost

    top = [r["id"] for r in results]
    _touch(con, top)                                  # reading reinforces
    wired = _hebbian(con, top) if wire else []
    return {"ok": True, "query": q, "count": len(results), "payload_chars": used,
            "activated": len(act), "wired": wired, "results": results}


def _hebbian(con, ids) -> list:
    """Fire together, wire together — but only after a pattern REPEATS.

    Each recall tallies co-activation; an edge appears only once a pair has fired
    together HEBBIAN_THRESHOLD times. That is closer to real potentiation than
    wiring on first co-occurrence, and it stops lexical coincidences (a ticket
    about "export" briefly co-activating the export-memory skill) from becoming
    permanent structure. Skills, channels and people are excluded: they are hubs,
    or they get linked deliberately (CONVENTIONS §2b), not by correlation.
    """
    made = []
    if len(ids) < 2:
        return made
    types = {}
    ph = ",".join("?" * len(ids))
    for r in con.execute(f"SELECT id,type FROM nodes WHERE id IN ({ph})", ids):
        types[r["id"]] = r["type"]
    top = [i for i in ids[:HEBBIAN_TOP]
           if i and types.get(i) not in HEBBIAN_SKIP_TYPES]
    ts = now()
    for x, y in [(p, q) for i, p in enumerate(top) for q in top[i + 1:]]:
        a, b = sorted((x, y))
        con.execute(
            "INSERT INTO coactivation(a,b,n,last) VALUES(?,?,1,?)"
            " ON CONFLICT(a,b) DO UPDATE SET n=n+1, last=excluded.last", (a, b, ts))
        n = con.execute("SELECT n FROM coactivation WHERE a=? AND b=?",
                        (a, b)).fetchone()["n"]
        # Note the parenthesisation: AND binds tighter than OR, so the pair test
        # must be grouped or the rel filter only applies to one direction.
        existing = con.execute(
            "SELECT rel, props FROM edges"
            " WHERE ((src=? AND dst=?) OR (src=? AND dst=?))", (a, b, b, a)).fetchall()
        if existing:
            # Already associated by ANY relation (has-pr, blocks, part-of…) — the
            # association exists, so do not pile a redundant relates-to on top.
            # If it is our own Hebbian edge, keep its weight current.
            for e in existing:
                if e["rel"] != "relates-to":
                    continue
                try:
                    pr = json.loads(e["props"] or "{}")
                except Exception:
                    pr = {}
                if pr.get("hebbian"):
                    pr["weight"] = n
                    pr["last_fired"] = ts
                    con.execute(
                        "UPDATE edges SET props=? WHERE rel='relates-to'"
                        " AND ((src=? AND dst=?) OR (src=? AND dst=?))",
                        (json.dumps(pr), a, b, b, a))
            continue
        if n < HEBBIAN_THRESHOLD or len(made) >= HEBBIAN_MAX_NEW:
            continue                                    # not yet a pattern
        try:
            con.execute(
                "INSERT INTO edges(src,dst,rel,props,created) VALUES(?,?,?,?,?)",
                (a, b, "relates-to",
                 json.dumps({"hebbian": True, "weight": n, "last_fired": ts}), ts))
            made.append({"src": a, "dst": b, "rel": "relates-to", "after": n})
        except Exception:
            pass
    con.commit()
    if made:
        _after_write(con, edges=[(m["src"], m["dst"], m["rel"]) for m in made])
    return made


def cmd_resume(args):
    """Rehydrate the working thread after the context window was cleared.

    THE POINT OF THIS COMMAND: `/clear` is the cheapest thing you can do to a
    conversation — it drops the accumulated window, and every later request stops
    paying for it. What normally makes clearing expensive is that you lose your
    place. You don't, because the brain kept it. So clear often, then run this.

    Returns a SMALL brief inside a char budget: what was most recently in play,
    anything parked, and the open loops. Everything here is SQL and full-text —
    no model call. Detail is fetched on demand (`get-node`, `unpark`, `recall`),
    which is the whole trade: query the brain instead of carrying it.
    """
    con = connect()
    budget = int(args.budget or 1800)
    since = (datetime.now(timezone.utc) - timedelta(days=int(args.days or 2))).isoformat()

    # What was being worked on: recently touched, minus the machinery (captures,
    # digests, people, skills, rules) and minus work explicitly owned by someone
    # else — after a clear you want YOUR thread back, not the last thing ingested.
    me = owner().lower()
    focus = []
    for r in con.execute(
        "SELECT * FROM nodes WHERE updated>=? AND type NOT IN"
        " ('capture','parked','channel','person','skill','rule','spec','area','team',"
        "  'conversation','agent','transition','gauge','context-event')"
        " AND id NOT LIKE 'note_digest_%' ORDER BY updated DESC LIMIT 60",
            (since,)).fetchall():
        d = _node_dict(r)
        a = str((d["props"] or {}).get("assignee", "")).strip().lower()
        if a and me not in a and a not in ("me", "unassigned", "team"):
            continue
        focus.append(d)
        if len(focus) >= int(args.limit or 8):
            break

    parked = [_node_dict(r) for r in con.execute(
        "SELECT * FROM nodes WHERE type='parked' ORDER BY updated DESC LIMIT 6"
    ).fetchall()]

    now_dt = datetime.now(timezone.utc)
    loops = []
    for r in con.execute("SELECT * FROM nodes ORDER BY updated DESC").fetchall():
        d = _node_dict(r)
        if not _is_open_loop(d):
            continue
        due_dt = _parse_dt(d["due"]) if d["due"] else None
        d["_overdue"] = bool(due_dt and due_dt < now_dt)
        loops.append(d)
    # overdue first, then whatever was touched most recently
    loops.sort(key=lambda d: (not d["_overdue"], d.get("updated") or ""), reverse=True)
    loops.sort(key=lambda d: not d["_overdue"])

    used = 0
    def build(d, extra=""):
        line = {"id": d["id"], "type": d["type"], "title": d["title"][:96]}
        if d.get("status"):
            line["status"] = d["status"]
        p = d["props"] or {}
        for k in ("priority", "jira", "pr", "waiting_on"):
            if p.get(k):
                line[k] = p[k]
        if extra:
            line["note"] = extra
        return line

    def fits(line, cap):
        """Append only if it stays inside the cap — the budget is a promise."""
        nonlocal used
        cost = len(json.dumps(line))
        if used + cost > cap:
            return False
        used += cost
        return True

    brief = {"ok": True, "budget": budget,
             "focus": [], "parked": [], "open_loops": []}
    for d in focus:
        line = build(d)
        if not fits(line, int(budget * 0.5)):
            break
        brief["focus"].append(line)
    for d in parked:
        line = build(d, f"{(d['props'] or {}).get('chars', '?')} chars — unpark --id {d['id']}")
        if not fits(line, int(budget * 0.75)):
            break
        brief["parked"].append(line)
    for d in loops:
        line = build(d)
        if not fits(line, budget):
            break
        brief["open_loops"].append(line)
    brief["chars"] = used
    brief["counts"] = {"focus": len(brief["focus"]), "parked": len(parked),
                       "open_loops": len(loops)}
    brief["hint"] = ("clear the window freely — detail lives in the brain. "
                     "Deepen any line with: recall '<cue>' · get-node --id · unpark --id")
    out(brief)


def cmd_park(args):
    """Move a blob of context OUT of the conversation and into the brain.

    Reads the content from --body or stdin, stores it, and returns a DELIBERATELY
    TINY receipt — id, size, and the first line. That is the whole point: once the
    content is in the graph the model can drop it from working context and fetch it
    back with `unpark` (or `get-node`) only if it is actually needed again.

    Nothing is summarised or lossy here; the text is stored verbatim.
    """
    con = connect()
    body = args.body
    if body is None:
        body = sys.stdin.read() if not sys.stdin.isatty() else ""
    body = body or ""
    if not body.strip():
        out({"ok": False, "error": "nothing to park (pass --body or pipe stdin)"}); return
    aid = None
    if getattr(args, "area", None):
        aid, err = _resolve_area(con, args.area)
        if err:
            out(err); return
    lines = body.count("\n") + 1
    first = next((l.strip() for l in body.splitlines() if l.strip()), "")
    title = args.title or f"Parked context — {first[:70]}"
    props = {"kind": "parked", "chars": len(body), "lines": lines,
             "parked_at": now(), "source": args.source or "conversation"}
    res = api_create_node(con, "parked", title, body, props, "", "",
                          getattr(args, "realm", None), args.id)
    if not res.get("ok"):
        out(res); return
    nid = res["node"]["id"]
    if aid:
        api_link(con, nid, aid, AREA_REL)
    for ref in (args.about or []):
        api_link(con, nid, ref, "about")
    # The receipt is the product: small enough that parking is a net saving even
    # for a few hundred chars.
    out({"ok": True, "parked": nid, "chars": len(body), "lines": lines,
         "first_line": first[:100], "area": aid,
         "hint": f"drop it from context; read it back with: unpark --id {nid}"})


def cmd_unpark(args):
    """Read parked context back — the whole thing, or a slice of it."""
    con = connect()
    row = con.execute("SELECT * FROM nodes WHERE id=?", (args.id,)).fetchone()
    if not row:
        out({"ok": False, "error": f"no node {args.id}"}); return
    d = _node_dict(row)
    body = d["body"] or ""
    start = max(0, int(args.offset or 0))
    if args.chars:
        chunk = body[start:start + int(args.chars)]
    else:
        chunk = body[start:]
    out({"ok": True, "id": d["id"], "title": d["title"], "total_chars": len(body),
         "offset": start, "returned": len(chunk),
         "more": start + len(chunk) < len(body), "body": chunk})


def _repair_digest_edges(con):
    """Point captures AT their digest, not the other way round.

    `part-of` reads "src is part of dst", so a capture belongs to its day's
    digest. Earlier consolidation runs wrote it backwards (digest part-of capture),
    which meant a capture never led to the summary that stands in for it — so
    recall spread outward into every raw capture instead of stopping at the digest.
    """
    bad = con.execute(
        "SELECT src, dst FROM edges WHERE rel='part-of' AND src LIKE 'note_digest_%'"
    ).fetchall()
    fixed = 0
    for r in bad:
        digest, capture = r["src"], r["dst"]
        con.execute("DELETE FROM edges WHERE src=? AND dst=? AND rel='part-of'",
                    (digest, capture))
        con.execute("INSERT OR IGNORE INTO edges(src,dst,rel,props,created)"
                    " VALUES(?,?,?,?,?)", (capture, digest, "part-of", "{}", now()))
        fixed += 1
    if fixed:
        con.commit()
    return fixed


def cmd_maintain(args):
    """Brain-side upkeep: consolidate, repair, and report what it saved.

    NOT the same thing as clearing context — see `resume`/`park` for that. This
    only tidies the STORE so reads stay small as it grows.

    Safe to run on a schedule and safe to run twice. NON-DESTRUCTIVE (§2a) —
    it consolidates and re-links, it never deletes. What it reduces is the SIZE OF
    A READ: once a day's captures hang off one digest, recall surfaces the digest
    instead of walking into every raw turn.
    """
    con = connect()
    keep = int(args.keep_days if args.keep_days is not None else 2)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep)).date().isoformat()

    before_rows = con.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(LENGTH(COALESCE(body,''))),0) c FROM nodes"
        " WHERE type='capture' AND substr(created,1,10)<=?"
        " AND json_extract(props,'$.consolidated_into') IS NULL", (cutoff,)).fetchone()

    # consolidate every day at or before the cutoff, one digest per day
    days = [r["d"] for r in con.execute(
        "SELECT DISTINCT substr(created,1,10) d FROM nodes WHERE type='capture'"
        " AND substr(created,1,10)<=? ORDER BY d", (cutoff,)).fetchall()]

    if getattr(args, "dry_run", False):
        # Everything below writes — the digests, the edge repair, the props
        # stamp — so the preview stops here and reports the plan instead. The
        # reversed-edge repair is counted, not performed.
        plan = consolidation_plan(con, days)
        reversible = con.execute(
            "SELECT COUNT(*) n FROM edges WHERE rel='part-of' AND src LIKE 'note_digest_%'"
        ).fetchone()["n"]
        out({"ok": True, "dry_run": True, "destructive": False,
             "keep_days": keep, "cutoff": cutoff,
             "reversed_edges_to_repair": reversible,
             "still_loose": before_rows["n"], **plan,
             "note": "nothing was written"})
        return

    backup = None
    if not getattr(args, "no_backup", False):
        backup = backup_db("maintain")
        prune_backups()

    repaired = _repair_digest_edges(con)
    digested = []
    for day in days:
        caps = [_node_dict(r) for r in con.execute(
            "SELECT * FROM nodes WHERE type='capture' AND substr(created,1,10)=?"
            " ORDER BY created", (day,)).fetchall()]
        if not caps:
            continue
        asked = [c for c in caps if c["props"].get("role") == "user"]
        did = f"note_digest_{day}"
        lines = [f"- {re.sub(chr(92)+'s+', ' ', c['title']).strip()[:110]}" for c in asked]
        body = (f"Digest of {len(caps)} captures on {day} ({len(asked)} prompts). "
                f"Originals kept and linked; read one with get-node.\n\n"
                + "\n".join(lines[:40]))
        props = {"kind": "capture-digest", "day": day, "captures": len(caps),
                 "prompts": len(asked), "consolidated_at": now()}
        if con.execute("SELECT 1 FROM nodes WHERE id=?", (did,)).fetchone():
            api_update_node(con, did, title=f"Digest — {day}", body=body,
                            props_patch=props)
        else:
            api_create_node(con, "note", f"Digest — {day}", body, props,
                            realm=DEFAULT_REALM, node_id=did)
        newly = 0
        for c in caps:
            # correct direction: the capture is part of the digest
            api_link(con, c["id"], did, "part-of")
            if not c["props"].get("consolidated_into"):
                newly += 1
            cp = dict(c["props"]); cp["consolidated_into"] = did
            con.execute("UPDATE nodes SET props=? WHERE id=?",
                        (json.dumps(cp), c["id"]))
        con.commit()
        digested.append({"day": day, "digest": did, "captures": len(caps),
                         "newly_consolidated": newly})

    after = con.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(LENGTH(COALESCE(body,''))),0) c FROM nodes"
        " WHERE type='capture' AND substr(created,1,10)<=?"
        " AND json_extract(props,'$.consolidated_into') IS NULL", (cutoff,)).fetchone()
    # The standing saving, not just this run's delta: everything a digest now
    # stands in for, versus the size of the digests themselves.
    behind = con.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(LENGTH(COALESCE(title,''))+LENGTH(COALESCE(body,''))),0) c"
        " FROM nodes WHERE type='capture'"
        " AND json_extract(props,'$.consolidated_into') IS NOT NULL").fetchone()
    digest_chars = con.execute(
        "SELECT COALESCE(SUM(LENGTH(COALESCE(body,''))),0) c FROM nodes"
        " WHERE id LIKE 'note_digest_%'").fetchone()["c"]

    _after_write(con, nodes=[d["digest"] for d in digested])
    out({"ok": True, "destructive": False, "keep_days": keep, "cutoff": cutoff,
         "backup": backup,
         "reversed_edges_repaired": repaired,
         "days_consolidated": len(digested), "digests": digested,
         "newly_consolidated": before_rows["n"] - after["n"],
         "still_loose": after["n"],
         "captures_behind_a_digest": behind["n"],
         "chars_they_hold": behind["c"],
         "digest_chars_standing_in": digest_chars,
         "read_reduction": (f"{100 * (1 - digest_chars / behind['c']):.0f}%"
                            if behind["c"] else "n/a"),
         "note": ("captures are kept and searchable; what shrinks is the read — "
                  "recall stops at the digest instead of every raw turn")})


def consolidation_plan(con, days: list) -> dict:
    """What a consolidation run over `days` WOULD do, computed without writing.

    The preview and the run must not be two implementations of the same
    arithmetic — a preview that is merely a good guess is worse than none, since
    it is trusted exactly when it is wrong. So this reads the rows, and the run
    below consolidates the days this returns.
    """
    plan, totals = [], {"captures": 0, "new_digests": 0, "updated_digests": 0,
                        "newly_consolidated": 0, "chars": 0}
    for day in days:
        caps = [_node_dict(r) for r in con.execute(
            "SELECT * FROM nodes WHERE type='capture' AND substr(created,1,10)=?"
            " ORDER BY created", (day,)).fetchall()]
        if not caps:
            continue
        did = f"note_digest_{day}"
        exists = bool(con.execute("SELECT 1 FROM nodes WHERE id=?", (did,)).fetchone())
        loose = [c for c in caps if not c["props"].get("consolidated_into")]
        chars = sum(len(c.get("title") or "") + len(c.get("body") or "") for c in caps)
        plan.append({
            "day": day, "digest": did,
            "digest_exists": exists,
            "action": "update" if exists else "create",
            "captures": len(caps),
            "prompts": sum(1 for c in caps if c["props"].get("role") == "user"),
            "newly_consolidated": len(loose),
            "already_consolidated": len(caps) - len(loose),
            "chars_behind_it": chars,
        })
        totals["captures"] += len(caps)
        totals["newly_consolidated"] += len(loose)
        totals["chars"] += chars
        totals["updated_digests" if exists else "new_digests"] += 1
    return {"days": plan, "totals": totals}


def capture_days(con, day: str = "", older_than=None) -> list:
    """The days a consolidation run would touch, under the same filter the run
    uses. Separated out so the preview cannot drift from the run."""
    where, params = "type='capture'", []
    if day:
        where += " AND substr(created,1,10)=?"; params.append(day)
    elif older_than is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(older_than))
                  ).date().isoformat()
        where += " AND substr(created,1,10)<=?"; params.append(cutoff)
    return [r["d"] for r in con.execute(
        f"SELECT DISTINCT substr(created,1,10) d FROM nodes WHERE {where}"
        " ORDER BY d", params).fetchall()]


def _decay_half_lives(overrides=None) -> dict:
    """Half-lives by kind/type: defaults < `decay_half_lives` setting (JSON, env
    or settings.json) < overrides."""
    raw = setting("decay_half_lives", "") or settings().get("decay_half_lives")
    return _decay.half_lives(overrides, raw)


def cmd_decay(args):
    """Per-type decay tiers (decay.py): expire captures past their half-life into
    one weekly digest note, and list — with --apply, close as `dropped` — open
    tasks older than 14 d whose title is really a pasted prompt. DRY-RUN by
    default; nothing is ever deleted."""
    con = connect()
    p = _decay.plan(con, table=_decay_half_lives(), stale_days=args.stale_days)
    res = {k: v for k, v in p.items() if not k.startswith("_")}
    if not args.apply:
        out({"ok": True, "dry_run": True, "destructive": False, **res,
             "hint": "re-run with --apply to write; list is capped, "
                     "see `stale_tasks.flagged` for the full count",
             "note": "nothing was written"})
        return
    backup = None
    if not args.no_backup:
        backup = backup_db("decay")
        prune_backups()
    done = _decay.apply(con, p, sys.modules[__name__])
    out({"ok": True, "dry_run": False, "destructive": False, "backup": backup,
         **res, "applied": done})


def cmd_consolidate(args):
    """Sleep-style consolidation: episodic captures -> one semantic digest per
    day, linked back to the originals. NON-DESTRUCTIVE — every capture is kept;
    the digest is what recall surfaces, so reads get cheaper without losing raw
    detail (CONVENTIONS §2a)."""
    con = connect()
    days = capture_days(con, args.day or "", args.older_than)
    if not days:
        out({"ok": True, "digests": [], "note": "no captures matched"}); return

    plan = consolidation_plan(con, days)
    if getattr(args, "dry_run", False):
        # Reads only. Nothing below this line runs, which is the point: the
        # answer to "what will this change?" must not change anything.
        out({"ok": True, "dry_run": True, "destructive": False, **plan,
             "note": "nothing was written"})
        return

    # A snapshot before the first write, so a run you regret is one file copy
    # away from undone. Skipped on --no-backup for the daily hook, which runs
    # unattended and would otherwise leave a snapshot behind every morning.
    backup = None
    if not getattr(args, "no_backup", False):
        backup = backup_db("consolidate")
        prune_backups()

    by_day = {}
    for r in con.execute(
            "SELECT * FROM nodes WHERE type='capture' AND substr(created,1,10) IN "
            "(" + ",".join("?" * len(days)) + ") ORDER BY created", days).fetchall():
        by_day.setdefault(r["created"][:10], []).append(_node_dict(r))

    digests = []
    for day, caps in sorted(by_day.items()):
        asked = [c for c in caps if (c["props"].get("role") == "user")]
        lines = []
        for c in asked:
            t = re.sub(r"\s+", " ", c["title"]).strip()
            lines.append(f"- {t[:110]}")
        did = f"note_digest_{day}"
        body = (f"Digest of {len(caps)} captures on {day} "
                f"({len(asked)} prompts). Originals kept and linked.\n\n"
                + "\n".join(lines[:40]))
        props = {"kind": "capture-digest", "day": day, "captures": len(caps),
                 "prompts": len(asked), "consolidated_at": now()}
        if con.execute("SELECT 1 FROM nodes WHERE id=?", (did,)).fetchone():
            api_update_node(con, did, title=f"Digest — {day}", body=body,
                            props_patch=props)
        else:
            api_create_node(con, "note", f"Digest — {day}", body, props,
                            realm=DEFAULT_REALM, node_id=did)
        linked = 0
        for c in caps:
            # "src is part of dst" — the CAPTURE belongs to the digest, not the
            # reverse. Backwards, recall never reached the digest from a capture.
            api_link(con, c["id"], did, "part-of")
            cp = dict(c["props"]); cp["consolidated_into"] = did
            con.execute("UPDATE nodes SET props=? WHERE id=?",
                        (json.dumps(cp), c["id"]))
            linked += 1
        con.commit()
        digests.append({"id": did, "day": day, "captures": linked,
                        "kept_originals": True})
    _after_write(con, nodes=[d["id"] for d in digests])
    out({"ok": True, "digests": digests, "destructive": False,
         "backup": backup, "planned": plan["totals"]})


# ---------------------------------------------------------------------------
# state awareness / open loops
# ---------------------------------------------------------------------------
# A node is an OPEN LOOP until its state says otherwise. State persists (stays
# the same) until explicitly changed via set-state / update-node.
CLOSED_STATES = {"done", "cancelled", "dropped", "closed", "answered", "resolved",
                 "merged", "shipped", "deployed", "declined"}
LOOP_TYPES = {"task", "subtask", "reminder", "pr", "email", "message"}


# --- external task boards (CONVENTIONS §2c) ---------------------------------
# A task another app has claimed carries `props.tracked_by`. The brain keeps
# capturing and remembering it, but stops nagging about it: that board owns the
# open/closed decision now, and reports it back with `complete` or
# `update-node --status cancelled`. Date views keep tracked tasks, so a due date
# the board writes back still shows on the agenda.
TRACKED_PROP = "tracked_by"


def tracked_by(d: dict) -> str:
    """The name of the board that claimed this node, or ""."""
    props = d.get("props") if isinstance(d, dict) else None
    val = (props or {}).get(TRACKED_PROP)
    # Tracked only when the claim is a dict with a non-empty system; strings,
    # {} and null count as untracked, the same as the SQL filters in cmd_list.
    if isinstance(val, dict):
        return str(val.get("system") or "").strip()
    return ""


def api_track(con, node_id, system, ref, url=""):
    """Claim a task for an external board. Idempotent: the same claim again
    changes nothing but `updated`; a different board replaces the claim."""
    system = (system or "").strip()
    if not system:
        return {"ok": False, "error": "system is required"}
    row = con.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
    if not row:
        return {"ok": False, "error": f"no node {node_id}"}
    cur = _node_dict(row)
    prev = cur["props"].get(TRACKED_PROP)
    prev = prev if isinstance(prev, dict) else {}
    since = prev.get("since") if prev.get("system") == system and prev.get("since") else now()
    claim = {"system": system, "ref": str(ref or ""), "url": str(url or ""), "since": since}
    return api_update_node(con, node_id, props_patch={TRACKED_PROP: claim})


def api_untrack(con, node_id):
    """Release a task from its board. The task is an ordinary open loop again."""
    row = con.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
    if not row:
        return {"ok": False, "error": f"no node {node_id}"}
    cur = _node_dict(row)
    if TRACKED_PROP not in cur["props"]:
        return {"ok": True, "node": cur, "sync": None}   # never claimed: nothing to release
    props = {k: v for k, v in cur["props"].items() if k != TRACKED_PROP}
    con.execute("UPDATE nodes SET props=?, updated=? WHERE id=?",
                (json.dumps(props), now(), node_id))
    con.commit()
    sync = _after_write(con, nodes=[node_id])
    row = con.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
    return {"ok": True, "node": _node_dict(row), "sync": sync}


def cmd_track(args):
    con = connect()
    out(api_track(con, args.id, args.system, args.ref, args.url or ""))


def cmd_untrack(args):
    con = connect()
    out(api_untrack(con, args.id))


def _is_open_loop(d):
    """True if node d is still an open loop. Tasks/trackables are open unless
    their status/state is a closed one; other nodes are open only if they carry
    a non-closed props.state (so notes/people aren't loops by default)."""
    status = (d.get("status") or "").lower()
    state = str(d["props"].get("state", "")).lower()
    if status in CLOSED_STATES or state in CLOSED_STATES:
        return False
    if d["type"] in LOOP_TYPES:
        return True
    return bool(state)  # any other node is a loop only if it has an open state


def cmd_set_state(args):
    """Change a node's state and stamp WHEN it changed (state_since). For tasks,
    also mirror it onto status. This is how a loop moves; until then it holds."""
    con = connect()
    row = con.execute("SELECT * FROM nodes WHERE id=?", (args.id,)).fetchone()
    if not row:
        out({"ok": False, "error": f"no node {args.id}"}); return
    d = _node_dict(row)
    p = d["props"]
    p["state"] = args.state
    p["state_since"] = now()
    if args.waiting_on is not None:
        p["waiting_on"] = args.waiting_on
    status = d["status"]
    if d["type"] in ("task", "subtask") and args.state in (
            "open", "doing", "blocked", "done", "cancelled"):
        status = args.state
    con.execute("UPDATE nodes SET status=?, props=?, updated=? WHERE id=?",
                (status, json.dumps(p), now(), args.id))
    con.commit()
    sync = _after_write(con, nodes=[args.id])
    row = con.execute("SELECT * FROM nodes WHERE id=?", (args.id,)).fetchone()
    out({"ok": True, "node": _node_dict(row), "sync": sync})


def cmd_open_loops(args):
    """Everything still awaiting action, oldest-first. The state-aware view:
    open tasks, tracked PRs/emails/messages, anything with a non-closed state.
    `age_days` counts from state_since (when it entered its state) or updated."""
    con = connect()
    now_dt = datetime.now(timezone.utc)
    rows = con.execute("SELECT * FROM nodes").fetchall()
    loops = []
    tracked = {}
    for r in rows:
        d = _node_dict(r)
        if not _is_open_loop(d):
            continue
        # A loop an external board holds is that board's to nag about (§2c).
        # Counted here so the brief can say "7 tracked in <board>" in one line.
        board = tracked_by(d)
        if board and not getattr(args, "include_tracked", False):
            tracked[board] = tracked.get(board, 0) + 1
            continue
        since = d["props"].get("state_since") or d["updated"]
        since_dt = _parse_dt(since) or now_dt
        d["age_days"] = (now_dt - since_dt).days
        d["kind"] = d["type"]
        d["waiting_on"] = d["props"].get("waiting_on", "")
        due_dt = _parse_dt(d["due"]) if d["due"] else None
        d["overdue"] = bool(due_dt and due_dt < now_dt)
        d["stale"] = d["age_days"] >= args.stale_days
        loops.append(d)
    # oldest first; overdue floats up
    loops.sort(key=lambda x: (not x["overdue"], -x["age_days"]))
    by_kind, by_prio = {}, {}
    for x in loops:
        by_kind[x["kind"]] = by_kind.get(x["kind"], 0) + 1
        pr = x["props"].get("priority", "--")
        by_prio[pr] = by_prio.get(pr, 0) + 1
    out({"ok": True, "count": len(loops),
         "stale": sum(1 for x in loops if x["stale"]),
         "overdue": sum(1 for x in loops if x["overdue"]),
         "by_kind": by_kind, "by_priority": by_prio,
         "tracked": tracked,
         "results": loops[: args.limit]})


# ---------------------------------------------------------------------------
# arg parsing
# ---------------------------------------------------------------------------
def cmd_flush(args):
    con = connect()
    pending = con.execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"]
    res = _flush(con)
    res["was_pending"] = pending
    out(res)


def cmd_neo4j_status(args):
    con = connect()
    cfg = _neo4j_cfg()
    pending = con.execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"]
    res = {"ok": True, "configured": bool(cfg["url"] and cfg["password"]),
           "url": cfg["url"], "up": _neo4j_up(), "pending_ops": pending,
           "machine": _sync_get(con, "machine_id"),
           "pulled_to": int(_sync_get(con, "pull_seq", "0") or 0),
           "last_sync": _sync_get(con, "last_sync")}
    # `up` only proves the root URL answers a GET. It stayed true for a whole day
    # while every flush timed out, so report whether the tx endpoint actually
    # works too — that is the thing sync depends on.
    if res["configured"]:
        ok, err = _neo4j_call([{"statement": "RETURN 1"}], timeout=10)
        res["writable"] = ok
        if not ok:
            res["error"] = err
    out(res)


def cmd_sync(args):
    """Two-way sync with the master: pull what other machines wrote, then push."""
    import brain_sync
    out(brain_sync.sync_now())


def cmd_sections(args):
    con = connect()
    rows = con.execute(
        "SELECT COALESCE(NULLIF(realm,''),?) AS realm, COUNT(*) c "
        "FROM nodes GROUP BY realm ORDER BY c DESC", (DEFAULT_REALM,)).fetchall()
    out({"ok": True, "default": DEFAULT_REALM,
         "sections": {r["realm"]: r["c"] for r in rows}})


def api_rename_realm(con, old: str, new: str) -> dict:
    """Move every node in realm `old` to `new`, and follow it in the settings.

    Renaming onto a realm that already has nodes merges the two: the result says
    so rather than refusing, because "fold `scratch` into `work`" is a rename too.
    A node with an empty realm counts as the current default, so renaming the
    default takes those along. `updated` is left alone on purpose: moving a
    section is not an edit to what a node says, and it must not make five
    thousand old nodes look fresh to recall. Snapshots the graph first."""
    old, new = (old or "").strip().lower(), (new or "").strip().lower()
    if not old or not new:
        return {"ok": False, "error": "both the old and the new realm name are required"}
    if new != slugify(new):
        return {"ok": False, "error": "realm must be lowercase letters, digits and dashes"}
    if old == new:
        return {"ok": False, "error": "the new name is the same as the old one"}
    cur = current_realm()
    where = "realm=?" + (" OR realm=''" if old == cur else "")
    ids = [r["id"] for r in con.execute(f"SELECT id FROM nodes WHERE {where}", (old,))]
    if not ids and old != cur:
        return {"ok": False, "error": f"no realm named {old!r}"}
    existing = con.execute("SELECT COUNT(*) c FROM nodes WHERE realm=?", (new,)).fetchone()["c"]
    backup = backup_db("rename-realm") if ids else None
    con.execute(f"UPDATE nodes SET realm=? WHERE {where}", (new, old))
    res = {"ok": True, "from": old, "to": new, "moved": len(ids),
           "merged_into_existing": existing > 0, "backup": backup}
    if old == cur:
        if _env("REALM"):
            res["warning"] = ("the default realm comes from $MEMORY_LEDGER_REALM / "
                              f"$SECOND_BRAIN_REALM; set it to {new!r} there too")
        else:
            save_settings({"realm": new})
        global DEFAULT_REALM
        DEFAULT_REALM = setting("realm", REALM_FALLBACK)
        res["default"] = DEFAULT_REALM
    res["sync"] = _after_write(con, nodes=ids)
    return res


def cmd_rename_realm(args):
    out(api_rename_realm(connect(), args.old, args.new))


def _areas(con):
    """Every area node with its member count, biggest first."""
    return con.execute(
        "SELECT n.id, n.title, n.realm, "
        " (SELECT COUNT(*) FROM edges e WHERE e.dst=n.id AND e.rel=?) AS members "
        "FROM nodes n WHERE n.type=? ORDER BY members DESC, n.id",
        (AREA_REL, AREA_TYPE)).fetchall()


def cmd_areas(args):
    con = connect()
    rows = _areas(con)
    out({"ok": True, "count": len(rows), "rel": AREA_REL,
         "areas": [{"id": r["id"], "title": r["title"],
                    "realm": r["realm"], "members": r["members"]} for r in rows]})


def cmd_add_area(args):
    """Create a domain area (idempotent on --id)."""
    con = connect()
    aid = args.id or f"hub_{slugify(args.title)}"
    row = con.execute("SELECT * FROM nodes WHERE id=?", (aid,)).fetchone()
    if row:
        out({"ok": True, "existed": True, "node": _node_dict(row)})
        return
    out(api_create_node(con, AREA_TYPE, args.title, args.body or "",
                        {"domain": args.domain or slugify(args.title)},
                        "", "", getattr(args, "realm", None), aid))


# The hub the genome is filed under is not a user choice — it is a constant of
# the engine — so every writer of a rule may create it. Before this, authoring a
# rule on a brain that had never run `add-area` failed with an error about a hub
# the person had no reason to know existed.
GENOME_HUBS = {
    AREA_CONVENTION: "Conventions — how this brain behaves",
    AREA_GENOME: "Genome — how this brain is built",
}


def _ensure_area(con, area_id: str, realm="shared"):
    """The area id, creating it first if this brain has never held it."""
    if not con.execute("SELECT 1 FROM nodes WHERE id=? AND type=?",
                       (area_id, AREA_TYPE)).fetchone():
        api_create_node(con, AREA_TYPE, GENOME_HUBS.get(area_id, area_id),
                        "", {"domain": area_id[len("hub_"):]}, "", "",
                        realm, area_id)
    return area_id


def _resolve_area(con, area):
    """An area id (or bare domain name) -> the area node id, or an error dict."""
    for candidate in (area, f"hub_{slugify(area)}"):
        row = con.execute("SELECT id FROM nodes WHERE id=? AND type=?",
                          (candidate, AREA_TYPE)).fetchone()
        if row:
            return row["id"], None
    known = ", ".join(r["id"] for r in _areas(con)) or "none yet"
    return None, {"ok": False,
                  "error": f"no area {area!r} (known: {known}) — create it with add-area"}


def _api_surface() -> list:
    """Introspect this engine's own CLI. Derived, never hand-copied, so the genome
    cannot drift from the code the way a hand-written command list would."""
    parser = build_parser()
    surface = []
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, sp in action.choices.items():
            flags = []
            for a in sp._actions:
                if not a.option_strings:
                    flags.append(f"<{a.dest}>")
                    continue
                if a.dest == "help":
                    continue
                flags.append(a.option_strings[0] + ("" if a.nargs == 0 else " V"))
            surface.append({"command": name, "flags": flags,
                            "help": (sp.description or "").strip()})
    return sorted(surface, key=lambda d: d["command"])


def _db_schema(con) -> list:
    rows = con.execute(
        "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"
        " AND name NOT LIKE 'sqlite_%' ORDER BY type DESC, name").fetchall()
    return [{"name": r["name"], "sql": " ".join((r["sql"] or "").split())} for r in rows]


_GENOME_SPECS = {
    "spec_rebuild": ("How to rebuild the Claude layer from this database alone", """
If the plugin is gone and only `graph.db` survives, this is the restore path. Any
AI with shell access and this file can reconstitute the interaction layer.

1. **Find the brain.** `$SECOND_BRAIN_DB` -> `$SECOND_BRAIN_HOME/graph.db` ->
   `~/.second_brain/graph.db`. The file IS the brain: schema, content, conventions,
   skills and this blueprint are all inside it.
2. **Read the API surface.** `spec_api_surface` lists every engine command and flag,
   introspected from the code at snapshot time. If `scripts/graph.py` is also gone,
   `spec_db_schema` has the full DDL — the tables are plain SQLite (`nodes`, `edges`,
   `nodes_fts`, `outbox`) and can be driven with SQL directly.
3. **Load the operating rules ON DEMAND, not all at once.** `match-rules "<what you
   are about to do>"` returns only the sections that apply, inside a char budget.
   Do NOT read the whole conventions text — that is the cost this design exists to
   remove. `list --area convention` enumerates them if you need the full set.
4. **Load the skills.** They are `skill` nodes, not files: `match-skills "<situation>"`
   then `get-node --id` for the procedure. `ingest-skills` re-imports markdown skills
   if a plugin checkout reappears.
5. **Re-create the always-on capture.** `spec_hooks` describes the three hooks, what
   fires them and what each stores. They are pure Python/SQL and can be rewritten
   from that description against the schema in `spec_db_schema`.
6. **Reconnect the mirror, only if one is configured.** `spec_sync` covers the
   optional graph mirror, the outbox queue and reconciliation. Skip it otherwise —
   the brain is fully functional as a single local file, which is the default.
7. **Re-seed the base layer only if the conventions are missing.** With a plugin
   checkout present, `ingest-conventions` reloads the shipped text — it KEEPS every
   user-authored or user-edited rule, so restoring never undoes the user's own brain.

Precedence when rebuilding: a `user` rule outranks a `learned` one outranks the
`plugin` default. A rule with `status=superseded` is history — do not apply it.
"""),
    "spec_hooks": ("How the always-on capture hooks work", """
Three hooks make capture automatic, with no model call. All are pure Python + SQL.

| Hook | Script | Fires | Stores | Injects into context |
|---|---|---|---|---|
| `UserPromptSubmit` | `hooks/capture_prompt.py` | every prompt | a `capture` node; auto-creates a `task` for an explicit to-do | ~197 chars |
| `Stop` | `hooks/capture_answer.py` | after each answer | a `capture` node summarising the answer | nothing |
| `SessionStart` | open-loops brief | session start | nothing | ~850 chars, capped at 6 loops so it cannot grow with the graph |

Consequences a rebuild must preserve:
- The turn is ALREADY captured. Never spend tokens re-storing a prompt or answer;
  add only the semantic nodes the regex could not infer (a `decision`, a `person`,
  a `note`).
- "Token-free" means no model call for deciding or storing. It is not literally
  zero: the two injected strings above are real context. Say "no model call" when
  that is what is meant.
- `SessionStart` also runs the once-a-day consolidation, guarded by a
  `~/.second_brain/.last_consolidate` stamp. Opt out with the `consolidate`
  setting (Settings in the UI) or `$MEMORY_LEDGER_NO_CONSOLIDATE`. It runs
  `--no-backup`: snapshots are for the runs a person triggers.
- With the OPT-IN `learned_rules` setting on (default off), `SessionStart` also runs
  `graph.py propose-rules` once a day, detached and silent (`.last_learned_rules`
  stamp). It only writes `rule-proposal` nodes; a person accepts them with
  `accept-rule`. Off, nothing runs.
"""),
    "spec_storage": ("Where the brain lives and how it is addressed", """
The brain is GLOBAL and lives outside any repo, so every project on the machine
shares one brain. It is never committed.

```
~/.second_brain/
  graph.db          the whole brain in one portable SQLite file (content + skills + genome)
  backups/          snapshots taken before a consolidation run, kept BACKUP_KEEP_DAYS
  learned_skills/   optional local SKILL.md exports; never the source of truth
  exports/          export-memory output
  neo4j.json        optional graph-mirror config (secret)
  settings.json     preferences shared by the CLI, hooks, cron and the web UI
  ui_prefs.json     the browser's view state, only when the `ui_sync` setting is on
  .last_consolidate stamp for the daily consolidation
```

`consolidate` and `maintain` take `--dry-run` (report the plan, write nothing) and
snapshot the graph before their first write unless given `--no-backup`. `backups`
lists, takes (`--new`) and expires (`--prune`) them; restore by copying one back
over `graph.db`.

Path resolution: `$SECOND_BRAIN_DB` (explicit file) -> `$SECOND_BRAIN_HOME/graph.db`
-> `~/.second_brain/graph.db`. Copying `graph.db` moves the brain; copying the home
directory moves brain + learned skills. Identical behaviour in Claude Code and the
Claude app. The file is the whole product — no server, no account, no network.

Two orthogonal groupings: `realm` is which section of life (`default`, `work`, `personal`,
`shared`, …); an `area` (`hub_*`, linked with `part-of`) is which domain of meaning.
"""),
    "spec_sync": ("Optional graph mirror and reconciliation", """
The brain is one local SQLite file and needs no network. OPTIONALLY, a graph mirror
can be configured (`~/.second_brain/neo4j.json` or `SECOND_BRAIN_NEO4J_*`) so several
machines share one brain — the plugin ships no default and connects to nothing unless
configured. Every write lands locally first, then mirrors. If the mirror is
unreachable the op is queued in the `outbox` table and pushed when it returns, so
losing it never blocks or loses a write. Use TLS if you configure one: credentials
and the whole graph cross the network on every write.

- `neo4j-status` — configured / reachable / pending op count.
- `flush` — drain the outbox.
- `scripts/brain_sync.py` — full bulk reconcile; `--verify` compares local and
  master, `--pull [--replace]` rebuilds the brain from the master.
- The mirror is complete: SQLite triggers queue every change to nodes, edges and
  `MIRROR_TABLES` (as :MirrorRow), and the home's own files go as :MirrorFile.
- `sync` — two-way: pull what any other writer put on the master (stamped
  `_seq`/`_origin`, deletes as :Tombstone), then push. The server runs it every
  minute. This is the road to retiring SQLite: once neo4j is fed directly, SQLite
  becomes a cache of it. Every direct write to neo4j must carry the same stamp.
- `$MEMORY_LEDGER_NO_AUTOSYNC=1`, or the `autosync` setting turned off — queue
  without per-op push (bulk loads), then flush. The env var is the negative and
  outranks the file, like every other switch in the engine.

Node `type` is a PROPERTY in neo4j, not a label, so changing a node's type needs no
label surgery — re-emitting the node through the API is enough.
"""),
}


def cmd_genome(args):
    """Emit (and optionally refresh) the brain's own build blueprint.

    `--refresh` regenerates the derived specs — the API surface and DB schema come
    from introspection, so the genome tracks the code instead of drifting from it.
    """
    con = connect()
    aid, err = _resolve_area(con, AREA_GENOME)
    if err:
        out(err); return
    ts = now()
    if args.refresh:
        derived = {
            "spec_api_surface": ("Engine command surface (introspected)",
                                 json.dumps(_api_surface(), indent=1)),
            "spec_db_schema": ("Database schema (DDL)",
                               json.dumps(_db_schema(con), indent=1)),
        }
        touched = []
        for nid, (title, body) in {**_GENOME_SPECS, **derived}.items():
            props = {"source": "plugin", "refreshed_at": ts,
                     "derived": nid in derived}
            row = con.execute("SELECT * FROM nodes WHERE id=?", (nid,)).fetchone()
            if row:
                cur = _node_dict(row)
                if cur["props"].get("user_edited") and nid not in derived:
                    continue
                con.execute("UPDATE nodes SET title=?, body=?, props=?, updated=? WHERE id=?",
                            (title, body.strip(), json.dumps({**cur["props"], **props}), ts, nid))
            else:
                con.execute(
                    "INSERT INTO nodes(id,type,title,body,props,status,due,realm,created,updated)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (nid, SPEC_TYPE, title, body.strip(), json.dumps(props),
                     "active", "", "shared", ts, ts))
            con.execute("INSERT OR IGNORE INTO edges(src,dst,rel,props,created)"
                        " VALUES(?,?,?,?,?)", (nid, aid, AREA_REL, "{}", ts))
            touched.append(nid)
        con.commit()
        _after_write(con, nodes=touched)

    specs = con.execute(
        "SELECT * FROM nodes WHERE type=? AND id IN"
        " (SELECT src FROM edges WHERE dst=? AND rel=?) ORDER BY id", (SPEC_TYPE, aid, AREA_REL)
    ).fetchall()
    rules = con.execute(
        "SELECT id,title,status,props FROM nodes WHERE type=? ORDER BY id", (RULE_TYPE,)).fetchall()
    skills = con.execute("SELECT COUNT(*) c FROM nodes WHERE type='skill'").fetchone()["c"]
    payload = {
        "ok": True,
        "generated": ts,
        "db": str(db_path()),
        "specs": [{"id": r["id"], "title": r["title"]} for r in specs],
        "rules": len(rules),
        "skills": skills,
        "restore": "read spec_rebuild first, then match-rules '<task>' — never the whole conventions",
    }
    if args.out:
        d = Path(args.out).expanduser(); d.mkdir(parents=True, exist_ok=True)
        for r in specs:
            (d / f"{r['id']}.md").write_text(
                f"# {r['title']}\n\n{r['body']}\n", encoding="utf-8")
        rule_rows = con.execute(
            "SELECT id,title,body,status,props FROM nodes WHERE type=? ORDER BY id",
            (RULE_TYPE,)).fetchall()
        (d / "rules.md").write_text("\n\n".join(
            f"## {r['title']}\n\n_id: `{r['id']}` · status: {r['status'] or 'active'}_\n\n{r['body']}"
            for r in rule_rows), encoding="utf-8")
        (d / "genome.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
        payload["written_to"] = str(d)
    out(payload)


# ---------------------------------------------------------------------------
# traceability: the agent tree, ideas, and conditional discard (§2k)
#
# The brain already records WHAT was said (capture nodes). This records WHAT WAS
# CONSIDERED and WHY it was dropped — and, for the discards that were only ever
# about a missing condition, brings them back when the condition stops holding.
#
# Everything below is captured BY HOOK OR EXPLICIT CALL, never by asking a model
# to re-read a transcript: deterministic, zero tokens, zero latency.
# ---------------------------------------------------------------------------
IDEA_STATUSES = ("proposed", "selected", "discarded", "superseded", "revived")
# Only `conditional` is re-evaluated automatically. A `preference` discard comes
# back when someone asks for it; a `permanent` one never does.
DISCARD_TYPES = ("conditional", "preference", "permanent")
AGENT_OUTCOMES = ("completed", "abandoned", "failed")
CONDITION_OPS = (">=", "<=", "==", "!=", ">", "<", "exists", "not_exists")
# Ops that carry no right-hand side.
UNARY_OPS = ("exists", "not_exists")
# An unblocked item is not re-announced inside this window — a reconsider prompt
# that repeats every run is a prompt nobody reads (§2k, "notify, don't execute").
NOTIFY_QUIET_DAYS = 7
PROBE_TIMEOUT = 20


def _conv_id(session_id: str) -> str:
    """Deterministic, so conversation-start is a true upsert per session."""
    return "conversation_" + (slugify(session_id) or "adhoc")


def _gauge_id(key: str) -> str:
    return "gauge_" + (slugify(key) or "unnamed")


_COND_RE = re.compile(
    r"^\s*(?P<key>[A-Za-z0-9_.\-]+)\s*"
    r"(?P<op>>=|<=|==|!=|>|<|\bexists\b|\bnot_exists\b)\s*"
    r"(?P<val>.*?)\s*$")


def parse_condition(text: str) -> dict:
    """`"gpu.vram_gb >= 16"` -> {gauge, op, value}.

    The CLI takes the readable form; the stored form is always structured. Free
    text is what makes a discard un-recheckable, so it never reaches the store.
    Raises ValueError with a usable message — the caller turns it into `ok:false`.
    """
    m = _COND_RE.match(text or "")
    if not m:
        raise ValueError(
            f"cannot parse condition {text!r} — expected '<gauge.key> <op> <value>', "
            f"ops: {', '.join(CONDITION_OPS)}")
    op, val = m.group("op"), m.group("val").strip().strip("'\"")
    if op in UNARY_OPS:
        val = ""
    elif not val:
        raise ValueError(f"condition {text!r} has operator {op} but no value")
    return {"gauge": m.group("key"), "op": op, "value": val}


_TRUTHY = {"true", "yes", "y", "on", "1"}
_FALSY = {"false", "no", "n", "off", "0"}


def _coerce(raw):
    """Best-effort typing for a gauge value: number, bool, version tuple, or text.

    Returned as (kind, value) so the comparison never guesses twice. Version
    handling matters for `dependency.<name>.version` — `2.10.0` is above `2.9.0`
    and below it as a string.
    """
    s = str(raw if raw is not None else "").strip()
    if s == "":
        return ("empty", "")
    low = s.lower()
    if low in _TRUTHY:
        return ("bool", True)
    if low in _FALSY:
        return ("bool", False)
    try:
        return ("num", float(s))
    except ValueError:
        pass
    if re.fullmatch(r"v?\d+(\.\d+)+[A-Za-z0-9.\-+]*", s):
        head = re.match(r"v?(\d+(?:\.\d+)+)", s).group(1)
        return ("ver", tuple(int(p) for p in head.split(".")))
    return ("text", s)


def compare_values(current, op: str, required) -> bool:
    """Evaluate one condition. Unknown/unmeasured gauges are NOT satisfied —
    a blocked item stays blocked until something actually measures otherwise."""
    ck, cv = _coerce(current)
    if op == "exists":
        return ck != "empty"
    if op == "not_exists":
        return ck == "empty"
    if ck == "empty":
        return False
    rk, rv = _coerce(required)
    if ck != rk:                      # mixed kinds: fall back to text equality
        if op == "==":
            return str(current).strip() == str(required).strip()
        if op == "!=":
            return str(current).strip() != str(required).strip()
        return False
    if op == "==":
        return cv == rv
    if op == "!=":
        return cv != rv
    if ck == "bool":                  # ordering booleans is a caller mistake
        return False
    if op == ">=":
        return cv >= rv
    if op == "<=":
        return cv <= rv
    if op == ">":
        return cv > rv
    if op == "<":
        return cv < rv
    return False


# --- the agent tree ---------------------------------------------------------
def api_conversation(con, session_id: str, *, cwd="", owner="", title="") -> dict:
    """Get-or-create the conversation for a session. Idempotent by construction:
    the id is derived from the session id, so every hook in the session lands on
    the same node without a lookup round-trip."""
    cid = _conv_id(session_id)
    row = con.execute("SELECT * FROM nodes WHERE id=?", (cid,)).fetchone()
    if row:
        return {"ok": True, "node": _node_dict(row), "created": False}
    props = {"session_id": session_id, "started_at": now(),
             "owner": owner or _resolve_owner()}
    if cwd:
        props["cwd"] = cwd
    res = api_create_node(con, "conversation",
                          title or f"Session {session_id[:8] or 'adhoc'}",
                          "", props, "open", "", "", cid)
    res["created"] = res.get("ok", False)
    return res


def _parent_of(con, agent_id: str):
    r = con.execute(
        "SELECT dst FROM edges WHERE src=? AND rel=? LIMIT 1",
        (agent_id, AREA_REL)).fetchone()
    return r["dst"] if r else None


def api_agent_spawn(con, parent_id: str, role: str, *, model="", body="",
                    session_id="") -> dict:
    """One node in the tree, under EXACTLY one parent.

    A parent is mandatory and must be a conversation or another agent: an agent
    with no parent is a branch that cannot be walked, and the whole point of the
    tree is that a subagent's discarded alternative stays visible from the root
    instead of dying with its branch (§3.2 of the spec).
    """
    if not (role or "").strip():
        return {"ok": False, "error": "missing role"}
    prow = con.execute("SELECT * FROM nodes WHERE id=?", (parent_id,)).fetchone()
    if not prow:
        return {"ok": False, "error": f"no parent {parent_id}"}
    if prow["type"] not in ("conversation", "agent"):
        return {"ok": False,
                "error": f"parent {parent_id} is a {prow['type']}; an agent's "
                         f"parent must be a conversation or an agent"}
    props = {"role": role, "started_at": now(), "outcome": ""}
    if model:
        props["model"] = model
    if session_id:
        props["session_id"] = session_id
    depth = 0
    walk, seen = parent_id, set()
    while walk and walk not in seen:
        seen.add(walk)
        walk = _parent_of(con, walk)
        depth += 1
    props["depth"] = depth
    res = api_create_node(con, "agent", role, body or "", props, "open",
                          "", prow["realm"] or DEFAULT_REALM)
    if res.get("ok"):
        api_link(con, res["node"]["id"], parent_id, AREA_REL)
        res["parent"] = parent_id
    return res


def api_agent_finish(con, agent_id: str, outcome: str, note="") -> dict:
    """Record the outcome. An unfinished agent is indistinguishable from one that
    crashed, so every branch closes explicitly — `abandoned` is a real answer."""
    if outcome not in AGENT_OUTCOMES:
        return {"ok": False,
                "error": f"outcome must be one of {'|'.join(AGENT_OUTCOMES)}"}
    row = con.execute("SELECT * FROM nodes WHERE id=?", (agent_id,)).fetchone()
    if not row:
        return {"ok": False, "error": f"no node {agent_id}"}
    patch = {"outcome": outcome, "ended_at": now()}
    if note:
        patch["outcome_note"] = note
    return api_update_node(con, agent_id, status="done", props_patch=patch)


def api_conversation_end(con, session_id: str, reason="") -> dict:
    """Close the conversation and every agent still open under it.

    The MAIN agent reaching the end of a session finished its work; a SUBAGENT
    still open when the session ends did not — it was abandoned. Recording both
    as the same thing would make "abandoned" meaningless, which is the one thing
    the outcome field exists to say.
    """
    cid = _conv_id(session_id)
    if not con.execute("SELECT 1 FROM nodes WHERE id=?", (cid,)).fetchone():
        return {"ok": False, "error": f"no conversation for session {session_id}"}
    closed = []
    for r in con.execute(
            "SELECT id, json_extract(props,'$.role') role FROM nodes "
            "WHERE type='agent' AND status!='done' "
            "AND json_extract(props,'$.session_id')=?", (session_id,)).fetchall():
        outcome = "completed" if r["role"] == "main" else "abandoned"
        api_agent_finish(con, r["id"], outcome, "conversation ended")
        closed.append({"id": r["id"], "outcome": outcome})
    patch = {"ended_at": now()}
    if reason:
        patch["end_reason"] = reason
    res = api_update_node(con, cid, status="done", props_patch=patch)
    res["closed_agents"] = closed
    return res


def _tree(con, root_id: str, depth=0, seen=None):
    """Walk children (anything `part-of` this node) into a nested dict."""
    seen = seen if seen is not None else set()
    if root_id in seen or depth > 12:
        return None
    seen.add(root_id)
    row = con.execute("SELECT * FROM nodes WHERE id=?", (root_id,)).fetchone()
    if not row:
        return None
    d = _node_dict(row)
    p = d["props"]
    node = {"id": d["id"], "type": d["type"], "title": d["title"]}
    for k in ("role", "model", "outcome", "discard_type"):
        if p.get(k):
            node[k] = p[k]
    if d["type"] == "idea":
        node["status"] = d["status"]
    kids, msgs, ideas = [], 0, []
    rows = con.execute(
        "SELECT n.* FROM edges e JOIN nodes n ON n.id=e.src "
        "WHERE e.dst=? AND e.rel=? ORDER BY n.created", (root_id, AREA_REL)
    ).fetchall()
    for r in rows:
        if r["type"] in ("agent", "conversation"):
            sub = _tree(con, r["id"], depth + 1, seen)
            if sub:
                kids.append(sub)
        elif r["type"] == "capture":
            msgs += 1
        elif r["type"] == "idea":
            sub = _tree(con, r["id"], depth + 1, seen)
            if sub:
                ideas.append(sub)
    if msgs:
        node["messages"] = msgs
    if ideas:
        node["ideas"] = ideas
    if kids:
        node["children"] = kids
    return node


# --- ideas ------------------------------------------------------------------
def _transition(con, idea_id: str, frm: str, to: str, *, reason="",
                discard_type="", conditions=None, recheck_on=None,
                caused_by="", realm="") -> dict:
    """Append one immutable entry to the idea's log.

    Transitions are never updated or deleted — the sequence IS the history, and
    `idea.status` is only the cached head of it.
    """
    props = {"idea": idea_id, "from_status": frm, "to_status": to,
             "at": now()}
    if reason:
        props["reason"] = reason
    if discard_type:
        props["discard_type"] = discard_type
    if conditions:
        props["blocked_by"] = conditions
    if recheck_on:
        props["recheck_on"] = recheck_on
    if caused_by:
        props["caused_by"] = caused_by
    res = api_create_node(con, "transition", f"{frm or 'new'} -> {to}", reason,
                          props, "", "", realm or DEFAULT_REALM)
    if res.get("ok"):
        tid = res["node"]["id"]
        api_link(con, tid, idea_id, "about")
        if caused_by and con.execute(
                "SELECT 1 FROM nodes WHERE id=?", (caused_by,)).fetchone():
            api_link(con, tid, caused_by, "caused-by")
    return res


def _project_blocked(con, idea_id: str, transition_id: str, conditions) -> int:
    """Write the transition's conditions into the recheck index.

    The index is a PROJECTION of the log (§9 of the spec): rebuildable, never the
    source of truth. Callers reach it through here, never with their own INSERT.
    """
    ts = now()
    n = 0
    for c in conditions or []:
        con.execute(
            "INSERT OR IGNORE INTO blocked_item"
            "(item_type,item_ref,gauge_key,op,required,source_ref,created,resolved_at)"
            " VALUES(?,?,?,?,?,?,?,'')",
            ("idea", idea_id, c["gauge"], c["op"], str(c.get("value", "")),
             transition_id, ts))
        n += 1
    con.commit()
    return n


def api_idea_propose(con, title: str, *, detail="", agent_id="", realm="") -> dict:
    """Every alternative becomes a record, not just the chosen one — that is the
    whole reason the same option does not get re-argued from zero six months on."""
    if not (title or "").strip():
        return {"ok": False, "error": "missing title"}
    props = {"proposed_at": now()}
    if agent_id:
        props["agent"] = agent_id
    res = api_create_node(con, "idea", title, detail or "", props, "proposed",
                          "", realm or DEFAULT_REALM)
    if not res.get("ok"):
        return res
    iid = res["node"]["id"]
    if agent_id and con.execute(
            "SELECT 1 FROM nodes WHERE id=?", (agent_id,)).fetchone():
        api_link(con, iid, agent_id, AREA_REL)
    _transition(con, iid, "", "proposed", realm=realm)
    return res


def api_idea_discard(con, idea_id: str, discard_type: str, reason: str, *,
                     conditions=None, recheck_on=None, caused_by="") -> dict:
    """The highest-value hook, and the easiest one to implement badly.

    A conditional discard WITHOUT a structured condition is a permanent discard
    wearing a disguise: nothing will ever re-check it, and the reason text will
    outlive everyone who could interpret it. So it is rejected at the write, not
    audited later — and every gauge it names must already be in the catalog, or
    the condition points at something nobody measures.
    """
    row = con.execute("SELECT * FROM nodes WHERE id=?", (idea_id,)).fetchone()
    if not row:
        return {"ok": False, "error": f"no node {idea_id}"}
    if row["type"] != "idea":
        return {"ok": False, "error": f"{idea_id} is a {row['type']}, not an idea"}
    if discard_type not in DISCARD_TYPES:
        return {"ok": False,
                "error": f"discard type must be one of {'|'.join(DISCARD_TYPES)}"}
    if not (reason or "").strip():
        return {"ok": False, "error": "a discard needs a human-readable reason"}
    conditions = list(conditions or [])
    if discard_type == "conditional" and not conditions:
        return {"ok": False, "error":
                "a conditional discard needs at least one structured condition "
                "(--blocked-by 'gauge.key >= value'); without one it is a "
                "permanent discard in disguise — use --type permanent instead"}
    if discard_type != "conditional" and conditions:
        return {"ok": False, "error":
                f"only a conditional discard carries conditions; "
                f"{discard_type} discards are never re-checked"}
    missing = [c["gauge"] for c in conditions
               if not con.execute("SELECT 1 FROM nodes WHERE id=? AND type='gauge'",
                                  (_gauge_id(c["gauge"]),)).fetchone()]
    if missing:
        return {"ok": False, "error":
                f"unknown gauge(s): {', '.join(sorted(set(missing)))} — add them "
                f"first (gauge-set --key <key> --value <v> --probe '<cmd>'), or "
                f"the condition names something nobody measures"}
    frm = row["status"] or "proposed"
    tr = _transition(con, idea_id, frm, "discarded", reason=reason,
                     discard_type=discard_type, conditions=conditions,
                     recheck_on=recheck_on, caused_by=caused_by,
                     realm=row["realm"])
    if not tr.get("ok"):
        return tr
    patch = {"discard_type": discard_type, "discard_reason": reason,
             "discarded_at": now()}
    if conditions:
        patch["blocked_by"] = conditions
    if recheck_on:
        patch["recheck_on"] = recheck_on
    res = api_update_node(con, idea_id, status="discarded", props_patch=patch)
    if not res.get("ok"):
        return res
    for c in conditions:                    # the graph shows the block too
        api_link(con, idea_id, _gauge_id(c["gauge"]), "blocked-by")
    res["transition"] = tr["node"]["id"]
    res["indexed_conditions"] = _project_blocked(con, idea_id, tr["node"]["id"],
                                                 conditions)
    res["rechecks_automatically"] = discard_type == "conditional"
    return res


def api_idea_move(con, idea_id: str, to: str, *, reason="", caused_by="",
                  supersedes="") -> dict:
    """selected / superseded / revived — the transitions that carry no condition."""
    row = con.execute("SELECT * FROM nodes WHERE id=?", (idea_id,)).fetchone()
    if not row:
        return {"ok": False, "error": f"no node {idea_id}"}
    if row["type"] != "idea":
        return {"ok": False, "error": f"{idea_id} is a {row['type']}, not an idea"}
    if to not in IDEA_STATUSES:
        return {"ok": False, "error": f"status must be one of {'|'.join(IDEA_STATUSES)}"}
    frm = row["status"] or "proposed"
    tr = _transition(con, idea_id, frm, to, reason=reason, caused_by=caused_by,
                     realm=row["realm"])
    if not tr.get("ok"):
        return tr
    patch = {f"{to}_at": now()}
    if reason:
        patch[f"{to}_reason"] = reason
    res = api_update_node(con, idea_id, status=to, props_patch=patch)
    if supersedes:
        api_link(con, idea_id, supersedes, "supersedes")
        api_idea_move(con, supersedes, "superseded",
                      reason=f"superseded by {idea_id}")
    if res.get("ok"):
        res["transition"] = tr["node"]["id"]
    return res


# --- the gauge catalog ------------------------------------------------------
def api_gauge_set(con, key: str, *, value=None, unit="", probe="",
                  description="", realm="") -> dict:
    """Upsert one measurable thing. A NEW GAUGE NEEDS NO NEW CODE — a row and a
    probe, and every idea blocked on it becomes re-checkable."""
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "missing key"}
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*", key):
        return {"ok": False, "error":
                f"gauge key {key!r} must be dotted alphanumerics, e.g. gpu.vram_gb"}
    gid = _gauge_id(key)
    row = con.execute("SELECT * FROM nodes WHERE id=?", (gid,)).fetchone()
    patch = {"key": key}
    if unit:
        patch["unit"] = unit
    if probe:
        patch["probe"] = probe
    if description:
        patch["description"] = description
    previous = None
    if value is not None:
        previous = (_node_dict(row)["props"].get("current_value") if row else None)
        patch["current_value"] = value
        patch["measured_at"] = now()
    if row:
        res = api_update_node(con, gid, body=description or None,
                              props_patch=patch)
    else:
        res = api_create_node(con, "gauge", key, description, patch, "",
                              "", realm or DEFAULT_REALM, gid)
    if res.get("ok"):
        res["previous_value"] = previous
        res["changed"] = value is not None and str(previous) != str(value)
    return res


def _gauge_values(con) -> dict:
    vals = {}
    for r in con.execute("SELECT * FROM nodes WHERE type='gauge'").fetchall():
        d = _node_dict(r)
        p = d["props"]
        vals[p.get("key") or d["title"]] = p
    return vals


def api_gauge_probe(con, key="") -> dict:
    """Run the stored probes and record what they measured.

    The probe is a shell command the OWNER put in their own catalog, and it only
    runs when someone asks for it — never as a side effect of a read. A gauge
    with no probe is a gauge nobody will ever re-check, which is why the catalog
    treats one as a warning.
    """
    rows = con.execute("SELECT * FROM nodes WHERE type='gauge' ORDER BY id").fetchall()
    ran, skipped, changed = [], [], []
    for r in rows:
        d = _node_dict(r)
        k = d["props"].get("key") or d["title"]
        if key and k != key:
            continue
        probe = (d["props"].get("probe") or "").strip()
        if not probe:
            skipped.append({"key": k, "why": "no probe"})
            continue
        try:
            p = subprocess.run(probe, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=PROBE_TIMEOUT)
        except Exception as e:
            skipped.append({"key": k, "why": f"probe failed: {e}"}); continue
        if p.returncode != 0:
            skipped.append({"key": k, "why": f"probe exit {p.returncode}",
                            "stderr": (p.stderr or "").strip()[:200]})
            continue
        val = (p.stdout or "").strip().splitlines()
        val = val[0].strip() if val else ""
        res = api_gauge_set(con, k, value=val)
        ran.append({"key": k, "value": val})
        if res.get("changed"):
            changed.append(k)
    return {"ok": True, "probed": ran, "skipped": skipped, "changed": changed}


# --- re-evaluation ----------------------------------------------------------
def _rebuild_blocked(con) -> int:
    """Re-derive the whole index from the transition log.

    The log is authoritative; this table is disposable. An item is blocked if its
    latest conditional discard has not been followed by a revival.
    """
    con.execute("DELETE FROM blocked_item")
    rows = con.execute(
        "SELECT * FROM nodes WHERE type='transition' ORDER BY created").fetchall()
    latest = {}
    for r in rows:
        d = _node_dict(r)
        p = d["props"]
        idea = p.get("idea")
        if not idea:
            continue
        latest[idea] = d           # ordered by creation, so the last one wins
    n = 0
    for idea, d in latest.items():
        p = d["props"]
        if p.get("to_status") != "discarded" or p.get("discard_type") != "conditional":
            continue
        n += _project_blocked(con, idea, d["id"], p.get("blocked_by") or [])
    con.commit()
    return n


def _blocked_groups(con, gauge_key="") -> dict:
    """Unresolved index rows grouped by (item, source transition).

    The group is the unit of evaluation because multiple conditions on one
    discard are a CONJUNCTION — all of them have to hold before anything comes
    back. Filtering by gauge only picks which groups to look at; the evaluation
    still reads the group's every condition.
    """
    if gauge_key:
        refs = [(r["item_ref"], r["source_ref"]) for r in con.execute(
            "SELECT DISTINCT item_ref, source_ref FROM blocked_item "
            "WHERE resolved_at='' AND gauge_key=?", (gauge_key,)).fetchall()]
        if not refs:
            return {}
        qs = ",".join("?" for _ in refs)
        rows = con.execute(
            f"SELECT * FROM blocked_item WHERE resolved_at='' AND source_ref IN ({qs})",
            [r[1] for r in refs]).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM blocked_item WHERE resolved_at='' ORDER BY created"
        ).fetchall()
    groups = {}
    for r in rows:
        groups.setdefault((r["item_type"], r["item_ref"], r["source_ref"]), []).append(
            {"gauge": r["gauge_key"], "op": r["op"], "value": r["required"]})
    return groups


def api_recheck(con, *, gauge_key="", apply=False, quiet_days=NOTIFY_QUIET_DAYS,
                force=False) -> dict:
    """What has become possible again?

    One indexed pass over `blocked_item` — no scan of the graph, no model call.
    It NOTIFIES; it never resumes the work. The context around a discarded idea
    has moved on too, and only a person can say whether it still makes sense.
    """
    gauges = _gauge_values(con)
    unblocked, still, unmeasured = [], 0, set()
    for (item_type, item_ref, src), conds in _blocked_groups(con, gauge_key).items():
        checks, ok_all = [], True
        for c in conds:
            g = gauges.get(c["gauge"]) or {}
            cur = g.get("current_value")
            if cur in (None, "") and c["op"] not in UNARY_OPS:
                unmeasured.add(c["gauge"])
            met = compare_values(cur, c["op"], c["value"])
            ok_all = ok_all and met
            checks.append({"gauge": c["gauge"], "op": c["op"],
                           "required": c["value"], "current": cur, "met": met})
        if not ok_all:
            still += 1
            continue
        row = con.execute("SELECT * FROM nodes WHERE id=?", (item_ref,)).fetchone()
        if not row:
            continue
        d = _node_dict(row)
        last = d["props"].get("notified_at") or ""
        if not force and last and _within_days(last, quiet_days):
            continue                      # already asked recently; stay quiet
        unblocked.append({"item_type": item_type, "id": item_ref,
                          "title": d["title"], "source_ref": src,
                          "reason_discarded": d["props"].get("discard_reason", ""),
                          "conditions": checks})
    res = {"ok": True, "gauge": gauge_key or "(all)", "unblocked": len(unblocked),
           "still_blocked": still, "items": unblocked}
    if unmeasured:
        res["unmeasured_gauges"] = sorted(unmeasured)
        res["warning"] = ("these gauges have no measurement, so anything waiting "
                          "on them can never be re-checked — give them a probe "
                          "and run gauge-probe")
    if apply:
        applied = []
        for it in unblocked:
            con.execute("UPDATE blocked_item SET resolved_at=? WHERE source_ref=? "
                        "AND resolved_at=''", (now(), it["source_ref"]))
            met = "; ".join(f"{c['gauge']} {c['op']} {c['required']} "
                            f"(now {c['current']})" for c in it["conditions"])
            api_idea_move(con, it["id"], "revived",
                          reason=f"condition no longer holds: {met}")
            api_update_node(con, it["id"], props_patch={"notified_at": now()})
            applied.append(it["id"])
        con.commit()
        res["revived"] = applied
    res["notice"] = [
        f"\"{it['title']}\" was discarded because {it['reason_discarded'] or 'a condition did not hold'}. "
        f"That condition now holds. Reconsider?" for it in unblocked]
    res["hint"] = ("this is a proposal, not a resumption — a person decides "
                   "whether it still makes sense" if unblocked else
                   "nothing became possible")
    return res


def _within_days(iso: str, days: int) -> bool:
    try:
        t = datetime.fromisoformat(iso)
    except Exception:
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t) < timedelta(days=days)


# --- context as an event stream ---------------------------------------------
def api_context_event(con, agent_id: str, op: str, item_type: str,
                      item_ref: str, tokens=0, message_id="") -> dict:
    """Record the CHANGE to the window, not a photograph of it.

    A snapshot per turn stores the same document fifty times; the deltas store it
    once and still reconstruct any instant by replay — and they answer the
    question a snapshot cannot: why was this in the window at all?
    """
    if op not in ("added", "removed", "summarized"):
        return {"ok": False, "error": "op must be added|removed|summarized"}
    props = {"op": op, "item_type": item_type, "item_ref": item_ref,
             "tokens": int(tokens or 0), "agent": agent_id, "at": now()}
    if message_id:
        props["message"] = message_id
    res = api_create_node(con, "context-event", f"{op} {item_type}:{item_ref}",
                          "", props, "", "", DEFAULT_REALM)
    if res.get("ok") and agent_id and con.execute(
            "SELECT 1 FROM nodes WHERE id=?", (agent_id,)).fetchone():
        api_link(con, res["node"]["id"], agent_id, AREA_REL)
    return res


def api_context_at(con, agent_id: str, at="") -> dict:
    """Replay the events up to `at` and return the window as it stood."""
    rows = con.execute(
        "SELECT * FROM nodes WHERE type='context-event' ORDER BY created").fetchall()
    window, total, seen = {}, 0, 0
    for r in rows:
        d = _node_dict(r)
        p = d["props"]
        if p.get("agent") != agent_id:
            continue
        if at and (p.get("at") or d["created"]) > at:
            continue
        seen += 1
        ref = f"{p.get('item_type')}:{p.get('item_ref')}"
        if p.get("op") == "removed":
            window.pop(ref, None)
        else:
            window[ref] = {"item_type": p.get("item_type"),
                           "item_ref": p.get("item_ref"),
                           "tokens": p.get("tokens", 0),
                           "op": p.get("op"), "at": p.get("at")}
    total = sum(int(v.get("tokens") or 0) for v in window.values())
    return {"ok": True, "agent": agent_id, "at": at or now(),
            "events_replayed": seen, "items": list(window.values()),
            "tokens": total}


# --- commands ---------------------------------------------------------------
def cmd_conversation_start(args):
    con = connect()
    out(api_conversation(con, args.session, cwd=args.cwd or "",
                         owner=args.owner or "", title=args.title or ""))


def cmd_conversation_end(args):
    con = connect()
    out(api_conversation_end(con, args.session, args.reason or ""))


def cmd_agent_spawn(args):
    con = connect()
    parent = args.parent
    if not parent:
        if not args.session:
            out({"ok": False, "error": "pass --parent, or --session to hang it "
                                       "off that session's conversation"}); return
        conv = api_conversation(con, args.session, cwd=args.cwd or "")
        if not conv.get("ok"):
            out(conv); return
        parent = conv["node"]["id"]
    out(api_agent_spawn(con, parent, args.role, model=args.model or "",
                        body=args.body or "", session_id=args.session or ""))


def cmd_agent_finish(args):
    con = connect()
    out(api_agent_finish(con, args.id, args.outcome, args.note or ""))


def cmd_tree(args):
    """The whole branch, token-free — including what a subagent discarded."""
    con = connect()
    root = args.id or (_conv_id(args.session) if args.session else "")
    if not root:
        out({"ok": False, "error": "pass --id or --session"}); return
    t = _tree(con, root)
    if not t:
        out({"ok": False, "error": f"no node {root}"}); return
    out({"ok": True, "tree": t})


def _conditions_from(args):
    return [parse_condition(c) for c in (args.blocked_by or [])]


def cmd_idea_propose(args):
    con = connect()
    agent = args.agent or ""
    if not agent and args.session:
        conv = api_conversation(con, args.session)
        agent = conv["node"]["id"] if conv.get("ok") else ""
    out(api_idea_propose(con, args.title, detail=args.detail or "",
                         agent_id=agent, realm=getattr(args, "realm", "") or ""))


def cmd_idea_discard(args):
    con = connect()
    try:
        conds = _conditions_from(args)
    except ValueError as e:
        out({"ok": False, "error": str(e)}); return
    out(api_idea_discard(con, args.id, args.type, args.reason or "",
                         conditions=conds, recheck_on=args.recheck_on or [],
                         caused_by=args.caused_by or ""))


def cmd_idea_select(args):
    con = connect()
    out(api_idea_move(con, args.id, "selected", reason=args.reason or "",
                      caused_by=args.caused_by or "",
                      supersedes=args.supersedes or ""))


def cmd_idea_revive(args):
    con = connect()
    con.execute("UPDATE blocked_item SET resolved_at=? WHERE item_ref=? AND "
                "resolved_at=''", (now(), args.id))
    con.commit()
    out(api_idea_move(con, args.id, "revived", reason=args.reason or "revived by hand"))


def cmd_ideas(args):
    """Every alternative, not just the one that won."""
    con = connect()
    sql = "SELECT * FROM nodes WHERE type='idea'"
    params = []
    if args.status:
        sql += " AND status=?"; params.append(args.status)
    sql += " ORDER BY updated DESC LIMIT ?"
    params.append(int(args.limit or 30))
    rows = con.execute(sql, params).fetchall()
    res = []
    for r in rows:
        d = _node_dict(r)
        p = d["props"]
        if args.agent and p.get("agent") != args.agent:
            continue
        item = {"id": d["id"], "title": d["title"], "status": d["status"]}
        for k in ("discard_type", "discard_reason", "agent"):
            if p.get(k):
                item[k] = p[k]
        if p.get("blocked_by"):
            item["blocked_by"] = [f"{c['gauge']} {c['op']} {c.get('value','')}".strip()
                                  for c in p["blocked_by"]]
        res.append(item)
    out({"ok": True, "count": len(res), "results": res})


def cmd_idea_history(args):
    """The full transition log for one idea — reconstructable, in order."""
    con = connect()
    row = con.execute("SELECT * FROM nodes WHERE id=?", (args.id,)).fetchone()
    if not row:
        out({"ok": False, "error": f"no node {args.id}"}); return
    d = _node_dict(row)
    rows = con.execute(
        "SELECT n.* FROM edges e JOIN nodes n ON n.id=e.src "
        "WHERE e.dst=? AND e.rel='about' AND n.type='transition' ORDER BY n.created",
        (args.id,)).fetchall()
    hist = []
    for r in rows:
        p = _node_dict(r)["props"]
        step = {"at": p.get("at"), "from": p.get("from_status"),
                "to": p.get("to_status")}
        for k in ("reason", "discard_type", "blocked_by", "recheck_on", "caused_by"):
            if p.get(k):
                step[k] = p[k]
        hist.append(step)
    out({"ok": True, "id": d["id"], "title": d["title"], "status": d["status"],
         "count": len(hist), "history": hist})


def cmd_gauge_set(args):
    con = connect()
    res = api_gauge_set(con, args.key, value=args.value, unit=args.unit or "",
                        probe=args.probe or "", description=args.description or "")
    if res.get("ok") and res.get("changed") and not args.no_recheck:
        # `resource.changed` is the trigger that closes the loop: a measurement
        # moves, and the list of things that became possible falls out of an
        # indexed query — nobody has to remember they were waiting on it.
        res["recheck"] = api_recheck(con, gauge_key=args.key, apply=args.apply)
    out(res)


def cmd_gauges(args):
    con = connect()
    rows = con.execute("SELECT * FROM nodes WHERE type='gauge' ORDER BY id").fetchall()
    res = []
    for r in rows:
        d = _node_dict(r)
        p = d["props"]
        k = p.get("key") or d["title"]
        waiting = con.execute(
            "SELECT COUNT(DISTINCT item_ref) c FROM blocked_item "
            "WHERE gauge_key=? AND resolved_at=''", (k,)).fetchone()["c"]
        item = {"key": k, "current_value": p.get("current_value"),
                "unit": p.get("unit", ""), "measured_at": p.get("measured_at", ""),
                "has_probe": bool(p.get("probe")), "blocking": waiting}
        if args.full and p.get("probe"):
            item["probe"] = p["probe"]
        if p.get("description"):
            item["description"] = p["description"]
        res.append(item)
    no_probe = [i["key"] for i in res if not i["has_probe"] and i["blocking"]]
    payload = {"ok": True, "count": len(res), "results": res}
    if no_probe:
        payload["warning"] = (f"blocking with no probe: {', '.join(no_probe)} — "
                              f"a gauge nobody measures never re-checks anything")
    out(payload)


def cmd_gauge_probe(args):
    con = connect()
    res = api_gauge_probe(con, args.key or "")
    if res.get("changed") and not args.no_recheck:
        res["recheck"] = {k: api_recheck(con, gauge_key=k, apply=args.apply)
                          for k in res["changed"]}
    out(res)


def cmd_recheck(args):
    con = connect()
    if args.rebuild:
        n = _rebuild_blocked(con)
        out({"ok": True, "rebuilt": n,
             "hint": "index re-derived from the transition log"}); return
    out(api_recheck(con, gauge_key=args.key or "", apply=args.apply,
                    quiet_days=int(args.quiet_days), force=args.force))


def cmd_context_event(args):
    con = connect()
    out(api_context_event(con, args.agent, args.op, args.item_type, args.item_ref,
                          tokens=args.tokens or 0, message_id=args.message or ""))


def cmd_context_at(args):
    con = connect()
    out(api_context_at(con, args.agent, at=args.at or ""))


def build_parser():
    p = argparse.ArgumentParser(prog="graph", description="memory-ledger graph engine")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)
    sub.add_parser("stats").set_defaults(func=cmd_stats)

    a = sub.add_parser("backups", description=(
        "Point-in-time snapshots of the whole graph, taken before consolidation "
        f"and kept {BACKUP_KEEP_DAYS} days. Restore by copying one back over "
        "graph.db."))
    a.set_defaults(func=cmd_backups)
    a.add_argument("--new", action="store_true", help="take a snapshot now")
    a.add_argument("--label", help="name the snapshot (default: manual)")
    a.add_argument("--prune", action="store_true", help="delete expired snapshots")

    sub.add_parser("disable", help=(
        "turn the ledger off machine-wide (writes <home>/.disabled); "
        "idempotent")).set_defaults(func=cmd_disable)
    sub.add_parser("enable", help=(
        "turn the ledger back on (removes <home>/.disabled); idempotent. "
        "Does NOT override $MEMORY_LEDGER_OFF/$SECOND_BRAIN_OFF if set in "
        "this shell")).set_defaults(func=cmd_enable)

    a = sub.add_parser("add-node"); a.set_defaults(func=cmd_add_node)
    a.add_argument("--type", required=True)
    a.add_argument("--title", required=True)
    a.add_argument("--body"); a.add_argument("--props")
    a.add_argument("--status"); a.add_argument("--due"); a.add_argument("--id")
    a.add_argument("--realm", help="section: work | personal | ... (default $SECOND_BRAIN_REALM or default)")
    a.add_argument("--area", help="file under a domain area (hub_quote | quote | hub_health ...) "
                                 "— adds the part-of edge; errors if the area does not exist")
    a.add_argument("--force-new", action="store_true",
                   help="person only: skip the identity guard and create a second "
                        "node even if the name matches an existing person")

    a = sub.add_parser("people"); a.set_defaults(func=cmd_people)
    a.add_argument("--all", action="store_true",
                   help="include nodes merged away (resolvers), normally hidden")

    a = sub.add_parser("who"); a.set_defaults(func=cmd_who)
    a.add_argument("name", help="any spelling — nickname, surname, email")

    a = sub.add_parser("person-dupes").set_defaults(func=cmd_person_dupes)

    a = sub.add_parser("merge-person"); a.set_defaults(func=cmd_merge_person)
    a.add_argument("--into", required=True, metavar="ID",
                   help="the person node to KEEP (canonical)")
    a.add_argument("--from", required=True, dest="from_id", metavar="ID",
                   help="the duplicate to fold in — kept as a resolver, never deleted")

    a = sub.add_parser("add-alias"); a.set_defaults(func=cmd_add_alias)
    a.add_argument("--id", required=True)
    a.add_argument("--alias", required=True,
                   help="a spelling the heuristic cannot infer (a nickname, a handle)")

    a = sub.add_parser("flush").set_defaults(func=cmd_flush)
    sub.add_parser("neo4j-status").set_defaults(func=cmd_neo4j_status)
    sub.add_parser("sync", description="Two-way sync with the neo4j master: pull "
                   "other machines' changes, then push this one's."
                   ).set_defaults(func=cmd_sync)
    sub.add_parser("sections").set_defaults(func=cmd_sections)
    a = sub.add_parser("rename-realm", help="move every node in one realm to another "
                                            "(merges if the new one exists)")
    a.add_argument("--from", dest="old", required=True)
    a.add_argument("--to", dest="new", required=True)
    a.set_defaults(func=cmd_rename_realm)
    sub.add_parser("areas").set_defaults(func=cmd_areas)

    a = sub.add_parser("add-area"); a.set_defaults(func=cmd_add_area)
    a.add_argument("--title", required=True)
    a.add_argument("--id", help="default hub_<slug of title>")
    a.add_argument("--domain", help="short domain key (default: slug of title)")
    a.add_argument("--body"); a.add_argument("--realm")

    a = sub.add_parser("ingest-conventions", description=(
        "Load CONVENTIONS.md into the brain as rule nodes, one per section. "
        "Idempotent; never overwrites a user-authored or user-edited rule."))
    a.set_defaults(func=cmd_ingest_conventions)
    a.add_argument("--file", help="default: the plugin's CONVENTIONS.md")
    a.add_argument("--realm", help="default shared")

    a = sub.add_parser("add-rule", description=(
        "Author or override a convention rule (the mutable half of the genome)."))
    a.set_defaults(func=cmd_add_rule)
    a.add_argument("--title", required=True)
    a.add_argument("--body"); a.add_argument("--id")
    a.add_argument("--scenario", help="cue words for match-rules (default: the title)")
    a.add_argument("--section", help="provenance label, e.g. 2e or 'user'")
    a.add_argument("--source", choices=["user", "learned", "plugin"], default="user")
    a.add_argument("--overrides", help="rule id this replaces (it is marked superseded)")
    a.add_argument("--non-negotiable", dest="non_negotiable", action="store_true",
                   help="always returned by match-rules, cue or no cue")
    a.add_argument("--realm")
    a.add_argument("--strict", action="store_true",
                   help="refuse an incomplete contract instead of reporting it later")
    # the §5b.1 sections, so a rule can be authored structured rather than as one
    # --body the author has to remember the headings for
    for _f, _h, _r in RULE_SECTIONS:
        a.add_argument("--" + _f.replace("_", "-"), dest=_f,
                       help=f"the {_h} section")

    a = sub.add_parser("match-rules", description=(
        "Which conventions apply to THIS situation — the token-light replacement "
        "for reading all of CONVENTIONS.md. No model call."))
    a.set_defaults(func=cmd_match_rules)
    a.add_argument("query")
    a.add_argument("--limit", type=int, default=6)
    a.add_argument("--budget", type=int, default=2600, help="max chars of rule text")
    a.add_argument("--full", action="store_true", help="whole rule bodies, unbudgeted")

    a = sub.add_parser("propose-rules", description=(
        "Learned-rules detector: spot corrections you repeat across sessions and "
        "PROPOSE rules. Needs `learned-rules on`; never applies anything. No model call."))
    a.set_defaults(func=cmd_propose_rules)
    a.add_argument("--dry-run", action="store_true", help="report, write nothing")
    a.add_argument("--days", type=int, default=LR_WINDOW_DAYS)
    a.add_argument("--min-count", type=int, default=LR_MIN_COUNT, dest="min_count")
    a.add_argument("--min-sessions", type=int, default=LR_MIN_SESSIONS, dest="min_sessions")

    a = sub.add_parser("rule-proposals", description="List learned-rule proposals (budgeted).")
    a.set_defaults(func=cmd_rule_proposals)
    a.add_argument("--status", choices=["proposed", "accepted", "rejected", "all"],
                   default="proposed")
    a.add_argument("--budget", type=int, default=READ_BUDGET)
    a.add_argument("--limit", type=int, default=50)

    a = sub.add_parser("accept-rule", description=(
        "Accept a proposal: it becomes an active rule, source learned, precedence 50."))
    a.set_defaults(func=cmd_accept_rule)
    a.add_argument("--id", required=True)
    a.add_argument("--title"); a.add_argument("--text")

    a = sub.add_parser("reject-rule", description=(
        "Reject a proposal. It is kept, and its cluster is not proposed again."))
    a.set_defaults(func=cmd_reject_rule)
    a.add_argument("--id", required=True); a.add_argument("--reason")

    a = sub.add_parser("learned-rules", description=(
        "Turn the learned-rules detector on or off (default off), or show its status."))
    a.set_defaults(func=cmd_learned_rules)
    a.add_argument("action", nargs="?", choices=["on", "off", "status"], default="status")

    a = sub.add_parser("genome", description=(
        "The brain's own build blueprint — how to rebuild the Claude layer from the "
        "DB alone. --refresh re-derives the API surface and schema from the code."))
    a.set_defaults(func=cmd_genome)
    a.add_argument("--refresh", action="store_true")
    a.add_argument("--out", help="write the full kit (specs + rules) to a directory")

    a = sub.add_parser("update-node"); a.set_defaults(func=cmd_update_node)
    a.add_argument("--id", required=True)
    a.add_argument("--title"); a.add_argument("--body"); a.add_argument("--props")
    a.add_argument("--status"); a.add_argument("--due"); a.add_argument("--realm")

    a = sub.add_parser("del-node"); a.set_defaults(func=cmd_del_node)
    a.add_argument("--id", required=True)

    a = sub.add_parser("get-node"); a.set_defaults(func=cmd_get_node)
    a.add_argument("--id", required=True)

    a = sub.add_parser("add-edge"); a.set_defaults(func=cmd_add_edge)
    a.add_argument("--src", required=True); a.add_argument("--dst", required=True)
    a.add_argument("--rel", required=True); a.add_argument("--props")

    a = sub.add_parser("del-edge"); a.set_defaults(func=cmd_del_edge)
    a.add_argument("--src", required=True); a.add_argument("--dst", required=True)
    a.add_argument("--rel", required=True)

    a = sub.add_parser("recall"); a.set_defaults(func=cmd_recall)
    a.add_argument("query", nargs="?", default="")
    a.add_argument("--limit", type=int, default=8)
    a.add_argument("--depth", type=int, default=ACTIVATION_DEPTH,
                   help="how far activation spreads across edges (default 2)")
    a.add_argument("--budget", type=int, default=RECALL_BUDGET,
                   help="max chars of payload to return (token guard)")
    a.add_argument("--all", action="store_true", help="include done/cancelled")
    a.add_argument("--include-captures", action="store_true",
                   help="include raw capture nodes (normally the digest covers them)")
    a.add_argument("--no-wire", dest="wire", action="store_false",
                   help="do not create Hebbian co-activation edges")
    a.set_defaults(wire=True)

    a = sub.add_parser("decay", description=(
        "Per-type decay tiers: expire old captures into weekly digest notes, flag "
        "stale open tasks that are really raw prompts. Dry-run unless --apply. "
        "Never deletes."))
    a.set_defaults(func=cmd_decay)
    a.add_argument("--apply", action="store_true",
                   help="write: weekly digests, expire captures, close stale raw-prompt "
                        "tasks as `dropped` (default: report only)")
    a.add_argument("--stale-days", type=float, default=14.0, dest="stale_days",
                   help="an open task older than this and prompt-shaped is flagged (default 14)")
    a.add_argument("--no-backup", action="store_true", dest="no_backup",
                   help="skip the pre-apply snapshot")

    a = sub.add_parser("consolidate"); a.set_defaults(func=cmd_consolidate)
    a.add_argument("--day", help="YYYY-MM-DD (default: every day with captures)")
    a.add_argument("--older-than", type=int, dest="older_than",
                   help="only days at least N days old")
    a.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="report what would change and write nothing")
    a.add_argument("--no-backup", action="store_true", dest="no_backup",
                   help="skip the pre-run snapshot (the daily hook uses this)")

    a = sub.add_parser("suggest-links"); a.set_defaults(func=cmd_suggest_links)
    a.add_argument("text", help="free text to scan for mentions of known nodes")
    a.add_argument("--limit", type=int, default=8)
    a.add_argument("--exclude", help="node id to never suggest (the node being edited)")

    a = sub.add_parser("search", description=(
        "Full-text search. BUDGETED by default (bodies trimmed to share "
        "--budget chars); --full restores whole bodies."))
    a.set_defaults(func=cmd_search)
    a.add_argument("query", nargs="?", default="")
    a.add_argument("--type"); a.add_argument("--limit", type=int, default=15)
    a.add_argument("--budget", type=int, default=READ_BUDGET,
                   help=f"max chars of body text (default {READ_BUDGET})")
    a.add_argument("--full", action="store_true", help="whole bodies, unbudgeted")

    a = sub.add_parser("resume", description=(
        "Rehydrate the working thread after clearing the context window: what was "
        "in play, what is parked, what is still open — inside a char budget, no "
        "model call. Run this right after /clear."))
    a.set_defaults(func=cmd_resume)
    a.add_argument("--budget", type=int, default=1800, help="max chars of brief")
    a.add_argument("--days", type=int, default=2, help="how far back 'in play' reaches")
    a.add_argument("--limit", type=int, default=8, help="max focus items")

    a = sub.add_parser("park", description=(
        "Move context OUT of the conversation into the brain and return a tiny "
        "receipt, so the model can drop the content from its window."))
    a.set_defaults(func=cmd_park)
    a.add_argument("--body", help="content to park (default: read stdin)")
    a.add_argument("--title"); a.add_argument("--id"); a.add_argument("--realm")
    a.add_argument("--area", help="file it under a domain area")
    a.add_argument("--source", help="where it came from (default: conversation)")
    a.add_argument("--about", action="append",
                   help="node id this context is about (repeatable)")

    a = sub.add_parser("unpark", description="Read parked context back, whole or sliced.")
    a.set_defaults(func=cmd_unpark)
    a.add_argument("--id", required=True)
    a.add_argument("--offset", type=int, help="start at this character")
    a.add_argument("--chars", type=int, help="return at most this many chars")

    a = sub.add_parser("maintain", description=(
        "Brain-side upkeep: consolidate old captures into per-day digests, repair "
        "reversed digest links, report what it saved. Non-destructive, idempotent, "
        "safe to schedule. This tidies the STORE, not the context window."))
    a.set_defaults(func=cmd_maintain)
    a.add_argument("--keep-days", type=int, dest="keep_days", default=2,
                   help="leave the last N days of captures loose (default 2)")
    a.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="report what would change and write nothing")
    a.add_argument("--no-backup", action="store_true", dest="no_backup",
                   help="skip the pre-run snapshot")

    a = sub.add_parser("list"); a.set_defaults(func=cmd_list)
    a.add_argument("--type"); a.add_argument("--status")
    a.add_argument("--area", help="only what is part-of this domain area "
                                 "(hub_quote | quote | hub_health ...)")
    a.add_argument("--due-before", dest="due_before")
    a.add_argument("--all", action="store_true",
                   help="include archived (done/cancelled) items")
    a.add_argument("--untracked", action="store_true",
                   help="only nodes no external task board has claimed (§2c)")
    a.add_argument("--tracked-by", dest="tracked_by", metavar="SYSTEM",
                   help="only nodes this external task board has claimed")
    a.add_argument("--limit", type=int, default=50)

    a = sub.add_parser("track", help="claim a task for an external task board "
                                     "(it leaves the open-loops brief; §2c)")
    a.set_defaults(func=cmd_track)
    a.add_argument("--id", required=True)
    a.add_argument("--system", required=True, help="the board's name, e.g. kanban")
    a.add_argument("--ref", required=True, help="the task's id on that board")
    a.add_argument("--url", help="a link to it there")

    a = sub.add_parser("untrack", help="release a task from its external board")
    a.set_defaults(func=cmd_untrack)
    a.add_argument("--id", required=True)

    a = sub.add_parser("complete"); a.set_defaults(func=cmd_complete)
    a.add_argument("--id", required=True)
    a.add_argument("--note", help="optional note about how it was completed")

    a = sub.add_parser("done"); a.set_defaults(func=cmd_done)
    a.add_argument("--limit", type=int, default=50)

    a = sub.add_parser("add-skill"); a.set_defaults(func=cmd_add_skill)
    a.add_argument("--name", required=True, help="short slug, e.g. triage-sentry")
    a.add_argument("--title", required=True)
    a.add_argument("--when", help="trigger conditions / when to use this skill")
    a.add_argument("--body", help="the procedure / steps")
    a.add_argument("--id")
    a.add_argument("--with-file", action="store_true",
                   help="also export a local SKILL.md (default: DB-only, the source of truth)")
    a.add_argument("--no-file", action="store_true",
                   help="(deprecated no-op — DB-only is now the default)")
    a.add_argument("--requires", help="skills this one's output is WRONG without (comma-separated)")
    a.add_argument("--waits-for", dest="waits_for",
                   help="skills that must finish first when they are also running (comma-separated)")
    a.add_argument("--enriched-by", dest="enriched_by",
                   help="skills to consume if they landed, never wait for (comma-separated)")
    a.add_argument("--reads", help="node types this skill consults (comma-separated)")
    a.add_argument("--writes", help="node types this skill leaves in the graph (comma-separated)")

    a = sub.add_parser("audit-contracts", description=(
        "Check the contracts that live in the DB, which the file checker cannot see: "
        "learned skills (§5.1) and user/learned rules (§5b.1). Reports gaps, never "
        "rewrites, because the missing clause is a judgment call."))
    a.set_defaults(func=cmd_audit_contracts)
    a.add_argument("--kind", help="only skills of this kind (plugin|learned)")
    a.add_argument("--source", action="append", help="only rules with this source")
    a.add_argument("--count-only", action="store_true", help="omit the gap list")

    a = sub.add_parser("snapshot-rules", description=(
        "Copy every rule VERBATIM into the rules-backup realm before a bulk edit "
        "(§5b.3). Snapshots are `rule-snapshot` nodes, never `rule`, so match-rules "
        "cannot return one. Idempotent by content; a changed rule gets a new "
        "generation and nothing is ever overwritten."))
    a.set_defaults(func=cmd_snapshot_rules)
    a.add_argument("--label", help="names this snapshot (default: pre-contract)")
    a.add_argument("--source", action="append",
                   help="only rules with this props.source (repeatable); default all")

    a = sub.add_parser("sync-skill-tree", description=(
        "Project the skill contracts into the graph as two trees (CONVENTIONS §5a): "
        "built-in skills under hub_skills_builtin, user skills under hub_skills_user. "
        "Built-in skills declare in their SKILL.md '## Depends on' block, user skills "
        "in props (add-skill --requires/--reads/--writes). Idempotent, and a true sync: "
        "generated edges that stopped being declared are pruned. Hand-made edges are "
        "never touched."))
    a.set_defaults(func=cmd_sync_skill_tree)
    a.add_argument("--realm", default="shared", help="section for the hubs and node-type nodes")
    a.add_argument("--no-node-types", action="store_true",
                   help="skip the node-type nodes; keep reads/writes as props only")

    a = sub.add_parser("ingest-skills"); a.set_defaults(func=cmd_ingest_skills)
    a.add_argument("--dir", action="append",
                   help="skill dir to scan (repeatable); default: plugin skills + learned_skills")
    a.add_argument("--realm", default="shared",
                   help="section for new skill nodes (default shared — process applies everywhere)")
    a.add_argument("--kind", help="label for skills from a custom --dir (default plugin)")
    a.add_argument("--all-md", action="store_true",
                   help="also ingest non-SKILL.md markdown files in the dirs")
    a.add_argument("--no-rules", dest="no_rules", action="store_true",
                   help="skip the exported rules/ folder beside the skills")

    a = sub.add_parser("export-skills", description=(
        "Write skill nodes back out as markdown so a brain can move by file: one "
        "folder per skill, plus a shared rules/ folder holding every rule they "
        "honor. Re-ingest on the other machine with ingest-skills."))
    a.set_defaults(func=cmd_export_skills)
    a.add_argument("--dir", help="destination (default: <home>/learned_skills)")
    a.add_argument("--kind", choices=["learned", "plugin", "all"], default="learned",
                   help="which tree to export (default learned — the built-ins "
                        "already ship as files)")
    a.add_argument("--no-rules", dest="no_rules", action="store_true",
                   help="skills only; leave the rules behind")

    a = sub.add_parser("export-bundle", description=(
        "Write SELECTED skills and rules to one zip: a directory per kind plus an "
        "import-plan.json naming the order they can be read back in. export-skills "
        "moves a whole tree to a folder; this is the route for handing a few "
        "contracts to someone else."))
    a.set_defaults(func=cmd_export_bundle)
    a.add_argument("--skill", action="append", help="skill node id (repeatable)")
    a.add_argument("--rule", action="append", help="rule node id (repeatable)")
    a.add_argument("--process", action="append", help="process node id (repeatable)")
    a.add_argument("--spec", help="JSON file with {skills:[ids], rules:[ids], "
                                  "processes:[ids]} — for selections too long to "
                                  "pass as flags")
    a.add_argument("--out", help="destination zip (default: <home>/bundles/skills-<stamp>.zip)")

    a = sub.add_parser("inspect-bundle", description=(
        "What is inside a bundle and which of it this brain already holds. Reads "
        "only — nothing is written, so it is safe to run on a zip you were sent."))
    a.set_defaults(func=cmd_inspect_bundle)
    a.add_argument("--zip", required=True, help="the bundle to look at")

    a = sub.add_parser("import-bundle", description=(
        "Apply a bundle in its plan's order. An item already in this brain is "
        "left alone unless you name it: update refreshes it, replace deletes it "
        "first, ignore skips it."))
    a.set_defaults(func=cmd_import_bundle)
    a.add_argument("--zip", required=True)
    a.add_argument("--select", action="append", metavar="ID[:MODE]",
                   help="import this id, with mode update|replace|ignore (repeatable)")
    a.add_argument("--spec", help="JSON file with {select:{id: mode}}")
    a.add_argument("--mode", choices=["update", "replace", "ignore"], default="update",
                   help="mode for selected ids that did not name one (default update)")
    a.add_argument("--realm", default="shared")
    a.add_argument("--process-realm", dest="process_realm", default="",
                   help="realm the bundle's processes land in (default: the "
                        "configured realm)")

    a = sub.add_parser("scripts", description=(
        "List the person's local scripts in <home>/scripts — name, size, "
        "executable bit, and the first line that says what each one is."))
    a.set_defaults(func=cmd_scripts)

    a = sub.add_parser("export-scripts", description=(
        "Zip local scripts (all, or the ones named) with their executable bit, "
        "into <home>/bundles/scripts-<stamp>.zip."))
    a.set_defaults(func=cmd_export_scripts)
    a.add_argument("--name", action="append", help="script filename (repeatable)")
    a.add_argument("--out", help="destination zip")

    a = sub.add_parser("create-script", description=(
        "Write one script into <home>/scripts, from --file or stdin. An existing "
        "one is replaced only with --replace, after a copy goes to "
        "<home>/backups/scripts-<stamp>/."))
    a.set_defaults(func=cmd_create_script)
    a.add_argument("--name", required=True, help="bare filename, e.g. deploy.sh")
    a.add_argument("--file", help="read the text from this file (default: stdin)")
    a.add_argument("--no-exec", dest="no_exec", action="store_true",
                   help="write it without the executable bit")
    a.add_argument("--replace", action="store_true", help="overwrite an existing one")

    a = sub.add_parser("inspect-scripts", description=(
        "What a scripts zip holds and which of it is already here. Reads only."))
    a.set_defaults(func=cmd_inspect_scripts)
    a.add_argument("--zip", required=True)

    a = sub.add_parser("import-scripts", description=(
        "Land scripts from a zip in <home>/scripts. A new file is written; an "
        "existing one is overwritten only with mode replace, after a copy goes "
        "to <home>/backups/scripts-<stamp>/."))
    a.set_defaults(func=cmd_import_scripts)
    a.add_argument("--zip", required=True)
    a.add_argument("--select", action="append", metavar="NAME[:MODE]",
                   help="import this script, with mode replace|keep (repeatable)")
    a.add_argument("--all", action="store_true", help="import every script in the zip")
    a.add_argument("--spec", help="JSON file with {select:{name: mode}}")
    a.add_argument("--mode", choices=list(SCRIPT_MODES), default="keep",
                   help="mode for scripts that did not name one (default keep)")

    a = sub.add_parser("add-process", description=(
        "Record a way of working as a process node in a realm, part-of "
        "hub_process. Given an existing process id, refreshes it."))
    a.set_defaults(func=cmd_add_process)
    a.add_argument("--title", required=True)
    a.add_argument("--body", default="")
    a.add_argument("--trigger", default="", help="when this process applies")
    a.add_argument("--realm", default="", help="default: the configured realm")
    a.add_argument("--id", default="")

    a = sub.add_parser("retype", description=(
        "Change the type of existing nodes, keeping their id and edges. The old "
        "type is kept in props.migrated_from, so running it again with that type "
        "reverses it."))
    a.set_defaults(func=cmd_retype)
    a.add_argument("--type", required=True, help="the new type, e.g. process")
    a.add_argument("--id", action="append", help="node id (repeatable)")
    a.add_argument("--ids-file", dest="ids_file",
                   help="file with one id per line (# comments allowed)")
    a.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="report what would change, write nothing")

    a = sub.add_parser("processes", description=(
        "Every process in every realm (the UI's Processes list)."))
    a.set_defaults(func=lambda args: out({"ok": True, "realm": current_realm(),
                                          "processes": process_catalog(connect())}))

    a = sub.add_parser("del-contracts", description=(
        "Delete skill and rule nodes. §2a's archive-never-delete is about work, "
        "not contracts: a skill nobody should fire again has to stop firing. A "
        "built-in needs --force, since its file would re-ingest it anyway."))
    a.set_defaults(func=cmd_del_contracts)
    a.add_argument("--id", action="append", help="node id (repeatable)")
    a.add_argument("--spec", help="JSON file with {ids:[...]}")
    a.add_argument("--force", action="store_true",
                   help="also delete built-ins, knowing ingest-skills brings them back")

    a = sub.add_parser("match-skills"); a.set_defaults(func=cmd_match_skills)
    a.add_argument("query", nargs="?", default="")
    a.add_argument("--limit", type=int, default=5)
    a.add_argument("--full", action="store_true",
                   help="return full skill bodies (default: cue-sized rows)")

    a = sub.add_parser("use-skill"); a.set_defaults(func=cmd_use_skill)
    a.add_argument("--id", required=True)

    a = sub.add_parser("open-loops"); a.set_defaults(func=cmd_open_loops)
    a.add_argument("--stale-days", dest="stale_days", type=int, default=2)
    a.add_argument("--limit", type=int, default=50)
    a.add_argument("--include-tracked", dest="include_tracked", action="store_true",
                   help="also list loops an external task board holds (§2c)")

    a = sub.add_parser("set-state"); a.set_defaults(func=cmd_set_state)
    a.add_argument("--id", required=True)
    a.add_argument("--state", required=True,
                   help="open|doing|blocked|waiting|needs-reply|done|closed|answered|...")
    a.add_argument("--waiting-on", dest="waiting_on")

    a = sub.add_parser("due"); a.set_defaults(func=cmd_due)
    a.add_argument("--within-days", dest="within_days", type=int, default=7)

    a = sub.add_parser("neighbors"); a.set_defaults(func=cmd_neighbors)
    a.add_argument("--id", required=True)
    a.add_argument("--rel"); a.add_argument("--depth", type=int, default=1)

    # --- traceability (§2k): the agent tree, ideas, gauges, recheck ---------
    a = sub.add_parser("conversation-start", description=(
        "Get-or-create the conversation node for a session (idempotent)."))
    a.set_defaults(func=cmd_conversation_start)
    a.add_argument("--session", required=True)
    a.add_argument("--cwd"); a.add_argument("--owner"); a.add_argument("--title")

    a = sub.add_parser("conversation-end", description=(
        "Close the conversation; any agent still open is marked abandoned."))
    a.set_defaults(func=cmd_conversation_end)
    a.add_argument("--session", required=True)
    a.add_argument("--reason")

    a = sub.add_parser("agent-spawn", description=(
        "Add an agent under EXACTLY one parent — a conversation or another agent."))
    a.set_defaults(func=cmd_agent_spawn)
    a.add_argument("--role", required=True)
    a.add_argument("--parent", help="parent node id; omit to use --session's conversation")
    a.add_argument("--session"); a.add_argument("--model")
    a.add_argument("--body"); a.add_argument("--cwd")

    a = sub.add_parser("agent-finish"); a.set_defaults(func=cmd_agent_finish)
    a.add_argument("--id", required=True)
    a.add_argument("--outcome", required=True, choices=list(AGENT_OUTCOMES))
    a.add_argument("--note")

    a = sub.add_parser("tree", description=(
        "The agent tree for a session or node, with its ideas. Token-free."))
    a.set_defaults(func=cmd_tree)
    a.add_argument("--session"); a.add_argument("--id")

    a = sub.add_parser("idea-propose", description=(
        "Record an alternative — every one, not just the chosen one."))
    a.set_defaults(func=cmd_idea_propose)
    a.add_argument("--title", required=True)
    a.add_argument("--detail"); a.add_argument("--agent"); a.add_argument("--session")
    a.add_argument("--realm")

    a = sub.add_parser("idea-select"); a.set_defaults(func=cmd_idea_select)
    a.add_argument("--id", required=True)
    a.add_argument("--reason"); a.add_argument("--caused-by", dest="caused_by")
    a.add_argument("--supersedes", help="idea id this one replaces")

    a = sub.add_parser("idea-discard", description=(
        "Discard an alternative WITH its reason. A conditional discard must "
        "carry at least one structured condition, or it is rejected."))
    a.set_defaults(func=cmd_idea_discard)
    a.add_argument("--id", required=True)
    a.add_argument("--type", required=True, choices=list(DISCARD_TYPES))
    a.add_argument("--reason", required=True, help="human-readable; never used in logic")
    a.add_argument("--blocked-by", dest="blocked_by", action="append",
                   metavar="'gauge.key >= value'",
                   help="structured condition; repeatable (all must hold)")
    a.add_argument("--recheck-on", dest="recheck_on", action="append",
                   metavar="EVENT", help="events that should trigger a re-check")
    a.add_argument("--caused-by", dest="caused_by", help="the turn that caused it")

    a = sub.add_parser("idea-revive"); a.set_defaults(func=cmd_idea_revive)
    a.add_argument("--id", required=True); a.add_argument("--reason")

    a = sub.add_parser("ideas"); a.set_defaults(func=cmd_ideas)
    a.add_argument("--status", choices=list(IDEA_STATUSES))
    a.add_argument("--agent"); a.add_argument("--limit", type=int, default=30)

    a = sub.add_parser("idea-history"); a.set_defaults(func=cmd_idea_history)
    a.add_argument("--id", required=True)

    a = sub.add_parser("gauge-set", description=(
        "Upsert one measurable thing in the catalog. A value change triggers "
        "a recheck of everything waiting on it."))
    a.set_defaults(func=cmd_gauge_set)
    a.add_argument("--key", required=True, metavar="gpu.vram_gb")
    a.add_argument("--value"); a.add_argument("--unit")
    a.add_argument("--probe", help="shell command whose first stdout line is the value")
    a.add_argument("--description")
    a.add_argument("--apply", action="store_true", help="also revive what unblocked")
    a.add_argument("--no-recheck", dest="no_recheck", action="store_true")

    a = sub.add_parser("gauges"); a.set_defaults(func=cmd_gauges)
    a.add_argument("--full", action="store_true", help="include probe commands")

    a = sub.add_parser("gauge-probe", description=(
        "Run the catalog's probes and record what they measured."))
    a.set_defaults(func=cmd_gauge_probe)
    a.add_argument("--key", help="probe one gauge (default: all with a probe)")
    a.add_argument("--apply", action="store_true")
    a.add_argument("--no-recheck", dest="no_recheck", action="store_true")

    a = sub.add_parser("recheck", description=(
        "What became possible again? One indexed pass, no model call. "
        "Notifies; --apply also marks the ideas revived."))
    a.set_defaults(func=cmd_recheck)
    a.add_argument("--key", help="only what waits on this gauge")
    a.add_argument("--apply", action="store_true")
    a.add_argument("--force", action="store_true", help="ignore the quiet window")
    a.add_argument("--quiet-days", dest="quiet_days", type=int,
                   default=NOTIFY_QUIET_DAYS)
    a.add_argument("--rebuild", action="store_true",
                   help="re-derive the index from the transition log")

    a = sub.add_parser("context-event", description=(
        "Record a CHANGE to the context window, not a snapshot of it."))
    a.set_defaults(func=cmd_context_event)
    a.add_argument("--agent", required=True)
    a.add_argument("--op", required=True, choices=["added", "removed", "summarized"])
    a.add_argument("--item-type", dest="item_type", required=True)
    a.add_argument("--item-ref", dest="item_ref", required=True)
    a.add_argument("--tokens", type=int); a.add_argument("--message")

    a = sub.add_parser("context-at", description=(
        "Replay the events and return the window as it stood."))
    a.set_defaults(func=cmd_context_at)
    a.add_argument("--agent", required=True)
    a.add_argument("--at", help="ISO instant (default: now)")

    a = sub.add_parser("export"); a.set_defaults(func=cmd_export)
    a.add_argument("--format", choices=["json", "md", "both"], default="both")
    a.add_argument("--out")

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except Exception as e:  # keep output machine-parseable
        out({"ok": False, "error": str(e)})
        sys.exit(1)


if __name__ == "__main__":
    main()
