"""The B2B relay's storage: sealed messages and sealed files, in PostgreSQL or SQLite.

PostgreSQL is used only when **all five** of these are set, and the first
connection works:

    B2B_DB_USER  B2B_DB_PASSWORD  B2B_DB_NAME  B2B_DB_URL (the host)  B2B_DB_PORT

A missing one, a driver that is not installed, or a failed connection or
schema setup falls straight back to SQLite, in the file `$B2B_SQLITE_PATH`
(default `~/.local/share/memory-ledger/b2b-relay.db`). `Db.backend` says which
one is in use. Files are stored as blobs in either.

Sessions and challenges are not stored: they live in the relay's memory, and a
restart only makes brains log in again.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PG_VARS = ("B2B_DB_USER", "B2B_DB_PASSWORD", "B2B_DB_NAME", "B2B_DB_URL", "B2B_DB_PORT")

# `{blob}` and `{real}` are the two types the engines spell differently.
SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id        TEXT PRIMARY KEY,
    recipient TEXT NOT NULL,
    sender    TEXT NOT NULL,
    at        TEXT NOT NULL,
    ts        {real} NOT NULL,
    envelope  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_recipient ON messages(recipient, ts);
CREATE TABLE IF NOT EXISTS files (
    id        TEXT PRIMARY KEY,
    sender    TEXT NOT NULL,
    recipient TEXT NOT NULL,
    size      BIGINT NOT NULL,
    sha256    TEXT NOT NULL,
    ts        {real} NOT NULL,
    data      {blob} NOT NULL
);
CREATE INDEX IF NOT EXISTS files_recipient ON files(recipient, sender);
"""


def sqlite_path() -> Path:
    """Where the SQLite file lives."""
    raw = os.environ.get("B2B_SQLITE_PATH", "").strip()
    if raw:
        return Path(raw).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "memory-ledger" / "b2b-relay.db"


def pg_settings(env: dict[str, str] | None = None) -> dict[str, Any] | None:
    """The PostgreSQL connection settings, or None when any variable is unset."""
    env = os.environ if env is None else env
    got = {k: (env.get(k) or "").strip() for k in PG_VARS}
    if not all(got.values()):
        return None
    host = got["B2B_DB_URL"]
    if "://" in host:                       # a URL was given: take its host
        host = urlparse(host).hostname or ""
    try:
        port = int(got["B2B_DB_PORT"])
    except ValueError:
        return None
    if not host:
        return None
    return {"host": host, "port": port, "user": got["B2B_DB_USER"],
            "password": got["B2B_DB_PASSWORD"], "dbname": got["B2B_DB_NAME"]}


class Tx:
    """One transaction: `q(sql, args)` runs a statement written with `?`."""

    def __init__(self, cur: Any, pg: bool) -> None:
        self._cur, self._pg = cur, pg
        self.rowcount = 0

    def q(self, sql: str, args: tuple = ()) -> list[tuple]:
        """Run a statement; the rows it returns, or [] for one that returns none."""
        self._cur.execute(sql.replace("?", "%s") if self._pg else sql, args)
        self.rowcount = self._cur.rowcount
        return list(self._cur.fetchall()) if self._cur.description else []


class Db:
    """One connection behind one lock; every operation is a transaction."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._con: Any = None
        self._pg: dict[str, Any] | None = None
        self.backend = "sqlite"
        self.fallback_reason = ""
        self._open()

    # -- connecting
    def _open(self) -> None:
        settings = pg_settings()
        if settings is None:
            missing = [k for k in PG_VARS if not (os.environ.get(k) or "").strip()]
            self.fallback_reason = ("PostgreSQL not configured (unset: " + ", ".join(missing)
                                    + ")") if missing else "B2B_DB_URL or B2B_DB_PORT is invalid"
            self._open_sqlite()
            return
        try:
            self._pg = settings
            self._con = self._connect_pg()
            self._schema(pg=True)
            self.backend = "postgresql"
        except Exception as e:  # any failure at start-up: SQLite, never a dead relay
            self._pg = None
            try:
                if self._con is not None:
                    self._con.close()
            except Exception:
                pass
            self._con = None
            self.fallback_reason = f"PostgreSQL failed ({type(e).__name__}: {e})"
            print(f"b2b: {self.fallback_reason}; using SQLite", file=sys.stderr, flush=True)
            self._open_sqlite()

    def _connect_pg(self) -> Any:
        import psycopg
        return psycopg.connect(**(self._pg or {}), connect_timeout=5)

    def _open_sqlite(self) -> None:
        path = sqlite_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA busy_timeout=5000")
        self.backend = "sqlite"
        self._schema(pg=False)

    def _schema(self, pg: bool) -> None:
        sql = SCHEMA.format(blob="BYTEA" if pg else "BLOB",
                            real="DOUBLE PRECISION" if pg else "REAL")
        cur = self._con.cursor()
        for stmt in (s.strip() for s in sql.split(";")):
            if stmt:
                cur.execute(stmt)
        if pg:
            self._con.commit()

    # -- using
    @contextmanager
    def tx(self) -> Iterator[Tx]:
        """A transaction under the lock: committed on exit, rolled back on error.
        A dropped PostgreSQL connection is reopened once."""
        pg = self.backend == "postgresql"
        with self._lock:
            if pg and getattr(self._con, "closed", False):
                self._con = self._connect_pg()
            cur = self._con.cursor()
            if not pg:
                cur.execute("BEGIN IMMEDIATE")
            try:
                yield Tx(cur, pg)
            except BaseException:
                self._con.rollback()
                raise
            else:
                self._con.commit()
            finally:
                cur.close()

    def close(self) -> None:
        """Close the connection."""
        with self._lock:
            if self._con is not None:
                self._con.close()
                self._con = None


# ---------------------------------------------------------------- the relay's data
class Mailbox:
    """Messages and files, on a `Db`. The caps are the caller's to pass."""

    def __init__(self, db: Db) -> None:
        self.db = db

    def sweep_files(self, older_than_s: float) -> int:
        """Delete files nobody downloaded in `older_than_s` seconds."""
        with self.db.tx() as t:
            t.q("DELETE FROM files WHERE ts < ?", (time.time() - older_than_s,))
            return t.rowcount

    # -- messages
    def put_message(self, msg_id: str, to: str, sender: str, at: str, envelope_json: str,
                    queue_max: int, mailboxes_max: int) -> None:
        """Queue a message. `OverflowError` ("full") when the relay holds
        `mailboxes_max` recipients already, or `OverflowError` (the id) when this
        one has `queue_max` waiting; `ValueError` for a repeated id."""
        with self.db.tx() as t:
            if t.q("SELECT 1 FROM messages WHERE id=?", (msg_id,)):
                raise ValueError("a message with this id is already waiting")
            n = t.q("SELECT COUNT(*) FROM messages WHERE recipient=?", (to,))[0][0]
            if n == 0:
                boxes = t.q("SELECT COUNT(DISTINCT recipient) FROM messages")[0][0]
                if boxes >= mailboxes_max:
                    raise OverflowError("the relay is full")
            elif n >= queue_max:
                raise OverflowError(to)
            t.q("INSERT INTO messages(id, recipient, sender, at, ts, envelope) "
                "VALUES(?,?,?,?,?,?)", (msg_id, to, sender, at, time.time(), envelope_json))

    def take_messages(self, to: str) -> list[tuple[str, str, str, str]]:
        """Every message waiting for `to`, oldest first, as `(id, sender, at,
        envelope_json)`, deleted in the same transaction."""
        with self.db.tx() as t:
            rows = t.q("SELECT id, sender, at, envelope FROM messages WHERE recipient=? "
                       "ORDER BY ts, id", (to,))
            if rows:
                t.q("DELETE FROM messages WHERE recipient=?", (to,))
            return [(r[0], r[1], r[2], r[3]) for r in rows]

    # -- files
    def put_file(self, file_id: str, sender: str, to: str, sha256: str, data: bytes,
                 quota: int) -> bool:
        """Store a sealed file for `to`. True when stored, False when this exact
        file (same id, sender, recipient and hash) was already there: an upload
        retried. `OverflowError` when `to` would hold more than `quota` bytes,
        `ValueError` when the id is taken by something else."""
        with self.db.tx() as t:
            have = t.q("SELECT sender, recipient, sha256 FROM files WHERE id=?", (file_id,))
            if have:
                if tuple(have[0]) == (sender, to, sha256):
                    return False
                raise ValueError("a file with this id is already waiting")
            used = t.q("SELECT COALESCE(SUM(size), 0) FROM files WHERE recipient=?", (to,))[0][0]
            if int(used) + len(data) > quota:
                raise OverflowError("quota")
            t.q("INSERT INTO files(id, sender, recipient, size, sha256, ts, data) "
                "VALUES(?,?,?,?,?,?,?)", (file_id, sender, to, len(data), sha256,
                                          time.time(), data))
            return True

    def get_file(self, file_id: str, to: str) -> tuple[bytes, str] | None:
        """`(data, sha256)` of a file waiting for `to`, or None."""
        with self.db.tx() as t:
            rows = t.q("SELECT data, sha256 FROM files WHERE id=? AND recipient=?",
                       (file_id, to))
        return (bytes(rows[0][0]), rows[0][1]) if rows else None

    def delete_file(self, file_id: str, to: str) -> bool:
        """Delete a file waiting for `to`. Whether there was one."""
        with self.db.tx() as t:
            t.q("DELETE FROM files WHERE id=? AND recipient=?", (file_id, to))
            return t.rowcount > 0

    def pending_bytes(self, to: str) -> int:
        """What `to` has waiting, in bytes."""
        with self.db.tx() as t:
            return int(t.q("SELECT COALESCE(SUM(size), 0) FROM files WHERE recipient=?",
                           (to,))[0][0])

    def refuse(self, to: str, sender: str) -> dict[str, int]:
        """`to` refuses `sender`: everything that sender left for `to`, messages
        and files, is deleted. Returns how many of each."""
        with self.db.tx() as t:
            t.q("DELETE FROM messages WHERE recipient=? AND sender=?", (to, sender))
            msgs = t.rowcount
            t.q("DELETE FROM files WHERE recipient=? AND sender=?", (to, sender))
            return {"messages": msgs, "files": t.rowcount}
