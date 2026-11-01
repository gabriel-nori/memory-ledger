#!/usr/bin/env python3
"""brain-sync — keep a complete copy of the brain in the neo4j master, and get
it back from there.

The local SQLite file is the source of truth for now; the aim is to retire it
and run on neo4j alone (docs/neo4j.md#the-road-off-sqlite). So the master holds
everything the file does, and the file can be fed back from the master:

  :Node        every node, props verbatim in `_props` (flattened too, for queries)
  :REL         every edge, with its props and created time
  :MirrorRow   every row of the tables in graph.mirror_tables() (trigger history,
               gates, checkpoints, channels, the co-activation tally)
  :MirrorFile  the brain home's own files people write (settings, user scripts,
               references, learned skills). Secrets are never sent.

Day to day, SQLite triggers queue every change in the outbox and the engine
pushes it, and `--sync` (run by the brain server every minute and at every
session start) pulls down whatever another writer put on the master. The full
push is the reconcile on top of that.

Config (endpoint + creds) is read from the PRIVATE brain home, never the repo:
  ~/.second_brain/neo4j.json  ->  {"url": "...", "db": "neo4j", "user": "...", "password": "..."}
or env: SECOND_BRAIN_NEO4J_URL / _DB / _USER / _PASSWORD.

Usage:
  python3 scripts/brain_sync.py --dry-run     # what would be sent, push nothing
  python3 scripts/brain_sync.py --sync        # two-way: pull the master's changes, push ours
  python3 scripts/brain_sync.py               # full push (idempotent MERGE)
  python3 scripts/brain_sync.py --verify      # does the master hold everything local has?
  python3 scripts/brain_sync.py --pull        # rebuild a brain from the master into a new file
  python3 scripts/brain_sync.py --pull --replace   # ...and swap it in (old file kept in backups/)

Note: if the endpoint is plain HTTP, data + credentials travel in cleartext.
Add TLS (reverse proxy / tunnel) before treating this as secure.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import graph  # noqa: E402

NODE_BATCH = 300
EDGE_BATCH = 1000
ROW_BATCH = 1000
FILE_BATCH = 50
PAGE = 1000

# What of the brain home is mirrored as files. Everything else there is either
# the database, derived (exports, bundles, backups, logs), or a secret.
HOME_FILES = ("settings.json", "ui_prefs.json", "eval_cases.json", "README.md")
HOME_DIRS = ("scripts", "references", "learned_skills")
SECRET_FILES = {"neo4j.json", "neo4j_bkp.json", "trigger.json"}
FILE_MAX = 2 * 1024 * 1024



def load_cfg() -> dict:
    return graph._neo4j_cfg()


def _batches(items, n):
    for i in range(0, len(items), n):
        yield items[i:i + n]


# ---------------------------------------------------------------- local state

def local_nodes(con) -> list[dict]:
    ids = [r[0] for r in con.execute("SELECT id FROM nodes ORDER BY id")]
    return [p for p in (graph._node_payload(con, i) for i in ids) if p]


def local_edges(con) -> list[dict]:
    refs = con.execute("SELECT src, dst, rel FROM edges ORDER BY src, dst, rel").fetchall()
    return [p for p in (graph._edge_payload(con, *r) for r in refs) if p]


def local_rows(con) -> list[dict]:
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    out = []
    for table, pk in graph.mirror_tables().items():
        if table not in have:
            continue
        # the key is built by SQLite's json_array, exactly as the trigger builds
        # it, so a bulk push and a live push land on the same :MirrorRow
        keys = con.execute(f"SELECT json_array({', '.join(pk)}) FROM {table}").fetchall()
        out += [p for p in (graph._row_payload(con, table, k[0]) for k in keys) if p]
    return out


def local_files(home: Path) -> list[dict]:
    paths = [home / f for f in HOME_FILES]
    for d in HOME_DIRS:
        if (home / d).is_dir():
            paths += sorted(p for p in (home / d).rglob("*") if p.is_file())
    out = []
    for p in paths:
        rel = p.relative_to(home).as_posix()
        if (not p.is_file() or p.name in SECRET_FILES or "__pycache__" in p.parts
                or p.name == ".DS_Store" or ".conflict-" in p.name
                or p.stat().st_size > FILE_MAX):
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue                     # binary or unreadable: not a brain file
        out.append({"path": rel, "content": content,
                    "sha": hashlib.sha256(content.encode()).hexdigest()})
    return out


# ---------------------------------------------------------------- push

def push(con, nodes, edges, rows, files) -> int:
    ok, err = graph._ensure_neo4j_schema()
    if not ok:
        print(f"ERROR preparing the master: {err}", file=sys.stderr)
        return 1
    # ops queued before this push are covered by it; later ones still flush
    top = con.execute("SELECT COALESCE(MAX(seq), 0) FROM outbox").fetchone()[0]
    plan = [(graph._NODE_UPSERT, "nodes", nodes, NODE_BATCH),
            (graph._EDGE_UPSERT, "edges", edges, EDGE_BATCH),
            (graph._ROW_UPSERT, "rows", rows, ROW_BATCH),
            (graph._FILE_UPSERT, "files", files, FILE_BATCH)]
    for stmt, param, items, size in plan:
        for i, chunk in enumerate(_batches(items, size)):
            ok, err = graph._neo4j_call(
                graph._stamped(con, [{"statement": stmt, "parameters": {param: chunk}}]),
                timeout=graph.FLUSH_TIMEOUT)
            if not ok:
                print(f"ERROR pushing {param} (batch {i + 1}): {err}", file=sys.stderr)
                return 1
    con.execute("DELETE FROM outbox WHERE seq <= ?", (top,))
    for f in files:
        graph._sync_set(con, "file:" + f["path"], f["sha"])
    con.commit()
    return 0


# ---------------------------------------------------------------- master state

def _page(statement) -> list:
    out, skip = [], 0
    while True:
        rows, err = graph._neo4j_query(statement + " SKIP $skip LIMIT $limit",
                                       {"skip": skip, "limit": PAGE}, timeout=120)
        if err:
            raise SystemExit(f"ERROR reading the master: {err}")
        out += rows
        if len(rows) < PAGE:
            return out
        skip += PAGE


def master_state() -> dict:
    return {
        "nodes": [r[0] for r in _page(
            "MATCH (x:Node) RETURN properties(x) ORDER BY x.id")],
        "edges": _page("MATCH (a:Node)-[r:REL]->(b:Node) "
                       "RETURN a.id, b.id, properties(r) ORDER BY a.id, b.id, r.rel"),
        "rows": _page("MATCH (x:MirrorRow) RETURN x.table, x.key, x.data ORDER BY x.uid"),
        "files": _page("MATCH (f:MirrorFile) RETURN f.path, f.content, f.sha ORDER BY f.path"),
    }


def _drop_nulls(d: dict) -> dict:
    """neo4j does not store null properties, so compare without them — nor the
    sync stamps the master adds (`_seq`, `_origin`)."""
    return {k: v for k, v in d.items()
            if v is not None and k not in graph.MIRROR_STAMPS}


# ---------------------------------------------------------------- verify

def verify(con, home: Path) -> int:
    m = master_state()
    report = {}

    def compare(name, local: dict, master: dict):
        missing = sorted(k for k in local if k not in master)
        differ = sorted(k for k in local if k in master and local[k] != master[k])
        extra = sorted(k for k in master if k not in local)
        report[name] = {"local": len(local), "master": len(master),
                        "missing_on_master": len(missing), "different": len(differ),
                        "only_on_master": len(extra),
                        "examples": (missing + differ)[:5]}

    compare("nodes",
            {n["id"]: _drop_nulls(n) for n in local_nodes(con)},
            {p["id"]: _drop_nulls(p) for p in m["nodes"]})
    compare("edges",
            {(e["src"], e["dst"], e["rel"]): _drop_nulls(e["p"]) for e in local_edges(con)},
            {(s, d, p.get("rel")): _drop_nulls(p) for s, d, p in m["edges"]})
    compare("rows",
            {(r["table"], r["key"]): r["data"] for r in local_rows(con)},
            {(t, k): d for t, k, d in m["rows"]})
    compare("files",
            {f["path"]: f["sha"] for f in local_files(home)},
            {p: s for p, _c, s in m["files"]})
    pending = con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    for name, r in report.items():
        r["examples"] = [" / ".join(map(str, k)) if isinstance(k, tuple) else k
                         for k in r["examples"]]
        flag = "OK " if not (r["missing_on_master"] or r["different"]) else "GAP"
        print(f"{flag} {name:5}  local {r['local']:6}  master {r['master']:6}  "
              f"missing {r['missing_on_master']:5}  different {r['different']:5}  "
              f"only-on-master {r['only_on_master']:5}")
        if r["examples"]:
            print(f"      e.g. {', '.join(r['examples'])}")
    print(f"outbox pending: {pending}")
    gaps = any(r["missing_on_master"] or r["different"] for r in report.values())
    if gaps:
        print("The master does NOT hold everything. Run `python3 scripts/brain_sync.py` "
              "to push it.", file=sys.stderr)
    else:
        print("The master holds a complete copy of this brain.")
    return 1 if gaps else 0


# ---------------------------------------------------------------- pull

def _node_row(p: dict) -> tuple:
    raw = p.get("_props")
    if raw is None:          # pushed before `_props` existed: best effort
        raw = json.dumps({k: v for k, v in p.items()
                          if k not in graph.NODE_FIELDS and not k.startswith("_")},
                         ensure_ascii=False)
    ts = graph.now()
    return (p["id"], p.get("type") or "note", p.get("title") or "", p.get("body"),
            raw, p.get("status"), p.get("due"), p.get("_realm", p.get("realm")),
            p.get("created") or ts, p.get("updated") or ts)


def _edge_row(src, dst, p: dict) -> tuple:
    raw = p.get("_props")
    if raw is None:
        raw = json.dumps({k: v for k, v in p.items()
                          if k not in ("rel", "created") and not k.startswith("_")},
                         ensure_ascii=False)
    return (src, dst, p.get("rel"), raw, p.get("created") or graph.now())


def build_restore(target: Path, m: dict, upto: int = 0) -> dict:
    """Write the master's state into a brand-new database at `target`. `upto` is
    the master's clock read BEFORE the state was, so the new file's first sync
    pulls anything written while the restore ran."""
    keys = ("MEMORY_LEDGER_DB", "SECOND_BRAIN_DB")
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ[k] = str(target)
    try:
        import trigger                        # creates the trigger_* tables on db()
        con = trigger.db()
        con.executemany(
            "INSERT OR REPLACE INTO nodes(id,type,title,body,props,status,due,realm,created,updated)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)", [_node_row(p) for p in m["nodes"]])
        ids = {p["id"] for p in m["nodes"]}
        edges = [_edge_row(s, d, p) for s, d, p in m["edges"] if s in ids and d in ids]
        con.executemany("INSERT OR IGNORE INTO edges(src,dst,rel,props,created) VALUES(?,?,?,?,?)",
                        edges)
        rows = 0
        for table, key, data in m["rows"]:
            if table not in graph.mirror_tables():
                continue
            rows += graph.put_row(con, table, json.loads(data))
        graph._rebuild_blocked(con)
        # every insert above queued itself for the master it came from; drop that
        con.execute("DELETE FROM outbox")
        graph._sync_set(con, "pull_seq", upto)
        for path, _content, sha in m["files"]:
            graph._sync_set(con, "file:" + path, sha)
        con.commit()
        check = con.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {"nodes": con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
                  "edges": con.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
                  "rows": rows, "integrity": check}
        con.close()
        return counts
    finally:                              # the new file is for this call only
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def restore_files(home: Path, files, overwrite: bool) -> list[str]:
    written = []
    for path, content, _sha in files:
        rel = Path(path)
        if rel.is_absolute() or ".." in rel.parts or rel.name in SECRET_FILES:
            continue
        dest = home / rel
        if dest.exists() and not overwrite:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def pull(args, home: Path) -> int:
    live = graph.db_path()
    stamp = graph.now().replace(":", "").replace("+0000", "Z")
    target = Path(args.out).expanduser() if args.out else live.with_name(
        f"{live.name}.restored-{stamp}")
    if target.exists():
        print(f"ERROR: {target} already exists", file=sys.stderr)
        return 2
    clock, err = graph._neo4j_query("MATCH (c:SyncClock {id:'main'}) RETURN c.seq")
    if err:
        print(f"ERROR reading the master: {err}", file=sys.stderr)
        return 1
    upto = int(clock[0][0] or 0) if clock else 0
    m = master_state()
    print(f"master: {len(m['nodes'])} nodes, {len(m['edges'])} edges, "
          f"{len(m['rows'])} rows, {len(m['files'])} files")
    if not m["nodes"]:
        print("ERROR: the master holds no nodes — refusing to build an empty brain.",
              file=sys.stderr)
        return 1
    counts = build_restore(target, m, upto)
    print(f"restored into {target}: {counts['nodes']} nodes, {counts['edges']} edges, "
          f"{counts['rows']} rows (integrity: {counts['integrity']})")
    if counts["integrity"] != "ok":
        print("ERROR: the restored file failed its integrity check; nothing replaced.",
              file=sys.stderr)
        return 1
    if args.replace:
        backups = home / "backups"
        kept = []
        for suffix in ("", "-wal", "-shm", "-journal"):
            old = Path(str(live) + suffix)
            if old.exists():
                backups.mkdir(parents=True, exist_ok=True)
                dest = backups / f"{live.name}{suffix}.pre-restore-{stamp}"
                shutil.move(str(old), str(dest))
                kept.append(str(dest))
        shutil.move(str(target), str(live))
        print(f"replaced {live}" + (f" (previous file kept at {kept[0]})" if kept else ""))
    else:
        print(f"nothing replaced. To use it: --pull --replace, or move it over {live}")
    written = restore_files(home, m["files"], args.overwrite_files)
    print(f"files restored: {len(written)}"
          + ("" if args.overwrite_files else " (existing files kept; --overwrite-files to replace)"))
    return 0


# ---------------------------------------------------------------- two-way sync
#
# Down: everything the master stamped after this file's `pull_seq`, written by
# any other writer — today another brain file, tomorrow the engine writing to
# neo4j first. Up: the outbox, then any home file that changed. Pull runs
# FIRST, so a file edited on both sides is seen as a conflict before this side's
# push would overwrite the other's copy.
#
# Who wins:
#   nodes     the newer `updated`. A local edit still queued keeps an older master
#             copy out; a newer master copy replaces a local one (the push would
#             be refused by the master's guard anyway, and the two would drift).
#   edges,    carry no time, so a change still queued here wins; everything else
#   rows      takes the master's.
#   deletes   apply unless the thing was re-created or is queued here — an edit
#             beats a delete, so nothing is lost to a race.
#   files     taken when this side has not changed since the last agreement;
#             when both changed, the local file stays and the master's copy is
#             written beside it as `<name>.conflict-<machine>`.

def _pending(con):
    nodes, edges, rows = set(), set(), set()
    for kind, ref in con.execute("SELECT kind, ref FROM outbox"):
        (nodes if kind in ("node", "del_node") else
         edges if kind in ("edge", "del_edge") else rows).add(ref)
    return nodes, edges, rows


def _since_clause(since: int) -> str:
    # before the first pull, things pushed before stamps existed count as new
    if since:
        return "x._seq > $since AND x._seq <= $upto"
    return "(x._seq IS NULL OR (x._seq > 0 AND x._seq <= $upto))"


def _changes(statement: str, params: dict) -> list:
    """One query, not pages: an item re-stamped mid-read would shift a SKIP
    window and silently drop a neighbour that is never pulled again."""
    rows, err = graph._neo4j_query(statement, params, timeout=180)
    if err:
        raise RuntimeError(err)
    return rows


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def pull_changes(con, home: Path) -> dict:
    me = graph.machine_id(con)
    since = int(graph._sync_get(con, "pull_seq", "0") or 0)
    clock, err = graph._neo4j_query("MATCH (c:SyncClock {id:'main'}) RETURN c.seq")
    if err:
        raise RuntimeError(err)
    upto = int(clock[0][0] or 0) if clock else 0
    if since and upto <= since:
        return {"from": since, "to": since, "nodes": 0, "edges": 0, "rows": 0,
                "deleted": 0, "files": 0, "conflicts": []}
    cond = _since_clause(since) + " AND coalesce(x._origin, '') <> $me"
    params = {"since": since, "upto": upto, "me": me}
    nodes = [r[0] for r in _changes(f"MATCH (x:Node) WHERE {cond} RETURN properties(x)",
                                    params)]
    edges = _changes(f"MATCH (a:Node)-[x:REL]->(b:Node) WHERE {cond} "
                     "RETURN a.id, b.id, properties(x)", params)
    mrows = _changes(f"MATCH (x:MirrorRow) WHERE {cond} RETURN x.table, x.key, x.data",
                     params)
    tombs = _changes(f"MATCH (x:Tombstone) WHERE {cond} RETURN x.kind, x.ref", params)
    files = _changes(f"MATCH (x:MirrorFile) WHERE {cond} "
                     "RETURN x.path, x.content, x.sha, x._origin", params)

    p_nodes, p_edges, p_rows = _pending(con)
    counts = {"from": since, "to": upto, "nodes": 0, "edges": 0, "rows": 0,
              "deleted": 0, "files": 0, "conflicts": []}
    transitions = False
    con.execute("INSERT INTO mirror_mute VALUES(1)")
    try:
        alive_nodes = set()
        for p in nodes:
            alive_nodes.add(p["id"])
            local = con.execute("SELECT updated FROM nodes WHERE id=?", (p["id"],)).fetchone()
            if local is not None and (p.get("updated") or "") <= (local[0] or ""):
                continue
            row = _node_row(p)
            con.execute(
                "INSERT INTO nodes(id,type,title,body,props,status,due,realm,created,updated)"
                " VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET"
                " type=excluded.type, title=excluded.title, body=excluded.body,"
                " props=excluded.props, status=excluded.status, due=excluded.due,"
                " realm=excluded.realm, created=excluded.created, updated=excluded.updated",
                row)
            counts["nodes"] += 1
            transitions = transitions or row[1] == "transition"
        alive_edges = set()
        for src, dst, p in edges:
            ref = graph.EDGE_SEP.join((src, dst, p.get("rel") or ""))
            alive_edges.add(ref)
            if ref in p_edges:
                continue
            if con.execute("SELECT COUNT(*) FROM nodes WHERE id IN (?,?)",
                           (src, dst)).fetchone()[0] < (1 if src == dst else 2):
                continue                  # an endpoint this side does not hold
            con.execute(
                "INSERT INTO edges(src,dst,rel,props,created) VALUES(?,?,?,?,?)"
                " ON CONFLICT(src,dst,rel) DO UPDATE SET props=excluded.props,"
                " created=excluded.created", _edge_row(src, dst, p))
            counts["edges"] += 1
        alive_rows = set()
        for table, key, data in mrows:
            uid = table + graph.EDGE_SEP + key
            alive_rows.add(uid)
            if uid in p_rows or table not in graph.mirror_tables():
                continue
            counts["rows"] += graph.put_row(con, table, json.loads(data))
        for kind, ref in tombs:
            if kind == "node" and ref not in alive_nodes and ref not in p_nodes:
                n = con.execute("DELETE FROM nodes WHERE id=?", (ref,)).rowcount
            elif kind == "edge" and ref not in alive_edges and ref not in p_edges:
                s, d, rel = ref.split(graph.EDGE_SEP)
                n = con.execute("DELETE FROM edges WHERE src=? AND dst=? AND rel=?",
                                (s, d, rel)).rowcount
            elif kind == "row" and ref not in alive_rows and ref not in p_rows:
                table, _, key = ref.partition(graph.EDGE_SEP)
                pk = graph.mirror_tables().get(table)
                if not pk:
                    continue
                n = con.execute(f"DELETE FROM {table} WHERE "
                                + " AND ".join(f"{c}=?" for c in pk), json.loads(key)).rowcount
            else:
                continue
            counts["deleted"] += n
        if transitions:
            graph._rebuild_blocked(con)
        con.execute("DELETE FROM mirror_mute")
        graph._sync_set(con, "pull_seq", upto)
        con.commit()
    except Exception:
        con.rollback()
        raise

    for path, content, sha, origin in files:
        rel = Path(path)
        if rel.is_absolute() or ".." in rel.parts or rel.name in SECRET_FILES:
            continue
        dest = home / rel
        base = graph._sync_get(con, "file:" + path)
        local = _sha(dest.read_text(encoding="utf-8")) if dest.is_file() else None
        if local == sha:
            pass
        elif local is None or local == base:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
            counts["files"] += 1
        else:
            side = dest.with_name(f"{dest.name}.conflict-{origin or 'master'}")
            side.write_text(content, encoding="utf-8")
            counts["conflicts"].append(str(side.relative_to(home)))
        graph._sync_set(con, "file:" + path, sha)
    con.commit()
    return counts


def push_files(con, home: Path) -> int:
    changed = [f for f in local_files(home)
               if graph._sync_get(con, "file:" + f["path"]) != f["sha"]]
    for chunk in _batches(changed, FILE_BATCH):
        ok, err = graph._neo4j_call(
            graph._stamped(con, [{"statement": graph._FILE_UPSERT,
                                  "parameters": {"files": chunk}}]),
            timeout=graph.FLUSH_TIMEOUT)
        if not ok:
            raise RuntimeError(err)
        for f in chunk:
            graph._sync_set(con, "file:" + f["path"], f["sha"])
        con.commit()
    return len(changed)


def sync_now(con=None, home: Path | None = None) -> dict:
    """Pull, then push. Safe to call as often as you like; a quiet master costs
    one round-trip."""
    cfg = graph._neo4j_cfg()
    if not cfg["url"] or not cfg["password"]:
        return {"ok": False, "error": "neo4j not configured"}
    con = con or graph.connect()
    home = home or graph.home_dir()
    try:
        graph._ensure_schema_once()
        down = pull_changes(con, home)
        up = graph._flush(con)
        if not up.get("ok"):
            return {"ok": False, "error": up.get("error"), "pulled": down, "pushed": up}
        files = push_files(con, home)
    except Exception as e:           # network, or a master that said no
        return {"ok": False, "error": str(e)}
    graph._sync_set(con, "last_sync", graph.now())
    con.commit()
    return {"ok": True, "pulled": down, "pushed": up.get("pushed") or {},
            "files_pushed": files, "machine": graph.machine_id(con)}


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="show what would be sent")
    ap.add_argument("--sync", action="store_true",
                    help="two-way: pull what other writers put on the master, then push ours")
    ap.add_argument("--verify", action="store_true",
                    help="compare local and master; exit 1 on any gap")
    ap.add_argument("--pull", action="store_true",
                    help="rebuild the brain from the master into a new file")
    ap.add_argument("--replace", action="store_true",
                    help="with --pull: swap the restored file in for the live one")
    ap.add_argument("--out", help="with --pull: where to write the restored file")
    ap.add_argument("--overwrite-files", action="store_true",
                    help="with --pull: overwrite home files that already exist")
    ap.add_argument("--quiet", action="store_true", help="print errors only")
    args = ap.parse_args()

    cfg = load_cfg()
    home = graph.home_dir()
    if not args.dry_run and (not cfg["url"] or not cfg["password"]):
        print("ERROR: set url + password in ~/.second_brain/neo4j.json or env "
              "(SECOND_BRAIN_NEO4J_URL / _PASSWORD).", file=sys.stderr)
        sys.exit(2)
    if cfg["url"].startswith("http://") and not args.quiet:
        print("WARNING: endpoint is plain HTTP — creds + data go in cleartext.",
              file=sys.stderr)

    if args.pull:
        sys.exit(pull(args, home))

    con = graph.connect()
    if args.verify:
        sys.exit(verify(con, home))
    if args.sync:
        res = sync_now(con, home)
        if not args.quiet or not res.get("ok"):
            print(json.dumps(res, indent=2, default=str))
        sys.exit(0 if res.get("ok") else 1)

    nodes, edges, rows, files = (local_nodes(con), local_edges(con),
                                 local_rows(con), local_files(home))
    summary = (f"{len(nodes)} nodes, {len(edges)} edges, {len(rows)} rows, "
               f"{len(files)} files")
    if args.dry_run:
        print(f"local brain: {summary}")
        print(f"endpoint: {cfg['url'] or '(unset)'}  db: {cfg['db']}  user: {cfg['user']}")
        print("sample node:", json.dumps(nodes[0], ensure_ascii=False)[:300] if nodes else "-")
        print("files:", ", ".join(f["path"] for f in files) or "-")
        print("DRY RUN — nothing sent.")
        return
    rc = push(con, nodes, edges, rows, files)
    if rc == 0 and not args.quiet:
        print(f"synced OK -> {cfg['url']}: {summary} upserted")
    sys.exit(rc)


if __name__ == "__main__":
    main()
