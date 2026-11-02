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
                    "key_env": p["key_env"], "models": p["models"],
                    "has_key": bool(p["key_env"] and load_key(kind)),
                    **(detect(kind) if p["bin"] else {})})
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
