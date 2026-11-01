"""brain_api/updates.py against a FAKE release: no network, a throwaway checkout.

    python -m pytest tests/test_updates.py
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from brain_api import updates

TAG = "v9.9.9"
NAME = f"memory-ledger-{TAG}.zip"


def _zip(files: dict[str, str], top: str = f"memory-ledger-{TAG}") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, body in files.items():
            z.writestr(f"{top}/{n}" if top else n, body)
    return buf.getvalue()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A checkout at 0.1.0 and a release feed the test controls."""
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": "0.1.0"}), "utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "old.py").write_text("old", "utf-8")
    monkeypatch.setattr(updates, "ROOT", tmp_path)
    monkeypatch.setitem(updates._cache, "at", 0.0)
    feed: dict = {"blob": _zip({"scripts/new.py": "new", ".claude-plugin/plugin.json": '{"version": "9.9.9"}'}),
                  "sha": None, "assets": True}

    def fake_get(url, limit=0):
        if url == updates.API:
            assets = []
            if feed["assets"]:
                assets = [{"name": n, "browser_download_url": updates.DOWNLOAD_PREFIX + f"{TAG}/{n}"}
                          for n in (NAME, NAME + ".sha256")]
            return json.dumps({"tag_name": TAG, "body": "notes", "html_url": "u", "assets": assets}).encode()
        if url.endswith(".sha256"):
            return f"{feed['sha'] or hashlib.sha256(feed['blob']).hexdigest()}  {NAME}\n".encode()
        return feed["blob"]

    monkeypatch.setattr(updates, "_get", fake_get)
    return tmp_path, feed


def test_parse_version():
    assert updates.parse_version("v1.2.3") == (1, 2, 3)
    assert updates.parse_version("0.10.0") > updates.parse_version("0.9.9")
    assert updates.parse_version("nightly") == ()


def test_check_reports_a_newer_release(checkout):
    info = updates.check(force=True)
    assert (info["current"], info["latest"], info["available"], info["installable"]) == ("0.1.0", TAG, True, True)


def test_check_is_quiet_when_current(checkout, monkeypatch):
    monkeypatch.setattr(updates, "current_version", lambda: "9.9.9")
    assert updates.check(force=True)["available"] is False


def test_a_git_checkout_is_not_offered_the_button(checkout):
    (checkout[0] / ".git").mkdir()
    info = updates.check(force=True)
    assert info["available"] and not info["installable"] and "git pull" in info["why_not"]
    assert updates.apply()["ok"] is False


def test_apply_swaps_the_code_and_keeps_what_is_not_in_the_release(checkout):
    root, _ = checkout
    (root / "keep.txt").write_text("mine", "utf-8")
    res = updates.apply()
    assert res["ok"] is True and res["installed"] == TAG
    assert (root / "scripts" / "new.py").exists() and not (root / "scripts" / "old.py").exists()
    assert (root / "keep.txt").read_text("utf-8") == "mine"
    assert not (root / updates.OLD_DIR).exists() and not (root / updates.NEW_DIR).exists()


def test_a_bad_checksum_changes_nothing(checkout):
    root, feed = checkout
    feed["sha"] = "0" * 64
    res = updates.apply()
    assert res["ok"] is False and "checksum" in res["error"]
    assert (root / "scripts" / "old.py").exists() and not (root / "scripts" / "new.py").exists()


def test_a_zip_that_escapes_its_directory_is_refused(checkout):
    root, feed = checkout
    feed["blob"] = _zip({"../../evil.txt": "x"})
    res = updates.apply()
    assert res["ok"] is False and "unsafe" in res["error"]
    assert (root / "scripts" / "old.py").exists()


def test_a_release_without_the_asset_is_refused(checkout):
    feed = checkout[1]
    feed["assets"] = False
    assert "no memory-ledger" in updates.apply()["error"]


def test_the_release_is_fetched_through_net_so_a_missing_ca_bundle_is_handled(monkeypatch):
    seen = {}

    def fake_net_get(url, timeout=0, headers=None, limit=None):
        seen.update(url=url, headers=headers, limit=limit)
        return b"{}"

    monkeypatch.setattr(updates._net, "get", fake_net_get)
    assert updates._get(updates.API, 123) == b"{}"
    assert seen["url"] == updates.API and seen["limit"] == 123
    assert seen["headers"]["User-Agent"] == "memory-ledger-updater"
