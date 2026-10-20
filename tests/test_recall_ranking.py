"""`recall` ranking: light pt/en stemming, hub correction with tail cut, and a
`why` that names the seed an associated row hangs off.

    python -m pytest tests/test_recall_ranking.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import graph  # noqa: E402

GRAPH_PY = ROOT / "scripts" / "graph.py"


@pytest.mark.parametrize("word,stem", [
    ("deploys", "deploy"), ("deploying", "deploy"), ("deployed", "deploy"),
    ("configuração", "configura"), ("api", "api"), ("bus", "bus"),
])
def test_stem(word, stem):
    assert graph._stem(word) == stem


def test_fts_query_dedupes_stems():
    assert graph._fts_query(["deploys", "deploying"]) == '"deploy"*'


class Brain:
    def __init__(self, home: Path) -> None:
        self.env = {**os.environ, "SECOND_BRAIN_HOME": str(home),
                    "SECOND_BRAIN_NO_AUTOSYNC": "1", "MEMORY_LEDGER_NO_CONSOLIDATE": "1"}

    def g(self, *args: str) -> dict:
        p = subprocess.run([sys.executable, str(GRAPH_PY), *args], capture_output=True,
                           text=True, encoding="utf-8", env=self.env)
        return json.loads(p.stdout)

    def node(self, title: str, body: str = "") -> str:
        return self.g("add-node", "--type", "note", "--title", title, "--body", body)["node"]["id"]

    def edge(self, a: str, b: str) -> None:
        self.g("add-edge", "--src", a, "--dst", b, "--rel", "relates-to")


@pytest.fixture(scope="module")
def brain(tmp_path_factory) -> Brain:
    b = Brain(tmp_path_factory.mktemp("rank"))
    seed = b.node("Pipeline releases", "we are deploying the nightly pipeline")
    near = b.node("Rollback checklist", "steps after a bad cut")
    b.edge(seed, near)
    hub = b.node("Owner hub", "everybody links here")
    b.edge(near, hub)
    for i in range(30):
        b.edge(hub, b.node(f"filler {i}", "unrelated text"))
    return b


def test_inflection_matches(brain):
    r = brain.g("recall", "deploys", "--no-wire")
    assert any(x["title"] == "Pipeline releases" and x["why"] == "matched"
               for x in r["results"])


def test_why_names_the_seed_and_hub_is_suppressed(brain):
    r = brain.g("recall", "deploying", "--no-wire")
    by = {x["title"]: x for x in r["results"]}
    assert by["Rollback checklist"]["why"] == 'linked to "Pipeline releases"'
    assert "Owner hub" not in by
