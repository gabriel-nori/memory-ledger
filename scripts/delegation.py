#!/usr/bin/env python3
"""One agent hands a task to a connector of another provider (docs/agents.md#delegation).

The agent runs `agents.py delegate <connector> --task "<brief>"` from its shell.
The checks live here, in code, because a prompt alone does not hold: the flag,
the switch in `agents.json`, the allowed targets, depth 1, a cap per turn, and
a hard stop when the brief names a node of a blocked realm. The delegate runs
read-only with only the brief and none of the brain's commands, and its answer
comes back as text and is stored as a sub-agent row under the delegating reply.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

import flags
import graph

LABEL = "delegated"
DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "targets": [],                  # connector ids that may receive a task
    "rules": "",                    # when to delegate, free text, goes in the system prompt
    "never": "",                    # what never to delegate, free text, same
    "blocked_realms": ["personal"],  # a brief naming a node of these realms is refused
    "max_per_turn": 3,
    "timeout_s": 600,
}
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,}")


def clean(raw: Any) -> dict[str, Any]:
    """`raw` as a valid delegation block; anything missing or wrong falls back to the default."""
    raw = raw if isinstance(raw, dict) else {}
    out = {**DEFAULTS, "targets": [], "blocked_realms": list(DEFAULTS["blocked_realms"])}
    out["enabled"] = raw.get("enabled") is True
    for key in ("targets", "blocked_realms"):
        if isinstance(raw.get(key), list):
            out[key] = [str(x).strip()[:80] for x in raw[key] if str(x).strip()][:20]
    for key in ("rules", "never"):
        out[key] = str(raw.get(key) or "").strip()[:4000]
    for key, lo, hi in (("max_per_turn", 1, 10), ("timeout_s", 30, 3600)):
        try:
            out[key] = max(lo, min(hi, int(raw.get(key, DEFAULTS[key]))))
        except (TypeError, ValueError):
            out[key] = DEFAULTS[key]
    return out


def active(cfg: dict[str, Any]) -> bool:
    return flags.enabled("ai_providers") and bool(clean(cfg.get("delegation"))["enabled"])


def agents_connector(cfg: dict[str, Any], cid: str) -> dict[str, Any] | None:
    return next((c for c in cfg.get("connectors") or [] if c.get("id") == cid), None)


def prompt_section(cfg: dict[str, Any], cmd: str, own_connector: str) -> str:
    """What the delegating agent is told. The code enforces it again in `check`."""
    d = clean(cfg.get("delegation"))
    targets = [t for t in d["targets"] if t != own_connector]
    if not (active(cfg) and targets):
        return ""
    lines = [
        "You may hand a self-contained task to an agent of another provider. Run "
        f"`{cmd} <connector> --task \"<brief>\"`; it prints that agent's answer. Connectors you "
        f"may use: {', '.join(targets)}. It sees only your brief, never the brain, the "
        "conversation or the files, runs read-only, and its provider is an external service: "
        "put nothing personal, private or secret in the brief. At most "
        f"{d['max_per_turn']} per turn, and it cannot delegate further. You stay responsible "
        "for the answer: check what comes back."]
    if any((agents_connector(cfg, t) or {}).get("kind") == "hfspace" for t in targets):
        lines.append("A Hugging Face Space connector makes one image from the brief and answers with "
                     "the saved file path. It sends the brief to a third party's server. Ask for the "
                     "minimum that serves: optional first lines `aspect=square|wide|tall|landscape|"
                     "portrait|W:H` (omit for the smallest size) and `quality=draft|standard|final` "
                     "(default draft); then the prompt.")
    if d["rules"]:
        lines.append("When to delegate: " + d["rules"])
    if d["never"]:
        lines.append("Never delegate: " + d["never"])
    return "\n".join(lines)


def _blocked_node(task: str, realms: list[str]) -> str:
    """The id of a node of a blocked realm that the brief names, else ''."""
    if not realms:
        return ""
    ids = {t for t in TOKEN.findall(task)}
    if not ids:
        return ""
    con = graph.connect()
    marks = ",".join("?" for _ in ids)
    row = con.execute(f"SELECT id FROM nodes WHERE id IN ({marks}) AND realm IN "
                      f"({','.join('?' for _ in realms)}) LIMIT 1", [*ids, *realms]).fetchone()
    return row["id"] if row else ""


def check(con: sqlite3.Connection, cfg: dict[str, Any], reply_id: str, target: str,
          task: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    """(parent row, target connector, error). The error is empty when the task may go."""
    import agents
    if not flags.enabled("ai_providers"):
        return None, None, "delegation needs the ai_providers flag (Settings > Flags)"
    d = clean(cfg.get("delegation"))
    if not d["enabled"]:
        return None, None, "delegation is off (Settings > Agents)"
    if not task.strip():
        return None, None, "say what the other agent should do"
    parent = con.execute("SELECT * FROM agent_msg WHERE id=?", (reply_id,)).fetchone()
    if not parent:
        return None, None, "delegate only works from inside an agent reply"
    if parent["role"] != "agent":
        return None, None, "a delegated agent cannot delegate again (depth 1)"
    c = agents.connector(cfg, target)
    if not c or not c.get("enabled"):
        return None, None, f"no enabled connector {target!r}"
    if target not in d["targets"]:
        return None, None, f"{target!r} is not allowed to receive delegated tasks"
    if target == parent["connector"]:
        return None, None, "delegate to a connector of another provider, not your own"
    used = con.execute("SELECT COUNT(*) FROM agent_msg WHERE parent_msg_id=? AND label LIKE ?",
                       (reply_id, LABEL + "%")).fetchone()[0]
    if used >= d["max_per_turn"]:
        return None, None, f"at most {d['max_per_turn']} delegated tasks per turn"
    node = _blocked_node(task, d["blocked_realms"])
    if node:
        return None, None, (f"the brief names node {node}, which is in a blocked realm; "
                            "nothing from there goes to an external provider")
    return dict(parent), c, ""


def delegate(target: str, task: str, reply_id: str) -> dict[str, Any]:
    """Run `task` on `target` now and answer `{ok, text | error}`."""
    import agents
    con = agents.db()
    cfg = agents.load_config()
    parent, c, err = check(con, cfg, reply_id, target, task)
    if err or parent is None or c is None:
        return {"ok": False, "error": err}
    d = clean(cfg.get("delegation"))
    sid = agents._new_id("asub")
    model = c.get("default_model") or ""
    con.execute(
        "INSERT INTO agent_msg(id,conv_id,role,at,status,reply_to,connector,model,full_tools,"
        "parent_msg_id,label,prompt) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, parent["conv_id"], "subagent", agents.now_iso(), "running", reply_id, target,
         model, 0, reply_id, f"{LABEL} to {c['name']}", task.strip()[:agents.TEXT_MAX]))
    con.commit()
    row = dict(con.execute("SELECT * FROM agent_msg WHERE id=?", (sid,)).fetchone())
    turn = {"system": "You are an agent another agent handed a task. Do only that task and "
                      "answer with its result, in the language of the task. You have no access "
                      "to the owner's files or notes: work from the brief.",
            "prompt": task.strip(), "context": [], "question": task.strip()}
    run_c = {**c, "timeout_s": min(int(c.get("timeout_s") or 900), d["timeout_s"])}
    try:
        res = agents.RUNNERS[c["kind"]](con, row, run_c, turn)
    except Exception as e:
        agents._update(con, sid, status="error", error=str(e)[:1000], finished_at=agents.now_iso())
        return {"ok": False, "error": str(e)[:1000]}
    text, _ = agents.strip_next(res["text"])
    agents._update(con, sid, status="done", text=text[:agents.TEXT_MAX], events=res["events"],
                   cost_usd=round(res["cost_usd"], 6) if res["cost_usd"] is not None else None,
                   cost_estimated=int(bool(res["cost_estimated"])), tokens_in=res["tokens"][0],
                   tokens_out=res["tokens"][1], finished_at=agents.now_iso())
    con.commit()
    return {"ok": True, "connector": target, "text": text}
