#!/usr/bin/env python3
"""Measure `recall` (hit@k and list size) while sweeping HUB_FREE_DEGREE and
RECALL_TAIL_CUT. eval_cues.py covers match-rules/match-skills only.

    python3 scripts/eval_recall.py build            # draft cases from the chat history
    python3 scripts/eval_recall.py run              # sweep the grid over reviewed cases

CASES ARE NOT IN THIS REPO (personal data). Default: <home>/recall_cases.json, or
--cases / $SECOND_BRAIN_RECALL_CASES.

`build` takes each past "Agents:" exchange: the query is the owner's first line;
the draft `expect` is every node the exchange pointed at (an edge other than
part-of, or an id quoted in the text). That is a WEAK label — review it. Cases get
`"reviewed": false`; `run` scores only the ones you flip to true.

`run` works on a throwaway COPY of graph.db: recall reinforces what it reads, and
an eval must not change the brain it measures.
"""
from __future__ import annotations
import argparse, json, os, re, shutil, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import graph  # noqa: E402


def cases_path(explicit):
    if explicit:
        return Path(explicit).expanduser()
    env = graph._env("RECALL_CASES")
    return Path(env).expanduser() if env else graph.home_dir() / "recall_cases.json"


def build(args):
    con = graph.connect()
    known = {r["id"] for r in con.execute("SELECT id FROM nodes")}
    cases = []
    for m in con.execute("SELECT id, body FROM nodes WHERE id LIKE 'message\\_agents-%' ESCAPE '\\'"):
        body = m["body"] or ""
        first = body.split("\n", 1)[0]
        if not first.startswith("Owner:"):
            continue
        q = first[6:].strip()
        if len(q) < args.min_len:
            continue
        want = set()
        for e in con.execute("SELECT src, dst FROM edges WHERE src=? OR dst=?", (m["id"], m["id"])):
            o = e["dst"] if e["src"] == m["id"] else e["src"]
            if not o.startswith(("conversation_", "message_")):
                want.add(o)
        want |= {t for t in re.findall(r"[`\[]\[?([a-z0-9_\-]{8,})", body) if t in known} - {m["id"]}
        cases.append({"query": q, "source": m["id"], "expect": sorted(want), "reviewed": False})
    p = cases_path(args.cases)
    p.write_text(json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8")
    withexp = sum(1 for c in cases if c["expect"])
    print(f"{len(cases)} drafts ({withexp} with a draft expect) -> {p}\nreview, set reviewed=true, then run.")


def run(args):
    cases = [c for c in json.loads(cases_path(args.cases).read_text(encoding="utf-8"))
             if c.get("reviewed") and c.get("expect")]
    if not cases:
        sys.exit("no reviewed cases with an expect list")
    cu = graph.connect(); src = graph.db_path(); cu.close()
    tmp = Path(tempfile.mkdtemp()) / "graph.db"
    shutil.copy(src, tmp)
    os.environ["SECOND_BRAIN_DB"] = str(tmp)
    con = graph.connect()
    print(f"{len(cases)} cases, k={args.k}\nHUB  CUT   hit@k  recall  avg_len")
    for hub in args.hub:
        for cut in args.cut:
            graph.HUB_FREE_DEGREE, graph.RECALL_TAIL_CUT = hub, cut
            hit = rec = ln = 0
            for c in cases:
                r = graph.api_recall(con, c["query"], limit=args.k, wire=False,
                                     exclude=[c["source"]])["results"]
                got = {x["id"] for x in r}
                want = set(c["expect"])
                hit += bool(got & want); rec += len(got & want) / len(want); ln += len(r)
            n = len(cases)
            print(f"{hub:<4} {cut:<5} {hit/n:5.0%}  {rec/n:6.0%}  {ln/n:6.1f}")
    shutil.rmtree(tmp.parent)


ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("cmd", choices=["build", "run"])
ap.add_argument("--cases")
ap.add_argument("--min-len", type=int, default=25)
ap.add_argument("--k", type=int, default=8)
ap.add_argument("--hub", type=int, nargs="+", default=[4, 8, 12, 16])
ap.add_argument("--cut", type=float, nargs="+", default=[0, 0.10, 0.15, 0.25])
a = ap.parse_args()
{"build": build, "run": run}[a.cmd](a)
