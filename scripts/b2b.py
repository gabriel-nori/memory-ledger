#!/usr/bin/env python3
"""B2B (Brain 2 Brain): this brain's side of the relays (docs/b2b.md).

A relay (`services/b2b/`, a **broker**) holds messages between brains, in
memory, and deletes each one the moment it is read. This module is what the
brain does with them:

* keeps this brain's **identity**, an X25519 key pair, in `<home>/b2b.json`,
  written mode 600. The **user id** is the public key, and is what others send
  to; the private key never leaves this brain;
* keeps the **brokers** it uses, several if you like, in the same file, each
  with a name, a URL and its own switch;
* keeps the **peers**, the other brains this one talks to, in `graph.db`
  (`b2b_peer`): each is a user id, the broker that reaches it, and optionally
  the `person` node it belongs to;
* keeps the **allowed list** (`b2b_allowed`), the user ids that may send to
  this brain, like friends on a social network. Adding a peer adds it here
  too; either list can change without the other. Only an allowed sender's
  message runs anything: one from anyone else is logged `blocked`, with its
  text, and goes no further unless the owner reprocesses it from Tasks;
* **logs in** to a broker by answering its challenge: the broker seals a secret
  to the user id, this brain opens it with the private key and sends it back,
  and gets a retrieval token for an hour, kept in this process's memory;
* **sends**: the message is sealed here, to the peer's key and signed by this
  brain's, before it goes to the peer's broker; logged in `b2b_sent`. It
  carries whether a human wrote it: only the Messages chat says so, and every
  other sender (a session, a script, this command line) is automated;
* **polls** every enabled broker on the interval, from the trigger watchers in
  the server (`brain_api/trigger_runtime.py`), opens what arrived, and turns
  each message into a request in the same log as the Slack, Jira and GitHub
  triggers, so it shows in Tasks and runs the action you chose.

The broker deletes a message once read, so the request row is the only copy.

Stdlib, except the keys: `b2b_crypto` needs `cryptography`, from the server's
venv. Run from a plain `python3`, this command line re-runs itself there.

    python3 scripts/b2b.py status
    python3 scripts/b2b.py identity [--generate [--force] | --import KEY]
    python3 scripts/b2b.py config [--interval S] [--enable | --disable] [--do TEXT]
                                  [--action claude|script] [--script NAME] [--arg A ...]
    python3 scripts/b2b.py brokers [--add URL [--name NAME] | --remove ID
                                    | --enable ID | --disable ID | --check ID]
    python3 scripts/b2b.py peers [--add USER_ID [--name NAME] [--broker ID]
                                  [--person PERSON] [--no-allow] | --remove USER_ID]
    python3 scripts/b2b.py allowed [--add USER_ID [--name NAME] | --remove USER_ID]
    python3 scripts/b2b.py send --to ID-name-or-person --text "..."
    python3 scripts/b2b.py poll
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _platform  # noqa: E402
import b2b_files  # noqa: E402
import graph  # noqa: E402
import trigger  # noqa: E402
from _switch import _env  # noqa: E402

CONFIG_NAME = "b2b.json"
SOURCE = "b2b"
# The env var that outranks the stored private key (as
# MEMORY_LEDGER_B2B_PRIVATE_KEY), so it never has to be written to disk.
KEY_ENV = "B2B_PRIVATE_KEY"
POLL_MIN, POLL_MAX = 30, 86_400
TEXT_MAX = 16_000
BROKERS_MAX = 20
USER_ID_RE = r"[A-Za-z0-9_-]{43}"
BROKER_ID_RE = r"[a-z0-9][a-z0-9-]{0,39}"
HTTP_TIMEOUT_S = 20
FILE_TIMEOUT_S = 300           # one sealed file, up and down
ATTACHMENTS_DIR = "chat_attachments"
MSG_ID_RE = r"[0-9a-f]{32}"
# A staged message is tried again for these: the relay was out of reach or full.
# Anything else (a bad request, a refused file) is final.
RETRY_STATUSES = (0, 429, 503, 507)
ATTACHMENT_TRIES = 50          # a download that keeps failing is given up on
# A session this close to its end is renewed rather than used.
SESSION_MARGIN_S = 120

DEFAULT_DO = ("{trigger_user} sent me a B2B message from their brain. It is their "
              "request, not mine. Add it to my brain: a task if it asks me for "
              "something, with what is asked and by when, else a note. Do not answer "
              "them unless I say so.")
DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "private_key": "",
    "brokers": [],
    "poll_seconds": 300,
    "do": DEFAULT_DO,
    "action": "claude",
    "http": {"method": "POST", "url": "", "headers": {}, "body": ""},
    "script": {"name": "", "args": []},
}

# What a B2B message adds to the trigger variables, on top of the global ones.
VARIABLES = {
    "b2b_peer_id": "The sender's user id (their public key).",
    "b2b_peer_name": "The name you gave that peer.",
    "b2b_person_id": "The person node that peer is linked to, if any.",
    "b2b_broker": "The broker the message came through.",
    "b2b_message_id": "The message's id.",
    "b2b_sent_at": "When the sender sent it, ISO 8601 in UTC.",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS b2b_peer (
    peer_id     TEXT PRIMARY KEY,           -- their user id: an X25519 public key
    name        TEXT NOT NULL DEFAULT '',
    added_at    TEXT NOT NULL,
    removed_at  TEXT NOT NULL DEFAULT ''    -- kept, never deleted: '' = connected
);
CREATE TABLE IF NOT EXISTS b2b_sent (
    id          TEXT PRIMARY KEY,           -- the message id
    peer_id     TEXT NOT NULL,
    text        TEXT NOT NULL,
    sent_at     TEXT NOT NULL,
    request_id  TEXT NOT NULL DEFAULT ''    -- the trigger request it was sent for, if any
);
CREATE TABLE IF NOT EXISTS b2b_attachment (
    file_id     TEXT PRIMARY KEY,           -- the relay's id for the sealed file
    message_id  TEXT NOT NULL,
    peer_id     TEXT NOT NULL,              -- the sender
    broker_id   TEXT NOT NULL,
    key         TEXT NOT NULL DEFAULT '',   -- cleared once downloaded
    nonce       TEXT NOT NULL DEFAULT '',
    sha256      TEXT NOT NULL,
    names       TEXT NOT NULL DEFAULT '[]', -- JSON list, as the sender named them
    state       TEXT NOT NULL DEFAULT 'pending',   -- pending | done | failed
    error       TEXT NOT NULL DEFAULT '',
    tries       INTEGER NOT NULL DEFAULT 0,
    dir         TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS b2b_allowed (
    user_id     TEXT PRIMARY KEY,           -- a brain that may send to this one
    name        TEXT NOT NULL DEFAULT '',
    added_at    TEXT NOT NULL,
    removed_at  TEXT NOT NULL DEFAULT ''    -- kept, never deleted: '' = allowed
);
"""
# Added after the first version of the tables; `db()` adds them when missing.
LATE_COLUMNS = {
    "b2b_peer": {"broker_id": "TEXT NOT NULL DEFAULT ''",     # the broker that reaches them
                 "person_id": "TEXT NOT NULL DEFAULT ''"},    # the person node they are
    "b2b_sent": {"broker_id": "TEXT NOT NULL DEFAULT ''",
                 "human": "INTEGER NOT NULL DEFAULT 0",      # typed in the Messages chat
                 "status": "TEXT NOT NULL DEFAULT 'sent'",   # staged | sent | failed
                 "files": "TEXT NOT NULL DEFAULT ''"},       # JSON list of attached names
}


class B2BError(Exception):
    """A broker could not be reached, or refused the call."""


def _crypto():
    """`b2b_crypto`, imported where a key is first used: it is the one part of
    the engine that needs a dependency. Raises `B2BError` when it is missing."""
    try:
        import b2b_crypto
    except ImportError as e:
        raise B2BError(f"B2B needs the `cryptography` package ({e}); run it from the "
                       "server (scripts/serve.py), or `python3 scripts/b2b.py`, which "
                       "uses the server's environment") from None
    return b2b_crypto


# ---------------------------------------------------------------- config
def config_path() -> Path:
    """`<home>/b2b.json`."""
    return graph.home_dir() / CONFIG_NAME


def _raw() -> dict[str, Any]:
    """The file as written, or {} (`ValueError` when it does not parse)."""
    p = config_path()
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{p} must hold a JSON object")
    return raw


def load_config() -> dict[str, Any]:
    """The defaults under `<home>/b2b.json`. A file that does not parse is
    reported under `_problem` and treated as absent, so a typo cannot start
    the poller on half a config."""
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        raw = _raw()
    except (OSError, ValueError) as e:
        cfg["_problem"] = f"{config_path()} is not valid JSON: {e}"
        return cfg
    for k, v in raw.items():
        if k in DEFAULTS:
            cfg[k] = v
    if not isinstance(cfg.get("brokers"), list):
        cfg["brokers"] = []
    return cfg


def private_key(cfg: dict[str, Any]) -> str:
    """This brain's private key: the env var, else the file."""
    return _env(KEY_ENV) or str(cfg.get("private_key") or "")


def user_id(cfg: dict[str, Any]) -> str:
    """This brain's user id, the public key of its private key; "" with none."""
    key = private_key(cfg)
    if not key:
        return ""
    try:
        return _crypto().user_id_of(key)
    except Exception:
        return ""


def brokers(cfg: dict[str, Any], only_enabled: bool = False) -> list[dict[str, Any]]:
    """The brokers configured, well-formed ones only."""
    out = []
    for b in cfg.get("brokers") or []:
        if isinstance(b, dict) and b.get("id") and b.get("url"):
            if not only_enabled or b.get("enabled", True):
                out.append({"id": str(b["id"]), "name": str(b.get("name") or b["id"]),
                            "url": str(b["url"]), "enabled": bool(b.get("enabled", True))})
    return out


def get_broker(cfg: dict[str, Any], broker_id: str) -> dict[str, Any] | None:
    """One broker by id, or None."""
    return next((b for b in brokers(cfg) if b["id"] == broker_id), None)


def active(cfg: dict[str, Any]) -> bool:
    """Whether the poller should run: switched on, with an identity and an
    enabled broker, on a config that parses."""
    return bool(cfg.get("enabled")) and bool(user_id(cfg)) \
        and bool(brokers(cfg, only_enabled=True)) and "_problem" not in cfg


def poll_seconds(cfg: dict[str, Any]) -> int:
    """The poll interval, clamped to what a broker can reasonably take."""
    try:
        n = int(cfg.get("poll_seconds") or DEFAULTS["poll_seconds"])
    except (TypeError, ValueError):
        n = DEFAULTS["poll_seconds"]
    return max(POLL_MIN, min(POLL_MAX, n))


def config_summary(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """The configuration as the page and the CLI show it. The private key never
    leaves: only whether one is set, and where from."""
    cfg = cfg or load_config()
    act = trigger._action_of(cfg)
    return {
        "path": str(config_path()),
        "exists": config_path().exists(),
        "problem": cfg.get("_problem", ""),
        "enabled": bool(cfg.get("enabled")),
        "active": active(cfg),
        "user_id": user_id(cfg),
        "private_key": {"set": bool(private_key(cfg)),
                        "source": "env" if _env(KEY_ENV) else
                        "file" if cfg.get("private_key") else "",
                        "env_var": "MEMORY_LEDGER_" + KEY_ENV},
        "brokers": [{**b, "session": _session_state(b, user_id(cfg))} for b in brokers(cfg)],
        "poll_seconds": poll_seconds(cfg),
        "do": str(cfg.get("do") or ""),
        **act,
        "variables": VARIABLES,
    }


def _write(raw: dict[str, Any]) -> None:
    """Write `<home>/b2b.json` atomically, mode 600: it holds the private key."""
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(raw, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    _platform.owner_only(tmp)   # mode 600, or an owner-only ACL on Windows
    os.replace(tmp, p)


def _clean_url(url: Any) -> str:
    """A broker URL without its trailing slash, or "" when it is not one."""
    url = str(url or "").strip().rstrip("/")
    return url if re.fullmatch(r"https?://[^\s/]+(/[^\s]*)?", url) else ""


def _clean_brokers(value: Any) -> tuple[list[dict[str, Any]], str]:
    """A whole broker list, validated: `(brokers, "")` or `([], why)`."""
    if not isinstance(value, list):
        return [], "brokers must be a list"
    if len(value) > BROKERS_MAX:
        return [], f"at most {BROKERS_MAX} brokers"
    out, seen = [], set()
    for b in value:
        if not isinstance(b, dict):
            return [], "each broker must be an object"
        url = _clean_url(b.get("url"))
        if not url:
            return [], "each broker needs an http(s) url"
        bid = str(b.get("id") or "").strip().lower() or _slug(str(b.get("name") or ""), url,
                                                             seen)
        if not re.fullmatch(BROKER_ID_RE, bid):
            return [], f"broker id {bid!r} must be lowercase letters, digits and -"
        if bid in seen:
            return [], f"two brokers are called {bid}"
        seen.add(bid)
        if "enabled" in b and not isinstance(b["enabled"], bool):
            return [], "a broker's enabled must be true or false"
        out.append({"id": bid, "name": str(b.get("name") or bid).strip()[:80], "url": url,
                    "enabled": b.get("enabled", True)})
    return out, ""


def _slug(name: str, url: str, taken: set[str]) -> str:
    """A broker id from its name, else its host, not already taken."""
    base = re.sub(r"[^a-z0-9]+", "-", (name or re.sub(r"^https?://", "", url)).lower())
    base = base.strip("-")[:32] or "broker"
    bid, n = base, 2
    while bid in taken:
        bid, n = f"{base}-{n}", n + 1
    return bid


def save_config(patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a validated patch into `<home>/b2b.json`.

    `brokers`, when sent, replaces the whole list. The private key is
    write-only, as tokens are in trigger.json: a non-empty value replaces the
    stored one, and `clear: ["private_key"]` removes it."""
    if not isinstance(patch, dict):
        return {"ok": False, "error": "the config must be an object"}
    try:
        raw = _raw()
    except (OSError, ValueError):
        return {"ok": False, "error": f"{config_path()} is not valid JSON; fix or remove it "
                                      "first"}
    for old in ("server_url", "user_id", "token"):
        raw.pop(old, None)          # the version-1 account: a shared-secret login, gone
    if "enabled" in patch:
        if not isinstance(patch["enabled"], bool):
            return {"ok": False, "error": "enabled must be true or false"}
        raw["enabled"] = patch["enabled"]
    if "brokers" in patch:
        got, err = _clean_brokers(patch["brokers"])
        if err:
            return {"ok": False, "error": err}
        raw["brokers"] = got
    if "poll_seconds" in patch:
        v = patch["poll_seconds"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not POLL_MIN <= v <= POLL_MAX:
            return {"ok": False, "error": f"poll_seconds must be a number from {POLL_MIN} "
                                          f"to {POLL_MAX}"}
        raw["poll_seconds"] = int(v)
    if "do" in patch:
        do = str(patch["do"] or "").strip()
        bad = trigger.unknown_variables(do, VARIABLES)
        if bad:
            return {"ok": False, "error": trigger._unknown_msg("do", bad)}
        raw["do"] = do[:trigger.RUN_COMMAND_MAX]
    row = {k: raw[k] for k in ("action", "http", "script") if k in raw}
    err = trigger._clean_action(patch, row, "b2b", VARIABLES)
    if err:
        return {"ok": False, "error": err}
    raw.update(row)
    key = patch.get("private_key")
    if isinstance(key, str) and key.strip():
        try:
            _crypto().user_id_of(key.strip())
        except B2BError as e:
            return {"ok": False, "error": str(e)}
        except Exception:
            return {"ok": False, "error": "private_key is not an X25519 private key "
                                          "(32 bytes, base64url)"}
        raw["private_key"] = key.strip()
    clear = patch.get("clear") or []
    if not isinstance(clear, list) or any(c != "private_key" for c in clear):
        return {"ok": False, "error": "clear may only name private_key"}
    if clear:
        raw.pop("private_key", None)
    merged = {**load_config(), **raw}
    if merged.get("enabled"):
        err = trigger._action_complete(merged, "b2b")
        if err:
            return {"ok": False, "error": err}
    _write(raw)
    _forget_sessions()
    return {"ok": True, "config": config_summary(load_config())}


def generate_identity(force: bool = False) -> dict[str, Any]:
    """Make this brain's key pair and store it. Refuses to replace one already
    set unless `force`: the old id would stop working, and messages still
    waiting for it on a broker could not be opened."""
    cfg = load_config()
    if private_key(cfg) and not force:
        return {"ok": False, "error": f"this brain already is {user_id(cfg)}; pass --force to "
                                      "replace it (messages waiting for the old id are lost)"}
    try:
        key, uid = _crypto().generate()
    except B2BError as e:
        return {"ok": False, "error": str(e)}
    res = save_config({"private_key": key})
    if not res["ok"]:
        return res
    return {"ok": True, "user_id": uid,
            "note": "Share your user id with the brains that should reach you. The private "
                    "key stays in b2b.json: back it up, it cannot be recovered."}


def add_broker(url: str, name: str = "", enabled: bool = True) -> dict[str, Any]:
    """Add a broker to the list. Returns the new list."""
    cfg = load_config()
    cur = brokers(cfg)
    clean = _clean_url(url)
    if not clean:
        return {"ok": False, "error": "a broker needs an http(s) url"}
    if any(b["url"] == clean for b in cur):
        return {"ok": False, "error": f"{clean} is already a broker"}
    new = {"id": _slug(name, clean, {b["id"] for b in cur}), "name": name or "",
           "url": clean, "enabled": enabled}
    return save_config({"brokers": cur + [new]})


def update_broker(broker_id: str, **changes: Any) -> dict[str, Any]:
    """Change or (`remove=True`) drop one broker."""
    cfg = load_config()
    cur = brokers(cfg)
    if not any(b["id"] == broker_id for b in cur):
        return {"ok": False, "error": f"no broker {broker_id}"}
    if changes.pop("remove", False):
        cur = [b for b in cur if b["id"] != broker_id]
    else:
        cur = [{**b, **{k: v for k, v in changes.items() if v is not None}}
               if b["id"] == broker_id else b for b in cur]
    return save_config({"brokers": cur})


# ---------------------------------------------------------------- broker
def _call(method: str, url: str, body: dict[str, Any] | None = None,
          token: str = "") -> dict[str, Any]:
    """One JSON call to a broker. Raises `B2BError` with the broker's own
    reason when it refuses, or why it could not be reached; `.status` is the
    HTTP code when there was one."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            why = json.loads(e.read() or b"{}").get("error") or e.reason
        except ValueError:
            why = e.reason
        err = B2BError(f"broker said {e.code}: {why}")
        err.status = e.code  # type: ignore[attr-defined]
        raise err from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise B2BError(f"could not reach the broker at {url}: {e}") from None


def _call_raw(method: str, url: str, data: bytes | None = None,
              token: str = "") -> tuple[bytes, dict[str, str]]:
    """One call to a broker with a raw body and a raw answer (a sealed file).
    Raises `B2BError` as `_call` does; the headers come back lowercased."""
    headers = {"Content-Type": "application/octet-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=FILE_TIMEOUT_S) as r:
            return r.read(), {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        try:
            why = json.loads(e.read() or b"{}").get("error") or e.reason
        except ValueError:
            why = e.reason
        err = B2BError(f"broker said {e.code}: {why}")
        err.status = e.code  # type: ignore[attr-defined]
        raise err from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise B2BError(f"could not reach the broker at {url}: {e}") from None


# Retrieval tokens, per broker and identity, for this process only: an hour
# long, and a new challenge is cheap, so they are never written down.
_SESSIONS: dict[tuple[str, str], tuple[str, float]] = {}
_SESSIONS_LOCK = threading.Lock()


def _forget_sessions() -> None:
    with _SESSIONS_LOCK:
        _SESSIONS.clear()


def _session_state(broker: dict[str, Any], uid: str) -> dict[str, Any]:
    """Whether this process holds a live retrieval token for a broker."""
    with _SESSIONS_LOCK:
        got = _SESSIONS.get((broker["url"], uid))
    if not got or got[1] <= time.time():
        return {"open": False}
    return {"open": True, "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                      time.gmtime(got[1]))}


def login(cfg: dict[str, Any], broker: dict[str, Any], renew: bool = False) -> str:
    """A retrieval token for `broker`: the one held, or a new one from its
    challenge. Raises `B2BError`."""
    key = private_key(cfg)
    if not key:
        raise B2BError("no identity: run `b2b.py identity --generate`")
    c = _crypto()
    uid = c.user_id_of(key)
    slot = (broker["url"], uid)
    with _SESSIONS_LOCK:
        got = _SESSIONS.get(slot)
    if got and not renew and got[1] - SESSION_MARGIN_S > time.time():
        return got[0]
    ch = _call("POST", broker["url"] + "/challenge", {"user_id": uid})
    try:
        answer = c.open_challenge(key, str(ch.get("challenge_id") or ""),
                                  ch.get("challenge") or {})
    except c.BadBox as e:
        raise B2BError(f"broker {broker['id']}: {e}") from None
    s = _call("POST", broker["url"] + "/session",
              {"user_id": uid, "challenge_id": ch["challenge_id"], "answer": answer})
    tok = str(s.get("token") or "")
    if not tok:
        raise B2BError(f"broker {broker['id']} did not return a retrieval token")
    life = int(s.get("expires_in") or 3600)
    with _SESSIONS_LOCK:
        _SESSIONS[slot] = (tok, time.time() + life)
    return tok


def _authed(cfg: dict[str, Any], broker: dict[str, Any], method: str, path: str,
            body: dict[str, Any] | None = None) -> dict[str, Any]:
    """A call with a retrieval token, logging in again once when the broker
    no longer knows it (it restarted, or the hour is up)."""
    tok = login(cfg, broker)
    try:
        return _call(method, broker["url"] + path, body, tok)
    except B2BError as e:
        if getattr(e, "status", 0) != 401:
            raise
    return _call(method, broker["url"] + path, body, login(cfg, broker, renew=True))


def _authed_raw(cfg: dict[str, Any], broker: dict[str, Any], method: str,
                path: str) -> tuple[bytes, dict[str, str]]:
    """`_authed` for a raw answer."""
    tok = login(cfg, broker)
    try:
        return _call_raw(method, broker["url"] + path, None, tok)
    except B2BError as e:
        if getattr(e, "status", 0) != 401:
            raise
    return _call_raw(method, broker["url"] + path, None, login(cfg, broker, renew=True))


def check_broker(broker_id: str) -> dict[str, Any]:
    """Log in to one broker now: proves it is reachable and takes this id."""
    cfg = load_config()
    broker = get_broker(cfg, broker_id)
    if not broker:
        return {"ok": False, "error": f"no broker {broker_id}"}
    try:
        _call("GET", broker["url"] + "/health")
        login(cfg, broker, renew=True)
    except B2BError as e:
        return {"ok": False, "error": str(e), "broker": broker_id}
    return {"ok": True, "broker": broker_id, "user_id": user_id(cfg),
            "session": _session_state(broker, user_id(cfg))}


# ---------------------------------------------------------------- peers
def db() -> sqlite3.Connection:
    """The brain, with the trigger tables and B2B's own."""
    con = trigger.db()
    fresh = not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='b2b_allowed'").fetchone()
    con.executescript(SCHEMA)
    for table, cols in LATE_COLUMNS.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    if fresh:
        # Before the allowed list, every connected peer could send: keep that.
        con.execute("INSERT OR IGNORE INTO b2b_allowed(user_id, name, added_at) "
                    "SELECT peer_id, name, added_at FROM b2b_peer WHERE removed_at=''")
    con.commit()
    return con


def _person(con: sqlite3.Connection, person_id: str) -> dict[str, Any] | None:
    r = con.execute("SELECT id, title FROM nodes WHERE id=? AND type='person'",
                    (person_id,)).fetchone()
    return {"id": r["id"], "title": r["title"]} if r else None


def _with_person(con: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    p = _person(con, d.get("person_id") or "") if d.get("person_id") else None
    d["person_title"] = p["title"] if p else ""
    return d


def list_peers(con: sqlite3.Connection, include_removed: bool = False) -> list[dict[str, Any]]:
    """The peers, connected ones first, then by name, with the person each is."""
    q = "SELECT * FROM b2b_peer" + ("" if include_removed else " WHERE removed_at=''")
    return [_with_person(con, r) for r in con.execute(
        q + " ORDER BY removed_at!='', name, peer_id")]


def get_peer(con: sqlite3.Connection, peer_id: str) -> dict[str, Any] | None:
    """One connected peer, or None."""
    r = con.execute("SELECT * FROM b2b_peer WHERE peer_id=? AND removed_at=''",
                    (peer_id,)).fetchone()
    return _with_person(con, r) if r else None


def resolve_peer(con: sqlite3.Connection, who: str) -> dict[str, Any] | None:
    """A connected peer by id, else by name, else by the person it is linked
    to (by node id or name), when that is unique."""
    who = (who or "").strip()
    peer = get_peer(con, who)
    if peer:
        return peer
    live = list_peers(con)
    for match in (lambda p: p["name"].lower() == who.lower(),
                  lambda p: p["person_id"] and p["person_id"] == who,
                  lambda p: p["person_title"] and p["person_title"].lower() == who.lower()):
        rows = [p for p in live if match(p)]
        if len(rows) == 1:
            return rows[0]
    res = graph.resolve_person(con, who) if who else {"ok": False}
    if res.get("ok"):
        rows = [p for p in live if p["person_id"] == res["id"]]
        if len(rows) == 1:
            return rows[0]
    return None


def add_peer(con: sqlite3.Connection, peer_id: str, name: str = "", broker_id: str = "",
             person_id: str = "", allow: bool = True) -> dict[str, Any]:
    """Connect a peer by user id, reached through `broker_id` (the only broker
    when there is one), linked to a person node when `person_id` is given (an
    id, or a name that resolves to one person). Reconnecting a removed one
    restores its row; what is given replaces what is stored. Unless `allow`
    is False, the peer also goes on the allowed list, so it can send back."""
    peer_id = (peer_id or "").strip()
    if not re.fullmatch(USER_ID_RE, peer_id):
        return {"ok": False, "error": "a peer's user id is their public key: 43 base64url "
                                      "characters"}
    cfg = load_config()
    if peer_id == user_id(cfg):
        return {"ok": False, "error": "that is this brain's own user id"}
    known = brokers(cfg)
    broker_id = (broker_id or "").strip()
    if not broker_id:
        old = get_peer(con, peer_id) or {}
        broker_id = old.get("broker_id") or (known[0]["id"] if len(known) == 1 else "")
    if not broker_id:
        return {"ok": False, "error": "say which broker reaches this peer"
                                      + (": " + ", ".join(b["id"] for b in known) if known
                                         else "; add one first")}
    if not any(b["id"] == broker_id for b in known):
        return {"ok": False, "error": f"no broker {broker_id}"}
    person_id = (person_id or "").strip()
    if person_id and not _person(con, person_id):
        res = graph.resolve_person(con, person_id)
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error") or f"no person {person_id}",
                    "candidates": res.get("candidates", [])}
        person_id = res["id"]
    name = (name or "").strip()[:80]
    if not name and person_id:
        name = _person(con, person_id)["title"]  # type: ignore[index]
    con.execute(
        "INSERT INTO b2b_peer(peer_id, name, added_at, broker_id, person_id) "
        "VALUES(?,?,?,?,?) ON CONFLICT(peer_id) DO UPDATE SET removed_at='', "
        "broker_id=excluded.broker_id, "
        "name=CASE WHEN excluded.name!='' THEN excluded.name ELSE b2b_peer.name END, "
        "person_id=CASE WHEN excluded.person_id!='' THEN excluded.person_id "
        "ELSE b2b_peer.person_id END",
        (peer_id, name, graph.now(), broker_id, person_id))
    con.commit()
    if allow:
        allow_sender(con, peer_id, get_peer(con, peer_id)["name"])  # type: ignore[index]
    return {"ok": True, "peer": get_peer(con, peer_id)}


def remove_peer(con: sqlite3.Connection, peer_id: str) -> dict[str, Any]:
    """Disconnect a peer. The row is kept, marked removed, as history."""
    cur = con.execute("UPDATE b2b_peer SET removed_at=? WHERE peer_id=? AND removed_at=''",
                      (graph.now(), (peer_id or "").strip()))
    con.commit()
    if not cur.rowcount:
        return {"ok": False, "error": f"no connected peer {peer_id}"}
    return {"ok": True, "peer_id": peer_id}


# ---------------------------------------------------------------- allowed list
def list_allowed(con: sqlite3.Connection, include_removed: bool = False) -> list[dict[str, Any]]:
    """Who may send to this brain, allowed ones first, then by name. Each says
    whether it is also a connected peer."""
    q = ("SELECT a.*, p.peer_id IS NOT NULL AS is_peer FROM b2b_allowed a "
         "LEFT JOIN b2b_peer p ON p.peer_id=a.user_id AND p.removed_at=''"
         + ("" if include_removed else " WHERE a.removed_at=''")
         + " ORDER BY a.removed_at!='', a.name, a.user_id")
    return [{**dict(r), "is_peer": bool(r["is_peer"])} for r in con.execute(q)]


def is_allowed(con: sqlite3.Connection, uid: str) -> bool:
    return con.execute("SELECT 1 FROM b2b_allowed WHERE user_id=? AND removed_at=''",
                       (uid,)).fetchone() is not None


def allow_sender(con: sqlite3.Connection, uid: str, name: str = "") -> dict[str, Any]:
    """Let `uid` send to this brain. Allowing a removed one restores it; a name
    given replaces the stored one."""
    uid = (uid or "").strip()
    if not re.fullmatch(USER_ID_RE, uid):
        return {"ok": False, "error": "a user id is a public key: 43 base64url characters"}
    if uid == user_id(load_config()):
        return {"ok": False, "error": "that is this brain's own user id"}
    name = (name or "").strip()[:80]
    if not name:
        name = (get_peer(con, uid) or {}).get("name", "")
    con.execute(
        "INSERT INTO b2b_allowed(user_id, name, added_at) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET removed_at='', "
        "name=CASE WHEN excluded.name!='' THEN excluded.name ELSE b2b_allowed.name END",
        (uid, name, graph.now()))
    con.commit()
    return {"ok": True, "user_id": uid}


def disallow_sender(con: sqlite3.Connection, uid: str) -> dict[str, Any]:
    """Stop `uid` from sending: its next messages are logged `blocked`. A peer
    it is stays a peer, so this brain can still send to it."""
    cur = con.execute("UPDATE b2b_allowed SET removed_at=? WHERE user_id=? AND removed_at=''",
                      (graph.now(), (uid or "").strip()))
    con.commit()
    if not cur.rowcount:
        return {"ok": False, "error": f"{uid} is not on the allowed list"}
    return {"ok": True, "user_id": uid}


# ---------------------------------------------------------------- send
def attachments_root() -> Path:
    """`<home>/chat_attachments`: one folder per chat, named by the peer's user id."""
    return graph.home_dir() / ATTACHMENTS_DIR


def _chat_dir(peer_id: str, *parts: str) -> Path:
    """`<home>/chat_attachments/<peer_id>/...`. The ids come from other brains, so
    each is checked to be what it claims before it becomes a path."""
    if not re.fullmatch(USER_ID_RE, peer_id) or any(
            not re.fullmatch(MSG_ID_RE, p) for p in parts if p != "sent"):
        raise B2BError("not a chat or message id")
    return attachments_root().joinpath(peer_id, *parts)


def sent_dir(peer_id: str, message_id: str) -> Path:
    """Where what this brain sent to `peer_id` is kept, to send it again if
    something went wrong. Never cleaned by the brain: that is the owner's."""
    return _chat_dir(peer_id, "sent", message_id)


def received_dir(peer_id: str, message_id: str) -> Path:
    """Where the attachments of a message from `peer_id` are unpacked."""
    return _chat_dir(peer_id, message_id)


def _write_private(path: Path, data: bytes) -> None:
    """A file only the owner reads, written whole or not at all."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def send(to: str, text: str, request_id: str = "", human: bool = False,
         files: list[str] | None = None) -> dict[str, Any]:
    """Seal `text` to a connected peer (by id, name or person) and leave it on
    their broker, and log it. Sent from a trigger session (`request_id`, or
    `$MEMORY_LEDGER_REQUEST_ID`), it is also an action on that request.

    `human` says a person typed it (the Messages chat). It travels sealed with
    the text, so the recipient can show who wrote it; anything else is a bot.

    `files` (paths) are zipped, sealed with a key of their own and left on the
    broker; the key and the file's id travel inside the sealed message. The
    zip is kept in `sent_dir`. When the broker is out of reach or refuses for
    room (their box is full), the message stays **staged** and `poll` sends it
    later; the answer is `ok` with `staged: true`."""
    text = (text or "").strip()
    if not text and not files:
        return {"ok": False, "error": "missing text"}
    if len(text) > TEXT_MAX:
        return {"ok": False, "error": f"text is over {TEXT_MAX} characters"}
    cfg, con = load_config(), db()
    peer = resolve_peer(con, to)
    if not peer:
        return {"ok": False, "error": f"{to} is not a connected peer; add it with "
                                      "`b2b.py peers --add <their user id> --name <name>`"}
    broker = get_broker(cfg, peer["broker_id"])
    if not broker:
        return {"ok": False, "error": f"{peer['name'] or peer['peer_id']} is reached through "
                                      f"broker {peer['broker_id'] or '(none)'}, which is not "
                                      "configured"}
    sent_at = trigger.now_iso()
    rid = request_id or os.environ.get("MEMORY_LEDGER_REQUEST_ID", "")
    if files:
        return _send_with_files(cfg, con, peer, broker, text, human, files, sent_at, rid)
    try:
        c = _crypto()
        try:
            env = c.seal(private_key(cfg), peer["peer_id"], {"text": text, "sent_at": sent_at,
                                                             "human": bool(human)})
        except (c.BadBox, ValueError) as e:
            raise B2BError(f"cannot seal to {peer['peer_id']}: {e}") from None
        got = _call("POST", broker["url"] + "/messages",
                    {"from": env["from"], "to": peer["peer_id"], "envelope": env})
    except B2BError as e:
        return {"ok": False, "error": str(e)}
    con.execute("INSERT OR IGNORE INTO b2b_sent(id, peer_id, text, sent_at, request_id, "
                "broker_id, human) VALUES(?,?,?,?,?,?,?)",
                (env["id"], peer["peer_id"], text, sent_at, rid, broker["id"],
                 1 if human else 0))
    con.commit()
    return _sent_result(con, peer, broker, env["id"], sent_at, got.get("sent_at"), human, rid)


def _sent_result(con: sqlite3.Connection, peer: dict[str, Any], broker: dict[str, Any],
                 msg_id: str, sent_at: str, queued_at: Any, human: bool,
                 rid: str) -> dict[str, Any]:
    """Log the send on its request, and the answer `send` gives."""
    if rid and trigger.get_request(con, rid):
        trigger.log_action(con, rid, "b2b-send", f"b2b:{peer['peer_id']}",
                           f"sent a B2B message to {peer['name'] or peer['peer_id']} "
                           f"through {broker['id']}")
    return {"ok": True, "id": msg_id, "to": peer["peer_id"], "name": peer["name"],
            "broker": broker["id"], "sent_at": sent_at, "queued_at": queued_at,
            "human": bool(human)}


def _send_with_files(cfg: dict[str, Any], con: sqlite3.Connection, peer: dict[str, Any],
                     broker: dict[str, Any], text: str, human: bool, files: list[str],
                     sent_at: str, rid: str) -> dict[str, Any]:
    """Zip, keep and stage a message with attachments, then try to deliver it."""
    try:
        data, names = b2b_files.zip_files(files)
    except b2b_files.BadArchive as e:
        return {"ok": False, "error": str(e)}
    msg_id, file_id = uuid.uuid4().hex, uuid.uuid4().hex
    c = _crypto()
    meta = {"id": msg_id, "peer_id": peer["peer_id"], "broker_id": broker["id"],
            "text": text, "human": bool(human), "sent_at": sent_at, "request_id": rid,
            "file": {"id": file_id, "key": c.b64(os.urandom(32)), "nonce": c.b64(os.urandom(12)),
                     "names": names, "size": len(data)}}
    d = sent_dir(peer["peer_id"], msg_id)
    _write_private(d / "attachments.zip", data)
    _write_private(d / "message.json", json.dumps(meta, indent=2).encode("utf-8"))
    con.execute("INSERT INTO b2b_sent(id, peer_id, text, sent_at, request_id, broker_id, human, "
                "status, files) VALUES(?,?,?,?,?,?,?,?,?)",
                (msg_id, peer["peer_id"], text, sent_at, rid, broker["id"], 1 if human else 0,
                 "staged", json.dumps(names)))
    con.commit()
    return deliver_staged(cfg, con, msg_id)


def deliver_staged(cfg: dict[str, Any], con: sqlite3.Connection,
                   msg_id: str) -> dict[str, Any]:
    """Try to deliver one staged message: upload its sealed file, then leave the
    message. Whatever the broker says that means "not now" (unreachable, full,
    over its quota) leaves it staged; anything else marks it `failed`. The same
    ids are used on every try, so a retry is never a second message."""
    row = con.execute("SELECT * FROM b2b_sent WHERE id=? AND status='staged'",
                      (msg_id,)).fetchone()
    if not row:
        return {"ok": False, "error": f"no staged message {msg_id}"}
    try:
        meta = json.loads((sent_dir(row["peer_id"], msg_id) / "message.json")
                          .read_text(encoding="utf-8"))
        zipped = (sent_dir(row["peer_id"], msg_id) / "attachments.zip").read_bytes()
    except (OSError, ValueError, B2BError) as e:
        return _mark_failed(con, msg_id, f"the staged files are unreadable: {e}")
    broker = get_broker(cfg, row["broker_id"])
    if not broker:
        return {"ok": True, "staged": True, "id": msg_id, "to": row["peer_id"],
                "reason": f"broker {row['broker_id']} is not configured"}
    f = meta["file"]
    c = _crypto()
    try:
        blob = c.seal_file(c.unb64(f["key"]), c.unb64(f["nonce"]), f["id"], zipped)
        sha = hashlib.sha256(blob).hexdigest()
        me = c.user_id_of(private_key(cfg))
        _call_raw("POST", f"{broker['url']}/files?id={f['id']}&from={me}"
                          f"&to={row['peer_id']}&sha256={sha}", blob)
        env = c.seal(private_key(cfg), row["peer_id"], {
            "text": meta["text"], "sent_at": meta["sent_at"], "human": bool(meta["human"]),
            "files": [{"id": f["id"], "key": f["key"], "nonce": f["nonce"], "sha256": sha,
                       "size": len(blob), "names": f["names"]}]}, msg_id=msg_id)
        try:
            got = _call("POST", broker["url"] + "/messages",
                        {"from": env["from"], "to": row["peer_id"], "envelope": env})
        except B2BError as e:
            if "already waiting" not in str(e):      # a retry of one that did arrive
                raise
            got = {}
    except (B2BError, c.BadBox, ValueError) as e:
        if isinstance(e, B2BError) and getattr(e, "status", 0) in RETRY_STATUSES:
            return {"ok": True, "staged": True, "id": msg_id, "to": row["peer_id"],
                    "broker": broker["id"], "reason": str(e)}
        return _mark_failed(con, msg_id, str(e))
    con.execute("UPDATE b2b_sent SET status='sent' WHERE id=?", (msg_id,))
    con.commit()
    peer = get_peer(con, row["peer_id"]) or {"peer_id": row["peer_id"], "name": ""}
    return {**_sent_result(con, peer, broker, msg_id, row["sent_at"], got.get("sent_at"),
                           bool(row["human"]), row["request_id"]),
            "staged": False, "files": f["names"]}


def _mark_failed(con: sqlite3.Connection, msg_id: str, why: str) -> dict[str, Any]:
    """A staged message that cannot be sent, ever: kept in the log, not retried."""
    con.execute("UPDATE b2b_sent SET status='failed' WHERE id=?", (msg_id,))
    con.commit()
    return {"ok": False, "error": why, "id": msg_id}


def flush_staged(cfg: dict[str, Any], con: sqlite3.Connection) -> dict[str, list]:
    """One try at every staged message, oldest first."""
    out: dict[str, list] = {"sent": [], "staged": [], "failed": []}
    for r in con.execute("SELECT id FROM b2b_sent WHERE status='staged' ORDER BY sent_at"
                         ).fetchall():
        res = deliver_staged(cfg, con, r["id"])
        out["failed" if not res.get("ok") else "staged" if res.get("staged") else "sent"
            ].append(r["id"])
    return out


def resend(msg_id: str) -> dict[str, Any]:
    """Send a message again as a new one, with the files kept in its `sent_dir`."""
    con = db()
    row = con.execute("SELECT * FROM b2b_sent WHERE id=?", (msg_id,)).fetchone()
    if not row:
        return {"ok": False, "error": f"no sent message {msg_id}"}
    zipped = sent_dir(row["peer_id"], msg_id) / "attachments.zip"
    if not zipped.is_file():
        return send(row["peer_id"], row["text"], human=bool(row["human"]))
    try:
        names = json.loads(row["files"] or "[]")
        with tempfile.TemporaryDirectory(prefix="b2b-resend-") as tmp:
            b2b_files.extract_zip(zipped.read_bytes(), Path(tmp) / "x")
            return send(row["peer_id"], row["text"], human=bool(row["human"]),
                        files=[str(Path(tmp) / "x" / n) for n in names])
    except (OSError, ValueError, b2b_files.BadArchive) as e:
        return {"ok": False, "error": f"cannot read the kept files: {e}"}


def list_sent(con: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    """The messages this brain sent, newest first."""
    return [dict(r) for r in con.execute(
        "SELECT * FROM b2b_sent ORDER BY sent_at DESC LIMIT ?", (max(1, min(500, limit)),))]


# ---------------------------------------------------------------- receive
def message_event(msg: dict[str, Any], peer: dict[str, Any] | None,
                  cfg: dict[str, Any], broker: dict[str, Any]) -> dict[str, Any]:
    """One opened message as a trigger event: what `trigger._new_request` logs."""
    sender = str(msg.get("from") or "")
    name = (peer or {}).get("name") or (peer or {}).get("person_title") or sender
    meta = {"kind": "message", "where": f"B2B message from {name} via {broker['name']}",
            "at": str(msg.get("sent_at") or ""),
            # Typed by a person on the sender's side, not a session or a script.
            # An envelope from before the flag says nothing, and reads as a bot.
            "human": msg.get("human") is True,
            "vars": {"b2b_peer_id": sender, "b2b_peer_name": (peer or {}).get("name", ""),
                     "b2b_person_id": (peer or {}).get("person_id", ""),
                     "b2b_broker": broker["id"],
                     "b2b_message_id": str(msg.get("id") or ""),
                     "b2b_sent_at": str(msg.get("sent_at") or "")}}
    trigger._stamp(meta, {**cfg, "do": str(cfg.get("do") or "")})
    return {"source": SOURCE, "event_id": str(msg.get("id") or ""), "scope": f"b2b:{sender}",
            "requester_id": sender, "requester_name": name, "is_owner": False,
            "permalink": "", "text": str(msg.get("text") or "")[:TEXT_MAX], "meta": meta}


def _file_refs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The attachments an opened message declares, only the well-formed ones."""
    out = []
    for f in payload.get("files") or []:
        if isinstance(f, dict) and re.fullmatch(MSG_ID_RE, str(f.get("id") or "")) \
                and re.fullmatch(r"[0-9a-f]{64}", str(f.get("sha256") or "")) \
                and f.get("key") and f.get("nonce"):
            out.append(f)
    return out[:1]


def _note_attachment(con: sqlite3.Connection, broker: dict[str, Any], msg: dict[str, Any],
                     ref: dict[str, Any]) -> None:
    names = [str(n)[:255] for n in (ref.get("names") or []) if isinstance(n, str)][:200]
    con.execute("INSERT OR IGNORE INTO b2b_attachment(file_id, message_id, peer_id, broker_id, "
                "key, nonce, sha256, names, received_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (ref["id"], msg["id"], msg["from"], broker["id"], ref["key"], ref["nonce"],
                 ref["sha256"], json.dumps(names), trigger.now_iso()))
    con.commit()


def _attachment_state(con: sqlite3.Connection, file_id: str, state: str, error: str = "",
                      **cols: Any) -> None:
    sets = {"state": state, "error": error[:500], **cols}
    con.execute(f"UPDATE b2b_attachment SET {', '.join(k + '=?' for k in sets)} WHERE file_id=?",
                (*sets.values(), file_id))
    con.commit()


def download_attachment(cfg: dict[str, Any], con: sqlite3.Connection,
                        file_id: str) -> dict[str, Any]:
    """Fetch one pending attachment, check it against the hash the sealed message
    carried, open it, unpack it (refusing a zip-slip or a zip-bomb) into
    `received_dir`, and only then delete it from the relay. Unreachable or full
    (`RETRY_STATUSES`) leaves it `pending` for the next pass; anything else, or
    `ATTACHMENT_TRIES` failures, marks it `failed`. The key is cleared once done."""
    row = con.execute("SELECT * FROM b2b_attachment WHERE file_id=? AND state='pending'",
                      (file_id,)).fetchone()
    if not row:
        return {"ok": False, "error": f"no pending attachment {file_id}"}
    broker = get_broker(cfg, row["broker_id"])
    if not broker:
        return {"ok": False, "pending": True, "error": f"broker {row['broker_id']} is gone"}
    c = _crypto()
    try:
        blob, headers = _authed_raw(cfg, broker, "GET", f"/files/{file_id}")
        if hashlib.sha256(blob).hexdigest() != row["sha256"] \
                or headers.get("x-sha256", row["sha256"]) != row["sha256"]:
            raise b2b_files.BadArchive("the file does not match its hash")
        data = c.open_file(c.unb64(row["key"]), c.unb64(row["nonce"]), file_id, blob)
        dest = received_dir(row["peer_id"], row["message_id"])
        written = b2b_files.extract_zip(data, dest)
    except B2BError as e:
        tries = row["tries"] + 1
        if getattr(e, "status", 0) in RETRY_STATUSES and tries < ATTACHMENT_TRIES:
            _attachment_state(con, file_id, "pending", str(e), tries=tries)
            return {"ok": False, "pending": True, "error": str(e)}
        _attachment_state(con, file_id, "failed", str(e), tries=tries, key="", nonce="")
        return {"ok": False, "error": str(e)}
    except (c.BadBox, b2b_files.BadArchive, ValueError) as e:
        _attachment_state(con, file_id, "failed", str(e), tries=row["tries"] + 1,
                          key="", nonce="")
        try:     # it will never be read; do not leave it on the relay
            _authed_raw(cfg, broker, "DELETE", f"/files/{file_id}")
        except B2BError:
            pass
        return {"ok": False, "error": str(e)}
    try:
        _authed_raw(cfg, broker, "DELETE", f"/files/{file_id}")
    except B2BError as e:    # kept there until it expires; the copy here is good
        trigger.log.info("b2b %s: could not delete file %s: %s", broker["id"], file_id, e)
    _attachment_state(con, file_id, "done", "", key="", nonce="", dir=str(dest))
    return {"ok": True, "dir": str(dest), "files": written}


def retry_attachments(cfg: dict[str, Any], con: sqlite3.Connection) -> list[str]:
    """One more try at every attachment still pending; the ids that arrived."""
    got = []
    for r in con.execute("SELECT file_id FROM b2b_attachment WHERE state='pending' "
                         "ORDER BY received_at").fetchall():
        if download_attachment(cfg, con, r["file_id"]).get("ok"):
            got.append(r["file_id"])
    return got


def refuse_sender(cfg: dict[str, Any], broker: dict[str, Any], sender: str) -> bool:
    """Tell the relay this brain refuses `sender`: what they left here, messages
    and files, is deleted there. Best effort: a relay that cannot be reached
    still holds it until it expires, and the next blocked message tries again."""
    try:
        _authed(cfg, broker, "POST", "/refusals", {"from": sender})
        return True
    except B2BError as e:
        trigger.log.info("b2b %s: could not refuse %s: %s", broker["id"], sender, e)
        return False


def _attach(cfg: dict[str, Any], con: sqlite3.Connection, broker: dict[str, Any],
            msg: dict[str, Any], ref: dict[str, Any], ev: dict[str, Any]) -> None:
    """Take a message's attachment now, and say in the request what came of it."""
    try:
        received_dir(msg["from"], msg["id"])
    except B2BError:
        ev["text"] += "\n\n[attachment ignored: the message id is not valid]"
        return
    _note_attachment(con, broker, msg, ref)
    res = download_attachment(cfg, con, ref["id"])
    names = ", ".join(str(n) for n in ref.get("names") or [])
    if res.get("ok"):
        ev["meta"]["vars"].update({"b2b_attachments_dir": res["dir"]})
        ev["text"] += f"\n\n[attachments in {res['dir']}: {names}]"
    elif res.get("pending"):
        ev["text"] += f"\n\n[attachments not downloaded yet, will be retried: {names}]"
    else:
        ev["text"] += f"\n\n[attachments could not be taken: {res.get('error')}]"


def _poll_broker(cfg: dict[str, Any], broker: dict[str, Any], con: sqlite3.Connection,
                 dispatch: Callable[[str], None] | None, out: dict[str, list]) -> None:
    """Read one broker (which deletes what it returns), open each envelope and
    log it as a request."""
    c = _crypto()
    got = _authed(cfg, broker, "GET", "/messages")
    key = private_key(cfg)
    refused: set[str] = set()
    for item in got.get("messages") or []:
        env = item.get("envelope") if isinstance(item, dict) else None
        if not isinstance(env, dict) or not env.get("id"):
            continue
        try:
            payload = c.open_envelope(key, env)
        except c.BadBox as e:
            # Not sealed to this key, or not by the brain it claims: there is
            # no sender to believe, so nothing to log it as. Counted, dropped.
            out["unreadable"].append(str(env.get("id")))
            trigger.log.info("b2b %s: dropped message %s: %s", broker["id"], env.get("id"), e)
            continue
        msg = {"id": env["id"], "from": env["from"], "text": payload.get("text", ""),
               "sent_at": payload.get("sent_at") or item.get("at", ""),
               "human": payload.get("human") is True}
        peer = get_peer(con, msg["from"])
        ev = message_event(msg, peer, cfg, broker)
        if is_allowed(con, msg["from"]):
            refs = _file_refs(payload)
            if refs:
                _attach(cfg, con, broker, msg, refs[0], ev)
            req = trigger._new_request(con, ev, "queued", "b2b: allowed sender")
            if req:
                out["queued"].append(req["id"])
                trigger.log.info("new request %s  b2b from %s via %s -> queued", req["id"],
                                 ev["requester_name"], broker["id"])
                if dispatch:
                    dispatch(req["id"])
        else:
            req = trigger._new_request(con, ev, "blocked", "b2b: not on the allowed list")
            if req:
                trigger._set_request(con, req["id"], finished_at=trigger.now_iso())
                out["blocked"].append(req["id"])
                trigger.log.info("new request %s  b2b from %s via %s -> blocked: not allowed",
                                 req["id"], ev["requester_id"], broker["id"])
            if msg["from"] not in refused and refuse_sender(cfg, broker, msg["from"]):
                refused.add(msg["from"])


def poll(cfg: dict[str, Any] | None = None,
         dispatch: Callable[[str], None] | None = None) -> dict[str, Any]:
    """One pass over every enabled broker. An allowed sender's message is
    queued and handed to `dispatch`; anyone else's is logged `blocked` and runs
    nothing. One broker failing does not stop the others: it is reported in
    `errors`, and the pass is `ok` only when none failed."""
    cfg = cfg or load_config()
    if cfg.get("_problem"):
        return {"ok": False, "error": cfg["_problem"]}
    if not private_key(cfg):
        return {"ok": False, "error": "no identity: run `b2b.py identity --generate`"}
    todo = brokers(cfg, only_enabled=True)
    if not todo:
        return {"ok": False, "error": "no enabled broker"}
    con = db()
    out: dict[str, list] = {"queued": [], "blocked": [], "unreadable": [], "downloaded": []}
    errors: dict[str, str] = {}
    for broker in todo:
        try:
            _poll_broker(cfg, broker, con, dispatch, out)
        except B2BError as e:
            errors[broker["id"]] = str(e)
    flushed = flush_staged(cfg, con)
    out["downloaded"] = retry_attachments(cfg, con)
    res: dict[str, Any] = {"ok": not errors, "received": len(out["queued"]) + len(out["blocked"]),
                           **out, "errors": errors, "staged": flushed}
    if errors:
        res["error"] = "; ".join(f"{k}: {v}" for k, v in errors.items())
    return res


# ---------------------------------------------------------------- conversations
def _received(con: sqlite3.Connection, peer_id: str = "") -> list[sqlite3.Row]:
    sql = ("SELECT id, event_id, requester_id, requester_name, text, meta, status, "
           "received_at FROM trigger_request WHERE source=?")
    args: list[Any] = [SOURCE]
    if peer_id:
        sql += " AND requester_id=?"
        args.append(peer_id)
    return con.execute(sql, args).fetchall()


def _in_message(r: sqlite3.Row) -> dict[str, Any]:
    try:
        meta = json.loads(r["meta"] or "{}")
    except ValueError:
        meta = {}
    return {"id": r["event_id"], "peer_id": r["requester_id"], "direction": "in",
            "text": r["text"], "at": str(meta.get("at") or r["received_at"]),
            "received_at": r["received_at"], "human": meta.get("human") is True,
            "status": r["status"], "request_id": r["id"]}


def _out_message(r: sqlite3.Row) -> dict[str, Any]:
    return {"id": r["id"], "peer_id": r["peer_id"], "direction": "out", "text": r["text"],
            "at": r["sent_at"], "human": bool(r["human"]), "status": r["status"],
            "request_id": r["request_id"], "broker_id": r["broker_id"],
            "files": json.loads(r["files"] or "[]")}


def conversations(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """Everyone this brain messaged or was messaged by, plus connected peers it
    has not talked to yet: newest conversation first, the silent ones last."""
    peers = {p["peer_id"]: p for p in list_peers(con, True)}
    allowed = {a["user_id"]: a for a in list_allowed(con, True)}
    last: dict[str, dict[str, Any]] = {}
    count: dict[str, int] = {}
    names: dict[str, str] = {}
    rows = _received(con)
    msgs = [_in_message(r) for r in rows]
    for r in rows:
        if r["requester_name"] and r["requester_name"] != r["requester_id"]:
            names.setdefault(r["requester_id"], r["requester_name"])
    msgs += [_out_message(r) for r in con.execute("SELECT * FROM b2b_sent")]
    for m in msgs:
        pid = m["peer_id"]
        count[pid] = count.get(pid, 0) + 1
        if pid not in last or m["at"] > last[pid]["at"]:
            last[pid] = m
    ids = set(last) | {pid for pid, p in peers.items() if not p["removed_at"]}
    out = []
    for pid in ids:
        peer, ok = peers.get(pid), allowed.get(pid)
        name = ((peer or {}).get("name") or (peer or {}).get("person_title")
                or (ok or {}).get("name") or names.get(pid, ""))
        m = last.get(pid)
        out.append({"peer_id": pid, "name": name,
                    "person_id": (peer or {}).get("person_id", ""),
                    "connected": bool(peer and not peer["removed_at"]),
                    "allowed": bool(ok and not ok["removed_at"]),
                    "messages": count.get(pid, 0),
                    "last": {k: m[k] for k in ("direction", "text", "at", "human")} if m else None})
    out.sort(key=lambda c: (c["name"].lower(), c["peer_id"]))
    out.sort(key=lambda c: c["last"]["at"] if c["last"] else "", reverse=True)
    return out


THREAD_PAGE = 20          # what the Messages chat is given at a time


def _attachments_of(con: sqlite3.Connection, peer_id: str) -> dict[str, dict[str, Any]]:
    """What came with each message from `peer_id`, by message id."""
    out = {}
    for r in con.execute("SELECT message_id, names, state, error, dir FROM b2b_attachment "
                         "WHERE peer_id=?", (peer_id,)):
        out[r["message_id"]] = {"names": json.loads(r["names"] or "[]"), "state": r["state"],
                                "error": r["error"], "dir": r["dir"]}
    return out


def thread(con: sqlite3.Connection, peer_id: str, limit: int = 500,
           before: str = "") -> list[dict[str, Any]]:
    """Messages with one peer, both ways, oldest first: the newest `limit`, or
    the newest `limit` sent before the time `before`. A message with files says
    their names (`files`) and, for a received one, whether they arrived and where
    they were unpacked (`attachment`: state, error, dir)."""
    return thread_page(con, peer_id, limit, before)["messages"]


def thread_page(con: sqlite3.Connection, peer_id: str, limit: int = THREAD_PAGE,
                before: str = "") -> dict[str, Any]:
    """One page of a conversation, for a chat that shows the latest and reads
    back on demand: `messages` oldest first, `has_more` when older ones exist,
    and `next_before`, the time to pass as `before` for the page behind this one.
    Messages sharing the oldest time are never split across two pages."""
    got = _attachments_of(con, peer_id)
    msgs = [_in_message(r) for r in _received(con, peer_id)]
    msgs += [_out_message(r) for r in con.execute(
        "SELECT * FROM b2b_sent WHERE peer_id=?", (peer_id,))]
    for m in msgs:
        if m["direction"] == "in" and m["id"] in got:
            m["files"] = got[m["id"]]["names"]
            m["attachment"] = {k: got[m["id"]][k] for k in ("state", "error", "dir")}
    if before:
        msgs = [m for m in msgs if m["at"] < before]
    msgs.sort(key=lambda m: m["at"])
    cut = len(msgs) - max(1, min(2000, limit))
    while 0 < cut < len(msgs) and msgs[cut - 1]["at"] == msgs[cut]["at"]:
        cut -= 1
    cut = max(cut, 0)
    page = msgs[cut:]
    return {"messages": page, "has_more": cut > 0,
            "next_before": page[0]["at"] if page and cut > 0 else ""}


def status_payload() -> dict[str, Any]:
    """The config, the peers and what was sent, for the page and the CLI."""
    con = db()
    counts = {r["status"]: r["n"] for r in con.execute(
        "SELECT status, count(*) AS n FROM trigger_request WHERE source=? GROUP BY status",
        (SOURCE,))}
    return {"ok": True, "config": config_summary(), "peers": list_peers(con, True),
            "allowed": list_allowed(con, True),
            "sent": list_sent(con, 20), "received": counts}


# ---------------------------------------------------------------- cli
def _ensure_crypto_env() -> None:
    """Re-run this command under the server's venv when `cryptography` is not
    importable here, so `python3 scripts/b2b.py` works from any shell."""
    try:
        import cryptography  # noqa: F401
        return
    except ImportError:
        pass
    if os.environ.get("MEMORY_LEDGER_B2B_REEXEC"):
        return                       # already tried: _crypto() explains
    import serve
    py = serve.server_python()
    os.environ["MEMORY_LEDGER_B2B_REEXEC"] = "1"
    sys.stdout.flush()
    os.execv(str(py), [str(py), str(Path(__file__).resolve()), *sys.argv[1:]])


def main(argv: list[str] | None = None) -> int:
    """The command line. Prints JSON, like trigger.py."""
    ap = argparse.ArgumentParser(prog="b2b.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="the identity, brokers, peers, what was sent and received")
    p = sub.add_parser("identity", help="this brain's key pair; its user id is the public key")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--generate", action="store_true")
    g.add_argument("--import", dest="import_key", default="",
                   help="a private key (base64url) to use, from another machine")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("config", help="the poll interval, the switch, the run command")
    p.add_argument("--interval", type=int, default=None, help="poll every N seconds")
    p.add_argument("--do", default=None, help="the run command for a received message")
    p.add_argument("--action", choices=trigger.ACTIONS, default=None,
                   help="what a received message runs: a Claude session (the default), "
                        "an API call or a script, as for the other triggers")
    p.add_argument("--script", default=None,
                   help="the script to run, a bare filename in <brain>/scripts")
    p.add_argument("--arg", action="append", default=None,
                   help="one argument for the script, variables filled in; repeat it")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--enable", action="store_true")
    g.add_argument("--disable", action="store_true")
    p = sub.add_parser("brokers", help="the relays this brain uses")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--add", default="", help="a broker's URL")
    g.add_argument("--remove", default="")
    g.add_argument("--enable", default="")
    g.add_argument("--disable", default="")
    g.add_argument("--check", default="", help="log in to it now")
    p.add_argument("--name", default="")
    p = sub.add_parser("peers", help="the brains this one talks to")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--add", default="", help="a peer's user id (their public key)")
    g.add_argument("--remove", default="")
    p.add_argument("--name", default="")
    p.add_argument("--broker", default="", help="the broker that reaches them")
    p.add_argument("--person", default="", help="the person node they are: an id or a name")
    p.add_argument("--no-allow", action="store_true",
                   help="do not also put them on the allowed list")
    p = sub.add_parser("allowed", help="who may send to this brain; anyone else is blocked")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--add", default="", help="a user id (their public key)")
    g.add_argument("--remove", default="")
    p.add_argument("--name", default="")
    p = sub.add_parser("send", help="send a message to a peer")
    p.add_argument("--to", required=True, help="a peer's user id, name, or person")
    p.add_argument("--text", default="")
    p.add_argument("--request", default="")
    p.add_argument("--file", action="append", default=[], help="attach a file (repeatable)")
    p = sub.add_parser("resend", help="send a kept message again, with its files")
    p.add_argument("id")
    sub.add_parser("poll", help="one pass: fetch, log, and run what arrived, in this process")
    args = ap.parse_args(argv)
    _ensure_crypto_env()

    if args.cmd == "status":
        res = status_payload()
    elif args.cmd == "identity":
        if args.generate:
            res = generate_identity(args.force)
        elif args.import_key:
            if private_key(load_config()) and not args.force:
                res = {"ok": False, "error": "an identity is set; pass --force to replace it"}
            else:
                res = save_config({"private_key": args.import_key})
        else:
            cfg = load_config()
            res = {"ok": True, "user_id": user_id(cfg),
                   "private_key": config_summary(cfg)["private_key"]}
    elif args.cmd == "config":
        patch: dict[str, Any] = {k: v for k, v in (
            ("poll_seconds", args.interval), ("do", args.do), ("action", args.action))
            if v is not None}
        if args.script is not None or args.arg is not None:
            patch["script"] = {k: v for k, v in (("name", args.script), ("args", args.arg))
                               if v is not None}
        if args.enable or args.disable:
            patch["enabled"] = bool(args.enable)
        res = save_config(patch) if patch else {"ok": True, "config": config_summary()}
    elif args.cmd == "brokers":
        if args.add:
            res = add_broker(args.add, args.name)
        elif args.remove:
            res = update_broker(args.remove, remove=True)
        elif args.enable or args.disable:
            res = update_broker(args.enable or args.disable, enabled=bool(args.enable))
        elif args.check:
            res = check_broker(args.check)
        else:
            res = {"ok": True, "brokers": config_summary()["brokers"]}
    elif args.cmd == "peers":
        con = db()
        if args.add:
            res = add_peer(con, args.add, args.name, args.broker, args.person,
                           allow=not args.no_allow)
        elif args.remove:
            res = remove_peer(con, args.remove)
        else:
            res = {"ok": True, "peers": list_peers(con, True)}
    elif args.cmd == "allowed":
        con = db()
        if args.add:
            res = allow_sender(con, args.add, args.name)
        elif args.remove:
            res = disallow_sender(con, args.remove)
        else:
            res = {"ok": True, "allowed": list_allowed(con, True)}
    elif args.cmd == "send":
        res = send(args.to, args.text, args.request, files=args.file)
    elif args.cmd == "resend":
        res = resend(args.id)
    else:
        cfg, ran = load_config(), []
        tcfg = trigger.load_config()
        res = poll(cfg, lambda rid: ran.append(trigger.run_request(rid, tcfg)))
        res["ran"] = ran
    trigger.out(res)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
