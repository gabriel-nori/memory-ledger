#!/usr/bin/env python3
"""Feature flags: defined in code, switched in Settings > Flags.

A flag is a line in FLAGS below, so deprecating one is a diff: delete the entry and
every place that asks for it (grep the name). Which flags are on is the person's, in
`<home>/flags.json` as `{name: true|false}`; an absent name is its `default`.

Use it at every site, never behind a helper that hides the name:

    if flags.enabled("subagent_tracking"):
        new behaviour
    else:
        the old block, unchanged

so the day the flag goes, the `else` is deleted and the `if` is unwrapped.
"""
from __future__ import annotations

import json
from typing import Any

import graph

FILE_NAME = "flags.json"

FLAGS: dict[str, dict[str, Any]] = {
    "subagent_tracking": {
        "title": "Sub-agent tracking",
        "description": "Each sub-agent a reply spawns is its own row, linked to the one that "
                       "spawned it, and shows as a block under that message in the Agents tab. "
                       "Off: sub-agents stay events inside the reply.",
        "default": False,
    },
    "ai_providers": {
        "title": "More AI providers",
        "description": "Connect Gemini and ChatGPT (Codex) next to Claude and Ollama, each run "
                       "through its own CLI. Off: only Claude and Ollama connectors.",
        "default": False,
    },
}


def _path():
    return graph.home_dir() / FILE_NAME


def _stored() -> dict[str, bool]:
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: bool(v) for k, v in raw.items() if k in FLAGS} if isinstance(raw, dict) else {}


def enabled(name: str) -> bool:
    if name not in FLAGS:
        raise KeyError(f"unknown flag {name!r}: add it to flags.FLAGS")
    return _stored().get(name, bool(FLAGS[name]["default"]))


def listing() -> list[dict[str, Any]]:
    stored = _stored()
    return [{"name": n, "title": f["title"], "description": f["description"],
             "default": bool(f["default"]), "enabled": stored.get(n, bool(f["default"])),
             "saved": n in stored} for n, f in FLAGS.items()]


def set_flag(name: str, on: bool) -> dict[str, Any]:
    if name not in FLAGS:
        return {"ok": False, "error": f"unknown flag {name}"}
    stored = _stored()
    stored[name] = bool(on)
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)
    return {"ok": True, "flags": listing()}
