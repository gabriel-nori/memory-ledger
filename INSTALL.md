# Installing Memory Ledger

Memory Ledger is a Claude Code plugin. Installing it makes its skills, the
`/ledger` command, and the always-on capture hooks available **globally** (every
project, every session). It works in Claude Code and the Claude app.

> **One name now.** The product, the repo, the marketplace and the plugin id are
> all **`memory-ledger`**, and the command is `/ledger`. They were `second-brain`
> up to 0.6.0. `/brain` was the old command name and is gone.
>
> **Your data does not move.** Your memory lives in `~/.second_brain` and stays
> there. `~/.memory-ledger` is the name the tools use now, and on an existing
> machine it is a **symlink** to that directory, created on your next session —
> both paths reach the same graph. `$SECOND_BRAIN_HOME` / `$SECOND_BRAIN_DB` keep
> working alongside the `$MEMORY_LEDGER_*` names. Renaming the plugin does not
> touch a single node.
>
> Already running the old plugin? See
> [Upgrading from second-brain](#upgrading-from-second-brain).

## Prerequisites

- **Claude Code** (recent version — plugin marketplaces + `Stop`/`UserPromptSubmit` hooks).
- **Python 3** on your `PATH` (the engine and hooks are stdlib-only — no packages).
- **git** (for the brain's own history and, optionally, installing from GitHub).

Nothing else. The engine, the CLI and the hooks have zero third-party
dependencies. The optional web UI's server does (FastAPI, uvicorn), and installs
them itself into its own venv the first time you start it — see below.

### One click: `install.sh` / `install.ps1`

From a release zip or a clone, with or without Python on the machine:

```bash
./install.sh                                                 # macOS, Linux
powershell -ExecutionPolicy Bypass -File install.ps1         # Windows
```

It looks for Python 3.11+ (the version pinned in `.python-version` is tried
first). If there is none it installs [`uv`](https://astral.sh/uv) and the pinned
Python into `~/.second_brain` (nothing system-wide, nothing in your PATH), then
runs `scripts/setup.py --launcher`. Setup builds the venv, builds the UI (a
release already ships it; from git it downloads a portable Node LTS, checked
against nodejs.org's SHA-256, when `npm` is missing), creates the brain, and puts
**Memory Ledger** on the Desktop (`.command` on macOS, `.lnk` on Windows,
`.desktop` on Linux). Clicking it runs `scripts/launcher.py start`: it starts
`serve.py`, waits for `/api/health` and opens the page. `launcher.py stop` shuts
it down; `status` and `install` (re-make the Desktop icon) also exist. The log is
`<home>/logs/serve.log`. A new release shows up as a banner in the page: see
[Updates](docs/api.md#updates).

### One command to set a machine up: `scripts/setup.py`

From a clone, on macOS, Linux or Windows:

```bash
./scripts/setup.sh                                          # macOS, Linux
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1   # Windows
python3 scripts/setup.py --voice                            # also dictation (and --piper for TTS)
```

The wrappers only find a Python 3.10+ (`py -3` first on Windows) and run
`setup.py`, which: builds the server's venv, runs `npm ci` and `npm run build`
for the UI (reported and skipped when npm is missing), detects the TTS engine
for this OS and ffmpeg, with `--voice` builds a separate voice venv with
faster-whisper and downloads the STT model (`--stt-model`, default `medium`),
runs `graph.py init` (the brain, with its built-in skills), and ends with
`doctor`, whose verdict is the exit code. It is idempotent, so run it again
after an upgrade. `--json` prints one report; `--skip-venv` and `--skip-ui`
leave those steps out. Nothing here is required: the plugin works without it.

`--voice` builds the voice venv with Python 3.13, 3.12 or 3.11 (the first found;
faster-whisper needs PyAV, which has no wheel for newer Pythons), and recreates
an existing voice venv that runs another version. The desktop launcher also
offers the voice setup once, on the first start in a terminal: it installs
`ffmpeg` (macOS, Homebrew) or `espeak-ng` (Linux, apt/dnf/pacman/zypper), then
runs `setup.py --voice --skip-ui`. The answer is kept in `<home>/run/voice-offer`,
so a "no" is never asked again; run `setup.py --voice` any time.

## 1. Install the plugin (global)

The plugin ships its own marketplace manifest (`.claude-plugin/marketplace.json`),
so you add the repo as a marketplace, then install from it.

**From GitHub directly** (no manual clone — this is now the normal path):
```
/plugin marketplace add Optibus/memory-ledger
/plugin install memory-ledger@memory-ledger
```
`Optibus/memory-ledger` is a **private** repo, so this shells out to `git
clone` over HTTPS using your own git credentials — Claude Code doesn't
prompt for a login. Make sure `git` can already clone it before you run
`/plugin marketplace add`:
```
gh auth setup-git     # wires gh's token into git's credential helper
```
(or configure any other credential helper that already works for private
Optibus repos). Skip this and the failure you get is an opaque `git clone`
error, not a helpful "auth required" message — if the marketplace add fails,
check git auth first.

Prefer SSH? Use the SSH remote instead:
```
/plugin marketplace add git@github.com:Optibus/memory-ledger.git
/plugin install memory-ledger@memory-ledger
```

**From a local clone** (if you already keep one, or want to hack on it):
```
git clone git@github.com:Optibus/memory-ledger.git ~/dev/memory-ledger
```
then, inside Claude Code:
```
/plugin marketplace add ~/dev/memory-ledger
/plugin install memory-ledger@memory-ledger
```

**Dev mode** (try it for one session without installing):
```
claude --plugin-dir ~/dev/memory-ledger
```

Restart the session (or run `/reload-plugins`) after installing.

**From a zip** (no GitHub access needed — see [below](#no-repo-access-install-from-a-zip)).

### Runs on every session (user-level)

Installing via `/plugin install` enables the plugin at **user level**, so it's
active in **all projects and all sessions** — the hooks are not scoped to any repo:

- The capture hooks (`UserPromptSubmit`, `Stop`) are **matcher-less** → they fire
  on every prompt and every completed turn, in every session.
- The session brief (`SessionStart`) fires on `startup`, `resume`, `clear`, and
  `compact`, so your open loops resurface however a session begins.
- Hook commands use `${CLAUDE_PLUGIN_ROOT}` (absolute) and the global brain home
  (`~/.second_brain`), so they behave identically regardless of the session's `cwd`.

Nothing extra is needed to "make it global" — user-level enable is the switch.
(Sub-agents don't double-capture: they have no `UserPromptSubmit`, and `Stop` is
the main agent's turn, not `SubagentStop`.)

## 2. That's it — no manual init

Older versions required you to `mkdir ~/.second_brain` and run `graph.py
init` by hand before anything worked. That step is gone. A `SessionStart`
hook (`hooks/bootstrap.py`) now runs on every session, before the open-loops
brief, and calls `graph.connect()` — which creates the home dir, the DB file,
and the schema the first time it's missing. It prints nothing, and after that
first run it's a cheap no-op (open a SQLite connection, confirm the schema is
there).

Installing the plugin is enough. Send any message in a fresh session and the
ledger is already capturing it.

The one thing bootstrap deliberately won't do is turn a disabled ledger back
on — see the switch below.

### Optional: set your timezone

Date views (open loops, agenda) bucket by timezone, so set yours once — either
in the UI (**Settings › Time zone**, which writes `~/.second_brain/settings.json`
and is read by the CLI, the hooks, and any cron too), or in your shell rc:
`export MEMORY_LEDGER_TZ="America/Sao_Paulo"`. The env var wins over the file.

### Optional: a local neo4j, in one command

Also not required. SQLite is the default and is enough for one person's brain;
neo4j buys **query shape** (variable-depth paths, shortest path, centrality), not
speed — measured, it costs ~850 MiB resident against 26 MiB. Read
[docs/neo4j.md](docs/neo4j.md) before turning it on.

```
python3 ~/dev/memory-ledger/scripts/neo4j_local.py up     # or /neo4j-up in Claude Code
```

Idempotent, and it does the whole job: a `neo4j:5` container with
**`--restart unless-stopped`**, the store bind-mounted at
`~/.second_brain/neo4j/{data,logs}`, bound to `127.0.0.1` only, a password
generated into `~/.second_brain/neo4j.json` (chmod 600, never printed), the
`backend` setting flipped to `auto`, and the existing brain backfilled into it.
Needs Docker; nothing else.

```
python3 ~/dev/memory-ledger/scripts/neo4j_local.py status
python3 ~/dev/memory-ledger/scripts/neo4j_local.py down      # stops, and stays stopped
```

If a master is **already** configured, `up` refuses to repoint it until you pass
`--switch` (the old config is backed up first) or `--keep-config`.

The `backend` setting it writes is where `brain_query.py` reads from when no
`--backend` flag is given: `auto` (neo4j when reachable, else the local mirror),
`neo4j` (master only), or `sqlite`. Change it any time in the UI's
**Settings › Database connection**.

### Optional: mirror one brain across machines

**Nothing here is required.** Out of the box the brain is one local SQLite file —
offline, instant, and connected to nothing.

If you want the same brain on more than one machine, point it at a graph server you
control by creating `~/.second_brain/neo4j.json`:

```json
{ "url": "https://<your-graph-host>:<port>", "db": "neo4j", "user": "…", "password": "…" }
```
(or the env vars `MEMORY_LEDGER_NEO4J_URL / _DB / _USER / _PASSWORD`). Writes land in
SQLite immediately and mirror through an offline outbox, so an unreachable mirror
never blocks or loses a write.

Two things worth getting right if you do this:
- **Use TLS.** The credentials and your whole graph cross the network on every write.
- **Keep the config file out of any repo — it holds a secret.** Restrict it with
  `chmod 600`, and never paste the credentials into a command that gets saved (a
  shell history or a tool-permission allowlist counts as saved).

### Optional: summon it from Slack, Jira and GitHub

A mention in a Slack thread, or a Jira comment that mentions you with a marker
like `@brain`, can start a headless Claude Code session that answers there as
you. So can a GitHub mention, or the pull request changes you pick (a review on
your PR, a review request, failing checks, a merge, …). It needs a Slack app in
Socket Mode (listen-only, no `chat:write`), a Jira API token, a GitHub token,
and `<home>/trigger.json`; the watchers then run inside
`scripts/serve.py`. Set all of it, switch each service on or off, and check it,
in the web UI under **Settings → Triggers**, or from the shell:

```
python3 scripts/trigger.py check
```

Setup, scopes and every key: [docs/trigger.md](docs/trigger.md#setting-it-up).

## 3. The on/off switch

Enablement used to just mean "the home dir exists" — deleting `~/.second_brain`
*was* the off switch. Now that bootstrap auto-creates that dir on every
session, its absence can't mean "off" any more (bootstrap would just recreate
it, empty, on the next session start). So being off is now a state you set
explicitly, on purpose, in one of two ways:

- **`/ledger off`** (or `python3 scripts/graph.py disable`) — writes
  `<home>/.disabled`. Machine-wide and persistent: it survives restarts,
  every hook checks for it first, and it stays off until you turn it back on.
  **`/ledger on`** (or `graph.py enable`) removes the marker. Both are
  idempotent.
- **`MEMORY_LEDGER_OFF=1`** — per-process. Scopes to one shell, one CI job, or
  one session, and forces the ledger off for exactly that process regardless
  of the marker file. Note: `graph.py enable` will **warn** (not silently
  succeed) if this is set in your shell, because removing the marker alone
  won't turn capture back on there — you also have to unset the env var
  wherever it's exported (rc file, cron, launchd).

**Deleting `~/.second_brain` still deletes your DATA** — it is not, any more,
how you turn the ledger off. Bootstrap will just recreate an empty home dir
on the next session. Use `/ledger off` if you want the ledger off; only
delete the directory if you actually want to lose what's in it.

## 4. Env vars

`MEMORY_LEDGER_*` is the canonical prefix. The legacy
`SECOND_BRAIN_*` names all still work as a fallback (checked in that order —
see `_env()` in `scripts/graph.py`) and are not going away, so nothing you
already have exported breaks.

- `$MEMORY_LEDGER_HOME` (was `$SECOND_BRAIN_HOME`) — override the home dir.
- `$MEMORY_LEDGER_DB` (was `$SECOND_BRAIN_DB`) — override the exact DB file.
- `$MEMORY_LEDGER_TZ` (was `$SECOND_BRAIN_TZ`) — timezone for date bucketing;
  the env var wins over `settings.json`.
- `$MEMORY_LEDGER_REALM` (was `$SECOND_BRAIN_REALM`) — default section for new
  nodes (`default`, `work`, `personal`, ...).

**The home directory is now `~/.memory-ledger`, and your data still isn't
moving.** On a machine that predates the rename, the first session creates
`~/.memory-ledger` as a *symlink* to the existing `~/.second_brain` (on Windows,
where a symlink needs developer mode or admin, a *junction*). Nothing is
copied, nothing is renamed, and every absolute path you have already baked into a
cron job, a backup or a `$SECOND_BRAIN_DB` keeps resolving. Both names work, and
so do `$MEMORY_LEDGER_HOME` and `$SECOND_BRAIN_HOME`.

A fresh machine, with no previous brain, just gets a real `~/.memory-ledger`
directory. Run `/ledger doctor` to see which case you are in — it prints the home
it resolved and, when it is a link, what it points at.

If you would rather keep the old name as the real path and skip the link
entirely, set `$MEMORY_LEDGER_HOME=~/.second_brain`; an explicit override outranks
all of this and no symlink is created.

## 5. What turns on automatically (no model call)

Once installed, hooks run in **pure Python/SQL — no model call**:

- **SessionStart** → runs `bootstrap.py` (see step 2), then injects a brief of
  your open loops (`open_loops_brief.py`), and **once a day** kicks off
  consolidation in the background: closed days' captures are folded into
  digest notes (originals kept). Detached, so it never delays the session;
  disable with `MEMORY_LEDGER_NO_CONSOLIDATE=1`. It runs here rather than as a
  scheduled cloud routine because the brain is a local file — a cloud routine
  cannot read it. If you want it to run even on days you never open Claude
  Code, add a launchd agent calling `graph.py consolidate --older-than 1`.
- **UserPromptSubmit** → `capture_prompt.py` stores each prompt as a `capture` node
  (routine ones — "ok", slash commands, pasted markup — are filtered first;
  `capture_filter`: `drop` | `cold` | `off`)
  and auto-creates `task` nodes for explicit to-dos ("remind me…", "todo:", "follow
  up…"), linked back to the turn. It injects nothing unless you opt in to gated
  recall (the Prompt recall switch on the Settings page, or `"prompt_recall": true`
  in `<home>/settings.json`): then up to two short
  snippets of clearly matching nodes, never the same one twice in a session
  (CONVENTIONS §0).
- **Stop** → `capture_answer.py` stores an extractive summary of the answer, linked
  to its question.
- **PreToolUse / PostToolUse** (matcher `Task`) → `capture_agent.py` puts each
  subagent in the **agent tree** as it spawns and stamps its outcome
  (`completed` / `failed`) as it returns, so what a subagent considered does not
  vanish when its branch ends (CONVENTIONS §2k). Other tools are ignored.
- **PreCompact** → `close_conversation.py compact` records one `summarized`
  context event: the one moment Claude Code says the window changed wholesale.
- **SessionEnd** → `close_conversation.py end` closes the conversation and stamps
  an outcome on every agent still open, so no branch is left reading like a crash.

So every interaction is remembered without a single model call. Two of the hooks do
add a short line to the conversation — 209 chars per prompt and a ~1.0 KB
open-loops brief per session start (everything else adds nothing) — which measured
**under 0.05% of a working day**. Recall itself runs outside the conversation
and costs nothing:

```
python3 scripts/brain_query.py agenda          # today/tomorrow/week/closed/range
python3 scripts/graph.py search "<terms>"      # full-text over the graph
python3 scripts/graph.py open-loops            # everything still awaiting action
python3 scripts/graph.py tree --session <sid>  # the agent tree, with its ideas
python3 scripts/graph.py recheck               # what became possible again (§2k)
```

### The brain UI (optional)

```
python3 scripts/serve.py                 # http://127.0.0.1:8770
python3 scripts/serve.py --no-write      # read-only
```
**No install step by hand, but the UI needs Node.js.** It is a React app, and
its bundle is not in git: the first start builds it (`npm` must be on PATH) and
later starts rebuild it when the source is newer. Without node the server still
starts and the API answers; only the page is a 503 saying how to build it.
The API behind it is a FastAPI app: the **first** start builds a venv for it
under `~/.cache/memory-ledger/` (this needs the network once; on Debian/Ubuntu,
`python3 -m venv` needs the `python3-venv` package). Every start after that is
immediate. Offline, point `$MEMORY_LEDGER_VENV` at a venv that already has
`fastapi` and `uvicorn`. See [the API](docs/api.md#the-servers-environment).
(Change the UI's source with node installed and the server rebuilds the bundle
on the next start, so you never serve a page older than your edit.)

Five sections behind the hamburger menu (`m`), each with its own URL: **Map**
(live graph), **Dashboard** (the query views), **People** (person nodes, what each
connects to, and merging the ones that are the same human), **Skills** (the two
contract trees and the rule catalogue), and **Settings** (your name, timezone,
default realm, the capture switch, database connection, consolidation, the
health check, and the Slack/Jira/GitHub triggers). The API documents itself at
[`/api/docs`](http://127.0.0.1:8770/api/docs). The dashboard also **edits** the brain — create tasks
with auto-recognised links, complete them, change status/assignee/due, rename, and
add/remove relations — all token-free (no model). Set the timezone once in
Settings rather than per command. Add `--host 0.0.0.0` to reach it from your phone
on the LAN / a tunnel; because the write API is **unauthenticated**, pair that with
`--no-write` unless you trust every device on the network.

## 6. Verify

With the manual init step gone, there's no longer an obvious moment that
proves the ledger is wired up — so there's a dedicated health check:

```
/ledger doctor          # or: python3 scripts/doctor.py
```

It reports, in plain language:
- **Status** — enabled or disabled, and if disabled, which switch did it (the
  `.disabled` marker or the `MEMORY_LEDGER_OFF`/`SECOND_BRAIN_OFF` env var).
- **Paths** — the resolved home dir and DB file, and whether each exists.
- **Config** — the effective timezone, realm and owner, plus the `autosync` and
  `consolidate` switches, and where each came from (env var, `settings.json`, or
  a built-in default).
- **Content** — total node count, a breakdown by type, open-loop count, and
  how stale the newest capture is (a warning if nothing's landed in the last
  2 days, or ever).
- **Built-in skills** — whether every skill in the plugin's `skills/` has its
  node in the brain. Any that are missing are ingested on the spot (the same
  idempotent pass as `graph.py ingest-skills`), and the report says how many.
- **Hook registration** — every hook in `hooks.json` per event, with its
  matchers, and whether the script file it points at actually exists.
- **Mirror status** — whether a neo4j mirror is configured and how many
  writes are pending in the offline outbox, without ever printing the
  credentials themselves.

Add `--json` for machine-readable output. It exits non-zero only when
something is **actionably** wrong (a missing DB, a hook pointing at a missing
script, unreadable config, a built-in skill that could not be ingested) — being disabled is a choice, not a fault, and
never affects the exit code by itself.

Complementary, Claude-Code-side checks:
```
/plugin              # memory-ledger shows as installed + enabled
/hooks               # SessionStart, UserPromptSubmit, Stop, PreToolUse,
                     # PostToolUse, PreCompact, SessionEnd are listed
claude plugin list   # from the CLI
```
Then send any message and check it landed:
```
python3 scripts/graph.py list --type capture --all --limit 5
python3 scripts/graph.py list --type conversation --all --limit 3   # the tree's root
```

Run the traceability tests any time (a throwaway brain in a temp dir — it never
touches `~/.second_brain`):
```
python3 -m pytest tests/test_traceability.py   # needs pytest: python3 -m pip install pytest
```

## No repo access? Install from a zip

`/plugin marketplace add Optibus/memory-ledger` needs the person to be able to read
the GitHub repo. Until that access exists, hand them a zip — Claude Code installs a
plugin from a **directory**, so unzipping *is* the install.

Build one:

```
python3 scripts/bundle.py                  # dist/memory-ledger-<version>.zip
python3 scripts/bundle.py --list           # what would go in
python3 scripts/bundle.py --out ~/Desktop
python3 scripts/bundle.py --include ~/.second_brain/exports/<your-doc>.md
python3 scripts/bundle.py --with-brain     # engine + YOUR brain, into ~/.second_brain/exports/
```

It packs **git-tracked files only** — so `.gitignore` decides what is excluded and
no brain data, eval cases or generated deck can ride along — under a single
top-level `memory-ledger/` directory, and generates two files into the archive:
`INSTALL-FROM-ZIP.md` (the recipient's whole procedure) and `BUNDLE.txt` (version,
commit, file list). Anything untracked is reported as a warning and left out, so
`git add` new files before building.

**`--with-brain` to move or back up the whole thing.** The one way brain data gets
into a bundle, and only when asked: it adds `memory-ledger/brain/` with a
consistent snapshot of `graph.db` (SQLite's backup API, safe while the server is
writing) and the home files the neo4j mirror carries — settings, your scripts
(executable bits kept), references, learned skills. Secrets (`neo4j.json`,
`trigger.json`) are never packed; the snapshot forgets its sync writer id, so a
copy restored elsewhere is a new writer. The zip is personal data: it defaults to
`~/.second_brain/exports/` (never `dist/`), is written owner-only (`0600`), and its
`INSTALL-FROM-ZIP.md` says how to put the brain in place without overwriting one.

**Restoring a backup: `scripts/restore.py`.** One script for both kinds of backup
— a `--with-brain` zip or a `.db` snapshot from `~/.second_brain/backups/`:

```
python3 scripts/restore.py --list                 # the backups it can find
python3 scripts/restore.py BACKUP --dry-run       # what it would do
python3 scripts/restore.py BACKUP                 # onto a home with no brain yet
python3 scripts/restore.py BACKUP --replace       # over the current brain
```

The backup is unpacked to a temp dir and integrity-checked before anything is
touched, so a bad backup restores nothing. An existing brain is never overwritten
without `--replace`, and with it the current one is first kept as
`backups/graph.db.pre-restore-<stamp>` (never pruned). Existing home files are
kept unless `--overwrite-files`, which saves each one it replaces. The database is
written in place through SQLite's backup API, so a running server sees the
restored brain without a restart. With a neo4j master configured, the next sync
brings down what was written there after the backup — right for a lost machine,
worth knowing before a rollback.

**Exporting a node file to hand over: `scripts/export_handoff_nodes.py [OUT.json]`.**
Writes a `second-brain-nodes/1` file with the non-personal work nodes (projects,
artifacts, resources, references, decisions, Jira issues, PRs, and the open tasks of
`--assignee`) and the edges among them, for `import_nodes.py` on the other side. It
skips the personal and rules-backup realms, captures, conversations, messages, done
tasks, other people's tasks and anything matching the personal keywords. It only reads
`graph.db`. `--owner` and `--for` record sender and recipient; the default path is
`exports/handoff/nodes-YYYYMMDD.json`.

**Importing a node file into a ledger: `scripts/import_nodes.py FILE`.** Brings the
nodes and edges of a handoff export into another brain. It is a dry-run unless
`--apply`, which first backs up `graph.db` (restorable with `restore.py`). Nodes
keep their original id and dates, so a second run adds nothing; nodes already
present are left alone unless `--update` (title, body, status, due, props — never
the realm). Imported nodes land in the `handoff` realm (`--realm` to change it) and
carry `imported_from` and `imported_at`. Edges come in only when both ends exist.
Nothing is sent to neo4j; the changes wait for the next sync. A file in another
format is refused.

**`--include` for anything company-specific.** The plugin is agnostic — it holds no
company's rules, runbooks or names. Material like that belongs outside the repo (in
your brain home, which is never committed) and rides along at bundle time:
`--include <path>` puts the file at the archive root and lists it under *attached*,
so the recipient can tell the plugin from what you attached to it. Repeatable.

The recipient runs:

```
unzip memory-ledger-<version>.zip -d ~/dev
```
then, in Claude Code:
```
/plugin marketplace add ~/dev/memory-ledger
/plugin install memory-ledger@memory-ledger
```
`mkdir -p ~/.second_brain && python3 ~/dev/memory-ledger/scripts/graph.py init`, and
they are running. No admin, no marketplace, no repo permission. When repo access
lands they can clone over it and get updates with `git pull`; `~/.second_brain` is
outside the repo, so the brain survives the swap.

The bundle carries no memory of yours. To share **rules** rather than code, export
the ones you want as a Markdown doc kept **outside this repo** (e.g.
`~/.second_brain/exports/<team>-rules.md`) with a paste block of `graph.py add-rule`
calls at the end, then attach it with `--include`. The recipient's agent then pulls
only the rules that match what they are doing (`match-rules`) instead of reading the
whole document.

## Upgrading from second-brain

The plugin id changed in **0.7.0**, so Claude Code treats it as a different
plugin. Nothing migrates automatically, and the old one keeps working until you
remove it, which means both can be installed at once and their skills will
collide. Remove the old one first:

```
/plugin uninstall second-brain@second-brain
/plugin marketplace remove second-brain
```
then install the new one as in step 1.

**What changes for you:** skills and commands are invoked under the new prefix.
`/second-brain:plan-today` becomes `/memory-ledger:plan-today`, and the same for
every other skill. `/ledger` is unchanged.

**What does not change:** your brain. Every node, edge, rule and learned skill
lives in `~/.second_brain/graph.db`, outside the plugin, and is untouched by the
rename. Do not delete that directory, and do not rename it: `$SECOND_BRAIN_HOME`,
`$SECOND_BRAIN_DB` and every path in the docs still point there on purpose.

**If you cloned the repo**, the remote moved too. Repoint it rather than
re-cloning, so your local history and branches survive:

```
git remote set-url origin git@github.com:Optibus/memory-ledger.git
```
GitHub redirects the old URL, so a stale remote keeps working silently and you
will not notice it is wrong. The local directory name does not matter: the plugin
resolves itself from `$CLAUDE_PLUGIN_ROOT`, never from what the folder is called.

## Uninstall

```
/plugin uninstall memory-ledger@memory-ledger
```
Your brain data in `~/.second_brain` is left untouched (delete that dir to remove it —
but see the on/off switch above if what you actually want is to turn capture off,
not lose your data).
A local neo4j container is separate: `python3 scripts/neo4j_local.py down --remove`,
and delete `~/.second_brain/neo4j` to drop its copy of the graph.
