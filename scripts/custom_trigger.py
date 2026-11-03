#!/usr/bin/env python3
"""Custom triggers: a poll the owner writes as blocks (docs/trigger.md#custom-triggers).

A trigger is one file, `<home>/triggers/<id>.yaml`: an interval and a short
list of blocks (`http_request`, `condition`, `transform`, `script`, `emit`,
`stop`). Each run executes the blocks in a child process of their own
(`_trigger_block_runner.py`, `python -I`, an empty environment, a timeout and
a cap on what it may print); every event an `emit` produces becomes a request
in the trigger engine's log, deduplicated by the event's id, and only then does
the trigger's checkpoint move. A crash in between re-finds the same events
next time, and the dedupe turns them into nothing.

The values of secrets live in `trigger.json` (`custom.secrets`), never in the
trigger's file: the file names them, the runner hands them to the child on
stdin, and every value is blotted out of what comes back.

    python3 scripts/trigger.py custom list
    python3 scripts/trigger.py custom validate <id>
    python3 scripts/trigger.py custom dry-run <id>
    python3 scripts/trigger.py custom run <id>
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _platform  # noqa: E402
import _trigger_block_runner as blocks  # noqa: E402
import graph  # noqa: E402
import trigger  # noqa: E402

DIR_NAME = "triggers"
RUNNER = Path(__file__).resolve().parent / "_trigger_block_runner.py"
SOURCE_PREFIX = "custom:"
MIN_INTERVAL_S = 30
MAX_EVENTS_DEFAULT = 20
OUTPUT_MAX = 1_000_000          # what the child may print; more is an error
STDERR_MAX = 64_000
LIMITS = {"timeout_s": (1, 7200, 60), "memory_mb": (32, 2048, 256)}
RETRY = {"attempts": (1, 5, 1), "backoff_s": (0, 60, 5)}
BLOCK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}")
SECRET_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_INTERVAL_RE = re.compile(r"^\s*(\d+)\s*(s|m|h|d)?\s*$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, None: 1}
REQUIRED = {"http_request": ("url",), "condition": ("if",), "emit": ("event",),
            "script": ("script",), "transform": (), "stop": ()}

SCHEMA = """
CREATE TABLE IF NOT EXISTS custom_trigger_state (
    trigger_id   TEXT PRIMARY KEY,
    last_run_at  TEXT NOT NULL DEFAULT '',
    last_status  TEXT NOT NULL DEFAULT '',
    last_error   TEXT NOT NULL DEFAULT '',
    last_run_id  TEXT NOT NULL DEFAULT '',
    updated      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS custom_trigger_run (
    run_id       TEXT PRIMARY KEY,
    trigger_id   TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL,              -- running | ok | skipped | error
    attempts     INTEGER NOT NULL DEFAULT 0,
    events       INTEGER NOT NULL DEFAULT 0, -- what the blocks emitted
    created      INTEGER NOT NULL DEFAULT 0, -- what was new, and became a task
    log          TEXT NOT NULL DEFAULT '[]',
    error        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_custom_trigger_run ON custom_trigger_run(trigger_id, started_at);
"""


# ---------------------------------------------------------------- files
def triggers_dir() -> Path:
    return graph.home_dir() / DIR_NAME


def path_of(tid: str) -> Path | None:
    """The file for `tid`, or None when the id is not a safe file name."""
    if not id_ok(tid):
        return None
    return triggers_dir() / f"{tid}.yaml"


def id_ok(tid) -> bool:
    return isinstance(tid, str) and graph.script_name_ok(tid) and "." not in tid


def parse_text(text: str):
    """YAML with PyYAML (`safe_load` only). Without it, JSON still parses:
    JSON is YAML, so a JSON-shaped trigger works on a bare Python too."""
    try:
        import yaml
    except ImportError:
        try:
            return json.loads(text)
        except ValueError:
            raise ValueError("PyYAML is not installed, so only a JSON-shaped trigger parses "
                             "here (pip install pyyaml, or run it from the server)") from None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"not valid YAML: {e}") from None


def load(tid: str) -> tuple:
    """(definition, error)."""
    p = path_of(tid)
    if p is None:
        return None, f"{tid!r} is not a trigger id: letters, digits, - and _"
    if not p.is_file():
        return None, f"no trigger {tid} ({p})"
    try:
        defn = parse_text(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"{p.name}: {e}"
    if not isinstance(defn, dict):
        return None, f"{p.name} must hold a mapping"
    return defn, ""


def is_portal_file(tid: str) -> bool:
    """A `type: portal` file shares the folder; portal_trigger.py owns it."""
    defn, err = load(tid)
    return not err and defn.get("type") == "portal"


def list_ids() -> list:
    d = triggers_dir()
    return sorted(p.stem for p in d.glob("*.yaml") if id_ok(p.stem)) if d.is_dir() else []


def set_enabled(tid: str, on: bool) -> dict:
    """Rewrite only the `enabled` line, so the owner's comments and layout
    stay as they were. A JSON-shaped file is rewritten as JSON."""
    defn, err = load(tid)
    if err:
        return {"ok": False, "error": err}
    p = path_of(tid)
    text = p.read_text(encoding="utf-8")
    word = "true" if on else "false"
    if text.lstrip().startswith("{"):
        defn["enabled"] = on
        new = json.dumps(defn, indent=2, ensure_ascii=False) + "\n"
    elif re.search(r"(?m)^enabled\s*:", text):
        new = re.sub(r"(?m)^enabled\s*:.*$", f"enabled: {word}", text, count=1)
    else:
        new = re.sub(r"(?m)^(id\s*:.*)$", rf"\1\nenabled: {word}", text, count=1)
        if new == text:
            new = f"enabled: {word}\n" + text
    p.write_text(new, encoding="utf-8")
    return {"ok": True, "id": tid, "enabled": on}


# ---------------------------------------------------------------- config
def custom_cfg(cfg: dict) -> dict:
    return cfg.get("custom") if isinstance(cfg.get("custom"), dict) else {}


def secret_values(cfg: dict) -> dict:
    s = custom_cfg(cfg).get("secrets")
    return {str(k): str(v) for k, v in s.items()} if isinstance(s, dict) else {}


def max_events(cfg: dict) -> int:
    try:
        return max(1, int(custom_cfg(cfg).get("max_events_per_run") or MAX_EVENTS_DEFAULT))
    except (TypeError, ValueError):
        return MAX_EVENTS_DEFAULT


def parse_interval(v) -> int | None:
    """`30s`, `5m`, `1h`, `1d` or a number of seconds."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    m = _INTERVAL_RE.match(str(v or ""))
    return int(m.group(1)) * _UNITS[m.group(2)] if m else None


def _bounded(section: dict, name: str, lo: int, hi: int, default: int, errors: list,
             where: str) -> int:
    v = section.get(name, default)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        errors.append(f"{where}.{name} must be a whole number from {lo} to {hi}")
        return default
    return v


def limits_of(defn: dict) -> dict:
    lim = defn.get("limits") if isinstance(defn.get("limits"), dict) else {}
    out = {k: lim.get(k, d) for k, (_, _, d) in LIMITS.items()}
    out["network"] = lim.get("network", True) is not False
    return out


def retry_of(defn: dict) -> dict:
    r = defn.get("retry") if isinstance(defn.get("retry"), dict) else {}
    return {k: r.get(k, d) for k, (_, _, d) in RETRY.items()}


# ---------------------------------------------------------------- validate
def _script_path(name: str) -> tuple:
    """(path, error) for a script block: a Python file in `<home>/scripts`."""
    if not graph.script_name_ok(name or ""):
        return None, f"{name!r} is not a script name: a bare filename in {graph.scripts_dir()}"
    path = graph.scripts_dir() / name
    if not path.is_file() or path.is_symlink():
        return None, f"no script {name} in {graph.scripts_dir()}"
    lang = graph._script_lang(name, path.read_text(encoding="utf-8", errors="replace")[:200])
    if lang != "python":
        return None, f"{name} is {lang}; a script block runs Python only"
    return str(path), ""


def _check_refs(block: dict, i: int, defn: dict, errors: list) -> None:
    bid = block.get("id") or f"#{i + 1}"
    config = defn.get("config") if isinstance(defn.get("config"), dict) else {}
    declared = set(defn.get("secrets") or []) if isinstance(defn.get("secrets"), list) else set()
    found = []
    for key, val in block.items():
        if key in ("id", "type"):
            continue
        for r in blocks.refs(val):
            found.append((key, r))
    if block.get("type") == "condition" and isinstance(block.get("if"), str):
        try:
            found += [("if", n) for n in blocks.condition_roots(block["if"])]
        except blocks.BlockError:
            pass                       # reported where the condition is checked
    for key, r in found:
        parts = blocks.parse_path(r)
        if not parts or not isinstance(parts[0], str):
            errors.append(f"block {bid}: {{{{{r}}}}} is not a path")
            continue
        root = parts[0]
        if root not in blocks.REF_ROOTS:
            errors.append(f"block {bid}: {{{{{r}}}}} — a reference starts with one of "
                          + ", ".join(x for x in blocks.REF_ROOTS))
        elif root == "prev" and i == 0:
            errors.append(f"block {bid}: the first block has no prev")
        elif root == "item" and not (block.get("type") == "emit" and "each" in block
                                     and key != "each"):
            errors.append(f"block {bid}: item is only there inside an emit with each")
        elif root == "config" and len(parts) > 1 and parts[1] not in config:
            errors.append(f"block {bid}: config.{parts[1]} is not in config")
        elif root == "secrets" and (len(parts) < 2 or parts[1] not in declared):
            errors.append(f"block {bid}: {r} is not one of the trigger's secrets")


def _check_block(block, i: int, defn: dict, ids: set, network: bool, errors: list,
                 warnings: list) -> None:
    if not isinstance(block, dict):
        errors.append(f"block #{i + 1} must be a mapping")
        return
    bid, kind = block.get("id"), block.get("type")
    if not isinstance(bid, str) or not BLOCK_ID_RE.fullmatch(bid):
        errors.append(f"block #{i + 1}: id must be letters, digits, - and _ (up to 40)")
        bid = f"#{i + 1}"
    elif bid in ids:
        errors.append(f"block {bid}: the id is used twice")
    ids.add(bid)
    if kind not in blocks.BLOCK_TYPES:
        errors.append(f"block {bid}: type must be one of {', '.join(blocks.BLOCK_TYPES)}")
        return
    for f in REQUIRED[kind]:
        if block.get(f) in (None, "", {}):
            errors.append(f"block {bid}: a {kind} block needs {f}")
    if kind == "http_request":
        if str(block.get("method") or "GET").upper() not in blocks.HTTP_METHODS:
            errors.append(f"block {bid}: method must be one of {', '.join(blocks.HTTP_METHODS)}")
        if block.get("on_error", "stop") not in ("stop", "continue"):
            errors.append(f"block {bid}: on_error is stop or continue")
        if not network:
            errors.append(f"block {bid}: limits.network is false, so no http_request")
    elif kind == "condition":
        try:
            blocks.parse_condition(block.get("if") or "")
        except blocks.BlockError as e:
            errors.append(f"block {bid}: {e}")
        for f in ("then", "else"):
            if f in block and block[f] not in blocks.FLOW:
                errors.append(f"block {bid}: {f} is continue or stop")
    elif kind == "transform":
        if not any(block.get(f) for f in ("set", "pick", "drop")):
            errors.append(f"block {bid}: a transform needs set, pick or drop")
        if "set" in block and not isinstance(block["set"], dict):
            errors.append(f"block {bid}: set is a mapping")
        for f in ("pick", "drop"):
            if f in block and not (isinstance(block[f], list)
                                   and all(isinstance(x, str) for x in block[f])):
                errors.append(f"block {bid}: {f} is a list of field names")
    elif kind == "emit":
        ev = block.get("event")
        if ev is not None and not isinstance(ev, dict):
            errors.append(f"block {bid}: event is a mapping (id, title, payload, permalink)")
        elif isinstance(ev, dict) and not str(ev.get("id") or "").strip():
            warnings.append(f"block {bid}: event has no id, so it defaults to "
                            "<trigger>:<now>, a new event every run. Give it a stable id")
    elif kind == "script":
        _, err = _script_path(str(block.get("script") or ""))
        if err and block.get("script"):
            errors.append(f"block {bid}: {err}")
    _check_refs(block, i, defn, errors)


def validate(defn, cfg: dict | None = None, expect_id: str = "") -> dict:
    """{ok, errors, warnings}. Checks the shape only: nothing runs, nothing is
    fetched."""
    cfg = cfg if cfg is not None else trigger.load_config()
    errors, warnings = [], []
    if not isinstance(defn, dict):
        return {"ok": False, "errors": ["a trigger is a mapping"], "warnings": []}
    if defn.get("type") == "portal":
        return {"ok": False, "errors": ["type: portal is not a custom trigger; use "
                                        "`trigger.py portal ...`"], "warnings": []}
    tid = defn.get("id")
    if not id_ok(tid):
        errors.append("id must be letters, digits, - and _")
    elif expect_id and tid != expect_id:
        errors.append(f"id is {tid!r}, but the file is {expect_id}.yaml")
    if "enabled" in defn and not isinstance(defn["enabled"], bool):
        errors.append("enabled is true or false")
    if defn.get("mode", "poll") != "poll":
        errors.append(f"mode {defn.get('mode')!r} is reserved; v1 runs poll only")
    secs = parse_interval(defn.get("interval"))
    if secs is None:
        errors.append("interval must look like 30s, 5m, 1h or 1d")
    elif secs < MIN_INTERVAL_S:
        errors.append(f"interval must be at least {MIN_INTERVAL_S}s")
    do = defn.get("do", "")
    if not isinstance(do, str) or len(do) > trigger.RUN_COMMAND_MAX:
        errors.append(f"do is text, up to {trigger.RUN_COMMAND_MAX} characters")
    elif trigger.unknown_variables(do):
        errors.append(trigger._unknown_msg("do", trigger.unknown_variables(do)))
    if "config" in defn and not isinstance(defn["config"], dict):
        errors.append("config is a mapping")
    names = defn.get("secrets", [])
    if not isinstance(names, list) or not all(isinstance(n, str) and SECRET_NAME_RE.fullmatch(n)
                                              for n in names):
        errors.append("secrets is a list of NAMES (capitals, digits, _); the values go in "
                      "trigger.json under custom.secrets")
    else:
        have = secret_values(cfg)
        for n in names:
            if n not in have:
                errors.append(f"secret {n} is not in {trigger.config_path()} (custom.secrets)")
    lim = defn.get("limits", {})
    if not isinstance(lim, dict):
        errors.append("limits is a mapping")
        lim = {}
    for k, (lo, hi, d) in LIMITS.items():
        _bounded(lim, k, lo, hi, d, errors, "limits")
    if "network" in lim and not isinstance(lim["network"], bool):
        errors.append("limits.network is true or false")
    retry = defn.get("retry", {})
    if not isinstance(retry, dict):
        errors.append("retry is a mapping")
        retry = {}
    for k, (lo, hi, d) in RETRY.items():
        _bounded(retry, k, lo, hi, d, errors, "retry")
    network = lim.get("network", True) is not False
    bl = defn.get("blocks")
    if not isinstance(bl, list) or not bl:
        errors.append("blocks is a list with at least one block")
        bl = []
    ids: set = set()
    for i, b in enumerate(bl):
        _check_block(b, i, defn, ids, network, errors, warnings)
    useful = [b for b in bl if isinstance(b, dict) and b.get("type") != "stop"]
    if bl and (not useful or useful[-1].get("type") != "emit"):
        warnings.append("the last block is not an emit: whatever comes after the last emit "
                        "is never seen, and a trigger with no emit always ends skipped")
    if "memory_mb" in lim and not sys.platform.startswith("linux"):
        warnings.append("limits.memory_mb is not enforced on this OS (best effort: Linux "
                        "only); the timeout and the output cap still hold")
    if not network and any(isinstance(b, dict) and b.get("type") == "script" for b in bl):
        warnings.append("limits.network: false stops http_request blocks only; a script "
                        "block can still open a connection")
    return {"ok": not errors, "errors": errors, "warnings": warnings}


# ---------------------------------------------------------------- store
def db():
    con = trigger.db()
    con.executescript(SCHEMA)
    con.commit()
    return con


def get_state(con, tid: str) -> dict:
    r = con.execute("SELECT * FROM custom_trigger_state WHERE trigger_id=?", (tid,)).fetchone()
    return dict(r) if r else {}


def _set_state(con, tid: str, **fields) -> None:
    fields["updated"] = trigger.now_iso()
    cols = ", ".join(fields)
    con.execute(
        f"INSERT INTO custom_trigger_state(trigger_id, {cols}) "
        f"VALUES(?, {', '.join('?' for _ in fields)}) "
        f"ON CONFLICT(trigger_id) DO UPDATE SET "
        + ", ".join(f"{k}=excluded.{k}" for k in fields),
        (tid, *fields.values()))
    con.commit()


def runs(con, tid: str, limit: int = 20) -> list:
    out = []
    for r in con.execute("SELECT * FROM custom_trigger_run WHERE trigger_id=? "
                         "ORDER BY started_at DESC LIMIT ?", (tid, max(1, int(limit)))):
        d = dict(r)
        try:
            d["log"] = json.loads(d["log"] or "[]")
        except ValueError:
            d["log"] = []
        out.append(d)
    return out


def is_due(con, defn: dict, now: datetime | None = None) -> bool:
    last = trigger._parse_dt(get_state(con, defn["id"]).get("last_run_at") or "")
    if last is None:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - last).total_seconds() >= (parse_interval(defn.get("interval")) or 0)


# ---------------------------------------------------------------- isolation
def redact(value, secrets: list):
    """Every secret's value, wherever it shows up, as ***."""
    if not secrets:
        return value
    if isinstance(value, str):
        for s in secrets:
            value = value.replace(s, "***")
        return value
    if isinstance(value, dict):
        return {redact(k, secrets): redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    return value


def child_env() -> dict:
    """Nothing of ours: a PATH, and what Windows needs to start Python and
    open a socket."""
    env = {"PATH": os.defpath}
    if _platform.WINDOWS:
        for k in ("SYSTEMROOT", "SystemRoot", "WINDIR", "TEMP", "TMP"):
            if os.environ.get(k):
                env[k] = os.environ[k]
    return env


def run_isolated(ctx: dict, timeout: int) -> dict:
    """Run the blocks in `python -I`. The result, or an `error` one: a
    timeout (the whole tree is killed), output over the cap, a crash, or
    anything that is not the JSON the runner writes."""
    argv = [sys.executable, "-I", "-X", "utf8", str(RUNNER)]
    cwd = graph.scripts_dir() if graph.scripts_dir().is_dir() else graph.home_dir()
    try:
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=child_env(), cwd=str(cwd),
                             **_platform.group_kwargs())
    except OSError as e:
        return {"status": "error", "error": f"could not start the runner: {e}", "log": []}
    _platform.track(p)
    out, err, over = bytearray(), bytearray(), threading.Event()

    def read(stream, buf, cap, kill):
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            if len(buf) + len(chunk) > cap:
                buf += chunk[:max(0, cap - len(buf))]
                if kill:
                    over.set()
                    _platform.kill_tree(p)
                    return
                continue               # stderr: keep draining, keep nothing more
            buf += chunk

    readers = [threading.Thread(target=read, args=(p.stdout, out, OUTPUT_MAX, True), daemon=True),
               threading.Thread(target=read, args=(p.stderr, err, STDERR_MAX, False), daemon=True)]
    for t in readers:
        t.start()
    timed_out = False
    try:
        try:
            p.stdin.write(json.dumps(ctx, ensure_ascii=False, default=str).encode("utf-8"))
            p.stdin.close()
        except OSError:
            pass                       # it died at once; its exit code says why
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _platform.kill_tree(p)
            p.wait()
        for t in readers:
            t.join(timeout=5)
    finally:
        _platform.untrack(p)
    tail = err.decode("utf-8", "replace").strip()[-1000:]
    if timed_out:
        return {"status": "error", "error": f"killed after {timeout}s", "log": []}
    if over.is_set():
        return {"status": "error", "error": f"printed more than {OUTPUT_MAX} bytes", "log": []}
    if p.returncode != 0:
        return {"status": "error", "error": f"exit {p.returncode}: {tail or 'no output'}",
                "log": []}
    try:
        res = json.loads(out.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {"status": "error", "error": f"the runner's answer is not JSON: {tail}", "log": []}
    if not isinstance(res, dict) or res.get("status") not in ("ok", "skipped", "error"):
        return {"status": "error", "error": "the runner's answer has no status", "log": []}
    return res


def context(defn: dict, cfg: dict, checkpoint: str) -> dict:
    """What the child gets on stdin. Secrets go here and nowhere else."""
    have = secret_values(cfg)
    scripts = {}
    for b in defn.get("blocks") or []:
        if isinstance(b, dict) and b.get("type") == "script":
            path, _ = _script_path(str(b.get("script") or ""))
            if path:
                scripts[b["script"]] = path
    return {"trigger": defn, "checkpoint": checkpoint or "", "now": trigger.now_iso(),
            "secrets": {n: have[n] for n in defn.get("secrets") or [] if n in have},
            "scripts": scripts, "max_events": max_events(cfg),
            "memory_mb": limits_of(defn)["memory_mb"]}


# ---------------------------------------------------------------- execute
def event_of(defn: dict, ev: dict, run_id: str) -> dict:
    """One emitted event as a trigger request. Found by the owner's own
    trigger: it goes in on the subscribed path, and opens nothing for anyone."""
    tid = defn["id"]
    source = SOURCE_PREFIX + tid
    return {"source": source, "event_id": str(ev.get("id") or "")[:500], "scope": source,
            "requester_id": source, "requester_name": f"custom trigger {tid}",
            "is_owner": False, "permalink": str(ev.get("permalink") or ""),
            "text": str(ev.get("title") or ""), "subscribed": True,
            "meta": {"kind": "custom", "trigger_id": tid, "run_id": run_id,
                     "title": str(ev.get("title") or ""), "payload": ev.get("payload"),
                     "do": str(defn.get("do") or ""), "where": f"custom trigger {tid}",
                     "at": trigger.now_iso()}}


def run_now(tid: str, **kw) -> dict:
    """A run by hand (the CLI, the page): under the trigger's lock, so it never
    overlaps a scheduled run of the same trigger."""
    lock = _lock_for(tid)
    if not lock.acquire(blocking=False):
        return {"ok": False, "id": tid, "status": "busy", "error": f"{tid} is running already"}
    try:
        return execute(tid, **kw)
    finally:
        lock.release()


def execute(tid: str, *, cfg: dict | None = None, dry_run: bool = False, dispatch=None,
            scheduled: bool = False, con=None, sleep=time.sleep) -> dict:
    """One run of one trigger, end to end. `scheduled` skips a trigger that is
    not due; by hand it always runs. `dry_run` runs the blocks and writes
    nothing at all: no task, no checkpoint, no run row, no state."""
    cfg = cfg if cfg is not None else trigger.load_config()
    defn, err = load(tid)
    # A dry run reads the checkpoint and writes no row anywhere.
    con = con if con is not None else db()
    if err:
        if not dry_run:
            _set_state(con, tid, last_status="invalid", last_error=err[:500])
        return {"ok": False, "id": tid, "status": "invalid", "error": err}
    v = validate(defn, cfg, expect_id=tid)
    if not v["ok"]:
        if not dry_run:
            _set_state(con, tid, last_status="invalid", last_error="; ".join(v["errors"])[:500])
        return {"ok": False, "id": tid, "status": "invalid", **v}
    if scheduled and not is_due(con, defn):
        return {"ok": True, "id": tid, "status": "not_due"}
    source = SOURCE_PREFIX + tid
    checkpoint = trigger.get_checkpoint(con, source)
    run_id = str(uuid.uuid4())
    started = trigger.now_iso()
    if not dry_run:
        con.execute("INSERT INTO custom_trigger_run(run_id, trigger_id, started_at, status) "
                    "VALUES(?,?,?,'running')", (run_id, tid, started))
        _set_state(con, tid, last_run_at=started, last_status="running", last_error="",
                   last_run_id=run_id)
    ctx = context(defn, cfg, checkpoint)
    hidden = sorted((s for s in ctx["secrets"].values() if s), key=len, reverse=True)
    retry = retry_of(defn)
    timeout = limits_of(defn)["timeout_s"]
    log, res = [], {}
    for attempt in range(1, retry["attempts"] + 1):
        res = redact(run_isolated(ctx, timeout), hidden)
        log.append({"attempt": attempt, "status": res.get("status"),
                    "error": res.get("error", ""), "blocks": res.get("log") or []})
        if res.get("status") != "error":
            break
        if attempt < retry["attempts"]:
            sleep(retry["backoff_s"])
    status, events = res.get("status", "error"), res.get("events") or []
    if dry_run:
        return {"ok": status != "error", "id": tid, "dry_run": True, "status": status,
                "events": [event_of(defn, e, run_id) for e in events],
                "checkpoint": {"now": checkpoint, "would_be": res.get("checkpoint")},
                "error": res.get("error", ""), "log": log, "warnings": v["warnings"]}
    created, seen = [], 0
    if status != "error":
        for e in events:
            req = trigger._new_request(con, event_of(defn, e, run_id), "queued",
                                       "subscribed: custom")
            if req is None:
                seen += 1
            else:
                created.append(req["id"])
        # Only now: a crash before this line re-finds the same events, and
        # the (source, event_id) dedupe makes the second pass a no-op.
        if res.get("checkpoint") is not None:
            trigger.set_checkpoint(con, source, str(res["checkpoint"]), only_forward=False)
    finished = trigger.now_iso()
    con.execute("UPDATE custom_trigger_run SET finished_at=?, status=?, attempts=?, events=?, "
                "created=?, log=?, error=? WHERE run_id=?",
                (finished, status, len(log), len(events), len(created),
                 json.dumps(log, ensure_ascii=False)[:200_000], str(res.get("error") or "")[:2000],
                 run_id))
    _set_state(con, tid, last_status=status, last_error=str(res.get("error") or "")[:500])
    con.commit()
    (trigger.log.warning if status == "error" else trigger.log.info)(
        "custom %s run %s %s: %d event(s), %d new%s", tid, run_id[:8], status, len(events),
        len(created), f" — {res.get('error')}" if status == "error" else "")
    for rid in created:
        if dispatch:
            dispatch(rid)
    return {"ok": status != "error", "id": tid, "run_id": run_id, "status": status,
            "events": len(events), "created": created, "seen": seen,
            "error": res.get("error", ""), "attempts": len(log)}


def summary(tid: str, con=None) -> dict:
    defn, err = load(tid)
    st = get_state(con, tid) if con is not None else {}
    row = {"id": tid, "file": str(path_of(tid)), "state": st}
    if err:
        return {**row, "ok": False, "error": err}
    return {**row, "ok": True, "name": str(defn.get("name") or tid),
            "enabled": defn.get("enabled") is True, "interval": defn.get("interval"),
            "blocks": len(defn.get("blocks") or [])}


# ---------------------------------------------------------------- schedule
_locks: dict = {}
_locks_guard = threading.Lock()


def _lock_for(tid: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(tid, threading.Lock())


def run_due(cfg: dict, dispatch, con=None) -> list:
    """One scheduler pass: every enabled trigger that is due, one after the
    other, each file read afresh so an edit counts without a restart. A
    trigger that does not load is noted in its state and the rest still run;
    one already running (a manual run from the page) is left to finish."""
    con = con if con is not None else db()
    done = []
    for tid in list_ids():
        defn, err = load(tid)
        if err:
            _set_state(con, tid, last_status="invalid", last_error=err[:500])
            continue
        if defn.get("type") == "portal" or defn.get("enabled") is not True:
            continue
        lock = _lock_for(tid)
        if not lock.acquire(blocking=False):
            done.append({"ok": True, "id": tid, "status": "busy"})
            continue
        try:
            done.append(execute(tid, cfg=cfg, dispatch=dispatch, scheduled=True, con=con))
        except Exception as e:     # one trigger's crash must not stop the others
            _set_state(con, tid, last_status="error", last_error=f"crashed: {e}"[:500])
            done.append({"ok": False, "id": tid, "status": "error", "error": str(e)})
        finally:
            lock.release()
    return [r for r in done if r.get("status") != "not_due"]


# ---------------------------------------------------------------- the page
TEXT_MAX = 64_000
TEMPLATE = """id: {id}
name: {id}
enabled: false
mode: poll
interval: 5m
# What the brain does with each event. {{event_title}} and {{event_payload}} are there.
do: "Tell me that {{event_title}}."
config:
  url: https://example.com/health
secrets: []
blocks:
  - id: check
    type: http_request
    url: "{{{{config.url}}}}"
    on_error: continue
  - id: down
    type: condition
    if: "prev.status == None or prev.status >= 400"
    then: continue
    else: stop
  - id: alert
    type: emit
    event:
      id: "{id}:{{{{prev.status}}}}"
      title: "{id}: status {{{{prev.status}}}}"
      payload: {{status: "{{{{prev.status}}}}"}}
"""


def template(tid: str = "my-trigger") -> str:
    return TEMPLATE.format(id=tid if id_ok(tid) else "my-trigger")


def raw_text(tid: str) -> str:
    p = path_of(tid)
    return p.read_text(encoding="utf-8") if p is not None and p.is_file() else ""


def check_text(tid: str, text: str, cfg: dict | None = None) -> dict:
    """Parse and validate what the page holds, without writing it."""
    if not id_ok(tid):
        return {"ok": False, "errors": [f"{tid!r} is not a trigger id: letters, digits, - "
                                        "and _"], "warnings": []}
    if len(text or "") > TEXT_MAX:
        return {"ok": False, "errors": [f"a trigger is at most {TEXT_MAX} characters"],
                "warnings": []}
    try:
        defn = parse_text(text or "")
    except ValueError as e:
        return {"ok": False, "errors": [str(e)], "warnings": []}
    return validate(defn, cfg, expect_id=tid)


def save_text(tid: str, text: str, cfg: dict | None = None) -> dict:
    """Write the file, as written, only when it validates."""
    res = check_text(tid, text, cfg)
    if not res["ok"]:
        return {**res, "id": tid, "saved": False,
                "error": "not saved: " + "; ".join(res["errors"])[:500]}
    triggers_dir().mkdir(parents=True, exist_ok=True)
    existed = path_of(tid).is_file()
    path_of(tid).write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return {**res, "id": tid, "saved": True, "created": not existed}


def detail(tid: str, con=None, limit: int = 20) -> dict:
    """One trigger for the page: the file as written, its verdict, state,
    checkpoint and newest runs."""
    con = con if con is not None else db()
    text = raw_text(tid)
    if not text:
        return {"ok": False, "error": f"no trigger {tid}"}
    return {"ok": True, "id": tid, "text": text, **check_text(tid, text),
            "state": get_state(con, tid),
            "checkpoint": trigger.get_checkpoint(con, SOURCE_PREFIX + tid),
            "runs": runs(con, tid, limit)}
