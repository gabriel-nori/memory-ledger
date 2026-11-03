import hashlib
import re
import sqlite3

import pytest

import portal_trigger as pt


TRIGGER_ID = "trigger-1"
OTHER_TRIGGER_ID = "trigger-2"
IPV4 = "192.0.2.10"
IPV4_MAPPED_IPV6 = "::ffff:192.0.2.10"
INVALID_IP = "not-an-ip"
WRONG_TOKEN = "wrong-token"
EMPTY = ""
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@pytest.fixture
def con():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE portal_token (
            trigger_id TEXT PRIMARY KEY,
            token_hash TEXT,
            created_at TEXT,
            revoked_at TEXT DEFAULT ''
        );
        """
    )
    yield connection
    connection.close()


def test_mint_token_returns_prefixed_value_and_stores_only_hash(con):
    token = pt.mint_token(con, TRIGGER_ID)

    row = con.execute(
        "SELECT token_hash, revoked_at FROM portal_token WHERE trigger_id=?",
        (TRIGGER_ID,),
    ).fetchone()

    assert token.startswith("ptl_")
    assert row["token_hash"] == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert HASH_PATTERN.fullmatch(row["token_hash"])
    assert token != row["token_hash"]
    assert row["revoked_at"] == EMPTY
    assert token not in row["token_hash"]


def test_second_mint_invalidates_first_and_clears_revoked(con):
    first_token = pt.mint_token(con, TRIGGER_ID)
    pt.revoke_token(con, TRIGGER_ID)

    second_token = pt.mint_token(con, TRIGGER_ID)

    assert second_token.startswith("ptl_")
    assert second_token != first_token
    assert pt.token_ok(con, TRIGGER_ID, first_token) is False
    assert pt.token_ok(con, TRIGGER_ID, second_token) is True
    row = con.execute(
        "SELECT token_hash, revoked_at FROM portal_token WHERE trigger_id=?",
        (TRIGGER_ID,),
    ).fetchone()
    assert row["token_hash"] == hashlib.sha256(second_token.encode("utf-8")).hexdigest()
    assert row["revoked_at"] == EMPTY


def test_revoke_token_returns_true_once_then_false(con):
    pt.mint_token(con, TRIGGER_ID)

    first = pt.revoke_token(con, TRIGGER_ID)
    second = pt.revoke_token(con, TRIGGER_ID)

    assert first == {"ok": True, "id": TRIGGER_ID, "revoked": True}
    assert second == {"ok": True, "id": TRIGGER_ID, "revoked": False}


def test_revoke_unknown_token_returns_false(con):
    result = pt.revoke_token(con, TRIGGER_ID)

    assert result == {"ok": True, "id": TRIGGER_ID, "revoked": False}


@pytest.mark.parametrize("presented", [EMPTY, WRONG_TOKEN])
def test_token_ok_rejects_empty_or_wrong_token(con, presented):
    pt.mint_token(con, TRIGGER_ID)

    assert pt.token_ok(con, TRIGGER_ID, presented) is False


def test_token_ok_rejects_revoked_token(con):
    token = pt.mint_token(con, TRIGGER_ID)
    pt.revoke_token(con, TRIGGER_ID)

    assert pt.token_ok(con, TRIGGER_ID, token) is False


def test_token_ok_rejects_unknown_trigger(con):
    token = pt.mint_token(con, TRIGGER_ID)

    assert pt.token_ok(con, OTHER_TRIGGER_ID, token) is False


def test_peer_allowed_handles_matching_invalid_and_mapped_addresses():
    assert pt.peer_allowed(IPV4, ["192.0.2.0/24"]) is True
    assert pt.peer_allowed(IPV4_MAPPED_IPV6, ["192.0.2.0/24"]) is True
    assert pt.peer_allowed(IPV4, ["198.51.100.0/24"]) is False
    assert pt.peer_allowed(INVALID_IP, ["192.0.2.0/24"]) is False
    assert pt.peer_allowed(INVALID_IP, ["*"]) is True
