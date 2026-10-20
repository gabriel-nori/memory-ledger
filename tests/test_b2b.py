"""B2B (docs/b2b.md): the relay, from the outside, and the brain's side of it.

    python -m pytest tests/test_b2b.py

It starts the real relay (`services/b2b/serve_b2b.py`, which brings its own
venv) twice, on free local ports, and talks to it over a socket; the brain's
side runs in process against a THROWAWAY brain, with `claude` replaced by a
script that records what a headless session would get. Nothing leaves the
machine.

The brain's keys need `cryptography`; without it the whole module is skipped
(the relay's own venv has it, the interpreter running the suite may not).

The classes under "the brain" build on one brain and two relays, in the order
they are written: each class is a stage of one conversation, and its tests run
in file order. The state they share lives in the module-scoped `brain` fixture.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("cryptography")

import b2b  # noqa: E402
import b2b_crypto as bc  # noqa: E402
import graph  # noqa: E402
import trigger  # noqa: E402
from conftest import LOCALHOST, ROOT, UTF8  # noqa: E402

RELAY = ROOT / "services" / "b2b" / "serve_b2b.py"
RELAY_START_TIMEOUT_S = 300          # the first run builds the relay's venv
RELAY_STOP_TIMEOUT_S = 10
SENT_AT = "2026-09-30T00:00:00+00:00"
ANA = "Ana Souza"
BROKER_A = "team-a-relay"
BROKER_B = "team-b-relay"
FILE_MAX_TEST = 2_000            # bytes: the relay's limits, lowered for the tests
QUOTA_TEST = 5_000
CLAUDE_CALLS = "claude-calls.jsonl"
SCRIPT_LOG = "b2b-script.log"
SCRIPT_NAME = "b2b-note.sh"
UNKNOWN_BROKER = "nope"

FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.path.join(os.environ["SECOND_BRAIN_HOME"], "claude-calls.jsonl"), "a") as f:
    f.write(json.dumps({"args": args, "prompt": sys.stdin.read(),
                        "src": os.environ.get("MEMORY_LEDGER_TRIGGER_SOURCE")})
            + "\\n")
print(json.dumps({"result": "noted it", "total_cost_usd": 0.01}))
"""


# ---------------------------------------------------------------- helpers
def call(base: str, method: str, path: str, body: Any = None,
         token: str = "") -> tuple[int, dict[str, Any]]:
    """One JSON call: (status, body)."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind((LOCALHOST, 0))
        return s.getsockname()[1]


class Client:
    """Another brain, as bare as it gets: a key pair and the three calls."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.key, self.id = bc.generate()
        self.token = ""

    def challenge(self) -> dict[str, Any]:
        return call(self.base, "POST", "/challenge", {"user_id": self.id})[1]

    def session(self, ch: dict[str, Any], answer: str) -> tuple[int, dict[str, Any]]:
        return call(self.base, "POST", "/session", {"user_id": self.id,
                                                    "challenge_id": ch["challenge_id"],
                                                    "answer": answer})

    def login(self) -> str:
        ch = self.challenge()
        answer = bc.open_challenge(self.key, ch["challenge_id"], ch["challenge"])
        _, s = self.session(ch, answer)
        self.token = s["token"]
        return self.token

    def send(self, to: str, text: str, human: bool | None = None) -> tuple[int, dict[str, Any]]:
        payload: dict[str, Any] = {"text": text, "sent_at": SENT_AT}
        if human is not None:
            payload["human"] = human
        env = bc.seal(self.key, to, payload)
        return call(self.base, "POST", "/messages", {"from": self.id, "to": to, "envelope": env})

    def read(self) -> list[dict[str, Any]]:
        _, r = call(self.base, "GET", "/messages", token=self.token)
        return [{**bc.open_envelope(self.key, m["envelope"]), "from": m["envelope"]["from"]}
                for m in r.get("messages") or []]


def raw(base: str, method: str, path: str, data: bytes | None = None,
        token: str = "") -> tuple[int, bytes, dict[str, str]]:
    """One call with a raw body: (status, body, headers)."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(), {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, e.read(), {k.lower(): v for k, v in e.headers.items()}


def _start_relay(log: Path, env: dict[str, str] | None = None) -> tuple[subprocess.Popen, str, Any]:
    port = _free_port()
    out = open(log, "wb")
    base_env = {k: v for k, v in os.environ.items() if not k.startswith("B2B_DB_")}
    base_env["B2B_SQLITE_PATH"] = str(log.with_suffix(".db"))
    base_env.update({"B2B_FILE_MAX": str(FILE_MAX_TEST), "B2B_QUOTA_BYTES": str(QUOTA_TEST)})
    base_env.update(env or {})
    proc = subprocess.Popen([sys.executable, str(RELAY), "--port", str(port)],
                            stdout=out, stderr=subprocess.STDOUT, env=base_env)
    base = f"http://{LOCALHOST}:{port}"
    deadline = time.time() + RELAY_START_TIMEOUT_S
    while time.time() < deadline:
        if proc.poll() is not None:
            out.close()
            pytest.fail("the relay exited: " + log.read_text(encoding=UTF8, errors="replace")[-2000:])
        try:
            if call(base, "GET", "/health")[1].get("ok"):
                return proc, base, out
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.3)
    proc.kill()
    out.close()
    pytest.fail("the relay did not start")


@pytest.fixture(scope="module")
def relays(tmp_path_factory) -> Iterator[tuple[str, str]]:
    """Two real relays; stopped at the end of the module."""
    logs = tmp_path_factory.mktemp("relay-logs")
    started = [_start_relay(logs / "relay-a.log"), _start_relay(logs / "relay-b.log")]
    yield started[0][1], started[1][1]
    for proc, _, out in started:
        proc.terminate()
        try:
            proc.wait(timeout=RELAY_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        out.close()


@pytest.fixture(scope="module")
def base(relays) -> str:
    return relays[0]


@pytest.fixture(scope="module")
def base2(relays) -> str:
    return relays[1]


@pytest.fixture(scope="module")
def brain(module_brain_home, tmp_path_factory, relays) -> SimpleNamespace:
    """The brain the "brain" classes build on: a home whose `claude` is a fake,
    two relays, and room for what the stages hand to each other."""
    from _fakes import make_exe
    fake = make_exe(tmp_path_factory.mktemp("bin") / "fake-claude", FAKE_CLAUDE)
    (module_brain_home / "trigger.json").write_text(json.dumps({
        "enabled": True, "claude": {"bin": str(fake), "timeout_s": 60}}), encoding=UTF8)
    return SimpleNamespace(home=module_brain_home, base=relays[0], base2=relays[1],
                           dispatched=[], calls=module_brain_home / CLAUDE_CALLS)


def claude_calls(brain: SimpleNamespace) -> list[dict[str, Any]]:
    return [json.loads(x) for x in brain.calls.read_text(encoding=UTF8).splitlines()]


# ================================================================ the crypto
class TestCrypto:
    """Seal and open, and what must not open."""

    def test_a_user_id_is_a_public_key_derived_from_the_private_one(self):
        key, uid = bc.generate()
        assert bc.is_user_id(uid)
        assert len(uid) == 43
        assert bc.user_id_of(key) == uid

    @pytest.mark.parametrize("value", ["", "not-a-key", "x" * 42, "x" * 44, "!" * 43, None, 5])
    def test_what_is_not_a_user_id(self, value):
        assert not bc.is_user_id(value)

    def test_the_envelope_says_who_it_is_from_not_what_it_says(self):
        ka, a = bc.generate()
        _, b = bc.generate()
        env = bc.seal(ka, b, {"text": "hi"})
        assert env["from"] == a
        assert "hi" not in json.dumps(env)

    def test_the_recipient_opens_it(self):
        ka, _ = bc.generate()
        kb, b = bc.generate()
        assert bc.open_envelope(kb, bc.seal(ka, b, {"text": "hi"}))["text"] == "hi"

    @pytest.mark.parametrize("label, tamper", [
        ("someone else cannot open it", "other-key"),
        ("a forged sender does not open", "forged-from"),
        ("a changed id does not open", "changed-id"),
    ])
    def test_what_must_not_open(self, label, tamper):
        ka, a = bc.generate()
        kb, b = bc.generate()
        _, c = bc.generate()
        env = bc.seal(ka, b, {"text": "hi"})
        key, e = {"other-key": (ka, env),
                  "forged-from": (kb, {**env, "from": c}),
                  "changed-id": (kb, {**env, "id": "0" * 32})}[tamper]
        with pytest.raises(bc.BadBox):
            bc.open_envelope(key, e)


# ================================================================ the relay
class TestRelay:
    """The relay's contract, as any client sees it."""

    def test_a_challenge_needs_a_public_key(self, base):
        st, _ = call(base, "POST", "/challenge", {"user_id": "not-a-key"})
        assert st == 400

    def test_a_challenge_is_a_sealed_secret_with_an_expiry(self, base):
        a = Client(base)
        st, ch = call(base, "POST", "/challenge", {"user_id": a.id})
        assert st == 200
        assert ch.get("challenge_id")
        assert set(ch["challenge"]) == {"eph", "nonce", "ct"}
        assert ch.get("expires_at")

    def test_a_wrong_answer_opens_nothing(self, base):
        a = Client(base)
        st, _ = a.session(a.challenge(), "a guess")
        assert st == 401

    def test_the_right_answer_spends_the_challenge(self, base):
        a = Client(base)
        ch = a.challenge()
        answer = bc.open_challenge(a.key, ch["challenge_id"], ch["challenge"])
        assert a.session(ch, answer)[0] == 201
        assert a.session(ch, answer)[0] == 401

    def test_another_key_cannot_open_a_challenge(self, base):
        a, b = Client(base), Client(base)
        ch = a.challenge()
        with pytest.raises(bc.BadBox):
            bc.open_challenge(b.key, ch["challenge_id"], ch["challenge"])

    def test_the_right_answer_gets_a_retrieval_token_for_an_hour(self, base):
        a = Client(base)
        ch = a.challenge()
        st, s = a.session(ch, bc.open_challenge(a.key, ch["challenge_id"], ch["challenge"]))
        assert st == 201
        assert s.get("token")
        assert s.get("expires_in") == 3600

    def test_sending_needs_no_token_and_the_recipient_never_logged_in(self, base):
        a, b = Client(base), Client(base)
        st, r = a.send(b.id, "hi b")
        assert st == 202
        assert r.get("to") == b.id
        assert r.get("id")

    @pytest.mark.parametrize("label, build, status", [
        ("a message without from", lambda a, b: {"to": b.id, "envelope": bc.seal(
            a.key, b.id, {"text": "x"})}, 400),
        ("a from that does not match the envelope", lambda a, b: {
            "from": a.id, "to": b.id, "envelope": bc.seal(b.key, b.id, {"text": "x"})}, 400),
        ("a malformed envelope", lambda a, b: {
            "from": a.id, "to": b.id, "envelope": {"v": 2}}, 400),
    ])
    def test_a_message_the_relay_refuses(self, base, label, build, status):
        a, b = Client(base), Client(base)
        assert call(base, "POST", "/messages", build(a, b))[0] == status

    def test_an_over_large_envelope_is_refused_not_cut(self, base):
        a, b = Client(base), Client(base)
        assert a.send(b.id, "x" * 40_000)[0] == 413

    def test_no_token_cannot_read_and_a_wrong_token_cannot_either(self, base):
        assert call(base, "GET", "/messages")[0] == 401
        assert call(base, "GET", "/messages", token="wrong")[0] == 401

    def test_the_senders_own_inbox_is_empty(self, base):
        a, b = Client(base), Client(base)
        a.login()
        a.send(b.id, "hi b")
        assert a.read() == []

    def test_the_recipient_reads_and_opens_it_once(self, base):
        a, b = Client(base), Client(base)
        a.send(b.id, "hi b")
        b.login()
        assert [(m["from"], m["text"]) for m in b.read()] == [(a.id, "hi b")]
        assert b.read() == []


def file_call(base: str, sender: Client, to: str, data: bytes, fid: str | None = None,
              sha: str | None = None) -> tuple[int, dict[str, Any]]:
    """Upload `data` as a file from `sender` to `to`."""
    fid = fid or uuid.uuid4().hex
    sha = sha or hashlib.sha256(data).hexdigest()
    st, body, _ = raw(base, "POST", f"/files?id={fid}&from={sender.id}&to={to}&sha256={sha}",
                      data)
    return st, {**json.loads(body or b"{}"), "_id": fid}


class TestRelayFiles:
    """Sealed files on the relay: upload, download, delete, quota, refusal."""

    def test_the_recipient_downloads_then_deletes_it(self, base):
        a, b = Client(base), Client(base)
        st, up = file_call(base, a, b.id, b"sealed-bytes")
        assert (st, up["stored"]) == (201, True)
        b.login()
        st, body, hdr = raw(base, "GET", f"/files/{up['_id']}", token=b.token)
        assert (st, body) == (200, b"sealed-bytes")
        assert hdr["x-sha256"] == hashlib.sha256(b"sealed-bytes").hexdigest()
        assert raw(base, "DELETE", f"/files/{up['_id']}", token=b.token)[0] == 200
        assert raw(base, "GET", f"/files/{up['_id']}", token=b.token)[0] == 404

    def test_a_retried_upload_is_the_same_file_not_a_second_one(self, base):
        a, b = Client(base), Client(base)
        fid = uuid.uuid4().hex
        assert file_call(base, a, b.id, b"x" * 1000, fid)[0] == 201
        st, up = file_call(base, a, b.id, b"x" * 1000, fid)
        assert (st, up["stored"]) == (200, False)
        assert file_call(base, a, b.id, b"y" * 10, fid)[0] == 409

    def test_nobody_else_can_read_or_delete_it(self, base):
        a, b, c = Client(base), Client(base), Client(base)
        fid = file_call(base, a, b.id, b"secret")[1]["_id"]
        for who in (a, c):
            who.login()
            assert raw(base, "GET", f"/files/{fid}", token=who.token)[0] == 404
            assert raw(base, "DELETE", f"/files/{fid}", token=who.token)[0] == 404
        assert raw(base, "GET", f"/files/{fid}")[0] == 401

    def test_a_body_that_does_not_match_its_hash_is_refused(self, base):
        a, b = Client(base), Client(base)
        assert file_call(base, a, b.id, b"data", sha="0" * 64)[0] == 400

    def test_an_over_large_file_is_refused_not_cut(self, base):
        a, b = Client(base), Client(base)
        assert file_call(base, a, b.id, b"x" * (FILE_MAX_TEST + 1))[0] == 413

    def test_past_the_quota_the_upload_is_refused_until_something_is_downloaded(self, base):
        a, b = Client(base), Client(base)
        chunk = b"q" * 1_800
        ids = [file_call(base, a, b.id, chunk + bytes([i]))[1]["_id"] for i in range(2)]
        assert file_call(base, a, b.id, chunk + b"c")[0] == 507         # 3 x 1,801 > 5,000
        b.login()
        assert raw(base, "DELETE", f"/files/{ids[0]}", token=b.token)[0] == 200
        assert file_call(base, a, b.id, chunk + b"d")[0] == 201

    def test_refusing_a_sender_deletes_their_messages_and_files_only(self, base):
        a, b, c = Client(base), Client(base), Client(base)
        fa = file_call(base, a, b.id, b"from a")[1]["_id"]
        fc = file_call(base, c, b.id, b"from c")[1]["_id"]
        a.send(b.id, "hi")
        b.login()
        st, r = call(base, "POST", "/refusals", {"from": a.id}, token=b.token)
        assert (st, r["messages"], r["files"]) == (200, 1, 1)
        assert raw(base, "GET", f"/files/{fa}", token=b.token)[0] == 404
        assert raw(base, "GET", f"/files/{fc}", token=b.token)[0] == 200
        assert b.read() == []

    def test_refusing_needs_a_session_and_a_user_id(self, base):
        a, b = Client(base), Client(base)
        assert call(base, "POST", "/refusals", {"from": a.id})[0] == 401
        b.login()
        assert call(base, "POST", "/refusals", {"from": "nope"}, token=b.token)[0] == 400


class TestRelayDatabase:
    """SQLite by default and when PostgreSQL is incomplete or fails."""

    def test_sqlite_is_the_default(self, base):
        assert call(base, "GET", "/health")[1]["database"] == "sqlite"

    @pytest.mark.parametrize("label, env", [
        ("a variable missing", {"B2B_DB_USER": "u", "B2B_DB_PASSWORD": "p", "B2B_DB_NAME": "d",
                                "B2B_DB_URL": "127.0.0.1"}),
        ("a server that is not there", {"B2B_DB_USER": "u", "B2B_DB_PASSWORD": "p",
                                        "B2B_DB_NAME": "d", "B2B_DB_URL": "127.0.0.1",
                                        "B2B_DB_PORT": "1"}),
        ("a port that is not a number", {"B2B_DB_USER": "u", "B2B_DB_PASSWORD": "p",
                                         "B2B_DB_NAME": "d", "B2B_DB_URL": "127.0.0.1",
                                         "B2B_DB_PORT": "x"}),
    ])
    def test_falls_back_to_sqlite(self, tmp_path, label, env):
        proc, url, out = _start_relay(tmp_path / "fb.log", env)
        try:
            health = call(url, "GET", "/health")[1]
            assert health["database"] == "sqlite"
            a, b = Client(url), Client(url)
            assert a.send(b.id, "still works")[0] == 202
        finally:
            proc.terminate()
            proc.wait(timeout=RELAY_STOP_TIMEOUT_S)
            out.close()

    def test_messages_survive_a_restart(self, tmp_path):
        a_key, a_id = bc.generate()
        b = None
        log = tmp_path / "keep.log"
        proc, url, out = _start_relay(log)
        try:
            b = Client(url)
            a = Client(url)
            assert a.send(b.id, "kept")[0] == 202
            assert file_call(url, a, b.id, b"kept file")[0] == 201
        finally:
            proc.terminate()
            proc.wait(timeout=RELAY_STOP_TIMEOUT_S)
            out.close()
        proc, url, out = _start_relay(log)     # same SQLite file
        try:
            b.base = url
            b.login()
            assert [m["text"] for m in b.read()] == ["kept"]
        finally:
            proc.terminate()
            proc.wait(timeout=RELAY_STOP_TIMEOUT_S)
            out.close()

    @pytest.mark.skipif(not all(os.environ.get(k) for k in (
        "B2B_TEST_PG_USER", "B2B_TEST_PG_PASSWORD", "B2B_TEST_PG_NAME", "B2B_TEST_PG_HOST",
        "B2B_TEST_PG_PORT")), reason="set B2B_TEST_PG_{USER,PASSWORD,NAME,HOST,PORT} to run "
                                     "against a real PostgreSQL")
    def test_postgresql_when_all_five_are_set(self, tmp_path):
        env = {"B2B_DB_USER": os.environ["B2B_TEST_PG_USER"],
               "B2B_DB_PASSWORD": os.environ["B2B_TEST_PG_PASSWORD"],
               "B2B_DB_NAME": os.environ["B2B_TEST_PG_NAME"],
               "B2B_DB_URL": os.environ["B2B_TEST_PG_HOST"],
               "B2B_DB_PORT": os.environ["B2B_TEST_PG_PORT"]}
        proc, url, out = _start_relay(tmp_path / "pg.log", env)
        try:
            assert call(url, "GET", "/health")[1]["database"] == "postgresql"
            a, b = Client(url), Client(url)
            fid = file_call(url, a, b.id, b"pg bytes")[1]["_id"]
            a.send(b.id, "via pg")
            b.login()
            assert [m["text"] for m in b.read()] == ["via pg"]
            assert raw(url, "GET", f"/files/{fid}", token=b.token)[1] == b"pg bytes"
            assert call(url, "POST", "/refusals", {"from": a.id}, token=b.token)[0] == 200
            assert raw(url, "GET", f"/files/{fid}", token=b.token)[0] == 404
        finally:
            proc.terminate()
            proc.wait(timeout=RELAY_STOP_TIMEOUT_S)
            out.close()


# ================================================================ the brain
class TestBrainIdentity:
    """Identity and brokers."""

    def test_polling_is_off_until_configured(self, brain):
        assert not b2b.active(b2b.load_config())

    def test_an_identity_is_generated(self, brain):
        res = b2b.generate_identity()
        assert res.get("ok"), res
        assert bc.is_user_id(res.get("user_id", ""))
        brain.me = res["user_id"]

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
    def test_b2b_json_is_mode_600(self, brain):
        assert stat.S_IMODE(os.stat(b2b.config_path()).st_mode) == 0o600

    def test_the_summary_shows_the_user_id_never_the_private_key(self, brain):
        summary = b2b.config_summary()
        assert summary["user_id"] == brain.me
        assert summary["private_key"]["set"]
        assert b2b.load_config()["private_key"] not in json.dumps(summary)

    def test_generating_again_needs_force(self, brain):
        assert b2b.generate_identity().get("ok") is False

    def test_a_broker_needs_a_url(self, brain):
        assert b2b.add_broker("nope").get("ok") is False

    def test_a_broker_is_added_with_an_id_from_its_name(self, brain):
        res = b2b.add_broker(brain.base, "Team A relay")
        assert res.get("ok"), res
        assert res["config"]["brokers"][0]["id"] == BROKER_A

    def test_and_a_second_one(self, brain):
        res = b2b.add_broker(brain.base2, "Team B relay")
        assert res.get("ok")
        assert len(res["config"]["brokers"]) == 2

    def test_the_same_url_twice_is_refused(self, brain):
        assert b2b.add_broker(brain.base).get("ok") is False

    def test_checking_a_broker_logs_in_to_it(self, brain):
        res = b2b.check_broker(BROKER_A)
        assert res.get("ok"), res
        assert res["session"]["open"]

    def test_an_unknown_broker_cannot_be_checked(self, brain):
        assert b2b.check_broker(UNKNOWN_BROKER).get("ok") is False

    @pytest.mark.parametrize("label, patch", [
        ("an interval under the floor", {"poll_seconds": 5}),
        ("an unknown {variable} in the run command", {"do": "tell {nobody}"}),
        ("a malformed private key", {"private_key": "short"}),
    ])
    def test_a_config_that_is_refused(self, brain, label, patch):
        assert b2b.save_config(patch).get("ok") is False

    def test_b2b_variables_are_known(self, brain):
        res = b2b.save_config({"do": "From {b2b_peer_name} ({b2b_peer_id}) via {b2b_broker}: "
                                     "log it. " + b2b.DEFAULT_DO})
        assert res.get("ok"), res

    def test_switching_on_with_an_identity_and_a_broker_makes_it_active(self, brain):
        res = b2b.save_config({"enabled": True, "poll_seconds": 60})
        assert res.get("ok"), res
        assert res["config"]["active"]
        assert res["config"]["poll_seconds"] == 60


class TestBrainPeers:
    """Peers, linked to people."""

    def test_setup(self, brain):
        brain.peer, brain.stranger, brain.far = (Client(brain.base), Client(brain.base),
                                                 Client(brain.base2))
        for c in (brain.peer, brain.stranger, brain.far):
            c.login()
        brain.con = b2b.db()
        brain.ana = b2b.graph.api_create_person(brain.con, ANA)["node"]["id"]

    def test_own_id_cannot_be_a_peer(self, brain):
        assert b2b.add_peer(brain.con, brain.me, broker_id=BROKER_A).get("ok") is False

    def test_a_malformed_id_is_refused(self, brain):
        assert b2b.add_peer(brain.con, "not an id!").get("ok") is False

    def test_with_two_brokers_a_peer_needs_one_named(self, brain):
        assert "which broker" in b2b.add_peer(brain.con, brain.peer.id).get("error", "")

    def test_an_unknown_person_is_refused(self, brain):
        res = b2b.add_peer(brain.con, brain.peer.id, broker_id=BROKER_A, person_id="Nobody Here")
        assert res.get("ok") is False

    def test_a_peer_connects_linked_to_a_person_by_name_and_named_after_them(self, brain):
        res = b2b.add_peer(brain.con, brain.peer.id, broker_id=BROKER_A, person_id=ANA)
        assert res.get("ok"), res
        assert res["peer"]["person_id"] == brain.ana
        assert res["peer"]["name"] == ANA

    def test_a_peer_on_the_other_broker_connects(self, brain):
        assert b2b.add_peer(brain.con, brain.far.id, "Bo", BROKER_B).get("ok")

    def test_sending_to_someone_who_is_not_a_peer_is_refused(self, brain):
        assert b2b.send(brain.stranger.id, "hey").get("ok") is False


class TestBrainSend:
    def test_send_reaches_a_peer_by_their_person(self, brain):
        res = b2b.send(brain.ana, "lunch on friday?")
        assert res.get("ok"), res
        assert res["to"] == brain.peer.id
        assert res["broker"] == BROKER_A

    def test_and_the_other_peer_through_the_other_broker(self, brain):
        res = b2b.send("bo", "and you?", human=True)
        assert res.get("ok"), res
        assert res["broker"] == BROKER_B

    def test_both_are_logged_and_say_whether_a_person_typed_them(self, brain):
        sent = b2b.list_sent(brain.con)
        assert sorted(s["text"] for s in sent) == ["and you?", "lunch on friday?"]
        assert {s["text"]: s["human"] for s in sent} == {"and you?": 1, "lunch on friday?": 0}

    def test_the_peer_reads_it_from_this_brain_signed_as_automated(self, brain):
        assert [(m["from"], m["text"], m["human"]) for m in brain.peer.read()] == [
            (brain.me, "lunch on friday?", False)]

    def test_the_far_one_reads_it_as_a_persons(self, brain):
        assert [(m["text"], m["human"]) for m in brain.far.read()] == [("and you?", True)]


class TestBrainPoll:
    """Poll every broker into the request log."""

    def test_a_pass_reads_all_three_across_both_brokers(self, brain):
        brain.peer.send(brain.me, "yes, 12:30", human=True)
        brain.stranger.send(brain.me, "buy now")
        brain.far.send(brain.me, "sure", human=False)
        res = b2b.poll(dispatch=brain.dispatched.append)
        assert res.get("ok"), res
        assert res["received"] == 3
        brain.res = res

    def test_the_peers_are_queued_and_dispatched(self, brain):
        assert len(brain.res["queued"]) == 2
        assert brain.dispatched == brain.res["queued"]

    def test_the_stranger_is_blocked_and_runs_nothing(self, brain):
        assert len(brain.res["blocked"]) == 1

    def test_the_request_is_a_b2b_one_from_the_peer_by_name(self, brain):
        reqs = [trigger.get_request(brain.con, r) for r in brain.res["queued"]]
        brain.req = next(r for r in reqs if r["requester_id"] == brain.peer.id)
        req = brain.req
        assert req["source"] == "b2b"
        assert req["requester_name"] == ANA
        assert req["scope"] == f"b2b:{brain.peer.id}"
        assert req["text"] == "yes, 12:30"
        assert req["meta"]["human"] is True

    def test_one_with_no_flag_reads_as_automated(self, brain):
        assert trigger.get_request(brain.con, brain.res["blocked"][0])["meta"]["human"] is False

    def test_it_carries_the_run_command_it_was_told(self, brain):
        assert "{b2b_peer_name}" in brain.req["meta"]["do"]
        assert brain.req["meta"]["action"]["type"] == "claude"

    def test_the_blocked_one_is_finished_and_says_why(self, brain):
        brain.bad = trigger.get_request(brain.con, brain.res["blocked"][0])
        assert brain.bad["status"] == "blocked"
        assert brain.bad["finished_at"]
        assert "allowed list" in brain.bad["reason"]

    def test_all_are_in_tasks(self, brain):
        ids = {t["id"] for t in trigger.tasks_payload()["tasks"]}
        assert ids >= {brain.req["id"], brain.bad["id"]}

    def test_the_b2b_variables_are_filled_in(self, brain):
        v = trigger.request_variables(brain.req, trigger.load_config())
        assert v["b2b_peer_name"] == ANA
        assert v["b2b_peer_id"] == brain.peer.id
        assert v["b2b_person_id"] == brain.ana
        assert v["b2b_broker"] == BROKER_A
        assert v["trigger_source"] == "b2b"

    def test_a_second_pass_finds_nothing_the_relays_deleted_them(self, brain):
        assert b2b.poll(dispatch=brain.dispatched.append).get("received") == 0


class TestBrainConversations:
    def test_one_per_brain_both_ways_the_stranger_too(self, brain):
        brain.convs = {c["peer_id"]: c for c in b2b.conversations(brain.con)}
        assert set(brain.convs) == {brain.peer.id, brain.far.id, brain.stranger.id}

    def test_a_peers_is_named_connected_and_counts_both_directions(self, brain):
        conv = brain.convs[brain.peer.id]
        assert conv["name"] == ANA
        assert conv["connected"]
        assert conv["messages"] == 2

    def test_a_stranger_cannot_be_written_to(self, brain):
        conv = brain.convs[brain.stranger.id]
        assert not conv["connected"]
        assert not conv["allowed"]

    def test_a_thread_is_both_directions_each_flagged_and_stamped_in_time_order(self, brain):
        th = b2b.thread(brain.con, brain.peer.id)
        assert [(m["direction"], m["text"], m["human"]) for m in th] == [
            ("in", "yes, 12:30", True), ("out", "lunch on friday?", False)]
        assert [m["at"] for m in th] == [SENT_AT, b2b.list_sent(brain.con)[-1]["sent_at"]]
        assert next(m for m in th if m["direction"] == "in")["request_id"] == brain.req["id"]

    def test_an_envelope_that_does_not_open_is_dropped_not_believed(self, brain):
        forged = bc.seal(brain.stranger.key, brain.me, {"text": "tampered"})
        call(brain.base, "POST", "/messages", {"from": brain.stranger.id, "to": brain.me,
                                               "envelope": {**forged, "id": "f" * 32}})
        res = b2b.poll(dispatch=brain.dispatched.append)
        assert res.get("ok"), res
        assert len(res["unreadable"]) == 1
        assert not res["queued"]


class TestBrainAllowedList:
    def test_connecting_a_peer_allowed_it_to_send(self, brain):
        allowed = {a["user_id"]: a for a in b2b.list_allowed(brain.con)}
        assert set(allowed) == {brain.peer.id, brain.far.id}
        assert allowed[brain.peer.id]["is_peer"]
        assert allowed[brain.peer.id]["name"] == ANA

    def test_a_malformed_id_is_not_allowed_nor_is_this_brains_own(self, brain):
        assert b2b.allow_sender(brain.con, "nope").get("ok") is False
        assert b2b.allow_sender(brain.con, brain.me).get("ok") is False

    def test_the_stranger_is_allowed_without_being_a_peer(self, brain):
        assert b2b.allow_sender(brain.con, brain.stranger.id, "Carla").get("ok")
        assert not b2b.get_peer(brain.con, brain.stranger.id)

    def test_and_their_next_message_is_queued(self, brain):
        brain.stranger.send(brain.me, "hi again")
        res = b2b.poll(dispatch=brain.dispatched.append)
        assert len(res["queued"]) == 1
        assert not res["blocked"]

    def test_they_are_blocked_again_and_blocking_twice_says_so(self, brain):
        assert b2b.disallow_sender(brain.con, brain.stranger.id).get("ok")
        assert b2b.disallow_sender(brain.con, brain.stranger.id).get("ok") is False

    def test_so_their_next_one_is_blocked(self, brain):
        brain.stranger.send(brain.me, "and again")
        res = b2b.poll(dispatch=brain.dispatched.append)
        assert len(res["blocked"]) == 1
        assert not res["queued"]

    def test_a_peer_added_with_allow_off_is_not_allowed(self, brain):
        res = b2b.add_peer(brain.con, brain.stranger.id, "Carla", BROKER_A, allow=False)
        assert res.get("ok")
        assert not b2b.is_allowed(brain.con, brain.stranger.id)
        b2b.remove_peer(brain.con, brain.stranger.id)


class TestBrainReprocess:
    def test_a_queued_request_cannot_be_reprocessed(self, brain):
        again: list[str] = []
        res = trigger.reprocess(brain.con, brain.req["id"], again.append)
        assert res.get("ok") is False
        assert not again

    def test_a_blocked_one_goes_back_in_the_queue_and_is_dispatched(self, brain):
        again: list[str] = []
        res = trigger.reprocess(brain.con, brain.bad["id"], again.append, by="test")
        assert res.get("ok"), res
        assert res["was"] == "blocked"
        assert again == [brain.bad["id"]]

    def test_with_what_it_ended_with_cleared_and_the_reprocess_logged(self, brain):
        back = trigger.get_request(brain.con, brain.bad["id"])
        assert back["status"] == "queued"
        assert not back["finished_at"]
        assert back["reason"] == "reprocess: was blocked"
        assert any(a["kind"] == "reprocess" for a in trigger.actions_for(brain.con, brain.bad["id"]))

    def test_it_runs(self, brain):
        run = trigger.run_request(brain.bad["id"])
        assert run.get("ok"), run
        assert run["status"] == "done"

    def test_a_failed_one_can_be_reprocessed_too(self, brain):
        trigger._set_request(brain.con, brain.bad["id"], status="failed",
                             finished_at=trigger.now_iso(), exit_code=1, result="boom")
        res = trigger.reprocess(brain.con, brain.bad["id"])
        back = trigger.get_request(brain.con, brain.bad["id"])
        assert res.get("ok"), res
        assert res["was"] == "failed"
        assert back["exit_code"] is None
        assert back["result"] == ""
        trigger._set_request(brain.con, brain.bad["id"], status="done",
                             finished_at=trigger.now_iso())


class TestBrainSession:
    def test_the_session_runs(self, brain):
        run = trigger.run_request(brain.req["id"])
        assert run.get("ok"), run
        assert run["status"] == "done"

    def test_it_is_told_the_source_and_that_it_is_not_a_conversation_to_post_in(self, brain):
        last = claude_calls(brain)[-1]
        assert "source: b2b" in last["prompt"]
        assert "b2b_event: message" in last["prompt"]
        assert "not a conversation to post in" in last["prompt"]
        assert last["src"] == "b2b"

    def test_it_is_told_how_to_answer_to_that_peer(self, brain):
        prompt = claude_calls(brain)[-1]["prompt"]
        assert f"send --to {brain.peer.id}" in prompt
        assert "From Ana Souza (" in prompt


@pytest.mark.skipif(os.name == "nt", reason="the script is a POSIX shell script")
class TestBrainScript:
    """A script instead of Claude."""

    def test_a_script_with_no_name_is_refused(self, brain):
        sdir = graph.scripts_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / SCRIPT_NAME).write_text(
            '#!/bin/sh\nprintf "%s|%s|%s\\n" "$1" "$BRAIN_TRIGGER_CONTENT" "$BRAIN_B2B_BROKER"'
            ' >> "$SECOND_BRAIN_HOME/b2b-script.log"\n', encoding=UTF8)
        (sdir / SCRIPT_NAME).chmod(0o755)
        assert b2b.save_config({"action": "script", "script": {"name": ""}}).get("ok") is False

    def test_an_unknown_variable_in_its_arguments_is_refused(self, brain):
        res = b2b.save_config({"action": "script",
                               "script": {"name": SCRIPT_NAME, "args": ["{nope}"]}})
        assert res.get("ok") is False

    def test_a_received_message_can_run_a_script(self, brain):
        res = b2b.save_config({"action": "script",
                               "script": {"name": SCRIPT_NAME, "args": ["{b2b_peer_name}"]}})
        assert res.get("ok"), res
        assert res["config"]["action"] == "script"
        assert res["config"]["script"]["name"] == SCRIPT_NAME

    def test_the_request_carries_the_script_action_and_runs_it(self, brain):
        n_calls = len(claude_calls(brain))
        brain.peer.send(brain.me, "ship it")
        res = b2b.poll(dispatch=brain.dispatched.append)
        assert res.get("queued"), res
        sreq = trigger.get_request(brain.con, res["queued"][0])
        assert sreq["meta"]["action"]["type"] == "script"
        run = trigger.run_request(sreq["id"])
        assert run.get("ok"), run
        assert run["status"] == "done"
        log = (brain.home / SCRIPT_LOG).read_text(encoding=UTF8)
        assert log.strip() == f"{ANA}|ship it|{BROKER_A}"
        assert len(claude_calls(brain)) == n_calls      # no Claude session

    def test_back_to_claude(self, brain):
        assert b2b.save_config({"action": "claude"}).get("ok")
        assert b2b.load_config()["action"] == "claude"


class TestBrainBrokers:
    """A broker that forgot us, and one that is down."""

    def test_a_stale_token_logs_in_again_by_itself(self, brain):
        b2b._SESSIONS[(brain.base, brain.me)] = ("stale-token", time.time() + 3000)
        brain.peer.send(brain.me, "after a restart")
        res = b2b.poll(dispatch=brain.dispatched.append)
        assert res.get("ok"), res
        assert len(res["queued"]) == 1

    def test_a_broker_disables(self, brain):
        assert b2b.update_broker(BROKER_B, enabled=False).get("ok")

    def test_a_broker_that_is_down_is_reported_and_can_be_removed(self, brain):
        b2b.add_broker(f"http://{LOCALHOST}:{_free_port()}", "Down")
        res = b2b.poll(dispatch=brain.dispatched.append)
        assert res.get("ok") is False
        assert "down" in res.get("errors", {})
        assert b2b.update_broker("down", remove=True).get("ok")


class TestBrainDisconnecting:
    def test_a_peer_disconnects_and_the_row_is_kept(self, brain):
        assert b2b.remove_peer(brain.con, brain.peer.id).get("ok")
        assert any(p["peer_id"] == brain.peer.id and p["removed_at"]
                   for p in b2b.list_peers(brain.con, True))

    def test_a_former_peer_still_on_the_allowed_list_can_send(self, brain):
        brain.peer.send(brain.me, "still there?")
        res = b2b.poll(dispatch=brain.dispatched.append)
        assert len(res["queued"]) == 1

    def test_once_off_the_list_their_message_is_blocked(self, brain):
        b2b.disallow_sender(brain.con, brain.peer.id)
        brain.peer.send(brain.me, "and now?")
        res = b2b.poll(dispatch=brain.dispatched.append)
        assert len(res["blocked"]) == 1
        assert not res["queued"]

    def test_and_the_list_keeps_them_marked_removed(self, brain):
        assert any(a["user_id"] == brain.peer.id and a["removed_at"]
                   for a in b2b.list_allowed(brain.con, True))
