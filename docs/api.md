# HTTP API

`scripts/serve.py` serves two things over one port: this JSON API, and the
React single-page app built from `frontend/`. It calls **no model** — it is the
graph engine behind a socket, so everything it does is token-free. The one
exception is by design: `POST /api/agent-send` and `/api/agent-retry` start a
model call for the [Agents](agents.md) tab.

The API is a FastAPI app in `scripts/brain_api/`, run under uvicorn. `serve.py`
itself stays stdlib-only and is still the one command you run: on first start it
builds a venv for the server from `scripts/requirements-server.txt` (see
[The server's environment](#the-servers-environment)) and re-enters it on every
start after that. The engine, the hooks and the CLI never import any of it.

The UI needs npm; the API does not. `frontend/dist` is not in git, so the
server builds it before binding the port on first start, and rebuilds it when
the source is newer than the bundle; `--no-build` skips that and `--rebuild`
forces it. With no `npm`, every endpoint still answers and only the page is a
503 naming the reason.

```bash
python3 scripts/serve.py                    # http://127.0.0.1:8770
python3 scripts/serve.py --port 9000 --host 127.0.0.1
python3 scripts/serve.py --no-write         # read-only
python3 scripts/serve.py --no-open          # don't open a browser
python3 scripts/serve.py --no-trigger       # don't start the Slack/Jira/GitHub watchers
```

When `<home>/trigger.json` enables them, the server also runs the
[trigger](trigger.md) watchers for as long as it is up. `--no-trigger` keeps them
off, and so does `--no-write`: a read-only server must not act on your behalf.

## The interactive reference

**This file is the prose; [`/api/docs`](http://127.0.0.1:8770/api/docs) is the
reference.** Start the server and open it: Swagger UI with every endpoint, every
parameter and request body, and a Try-it-out button that hits your own brain.

| Where | What |
|---|---|
| `GET /api/docs` | Swagger UI. The viewer loads from a CDN; the document does not. |
| `GET /api/openapi.json` | The OpenAPI document, generated from the routes at runtime. |

There is **no checked-in copy and nothing to regenerate.** FastAPI builds the
document from the route decorators and the request models in
`scripts/brain_api/models.py` when it is first asked for, so it is the router
itself, described — it cannot hold an endpoint the server does not have, or
miss one it does. Summaries and descriptions live on the decorators; field
descriptions live on the models.

What the document does not check is the *behaviour*: the envelope, the status
codes, the refusals. That is `tests/test_api.py`, which starts the real
`serve.py` against a throwaway brain and pins every route from the outside —
and which holds the route inventory, so a route appearing or disappearing
without a matching edit there fails the suite.

## Security — read this before changing `--host`

**The write API is unauthenticated.** There is no token, no session, no CSRF
protection. Anything that can reach the port can rewrite your graph.

- Default bind is `127.0.0.1` — keep it there.
- If you must expose it, pass `--no-write`.
- Do not put it behind a plain reverse proxy and call it done; that publishes an
  unauthenticated write API to whoever can reach the proxy.

Writes are enabled unless `--no-write` is passed, in which case every `POST`
returns 403 with `{"ok": false, "error": "server started with --no-write; writes
are disabled"}`.

## The envelope

Every answer is JSON with an `ok` boolean, and every failure carries `error`: the
reason worth showing a person. The page displays it as-is
(`frontend/src/api/client.ts`), so FastAPI's own error shapes never reach it —
`scripts/brain_api/errors.py` maps each one back:

| Situation | Status | Body |
|---|---|---|
| A write the engine or a closed set refused | 400 | `{"ok": false, "error": "status must be one of open, doing, …"}` |
| A body that is not JSON, not an object, or has a field of the wrong type | 400 | `{"ok": false, "error": "title: Input should be a valid string"}` |
| `--no-write` and a known write route | 403 | as above |
| An unknown route, or a method a route does not take | 404 | `{"ok": false, "error": "not found"}` (`unknown view: …` under `GET /api/`) |
| A handler that raised | 500 | `{"error": "<the exception>"}` |

A missing body is an empty object, so a route refuses it on its merits
("missing id") rather than as malformed. `null` for a text field means empty.
A junk entry inside a list of objects (`links`, `new_rules`) is skipped, not
fatal. Text is length-capped, never refused for length. Most reads answer 200
even when `ok` is false — an unknown node is a successful read of nothing; the
exceptions are the date views (400 on a bad query) and `/api/bundle` (404).

Every response is `Cache-Control: no-store`.

---

## Conventions

- All responses are JSON with an `ok` boolean; failures carry `error`.
- `POST` bodies are JSON.
- String inputs are length-capped server-side (`id` 200, `title` 300,
  `assignee` 60, `note` 500, `due` 40, `status` 20). Over-long values are
  truncated, not rejected.
- Nodes created through the UI get `props.source = "web-ui"` so UI-origin data
  is distinguishable from hook-captured and skill-written data.

---

## GET — reads

| Endpoint | Returns |
|---|---|
| `/api/graph` | nodes + edges for the map canvas |
| `/api/views` | the dashboard view payloads |
| `/api/assignees` | distinct assignees, for the filter chips |
| `/api/realms` | every realm in the brain + `{nodes, open_tasks}` counts, and the server's `default` |
| `/api/kinds` | every `props.kind` + the same counts, and the `none_token` for nodes carrying no kind |
| `/api/node?id=<id>` | one node in full plus its edges (the expand view) |
| `/api/meta` | the vocabulary — node types, realms, kinds, edge rels, statuses, and the two contract shapes (`skill_sections`, `rule_sections`, `decl_labels`, `node_types`) |
| `/api/skills` | the two skill trees, the rule catalog, `node_types` (each with its description, how many of it the brain holds, and which skills read or write it), and the area titles the filters label themselves with |
| `/api/rule?id=<id>` | one rule in full, for the skill drawer |
| `/api/rule-proposals?status=&budget=` | learned-rule proposals (default `proposed`), budgeted; readable while the detector is off |
| `/api/scripts` | the local scripts in `<home>/scripts`: `dir`, `exists`, and per script `name`, `bytes`, `executable`, `language`, `summary`, `sha256`, `modified`. Files the listing leaves out for size are in `skipped` |
| `/api/script?name=<file>` | one script's `text`. `name` must be a bare filename in `<home>/scripts`; anything else is `no such script` |
| `/api/bundle?name=<file>` | an exported bundle, as a zip attachment. `name` is reduced to a bare filename, so it can only name a file in `<home>/bundles` |
| `/api/suggest-links` | proposed edges awaiting acceptance |
| `/api/search?q=<q>` | node picker results for the relation editor |
| `/api/agent-icon` (POST) | `{data?}` | the brain's icon: `data` is the image, base64 (PNG, JPG, ICO or SVG, up to 512 KB, type read from the bytes, SVG with scripts refused). Stored as `<home>/agent-icon.<ext>` and recorded in the `agent_icon` setting; empty `data` removes it. Answers with the settings payload |
| `/api/agent-icon` | the brain's icon: the image uploaded in Settings, else the default `favicon.svg`. `/api/meta` carries `agent_icon` (its mtime, 0 = default) as a cache-buster |
| `/api/settings` | stored preferences, plus what is actually `effective` and which keys the environment has overridden |
| `/api/timezones` | every IANA zone name, for the timezone picker |
| `/api/status` | where the data lives: db path, home, node/edge counts, queued writes, loose captures, and the neo4j master's reachability |
| `/api/people` | every person node with the spellings it answers to, for the People section and the assignee picker |
| `/api/person-duplicates` | groups of person nodes that look like one human, with the canonical pick. Name matching over every alias — **no model call** |
| `/api/capture` | whether the ledger is recording, and which switch decided it (`env_off` matters: `$MEMORY_LEDGER_OFF` outranks the marker) |
| `/api/doctor` | the health check as JSON — the same report `scripts/doctor.py` prints |
| `/api/consolidate-preview?keep_days=N` | what a consolidation run **would** change. A read: it writes nothing |
| `/api/backups` | the snapshots consolidation leaves behind, each with when it expires |
| `/api/ui-prefs` | the browser's stored view state, and whether syncing it is on |
| `/api/trigger?limit=N` | the [trigger](trigger.md) config (whether each token is set, never the token), what this process's watchers are doing, request counts by status, enabled conversations, the open and closed Slack `channels`, and the newest requests |
| `/api/trigger-request?id=<uuid>` | one request and every action taken for it |
| `/api/custom-triggers` | the [custom triggers](trigger.md#custom-triggers): `dir`, whether they are scheduled here (`active`: the master switch and `custom.enabled`), the secret names in `trigger.json` (never a value), one row per file with its last run, and a `template` for a new one |
| `/api/custom-trigger?id=<id>&limit=N` | one custom trigger: the file as written (`text`), its `errors` and `warnings`, `state`, `checkpoint` and the newest `runs` |
| `/api/trigger-people?q=<name or email>&source=jira` | Jira accounts matching `q` (active humans only), as `{user_id, name, email}`, to allow one. Asks Jira with the stored token; reads only |
| `/api/b2b-conversations` | every brain this one messaged or heard from, newest first, plus connected peers not talked to yet: name, whether it is connected and allowed, the message count and the last message |
| `/api/b2b-thread?peer=` | both directions with one peer, oldest first; each message carries `at` (when it was sent), `human` (typed in the Messages chat, rather than by a session or a script), and for a received one its request and status |
| `/api/agents` | the [Agents](agents.md) config and every connector with the models it offers now (an Ollama host is asked for its own) |
| `/api/agent-conversations` | every agent conversation, newest first, hidden ones flagged; `state` is its badge (`pending`, `question`, `done`, `error`, `empty`), with the last message and the total cost |
| `/api/agent-commands` | the slash commands the composer offers (`name`, `plugin`, `kind` skill or command, `description`, `writes` — a guess from the name and description) and every installed plugin (`key`, `enabled`, `commands`); read from disk, no model call |
| `/api/voice` | the voice settings, whether faster-whisper and a TTS engine work here, and the engine's voices |
| `WS /api/voice-stt` | dictation: PCM16 16 kHz in, `partial`/`final` text out (docs/agents.md#voice) |
| `/api/agent-thread?id=` | one conversation, oldest first; each reply carries its status, event log (tools, skills, subagents, thinking), recalled node ids, tokens and cost — live while it runs |
| `/api/b2b` | the [B2B](b2b.md) config (the user id; whether the private key is set, never the key), the brokers and their sessions, the peers, the messages sent, the received ones by status, and what this process's poller is doing |
| `/api/tasks?limit=N` | every trigger task, newest first (default 200, max 500), for the Tasks section: who fired it, the source, a `name` (command, title or event kind), the status and timings, `cost_usd` (with `cost_estimated` when it was worked out from the transcript because the run failed or timed out before claude reported it), `counts` by status. A running task carries `progress`: the subagents, actions and latest answer the hooks have written to the database, plus the tool calls from its transcript. `text` and `result` are clipped |
| `/api/task?id=<uuid>` | one task in full: the request with its `command`, `cwd`, `session_id` and `stderr`, every action, `activity` (the session's conversation, agents and a timeline, read live from the database) and `transcript` (the session's text and tool calls with input and result; the newest 400 events, each field clipped to 4000 characters) |
| `/api/<view>?<params>` | any `brain_query.py` view: `agenda`, `open`, `today`, `tomorrow`, `week`, `closed`, `range` |

`/api/meta` exists so the UI never hardcodes vocabulary. Two of those sets are
**closed** — `rels` and `statuses` are enforced on write — and two are **open**:
`types`, `realms` and `kinds` are whatever the brain actually contains. Nobody's
brain has the same list, so all three are read at runtime and the UI renders what
comes back. Invent a realm with `add-node --realm skunkworks`, or a kind with
`--props '{"kind":"postmortem"}'`, and it is a filter chip on the next poll, with
no code change.

`/api/realms` reports two counts per realm because they answer different
questions: `nodes` is what the map filters over, `open_tasks` is what the
dashboard filters over. A realm with 40 nodes and 0 open tasks is real, and the
chip says so rather than showing a number that filters down to nothing.

Nodes in `/api/graph` each carry a `realm`, and `meta.by_realm` gives the totals —
that is what drives the map's realm legend. `meta.ego` is the id of the owner's
person node, the centre the map lays itself out around.

The dashboard views accept the same parameters as the CLI, as query string:

```
/api/agenda?mine=1
/api/open?assignee=unassigned
/api/agenda?realm=personal
/api/open?realm=work,shared&mine=1
/api/open?kind=postmortem
/api/agenda?kind=(none)          # nodes that carry no kind at all
/api/range?from=2026-07-20&to=2026-07-31&status=open
/api/closed?since_days=14
/api/week?tz=America/Sao_Paulo
```

`realm` takes a comma list; omitting it means every realm. An empty realm on a
node is normalised to the configured default (`$SECOND_BRAIN_REALM`, `default`
out of the box), so filtering never silently drops untagged nodes.

`kind` works the same way over `props.kind`, which is optional and sparse — most
nodes have none. Rather than hide those, they land in an explicit `(none)` bucket,
so "everything without a kind" is a filter you can express. On the **map** every
kind is offered; on the **dashboard** only kinds that appear on open tasks are,
since the dashboard lists tasks and a kind with none would be dead UI.

---

## POST — writes

| Endpoint | Body | Notes |
|---|---|---|
| `/api/set-due` | `{id, due}` | `due` is ISO; empty clears it |
| `/api/add-task` | `{title, type?, assignee?, priority?, realm?, due?, body?, links?}` | `type` must be `task` or `subtask`; `realm` defaults to the server's |
| `/api/complete` | `{id, note?}` | moves to the archive; never deletes |
| `/api/set-status` | `{id, status, waiting_on?}` | `status` must be one of `graph.TASK_STATUSES` |
| `/api/set-assignee` | `{id, assignee}` | |
| `/api/update-node` | `{id, title?, body?, props?}` | empty `title` is rejected |
| `/api/link` | `{src, dst, rel}` | |
| `/api/unlink` | `{src, dst, rel}` | |
| `/api/propose-idea` | `{title, detail?, realm?}` | an `idea` node, `proposed`, with its first transition logged — the write `graph.py idea-propose` makes (CONVENTIONS §2k). `realm` must be a slug and defaults to the server's current one. The map's **+ idea** button |
| `/api/settings` | `{tz?, realm?, owner?, brain_name?, backend?, autosync?, consolidate?, ui_sync?, learned_rules?, map_poll_seconds?, dash_refresh_seconds?}` | writes `<home>/settings.json`; only these keys, each validated. `""` **removes** a key (back to env/system default). A boolean key takes `true`/`false`; `false` is a value, not an absence |
| `/api/capture` | `{enabled}` | writes (or removes) `<home>/.disabled` — the same switch `graph.py disable` throws. **Refused** while `$MEMORY_LEDGER_OFF` is set in the server's environment, because that outranks the marker |
| `/api/add-alias` | `{id, alias}` | pins another spelling onto a person, through the engine's `api_add_alias`. A spelling they already answer to is a no-op |
| `/api/ui-prefs` | `{prefs}` | **replaces** `<home>/ui_prefs.json` (≤64KB). The browser holds the whole blob; merging would resurrect a preference just cleared |
| `/api/backup` | `{label?}` | snapshots the graph now and expires the ones past their window |
| `/api/rename-realm` | `{old, new}` | moves every node in `old` to `new` (merging if `new` exists), follows it in the default realm setting, snapshots first; `updated` is untouched |
| `/api/neo4j-config` | `{url, db?, user?, password?}` | writes `<home>/neo4j.json` (mode 600) and reports whether the master answered. Blank `password` keeps the stored one; empty `url` deletes the file (sqlite-only) |
| `/api/neo4j-test` | `{}` | runs `RETURN 1` against the configured master |
| `/api/flush` | `{}` | drains the outbox to the master |
| `/api/prune` | `{confirm, keep_days?}` | runs `graph.py maintain` (default `2`). Non-destructive — consolidates, never deletes. **Refused without `confirm: true`**: preview with `GET /api/consolidate-preview` first. Takes a snapshot before the first write |
| `/api/consolidate` | `{confirm, keep_days?}` | the same operation under the name the CLI uses, so a client can run the plan it previewed without renaming it |
| `/api/create-rule` | `{title, scenario, source?, section?, realm?, non_negotiable?, overrides?, sections{}}` | authors a rule (§5b). **Refused** unless `sections.the_rule` and `sections.judged_against` are filled |
| `/api/propose-rules` | `{}` | runs the learned-rules detector; proposes, never applies. A no-op that says so while `learned_rules` is off |
| `/api/accept-rule` | `{id, title?, text?}` | a proposal becomes an active rule: `source: learned`, precedence 50 |
| `/api/reject-rule` | `{id, reason?}` | marks a proposal rejected; kept, and not proposed again |
| `/api/create-skill` | `{name, title, description?, realm?, sections{}, requires?, waits_for?, enriched_by?, reads?, writes?, rules[], new_rules[]}` | authors a skill (§5). **Refused** unless the six typed sections of §5.1 are filled. `rules[]` are existing rule ids, `new_rules[]` are whole rule bodies created first in the same call |
| `/api/export-skills` | `{kind?, dir?}` | `graph.py export-skills` — a `SKILL.md` per skill plus every rule they honor |
| `/api/ingest-skills` | `{dir?}` | `graph.py ingest-skills` — reads a folder back in; a rule this brain already holds is kept, never overwritten |
| `/api/export-bundle` | `{skills[], rules[]}` | `graph.py export-bundle` — a selection as one zip under `<home>/bundles`; returns `path`, `name`, a `download` URL, and any required skill that did not travel |
| `/api/upload-bundle` | `{filename, data}` | base64 zip, ≤32MB. Stages it and returns a one-shot `token` plus every item with `exists`. **Reads only — nothing is written** |
| `/api/import-bundle` | `{token, select{id: update\|replace\|ignore}}` | applies the staged bundle in its plan's order and consumes it. An unmentioned item is created when new, untouched when it exists |
| `/api/trigger-gate` | `{scope, enabled}` | opens (`true`) or closes a conversation to other people's mentions. `scope` is `slack:<channel>`, `slack:<channel>:<thread_ts>`, `jira:<KEY>` or `github:<owner>/<repo>#<number>`; anything else is refused. `enabled` must be a boolean |
| `/api/trigger-config` | `{enabled?, signature?, refusal?, claude?{…}, slack?{…}, jira?{…}, github?{…}, clear?[]}` | merges a validated patch into `<home>/trigger.json` (mode 600) and restarts this server's watchers on it. Only the keys sent change; an unknown key is ignored. **Secrets are write-only**: `slack.app_token` / `bot_token` / `user_token`, `jira.token` and `github.token` are replaced by a non-empty value, kept by an empty or missing one, and removed by naming them in `clear`. None is ever sent back; `config.secrets` says only `set`, `stored` and `source` (`env` outranks the file). A token with the wrong prefix (`xapp-`, `xoxb-`, `xoxp-`; `ghp_`, `github_pat_`, … for GitHub) is refused, as are out-of-range numbers, a non-http(s) Jira or GitHub URL, and an unknown key under `github.events`. `slack.events`, `jira.events` and `github.events` merge per kind and per field: `{"ci_failed": {"on": true}}` leaves its `do` and every other kind alone. Each event takes `on`, `do`, and an action (`action`: `claude` \| `http` \| `script`, with `http{method,url,headers,body}` or `script{name,args}`); a switched-on one that could not run (an API call with no URL, a script not in `<home>/scripts`) is refused. `slack.commands` is replaced whole and checked: unique names, a pattern that compiles and whose named groups are not variables. `slack.allowed_users` / `blocked_users` are Slack user ids. `meeting.phrases` is replaced whole and checked: at least two words, only `{brain}` and `{owner}` in a phrase, none listed twice. `refuse_without_model` and `refusals.not_allowed` are top-level. An unknown `{variable}` anywhere a template goes is refused ([docs/trigger.md](trigger.md)) |
| `/api/custom-trigger` | `{id, text, check_only?}` | validates the YAML and writes `<home>/triggers/<id>.yaml` only when it is valid (400 with `errors` otherwise). With `check_only`, writes nothing and answers 200 with the verdict in `valid` |
| `/api/custom-trigger-dry-run` | `{id}` | runs the saved trigger's blocks (its HTTP calls go out) and answers the `events` it would create and the `checkpoint` it would set. Writes no task, no checkpoint, no run |
| `/api/custom-trigger-enable` | `{id, enabled}` | rewrites only the `enabled:` line of the file. Switching on refuses a trigger that does not validate. The scheduler sees it on its next pass |
| `/api/trigger-check` | `{only?, offline?}` | checks `claude`, `slack`, `jira` or `github` (empty: everything switched on), calling Slack, Jira and GitHub with the stored tokens unless `offline`. Starts and writes nothing. `ok` means the check ran; **`ready`** is the verdict, with `problems` and `notes` |
| `/api/trigger-channel` | `{channel, name?, enabled}` | opens (`true`) or closes a Slack channel to everyone's mentions. `channel` is an id or any Slack link into it. Closing keeps the row, marked removed; reopening restores it. Returns the whole list as `channels` |
| `/api/trigger-person` | `{source?, user_id, name?, enabled}` | lets one person (`true`) tag the brain on any conversation of `source` (only `jira`, the default), or takes them off. `user_id` is their Jira account id. Taking off keeps the row, marked removed. Returns the whole list as `people` |
| `/api/trigger-reprocess` | `{request_id}` | puts a `blocked`, `refused`, `failed`, `timeout`, `interrupted` or `cancelled` request back in the queue and runs it now (on the trigger pool, or on its own thread when the watchers are off), with the action it was stamped with. Clears what the last run left; its logged actions stay, and a `reprocess` action is added. Anything else is refused |
| `/api/trigger-cancel` | `{request_id}` | stops a `queued` or `running` request: it turns `cancelled` at once, a running session or script is killed within a second, and an HTTP call already sent finishes but its answer is not kept. Logs a `cancel` action. Anything else is refused |
| `/api/trigger-scan` | `{text, title?, link?, where?, at?, key?, me?[], dry_run?}` | scans a meeting's transcript for the wake phrases (`meeting.phrases`) and starts a task for each one said, on the trigger pool: the owner's at once, anyone else's only for a phrase marked `anyone` (the rest are logged `refused`). A line already scanned for the same meeting (`key`, else `link`, else `title`) runs nothing again. `dry_run` returns the `matches` and whether each `runs`, logging nothing. Refused while the master switch or `meeting.enabled` is off ([docs/trigger.md](trigger.md#wake-phrases)) |
| `/api/trigger-decline` | `{request_id, reason, rule?}` | what the session calls for a request that breaks a rule: marks it `declined`, logs the rule and reason, and returns the policy `notice` (with `{reason}` filled) and the `signature` to post |
| `/api/agent-send` | `{text, attachments?, conversation?, connector?, model?, full_tools?, context_turns?}` | stores the message (with a link to each attached file) and queues the reply in the background; with no `conversation`, makes one titled from the message. **Starts a model call** |
| `/api/agent-attach` | `{name, data}` (base64, up to 25 MB) | saves a file the owner attached under `<home>/attachments/` and returns its `path`; no model call |
| `/api/agent-conversation` | `{id?, title?, connector?, model?, full_tools?, context_turns?, hidden?, draft?, draft_at?}` | creates a conversation (no `id`) or changes one; only the keys sent change. A `draft` older than the stored `draft_at` is ignored |
| `/api/agent-retry` | `{id}` | runs a failed reply again with the conversation's current connector. **Starts a model call** |
| `/api/agent-cancel` | `{id}` | stops a queued or running reply: it fails at once as cancelled (Retry runs it again). A Claude session is killed; an Ollama chat stops at its next round |
| `/api/agent-plugin` | `{key, enabled}` | turns an installed plugin on or off in `~/.claude/settings.json` (`enabledPlugins`, which every Claude session reads); answers the new `/api/agent-commands` payload |
| `/api/agent-panel` | `{cwd?}` | opens the Ledger Agent floating terminal box on this Mac (macOS only), detached from the server, running the `claude` CLI in `cwd` (default: the repo). The first open builds it (`building: true`). No model call until you ask |
| `/api/voice-config` | `{stt_language?, stt_auto?, tts_language?, tts_auto?, translate?, translate_target?, tts_enabled?, tts_background?, silence_s?, tts_voices?, verbosity?}` | merges a validated patch into `<home>/voice.json` |
| `/api/voice-translate` | `{text, target?}` | one block translated (a model call); skipped when already in the target |
| `/api/voice-speak` | `{text, language?, voice?}` | the block as a WAV; open on `--no-write` too |
| `/api/agent-config` | `{connectors?[], default_connector?, context_turns?, recall_limit?, recall_budget?, max_concurrent?}` | merges a validated patch into `<home>/agents.json`; `connectors` replaces the list |
| `/api/b2b-config` | `{enabled?, brokers?[], private_key?, poll_seconds?, do?, action?, http?, script?, clear?[]}` | merges a validated patch into `<home>/b2b.json` (mode 600) and restarts the poller. The private key is write-only: `clear: ["private_key"]` removes it. `brokers` replaces the list. `poll_seconds` is 30 to 86400; an unknown `{variable}` in `do` is refused ([docs/b2b.md](b2b.md)) |
| `/api/b2b-identity` | `{force?}` | makes this brain's X25519 key pair; the public key is the user id. Refuses to replace one unless `force` |
| `/api/b2b-broker` | `{id?, url?, name?, enabled?, remove?}` | with no `id`, adds the broker at `url`; with one, changes it, or drops it with `remove` |
| `/api/b2b-broker-check` | `{id}` | answers that broker's challenge now and opens a one-hour session |
| `/api/b2b-peer` | `{peer_id, name?, broker_id?, person_id?, enabled, allow?}` | connects (`true`) or disconnects a peer, through a broker, linked to a person node. Connecting also puts it on the allowed list unless `allow` is false. Disconnecting keeps the row; returns the whole lists as `peers` and `allowed` |
| `/api/b2b-allowed` | `{user_id, name?, enabled}` | allows (`true`) a user id to send to this brain, or blocks it again (`false`, the row is kept). Only an allowed sender's messages run anything; anyone else's are logged `blocked`. Returns the whole list as `allowed` |
| `/api/b2b-send` | `{to, text, human?}` | seals and sends to a connected peer, by id, name or person, through its broker, and logs it. `human: true` (the Messages chat) marks it as a person's; it is sealed with the text, so the peer sees it too |
| `/api/trigger-action` | `{request_id, kind, target?, detail?}` | appends an action to a request's audit log (what `trigger.py action` does). Refused for an unknown request or an empty `kind` |
| `/api/export-scripts` | `{names[]}` | `graph.py export-scripts` — the named scripts (all, when empty) as one zip under `<home>/bundles`, executable bits kept; returns `path`, `name`, `count`, `scripts` and a `download` URL through `/api/bundle` |
| `/api/upload-scripts` | `{filename, data}` | base64, ≤32MB: a scripts zip, or one loose script (a name not ending in `.zip`), staged as a zip of one. Returns a one-shot `token`, the target `dir`, and every script with `exists` and `identical`. **Reads only — nothing is written** |
| `/api/create-script` | `{name, text, executable?, replace?}` | writes one script into `<home>/scripts`, executable unless `executable: false`. An existing name is refused (`exists: true`) unless `replace`, and then its old bytes go to `<home>/backups/scripts-<stamp>/` first (returned as `backup`). What a trigger's *New script…* calls |
| `/api/import-scripts` | `{token, select{name: replace\|keep}}` | lands the selected scripts in `<home>/scripts` and consumes the token. New files are written either way; an existing one is overwritten only for `replace`, after a copy to `<home>/backups/scripts-<stamp>/` (returned as `backup`). A name not in `select` is not imported |
| `/api/delete-contracts` | `{ids[], force?}` | `graph.py del-contracts` — deletes skill/rule nodes and their edges. A built-in is refused unless `force`, since its file would re-ingest it |

### Authoring contracts

The two create routes exist so the brain can grow without a model call, and they
are **stricter than the CLI**, not looser. `graph.py add-rule` accepts a half
contract and leaves the gap for `audit-contracts` to report; these refuse it at
write time, because a form has all the fields in front of it and no excuse to
submit one that cannot be checked.

The shapes come from `/api/meta` rather than being written into the page. A
heading typed into `index.html` would be a second definition of §5.1, and the one
nothing checks:

```json
"skill_sections": [
  {"field": "when_to_use",   "heading": "## When to use", "required": true,  "generated": false},
  {"field": "rules_honored", "heading": "## Rules honored", "required": false, "generated": true},
  …
]
```

`generated: true` marks the two sections nobody types — `## Rules honored` is
rendered from `rules[]`, `## Depends on` from the declaration fields — so the
form knows to leave them out.

The pickers those forms open are fed from `/api/skills`, which is why
`node_types` carries more than a list of names: a card that says only `gauge`
asks the person to already know what a gauge is, and the readers and writers are
what say which skills would become coupled to one that declares it.

A skill's rules land **twice on purpose**: as ids in the `## Rules honored` prose,
which is what a person reads when the skill fires, and as `honors` edges, which
is what the graph can walk (§5a). `new_rules[]` are written before the skill and a
failure in one aborts the whole call, so a skill is never left accountable to a
rule that does not exist.

Example:

```bash
curl -s localhost:8770/api/set-due \
  -H 'content-type: application/json' \
  -d '{"id":"task_review-pr_1a2b","due":"2026-08-04T18:00:00-03:00"}'
```

Validation is server-side and explicit — an invalid `status` comes back with the
allowed set in the error string rather than being silently coerced.

**A disconnected client is not a 500.** The map polls, so a reload aborts a
response mid-write. The server returns silently rather than reporting the error
back down a socket that has already closed, which would raise a second exception
out of the error path. A genuine handler bug still comes back as a 500 with its
reason, and the server stays up either way.

---

## Settings

Preferences live in **`<home>/settings.json`**, not in the browser. A timezone is
only useful if the CLI, the hooks and any cron reading the same brain agree with
the page that set it, and `localStorage` cannot do that. Eleven keys, all optional:

| Key | Effect |
|---|---|
| `tz` | IANA name deciding which calendar day a due date falls on (`today`, `week`, overdue) |
| `realm` | default realm for new nodes when nothing else says |
| `owner` | who "me" is — what `--mine` filters on, and what an unassigned task falls back to |
| `brain_name` | what the brain answers to: `{brain}` in a wake phrase, a signature or a run command, and the name in the page's sidebar and tab (default `Brain`; needs a letter or digit, no braces, up to 40) |
| `agent_icon` | filename of the brain's uploaded icon in `<home>` (`agent-icon.png`, `.jpg`, `.ico`, `.svg`). Set by `POST /api/agent-icon`, not `POST /api/settings`; empty means the default favicon |
| `backend` | which store a CLI read uses when given no `--backend` (`sqlite`, `neo4j`, `auto`) |
| `autosync` | push each write to the neo4j mirror, instead of only queueing it (default on) |
| `consolidate` | let the daily hook fold closed days into digests (default on) |
| `learned_rules` | the learned-rules detector, which only proposes (default **off**; `$MEMORY_LEDGER_NO_LEARNED_RULES` forces it off) |
| `ui_sync` | keep the browser's view preferences in the brain as well (default off) |
| `map_poll_seconds` | how often the map re-reads `/api/graph` (default 2) |
| `dash_refresh_seconds` | how often the dashboard re-runs its view (default 120) |

Precedence is the same everywhere in the engine: **explicit flag > env var > this
file > built-in default**. `GET /api/settings` returns the stored values, the
`effective` ones, and an `env` map naming every key a shell variable has taken
over — the Settings page shows that as a warning rather than pretending the save
took effect.

**The three switches are overridden by a NEGATIVE env var** —
`$MEMORY_LEDGER_NO_AUTOSYNC`, `$MEMORY_LEDGER_NO_CONSOLIDATE`,
`$MEMORY_LEDGER_NO_UI_SYNC` — because that is the spelling every existing rc
file, cron and CI job already uses. A truthy one forces the switch off and
outranks the file, exactly as `$MEMORY_LEDGER_OFF` outranks the `.disabled`
marker. `effective.<key>_source` says which of `env`, `settings` or `default`
answered, so the page can grey out a box it cannot change and say why.

`realm` and `owner` are re-resolved in-process on save, so they apply to the next
write and the next view instead of after a restart.

### What is NOT a setting

Which dashboard view you had open, how far back *closed* looks, what the map has
filtered out. These change nothing the engine computes, so forcing them through
the same file would make every idle click a disk write on a value no other reader
cares about. The browser owns them in `localStorage`, and mirroring them into
`<home>/ui_prefs.json` is opt-in (`ui_sync`) for the one case `localStorage`
cannot serve: the same person on a second machine. The blob is opaque to the
server on purpose — its shape belongs to the frontend, so a new toggle needs no
server change.

Capture's own switch is not a setting either: it is the `<home>/.disabled` marker
plus `$MEMORY_LEDGER_OFF` (§2d), read by `GET /api/capture` and written by
`POST /api/capture`.

### Upkeep, and undoing it

Consolidation is non-destructive — every capture is kept and linked to the digest
that stands in for it (§2a) — which makes it safe, not reversible. So:

* `GET /api/consolidate-preview?keep_days=N` reports what a run **would** do and
  writes nothing. The plan comes from `graph.consolidation_plan`, the same
  function the run itself consolidates from, so the two cannot drift.
* `POST /api/prune` (or `/api/consolidate`) is **refused without `confirm: true`**.
* A run snapshots the whole graph to `<home>/backups/` first, kept
  `graph.BACKUP_KEEP_DAYS` (5) days. Restore by copying one back over `graph.db`
  with the server stopped. `GET /api/backups` lists them; `POST /api/backup`
  takes one on demand. The daily session-start hook passes `--no-backup`, since a
  snapshot every morning for a run nobody asked for is just disk.

`GET /api/doctor` returns the same report `scripts/doctor.py` prints. Its `ok`
means the call succeeded; `healthy` is the verdict — an unhealthy brain is a
successful request, not a failed one.

**The neo4j password is never sent to the browser.** `/api/status` reports
`password_set` only, and an empty `password` on `POST /api/neo4j-config` means
"keep the stored one". The file is written mode 600.

---

## Reload

The server restarts itself when its own code changes:

```bash
python3 scripts/serve.py                 # reload on, the default
python3 scripts/serve.py --no-reload     # off
```

This exists because of a failure that is genuinely hard to diagnose from the
browser. The built page is read from disk on **every request**, but `serve.py`,
the `brain_api` package, `graph.py` and `brain_query.py` are imported **once at
startup**. A server left running for hours therefore serves a CURRENT page
against a STALE API: the page asks for a facet the process has never heard of,
gets nothing back, and renders an empty filter that looks broken rather than out
of date.

How it behaves:

- Polls the mtimes of those files (and the built index and the requirements
  file) once a second. No watch dependency for a dozen files. This is serve.py's
  own watcher, not uvicorn's `--reload`, for the next point:
- **It refuses to restart into code that does not compile.** A typo mid-edit prints
  `reload held — does not compile: reads.py:112: invalid syntax` and the server
  keeps serving; fix the file and it restarts on the next poll.
- On change it stops uvicorn, which closes the listening socket, and re-execs with
  the same argv — so the pid is unchanged and the port is rebound. It does not
  reopen the browser.
- Every process has a `boot` id, returned in `/api/meta` and `meta.boot` of
  `/api/graph`. The page compares it on each poll and reloads itself when it
  changes, so an open tab picks up the new page without being touched.

If a facet ever renders "not sent by this server", that is this problem with
`--no-reload`, or a server predating the feature. Restart it.

## What the UI does with it

Two tabs over one dataset:

- **Map** — laid out around **you**: `meta.ego` names the person node the
  `owner` setting resolves to (empty if it names nobody, and the map falls back to
  the best-connected node). That node is pinned at the centre and every other node
  starts on a ring by its hop distance from it, so a hand-written force simulation
  only has small corrections to make — local repulsion, a pull back to each ring,
  degree-weakened springs, a per-frame step cap. It settles in a moment and then
  holds still; a poll places only nodes that are new, hovering a node freezes it,
  and a double-click on the background resets to the ring layout. Legend chips filter by **node type** and by
  **realm** and **kind**, independently, each with its own `all` reset; clicking a node shows
  its neighbourhood. High-volume types (`channel`, `capture`) are worth hiding to
  make the rest legible. On a writable server the toolbar's **+ idea** posts to
  `/api/propose-idea`, then refreshes and selects the new node once a read of
  `/api/graph` carries it — not when the write answers, since the canvas can only
  centre on a node it has placed.
- **Dashboard** — the date views, with a "Mine · needs action" toggle, one chip
  per assignee, one per realm, one per kind that has open tasks, and `blocked_by` / `blocks` / `related` badges from
  the edges. Cards expand on click (`/api/node`), and the date chip opens a picker
  that writes via `/api/set-due`.

### shade instead of hide

The filters pick **which** nodes fall out. This toggle picks what falling out
**looks like**, and the split matters because the two modes answer different
questions. Hiding answers *"get this out of my way"*: the node leaves the canvas,
drops out of the force simulation, and the layout closes the gap. Shading answers
*"show me just this"*: the node stays exactly where it is, dimmed to 20% with its
edges dimmed alongside it, and the graph keeps its shape so what you kept can be
read in context instead of floating alone.

One predicate feeds both, which is what keeps them honest:

```ts
// frontend/src/lib/graphFilter.ts
export function filteredOut(node, f, cascadeOff) {
  return directHidden(node, f) || cascadeOff.has(node.id);
}
export function isHidden(node, f, off) { return !f.shade && filteredOut(node, f, off); }
export function isShaded(node, f, off) { return  f.shade && filteredOut(node, f, off); }
```

It is a pure module with no React and no canvas in it, which is what makes the
property above testable rather than merely asserted:
`frontend/src/lib/graphFilter.test.ts` renders neither and checks that the two
modes reach exactly the same set.

Every filter — the three facet dropdowns, hide-done, and the cascade below —
computes `filteredOut` and nothing else, so none of them knows the mode exists and
flipping it can never change *which* nodes are affected, only how they are painted.

Two rules make shading readable rather than just dimmer:

- **A shaded node loses its label.** It keeps its dot and its edges, which is the
  shape it was kept for, but a few thousand dimmed labels over the handful you
  isolated is noise, not context. Selected, hovered and search-hit nodes keep theirs.
- **A search hit stays lit.** You asked for it by name; dimming it because a type
  filter also excluded it would read as *"no results"* rather than *"filtered out"*.

An edge is only as lit as its dimmer end — one shaded endpoint dims the edge —
or a shaded node would stay wired into the lit graph by full-strength lines and
read as part of it.

### drop unsupported

The map's other filter toggle turns the filters from *hiding* into *unwinding*. Normally
hiding a type or realm leaves its dependants floating with nothing attached. With
**drop unsupported** on, a node survives only while at least one edge still points
at it from something visible. A node nothing points at is a root, and only the
chips can hide it.

```
A -> B -> C -> D,  E -> C

hide A        B goes with it (A was its only support).
              C survives on E, so D does too.
hide A and E  B falls, then C, then D — the whole chain unwinds.
```

It runs on the filter, not on what is drawn, so shade mode reaches exactly the
same set — the unwound nodes are dimmed rather than removed. It runs to a
fixpoint, because order matters: C has to fall before D can. Cycles
hold each other up and so are never cascaded away on their own; a self-loop does
not count as support. The result is cached in `cascadeOff` and recomputed only
when a filter or the graph changes — never per frame. On an 830-node / 1393-edge
brain a full recompute is under a millisecond.

The chip's counter shows how many extra nodes the cascade took, so the toggle's
effect is legible rather than mysterious.

Every one of those interactions is SQL. Leaving the dashboard open costs nothing.

---

## Extending it

Reads are in `scripts/brain_api/reads.py`, writes in `writes.py`, their request
bodies in `models.py`. To add a write:

1. Add a request model to `models.py`, with a `description` on each field that
   is not obvious — it is what `/api/docs` shows.
2. Add a function to `writes.py` under `@router.post("/your-route", tags=[...],
   summary=...)`, taking `data: m.YourModel = body(m.YourModel)`. It is behind the
   `--no-write` check automatically.
3. Length-cap text with `clip(value, max_len)` and return `reply(result)`, which
   answers 200 when `result["ok"]` and 400 otherwise.
4. Go through a `graph.api_*` helper rather than raw SQL, so the neo4j outbox and
   FTS triggers stay correct.
5. **Add it to `POST_ROUTES` in `tests/test_api.py`**, with a check of what it
   does and what it refuses. The suite fails until you do: the document can only
   say a route exists, and this is where what it does is written down.

A read is the same in `reads.py`, registered ABOVE the `/{view}` route at the
bottom of the file — that one matches any single segment under `/api/`.

The dependencies are pinned in `scripts/requirements-server.txt`. Changing a pin
changes the venv's name, so the next start builds a fresh environment beside the
old one rather than upgrading a working one in place.

On the client side, add it to the named surface in
`frontend/src/api/client.ts` rather than calling `fetch` from a component. Every
call in the app goes through one `get`/`post` pair, so retry, error shaping, the
`--no-write` 403 and abort-on-unmount are decided once instead of at each of the
fifty call sites the old single-file page had.

That last point matters: writing straight to SQLite bypasses `_after_write()`,
which is what enqueues the neo4j sync. See [the neo4j backend](neo4j.md).

## The server's environment

`serve.py` needs FastAPI and uvicorn, and the promise is still one command with
no install step. So it owns its environment:

- The venv lives at `~/.cache/memory-ledger/server-<hash>` (or under
  `$XDG_CACHE_HOME`; on Windows, under `%LOCALAPPDATA%`). The hash covers `scripts/requirements-server.txt` and the
  Python version, since a venv is tied to both.
- The first start creates it with `python -m venv` and `pip install -r`, which
  **needs the network once**. It is built in a temp directory and renamed into
  place only after pip succeeded, so a half-installed venv is never used, and two
  servers starting at once cannot both write into it.
- Every start then re-execs `serve.py` under that venv's interpreter.
- `$MEMORY_LEDGER_VENV=/path/to/venv` uses a venv of your own instead, as it is:
  nothing is installed into it. Use that on a machine with no network, or to pin
  the server into an environment you already manage.
- `rm -rf ~/.cache/memory-ledger` is always safe; the next start rebuilds it.

On Debian and Ubuntu, `python -m venv` needs the `python3-venv` package; the
server says so if it is missing.
