"""The relay's storage caps: a global file ceiling, expiry of unread mail, and the
refusal to fall back to SQLite when asked not to."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services" / "b2b"))
import b2b_store  # noqa: E402

A, B, C = "a" * 43, "b" * 43, "c" * 43


@pytest.fixture
def mail(tmp_path, monkeypatch):
    monkeypatch.setenv("B2B_SQLITE_PATH", str(tmp_path / "relay.db"))
    for k in b2b_store.PG_VARS:
        monkeypatch.delenv(k, raising=False)
    db = b2b_store.Db()
    yield b2b_store.Mailbox(db)
    db.close()


def test_total_cap_refuses_a_new_recipient_when_the_relay_is_full(mail):
    assert mail.put_file("1" * 32, A, B, "h", b"x" * 60, quota=100, total_max=100)
    with pytest.raises(OverflowError, match="full"):
        mail.put_file("2" * 32, A, C, "h", b"x" * 60, quota=100, total_max=100)
    with pytest.raises(OverflowError, match="quota"):
        mail.put_file("3" * 32, A, B, "h", b"x" * 60, quota=100, total_max=1000)


def test_unread_mail_expires_but_recent_mail_stays(mail):
    mail.put_message("1" * 32, B, A, "t", "{}", 10, 10)
    mail.put_file("2" * 32, A, B, "h", b"x", quota=100)
    with mail.db.tx() as t:
        t.q("UPDATE messages SET ts=?", (time.time() - 100,))
        t.q("UPDATE files SET ts=?", (time.time() - 100,))
    mail.put_message("3" * 32, B, A, "t", "{}", 10, 10)
    assert mail.sweep(files_s=50, messages_s=50, force=True) == 2
    assert [m[0] for m in mail.take_messages(B)] == ["3" * 32]
    assert mail.pending_bytes(B) == 0


def test_sweep_is_throttled(mail):
    assert mail.sweep(1, 1, force=True) == 0
    mail.put_message("1" * 32, B, A, "t", "{}", 10, 10)
    with mail.db.tx() as t:
        t.q("UPDATE messages SET ts=0")
    assert mail.sweep(1, 1) == 0                 # ran a moment ago: skipped
    assert mail.sweep(1, 1, force=True) == 1


def test_strict_mode_refuses_the_sqlite_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("B2B_SQLITE_PATH", str(tmp_path / "relay.db"))
    for k, v in zip(b2b_store.PG_VARS, ("u", "p", "n", "127.0.0.1", "1")):
        monkeypatch.setenv(k, v)                 # configured, but nothing listens on port 1
    monkeypatch.setenv("B2B_DB_STRICT", "1")
    with pytest.raises(RuntimeError, match="B2B_DB_STRICT"):
        b2b_store.Db()
    monkeypatch.delenv("B2B_DB_STRICT")
    db = b2b_store.Db()
    assert db.backend == "sqlite" and "PostgreSQL failed" in db.fallback_reason
    db.close()
