"""Write routes answer only the brain's own page (brain_api/writes.py `require_local_origin`).

    python -m pytest tests/test_write_origin.py
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from brain_api import state
from brain_api.app import create_app

BODY = {"name": "update_check", "enabled": False}


@pytest.fixture
def client(module_brain_home, tmp_path):
    return TestClient(create_app(tmp_path, "no ui"), base_url="http://127.0.0.1:8770")


def post(client, **headers):
    return client.post("/api/flag", json=BODY, headers=headers)


def test_the_pages_own_origin_may_write(client):
    assert post(client, Origin="http://127.0.0.1:8770").status_code == 200


def test_no_origin_is_a_local_script_and_may_write(client):
    assert post(client).status_code == 200


@pytest.mark.parametrize("origin", ["https://evil.example", "http://127.0.0.1:9999",
                                    "http://localhost:8770", "null"])
def test_a_foreign_origin_or_another_port_is_refused(client, origin):
    r = post(client, Origin=origin)
    assert r.status_code == 403 and r.json()["ok"] is False


def test_a_spoofed_host_is_refused(client):
    assert post(client, Host="evil.example", Origin="http://evil.example").status_code == 403
    assert post(client, Host="evil.example").status_code == 403


@pytest.mark.parametrize("bad", ["[", "http://[::1", "http://[bad"])
def test_a_malformed_header_is_a_refusal_not_a_500(client, bad):
    assert post(client, Origin=bad).status_code == 403


def test_a_wildcard_bind_adds_no_host():
    for wild in ("0.0.0.0", "::", ""):
        state.BOUND_HOST = wild
        assert state.extra_hosts() == frozenset()
    state.BOUND_HOST = ""


def test_a_deliberate_non_local_bind_is_accepted(client, monkeypatch):
    monkeypatch.setattr(state, "BOUND_HOST", "brain.lan")
    assert post(client, Host="brain.lan:8770", Origin="http://brain.lan:8770").status_code == 200


def test_reads_are_not_gated(client):
    assert client.get("/api/health", headers={"Origin": "https://evil.example"}).status_code == 200
