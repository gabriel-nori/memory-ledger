"""What the read routes answer — built here, framework-free.

Every function returns a plain dict. They were lifted out of the old stdlib
handler unchanged, so the shapes the page reads did not move with the framework;
`tests/test_api.py` pins them from the outside.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import brain_query
import graph

from . import state


def _sqlite_payload() -> dict:
    con = graph.connect()
    node_rows = con.execute("SELECT * FROM nodes").fetchall()
    edge_rows = con.execute("SELECT src, dst, rel FROM edges").fetchall()

    degree: dict[str, int] = {}
    for e in edge_rows:
        degree[e["src"]] = degree.get(e["src"], 0) + 1
        degree[e["dst"]] = degree.get(e["dst"], 0) + 1

    nodes = []
    mutated = ""          # newest `updated` across the graph — the change signal
    for r in node_rows:
        d = graph._node_dict(r)
        if d.get("updated", "") > mutated:
            mutated = d["updated"]
        nodes.append({
            "id": d["id"], "type": d["type"], "title": d["title"],
            "status": d["status"], "due": d["due"], "body": d["body"],
            "realm": brain_query.norm_realm(d.get("realm", "")),
            "kind": brain_query.norm_kind(d["props"]),
            "props": d["props"], "degree": degree.get(d["id"], 0),
        })
    edges = [{"src": e["src"], "dst": e["dst"], "rel": e["rel"]} for e in edge_rows]

    by_type: dict[str, int] = {}
    by_realm: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for n in nodes:
        by_type[n["type"]] = by_type.get(n["type"], 0) + 1
        by_realm[n["realm"]] = by_realm.get(n["realm"], 0) + 1
        by_kind[n["kind"]] = by_kind.get(n["kind"], 0) + 1

    return {
        "nodes": nodes, "edges": edges,
        "meta": {"nodes": len(nodes), "edges": len(edges),
                 "by_type": by_type, "by_realm": by_realm, "by_kind": by_kind,
                 "boot": state.BOOT, "generated": graph.now(),
                 "source": "sqlite (local)", "db": str(graph.db_path())},
        # Cheap change token so the client only re-lays-out when something moved.
        # Reads the `updated` COLUMN: an in-place edit (a status flip, a new due
        # date) leaves the node/edge counts alone, so a count-only token would make
        # the write invisible to the poller. The old token read props["updated_at"],
        # which only learn-skill ever writes.
        "version": f"sqlite-{len(nodes)}-{len(edges)}-{mutated}",
    }


def _neo4j_payload():
    """Fetch the graph from the neo4j master. Returns None if not configured /
    unreachable, so the caller can fall back to the local SQLite mirror."""
    import base64
    import urllib.request
    cfg = graph._neo4j_cfg()
    if not (cfg["url"] and cfg["password"]):
        return None
    endpoint = cfg["url"].rstrip("/") + f"/db/{cfg['db']}/tx/commit"
    auth = base64.b64encode(f"{cfg['user']}:{cfg['password']}".encode()).decode()
    body = json.dumps({"statements": [
        {"statement": "MATCH (n:Node) RETURN n"},
        {"statement": "MATCH (a:Node)-[r:REL]->(b:Node) RETURN a.id, b.id, r.rel"},
    ]}).encode()
    req = urllib.request.Request(endpoint, data=body, headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "Authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read())
        if data.get("errors"):
            return None
        node_rows = data["results"][0]["data"]
        edge_rows = data["results"][1]["data"]
    except Exception:
        return None

    CORE = {"id", "type", "title", "body", "status", "due", "created", "updated"}
    degree = {}
    edges = []
    for d in edge_rows:
        s, dst, rel = d["row"]
        edges.append({"src": s, "dst": dst, "rel": rel})
        degree[s] = degree.get(s, 0) + 1
        degree[dst] = degree.get(dst, 0) + 1
    nodes, by_type, by_realm, by_kind = [], {}, {}, {}
    mutated = ""          # newest `updated` — same change signal as the sqlite path
    for d in node_rows:
        p = d["row"][0] or {}
        nid = p.get("id")
        realm = brain_query.norm_realm(p.get("realm", ""))
        if p.get("updated", "") > mutated:
            mutated = p["updated"]
        props = {k: v for k, v in p.items() if k not in CORE}
        kind = brain_query.norm_kind(props)
        nodes.append({"id": nid, "type": p.get("type", ""), "title": p.get("title", ""),
                      "status": p.get("status", ""), "due": p.get("due", ""),
                      "body": p.get("body", ""), "realm": realm, "kind": kind,
                      "props": props, "degree": degree.get(nid, 0)})
        by_type[p.get("type", "")] = by_type.get(p.get("type", ""), 0) + 1
        by_realm[realm] = by_realm.get(realm, 0) + 1
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return {
        "nodes": nodes, "edges": edges,
        "meta": {"nodes": len(nodes), "edges": len(edges), "by_type": by_type,
                 "by_realm": by_realm, "by_kind": by_kind, "boot": state.BOOT,
                 "generated": graph.now(),
                 "source": f"neo4j ({cfg['url']})"},
        # had no mutation component at all, so an in-place write never moved it
        "version": f"neo4j-{len(nodes)}-{len(edges)}-{mutated}",
    }


def graph_payload() -> dict:
    """Prefer the neo4j master; fall back to the local SQLite mirror if neo4j is
    unreachable so the map still works offline."""
    payload = _neo4j_payload() or _sqlite_payload()
    payload["meta"]["ego"] = _ego_id({n["id"] for n in payload["nodes"]})
    return payload


def _ego_id(ids: set) -> str:
    """The person node the owner setting names — the map centres its layout on it.

    Empty when the owner resolves to nobody (or to several people): the map then
    falls back to its best-connected node rather than guessing between names."""
    try:
        hit = graph.resolve_person(graph.connect(), graph.owner())
    except Exception:
        return ""
    return hit["id"] if hit.get("ok") and hit["id"] in ids else ""


# ---------------------------------------------------------------------------
# settings + store health — what the Settings page reads and writes
#
# Preferences live in <home>/settings.json (see graph.settings), NOT in the
# browser: a timezone the UI sets has to hold for the cron, the hooks, and the
# CLI too, and localStorage cannot do that.
# ---------------------------------------------------------------------------
def timezones() -> list:
    try:
        from zoneinfo import available_timezones
        return sorted(available_timezones())
    except Exception:
        return []


def _int_setting(name: str, default: int) -> int:
    """A hand-edited settings.json (or a stray env var) must not 500 this page."""
    try:
        return int(graph.setting(name, str(default)))
    except (TypeError, ValueError):
        return default


def settings_payload() -> dict:
    """Stored values, plus what is ACTUALLY in effect. They differ whenever an
    env var overrides the file, and a settings page that hides that is lying —
    so `env` names every key the shell has taken over."""
    stored = graph.settings()
    # Resolved through graph._env(), which honours MEMORY_LEDGER_* first and
    # SECOND_BRAIN_* as fallback. Checking only the legacy prefix here would
    # make the page claim a setting is "from the file" while the engine is in
    # fact using a MEMORY_LEDGER_* override — precisely the lie this dict exists
    # to prevent.
    env = {k: graph._env(k.upper())
           for k in graph.SETTING_KEYS
           if graph._env(k.upper())}
    tz = graph.setting("tz")
    return {
        "ok": True, "writable": state.WRITABLE, "keys": graph.SETTING_KEYS,
        "settings": stored, "env": env, "path": str(graph.settings_path()),
        "effective": {
            "tz": tz, "tz_source": ("env" if "tz" in env else
                                    "settings" if stored.get("tz") else "system"),
            "system_tz": str(datetime.now().astimezone().tzinfo),
            "now": datetime.now(brain_query._local_tz(None)).isoformat(
                timespec="seconds"),
            "realm": graph.DEFAULT_REALM,
            # Who "me" is: what --mine filters on and what an unassigned task
            # falls back to. Hardcoded until this page could set it.
            "owner": graph.owner(),
            "owner_source": ("env" if "owner" in env else
                             "settings" if stored.get("owner") else "default"),
            # What the brain answers to: `{brain}` in a template, and the word a
            # wake phrase is said with.
            "brain_name": graph.brain_name(),
            "brain_name_source": ("env" if "brain_name" in env else
                                  "settings" if stored.get("brain_name") else "default"),
            # the uploaded icon's filename, "" when the default is in use
            "agent_icon": p.name if (p := graph.agent_icon_path()) else "",
            # what a CLI read (`brain_query.py`) uses when given no --backend.
            # This page and the map always ask for "auto", so they are unaffected.
            "backend": brain_query.default_backend(),
            "backends": list(graph.BACKENDS),
            # The switches. Each reports its source too, because the env var is
            # the NEGATIVE ($MEMORY_LEDGER_NO_AUTOSYNC) and a page that shows
            # only "off" leaves the user editing a file that cannot win.
            "autosync": graph.flag("autosync"),
            "autosync_source": graph.flag_source("autosync"),
            "consolidate": graph.flag("consolidate"),
            "consolidate_source": graph.flag_source("consolidate"),
            "ui_sync": graph.flag("ui_sync"),
            "ui_sync_source": graph.flag_source("ui_sync"),
            "learned_rules": graph.flag("learned_rules"),
            "learned_rules_source": graph.flag_source("learned_rules"),
            "prompt_recall": graph.flag("prompt_recall"),
            "prompt_recall_source": graph.flag_source("prompt_recall"),
            "agents_sync": graph.agents_sync_on(),
            "agents_sync_source": ("env" if os.environ.get("AGENTS_SYNC") == "1"
                                   else graph.flag_source("agents_sync")),
            "map_poll_seconds": _int_setting("map_poll_seconds", 2),
            "dash_refresh_seconds": _int_setting("dash_refresh_seconds", 120),
        },
        # Capture's own switch is not a setting — it is a marker file plus an env
        # var (§2d), and disable/enable write it. Reported here so one page can
        # answer "is it capturing?" without a second round trip.
        "capture": capture_payload(),
    }


def capture_payload() -> dict:
    """Whether the ledger is recording, and which switch decided that.

    Two switches, and they are not equal: $MEMORY_LEDGER_OFF is per-process and
    OUTRANKS the marker, so a UI that offers to "turn it back on" while the
    server's own shell exports that var would be promising something it cannot
    deliver. Hence `env_off` — the page greys the toggle out and says why."""
    marker = graph.home_dir() / graph.DISABLE_MARKER
    env_off = graph._env("OFF").strip().lower() in graph._OFF_TRUTHY
    return {
        "enabled": graph.enabled(),
        "marker": str(marker),
        "marker_present": marker.exists(),
        "env_off": env_off,
        "source": "env" if env_off else "marker" if marker.exists() else "on",
    }


def doctor_payload() -> dict:
    """The health check, as JSON, for the Settings page.

    Imported and called rather than shelled out: `doctor` is written to never
    traceback and to report each section's own failure, so there is nothing a
    subprocess boundary would protect the server from — and in-process it cannot
    pick up a different $MEMORY_LEDGER_HOME than the one this server is serving.
    """
    try:
        import doctor
        report = doctor.run()
    except Exception as e:                  # a health check that 500s is useless
        return {"ok": False,
                "error": f"doctor failed: {type(e).__name__}: {e}"}
    # doctor's own `ok` means HEALTHY; the envelope's means the call worked.
    # Collapsing them would make an unhealthy brain look like a failed request,
    # and the page would show a fetch error instead of the diagnosis.
    return {**report, "ok": True, "healthy": bool(report.get("ok"))}


def consolidate_preview(keep_days: str = "") -> dict:
    """What `maintain` would do, without doing any of it.

    The page shows this BEFORE offering the button, because consolidation
    rewrites props on every capture it touches and "non-destructive" is not the
    same promise as "you will like the result"."""
    try:
        keep = max(0, min(365, int(keep_days))) if str(keep_days).strip() else 2
    except (TypeError, ValueError):
        return {"ok": False, "error": "keep_days must be a whole number"}
    con = graph.connect()
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep)).date().isoformat()
        days = [r["d"] for r in con.execute(
            "SELECT DISTINCT substr(created,1,10) d FROM nodes WHERE type='capture'"
            " AND substr(created,1,10)<=? ORDER BY d", (cutoff,)).fetchall()]
        plan = graph.consolidation_plan(con, days)
        loose = con.execute(
            "SELECT COUNT(*) n FROM nodes WHERE type='capture'"
            " AND substr(created,1,10)<=?"
            " AND json_extract(props,'$.consolidated_into') IS NULL",
            (cutoff,)).fetchone()["n"]
        return {"ok": True, "dry_run": True, "keep_days": keep, "cutoff": cutoff,
                "still_loose": loose, "backup_keep_days": graph.BACKUP_KEEP_DAYS,
                **plan}
    finally:
        con.close()


def status_payload() -> dict:
    """Where the data lives and whether the master is reachable."""
    con = graph.connect()
    nodes = con.execute("SELECT COUNT(*) c FROM nodes").fetchone()["c"]
    edges = con.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"]
    pending = con.execute("SELECT COUNT(*) c FROM outbox").fetchone()["c"]
    captures = con.execute(
        "SELECT COUNT(*) c FROM nodes WHERE type='capture'"
        " AND json_extract(props,'$.consolidated_into') IS NULL").fetchone()["c"]
    cfg = graph._neo4j_cfg()
    up = graph._neo4j_up() if cfg["url"] else False
    return {
        "ok": True, "writable": state.WRITABLE, "boot": state.BOOT,
        "db": str(graph.db_path()), "home": str(graph.home_dir()),
        "nodes": nodes, "edges": edges, "pending_ops": pending,
        "loose_captures": captures,
        "neo4j": {
            "url": cfg["url"], "db": cfg["db"], "user": cfg["user"],
            "password_set": bool(cfg["password"]),
            "configured": bool(cfg["url"] and cfg["password"]),
            "up": up, "reading_from": "neo4j" if up else "sqlite (local mirror)",
            "file": str(graph.home_dir() / "neo4j.json"),
            # the file is only half the story — a shell that exports these wins
            "env": sorted(k for k in ("SECOND_BRAIN_NEO4J_URL", "SECOND_BRAIN_NEO4J_DB",
                                      "SECOND_BRAIN_NEO4J_USER",
                                      "SECOND_BRAIN_NEO4J_PASSWORD")
                          if os.environ.get(k)),
        },
    }
