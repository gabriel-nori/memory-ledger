# Memory Ledger

An external working memory for a **busy** brain — a graph-linked local memory
plus a set of skills you drive in plain language (any language), from **Slack,
Claude Code, or the Claude app**. You talk; it remembers, breaks work into tiny
next-steps, sets real alarms, and reaches your other tools. It records as you
work and keeps what is still open: what you owe, to whom, by when.

> **Install:** see [INSTALL.md](INSTALL.md) — `/plugin marketplace add` this repo,
> `/plugin install memory-ledger@memory-ledger`. The home dir and DB are created
> automatically on first session (no manual `mkdir`/`init` step); the command is
> `/ledger`.
>
> **Renamed in 0.7.0.** The repo, the marketplace and the plugin id were all
> `second-brain` until 0.6.0 and are now `memory-ledger`, matching the product
> name. Existing installs must reinstall, see
> [INSTALL.md](INSTALL.md#upgrading-from-second-brain). Your brain is untouched:
> the data stays exactly where it is, and `~/.memory-ledger` is pointed at it
> with a symlink so both names reach the same graph.
>
> **Building on it?** [`docs/`](docs/README.md) is the developer reference —
> [the algorithm](docs/algorithm.md), [query methods](docs/queries.md),
> [search patterns](docs/search.md), [the HTTP API](docs/api.md),
> [the frontend](docs/frontend.md),
> [measured performance](docs/performance.md), [running neo4j](docs/neo4j.md),
> [how it compares to MemPalace](docs/comparison.md),
> [the roadmap](docs/roadmap.md),
> and [how to contribute](CONTRIBUTING.md).

## The idea

Everything you tell it becomes a **node** in a graph (a person, task,
requirement, decision, note, reminder, writing sample…) connected by **edges**
(`assigned-to`, `part-of`, `depends-on`, `about`…). Nothing lives only in your
head, and everything is one link away from its context.

Storage is a single **SQLite** file — fast to query, trivial to back up, and
exportable to readable Markdown or JSON any time.

The brain is **global and lives outside any repo**, at `~/.memory-ledger`
(override with `$MEMORY_LEDGER_HOME`), so every project on your machine shares one
brain and it's never committed. On any machine that predates the rename that name
is a symlink to the original `~/.second_brain`, which is still where the bytes
are — nothing was moved, and both paths work:

```
~/.second_brain/
  graph.db            the memory graph — nodes, edges, AND skills (all live here)
  learned_skills/     optional local SKILL.md exports (only if add-skill --with-file)
  exports/            export-memory output
  neo4j.json          optional graph-mirror config (secret — never commit)
  (routines, definitions live here too)
```

**Finished work is archived, never deleted** — `complete` a task and it moves to
the done archive: out of your active views, but kept, searchable, and still
linked so the brain can learn from what worked.

**One local file, and sections.** The brain is a single SQLite file on your
machine — durable, offline, portable. Nothing about it needs a network, and it
connects to nothing unless you ask it to. Every node carries a **`realm` /
section** (`default`, `work`, `personal`, …) so one graph holds work + personal
life, filterable (`sections`, `add-node --realm`). A fresh brain writes into one
called `default`; `rename-realm --from default --to work` renames it, and writing a
node with `--realm <new>` adds another. Settings has both.

**Optional graph mirror.** If you want one brain across several machines, point it
at a graph server you control and every write mirrors there too. Lose the
connection and writes queue in an offline outbox, draining automatically when it
returns (`neo4j-status`, `flush`, `scripts/brain_sync.py`) — the local file is
always authoritative for reads, so the mirror can never block you. The mirror is a
complete copy that syncs **both ways** (`graph.py sync`, automatic every minute in
the server and at session start), and a lost file is restored from it with
`brain_sync.py --pull --replace`. It is the first step to retiring SQLite and
running on neo4j alone ([the road off SQLite](docs/neo4j.md#the-road-off-sqlite)).

**Areas — nothing durable floats loose.** Every keeper belongs to a **domain
area** (`hub_quote`, `hub_health`, `hub_process`, …), so a domain is one edge walk
away instead of a full-text guess: `areas` lists them with counts, `list --area
health` returns just that domain, and `add-node --area quote` files it as you
create it. Realm and area are orthogonal — realm is *which part of life*, area is
*which domain of meaning*. Add one with `add-area` the moment a second item of a
kind shows up.

**Clearing context is cheap; the brain makes it safe.** The expensive thing is the
context window — every request pays for all of it — while the graph costs only what
you read. So `park` moves a bulky blob out of the conversation and hands back a
~200-char receipt, `/clear` drops the window, and `resume` gives the thread straight
back: what was in play, what's parked, what's still open, inside a char budget and
with no model call. The session brief already re-fires on `/clear`, so most of that
is automatic. Read paths (`search`, `recall`, `match-rules`, `resume`) are budgeted
for the same reason — answer from a lookup instead of keeping the answer resident.
Separately, `maintain` is brain-side upkeep: it rolls old captures into per-day
digests so reads stay small as the store grows (currently an 89% reduction in what a
read traverses), and it never deletes.

**The brain carries its own Claude layer (the genome).** The conventions are not a
file the skills read top-to-bottom — they live in the graph as `rule` nodes, and a
skill asks `match-rules "<what it is about to do>"` for just the ones that apply:
**~8% of the full document, no model call**. The plugin ships the base layer;
`add-rule` lets the user redefine it, and a `user` rule outranks the shipped default
it overrides. Alongside them, `hub_genome` holds `spec` nodes describing the storage,
the hooks, the engine's command surface and the DB schema — the API surface and
schema are *introspected*, so they cannot drift from the code. `genome --out DIR`
writes the whole kit, so given only `graph.db` any AI can rebuild the interaction
layer and reconnect to the storage. The plugin becomes replaceable; the brain does
not.

**Learned rules: opt-in, and only ever proposed.** The `learned` tier is filled by a
token-free detector that is **off until you turn it on** (`graph.py learned-rules on`,
or Settings). It spots corrections you repeat across sessions ("no, never…", "não,
nunca…") and stores a `rule-proposal` — not a rule, so `match-rules` ignores it.
`accept-rule` makes it a `learned` rule (precedence 50, below yours); `reject-rule`
keeps it and stops it coming back. Nothing is applied silently and nothing is deleted.

**The brain grows (neuroplasticity).** When a repeatable procedure shows up, it
`learn`s a new skill — stored **as a node in the graph** (triggers + procedure in
the DB, synced everywhere; not an isolated file). Next time a matching situation
appears, it recalls the skill (ranked by relevance, reinforcement as a booster),
follows it, and `reinforce`s it so proven skills surface first. Learnings are
global, so a skill learned in one project applies in another. (`ingest-skills`
pulls any existing markdown skills into the DB as nodes. The built-in skills are
already there: a brain made from scratch is seeded with them, and `init` and
`doctor` put back any that are missing.)

## Designed around four needs

1. **Externalize memory** — capture everything, resurface it unprompted.
2. **Micro-steps** — every task is auto-split into tiny, startable actions.
3. **Time-awareness support** — due dates, estimates, and *real* alarms that
   fire on their own and DM you.
4. **Reduce overwhelm** — replies are one calm, clear next action, never a wall.

These are enforced in `CONVENTIONS.md`, which every skill reads first.

**Always-on capture — no model call.** When the brain is enabled, plugin hooks
persist every interaction in pure Python/SQL: a `UserPromptSubmit` hook
(`hooks/capture_prompt.py`) stores each prompt and auto-creates tasks for explicit
to-dos; a `Stop` hook (`hooks/capture_answer.py`) stores a summary of the answer,
linked to its question. `PreToolUse`/`PostToolUse` on `Task`
(`hooks/capture_agent.py`) put every subagent in the tree as it spawns and stamp
its outcome as it returns, and `SessionEnd` closes the branch. Deciding and
storing cost **zero** tokens — it is regex and SQL. Two of the hooks do inject a
short status line into the conversation (209 bytes per prompt, a ~1.0 KB
open-loops brief at session start; every other hook injects nothing), which
measures **under 0.05% of a working day — a few cents**. Effectively
free, not literally zero. Full accounting in [What it costs](#what-it-costs).

So a simple answered question becomes a recallable node on its own, and you get it
back by running `graph.py search` / `brain_query.py` — those run outside the
conversation, so recall itself costs nothing. Disable the whole thing with
`graph.py disable` (`/ledger off`) — deleting the brain home no longer works as an
off switch, since the bootstrap hook recreates it on the next session; see
`graph.disabled()` in `scripts/graph.py` for the authoritative check.

## Skills

| Skill | What it does |
|---|---|
| `remember` | Capture info into the graph as linked nodes. |
| `ingest-context` | Ingest a Google Meet transcript, Slack thread, email, or plain text into the graph (auto-ingests meeting-transcript emails). |
| `recall` | Retrieve / resurface context ("what's on my plate", "what's due"). |
| `add-task` | Create a task **and** break it into micro-steps. |
| `complete-task` | Mark a task done → move to the archive (never delete). |
| `follow-up` | Surface every open loop (tasks, PRs, emails, messages) and drive it toward closed. |
| `learn-skill` | Grow a new skill at runtime, recall it, reinforce it (neuroplasticity). |
| `visualize` | Open the live brain UI in the browser — graph **map**, query **dashboard**, **skills** trees + rule catalogue (filterable), and **settings**. |
| `remind` | Set a real alarm (scheduled routine) that DMs you on Slack. |
| `calendar` | Read/create Google Calendar events; time-block tasks. |
| `read-email` | Read & triage Gmail; turn action items into tasks. |
| `send-slack` | Send/schedule a Slack message in your voice. |
| `slack-inbox` | Read Slack and route each message to the right skill. |
| `read-jira` | Read Jira issues/JQL; cache them into the graph. |
| `web-search` | Search the web; save useful findings as resources. |
| `delegate` | Hand a task to a person/agent and track the follow-up. |
| `writing-style` | Learn your voice and write as you, in your language, at the register the reader can act on. |
| `export-memory` | Dump the whole graph to Markdown / JSON. |
| `custom-trigger-builder` | Build a custom trigger with you: a poll written as blocks that starts a task when it finds something ([custom triggers](docs/trigger.md#custom-triggers)). |
| `respond` | Answer a Slack, Jira or GitHub mention as you (or act on a GitHub PR change you subscribed to, or a [B2B](docs/b2b.md) message from a peer brain), from a headless session the trigger engine starts ([triggers](docs/trigger.md)). |

**Every skill is a contract, not a script** (CONVENTIONS §5). Each one declares
what must be TRUE when it finishes, what it reads and writes in the graph, and
what it refuses to do, in eight fixed sections. It never declares the keystrokes:
a step list ages into a lie the moment the engine grows a flag, while a
deliverable stays true when the command changes underneath it. Two things fall
out of that shape:

- **Rules are asked for, never restated.** A skill names the `match-rules` call it
  makes and the *rule classes* it touches (`outward-text`, `mailbox-state`,
  `durable-storage`, `cost`). Your rules stay in your brain, so this plugin ships
  to brains it has never seen without carrying anyone's house rules inside it.
- **The graph is the hand-off.** Skills do not pass artifacts to each other, they
  read and write node types. `docs/skill-dependencies.md` is where that coupling
  is visible, and it has to agree with every skill's declaration.
- **The tree is in the brain too.** `graph.py sync-skill-tree` projects those
  declarations into nodes and edges as **two trees**: `hub_skills_builtin` for
  what the plugin ships, `hub_skills_user` for what you taught it. Each skill's
  `reads`/`writes` point at a `node-type` node, so opening `nodetype_task` in the
  map shows every skill that touches a task, one hop away. A learned skill may
  `require` a built-in one, which is where your brain grafts onto the corpus.
  Sync prunes what it generated and stopped being declared, and never touches an
  edge you made by hand.

`scripts/check-contracts.sh` enforces all of it from the pre-commit hook, down to
linting the corpus's own prose: a set of skills that tells a model how to write
should not itself read like unedited model output.

Front door in Claude Code: **`/ledger <anything>`** routes to all of the above.
**`/plan-today`** picks up only *your* actionable tasks (token-free) and blocks
them into your calendar's free windows — batching by context and protecting one
deep block, because the cheapest day is the one with the fewest re-entries, not
the fullest one. Ends with one clear first action.
**Mention it instead of opening it.** With `<home>/trigger.json` set up, a
mention in a Slack thread, a marked Jira comment, a GitHub mention or a PR change
you picked (reviews, review requests, failing checks, merges; a Jira issue moving
status or changing hands; the bot added to a Slack channel) starts a headless
session that answers there as you, signed as your second brain, and logs who asked for what
under a request id. A Slack command (`@brain deploy {staging, v1.2}`) can do the
same, or call an API or run one of your scripts with no model call at all.
Closed conversations stay closed until you open them, and turning someone away
costs nothing. See
[docs/trigger.md](docs/trigger.md).

**Say it in a meeting.** Give the brain a name (Settings → Preferences) and a
wake phrase like `{brain} will handle`. Say "Brain will handle this, send them the
deck" on a call, and when the brain ingests the transcript the task starts at
once, with the lines around it as context. Only your own phrases run, unless
you mark one for anyone. See
[docs/trigger.md](docs/trigger.md#wake-phrases).

**Let brains talk to each other.** B2B (Brain 2 Brain) is a small in-memory
relay (`services/b2b/`) where one brain leaves a message for another. Your user
id is your public key, a challenge proves you hold the private key, and messages
are sealed by the sender so only the recipient can read them, then deleted the
moment they are read. Your brain polls every broker you set up, on the interval
you set, and a message from a peer you connected (linked to a person) lands in
Tasks and runs like a trigger. See [docs/b2b.md](docs/b2b.md).
**`/neo4j-up`** stands up the optional local graph database (Docker, one command,
data under `~/.second_brain/neo4j`) and points the brain at it.

## The graph engine

`scripts/graph.py` — a dependency-free CLI over SQLite (FTS5 search, graph
traversal, due-date queries, export). Skills call it; you can too:

```bash
python3 scripts/graph.py add-node --type task --title "Ship report" --due 2026-07-23T09:00:00+00:00
python3 scripts/graph.py recall "report"        # associative: matches + what they connect to
python3 scripts/graph.py search "report"
python3 scripts/graph.py due --within-days 7
python3 scripts/graph.py neighbors --id <node-id> --depth 2
python3 scripts/graph.py export --format both
```

The DB path resolves as: `$SECOND_BRAIN_DB` → `$SECOND_BRAIN_HOME/graph.db` →
`~/.second_brain/graph.db` (default). Same in Claude Code and the Claude app.
See `CONVENTIONS.md` §2.

```bash
python3 scripts/graph.py complete --id <id> --note "shipped"   # archive, don't delete
python3 scripts/graph.py add-skill --name triage-x --title "..." --when "..." --body "..."  # node-first (--with-file for a local .md)
python3 scripts/graph.py ingest-skills                         # pull markdown skills into the DB as nodes
python3 scripts/graph.py sync-skill-tree                       # project the contracts into the graph: built-in + user trees
python3 scripts/graph.py snapshot-rules --label pre-contract   # verbatim backup of every rule into the rules-backup realm
python3 scripts/graph.py audit-contracts                       # which DB-side contracts (learned skills, user rules) are incomplete
python3 scripts/graph.py match-skills "a situation"            # recall: relevance-ranked, reinforcement-boosted
python3 scripts/graph.py use-skill --id skill_triage-x         # reinforce
```

## Ideas that come back when the reason they were dropped stops holding

The brain records what was **said**. It also records what was **considered** —
and, for an alternative dropped only because something was missing, brings it
back when that stops being true. The failure it prevents: *an idea was discarded
because of a limit that no longer exists, and nobody knows.*

Every alternative becomes a node, not just the chosen one. A discard carries its
reason **and**, when it is conditional, a machine-checkable condition:

```bash
python3 scripts/graph.py gauge-set --key gpu.vram_gb --value 8 --unit GB \
        --probe "nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits"

python3 scripts/graph.py idea-discard --id idea_run-13b-locally --type conditional \
        --reason "not enough memory for the model we wanted" \
        --blocked-by "gpu.vram_gb >= 16"
```

Free text would mean a person has to read and interpret it before anything gets
re-checked — which means nothing ever does. So **a conditional discard without a
structured condition is rejected at the write**, and the condition may only name
a gauge that something actually measures. Months later:

```bash
python3 scripts/graph.py gauge-probe        # measure; a new box, a new value
python3 scripts/graph.py recheck            # -> "run the 13B model locally" came back
```

One indexed query answers *what became possible*, with no scan and no model call.
It **notifies and never resumes** — the context moved on too, so a person decides
whether it still makes sense. The session-start brief surfaces them, with a quiet
window so a reconsider prompt cannot become noise.

The same hooks build the **agent tree**: every agent under exactly one parent,
messages hanging off an agent rather than the conversation, and every branch
closing with an outcome — so what a subagent considered and dropped stays visible
from the root instead of dying when its Task returns.

```bash
python3 scripts/graph.py tree --session <session-id>   # the branch, with its ideas
python3 scripts/graph.py ideas --status discarded      # every alternative, and why
python3 scripts/graph.py idea-history --id <idea>      # the full, ordered history
```

The same `Stop` hook records **where the tokens went** — Claude Code already
writes the counts to the transcript, so they cost a bounded tail-read: per turn
on the message, totalled on its agent, so you can see which branch was expensive.

Only the `Task` tool is hooked, not every tool: `Read`, `Grep` and `Bash` fire
constantly, and thousands of bookkeeping nodes a week would degrade recall for
records nobody queries. `Task` is the one tool that creates a branch with
reasoning to lose.

Everything here is captured **by hook or explicit call**, never by asking a model
to re-read a transcript: deterministic, free, and adding nothing to the answer's
latency. Full rules in `CONVENTIONS.md` §2k.

## One person, one node

The same colleague arrives through many doors: a Slack display name, an email local
part, a first name in a meeting transcript, a surname in a Jira comment. Left alone
that yields `Mari`, `Maria` and `Maria Castillo` as three people, and quietly
splits their work three ways.

So person creation goes through an identity guard. A name is matched on its
normalized parts — case, accents and punctuation folded, an email reduced to its
local part — at two confidence levels, because the two possible mistakes do not cost
the same. **Merging two real people is far worse than leaving a duplicate**: it
silently reassigns work. So:

| | example | what happens |
|---|---|---|
| **certain** | `maria.castillo@example.com`, `Castillo, Maria`, `Maria Luisa Castillo` | resolves to the existing node |
| **likely** | `Mari`, `Maria`, `Castillo` | resolves **only if exactly one** person fits |
| **no match** | `Daniel Reyes` vs `Daniela Reyes` | stays a separate person |

Two names that both carry a surname and disagree on it are different people, however
much the first name matches. An ambiguous name is **refused with its candidates**
rather than resolved by coin flip.

```bash
python3 scripts/graph.py people                        # the roster, one entry per human
python3 scripts/graph.py who "Mari"                     # which node does this spelling mean?
python3 scripts/graph.py person-dupes                  # report duplicates already in the graph
python3 scripts/graph.py merge-person --into <keep> --from <dup>
python3 scripts/graph.py add-alias --id <person> --alias "Bebel"   # what the heuristic can't infer
python3 scripts/graph.py add-node --type person --title "..." --force-new   # two people really do share a name
```

A merge **never deletes and never rewrites a name that was already written down**.
Edges move to the canonical node, the duplicate's spelling is pinned there as an
alias, and the duplicate stays in the graph as a resolver (`superseded_by` plus an
edge to follow). A capture that said "Mari" still says "Mari" — and still resolves to
the right human, because identity is resolved when **read**.

All of that is also a page: **People** in the web UI lists the roster, expands any
person to what they connect to, and puts the duplicate groups at the top with the
merge one click away. Same matching, same merge, no model call — the CLI is no
longer the only place identity gets tidied.

## Less noise, tiered decay

Not every prompt deserves a node. The `UserPromptSubmit` hook runs a regex filter
(`scripts/noise.py`, no model) before it writes: bare "continue"/"ok", slash
commands, pasted system-reminders and JSON, image-only pastes and short no-verb
fragments are dropped — and counted on the session's conversation node. An error,
a decision word, a URL, a file path or a config/git change is always kept. Set
`capture_filter` to `cold` to park the raw prompts in a `cold` realm instead.

Memory also ages by kind: decisions halve every 90 days, tasks and bugfixes every
14 once closed (an open loop never fades), captures and changes every 7
(`scripts/decay.py`, configurable). `recall`
applies it. To tidy up:

```bash
python3 scripts/graph.py decay            # dry-run: what would expire, which tasks look like raw prompts
python3 scripts/graph.py decay --apply    # weekly digest notes; stale raw-prompt tasks -> dropped
```

Nothing is deleted. See CONVENTIONS §0 and §2h.

## Recall behaves like memory

`recall` is the retrieval path — prefer it over `search`. It seeds on full-text,
spreads activation across edges (so asking about a ticket also surfaces its PR, its
blocker and its owner), and ranks by **memory strength**: reading reinforces, disuse
decays on a ~21-day half-life. Nodes that co-activate **3 times** get wired together
automatically, so the graph densifies from use instead of manual linking. Raw
`capture` nodes are covered by a daily digest (`consolidate`, non-destructive), so
recall surfaces one summary instead of forty log entries — and that digest happens
**automatically**: the session-start hook runs it once a day in the background,
folding closed days while today stays raw.

It is also the cheap path: `match-skills` shrank **7,449 → 682 chars (−91%)** and a
context lookup **−41%** — roughly **2,000 tokens saved per routed request**. See
CONVENTIONS §2g.

## Token-free views (`brain_query.py`)

Everyday reads don't need the model. `scripts/brain_query.py` answers the common
date views in pure SQL — **no model call** — as a text table or `--json`. Run it in a
terminal and it costs nothing at all; run it from a session and you pay only for the
rows you read back:

```bash
python3 scripts/brain_query.py agenda      # overdue + today + tomorrow
python3 scripts/brain_query.py open        # ALL open tasks (dated or not) — best with --assignee
python3 scripts/brain_query.py week        # next 7 days (+ overdue)
python3 scripts/brain_query.py closed --since-days 7
python3 scripts/brain_query.py range --from 2026-07-20 --to 2026-07-31 --status open
python3 scripts/brain_query.py agenda --mine   # only what YOU must act on
```

Add **`--mine`** to any view (dashboard: the "🙋 Mine · needs action" toggle) to
drop tasks assigned to someone else, delegated away, or parked waiting/blocked —
leaving only the owner's live actions (`$SECOND_BRAIN_OWNER`, default `Owner`;
unassigned counts as the owner's). Or filter by exact owner with
**`--assignee me|unassigned|<name>`** (dashboard: one toggle chip per assignee).
`--mine` is **strict**: only tasks whose `assignee` is the owner (not unassigned,
not someone else), and never delegated or parked waiting/blocked. Rows carry
**blocked_by / blocks / related** flags from the graph edges (dashboard shows
🔒/⛓/🔗 badges), cards **expand on click** for the full body + linked neighbors
(`/api/node`), and you can **set a due date right in the dashboard** — click a
card's date chip → date picker (writes token-free via `/api/set-due`). Captured
to-dos get their **assignee inferred** (mine / a named person / unassigned — never
blindly "me"; "ask/chase <Name>" stays *yours*).
Local-tz buckets via `--tz`, `$SECOND_BRAIN_TZ`, or Settings › Time zone.
`--backend sqlite|neo4j|auto` picks the store; with no flag it follows the
**`backend` setting** (`$SECOND_BRAIN_BACKEND`, then `<home>/settings.json`,
default `sqlite`) — so the CLI, the hooks and any cron share one default, set
once in Settings › Database connection or by `neo4j_local.py up`.
Recall stays free — reach for this (or `graph.py search` / `open-loops`) instead
of asking the model to remember.

**Another app can be the task board.** `graph.py track --id <task> --system
<name> --ref <id>` hands a task to an external board (a kanban, a desk, a team
tracker). The brain keeps remembering it but stops nagging: the session brief
counts it in one line ("Tracked elsewhere: 3 in kanban") instead of listing it,
and the board closes it back with `complete`. `list --untracked` is what a board
pulls; `untrack` releases a task. CONVENTIONS §2c has the contract: `track`, `untrack`, `complete`, `update-node`, `list`.

## What it costs

The honest split: **the always-on part never calls the model.** Only the skills that
reach out (recall through Claude, ingest, Slack/Gmail/Jira) spend tokens, and those
run when you ask — or, for ingest, once or twice a day rather than on every hook.

**Verify the zero-token claim yourself.** No path in `hooks/` or `scripts/` calls a
model API — the only outbound HTTP is the optional neo4j mirror:

```bash
grep -rn -iE "anthropic|api\.claude|openai|httpx" hooks/*.py scripts/*.py
# only neo4j HTTP tx endpoints in graph.py / brain_query.py / brain_sync.py
```

### Measured hook overhead

Each hook's real output, measured by running it (`python3 hooks/<hook>.py < event.json`):

| Hook | Fires | Injected into context | Latency |
|---|---|---|---|
| `capture_prompt.py` | every prompt | 209 bytes (~52 tokens) | 81 ms |
| `open_loops_brief.py` | session start / clear / compact | ~1.0 KB (~267 tokens) | 236 ms |
| `capture_answer.py` | every answer | **nothing** (writes only) | 78 ms |
| `capture_agent.py` | each `Task` call, before and after | **nothing** (writes only) | 81 / 76 ms |
| `close_conversation.py` | compaction, session end | **nothing** (writes only) | 78 / 76 ms |

The open-loops brief scales with how many loops are open and is budget-capped, so it
does not grow without bound. It now also lists anything that **became possible
again** (§2k) — bounded to three lines, each item held behind a 7-day quiet window.
The traceability hooks write only; they add **nothing** to any request.

**No hook waits on the network.** Writes land in the local SQLite mirror and are
drained to the neo4j master by a *detached* flush, so a slow or wedged master
cannot stall a prompt. Pinned by a test against a deliberately slow server —
every hook stays under 250 ms where an inline push costs 15–45 s
(`tests/test_traceability.py`).

### What that costs per day

At Claude Opus 5 input pricing ($5/MTok; $6.25 cache write, $0.50 cache read), a
heavy day of ~60 prompts across ~6 sessions:

| | Tokens/day | Cost/day |
|---|---|---|
| Per-prompt status line | 60 × 52 = 3,120 | $0.016 |
| Session-start brief | 6 × 267 = 1,602 | $0.008 |
| **Always-on total** | **~4,700** | **~$0.02–0.03** |

Set against what a real day of Claude Code actually costs on the same machine — run
`npx ccusage@latest daily` and compare your own — the brain's always-on overhead lands
**under 0.05% of a working day**. Capture, search, open-loops, task insert, and every
date view stay at exactly **zero** tokens — they are SQLite.

The parts that *do* spend tokens are ordinary Claude Code work, billed like any other
prompt: `/ledger` recall, `ingest-context`, and the Slack/Gmail/Jira skills. Ingest is
deliberately manual (once or twice a day) rather than hook-driven, to keep that
bounded.

Re-run the whole measurement any time:

```bash
npx ccusage@latest daily                     # your real spend, per day
echo '{"session_id":"m","cwd":"'"$PWD"'","prompt":"test"}' > /tmp/hookin.json
for h in open_loops_brief capture_prompt capture_answer; do
  printf '%s: %s bytes\n' "$h" \
    "$(python3 hooks/$h.py < /tmp/hookin.json | wc -c | tr -d ' ')"
done
```

## Visualize the brain

A local server serves one page with **five sections**, behind a
hamburger menu on the left (`m` toggles it):

- **Map** — the live graph, centred on you: your person node in the middle and
  everything else in rings by how many hops away it is, settling once and then
  holding still so a node can be clicked. Zoom/pan, a search you can clear, and
  one multi-select dropdown per facet (node types / realms / kinds / display),
  each with its own **all** and **none**. Under Display, **shade instead of hide**
  changes what filtering *looks like*: filtered-out nodes stay on the map, dimmed
  and unlabelled, so what you kept can be read against the shape it sits in rather
  than in isolation. Same set either way — a search hit stays lit through it.
  Click any node to inspect every connection and its direction. Polls the DB so changes appear in real time. The
  inspect card is also where you **act on** what it's showing, so working in the
  graph never means leaving it:
  - `✓ Complete` on a `task`, `subtask` or `reminder` — archived, not deleted.
  - `✎ Edit` on any node — title, body and realm, plus due, priority and
    **assignee** on the actionable types. One save is one write.
  - Assignee is a dropdown of **person nodes**, never a typed name (see
    [One person, one node](#one-person-one-node)). Pick a human, and the card
    writes both the `assignee` prop every query filters on and the `assigned-to`
    edge the map draws.

  The toolbar's **+ idea** records an alternative without leaving the graph: a
  title, an optional detail and a realm become an `idea` node, `proposed`, through
  `POST /api/propose-idea` (the same write as `graph.py idea-propose`). The map
  re-reads at once and opens the new idea's card, centred on it.

  All of it is hidden on a `--no-write` server.
- **Dashboard** — the `brain_query.py` views (agenda / open / today / tomorrow /
  week / closed / range) as cards, over the same store. It is also a **full
  editor**: create tasks, tick them off, change status/assignee/due, rename them,
  and add or remove relations — all token-free (see below).
- **People** — every person node as a card, expanding to everything one hop away
  grouped by relation, and above them the groups that look like **one human
  twice**. Detection is name matching across every alias a node answers to, with
  **no model call**; merging moves the edges onto the node you keep, pins the
  other spelling as an alias, and leaves the duplicate pointing at its
  replacement, so a capture that said "Mari" still resolves to Maria. The engine
  proposes the most connected node as canonical — a radio overrules it, because
  only a human can say which spelling is the real person.
- **Messages** — [brain-to-brain](docs/b2b.md) messages as conversations: the
  brains you talked to on the left, with search and a new-conversation button,
  and the thread on the right. Every message is stamped with when it was sent
  and flagged 🧬 when a person typed it in the chat or 🤖 when a session or a
  script sent it. Messages show their formatting: **bold**, *italic*,
  __underline__, ~~strike~~, `code` and links (Markdown, Slack's `<url|text>`,
  or a bare URL), all rendered as text and links only, never as HTML. The
  composer has buttons for each, and ⌘B / ⌘I / ⌘U / ⌘K. A thread opens at its
  latest message; older ones load as you scroll up.
- **Agents** — [chat with Claude or a local Ollama model](docs/agents.md). Each
  turn is one stateless call given your message, the last few exchanges and what
  the brain recalls for it; every exchange is written back to the graph. The list
  shows a spinner while a reply runs, **?** when the agent is waiting on you and
  ✓ when it answered. Switch agent or model mid-conversation, keep it read-only
  (the **Read-only** box, ticked by default) or untick it to turn tools on, and open the folded log of every tool and skill it used. The
  chat header searches the thread, shows the live cost, exports PDF / text /
  JSON, and hides a chat under the active list. The **Ledger Agent** is the
  other way to talk to Claude from here: a floating terminal box over whatever
  you are doing, running the real `claude` CLI in the folder you opened it from
  (`python3 scripts/agent_panel.py`, or the pop-out next to **＋ New**) — see
  [docs/agents.md](docs/agents.md#ledger-agent-the-floating-box).
- **Capabilities** — skills, rules, processes and scripts, in that order. The
  two contract trees (built-in and learned), nested by the
  `requires` edge, beside the rule catalogue, the processes, and your local
  scripts in `<home>/scripts`, with one filter over all of them. Open a
  skill to see what it reads and writes in the graph, the rules it answers to,
  and **its relations one level deep**; open a rule to read it in full. The `+`
  button authors a new skill or rule — see
  [Grow the brain from the page](#grow-the-brain-from-the-page).
- **Usage** — "Where the Quota Went", drawn by `scripts/usage_report.py` from
  the brain's `usage-week` nodes (one per ISO week of Claude Code usage). The
  plugin writes them itself: `scripts/usage_ingest.py` reads the session logs
  Claude Code already keeps (`~/.claude/projects`) and aggregates them per week —
  cost, sessions, turns, typed prompts, model split, cost composition (cache
  reads/writes), session-length buckets, repositories edited, zero-write output,
  rate-limit hits, and cost by hour and weekday. It is a **token-free** report, so
  a person who is out of Claude Code quota can still see their numbers; the
  model-written insights stay with `ccalyze`. **No model call and no clock** in
  the report: the same weeks give the same page, byte for byte, with static SVG
  charts. Pick a week range, then **Download HTML** (the exact page) or **Save as
  PDF** (the print dialog, opened on the report alone). From the CLI:
  `python3 scripts/usage_report.py render [--weeks N | --from YYYY-Www --to YYYY-Www] [--out file.html] [--ingest]`,
  or over HTTP at `GET /api/usage-report`. See
  [the usage ingest](docs/usage.md) for how the numbers are made and kept fresh.
- **Settings** — **your name** (who `--mine` means), timezone (which day "today"
  means), default realm, read backend, poll intervals, and three switches: mirror
  autosync, daily consolidation, and whether this browser's view preferences are
  kept in the brain. Plus **is it capturing?** with the switch to change it,
  **doctor** on demand, the database connection (local mirror + neo4j master,
  with a test and a queue flush), and **consolidation** — which shows what it
  would change, asks, and snapshots the whole graph first (kept 5 days).
  **Custom triggers** lists your [block polls](docs/trigger.md#custom-triggers)
  with their last run and an on/off box, and edits each one's YAML with
  validate, save and a dry run that creates nothing.

  Preferences are saved to `~/.second_brain/settings.json`, so the CLI, the hooks
  and any cron read the same answer as the page — not to the browser. What you
  were *looking at* is a different question: the open view, the filters, the map's
  display toggles are this browser's, kept in `localStorage` and restored when you
  come back, and syncing those to the brain is the one opt-in switch, for when you
  use the same brain from a second machine.

### Feed the brain without the model

The dashboard is a complete CRUD client, so the AI is one way in, not the only
one. Click **＋ New task** and type; as you write, memory-ledger scans your title
for things it already knows and proposes how they connect:

```
"Rerun the export once PR #7366 lands, needs the migration first, with Kenji"
                        └ follows-up      └ blocked-by          └ mentions
```

Ticket keys (`ABC-4521`), PR numbers (`#7366`), `#channels` and people are all
recognised, and the wording picks the relation — `blocked by` → `blocked-by`,
`caused by` → `caused-by`, `part of` → `part-of`, `depends on` → `depends-on`.
Every suggestion is a row you can re-point or uncheck before saving. Expand any
card to complete it, move its state, reassign it, edit it, or manage its links
with a `×` and a search box. No model call, no tokens — the same engine the CLI
uses (`graph.py suggest-links "…"`).

### Grow the brain from the page

The `+` on the Capabilities section writes new **contracts** — a rule, or a skill —
without a model call. Both forms are generated from the shapes the server sends,
so the page can never disagree with the engine about what a contract is, and both
are **refused when a required section is empty**: a rule with no `JUDGED AGAINST`
cannot be checked, and a skill with no `Deliverables` is a step list waiting to
age into a lie.

Nothing is chosen from a dropdown. Rules, skills and node types are picked from
searchable cards that show what each one is for, with the full contract one click
away, so you are choosing between things you can read rather than between titles.

A skill's rules get **one line each**, added and removed freely. `+ New rule…`
opens a rule form on top of the half-filled skill one and hands the result back
as a pending line — nothing is written until you save the skill, so abandoning it
leaves nothing behind. Each linked rule lands as an `honors` edge in the graph as
well as a line in the contract, so a skill's accountability is something the
graph can walk and not just a paragraph a regex has to guess at.

Editing an existing contract still happens where its check lives: a built-in in
its `SKILL.md`, gated by the pre-commit checker.

### Move a brain between machines by file

The neo4j mirror is the usual way to share one brain across machines. When there
is no master to mirror through, the same content travels as markdown:

```bash
python3 scripts/graph.py export-skills --dir ~/brain-out   # SKILL.md + rules/
python3 scripts/graph.py ingest-skills --dir ~/brain-out   # on the other machine
```

Or the two buttons on the Capabilities section. The export writes one folder per skill
plus every rule they honor; the ingest reads the rules first so the skills can
link to them. A round trip is lossless — contract, declarations and `honors`
edges all survive — and it never overwrites a rule the receiving brain already
holds, because the incoming copy may be the older one.

### Hand someone a few skills

A whole tree is the wrong shape for "send me your three review skills". For a
**selection**, bundle it:

```bash
python3 scripts/graph.py export-bundle --skill skill_x --rule rule_user_y
python3 scripts/graph.py inspect-bundle --zip ~/.memory-ledger/bundles/skills-*.zip
python3 scripts/graph.py import-bundle  --zip … --select skill_x:update
```

The zip holds one directory per kind and an `import-plan.json` naming the order
the items can be read back in — rules first, then each skill after the skills it
requires — so the dependency tree survives the trip. Rules a selected skill
honors travel with it; a skill it *requires* that you did not pick is named in
the report rather than added behind your back.

In the browser this is the **Options** menu on the Capabilities section: *Select
skills, rules, processes and scripts* puts a checkbox on every card, and the bar that
appears exports or deletes what you ticked. *Import from a bundle* opens a zip,
lists what is in it, and for anything this brain already holds asks whether to
**update**, **replace** or **ignore** it — nothing is written until you confirm.
Processes in a bundle land in your current realm unless you pick another.

### Carry your own scripts

The automation in `<home>/scripts` (too tied to one person's setup to ship in the
plugin) is listed under **Scripts** on the Capabilities section. Open one to read
it; the brain never runs one on its own, only a script you linked to a trigger
([docs/trigger.md](docs/trigger.md#actions)). *Options › Export all scripts* zips them with their
executable bits; *Import scripts…* takes that zip, or one loose file, shows which
are new, identical or different from yours, and keeps yours unless you say
**Replace** — the replaced file is copied to `<home>/backups/` first.

```bash
python3 scripts/graph.py scripts
python3 scripts/graph.py export-scripts                       # all of them
python3 scripts/graph.py import-scripts --zip scripts-*.zip --all --mode keep
```

New things are written from the floating **+** button (or `n`) on the Capabilities
section: *a process* (title, when it applies, steps, and a realm dropdown of the
brain's realms, starting on the current one), *a skill* or *a rule*. What it writes appears on the page at once,
and a process saved in a realm the filter is not showing adds that realm to it.

Writes are unauthenticated and bound to localhost; serve with `--no-write` if you
expose the UI on your network.

```bash
python3 scripts/serve.py            # opens http://127.0.0.1:8770/ in your browser
python3 scripts/serve.py --no-open --host 0.0.0.0 --port 8770 &   # reachable on LAN / phone
```

The UI is a React single-page app (`frontend/`) over a FastAPI backend
(`scripts/brain_api/`). The UI is **built by the server** on the first start
and rebuilt when the source changes, so it needs `npm` on the machine — the API
never does; the backend's dependencies are installed by `serve.py` itself, into
its own venv, on the first start. See
[the frontend](docs/frontend.md) to work on it. The API describes itself at
**`/api/docs`**: Swagger UI over an OpenAPI 3.1 document the running process
builds, so it can never describe a server other than the one answering you.

Reads neo4j when reachable, else the SQLite mirror. Binds to localhost by default;
use `--host 0.0.0.0` (behind a tunnel) to reach it from your phone. The dashboard
**writes** too (see above), and the write API is unauthenticated — add
`--no-write` for a read-only server when exposing it.

### Voice in the Agents chat

Dictation, reading aloud and conversation mode are opt-in (Settings > Voice, `voice.json`). Reading uses macOS `say` by default; set `BRAIN_TTS_ENGINE=piper` and `BRAIN_PIPER_PYTHON` (a Python with `piper-tts`; voices in `~/.cache/brain-piper`) to use Piper. The agent replies in the language of your message, typed or dictated: `reply_language_instruction` in `scripts/voice.py` detects it from the text and adds "reply in that language" to the system prompt.

## Install

See **[INSTALL.md](INSTALL.md)** for the full flow. In short:

**Claude Code** — install as a plugin (marketplace add this repo →
`/plugin install memory-ledger@memory-ledger`). The home dir and DB are created
automatically the first time a hook fires — no manual init step. Enabling
at user level makes it active in every session. Talk to it with `/ledger ...` or
invoke skills by name, and run `/ledger doctor` (or `python3 scripts/doctor.py`)
any time you want to confirm it's actually capturing.

**Claude app** — upload the skill folders as Agent Skills. The same graph engine
runs in the sandbox; keep memory portable via `~/.second_brain/` or a connected
persistent location.

**No repo access** — `python3 scripts/bundle.py` builds
`dist/memory-ledger-<version>.zip` from the git-tracked files (no brain data), with
an `INSTALL-FROM-ZIP.md` inside. Add `--with-brain` for engine **and** brain in one
private zip (a consistent `graph.db` snapshot plus your non-secret home files);
`scripts/restore.py <zip>` puts it back ([details](INSTALL.md)). Unzipping is the install: a plugin loads from a
directory, so `/plugin marketplace add ~/dev/memory-ledger` works on a local copy
with no marketplace or GitHub permission.

**A local neo4j (optional)** — `/neo4j-up`, or
`python3 scripts/neo4j_local.py up`: a Docker `neo4j:5` with
`--restart unless-stopped`, its store bind-mounted at `~/.second_brain/neo4j`,
localhost-only, password generated into `neo4j.json`, made the default read
backend, and the existing brain backfilled into it. SQLite remains the durable
mirror — see [docs/neo4j.md](docs/neo4j.md) for why you would, and why you might
not.

## Connected machines

Reminders/alarms use scheduled routines; Calendar, Gmail, Slack, and Jira use
their MCP connectors. Reading is free; anything outward (send, create, schedule)
is confirmed once before it happens.
