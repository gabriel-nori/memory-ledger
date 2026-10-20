#!/usr/bin/env python3
"""Where the ledger lives, and whether it is on.

This is the smallest module in the tree on purpose. It holds the answers a
caller needs *before* it can decide to do nothing — which config prefix wins,
where home is, whether the user turned the ledger off, and how the other
boolean switches (autosync, consolidate) resolve — and it imports nothing but
`json`, `os` and `pathlib` to give them.

The reason is the SessionStart path. `hooks/open_loops_brief.py` runs on every
session and its first act is to ask "am I even enabled?". Reaching that answer
through the full engine costs ~60 ms of transitive imports (urllib, argparse,
email), and it costs it *most* on the path that was supposed to be free: the
one where the user has turned capture off and the honest response is to exit.
A switch you pay for whether or not you flip it is a badly placed switch.

So the predicate moves down here and `graph.py` imports it back up, rather than
the hooks reaching down through `graph`. There is still exactly one
implementation — CONVENTIONS §2d — and `graph.disabled()` / `graph.enabled()`
still resolve to it, so nothing that already reads it through the engine has to
change. Never re-implement these; import them.
"""

import json
import os
from pathlib import Path

import _platform  # scripts/ is on the path of whoever imported this


def _env(name: str, default: str = "") -> str:
    """Config env lookup: MEMORY_LEDGER_<name>, else the legacy
    SECOND_BRAIN_<name>, else the default. The MEMORY_LEDGER_ prefix is
    canonical; the SECOND_BRAIN_ one keeps every existing shell rc, cron and
    launchd agent working and is not going away."""
    return (os.environ.get("MEMORY_LEDGER_" + name)
            or os.environ.get("SECOND_BRAIN_" + name)
            or default)


# ---------------------------------------------------------------------------
# where home is
#
# The product is `memory-ledger`, so the brain lives at `~/.memory-ledger`. It
# used to live at `~/.second_brain`, and on most machines it still physically
# does: adopt_canonical_home() points the new name at the old directory with a
# symlink rather than moving anything. Nothing is copied, nothing is renamed,
# and one directory of data keeps one identity on disk — which matters because
# the alternative, a move, would strand every absolute path a user has already
# baked into a cron, a backup job or a $SECOND_BRAIN_DB.
#
# Resolution accepts every spelling either name has plausibly been written in,
# newest first. A near-miss (`.memory_ledger` when you meant `.memory-ledger`)
# should find the brain that is there, not silently start an empty second one.
# ---------------------------------------------------------------------------
HOME_DIRNAME = ".memory-ledger"          # canonical

# Recognised in this order. The first that exists wins, so the canonical name
# always beats a legacy one when both are present.
HOME_CANDIDATES = (
    ".memory-ledger",
    ".memory_ledger",
    ".second_brain",                     # what it was actually called
    ".second-brain",
)


def home_dir() -> Path:
    """The brain's home — a single global directory OUTSIDE any repo, so every
    project on the machine shares one brain (memory, learned skills, configs,
    routines, definitions). Override with $MEMORY_LEDGER_HOME (or the legacy
    $SECOND_BRAIN_HOME). Default: ~/.memory-ledger, falling back to whichever
    legacy name exists (~/.second_brain on any machine that predates the
    rename).

    Pure: this only ever reads the filesystem. Creating the directory, and
    linking the canonical name to a legacy one, is adopt_canonical_home()'s
    job — a resolver that writes to disk every time something asks it a
    question is a resolver you cannot call from a read path."""
    env = _env("HOME")
    if env:
        return Path(env).expanduser()
    base = Path.home()
    for name in HOME_CANDIDATES:
        p = base / name
        if p.exists():                   # follows the symlink, so a live link wins
            return p
    return base / HOME_DIRNAME           # fresh install: the canonical name


def legacy_homes() -> list:
    """Every recognised non-canonical home that exists on this machine, as real
    directories rather than links to the canonical one. Normally empty (after
    adoption the canonical name IS the legacy dir, reached through a symlink);
    non-empty means two separate brains, which only a human can resolve."""
    base = Path.home()
    canonical = base / HOME_DIRNAME
    try:
        target = canonical.resolve() if canonical.exists() else None
    except OSError:
        target = None
    out = []
    for name in HOME_CANDIDATES[1:]:
        p = base / name
        if not p.exists():
            continue
        try:
            if target is not None and p.resolve() == target:
                continue                 # same directory, reached by two names
        except OSError:
            pass
        out.append(p)
    return out


def adopt_canonical_home() -> dict:
    """Make `~/.memory-ledger` the way in, without moving a byte.

    Called once per session by hooks/bootstrap.py, and by `graph.py init`.
    Idempotent, and never raises — a machine where the symlink cannot be created
    (no permission, a filesystem without links) must keep working on the legacy
    path, so every failure here degrades to "carry on as before".

      env override set   -> do nothing; an explicit path outranks any default
      canonical exists   -> do nothing
      legacy exists      -> symlink canonical -> legacy
      neither exists     -> mkdir canonical

    Returns what it did, so `init` and `doctor` can say so out loud.
    """
    if _env("HOME"):
        return {"action": "skipped", "reason": "$MEMORY_LEDGER_HOME/$SECOND_BRAIN_HOME is set"}
    base = Path.home()
    canonical = base / HOME_DIRNAME
    try:
        # A symlink whose target is gone: exists() is False but the name is
        # taken, so mkdir would fail. Say so rather than papering over it —
        # a dangling home is someone's deleted brain, not a thing to recreate.
        if canonical.is_symlink() and not canonical.exists():
            return {"action": "broken-link", "path": str(canonical),
                    "points_at": os.readlink(str(canonical)),
                    "reason": "the canonical home is a symlink to something that no longer exists"}
        if canonical.exists():
            return {"action": "already", "path": str(canonical)}
        for name in HOME_CANDIDATES[1:]:
            legacy = base / name
            if legacy.exists():
                # Relative (".second_brain", not "/Users/x/.second_brain") so the
                # link survives the home directory itself moving — a migrated
                # account, a remounted volume, a $HOME that differs between a
                # login shell and a launchd agent. An absolute link would break
                # in exactly the cases a symlink is supposed to survive.
                # On Windows, where a symlink needs developer mode or admin,
                # a junction (absolute, but needing no privilege) stands in.
                how = _platform.link_dir(canonical, legacy)
                return {"action": "linked", "path": str(canonical), "legacy": str(legacy),
                        "how": how}
        canonical.mkdir(parents=True, exist_ok=True)
        return {"action": "created", "path": str(canonical)}
    except Exception as e:                # never block a session over this
        return {"action": "failed", "path": str(canonical),
                "error": f"{type(e).__name__}: {e}",
                "reason": "falling back to whichever home already resolves"}


# ---------------------------------------------------------------------------
# the on/off switch
#
# The ledger used to be "enabled" when its home dir existed, which made
# `rm -rf <home>` the off switch — the same act as deleting your data. Now the
# home dir is created automatically on first session (hooks/bootstrap.py), so
# absence can no longer mean "off": a switch that auto-init silently undoes is
# not a switch. Being OFF is therefore something you state explicitly, and it is
# stated in the one place auto-init will never overwrite.
# ---------------------------------------------------------------------------
DISABLE_MARKER = ".disabled"

# Named apart from the `_TRUTHY` used by the gauge coercion in graph.py: that
# module imports these names, and two constants sharing one name would shadow.
_OFF_TRUTHY = {"1", "true", "yes", "on"}


def disabled() -> bool:
    """True when the ledger has been explicitly turned off, by either switch:

    - `$MEMORY_LEDGER_OFF=1` — per-process, so it scopes to one shell, one CI
      job, one session. Env vars do not reach a hook or a cron you did not
      launch yourself, which is exactly why it is not the only switch.
    - `<home>/.disabled` — machine-wide and persistent; written by `graph.py
      disable` (`/ledger off`) and removed by `graph.py enable`.

    Every capture hook and the bootstrap check this first and no-op when true.
    """
    if _env("OFF").strip().lower() in _OFF_TRUTHY:
        return True
    try:
        return (home_dir() / DISABLE_MARKER).exists()
    except Exception:
        return False        # never let a stat() failure silence the ledger


def enabled() -> bool:
    return not disabled()


# ---------------------------------------------------------------------------
# the other switches
#
# `disabled()` is not the only thing a hook has to know before deciding to do
# nothing: the SessionStart path also asks "should I consolidate?", and the
# capture path asks "should I push to the mirror?". Those live here for the same
# reason — they are read on the cheap path, by callers that must not pay for the
# full engine to be told to stand down.
#
# graph.py imports these back up, so `graph.flag()` and this are one
# implementation. Never re-implement them; import them.
# ---------------------------------------------------------------------------

#: Boolean settings and what each is when nobody has said. Spelled POSITIVELY
#: ("autosync: on") while the matching env var keeps the negative the engine has
#: always used ($MEMORY_LEDGER_NO_AUTOSYNC): a file you edit should say what you
#: want, an escape hatch you export for one shell should say what you suppress.
BOOL_SETTINGS = {"autosync": True, "consolidate": True, "ui_sync": False,
                 # learned rules (graph.py propose-rules): opt-in, never on by default
                 "learned_rules": False,
                 # gated per-prompt recall (hooks/capture_prompt.py): opt-in, it
                 # spends context tokens on every prompt it fires on
                 "prompt_recall": False,
                 # agent chats mirrored to the master (graph.agents_sync_on): opt-in
                 "agents_sync": False}

_BOOL_TRUE = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off"}


def settings() -> dict:
    """Read <home>/settings.json. Missing or corrupt reads as empty — a bad
    settings file must never stop the brain from answering."""
    try:
        val = json.loads((home_dir() / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return val if isinstance(val, dict) else {}


def _env_says_off(name: str) -> bool:
    """True when $MEMORY_LEDGER_NO_<NAME> is set to something that means yes.
    Explicitly setting it to a falsey value means "do not suppress", so it
    reads as unset rather than as a second way to say off."""
    return _env("NO_" + name.upper()).strip().lower() not in _BOOL_FALSE | {""}


def flag(name: str) -> bool:
    """One boolean setting, resolved the way the engine resolves everything.

    Order is flag > env > file > default, with one wrinkle: the env var is the
    NEGATIVE, because that is the spelling every existing rc file, cron and CI
    job already uses and those must not stop working. So a truthy `NO_<NAME>`
    forces the switch OFF and outranks the file, exactly as $MEMORY_LEDGER_OFF
    outranks the .disabled marker.
    """
    if _env_says_off(name):
        return False
    raw = settings().get(name)
    if raw is None:
        return BOOL_SETTINGS.get(name, False)
    if isinstance(raw, bool):
        return raw
    low = str(raw).strip().lower()
    if low in _BOOL_TRUE:
        return True
    if low in _BOOL_FALSE:
        return False
    return BOOL_SETTINGS.get(name, False)   # hand-edited nonsense reads as unset


def flag_source(name: str) -> str:
    """Where flag() got its answer — "env", "settings" or "default". The
    Settings page shows this, because a switch that says "off" without saying
    WHY is the same lie as a timezone field that hides an env override."""
    if _env_says_off(name):
        return "env"
    return "settings" if settings().get(name) is not None else "default"


def hook_context(event: str, text: str) -> str:
    """The JSON a hook prints to put `text` into the model's context.

    Claude Code reads `additionalContext` only under `hookSpecificOutput`, next
    to the event it answers. A top-level `{"additionalContext": ...}` is not a
    documented field and is dropped, which is how the open-loops brief and the
    per-prompt note both went unread while looking like they worked."""
    return json.dumps({"hookSpecificOutput": {"hookEventName": event,
                                              "additionalContext": text}})
