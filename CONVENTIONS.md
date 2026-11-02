# memory-ledger — shared conventions

Every memory-ledger skill reads this once per session before acting. It defines
(1) how we talk to the person, (2) the memory graph API, (3) the vocabulary.

---

## 0. Always-on capture — the offload rule (non-negotiable)

When the brain is enabled, **every interaction leaves brain data** — even a
simple answered question. The whole point is to offload working memory to the
machine so questions and decisions are recallable later. This baseline is
**automatic and needs no model call**: a `UserPromptSubmit` hook
(`hooks/capture_prompt.py`) stores each prompt (and auto-creates tasks for
explicit to-dos), and a `Stop` hook (`hooks/capture_answer.py`) stores a summary
of the answer — all in pure Python/SQL. You don't need to re-store the turn; add
richer nodes/links only for things the heuristics miss.

**What "token-free" means here — precisely.** No skill, hook or query ever spends
tokens *deciding* or *storing*: that work is regex + SQL. What does cost tokens is
text that lands in the conversation — command output you read, and the short
`additionalContext` the SessionStart brief injects. Hooks print it as
`{"hookSpecificOutput": {"hookEventName": "<event>", "additionalContext": "…"}}`
(`_switch.hook_context`); a top-level `additionalContext` is not read by Claude
Code and is silently dropped. Measured 2026-07-27, revised 2026-10-02:

| Hook | Injected into context | Latency |
|---|---|---|
| `UserPromptSubmit` | **nothing** by default. With the opt-in `prompt_recall` setting, at most 2 snippets of ≤200 chars (~110 tokens), only on prompts that clearly match a node | 64 ms |
| `SessionStart` | **~1.1 KB** (~290 tokens; "capture is on" once, plus the open-loops brief, capped at 6 loops so it does not grow with the graph). Sits in the cached prefix | 185 ms |
| `Stop` | **nothing** (writes only) | 57 ms |

"Capture is on, don't re-store the turn" is said once per session, in the
SessionStart brief, not on every prompt: the same message on every prompt was
~52 tokens a prompt that taught the model nothing new after the first.

**Gated per-prompt recall** (`prompt_recall`, off by default; turn on with the
Prompt recall switch on the Settings page or `"prompt_recall": true` in
`<home>/settings.json`, force off with
`MEMORY_LEDGER_NO_PROMPT_RECALL=1`). Decided by FTS + regex, no model call. A node
is injected only when it contains ≥2 of the prompt's content terms covering
≥60% of them, or ≥1 when the prompt points at past work ("we decided", "last
time", a ticket key, a PR link). Never a `capture`, `rule` or digest, never a
`personal`-realm node, never the same node twice in a session, and never into a
respond session (`$MEMORY_LEDGER_REQUEST_ID` set). Each snippet carries its id
(`[n:<id>]`), and the ids injected are logged on the turn's capture node
(`props.recalled`), so the hit rate is measured rather than guessed.

So: effectively free, **not literally zero**.
Say "no model call" when you mean it. Re-measure with the recipe in
[`docs/performance.md`](docs/performance.md).

**Capture noise filter.** `UserPromptSubmit` runs every prompt through
`scripts/noise.py` BEFORE any write — regex only, no model. Dropped: bare acks
("continue", "ok", "yes", "go"), a slash command with no real arguments, a pasted
`<system-reminder>` / task-notification / JSON blob, an image-only paste
(`[Image #n]`), and a prompt under `capture_min_chars` (default 40) with no verb.
Always kept: an error string, a decision word (decided / always / never / from now
on), a URL, a file path, a config/git change, a ticket key, and any prompt
`detect_tasks` would turn into a to-do. Nothing is lost silently: the count per
session is on the conversation node (`props.noise_dropped`, per rule in
`props.noise_reasons`). `capture_filter` setting (or
`MEMORY_LEDGER_CAPTURE_FILTER`): `drop` (default), `cold` (store the raw prompt in
the `cold` realm, counted as `props.noise_cold`) or `off` (the old behaviour).


- **Recall first.** Before answering, `match-skills` / `search` the graph (or the
  free `brain_query.py` views) so you build on prior context instead of starting
  cold.
- **The raw turn is already captured — for free.** The hooks stored the prompt,
  a summary of the answer, and any obvious to-dos in Python/SQL. Do **not** spend
  tokens re-storing the turn.
- **Enrich what the heuristics can't infer.** Add the *semantic* nodes on top: a
  choice → a `decision` (`part-of hub_process`); a new person/project/ticket/
  channel → its node; a durable, reference-worthy answer → a `note`. `search`
  first to dedupe, then link (`about` / `relates-to` / `assigned-to` / `part-of`).
- **Skip only pure chit-chat.** Everything substantive becomes recallable memory.

## 1. How to respond — the focus contract (non-negotiable)

These four goals shaped this whole plugin. They override generic helpfulness.

1. **Externalize memory.** Capture *everything* worth keeping the moment it
   appears — people, tasks, decisions, preferences, context. The person should
   never have to hold it in their head. When in doubt, `remember` it.
2. **Micro-steps.** Never hand back a big task. Break it into the smallest
   possible *next physical action* ("open the doc", not "write the report").
   Starting is the hard part — lower the activation energy.
3. **Time-awareness support.** Attach due dates and time estimates to anything
   time-bound. Surface what's due/overdue proactively. Prefer real alarms
   (scheduled routines) over "remember to...".
4. **Reduce overwhelm.** Reply with **one clear next action**, not a wall of
   text and not a dumped list. Calm, direct, warm, concrete. If there are many
   things, say how many exist, then show the single most important one.

Tone: short sentences. No hype, no filler, no "Great question!". Bold the one
thing that matters. If you must show a list, cap it at 3 and mark the top pick.

**Language.** Detect the language of the current conversation/context and reply
in that same language (English or the person's own). Store memory in the
language it was given — don't translate their words when saving. Titles stay
searchable regardless of language (search is character-based).

When **writing to someone else** (a Slack DM/message, an email): use **that
recipient's language**, not English by default — a Slack message may absolutely
be in the language the person speaks. Read it from their `person` node's
`props.lang` (e.g. `pt-BR` for a colleague who writes in Portuguese, `en`). If a
recipient's language can't be inferred from context or their node, **ask the
user which language to use and store it** on that person
(`update-node --id <person> --props '{"lang":"..."}'`). Tone is always **direct
and friendly**.

---

## 2. The memory graph API

Everything is a **node** (a thing) connected by **edges** (a relationship).
Storage is one SQLite file. Never write SQL yourself — always go through:

```
python3 "$CLAUDE_PLUGIN_ROOT/scripts/graph.py" <command> [flags]
```

**Paths in commands an agent runs.** Where code builds such a command with an absolute
path (the Agents prompt, the Gemini/Codex brain block, trigger and B2B prompts), write the
path with `_platform.shell_path()`, never the raw `Path`: on Windows a `\` path is mangled
by Git Bash, the command fails and is retried (tokens), and the allowlist stops matching.
It writes `/` on Windows, quotes a path with a space, and leaves macOS/Linux as is.
`$CLAUDE_PLUGIN_ROOT` above is expanded by Claude Code itself; whether it uses `/` on
Windows is unverified.

**The brain is global and lives OUTSIDE any repo.** Its home is
`~/.memory-ledger` (override with `$MEMORY_LEDGER_HOME`, or the legacy
`$SECOND_BRAIN_HOME`), so every project on the machine shares one brain. It is
never committed.

**The canonical name is a door, not a move.** The home used to be
`~/.second_brain`, and on every machine that predates the rename that is still
where the data physically is. `adopt_canonical_home()` in `scripts/_switch.py`
symlinks `~/.memory-ledger` at it rather than renaming the directory, because a
move would strand every absolute path a user has already baked into a cron, a
backup job or a `$SECOND_BRAIN_DB`. The link is **relative**, so it survives the
home directory itself moving. Resolution accepts `.memory-ledger`,
`.memory_ledger`, `.second_brain` and `.second-brain`, newest first — a near-miss
should find the brain that exists rather than quietly start an empty second one.
Adoption runs once per session from `hooks/bootstrap.py` and from `graph.py init`;
`home_dir()` itself never writes, because a resolver called from every read path
must not touch the disk. If two of those names are **real directories**, that is
two brains and only a human can say which is the brain: the canonical one is used
and `doctor` reports the other, never merging or deleting either. The home holds:

```
~/.second_brain/
  graph.db            the memory graph (single portable file) — skills live here too, as nodes
  backups/            point-in-time snapshots taken before consolidation, kept 5 days
  learned_skills/     OPTIONAL local SKILL.md exports (only if add-skill --with-file); not the source of truth
  exports/            export-memory output
  neo4j.json          optional graph-mirror config (secret — never commit)
  settings.json       preferences the CLI, hooks, cron and the web UI all read
  ui_prefs.json       the browser's own view state, only when `ui_sync` is on
  (routines, definitions live here too)
```

Path resolution: `$SECOND_BRAIN_DB` (explicit file) → `$SECOND_BRAIN_HOME/graph.db`
→ `~/.second_brain/graph.db`. Copying `graph.db` moves the whole brain; copying
the home dir moves brain + learned skills. This works identically in Claude Code
and the Claude app.
Every command prints JSON to stdout, and **wraps its result in an envelope** —
`{"ok": true, ...}` on success, `{"ok": false, "error": "..."}` on failure. The
payload key varies by command: a write returns the whole node under **`node`**
(so a created id is `.node.id`, **not** `.id`), reads return **`results`** +
`count`, `del-node` returns `deleted`. Bad flags are an argparse error on
**stderr** with a non-zero exit — so parse stdout as JSON *and* let stderr
through, or a typo'd flag looks like an empty result:

```bash
ID=$(python3 scripts/graph.py add-node --type task --title "..." | python3 -c \
  'import sys,json; print(json.load(sys.stdin)["node"]["id"])')
```

Commands:

| Command | Purpose |
|---|---|
| `init` | Create the DB (idempotent). Bootstrap (`hooks/bootstrap.py`) already calls this on every `SessionStart`, so a manual `init` is no longer a required install step — it exists for scripting/tests. |
| `disable` / `enable` | Turn capture off/on machine-wide by writing/removing `<home>/.disabled` (`/ledger off` / `/ledger on`). This is the only durable off switch — deleting the home dir is not (see §2d). |
| `add-node --type T --title "..." [--body ...] [--props JSON] [--status S] [--due ISO] [--id ID] [--realm R] [--area A]` | Create a node. `--area` files it under a domain area in the same call (§2e) and **errors instead of creating an orphan** if the area does not exist. Returns the full node under `node` — read the new id from **`.node.id`**. There is **no `add-task` subcommand**: a task is `add-node --type task --status open` (the `add-task` *skill* wraps that and adds micro-steps; the web UI's is `POST /api/add-task`). |
| `update-node --id ID [--title][--body][--props JSON][--status][--due]` | Update fields. `--props` MERGES into existing props. |
| `get-node --id ID` | One node **plus all its edges** (neighbors named). |
| `del-node --id ID` | Delete a node (its edges cascade). |
| `add-edge --src ID --dst ID --rel REL [--props JSON]` | Link two nodes (idempotent). |
| `del-edge --src ID --dst ID --rel REL` | Remove a link. |
| `search "text" [--type T] [--limit N] [--budget CHARS] [--full]` | Full-text search over title+body (prefix-matched). **Budgeted by default** — bodies are trimmed to share `--budget` (2,600) and only decision-relevant props are returned; uncapped it was ~9k chars where `recall` answered the same cue in ~1.5k. `--full` restores whole bodies; `get-node --id` reads one completely. |
| `suggest-links "text" [--limit N] [--exclude ID]` | **Link recognition** — which known nodes this text mentions, and the relation its wording implies (`blocked by X` → `blocked-by`). Pure regex + a node scan: no model, no tokens. Powers the web UI's live suggestions as you type. |
| `recall "cue" [--limit N] [--depth 2] [--budget CHARS]` | **Associative recall — prefer this over `search`.** Seeds on full-text, spreads activation across edges, modulates by memory strength, returns a SLIM ranked payload inside a char budget. One call replaces `search` + `neighbors` and costs ~40% fewer chars. |
| `consolidate [--day D] [--older-than N]` | Roll a day's `capture` nodes into one digest note. **Non-destructive** — every capture is kept and linked; the digest is what recall surfaces. |
| `list [--type T] [--status S] [--area A] [--due-before ISO] [--all] [--limit N]` | Filtered list. **Hides the done/cancelled archive by default** — pass `--all` or `--status done` to see it. `--area` keeps only what is `part-of` that domain area (§2e). |
| `areas` | Every domain area with its member count (§2e). Token-free. |
| `add-area --title T [--id ID] [--domain KEY] [--body ...] [--realm R]` | Create a domain area. Idempotent on `--id`; default id is `hub_<slug of title>`. |
| `match-rules "situation" [--limit N] [--budget CHARS] [--full]` | **Which conventions apply to THIS situation** (§2i) — the token-light replacement for reading this whole file (~8% of it). No model call. Always-apply rules are appended, not counted against `--limit`. |
| `ingest-conventions [--file F] [--realm R]` | Load this file into the brain as `rule` nodes, one per section (§2i). Idempotent and **never overwrites a user-authored or user-edited rule**. |
| `audit-contracts [--kind K] [--source S]... [--count-only]` | Check the contracts that live in the **DB**, which the file checker cannot see: learned skills (§5.1) and user/learned rules (§5b.1). Reports gaps, **never rewrites** — the missing clause is a judgment call, and filling it mechanically produces exactly the unfalsifiable sentence §5b exists to prevent. |
| `snapshot-rules [--label L] [--source S]...` | Copy every rule **verbatim** into the `rules-backup` realm before a bulk edit (§5b.3). Stores `rule-snapshot` nodes, never `rule`, so `match-rules` cannot return one; captures body, title and props, because a rule without its `scenario` could not be restored to firing. Idempotent by content: an unchanged rule stores nothing, a changed one stores a new generation, nothing is overwritten. |
| `add-rule --title T [--body ...] [--scenario CUES] [--section S] [--source user\|learned\|plugin] [--overrides ID] [--non-negotiable] [--id ID] [--realm R]` | Author or override a rule — the mutable half of the genome (§2i). `--overrides` marks the shipped rule `superseded`. **`--scenario` is indexed** — put the words a person would actually type there, including ones the body never uses; that is what `match-rules` fires on. Same for a skill's `triggers`/`description`. The §5b.1 sections also have flags of their own (`--the-rule`, `--why`, `--how-to-apply`, `--exceptions`, `--judged-against`, `--related`) so a rule can be authored structured rather than as one `--body` whose headings you have to remember; `--strict` refuses an incomplete one instead of leaving it for `audit-contracts`. |
| `learned-rules [on\|off\|status]` | The learned-rules switch (§2i) — stored as the `learned_rules` setting, **OFF by default**, per person. `status` shows it, where the answer came from, and the proposal counts. `$MEMORY_LEDGER_NO_LEARNED_RULES` forces it off. |
| `propose-rules [--dry-run] [--days N] [--min-count N] [--min-sessions N]` | Run the learned-rules detector (§2i): spot corrections the person keeps repeating in captured prompts ("no, …", "don't …", "never …", "always …", "stop …", "I said …", "instead …", and "não", "nunca", "sempre", "pare de") and **propose** a rule when one recurs >= N times (default 2) across >= 2 sessions. Token-free, deterministic, conservative. **Does nothing while the switch is off** and says how to turn it on. Never creates a `rule`. |
| `rule-proposals [--status proposed\|accepted\|rejected\|all] [--budget CHARS]` | List proposals with counts and evidence snippets, budgeted. Readable while the switch is off. |
| `accept-rule --id X [--title T] [--text T]` | Turn a proposal into an active `rule`: `source: learned`, precedence 50, linked `learned-from` to the proposal and the prompts that taught it. The only road from a proposal to a rule. |
| `reject-rule --id X [--reason R]` | Mark a proposal `rejected`. It is **kept**, and the cluster is not proposed again, even reworded. |
| `park [--body TEXT] [--title T] [--about ID] [--area A]` | **Move context OUT of the window** (§2j): stores the blob (stdin or `--body`) and returns a tiny receipt, so the model can drop the content. |
| `unpark --id ID [--offset N] [--chars N]` | Read parked context back, whole or sliced. |
| `resume [--budget CHARS] [--days N] [--limit N]` | **Rehydrate after `/clear`** (§2j) — the thread that was in play, what's parked, what's open, inside a budget. No model call. |
| `decay [--apply] [--stale-days N] [--no-backup]` | Per-type decay tiers (§2h, `scripts/decay.py`). **Dry-run unless `--apply`.** Reports captures past the capture half-life (7 d), the open tasks the capture hook created (`props.source=auto-capture`) older than N days (default 14) whose title is really a pasted prompt, and how many stored prompts today's noise filter would refuse. `--apply` folds expired captures into one `note` per ISO week (`note_week_<YYYY-Www>`, compressed bullets; originals kept, linked `part-of`, stamped `expired_at`/`expired_into`) and closes the flagged tasks as `dropped` (archived, hidden from `list`). Backs up first; never deletes; idempotent. |
| `maintain [--keep-days N]` | Brain-side upkeep: consolidate old captures into per-day digests, repair reversed digest links, report the read reduction. Non-destructive, idempotent, schedulable. |
| `genome [--refresh] [--out DIR]` | The brain's own rebuild blueprint (§2i). `--refresh` re-derives the API surface + DB schema from the code; `--out` writes the full kit for an AI with no plugin. |
| `due [--within-days N]` | Tasks/reminders due within N days (flags `overdue`). |
| `neighbors --id ID [--rel REL] [--depth N]` | Walk the graph out to depth N. |
| `complete --id ID [--note ...]` | Mark a task **done** — archives it (kept forever, hidden from active views), stamps `completed_at`. Never delete finished work; complete it. |
| `done [--limit N]` | The done archive — everything completed. |
| `add-skill --name slug --title T [--when triggers] [--body steps] [--with-file] [--requires S] [--waits-for S] [--enriched-by S] [--reads T] [--writes T]` | **Learn a skill at runtime** — stores a `skill` node (triggers + full procedure in the DB, synced everywhere), stamped `kind: learned`. **Node-first**: a local `SKILL.md` is written only with `--with-file`. The five declaration flags are a user skill's contract (§5a): comma-separated skill names for the first three, node types for `--reads`/`--writes`. They are what puts it in the user tree with edges rather than as a leaf. |
| `sync-skill-tree [--realm R] [--no-node-types]` | Project the skill contracts into the graph as **two trees** (§5a): built-in skills under `hub_skills_builtin`, user skills under `hub_skills_user`. Built-in skills declare in their `SKILL.md` `## Depends on` block, user skills in props (`add-skill --requires/--reads/--writes`). Idempotent, and a **true sync**: generated edges that stopped being declared are pruned. Hand-made edges carry no marker and are never touched. |
| `ingest-skills [--dir D]... [--realm R] [--no-rules]` | Pull existing markdown skills (plugin `skills/` + `learned_skills/`) into the DB as `skill` nodes (idempotent; re-run to refresh). A `rules/` folder beside them is read **first**, so the skills landing next to it can link to the rules that travelled with them, and a rule this brain already holds is **kept**, never overwritten. Re-projects the tree when anything changed. |
| `export-skills [--dir D] [--kind learned\|plugin\|all] [--no-rules]` | The other direction (§5a): write skill nodes back out as `D/<name>/SKILL.md` plus `D/rules/<rule id>.md` for every rule they honor, so a brain can move between machines by file when there is no master to mirror through. A round trip is lossless — contract, declarations and `honors` edges all survive. |
| `retype --type T (--id ID... \| --ids-file F) [--dry-run]` | Change existing nodes' type, keeping id and edges; the old type goes to `props.migrated_from`, so running it again with that type reverses it. How decisions and notes that describe a standing way of working became `process` nodes. |
| `add-process --title T [--body B] [--trigger W] [--realm R] [--id ID]` | Record a way of working as a `process` node part-of `hub_process`, in the configured realm unless `--realm` says otherwise. `processes` lists every realm's. |
| `export-bundle [--skill ID]... [--rule ID]... [--process ID]... [--spec F] [--out Z]` | A **selection** as one zip (§5a): a directory per kind plus an `import-plan.json` naming the order it can be read back in. Rules a selected skill honors travel with it; a skill it *requires* that was not selected is **named in the report**, never pulled in silently. |
| `inspect-bundle --zip Z` | What is inside a bundle and which of it this brain already holds. Read-only, so it is safe to run on a zip you were sent. |
| `import-bundle --zip Z [--select ID:MODE]... [--spec F] [--mode M] [--process-realm R]` | Apply a bundle in its plan's order. Processes land in `--process-realm` (default: the configured realm), keeping the realm they left as `origin_realm`. An item already here is left alone unless you name it: `update` refreshes it, `replace` deletes it first, `ignore` skips it. An item you never mentioned is created when new and untouched when it exists. |
| `scripts` | List the person's local scripts in `<home>/scripts` (§5c): name, size, executable bit, language, and the first docstring or comment line. Files only — the brain runs one only when a trigger links it. |
| `create-script --name N [--file F] [--no-exec] [--replace]` | Write one script into `<home>/scripts`, from a file or stdin, executable unless `--no-exec`. An existing one is overwritten only with `--replace`, after its old bytes go to `<home>/backups/scripts-<stamp>/` (§5c). |
| `export-scripts [--name N]... [--out Z]` | Zip the named scripts (all, when none are named) into `<home>/bundles/scripts-<stamp>.zip`, each keeping its executable bit (§5c). |
| `inspect-scripts --zip Z` | What a scripts zip holds and which of it collides with a local file, and whether the bytes are identical. Read-only. |
| `import-scripts --zip Z [--select NAME:MODE]... [--all] [--spec F] [--mode keep\|replace]` | Land scripts in `<home>/scripts`. A new file is written; an existing one is overwritten **only** for `replace`, after its old bytes are copied to `<home>/backups/scripts-<stamp>/`. A name not selected is not imported. |
| `del-contracts [--id ID]... [--spec F] [--force]` | Delete skill and rule nodes, edges included. §2a's archive-never-delete is about **work**; a contract nobody should fire again has to actually stop firing. A built-in needs `--force`, since its file re-ingests it anyway. |
| `match-skills "situation" [--limit N]` | Find skills matching the situation — ranked by **relevance first**, with reinforcement (usage) as a bounded booster. Read the procedure from the node body (`get-node`). |
| `use-skill --id ID` | **Reinforce** a skill after it fires (usage_count↑, confidence↑, last_used stamped). |
| `conversation-start --session SID [--cwd D] [--owner O] [--title T]` | Get-or-create this session's conversation node (§2k). Idempotent — the id is derived from the session id. |
| `conversation-end --session SID [--reason R]` | Close it. The **main** agent is completed; any subagent still open is **abandoned**. |
| `agent-spawn --role R (--parent ID \| --session SID) [--model M] [--body ...]` | One node in the tree, under **exactly one** parent — a conversation or another agent. Errors rather than create an orphan. |
| `agent-finish --id ID --outcome completed\|abandoned\|failed [--note ...]` | Record how the branch ended. Every branch closes explicitly. |
| `tree [--session SID] [--id ID]` | The agent tree with its ideas — including what a subagent discarded. Token-free. |
| `idea-propose --title T [--detail ...] [--agent ID] [--session SID]` | Record an alternative. **Every** one, not just the chosen one (§2k). |
| `idea-select --id ID [--reason ...] [--supersedes ID] [--caused-by ID]` | Mark it chosen; `--supersedes` moves the one it replaces to `superseded`. |
| `idea-discard --id ID --type conditional\|preference\|permanent --reason R [--blocked-by 'gauge.key >= value']... [--recheck-on EVENT]... [--caused-by ID]` | **The critical one.** A `conditional` discard **without** `--blocked-by` is **rejected** — it would be a permanent discard in disguise. Conditions are a conjunction; every gauge must already be in the catalog. |
| `idea-revive --id ID [--reason ...]` | Bring one back by hand (a `preference` discard, or a judgement call). |
| `ideas [--status S] [--agent ID] [--limit N]` | Every alternative with its status and why it was dropped. Token-free. |
| `idea-history --id ID` | The full transition log for one idea, in order. |
| `gauge-set --key K [--value V] [--unit U] [--probe CMD] [--description D] [--apply] [--no-recheck]` | Upsert one measurable thing (§2k). **A value change triggers a recheck** of everything waiting on it; `--apply` also revives what unblocked. |
| `gauges [--full]` | The catalog, with what each gauge is blocking. Warns about a blocking gauge with **no probe**. |
| `gauge-probe [--key K] [--apply]` | Run the stored probes, record what they measured, recheck what moved. |
| `recheck [--key K] [--apply] [--force] [--quiet-days N] [--rebuild]` | **What became possible again?** One indexed pass over `blocked_item`, no model call. It **notifies**; `--apply` also marks the ideas `revived`. `--rebuild` re-derives the index from the transition log. |
| `context-event --agent ID --op added\|removed\|summarized --item-type T --item-ref R [--tokens N]` | Record a **change** to the context window, not a snapshot (§2k). |
| `context-at --agent ID [--at ISO]` | Replay the events and return the window as it stood. |
| `stats` | Counts by type. |
| `export --format json\|md\|both [--out DIR]` | Dump the whole graph to files. |

**Workflow rule:** before creating a node, `search` first to avoid duplicates.
If a close match exists, update/link it instead of making a new one.

## 2a. Finishing work — archive, never delete

When a task is done, **`complete` it** (don't `del-node`). It moves to the done
archive: kept forever, out of active views (`list`, `due`), still searchable and
still linked. This preserves history and lets the brain learn from what worked.

## 2b. Learned skills — neuroplasticity

The brain grows. When you notice a **repeatable procedure** — something the user
did, taught, or asked for that will recur — capture it as a learned skill so the
brain can reuse it later, in any project:

1. **Learn:** `add-skill` stores a `skill` **node in the DB** (triggers + full
   procedure in the body, synced everywhere) — no file unless you pass
   `--with-file`. Skills are nodes, not isolated markdown. Existing markdown
   skills are pulled in with `ingest-skills`. The built-in ones need no step:
   the database `connect()` creates starts with them, and `init` and `doctor`
   ingest any that are missing (`SECOND_BRAIN_NO_SKILL_SEED=1` turns that off).
2. **Recall:** at the start of handling a request, run `match-skills "<the
   user's situation>"` (ranked by relevance, reinforcement as a booster). If a
   skill matches, **read its procedure from the node body** (`get-node --id`) and
   follow it instead of improvising.
3. **Reinforce:** after a learned skill fires successfully, `use-skill --id` to
   strengthen it — frequently-useful skills edge up among equally-relevant matches.
4. **Connect:** link skills into the graph — `applies-to` a task/project type,
   `learned-from` the task/note that taught it, `relates-to` sibling skills — so
   learnings transfer across situations, not just the one they came from.

This is the plastic layer: the brain forms new pathways from experience and
strengthens the ones that keep paying off. Consult it before falling back to
generic behaviour.

---

## 2c. State awareness & open loops

The brain is a **state machine**: every trackable thing **holds its state until
something changes it** — nothing silently resolves. An **open loop** is anything
still awaiting action, and it stays open until closed.

- **What's a loop:** open `task`/`subtask` (status not done/cancelled), tracked
  `pr` / `email` / `message` / `reminder`, or any node carrying a non-closed
  `props.state`. Closed states: `done, cancelled, closed, answered, resolved,
  merged, shipped, deployed, declined`.
- **Move state explicitly** with `set-state --id --state <s> [--waiting-on who]`
  — it stamps `state_since` (when it changed), so "age" is honest and a loop
  that hasn't moved shows as stale. Finish tasks with `complete` (archives).
- **See it all:** `open-loops [--stale-days N]` — oldest/overdue first, grouped
  by kind + priority, with `age_days`, `overdue`, `stale`, `waiting_on`.
- **Proactively close.** The `follow-up` skill drives loops toward closed; the
  SessionStart hook surfaces them each session. Always nudge toward closing the
  oldest/highest-priority first, and flag anything the person is blocking others on.
- **Track new loops** as they appear (a PR to move, an email/message to answer)
  as `pr`/`email`/`message` nodes with a `props.state`, so they can't slip.
- **An external task board may own a task.** Another app (a kanban, a desk, a
  team tracker) claims a task with `track --id <task> --system <name> --ref <its
  id> [--url]`, which sets `props.tracked_by = {system, ref, url, since}`. From
  then on that board decides open or closed and reports back with `complete`
  or `update-node --status cancelled`; a due date it sets comes back with
  `update-node --due`. The brain keeps capturing and remembering the task but
  stops nagging: `open-loops` leaves tracked tasks out of its results and
  counts them as `tracked: {<name>: n}`, and the SessionStart brief says
  "Tracked elsewhere: n in <name>" in one line instead of listing them. The
  date views (`brain_query.py`) keep them, so a date the board wrote back still
  shows on the agenda. The board pulls what is still unclaimed with
  `list --type task --status open --untracked` and lists what it holds with
  `--tracked-by <name>`; `untrack --id` releases a task. This is the whole
  contract: no board is named here, and any app that speaks these commands
  (`track`, `untrack`, `complete`, `update-node`, `list`) is a board. `open-loops`
  `count`, `by_kind` and `by_priority` leave tracked loops out unless
  `--include-tracked`; the `tracked` map counts them per board.

## 2d. Storage: one local file, an optional mirror, and sections

**The brain is one local SQLite file, and that is the whole product.** It is
durable, offline, fast to read, and portable — copy `graph.db` and the brain moves
with it. Nothing about the brain requires a network.

**Enabled means "not explicitly turned off."** `hooks/bootstrap.py` runs on every
`SessionStart` and auto-creates the home dir + DB if they don't exist, so there is
no manual init step and — just as importantly — deleting the home dir is **not**
an off switch (bootstrap just recreates it next session). The brain is OFF only
when `$MEMORY_LEDGER_OFF`/`$SECOND_BRAIN_OFF` is truthy for the current process, or
`<home>/.disabled` exists; `disabled()` / `enabled()` are the single source of
truth for this check — read them, never re-implement them. They live in
**`scripts/_switch.py`**, a module that imports nothing heavier than `pathlib`,
and `graph.py` re-exports them (along with `_env` and `home_dir`) so
`graph.disabled()` still resolves for every caller that already reads it through
the engine. The split is deliberate: a SessionStart hook whose first act may be
to do nothing should not import a 5k-line engine to find that out — ~12 ms
against ~55 ms — and the user who turned capture **off** is the last person who
should pay for the machinery they switched off. Toggle the marker with `graph.py disable` / `graph.py enable`
(`/ledger off` / `/ledger on`). `scripts/doctor.py` (`/ledger doctor`) reports
whether the brain is currently enabled, where the DB lives, and whether anything
looks mis-wired — reach for it before assuming a broken install.

**The other switches resolve the same way, in the same module.** `autosync`
(push each write to the mirror) and `consolidate` (let the daily hook fold closed
days into digests) are `_switch.flag()`, read on the same cheap path for the same
reason — a SessionStart hook asking "should I consolidate?" must not import the
engine to be told no. Each is spelled POSITIVELY in `settings.json` and
overridden by a NEGATIVE env var (`$MEMORY_LEDGER_NO_AUTOSYNC`,
`$MEMORY_LEDGER_NO_CONSOLIDATE`), because that is the spelling every existing rc
file and cron already uses; a truthy one wins over the file exactly as
`$MEMORY_LEDGER_OFF` wins over the marker. `flag_source()` says which layer
answered, so the Settings page can grey out a box it cannot change and say why.

**Consolidation is undoable by snapshot, not by unpicking.** It is
non-destructive (§2a) — every capture is kept and linked to its digest — which
makes it safe but not reversible: undoing a run means removing a props key from
every capture it touched. So a run triggered by a person snapshots the whole
graph to `<home>/backups/` first (sqlite's backup API, never a file copy of a
live database), keeps it `BACKUP_KEEP_DAYS` days, and you restore by copying one
back over `graph.db`. Expiring a snapshot is **not** the delete §2a forbids: that
rule is about the graph's own content, this is retention on a copy whose purpose
is to be temporary. The daily hook passes `--no-backup` — a snapshot every
morning for a run nobody asked for is just disk. `graph.py backups [--new]
[--prune]` reaches all of it from the CLI, because a recovery path that exists
only in a browser is not a recovery path.

**Optional graph mirror.** If a `graph` backend is configured, every mutation also
pushes there, so several machines can share one brain. Configure it with
`~/.second_brain/neo4j.json` or the `SECOND_BRAIN_NEO4J_*` env vars — any
reachable graph server works; the plugin ships no default and connects to nothing
unless you configure it. When the mirror is unreachable the change is queued in an
`outbox` and **drains automatically** on the next successful write or an explicit
`flush`, so losing the mirror never blocks or loses a write.
- `neo4j-status` — configured / reachable / pending-ops · `flush` — drain the queue.
- `$SECOND_BRAIN_NO_AUTOSYNC=1` queues without per-op push (for heavy batch loads;
  `flush` afterwards) · `scripts/brain_sync.py` does a full bulk reconcile,
  `--verify` proves the master holds everything, `--pull` restores from it.
- The mirror is COMPLETE: SQLite triggers queue every change to `nodes`, `edges` and
  `graph.MIRROR_TABLES` into the outbox, whatever code path made it. A new table a
  restore would need goes in `MIRROR_TABLES`; a derived one stays out.
- Sync is two-way: `graph.py sync` pulls every master change stamped after this
  file's `pull_seq` by another writer, then pushes. It is the road to retiring
  SQLite (docs/neo4j.md#the-road-off-sqlite), so two rules hold now:
  **every write to neo4j is stamped** (`_seq` from `:SyncClock`, `_origin`, deletes
  as `:Tombstone` — reuse `graph._NODE_UPSERT` and friends), or `sync` never sees
  it; and **pulled data is written with the triggers muted** (`mirror_mute`), or it
  echoes straight back up.
- The visualizer (`scripts/serve.py`) reads the mirror when one is reachable, else the
  local file.

**A local graph server in one command.** `scripts/neo4j_local.py up` (slash command
`/neo4j-up`) runs neo4j in Docker with `--restart unless-stopped`, the store
bind-mounted at `<home>/neo4j/{data,logs}`, bound to `127.0.0.1`, a generated
password written to `neo4j.json` (chmod 600, **never printed** — capture would store
it), the `backend` setting flipped, and the existing brain backfilled. Idempotent;
`status` / `down` / `down --remove` alongside it. It **refuses to repoint an
already-configured master** without `--switch` (or `--keep-config`), because
silently moving where writes mirror is how two stores drift. Optional — SQLite is
the product (see [docs/neo4j.md](docs/neo4j.md) for the measured cost).

**The `backend` setting** decides where a read goes when nothing says otherwise:
`sqlite` (default), `auto` (mirror when the master is down), or `neo4j`. It lives in
`<home>/settings.json` so the CLI, hooks and cron share one answer; `$SECOND_BRAIN_BACKEND`
wins over it, and an explicit `--backend` wins over both. `graph.py` always reads
the local mirror regardless — the mirror is written first on every mutation, so it
is never behind.

Node `type` is a property in the mirror, not a label, so changing a node's type
needs no label surgery — re-emitting the node through the API is enough.

**Sections (realms):** every node has a `realm` — the section it belongs to:
`default` out of the box, plus whatever you add (`work`, `personal`, `shared`, …).
Set with `add-node --realm <section>` or `$SECOND_BRAIN_REALM`; `sections` lists
the counts. Adding a realm is just writing a node into it; `rename-realm --from
<old> --to <new>` renames one (merging when `<new>` exists) and follows it in the
default realm setting.

## 2e. Areas — one hub per domain

**Nothing durable floats loose.** A quote, a health note, a way of working: each
belongs to a **domain area** so it can be *queried as a domain* rather than found
by guessing full-text. An area is a node of type `area`, content points at it with
`part-of`, and the id convention is `hub_<domain>`:

```
python3 scripts/graph.py areas                       # every domain + member count
python3 scripts/graph.py list --area quote           # everything in one domain
python3 scripts/graph.py add-node --type quote --area hub_quote --title "..."
python3 scripts/graph.py add-area --title "Finance — money, contracts" --domain finance
```

`--area` accepts the full id (`hub_quote`) **or the bare domain** (`quote`). It
resolves the area *before* creating the node, so a typo returns an error listing
the known areas rather than leaving a node outside every domain.

| Area | Domain | Realm | Holds |
|---|---|---|---|
| `hub_process` | process | default | The owner's ways of working — `process` nodes for standing methods and norms, `decision`s for one-off calls |
| `hub_quote` | quote | shared | Quotes the owner keeps, with `attributed_to` / `source_work` in props |
| `hub_health` | health | personal | Symptoms, episodes, energy, appointments — anything affecting capacity |

**Realm and area are orthogonal.** The realm is *which section of life* (work /
personal / shared); the area is *which domain of meaning*. A health note is realm
`personal`, area `health`. Add a new area the moment a second item of a domain
shows up — two loose notes of a kind is the signal, not a full taxonomy up front.

Areas are also where **cross-domain links earn their keep**: link a quote to the
`decision` it justifies, or a health episode to the day it cost, so recall can
explain *why*, not just list *what*.

## 2f. Token-free reads (`brain_query.py`)

Everyday "what's on my plate" reads don't need the model. `scripts/brain_query.py`
answers the common date views in pure SQL — **no model call** (§0) — as a text table
(read it directly) or `--json` (for the UI/phone backend):

```
python3 scripts/brain_query.py agenda      # overdue + today + tomorrow
python3 scripts/brain_query.py open        # ALL open tasks (any/no due) — pair with --assignee
python3 scripts/brain_query.py today | tomorrow | week | closed [--since-days N]
python3 scripts/brain_query.py range --from YYYY-MM-DD --to YYYY-MM-DD [--status open|done|any]
```
The date views (today/week/…) only show **dated** tasks; use **`open`** to see
undated ones (e.g. another person's tasks) — it's the right view for the assignee filter.

Prefer it (or `graph.py search` / `open-loops`) over asking the model to recall —
recall is free. `--backend sqlite|neo4j|auto` (with no flag: the `backend` setting,
§2d, default `sqlite`), local-tz buckets via `--tz`, `$SECOND_BRAIN_TZ`, or the `tz`
setting in `<home>/settings.json` (§2g) — in that order. The dashboard tab of
`scripts/serve.py` is this same layer over HTTP.

**`--mine` — only what the owner must act on.** Any view takes `--mine` (the
dashboard's "🙋 Mine · needs action" toggle → `?mine=1`) to keep ONLY the owner's
live action: **strictly `assignee == owner`** (`$SECOND_BRAIN_OWNER`, default
`Owner`), NOT `unassigned` and NOT someone else's, and never `delegated` or
parked `waiting`/`blocked`. Selecting the **`me`** assignee chip applies the same
strict actionable rule (not "every task tagged me").

**`--assignee me|unassigned|<name>[,…]`** — exact grouping (the dashboard renders
one toggle chip per distinct assignee, from `/api/assignees`). Distinct from
`--mine`: `assignee=me` matches only tasks *explicitly* the owner's, not
unassigned ones.

**`--kind <k>[,…]`** — scope by `props.kind` (free-form: `learned`, `plugin`,
whatever a note author used). Nodes carrying no kind land in an explicit `(none)`
bucket rather than being unfilterable. Open vocabulary like realms — read it from
`/api/kinds` or `brain_query.kinds()`, never hardcode it.

**`--realm <r>[,…]`** — scope any view to one or more sections of life
(`?realm=work,shared` on the dashboard). Omit it for every realm. A node with
no realm counts as `$SECOND_BRAIN_REALM` (default `default`), the same default the
engine writes with, so filtering never silently drops untagged nodes. **Realms are
open vocabulary** — never hardcode the list. Read it from `/api/realms` (which also
returns the server default) or `brain_query.realms()`; `graph.py sections` is the
CLI equivalent. Same for node types: `/api/meta` returns `realms` and `types` from
the brain precisely so nothing downstream ships a fixed list.

**Relation flags + expand.** Each row carries `blocked_by` / `blocks` / `related`
(derived from `blocked-by`/`depends-on`/`blocks`/`relates-to` edges) so the
dashboard flags 🔒 blocked / ⛓ blocks / 🔗 related without the model. `/api/node?id=`
returns a node's full body + named neighbors for the click-to-expand card. All
token-free.

## 2g. The web UI is a full write surface — the brain works without the model

**The model helps feed and manage the brain; it is never the only way in.** The
dashboard is a complete CRUD client: everything below is plain SQL through the
engine's `api_*` helpers, with **no model call and no tokens** — the same
guarantee as the read layer.

| Do this in the browser | Endpoint | Notes |
|---|---|---|
| Create a task | `POST /api/add-task` | `{title, body, due, assignee, priority, type, links[]}`. Type limited to `task`/`subtask`; created `open` with `props.source="web-ui"`. |
| See what a title connects to | `GET /api/suggest-links?text=` | Live as you type — see below. |
| Mark done | `POST /api/complete` | Archives (§2a): `status=done` + `completed_at`. Never deletes. |
| Move state | `POST /api/set-status` | Only `open,doing,blocked,waiting,done,cancelled`; stamps `state_since` like `set-state`. |
| Reassign | `POST /api/set-assignee` | `""` un-assigns. |
| Rename / edit notes | `POST /api/update-node` | `title` (non-empty), `body`, `priority`. |
| Set a due date | `POST /api/set-due` | The date chip ("📅 not set" when empty) opens a picker. |
| Create a relation | `POST /api/link` | Search for the other end, pick the relation, done. |
| Remove a relation | `POST /api/unlink` | The `×` on any row under **Related**. |
| Record an idea | `POST /api/propose-idea` | `{title, detail, realm}` — the map's **+ idea**. Same write as `idea-propose` (§2k): `idea` node, `proposed`, first transition logged. |
| Read the vocabulary | `GET /api/meta` | Relations + statuses + `writable`; the UI never hardcodes them. |
| Read the API itself | `GET /api/docs` | Swagger UI over `GET /api/openapi.json`, generated from the routes by the running process so it cannot describe a different server. Linked from the drawer, above Settings: the row opens it framed inside the app (`#/api-docs`), so reading the API never leaves the brain, and the ↗ arrow beside it opens the server's page in its own tab. |
| Find a node to link | `GET /api/search?q=` | Title substring, for the relation picker. |
| Change a preference | `POST /api/settings` | `tz`, `realm`, `owner`, `backend`, `autosync`, `consolidate`, `ui_sync`, `map_poll_seconds`, `dash_refresh_seconds` → `<home>/settings.json`. |
| Stop or resume capture | `POST /api/capture` | `{enabled}` — writes the same `.disabled` marker `graph.py disable` writes. Refused while `$MEMORY_LEDGER_OFF` is set, because that outranks it. |
| See everyone, and who is who | `GET /api/people` / `/api/person-duplicates` | Person nodes, and the groups that look like one human. Name matching over every alias — **no model call**. |
| Merge two spellings of a person | `POST /api/merge-person` | `{canonical, duplicate}`. Edges move, the other name becomes an alias, the duplicate stays as a resolver (§2a). |
| Pin another spelling | `POST /api/add-alias` | `{id, alias}`. A name they already answer to is a no-op. |
| Check the install | `GET /api/doctor` | The same report `doctor` prints. `ok` is "the call worked"; `healthy` is the verdict. |
| Preview consolidation | `GET /api/consolidate-preview` | What a run **would** change, computed by the same function the run uses. Writes nothing. |
| Snapshot / list snapshots | `POST /api/backup` / `GET /api/backups` | Point-in-time copies of the graph, kept 5 days. |
| Rename a realm | `POST /api/rename-realm` | Moves every node in one realm to another, merging if it exists, and follows it in the default realm setting. Asks first; snapshots before the first write. |
| Remember a view preference | `GET`/`POST /api/ui-prefs` | The browser's own state, stored here only when `ui_sync` is on — see below. |
| Point at the master | `POST /api/neo4j-config` / `/api/neo4j-test` | Writes `<home>/neo4j.json` (mode 600) and probes it. Empty url = sqlite only, and resets `backend` to `sqlite`. The **default read backend** select (§2d) sits in this card. |
| Drain the queue | `POST /api/flush` | Pushes the outbox to neo4j. |
| Prune | `POST /api/prune` | Runs `maintain` — consolidates old captures, deletes nothing (§2a). **Refused without `confirm: true`**: it still rewrites props on every capture it touches, so the page previews first. Snapshots the graph before the first write. |

**Link recognition — the brain guesses the relation, you confirm it.** As you
type a task title the composer calls `suggest-links`, which scans the text for
mentions of nodes you already have (whole titles, ticket keys like `ABC-4521`,
PR numbers like `#7366`, `#channel` handles, a person's name) and reads the
wording around each mention to pick the relation: *"blocked by PR #7366"* →
`blocked-by`, *"caused by …"* → `caused-by`, *"needs X first"* → `blocked-by`,
*"part of …"* → `part-of`, *"with <person>"* → `mentions`, nothing recognisable →
`relates-to`. Each suggestion renders as a row you can **re-point to any relation
or uncheck**, and your override survives further typing. It is a suggestion
engine, not an oracle — the user always has the last word.

**The UI never writes a value you did not change.** The card's assignee input,
status select and due chip all compare against the value they rendered and skip
the `POST` when it is unchanged — a change handler on a pre-filled field can fire
on a bare focus+blur, and a no-op write would churn `updated` and the mirror sync
for nothing.

**A client hanging up is not an error.** The map re-reads `/api/graph` every
couple of seconds, so a reload or a closed tab aborts a response mid-write and
Python raises from the socket. The server treats that as normal: the handler
returns silently, and the error path never tries to report a 500 down the socket
that just closed, which is what turned one aborted poll into two stacked
tracebacks in the log.

**Writes are unauthenticated.** They are bound to `127.0.0.1` by default. If you
expose the UI (`--host 0.0.0.0` for phone access), pass **`--no-write`**: every
`POST` then returns 403 and the UI hides its editing controls (`/api/meta`
reports `writable: false`).

**Five sections, behind one hamburger: Map, Dashboard, People, Capabilities, Settings.** The UI
is a React single-page app (`frontend/`, built to `frontend/dist`, which is not
in git: `scripts/serve.py` builds it on first start and rebuilds it when your
source is newer, so the UI needs npm and the API does not — see
[the frontend](docs/frontend.md)). The
sections live in a left drawer (`m` toggles it, Settings pinned at the bottom) so
nothing sits on top of the search bar, each section owns the whole viewport, and
**the section is in the URL** (`/map`, `/dashboard`, `/people`, `/capabilities`,
`/settings`; the old `/skills` redirects to `/capabilities`), so
a reload lands where you were and a link to a section is a link to that section. The map's
facet filters — node types, realms, kinds, and the display toggles — are one
dropdown each, multi-select, with an **all** / **none** that applies to that scope
only; the button says what the closed menu is doing (`all`, `none`, `4/9`). Nothing
in there is hardcoded: the options and their counts come from `/api/graph` meta, so
a realm or kind invented today is a filter on the next poll.

**A filter decides which nodes fall out; Display decides what that looks like.**
The two are separate questions and the UI keeps them separate: hiding answers "get
this out of my way" and the layout closes the gap, **shade instead of hide** answers
"show me just this" and leaves the rest dimmed in place. The set is identical either
way — every filter, the cascade included, obeys the choice without knowing about it,
so flipping it never changes *which* nodes are affected. Shaded nodes keep their dot
and their edges and lose their label, because a few thousand dimmed labels over the
handful you isolated is noise rather than context; a search hit keeps both, since
dimming what you just asked for by name reads as "no results".

**Settings are server-side, in `<home>/settings.json`.** `tz`, `realm`, `owner`,
`backend`, the three switches, `map_poll_seconds`, `dash_refresh_seconds` — read
by the CLI, the hooks and any cron over the same brain, because a preference only
one browser knows is not a preference. Precedence is the engine's usual **flag >
env > file > default**, and `GET /api/settings` reports which keys an env var has
taken over so the page can say the save will not take effect.

**A setting changes what the ENGINE computes; a view preference does not.** That
is the line the Settings page is grouped by. `owner` decides what `--mine`
returns, so it has to be server-side or the CLI and the page disagree about whose
work it is. Which dashboard view you had open decides nothing, so it lives in the
browser's `localStorage` — instant, and never a disk write on the brain for a
value no other reader cares about. Mirroring those into `<home>/ui_prefs.json` is
**opt-in** (`ui_sync`) and covers the one case `localStorage` cannot: the same
person on a second machine. Local is authoritative while you work and the push is
debounced, so an unreachable brain costs you cross-machine sync and never your own
UI. The blob is opaque to the server on purpose — its shape belongs to the
frontend, so a new toggle needs no server change.

The Settings section also answers **"is it capturing?"** (the marker and the env
var, with the env var's precedence stated rather than hidden), runs **doctor** in
the browser, manages the store (db path, master reachability, queued writes,
credentials — the password is never sent to the browser), and runs
**consolidation**: preview first, confirm second, snapshot before the first write.

**The People section** is where person nodes stop decaying. Every person is a
card that expands to everything one hop away, grouped by relation and fetched
only on expand — a hundred people must not be a hundred reads. Duplicates are
detected by name matching across every alias each node answers to, **with no
model call**, and the canonical is the most connected node because merging into
it moves the fewest edges. That is a default, not a verdict: the radio on any
member re-points the merge, since only a human can say which spelling is the real
person.

**The Capabilities section** (`☰` › Capabilities) holds four kinds of thing, in
this order: **skills**, **rules**, **processes** and **scripts**. It was the Skills
section until it held more than skills. It draws the two skill trees §5a put in the
graph, built-in and user, nested by the `requires` edge. Opening a skill shows
its graph surface (which node types it reads and writes), its `waits-for` and
`enriched-by` links, and **the rules it answers to**, in three buckets:
*declared* (the skill names the rule itself), *matched* (`match-rules` returns it
for the skill's triggers, which is where a person's own rules show up against a
skill that never named them), and *always on* (non-negotiable, collapsed by
default so the handful that are specific to this skill stay visible). Clicking a
rule reads its full body.

**The genome is listed beside the trees**, because a rule is only half-visible
when you can reach it only through a skill that happens to name it. One filter
narrows both at once:

- **Text** — matches a skill's name, description, triggers and the node types it
  touches, and a rule's title, id and `scenario`. `scenario` is the field
  `match-rules` actually fires on, so a rule whose body never says "slack" still
  answers a search for it, which is the whole reason that field exists.
- **Source** — built-in, user, or learned. The same axis splits both: a built-in
  rule comes from this file, a user or learned one from the person's brain.
- **Domain** — the area a skill or rule is filed under, labelled with the
  person's own area titles, read from the graph rather than a hardcoded list.

A skill matches on the rules it pulled *specifically*, never on the always-on
ones. Every skill carries those, so matching them would answer "which skills obey
the no-emoji rule" with the entire corpus: true, and useless.

Opening a skill or a rule also shows **its relations, one level deep** — every
edge in and out, named, read from `/api/node`, the same endpoint the dashboard's
expander uses. One level, never two: the question a card answers is "what else is
this wired to". "What is the graph" is what the map is for.

**Authoring is a write, reading an existing contract is not.** The `+` button
creates a new skill or a new rule; it does not edit one that exists. That split
is not squeamishness about the browser — it is about where the check lives. A new
contract written here goes through `build_skill_contract` / `build_rule_contract`
and is **refused when a required section is empty**, which is stricter than the
CLI, where `audit-contracts` reports the gap afterwards. An existing contract is
edited where its check already lives: a built-in in its `SKILL.md`, gated by the
pre-commit checker.

| Do this in the browser | Endpoint | Notes |
|---|---|---|
| Write a rule | `POST /api/create-rule` | `{title, scenario, source, section, realm, non_negotiable, overrides, sections{}}`. Refused unless `THE RULE` and `JUDGED AGAINST` are filled (§5b.1). |
| Write a skill | `POST /api/create-skill` | `{name, title, description, realm, sections{}, requires, waits_for, enriched_by, reads, writes, rules[], new_rules[]}`. Refused unless the six typed sections of §5.1 are filled. |
| Export the learned tree | `POST /api/export-skills` | `graph.py export-skills` — SKILL.md per skill, plus every rule they honor. |
| Read markdown back in | `POST /api/ingest-skills` | `graph.py ingest-skills` — idempotent; a rule this brain already holds is kept. |
| Record a process | `POST /api/create-process` | `{title, body, trigger, realm, id}`. A `process` node part-of `hub_process`; `realm` defaults to the configured one. |
| Bundle a selection | `POST /api/export-bundle` | `{skills[], rules[], processes[]}` → one zip under `<home>/bundles`, plus a `download` URL. |
| Download a bundle | `GET /api/bundle?name=` | The zip, as an attachment. `name` is reduced to a bare filename, so it cannot point out of that directory. |
| Preview someone's bundle | `POST /api/upload-bundle` | `{filename, data}` (base64, ≤32MB). Stages it and reports every item and whether this brain already holds it. **Writes nothing.** |
| Import what was ticked | `POST /api/import-bundle` | `{token, select{id: update\|replace\|ignore}, process_realm}`. The staged zip is consumed, so a token works once. |
| List the local scripts | `GET /api/scripts` | `graph.py scripts` — every file in `<home>/scripts` (§5c). `GET /api/script?name=` reads one. |
| Zip scripts | `POST /api/export-scripts` | `{names[]}` (empty: all) → one zip under `<home>/bundles`, plus a `download` URL through `/api/bundle`. |
| Preview a scripts upload | `POST /api/upload-scripts` | `{filename, data}` — a scripts zip, or one loose script file staged as a zip of one. Reports each script with `exists` and `identical`. **Writes nothing.** |
| Import the ticked scripts | `POST /api/import-scripts` | `{token, select{name: replace\|keep}}`. A replaced file is backed up first; the token works once. |
| Delete what was ticked | `POST /api/delete-contracts` | `{ids[], force?}` → `graph.py del-contracts`. A built-in is refused unless `force`. |
| Read the contract shapes | `GET /api/meta` | `skill_sections`, `rule_sections`, `decl_labels`, `node_types`. The form is generated from these, never from headings typed into the page. |

**Choosing is done on cards, never in a `<select>`.** Every "which of these"
in the forms — the rules a skill honors, the rule a new one replaces, the
skills and node types it declares — opens a searchable picker whose rows carry
the name, what the thing is *for*, and the full text one click away. A native
select shows one line of text per option, which for a rule is its title and
nothing about when it fires or what it forbids; choosing among forty of those is
guessing. The picker is multi-select wherever the field takes more than one, and
`/api/skills` carries `node_types` so a type card can say how many of it this
brain holds and which skills already touch it — which is the actual question
`reads`/`writes` answer.

**Rules a skill needs but does not have yet are written in the same flow.** The
skill form's rule lines each take an existing rule, and `+ New rule…` opens a
rule form **on top** of the half-filled skill one. Nothing is written until the
skill is saved: the inline rules are created first, in one call, and a rule that
fails to write aborts the skill rather than leaving one accountable to a rule
that does not exist. Sending someone away to author a rule and losing their
half-typed skill is the failure this shape prevents.

The page reads the edges `sync-skill-tree` wrote, so a stale tree means the sync
has not been run, not that the page is wrong — except after an authoring write,
which projects the tree itself so a new skill is drawable immediately.

## 2h. Memory dynamics — how recall behaves like memory

Three properties, all pure SQL/regex (recall stays model-free):

**Strength + decay.** Every node carries `access_count` / `last_accessed` /
`strength` (0–1). **Reading reinforces** — `recall` and `match-skills` bump the
nodes they return. Strength decays with disuse on a ~21-day half-life, so a note
used weekly outranks one written once and never reopened. Nothing is ever deleted;
a faded memory just stops winning the ranking. A node accessed 3× reads 0.56 fresh,
0.28 after three untouched weeks, 0.00 after six months.

**Decay tiers.** Strength (above) is per-node use; the *tier* is per-kind age.
`scripts/decay.py` gives each kind a half-life — decision 90 d, discovery 60 d,
bugfix/task 14 d, change/capture 7 d, by `props.kind` first then node type; kinds
not listed (person, project, skill…) do not decay, and an open loop (task, pr,
reminder…) keeps full weight until it is closed. Override with the
`decay_half_lives` setting (JSON, e.g. `{"capture": 5}`). `recall` multiplies a
node's score by `0.5^(age/half-life)`, floored at 0.2, so a 60-day-old decision
still beats a 60-day-old bugfix. `graph.py decay` (dry-run by default) is the
housekeeping side: weekly digest notes for expired captures and a list of stale
raw-prompt tasks. Decay changes rank and what is surfaced, never existence.

**Spreading activation.** `recall` seeds on FTS, then spreads across edges to
`--depth` (default 2), summing convergent paths so independent evidence adds up.
Two dampers keep it honest:
- **fan-out normalisation** — a node divides activation among its neighbours
  instead of giving each a full share. Without it the owner's `person` node (~80
  edges) collects activation from every seed and re-broadcasts it, and a query for
  a ticket comes back as a list of people.
- **ubiquity penalty** — an *associated* hit is divided by `log2(1+degree)`. A node
  wired to everything answers every query and distinguishes none. Lexical matches
  are exempt; they earned their rank.
Cue words are stopword-filtered (English + pt-BR), since function words widen the
sweep for nothing.

**Daily consolidation runs itself.** The `SessionStart` hook fires
`consolidate --older-than 1` **once per day, detached** — closed days get folded
into digests while today's captures stay raw until the day ends. It lives in the
hook, not in a cloud routine, because the brain is a LOCAL file that a cloud
routine cannot reach; session start is the one moment we know we are on the machine
that owns the graph. Guarded by a `~/.second_brain/.last_consolidate` stamp, never
blocks the session, and is safe to interrupt (non-destructive, so the next session
just picks it up). Opt out with `$SECOND_BRAIN_NO_CONSOLIDATE`.

**Hebbian wiring.** Co-activated nodes get a weighted `relates-to` edge — but only
after the pair fires together **3 times** (tallied in the local `coactivation`
table, which never syncs). One co-occurrence is a coincidence, not a pattern: an
early version wired a shapefile ticket to the `export-memory` skill because both
matched "export". Pairs already joined by any relation are skipped, and
`skill`/`channel`/`person`/`capture` never auto-wire — those are hubs, or they get
linked by intent (§2b).

**Why this is also a token optimisation.** Better ranking means fewer results are
needed, and payloads carry a snippet instead of a whole body. Measured on the real
graph: `match-skills` **7,449 → 682 chars (−91%)** — and §3 mandates it on every
routed request — while a context lookup drops **41%**. About **2,000 tokens saved
per routed request**. `match-skills` returns cue-sized rows now; read the full
procedure with `get-node --id` for the one skill you actually pick (`--full`
restores the old payload).

## 2i. The genome — the brain carries its own Claude layer

**This document is the base layer, not the source of truth.** The plugin ships it;
the brain stores it, the user mutates it, and every read goes through the graph. So
a brain is self-describing: given only `graph.db`, any AI can rebuild the
interaction layer and reconnect to the storage.

Two areas hold it (§2e):

| Area | Node type | Holds |
|---|---|---|
| `hub_convention` | `rule` | the operating rules — this file, one node per section, plus the user's own |
| `hub_genome` | `spec` | the rebuild blueprint: storage, hooks, API surface, DB schema, sync, restore path |

**Read rules by scenario, never the whole file.** This is the point:

```
python3 scripts/graph.py match-rules "<what you are about to do>"
```

No model call. It returns the sections that match the cue, plus the always-apply
rules, trimmed to a `--budget` (default 2,600 chars). Measured on the real graph:
**~2.4k chars against 28.5k for the full document — about 8%**, and the rule you
actually need comes back first. `--full` returns whole bodies; `get-node --id` reads
one rule completely.

**Precedence — the user redefines the brain.** `user` (100) > `learned` (50) >
`plugin` (0). Precedence is a *bounded booster on relevance*, never a re-sort, so a
user rule edges past an equally-relevant default but a barely-relevant one cannot
outrank a real match. `add-rule --overrides <plugin rule id>` marks the shipped rule
`superseded` — kept as history (§2a), excluded from matching.

**Learned rules — proposed, never applied.** The `learned` tier is filled by a
detector, behind a switch that is **OFF by default** (`graph.py learned-rules
on|off|status`, the `learned_rules` setting, the Settings page;
`$MEMORY_LEDGER_NO_LEARNED_RULES` forces it off). `propose-rules` reads the
`capture` prompts of the last 30 days, finds corrections opening a clause
(English and Portuguese cues), clusters near-duplicates by Jaccard over content
words (opposite polarity never clusters) and, when a cluster recurs at least N
times (default 2) across at least 2 sessions, stores a **`rule-proposal`** node
(`status: proposed`) with its evidence: prompt ids, snippets, counts, sessions.
It is deliberately conservative: long prompts, questions, code and vague
one-liners are skipped. No model call. A proposal is **not a `rule`**, so
`match-rules` cannot return it (the same trick as `rule-snapshot`). `accept-rule`
creates the rule (`source: learned`, precedence 50, below every user rule);
`reject-rule` keeps the proposal and stops its cluster returning. Nothing is
deleted. While the switch is on, SessionStart runs the detector at most once a
day, detached and silent (`.last_learned_rules` stamp); while off, nothing runs
automatically and `propose-rules` only says how to turn it on.

**`ingest-conventions` is non-destructive.** Re-run it any time to refresh the base
layer: it updates `source=plugin` rules and **keeps every user-authored or
user-edited one**. Restoring the plugin can therefore never undo the user's brain.
Only sections listed in `ALWAYS_APPLY_SECTIONS` (§0, §1) are forced into every
answer — keep that set tiny, since each is a fixed cost on every lookup. Note the
doc labels §4 non-negotiable too, but it binds only when you change behaviour, so it
is matched, not forced.

**`genome --refresh` re-derives what can be derived.** `spec_api_surface` is
introspected from the engine's own argparse and `spec_db_schema` from `sqlite_master`,
so neither can drift from the code the way a hand-written list would. `genome --out
DIR` writes the whole kit — specs, rules, and a manifest — for an AI with no plugin.
Start at `spec_rebuild`.

## 2j. Clearing context is cheap — the brain is what makes it safe

**The context window is the expensive thing, not the graph.** Every request pays
for the whole accumulated window; the graph costs only what you read from it. So
the way to reduce spend is not to store less — it is to **carry less**, and query
instead.

That inverts the usual instinct. Do not preserve context "just in case": park it,
clear it, and ask for it back if it turns out to matter.

**Three moves, in the order you reach for them.**

1. **`park`** — something bulky is in the conversation (a transcript, a long file,
   an analysis) and you are done reading it. Park it and drop it:
   ```
   <blob> | python3 scripts/graph.py park --title "..." --about <node-id>
   ```
   It returns a receipt of a couple of hundred chars — id, size, first line — and
   nothing else. Measured: 2,893 chars in, 245 chars of receipt out. Read it back
   whole or sliced with `unpark --id`.
2. **`/clear`** — then actually clear. This is the single biggest saving available
   and it is under-used because it feels lossy. It is not lossy here: the hooks
   already captured the turns, and the semantic nodes were written as you went.
3. **`resume`** — rehydrate the thread: what was in play, what is parked, what is
   still open, inside a `--budget` (default 1,800 chars). Pure SQL and full-text,
   **no model call**. `SessionStart` fires on `/clear`, so the brief hands most of
   this back automatically — `resume` is the fuller, on-demand version.

**Answer from a query, not from memory of the window.** When a question is about
something already in the brain, look it up (`recall`, `search`, `brain_query.py`,
`match-rules`) rather than keeping the answer resident in context so it can be
re-read later. A budgeted lookup costs ~1.5k chars; the same material sitting in
the window costs that on *every* subsequent request.

**Read paths are budgeted for this reason.** `search`, `recall`, `match-rules`,
`match-skills` and `resume` all cap their payload and point at `get-node --id` for
the one thing you actually want in full. `search --full` exists but is the
exception, not the habit.

**`maintain` is brain-side upkeep, and is a different thing.** It consolidates old
`capture` nodes into one digest per day so *reads* stay small as the store grows —
it does not clear context and it never deletes (§2a). Currently 141 captures
holding 32,869 chars sit behind 3,505 chars of digest, an **89% reduction in what a
read has to traverse**. Safe to schedule; the `SessionStart` hook already runs the
daily pass.

## 2k. Traceability — the tree, what was considered, and what unblocked

The brain records what was **said** (§0). This records what was **considered** —
and brings back the ideas that were only ever dropped for a reason that has since
stopped being true.

The failure it exists to prevent: **an idea was discarded because of a limit that
no longer exists, and nobody knows.** Six months later the same option is argued
from zero, or never revisited at all.

**Everything here is captured by hook or explicit call — never by asking a model
to re-read a transcript.** Extraction costs tokens on every turn, varies between
runs, and omits things without saying so. A hook is deterministic, free, and
adds no latency to the answer. The model is used only where there is judgment:
proposing that something be reconsidered. **Structure is the hook's job.**

### The agent tree

**Every agent has exactly one parent** — the conversation, or another agent.
Never both, never none. `part-of` carries the link, so the tree is the same walk
as any other hierarchy in the graph.

```
conversation
├── agent: main                 <- the person's thread; messages live here
│   ├── agent: Explore          <- one per Task call
│   └── agent: Plan
```

Messages hang off an **agent**, never off the conversation directly. That is what
keeps a subagent's discarded alternative visible from the root instead of dying
with its branch when the Task returns.

Every branch closes with an **outcome** — `completed`, `abandoned`, or `failed`.
An agent with no outcome reads exactly like one that crashed, and the tree stops
being trustworthy the moment that is true. At session end the **main** agent is
`completed`; a subagent still open was **abandoned** — recording both the same
way would make `abandoned` mean nothing.

### Ideas — record the discarded, not just the chosen

Every alternative becomes an `idea` node. Statuses: `proposed`, `selected`,
`discarded`, `superseded`, `revived`. Each move appends an immutable `transition`
node carrying the reason and the turn that caused it, so the **whole history is
reconstructable** — `idea.status` is only the cached head of it.

Three kinds of discard, and only one of them comes back on its own:

| Type | Returns by itself? | For |
|---|---|---|
| **`conditional`** | **Yes — when the condition stops holding** | a missing resource, an absent dependency |
| `preference` | Only if asked for | we went the other way |
| `permanent` | Never | wrong, or out of scope |

### The condition must be structured — this is the whole point

Free text means a human has to read and interpret it before anything can be
re-checked, which means nothing ever is. Structure turns re-evaluation into a
query:

```bash
graph.py idea-discard --id <idea> --type conditional \
  --reason "not enough memory for the model we wanted" \
  --blocked-by "gpu.vram_gb >= 16" --recheck-on hardware.changed
```

`--reason` is for the person who reads it later and is **never used in the
logic**. `--blocked-by` is what the machine acts on. Multiple conditions are a
**conjunction** — all of them have to hold. Operators: `>=` `<=` `>` `<` `==`
`!=` `exists` `not_exists`.

**A conditional discard with no structured condition is rejected at the write.**
It would be a permanent discard in disguise: nothing would ever re-check it, and
the reason text would outlive everyone who could interpret it. Discarding with no
condition is a legitimate thing to do — say so with `--type permanent`.

### The gauge catalog

Each measurable thing is **a row, not code**: a key (`gpu.vram_gb`), what it is,
a **probe** that measures it, its last value and unit. A condition may only name
a gauge that is already in the catalog, or it points at something nobody
measures.

**A new gauge needs no new code** — `gauge-set` a key and a probe, and every idea
blocked on it becomes re-checkable. A gauge with no probe is a gauge nobody will
ever measure; `gauges` warns when one of those is blocking something.

> It is called a **gauge** here and a *resource* in the specification this
> implements. `resource` was already taken in this brain (§3) — a document, a
> link, an artifact.

### Re-evaluation — notify, never execute

A gauge value changes → the ideas waiting on it are found by **one indexed query**
over `blocked_item`. No scan, no model:

```bash
graph.py gauge-set --key gpu.vram_gb --value 24   # measurement moved
graph.py recheck --apply                          # what became possible?
graph.py gauge-probe                              # measure everything, then recheck
```

A revived idea is **a proposal, not a resumption**. The context around it moved
on too, and only a person can say whether it still makes sense. The SessionStart
brief surfaces them as *"became possible again — offer, don't resume"*, and
`recheck` holds a quiet window (7 days) per item so a reconsider prompt cannot
turn into noise a person learns to skip.

### Transactional and log

`transition` nodes are the **log**: immutable, synced to the master, read in
sequence to reconstruct history. `blocked_item` is a **projection** of that log —
a local indexed table, the thing consulted by trigger. Nothing writes it
directly, and `recheck --rebuild` re-derives it from the transitions at any time.
Reconstructing by replaying the log is fine over months and hopeless over years,
so what needs a fast answer does not depend on it.

### Where the token went

Claude Code already writes each turn's token counts to the transcript, so the
`Stop` hook records them for free — `tokens_in`, `tokens_out`,
`tokens_cache_read`, `tokens_cache_write` on the assistant's `capture` node, and
a running total plus `turns` on its **agent**. Per-turn counts say what a turn
cost; the total on the branch answers the question people actually ask — *which
part of the work was expensive*. The hook reads only the **tail** of the
transcript (256 KB), so a long session does not make every turn cost more.

### Hooks never wait on the network

`graph.py`'s `api_*` writers push to the neo4j master **inline** — correct for a
CLI, wrong in a hook. A hook sets `SECOND_BRAIN_NO_AUTOSYNC` for its own process,
writes to the local mirror, and lets a **detached** `graph.py flush` do the
network work. Measured against a deliberately slow master, an inline push cost
**15–45 s per hook**; the detached path stays under 250 ms. This is pinned by
`tests/test_traceability.py` — it is invisible on a machine with no master
configured, which is exactly why it needs a test rather than care.

### What is deliberately NOT instrumented

Every tool call could be a node. It should not be: `Read`, `Grep` and `Bash` fire
constantly, and a few thousand bookkeeping nodes a week would degrade every
recall in the graph to record traffic nobody queries — the exact failure mode of
*put everything in the log* (§2k, transactional and log). So only `Task` is
hooked, because that is the one tool that creates a **branch** with reasoning to
lose. Anything else worth recording is an explicit `context-event`.

### Context as events

The window is recorded as **changes** (`added` / `removed` / `summarized`), not as
a photograph per turn. A snapshot stores the same document fifty times; the
deltas store it once, still replay any instant (`context-at`), and answer what a
snapshot cannot — *why was this in the window at all*, and what did each part
cost.

### What this answers

| Question | How |
|---|---|
| Why didn't we go with X? | `idea-history --id` — the transition and its reason |
| **What unblocked when Y changed?** | **`recheck --key Y` — one indexed query** |
| What did the research subagent discard? | `tree --session` |
| What alternatives existed at all? | `ideas` — every one, not just the winner |
| Why was this in the context window? | `context-at --agent` |
| Where were the tokens spent? | `tokens_*` on the turn, totalled on its agent |

## 2l. Triggers — summoned from Slack and Jira

A mention of the brain in a Slack thread (`app_mention`, Socket Mode) or a Jira
comment (the owner mentioned + the configured text marker, polled) starts **one
stateless headless session** (`claude -p --dangerously-skip-permissions`) that
answers **as the owner**, through the owner's own MCP servers, using the
`respond` skill. The watchers run inside `scripts/serve.py`; the engine is
`scripts/trigger.py` (stdlib). Full design: [`docs/trigger.md`](docs/trigger.md).

- **The bot is an ear, never a mouth.** It has no `chat:write`. Only the session
  (as the owner) replies, and every reply ends with the configured signature.
- **Closed until the owner opens it.** The owner's mention runs and enables that
  conversation (a Slack thread or a Jira issue); anyone else's runs only in an
  enabled one, otherwise it is refused with one notice per conversation. The
  gate is `trigger_gate` in `graph.db`; whole Slack channels opened to everyone
  are `trigger_channel`, kept (marked removed) when closed.
- **Two noes.** `refused` (the conversation is not open, notice sent by the
  engine) and `declined` (the request breaks a rule: the session runs
  `trigger.py decline` and posts the policy notice it prints).
- **In the sender's language, as the owner.** Replies and notices follow the
  language of the mentioning message; the signature is never translated. When
  the owner cannot post in the channel, the requester gets a DM instead.
- **Logged before it runs.** Each mention gets a request id (UUID) in
  `trigger_request` and a `request` node; every action the session takes is
  appended to `trigger_action` with `trigger.py action --request <id>`. A skill
  running under `$MEMORY_LEDGER_REQUEST_ID` logs **every** outward action.
- **Configuration is personal** and lives in `<home>/trigger.json` (mode 600),
  never in the plugin; tokens come from `MEMORY_LEDGER_SLACK_*_TOKEN` /
  `MEMORY_LEDGER_JIRA_TOKEN` first.

---

## 3. Vocabulary

**Node types:** `person`, `task`, `subtask`, `requirement`, `decision`,
`note`, `project`, `reminder`, `writing-sample`, `association`, `meeting`,
`jira-issue`, `resource`, `skill` (learned skill), `team`, `channel`,
`area` (domain hub), `node-type` (one per node type, the skill tree's read/write
target, §5a), `rule-snapshot` (a verbatim copy of a rule, §5b.3), `rule-proposal` (a learned-rules
detector's finding awaiting accept/reject, §2i — never a `rule` until accepted),
`quote`, `parked` (context moved out of the window, §2j),
`rule` (a convention, §2i), `spec` (a genome
blueprint, §2i), `process`, the stateful trackables `pr`, `email`, `message`, and
`request` (one Slack/Jira mention that summoned the brain, §2l), and `usage-week`
(one ISO week of Claude Code usage metrics, written by `scripts/usage_ingest.py`
from the local session logs and drawn by the Usage report — `props.week`, `from`,
`to`, `metrics`; id `usage-week_<YYYY-Www>`, updated in place, never deleted).
Traceability (§2k): `conversation`, `agent`, `idea`, `transition` (one immutable
entry in an idea's history), `gauge` (a measurable thing with a probe — the
specification's *resource*, renamed because `resource` already means a document
here), `context-event`.

**State values** (`props.state`, or task `status`): open, doing, blocked,
waiting, needs-reply · and closed ones: done, cancelled, closed, answered,
resolved, merged, shipped, deployed, declined.

**Idea statuses** (§2k): `proposed`, `selected`, `discarded`, `superseded`,
`revived`. **Discard types:** `conditional` (re-checked automatically),
`preference`, `permanent`. **Agent outcomes:** `completed`, `abandoned`, `failed`.

**Edge relations:** `relates-to`, `blocks`, `blocked-by`, `part-of`,
`has-subtask`, `assigned-to`, `about`, `depends-on`, `causes`, `caused-by`,
`mentions`, `authored-by`, `example-of`, `follows-up`, and for learned skills:
`applies-to`, `learned-from`, `reinforces`, `triggered-by`; and for the skill
tree (§5a): `requires`, `waits-for`, `enriched-by`, `reads`, `writes`, `honors`.
Traceability (§2k)
reuses `part-of` for the agent tree and message→agent, `about` for a transition
to its idea, `blocked-by` for an idea to the gauge holding it back, `caused-by`
for the turn that caused a transition, and adds `supersedes`.

Both vocabularies are **soft** — the engine does not reject an unlisted type or
relation (hooks legitimately write `capture` nodes and `captured-from` edges).
Closed sets are enforced at untrusted boundaries instead: the web UI's write API
(`scripts/serve.py`) accepts only the relations above and only `task`/`subtask`.

**Task status values:** `open`, `doing`, `blocked`, `done`, `cancelled`.
`done`/`cancelled` are the **archive** — hidden from active views but kept.

**Dates** are ISO-8601 with timezone, e.g. `2026-07-23T09:00:00+00:00`.
The person's timezone lives on their `person` node's props (`tz`). Use it when
turning "tomorrow 9am" into an ISO time.

**Props** is a free JSON dict for anything that isn't a first-class column:
`slack`, `tz`, `email`, `jira_key`, `url`, `estimate_min`, `energy`, etc.
- **`assignee`** — who owns a task (the actor). The capture hook **infers** it:
  first-person → owner; **"ask/chase/remind/ping <Name>" → the OWNER** (they do the
  asking; the person is the *target*, not the assignee); "delegate to <Name>" or
  "<Name> will/should/needs to…" → that person; otherwise left unset (NOT assumed
  the owner's). Set it explicitly when you know. Unset = unknown (still counts under
  `--mine`, shows as `unassigned` in the assignee filter).
  **Ingestion may use the model** to judge the assignee (tokens OK when capturing
  notes/emails/threads); **querying stays token-free** — the hook heuristic above
  is the no-model fallback.
- **`delegated`** — set (e.g. `another-plugin:review`) when the work is handed off;
  also excluded from `--mine`. Pair with `waiting_on`.

---

## 4. Keep docs in sync — update them on every behavior change (non-negotiable)

Docs and behavior **ship together**. Any change to how the system works updates
the docs in the *same* change — never "later". A change that alters behavior
without matching doc updates is **incomplete**.

**A "behavior change" means any of:**
- a new / changed / removed `graph.py` command or flag, or a changed default;
- a new / renamed / removed skill (`skills/*/SKILL.md`) or command (`commands/*`);
- a hook change (`hooks/`) — what fires, when, or what it stores;
- a new or changed script / endpoint (`brain_query.py`, `scripts/serve.py`, sync) —
  and a new endpoint also means pinning it in `tests/test_api.py`, whose route
  inventory fails until you do (the OpenAPI document is generated from the routes,
  so it needs no edit);
- a change to the web UI (`frontend/src/`). There is no bundle to commit with
  it — `frontend/dist` is ignored, and `serve.py` rebuilds it from the source;
- a change to storage, sync, realms, vocabulary (node types, edges, states), or
  the install/enable flow.

**Then update every doc that describes it, in the same commit:**
- `CONVENTIONS.md` (this file) — the commands table, vocabulary, and workflow
  rules. This is the source of truth every skill reads; it must never lie.
- `README.md` — the overview, skills table, engine/usage examples.
- `INSTALL.md` — if install, enable, hooks, or config changed.
- the relevant `skills/*/SKILL.md` and `commands/ledger.md` — if their flow changed.

**How:** after editing the engine/hooks/scripts, `grep` the docs for the old
name/flag/behavior and fix **every** mention (don't leave a stale example). If a
default changed, update the example that shows it. Verify commands/flags shown in
docs actually exist (`graph.py <cmd> --help`). Then commit code + docs together.

**Enforced by a pre-commit hook** (`.githooks/pre-commit`), in two stages. First,
a commit that stages behavior (`scripts/`, `hooks/`, `frontend/src/`, or
`.claude-plugin/*.json`) without also staging a docs change (any `*.md`) is
**blocked**. Second, `scripts/check-contracts.sh` runs, so a skill that lost its
contract shape, its graph surface, or its place in the dependency tree blocks the
commit too (§5, §5a). Enable the hook once per clone:

```
git config core.hooksPath .githooks
```

A genuinely doc-neutral change can bypass with `git commit --no-verify` — use it
sparingly, and only when you're sure no doc describes what changed.

---

## 5. Skill contracts: declare the outcome, not the steps (non-negotiable)

A skill is a **contract**, not a script. It declares WHAT must be true when it
finishes and WHICH constraints it may not break. It never declares the keystrokes
to get there. The model already knows how to run a command. What it cannot invent
is what "done" means here, and what it is forbidden to do on the way.

The failure this prevents: a skill written as a step list ages into a lie. The
engine grows a flag, the steps still name the old one, and the skill quietly does
the wrong thing while reading as if it were followed. A contract ages differently,
because the deliverable stays true when the command changes underneath it.

### 5.1 The shape (hard)

Every `skills/*/SKILL.md` carries these eight sections, in this order:

| Section | What goes in it |
|---|---|
| `## When to use` | The trigger, in the person's words. One short paragraph. |
| `## Given` | What the skill receives: the request, the graph state, config. Say what each input is FOR. Context it cannot infer, such as where things live or which command runs them, belongs here, named as context, never as procedure. |
| `## Rules honored` | The `match-rules` call this skill makes before acting, plus the non-negotiable rules it is accountable for, by id. See §5.2. |
| `## Depends on` | `Requires` / `Reads` / `Writes`, mirrored in `docs/skill-dependencies.md` (§5a). |
| `## Deliverables` | Named clauses. Each states what must be TRUE or EXIST when the skill finishes. No steps. |
| `## Judged against` | The conditions someone who never watched the run can check from the graph and the reply alone. |
| `## Guardrails — must not` | What the skill refuses to do, each carrying the failure it prevents. |
| `## Reply (focus contract)` | The shape of the one-action reply §1 demands. |

A deliverable clause is a **public extension point**: a learned rule or a future
skill can aim at it by name. Choose names worth aiming at, and treat a rename as
a breaking change.

Compare:

```
✗  3. Run add-node --type task, then add-edge it part-of the area, then print the id.
✓  **The task exists and is filed.** A task node exists with a due date and an
   owning area, and no orphan is created.
```

### 5.2 Rules are honored, not remembered

The genome (§2i) is the source of truth for how the brain behaves. A skill that
hardcodes a rule forks it. Every skill therefore **asks** rather than restates:

```
python3 "$CLAUDE_PLUGIN_ROOT/scripts/graph.py" match-rules "<what you are about to do>"
```

`## Rules honored` names that call, lists the **shipped** rules the skill is
accountable for by id (`rule_s1_...`, which come from this file), and names the
**rule classes** where the person's own genome will add more. Restating a rule's
*content* inside a skill is a defect: the rule moves, the copy does not.

**Why classes and not ids.** This plugin ships to people whose rules it has never
seen. A skill that names `rule_user_<something>` is naming one person's brain, and
that is exactly the company-specific material the plugin keeps outside itself
(INSTALL.md, `bundle.py --include`). So a skill names the *situation*, and
`match-rules` returns whatever that brain actually holds for it. Four classes
cover the surface:

| Class | The situation | The `match-rules` phrase |
|---|---|---|
| **outward-text** | Text that leaves under the person's name: Slack, email, Jira, a PR body, a commit, an event title. Voice, register, punctuation, emoji, and which language. | `"draft a message to <recipient>"` |
| **mailbox-state** | Changing the read, label, archive or folder state of someone's inbox or queue. | `"change the state of an email"` |
| **durable-storage** | Where a rule or a durable fact is allowed to live. | `"store a durable fact"` |
| **cost** | Which path to take when a token-free one exists. | `"read without spending a model call"` |

A skill declares the classes it touches. The brain supplies the rules.

Two of those, emoji and dash-as-punctuation in **this corpus's own prose**, are
also checked mechanically by `scripts/check-contracts.sh`. That is a house style
for the plugin's own writing, not a claim about the reader's: a corpus that
instructs a model how to write has no standing to read like unedited model output.
Turn it off with `SECOND_BRAIN_SKIP_STYLE=1`.

### 5.3 The four-case test

Before keeping any sentence in a skill, run it through:

1. **A real dependency.** Keep it, in `Depends on`, not as prose.
2. **Irreversible or outward-facing**, such as a send, a delete, or a write into
   someone else's system. Keep it, as a guardrail or a gate.
3. **Knowledge it cannot infer**, such as where the DB lives or which endpoint to
   call. Keep it, in `Given`, named as context.
4. **Telling it HOW.** Delete it, and replace it with a deliverable clause plus
   whatever guardrail the procedure was silently carrying.

Case 4 is the one that loses rules. Every constraint buried in a deleted
procedure must reappear as a guardrail **before** the procedure goes. Losing a
rule in translation is the only unacceptable outcome of adopting this shape.

**Enforced by `scripts/check-contracts.sh`**, run from the pre-commit hook.
Bypass a deliberate exception with `SECOND_BRAIN_SKIP_CONTRACTS=1`.

**A learned skill carries the same eight sections**, in its node body rather than
a file. Two differences follow from where it lives. It may name `rule_user_*` ids
directly, because it is in one person's private brain and not in a corpus that
ships to strangers, which is the whole reason §5.2 makes the built-ins use classes
instead. And no file checker can see it, so `graph.py audit-contracts` is its
gate.

---

## 5a. The skill dependency tree

`docs/skill-dependencies.md` and the `## Depends on` blocks are **two views of one
graph**. Both must agree, and the checker fails the build when they do not.

- **Requires:** the skill's output is WRONG without it. `none` is written
  explicitly, because silence is never a declaration.
- **Optional (ordered):** when that skill is also running, starting first makes
  this one's output wrong, so wait for it.
- **Optional (enriching):** consume it if it has landed, never wait.
- **Reads:** what this skill consults, such as `graph.py` commands, node types, a
  calendar, an inbox. This is its read surface.
- **Writes:** what it leaves in the graph, as node types and edges. A read-only
  skill writes `none`, explicitly.

`Reads` and `Writes` are what make the tree honest for a brain. Second-brain
skills hand off through **the graph**, not through files, so two skills that touch
the same node type are coupled whether or not either one mentions the other. The
tree is where that coupling becomes visible.

### The tree also lives in the graph

A tree that only exists in a document is a tree the brain cannot query. So
`graph.py sync-skill-tree` projects the same declarations into nodes and edges,
as **two trees**:

| Tree | Hub | Declares in | Who is in it |
|---|---|---|---|
| Built-in | `hub_skills_builtin` | the `## Depends on` block of its `SKILL.md` | the skills this plugin ships (`kind: plugin`) |
| User | `hub_skills_user` | props, set by `add-skill --requires/--waits-for/--enriched-by/--reads/--writes` | skills learned in this brain at runtime (`kind: learned`) |

Two trees rather than one, because they answer different questions. The built-in
tree is a property of the plugin and is identical in every install, so a change to
it is a change to the product. The user tree is a property of one person's brain
and is different on every machine. Merging them would make neither answerable:
"what does this plugin do" and "what have I taught it" are not the same question.
The trees are not sealed off from each other, though. A learned skill may
`require` a built-in one, and that edge is exactly where a person's brain grafts
onto the shipped corpus.

Each skill hangs `part-of` its hub. `requires`, `waits-for` and `enriched-by`
connect skills to each other, and `honors` connects a skill to a rule it is
accountable for. `reads` and `writes` point at a `node-type` node,
one per type, which is what makes the coupling visible: open `nodetype_task` in
the map and every skill that touches a task is one hop away, whether or not any of
them mention each other.

**Sync owns what it generated, and nothing else.** Every edge it writes carries
`props.src = "skill-tree"`. On each run it recomputes the declared set, adds what
is missing, and **prunes the marked edges that are no longer declared**, so the
graph cannot keep asserting a dependency the corpus dropped. An edge you made by
hand carries no marker and is never touched. Run it after `ingest-skills`, and
after any change to a `## Depends on` block.

A token named in `Reads`/`Writes` that is not a known node type (a command, an
external system) is kept on the skill as `reads_other` / `writes_other` rather
than being turned into a node, so the graph stays types-only and nothing is lost.

### Accountability is an edge, not only a paragraph

`## Rules honored` names the rules a skill answers to in prose. That is what a
person reads when the skill fires, and it is the wrong thing to make the graph
depend on: finding it means regexing `rule_[a-z0-9_-]+` out of a body, which
guesses, and which a reworded paragraph silently breaks.

So a skill also carries an **`honors`** edge to each rule it is accountable for.
The two are written together — `api_add_skill` puts the ids in the body and the
edges in the graph in one call — and read together: `honored_rules()` takes the
edges first and falls back to the ids in the prose, because the built-in corpus
predates the edge and naming its rules in prose is still a valid declaration.

### The third route between machines: markdown

The DB is the source of truth and the neo4j mirror (§2d) is how two machines
share it. Not everyone has a master to mirror through, so the same content also
moves as files:

```
graph.py export-skills [--dir D] [--kind learned|plugin|all]
graph.py ingest-skills [--dir D]
```

The export writes `D/<name>/SKILL.md` per skill and `D/rules/<rule id>.md` for
every rule any of them honors, and the ingest reads both back — rules **first**,
so a skill landing beside them can link to them. A round trip is lossless: the
contract, the `## Depends on` declarations, and the `honors` edges all survive,
and the result passes `audit-contracts`.

### The fourth route: a bundle

A directory export moves a **whole tree** between two machines you own. Handing
someone three skills is a different question, and it has a different answer:

```
graph.py export-bundle --skill skill_x --rule rule_user_y [--out Z]
graph.py inspect-bundle --zip Z
graph.py import-bundle  --zip Z --select skill_x:update
```

The zip holds **one directory per kind** (`skills/`, `rules/`) and an
**`import-plan.json`**. The plan is why this is not just a zip of the export
directory: the order the files land in decides whether the dependency tree
survives. A skill read before the rule it honors links to nothing; a skill read
before the skill it requires lands with a dangling `requires`. The plan states
the one order in which every link can be made — rules first, then each skill
after the skills it declares — so the importer never guesses and two importers
cannot guess differently. A cycle cannot be ordered, so it is **reported** in
the plan rather than resolved arbitrarily and silently.

**Processes travel too** (`processes/`, `--process ID`), read after the rules
and before the skills. Unlike a contract, a process is **realm-scoped**: it lands
in the importer's configured realm, or the one they pick in the import dialog,
and keeps the realm it came from as `props.origin_realm` — so a teammate's way
of working arrives in *your* work realm and still says whose it was. The Capabilities
page lists processes beside skills, rules and scripts, filters by type (multi-select:
skill, rule, process, script), by source (multi-select: built-in, user, learned; every
process counts as user) and by realm, defaulting to the current realm. A new
process is written from the page's floating **+** button, beside skills and rules.

Selection is respected in both directions. Exporting a skill carries the rules
it honors, for the reason above — but a skill it *requires* and you did not pick
is **named in the report**, not quietly added: expanding someone's selection
behind their back is as wrong as shipping a broken tree, and only one of the two
is visible before the zip is sent. Importing, an item you never mentioned is
**created when it is new and left alone when it already exists**, because a
bundle names dependencies you did not tick, and creating those is what makes the
tree link — while overwriting something you were never asked about is not a
decision an importer gets to make.

A bundle is an **untrusted file**, including its plan. Member names are checked
against the bundle's own layout before anything is read, and the folder name a
skill lands in is slugified rather than used raw: a plan is free to say
`../../.ssh/authorized_keys`, and nothing here ever hands the filesystem a name
that came out of the archive.

Two asymmetries are deliberate. The export **overwrites its own files** but the
ingest **never overwrites a rule this brain already holds** — the incoming copy
may be the older one, and silently losing an edit is worse than skipping an
import you can redo by hand. And a skill naming a rule that did not travel is
**reported, not invented**: a skill pointing at a rule id this brain has never
held would read as accountable and enforce nothing.
## 5b. Rule contracts: a rule that cannot be checked is advice

The same argument as §5, one level down. A rule written as a paragraph of
intention gets argued away the first time it is inconvenient, because nobody can
say whether it was followed. A rule contract makes that decidable.

### 5b.1 The shape

A `rule` node's body carries these sections, in this order. Only `THE RULE` and
`JUDGED AGAINST` are required, because a rule with no checkable outcome is the
thing this shape exists to prevent.

| Section | What goes in it |
|---|---|
| `THE RULE` | What must or must not happen. The clause itself. |
| `WHY` | The failure it prevents. A rule whose reason is missing loses every argument with a deadline. |
| `HOW TO APPLY` | What to do concretely, especially at the moment the rule bites. |
| `EXCEPTIONS` | Where it does NOT apply. Write `none` explicitly: an unstated exception gets invented under pressure, and a rule with no stated limit gets over-applied until someone drops it entirely. |
| `JUDGED AGAINST` | How a reader can tell, from the artifact alone, that it was followed. |
| `RELATED` | `[[wikilinks]]` to the rules this one leans on or bounds. |

**An empty section is left out, never written as a bare heading.** Both checkers
ask whether a heading appears in the body, so a heading with nothing under it
would report the contract as complete and hide the one gap the check exists to
find. A section that is missing looks missing.

`JUDGED AGAINST` is the section that does the work. Compare:

```
✗  Keep messages short and respectful.
✓  A reader who sees only the sent message can find the ask in the first
   sentence, and the message is under ten lines.
```

The first cannot be checked, so it is never enforced and never violated. The
second can be checked by anyone, including the person who wrote it, before
sending.

### 5b.2 GOVERNS is generated, never written

A rule also needs to say *when* it applies. That already exists: `props.scenario`
is the indexed field `match-rules` fires on, in the words a person would actually
type. So the situation is **not** written into the body. Readers that show a
GOVERNS line render it from `scenario`.

Writing it twice would guarantee the two disagree, and the one that matters is
the indexed one: a rule whose body eloquently describes a situation its
`scenario` does not match is a rule that never fires. Converting a rule therefore
means **making its `scenario` complete**, not restating the situation in prose.

### 5b.3 What conversion may not do

The bodies are the person's own writing and carry their judgment, their examples
and the incident that produced them. Conversion **relocates prose under headings
and adds the missing clauses**. It does not paraphrase, compress, or improve the
wording. Losing the reason a rule exists, or the story that makes it stick, costs
more than the structure gains.

Shipped `rule_s*` rules are **excluded** from conversion: they are regenerated
from this file by `ingest-conventions`, so a hand-edit to their bodies is
overwritten on the next run. Their contract is this document.

**Take the backup first.** `graph.py snapshot-rules --label <why>` copies every
rule verbatim into the `rules-backup` realm before the first edit. It is its own
realm rather than a folder or a file because the brain already has a word for
"a section of life that views do not mix together", and a backup that leaves the
graph stops being diffable against the thing it backs up. The snapshots are
`rule-snapshot` nodes, never `rule`, so a backup can never start being read as a
convention. Run it before any bulk edit of the genome, not only this one.

## 5c. Local scripts — the person's own automation, as files

`<home>/scripts` holds the automation that is **not agnostic enough to ship in
the plugin**: it assumes one person's repos, ticket keys or tool set (the home
README says why). The plugin stays company-neutral; these ride alongside it. They
are **files, not nodes** — nothing about a script is written into the graph, and
the brain **never executes one on its own**. The one exception is a script the
person linked to a trigger's `script` action in Settings → Triggers
([docs/trigger.md](docs/trigger.md#actions)): that link is the person's choice,
and the engine runs that file, by name, with argv and no shell. Otherwise it does
three things with them, from the CLI (`scripts`, `export-scripts`,
`inspect-scripts`, `import-scripts`) and from the Capabilities page:

- **List and read.** Each script shows its name, language, size, executable bit,
  and the first line that says what it is (a module docstring or a leading
  comment, past the shebang). Only plain files with a safe name are listed:
  dotfiles, subdirectories (`__pycache__`), symlinks and anything over 2MB are not
  scripts.
- **Export** to `<home>/bundles/scripts-<stamp>.zip` with a `scripts-plan.json`,
  each member under `scripts/` and **keeping its unix mode**, so an executable
  arrives executable. All of them from *Options › Export all scripts*, or the
  ticked ones through selection mode — a zip of their own, never mixed into a
  skills bundle, since a bundle's plan is an import order for contracts.
- **Import** a scripts zip, or a **single loose file** (staged as a zip of one).
  The upload is inspected first and writes nothing; the dialog marks each script
  *new*, *identical* or *differs from yours*. A differing one defaults to **keep
  mine**: a script is code, and an import that silently overwrote a working copy
  is the worst way to find out two machines drifted. **Replace** is an explicit
  choice, and the old bytes go to `<home>/backups/scripts-<stamp>/` before the
  new ones land.

- **Create** one, from a trigger's *New script…* or `graph.py create-script`
  (`POST /api/create-script`): written executable, refused over an existing name
  unless replacing, and a replaced file's bytes are backed up first, as on import.

A scripts zip is **untrusted**: only members named `scripts/<name>` are read, the
name must match a strict bare-filename pattern (no separator, no leading dot, no
`..`), and the file that reaches disk is joined from that checked name — never
from the member path. Selection mode never **deletes** a script: removing one is
done on disk, by the person.

---

