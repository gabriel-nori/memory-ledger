#!/usr/bin/env python3
"""The AI providers an agent connector can run on (docs/agents.md).

One row per `kind`: the CLI to run, how to install and check it, the models it
offers and the environment variable its API key rides in. CLI flags and model
names change often; the values below were checked against each vendor's
current docs on 2026-11-02 (Gemini CLI: cli-reference and headless; Codex:
non-interactive mode). Re-check before changing a flag.

API keys never go in `agents.json`, the graph or a log. They live one file each
under `<home>/secrets/`, mode 600, and reach the CLI only as an environment
variable of the child process. The default sign-in is the CLI's own (OAuth);
a key is used only when the person saved one for that provider.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import _platform
import graph

SECRETS_DIR = "secrets"

PROVIDERS: dict[str, dict[str, Any]] = {
    "claude": {
        "title": "Claude", "bin": "claude", "npm": "@anthropic-ai/claude-code",
        "key_env": "ANTHROPIC_API_KEY", "check": ["--version"], "login": "claude",
        "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5-20251001",
                   "claude-fable-5-1"],
        "default_model": "claude-sonnet-5-5",
    },
    "gemini": {
        "title": "Gemini", "bin": "gemini", "npm": "@google/gemini-cli",
        "key_env": "GEMINI_API_KEY", "check": ["--version"], "login": "gemini",
        # `auto` is the CLI's own alias; read-only is `--approval-mode plan`
        "models": ["auto", "pro", "flash", "flash-lite"], "default_model": "auto",
    },
    "codex": {
        "title": "ChatGPT (Codex)", "bin": "codex", "npm": "@openai/codex",
        "key_env": "CODEX_API_KEY", "check": ["--version"], "login": "codex login",
        # no model list to ship: the CLI picks its own default when none is given
        "models": [], "default_model": "",
    },
    "ollama": {
        "title": "Ollama", "bin": "", "npm": "", "key_env": "", "check": [], "login": "",
        "models": [], "default_model": "",
    },
}
# Providers that run as a local CLI (everything but Ollama's HTTP API).
CLI_KINDS = tuple(k for k, p in PROVIDERS.items() if p["bin"])
NEW_KINDS = ("gemini", "codex")         # behind the `ai_providers` flag


def install_command(kind: str) -> str:
    p = PROVIDERS.get(kind) or {}
    return f"npm install -g {p['npm']}" if p.get("npm") else ""


# ---------------------------------------------------------------- keys
def _key_path(kind: str) -> Path:
    if kind not in PROVIDERS or not PROVIDERS[kind]["key_env"]:
        raise ValueError(f"{kind!r} takes no API key")
    return graph.home_dir() / SECRETS_DIR / f"{kind}.key"


def save_key(kind: str, key: str) -> None:
    key = (key or "").strip()
    if not key or re.search(r"\s", key) or len(key) > 500:
        raise ValueError("an API key is one token with no spaces")
    p = _key_path(kind)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.parent.chmod(0o700)
    except OSError:
        pass
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key)
    tmp.replace(p)


def delete_key(kind: str) -> bool:
    p = _key_path(kind)
    if not p.exists():
        return False
    p.unlink()
    return True


def load_key(kind: str) -> str:
    try:
        return _key_path(kind).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""


def child_env(kind: str, env: dict[str, str]) -> dict[str, str]:
    """`env` with the provider's saved key set. No saved key: untouched, so the
    CLI signs in the way it was set up (OAuth)."""
    var = (PROVIDERS.get(kind) or {}).get("key_env") or ""
    key = load_key(kind) if var else ""
    return {**env, var: key} if key else env


def redact(text: str, kind: str) -> str:
    """`text` without the provider's saved key, for anything that gets shown or logged."""
    key = load_key(kind) if (PROVIDERS.get(kind) or {}).get("key_env") else ""
    return text.replace(key, "***") if key else text


# ---------------------------------------------------------------- state
def detect(kind: str, bin_name: str = "") -> dict[str, Any]:
    """Is the CLI installed, and which version. Never raises."""
    p = PROVIDERS.get(kind) or {}
    name = bin_name or p.get("bin") or ""
    if not name:
        return {"installed": False, "path": "", "version": ""}
    path = _platform.resolve_bin(name)
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        return {"installed": False, "path": "", "version": ""}
    version = ""
    try:
        import _textio
        r = _textio.run_text([path, *p["check"]], timeout=10)
        version = (r.stdout or r.stderr or "").strip().splitlines()[0][:80] if (r.stdout or r.stderr) else ""
    except Exception:
        pass
    return {"installed": True, "path": path, "version": version}


def listing() -> list[dict[str, Any]]:
    """What the setup wizard shows per provider. Never carries a key, only whether one is saved."""
    out = []
    for kind, p in PROVIDERS.items():
        out.append({"kind": kind, "title": p["title"], "install": install_command(kind),
                    "key_env": p["key_env"], "login": p["login"], "models": p["models"],
                    "has_key": bool(p["key_env"] and load_key(kind)),
                    **(detect(kind) if p["bin"] else {}),
                    **brain_state(kind)})
    return out


# ---------------------------------------------------------------- commands
def cli_command(kind: str, c: dict[str, Any], model: str, full: bool) -> list[str]:
    """argv for one turn of gemini or codex. The prompt goes in on stdin.
    Read-only unless the conversation has tools on."""
    p = PROVIDERS[kind]
    exe = _platform.resolve_bin(str(c.get("bin") or p["bin"]))
    if kind == "gemini":
        # `-p ""` forces headless mode; the piped prompt is the whole input
        cmd = [exe, "-p", "", "-o", "text", "--approval-mode", "yolo" if full else "plan"]
        if model:
            cmd += ["-m", model]
        return cmd
    if kind == "codex":
        cmd = [exe, "exec", "--skip-git-repo-check",
               "--sandbox", "workspace-write" if full else "read-only"]
        if model:
            cmd += ["-m", model]
        return cmd + ["-"]
    raise ValueError(f"{kind!r} is not a CLI provider")


# ---------------------------------------------------------------- brain access
# How each CLI learns to read and write the brain on its own, outside the Agents tab:
# Claude takes the memory-ledger plugin from this checkout's marketplace; Gemini and Codex
# have no plugin of ours, so a marked block goes in the context file each reads at start.
PLUGIN = "memory-ledger"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
CONTEXT_FILE = {"gemini": (".gemini", "GEMINI.md"), "codex": (".codex", "AGENTS.md")}
BLOCK_START, BLOCK_END = "<!-- memory-ledger:begin -->", "<!-- memory-ledger:end -->"


def context_path(kind: str) -> Path | None:
    d = CONTEXT_FILE.get(kind)
    return Path.home() / d[0] / d[1] if d else None


def context_block() -> str:
    g = f"python3 {Path(__file__).resolve().parent / 'graph.py'}"
    q = f"python3 {Path(__file__).resolve().parent / 'brain_query.py'}"
    return "\n".join([
        BLOCK_START,
        "## Memory Ledger (the owner's second brain)",
        "The brain is yours to read and write, without asking first. Run these exactly as "
        "written, never after a `cd`:",
        f"- `{g} match-rules \"<what you are about to do>\"`: the owner's rules. Run it before acting.",
        f"- `{g} recall \"<topic>\"`, `search \"<text>\"`, `get-node --id <id>`: facts, people, "
        "projects, decisions.",
        f"- `{q} open --mine`: the owner's open loops.",
        f"- `{g} add-node --type <note|task|decision|person|...> --title ... --body ...` and "
        "`add-edge`: store what is worth keeping from the work.",
        BLOCK_END, ""])


def _plugin_installed(exe: str) -> bool:
    import json
    import _textio
    try:
        r = _textio.run_text([exe, "plugin", "list", "--json"], timeout=60)
        rows = json.loads(r.stdout or "[]")
    except Exception:
        return False
    rows = rows if isinstance(rows, list) else []
    return any(PLUGIN in str(x.get("id") or x.get("name") or "") for x in rows
               if isinstance(x, dict))


def brain_state(kind: str, bin_name: str = "") -> dict[str, Any]:
    """Does this provider's CLI already have the brain wired in. Never raises."""
    p = PROVIDERS.get(kind) or {}
    if kind == "claude":
        d = detect(kind, bin_name)
        return {"brain": d["installed"] and _plugin_installed(d["path"])}
    path = context_path(kind)
    if path and p.get("bin"):
        try:
            return {"brain": BLOCK_START in path.read_text(encoding="utf-8")}
        except OSError:
            return {"brain": False}
    return {}


def install_brain(kind: str, bin_name: str = "", timeout: int = 120) -> dict[str, Any]:
    """Give the provider's CLI the brain: the plugin for Claude, the context block for Gemini
    and Codex. Safe to repeat. Writes under the person's home, so the caller has their yes."""
    import _textio
    p = PROVIDERS.get(kind) or {}
    if not p.get("bin"):
        return {"ok": False, "error": f"{kind!r} has no CLI to give the brain to"}
    d = detect(kind, bin_name)
    if not d["installed"]:
        return {"ok": False, "error": f"{p['bin']} is not installed yet"}
    if kind == "claude":
        if _plugin_installed(d["path"]):
            return {"ok": True, "brain": True, "already": True}
        for argv in (["plugin", "marketplace", "add", str(PLUGIN_ROOT)],
                     ["plugin", "install", f"{PLUGIN}@{PLUGIN}"]):
            try:
                r = _textio.run_text([d["path"], *argv], timeout=timeout)
            except Exception as e:
                return {"ok": False, "error": f"claude {' '.join(argv[:2])} did not finish: {e}"}
            tail = ((r.stderr or "") + (r.stdout or "")).strip()[-400:]
            # an already-added marketplace is fine; any other failure stops here
            if r.returncode != 0 and "already" not in tail.lower():
                return {"ok": False, "error": tail or f"claude {argv[1]} exited {r.returncode}",
                        "command": "claude " + " ".join(argv)}
        return {"ok": True, "brain": _plugin_installed(d["path"]),
                "note": "restart open Claude Code sessions to load it"}
    path = context_path(kind)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        cur = path.read_text(encoding="utf-8") if path.exists() else ""
        block = context_block()
        if BLOCK_START in cur and BLOCK_END in cur:
            head, rest = cur.split(BLOCK_START, 1)
            new = head + block + rest.split(BLOCK_END, 1)[1].lstrip("\n")
        else:
            new = cur + ("\n" if cur and not cur.endswith("\n\n") else "") + block
        if new != cur:
            path.write_text(new, encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": f"could not write {path}: {e}"}
    return {"ok": True, "brain": True, "path": str(path)}


# ---------------------------------------------------------------- wizard actions
def install(kind: str, timeout: int = 300) -> dict[str, Any]:
    """Run the provider's `npm install -g`. The caller has the person's explicit yes."""
    import _textio
    cmd = install_command(kind)
    if not cmd:
        return {"ok": False, "error": f"{kind!r} has nothing to install"}
    npm = _platform.resolve_bin("npm")
    if not (os.path.isfile(npm) and os.access(npm, os.X_OK)):
        return {"ok": False, "error": "npm is not installed (it ships with Node.js)",
                "command": cmd}
    try:
        r = _textio.run_text([npm, "install", "-g", PROVIDERS[kind]["npm"]], timeout=timeout)
    except Exception as e:
        return {"ok": False, "error": f"npm did not finish: {e}", "command": cmd}
    tail = ((r.stderr or "") + (r.stdout or "")).strip()[-600:]
    if r.returncode != 0:
        return {"ok": False, "error": tail or f"npm exited {r.returncode}", "command": cmd}
    return {"ok": True, "command": cmd, **detect(kind), **brain_state(kind)}


def ping(kind: str, bin_name: str = "", timeout: int = 90) -> dict[str, Any]:
    """One real call, "reply ok", with the saved key if there is one. The answer or the error,
    with the key masked."""
    import _textio
    p = PROVIDERS.get(kind) or {}
    if not p.get("bin"):
        return {"ok": False, "error": f"{kind!r} is not a CLI provider"}
    if not detect(kind, bin_name)["installed"]:
        return {"ok": False, "error": f"{p['bin']} is not installed yet"}
    if kind == "claude":
        exe = _platform.resolve_bin(bin_name or p["bin"])
        cmd = [exe, "-p", "--output-format", "text", "--model", p["default_model"]]
    else:
        cmd = cli_command(kind, {"bin": bin_name}, "", False)
    try:
        r = _textio.run_text(cmd, timeout=timeout, input="Reply with the single word: ok",
                             env=child_env(kind, dict(os.environ)))
    except Exception as e:
        return {"ok": False, "error": redact(f"the call did not finish: {e}", kind)}
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        return {"ok": False, "error": redact(((r.stderr or "") or out).strip()[-600:], kind)
                or f"{p['bin']} exited {r.returncode} without an answer"}
    return {"ok": True, "answer": redact(out[:200], kind)}
