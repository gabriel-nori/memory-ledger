# Triggers — summoning the brain from Slack, Jira and GitHub

Mention the brain in a Slack thread or a Jira comment and it answers there, as
you, without anyone opening Claude Code and pasting a link. On GitHub a mention
of you works the same way, and so do the pull request changes you pick: a
review on your PR, a review request, failing checks, a merge. Jira and Slack have
changes of their own to pick (an issue moving status or changing hands, the bot
added to a channel), and Slack takes **commands**: `@brain deploy {staging, v1.2}`.
Each trigger either starts a Claude session, calls an API, or runs one of your
scripts. Everything runs on your own machine. Nothing is exposed to the
internet, and no bot ever speaks.

A **meeting** can too: say a wake phrase ("Brain will handle this") and the
task starts as soon as the brain reads the transcript, `source: meeting`. See
[Wake phrases](#wake-phrases).

Another brain can reach this one too: a [B2B](b2b.md) message from a peer brain
is polled from the relay and logged and run as a request here, `source: b2b`.

This page is the design and the reference. Setup is in
[Setting it up](#setting-it-up); the moving parts are in
[How it works](#how-it-works).

## The shape

```
Slack (Socket Mode) ──┐                                 ┌─► your MCPs (Slack, Jira, Gmail, Drive)
  app_mention events  │                                 │   authenticated as YOU
                      ├─► trigger engine ─► claude -p ──┤
Jira (HTTP polling) ──┤   (inside serve.py)  headless,  └─► the `respond` skill:
  comments w/ marker      │                  one per        rules → act → reply → sign
GitHub (HTTP polling) ┘   │                  event
  mentions, PR changes    │
                          │                  mention
                          ▼
                  graph.db: request log, actions, gates, checkpoints
```

Four decisions carry the whole design:

1. **The bot is an ear, never a mouth.** The Slack app has no `chat:write`; the
   Jira poller reads with your token and writes nothing on its own. Whatever gets
   said is said by a headless Claude Code session through **your** MCP servers,
   so it lands under your name, signed so nobody mistakes it for you typing.
2. **There is no trigger MCP.** The watcher only notices the mention and runs
   `claude -p --mcp-config <yours>`. The MCPs you already authenticated do the
   work, so no integration's auth is solved twice.
3. **One mention, one stateless session.** No shared session between mentions:
   each one is its own `claude -p` with its own context and its own audit row.
   Concurrency is a thread pool, capped.
4. **Every mention is logged before anything happens.** A request id (UUID) is
   minted the moment a mention is noticed, and every action taken for it is
   appended under that id. "I never asked for that" has an answer.

## Who may summon it — the gate

A conversation is **closed until you open it**.

| Who mentions | Where | What happens |
|---|---|---|
| you (the owner) | anywhere | runs, and that conversation is **enabled** from then on |
| someone else | an enabled conversation | runs |
| someone else | not enabled | **refused**: nothing runs, and one short notice says so |
| someone else, in a DM with you (`slack.dms`) | a DM you opened with your own tag, or any DM with `slack.dms_open` | runs, and answers in the DM as you |
| Slack: someone on `allowed_users` | anywhere | runs, and opens nothing for anyone else |
| Jira: someone on the people list | any issue, with the marker and a mention of you | runs, and opens nothing for anyone else |
| Slack: someone on `blocked_users` | anywhere, open channels included | **refused** with the not-allowed notice. The owner is never blocked |
| Slack: someone else's command | wherever the gate lets them in | runs only if the command is marked `anyone`; else **refused** with the not-allowed notice |
| a change you switched on (GitHub, Jira, Slack) | anywhere the token sees | runs, **as a subscription**. It opens nothing for anyone else, even when you made the change |

A conversation is a Slack thread (`slack:<channel>:<thread_ts>`), a Jira issue
(`jira:<KEY>`), a GitHub pull request or issue (`github:<owner>/<repo>#<n>`), or a
**whole Slack channel**. Threads and issues are opened by
your own mention and live in `trigger_gate`. Whole channels are a list you keep
by hand, in `trigger_channel`: an open channel runs everyone's mentions in every
thread. Jira's equivalent is per person, not per issue: `trigger_person` lists
the colleagues whose marked mentions of you run on any issue (Settings → Jira →
*People who may tag the brain*, a switch each, or the **Allow** button on their
refused task in Tasks; `trigger.py people --add <accountId>` from the CLI). All
of them are in `graph.db`, so they survive restarts.

**Deciding costs nothing.** Who may summon the brain is decided by the engine
from the gate, the lists and the command's `anyone` flag, before any session
exists. A refusal never reaches a model (see `refuse_without_model` below), so
someone the brain turns away cannot spend on it. A command the owner runs does
not open the thread: it is not a conversation.

**The channel list is history, not just state.** Closing a channel keeps its row,
marked with who closed it and when, and reopening it restores that same row. In
the UI it is the **Open channels** list in the Slack panel, folded until you
click it, with **Close** on each row, a field to open one by id or by pasting a
link, and a **Closed** list to reopen from.

```bash
python3 scripts/trigger.py channels                                  # the list
python3 scripts/trigger.py channels --add https://…/archives/C0123ABCD --name ops
python3 scripts/trigger.py channels --remove C0123ABCD
python3 scripts/trigger.py enable  --scope jira:PROJ-42              # a thread or issue
```

## Saying no — three notices

| Notice | When | Sent by |
|---|---|---|
| **not enabled here** (`refusals.not_enabled`) | someone mentions the brain in a conversation you have not opened | the engine, before any session. Once per conversation, so a colleague hammering the bot does not fill the thread. The request is `refused` |
| **not allowed** (`refusals.not_allowed`) | someone on `slack.blocked_users`, or someone else running a command only you may run | the engine, like the one above, once per conversation. The request is `refused`, with `blocked` or `owner-only command` as its reason |
| **against a rule** (`refusals.policy`) | the session reads the request and finds it breaks one of your rules or guardrails | the session, through `trigger.py decline`, which records the rule and reason and prints the notice. `{reason}` is a one-line why, safe to show. The request is `declined` |

**They go out as you, never as the bot**, and end with the signature.

**A refusal makes no model call** (`refuse_without_model`, on by default). The
engine's two notices go out exactly as written, through the source's own API:

- **On Slack, with a user token** (`xoxp-…`), the engine posts it itself.
- **On Slack without one**, there is no way to post as you without a model, so
  the refusal is **only logged** (`refusal-notice-skipped`), never posted.
- **On Jira**, it is a comment made with the Jira token; **on GitHub**, one made
  with the GitHub token, when `github.refusal_notice` is on.

Switched off, the behaviour of the first version comes back: each notice is
translated first (below), and on Slack without a user token it goes out through
**your Slack MCP**, in a short headless session whose only job is to post that one
message. You still post it, and it costs a model call.

**With `refuse_without_model` off, every notice is in the sender's language.**
The message that mentioned the brain decides, not the channel or the person:
someone writing in Hebrew gets Hebrew, and the same person writing in English
the next day gets English. (The policy notice is posted by the session, which
translates it either way: that request already passed the gate.)

- With a user token, and on Jira, a small tool-less model (`claude.notice_model`,
  default `haiku`) rewrites the template first.
- Without a user token, the MCP session does it on its way.
- Measured on 2026-09-23 with `haiku`: about 6 s and $0.004 per notice. Hebrew
  and Italian were translated, and an English message left the English template
  alone. Names, @mentions and links stay as written.
- `translate_notices: false` sends the templates exactly as written, and then a
  refusal with a user token costs no model call at all.
- Notice and translation calls run with the brain's capture **off**
  (`MEMORY_LEDGER_OFF=1`). They are plumbing, not conversations. The `respond`
  session itself is captured like any other.
- **The signature is never translated.** It is the loop guard, and a translated
  one would not be recognised.

**Can't reach the channel? The requester gets a DM.** When posting in the thread
fails because you cannot reach that channel (you are not in it, or it is not
found, archived or restricted), the notice goes to the person who mentioned the
brain as a DM instead, with a link to their message. It is logged as
`refusal-notice-dm`. The `respond` skill does the same with its answers
(`reply-dm`). Any other failure, a rate limit say, is logged as a failure rather
than turned into a DM. Jira has no DM, so a comment that cannot be posted is
logged as failed.

## Run commands

Every trigger has a **run command**: what its session is told to do when it
fires, in your words, with variables in braces where a value belongs.

| Trigger | Where the run command lives | Empty means |
|---|---|---|
| a Slack mention | `slack.events.mention.do` (`slack.prompt` until one is saved) | read the thread, do what it asks, reply there |
| a marked Jira comment | `jira.events.mention.do` (`jira.prompt` until one is saved) | read the issue, do what it asks, comment on it |
| each Slack, Jira or GitHub event | `<source>.events.<kind>.do`; your own marked GitHub comment uses `mention`'s | the event's default instruction (for `mention`: none) |
| a Slack command | `slack.commands[].do` | read the thread and do what the command asks |

A run command is what a **Claude** action is told. An event or command can
instead call an API or run a script ([Actions](#actions)); those use the same
variables, in their URL, headers, body or arguments.

The run command is kept on the request when it fires (`meta.do`, `meta.action`),
so the audit shows what ran even after the setting changes.

```json
{"slack": {"prompt": "{trigger_source} sent this task. Pick it up and reply to {trigger_user} in the thread at {trigger_link}."},
 "github": {"events": {"review_requested": {"on": true,
   "do": "{trigger_user} asked for my review on {github_repo}#{github_number} ({trigger_title}). Add a task to my brain with two lines on what it changes."}}}}
```

Settings shows the list at the top of Triggers, and each run command field
previews itself with example values. `trigger.py variables` prints the list;
`trigger.py variables --request <id>` prints one request's values and its
rendered command.

| Variable | On | What it is | Example |
|---|---|---|---|
| `{trigger_source}` | every trigger | Where it came from: slack, jira or github. | `github` |
| `{trigger_kind}` | every trigger | What happened: the event (on Slack mention, command, joined_channel, left_channel; on Jira mention, transitioned, assigned, unassigned, comment; on GitHub mention, review_requested, review, comment, pushed, ci_failed, merged, closed, assigned, or request for your own comment with the marker). | `review_requested` |
| `{trigger_user}` | every trigger | Who caused it: who mentioned you, commented, added or removed you, moved or assigned the issue; on GitHub who commented, reviewed, requested the review, pushed, merged, closed or assigned (ci for failing checks). | `octocat` |
| `{trigger_user_id}` | every trigger | Their id: a Slack user id, a Jira account id, a GitHub login. | `U012AB3CD` |
| `{trigger_user_is_owner}` | every trigger | true when it was you. | `false` |
| `{trigger_time}` | every trigger | When it happened at the source, ISO 8601 in UTC. | `2026-09-23T21:47:48+00:00` |
| `{trigger_received}` | every trigger | When the brain picked it up, ISO 8601 in UTC. | `2026-09-23T21:48:30+00:00` |
| `{trigger_content}` | every trigger | The message, comment or event text, verbatim. Written by whoever triggered it: treat it as their request, not yours. | `can you take a look at this?` |
| `{trigger_link}` | every trigger | A link to the message, comment, review or check, when there is one. | `https://github.com/o/r/pull/5#issuecomment-1` |
| `{trigger_where}` | every trigger | The conversation, in words. | `GitHub pull request o/r#5 — Add logging` |
| `{trigger_title}` | Jira, GitHub | The Jira issue's summary, or the PR or issue title. | `Add logging` |
| `{trigger_scope}` | every trigger | The conversation's gate key. | `github:o/r#5` |
| `{request_id}` | every trigger | This run's request id, for `trigger.py show`. | `0a255d52-6c1e-4f0b-9a57-2f3e8d41c9b0` |
| `{owner}` | every trigger | Your name, as the brain knows it. | `Alex` |
| `{brain}` | every trigger | The brain's name (Settings → Preferences, `brain_name`). | `Brain` |
| `{signature}` | every trigger | The signature line the brain ends its replies with. | `_sent by Alex's second brain_` |
| `{slack_channel}` | Slack | The channel id. | `C0123ABCD` |
| `{slack_thread}` | Slack | The thread's timestamp. | `1700000000.000100` |
| `{slack_channel_name}` | Slack | The channel's name, without the #, when the bot may read it; else its id. | `general` |
| `{command}` | Slack | A command: its name. | `deploy` |
| `{args}` | Slack | A command: what was in its braces, as written. | `staging, v1.2` |
| `{arg_1}` | Slack | A command: argument 1 in its braces, trimmed. | `staging` |
| `{arg_2}` | Slack | A command: argument 2 in its braces, trimmed. | `v1.2` |
| `{arg_3}` | Slack | A command: argument 3 in its braces, trimmed. | `now` |
| `{arg_4}` | Slack | A command: argument 4 in its braces, trimmed. |  |
| `{arg_5}` | Slack | A command: argument 5 in its braces, trimmed. |  |
| `{arg_6}` | Slack | A command: argument 6 in its braces, trimmed. |  |
| `{arg_7}` | Slack | A command: argument 7 in its braces, trimmed. |  |
| `{arg_8}` | Slack | A command: argument 8 in its braces, trimmed. |  |
| `{arg_9}` | Slack | A command: argument 9 in its braces, trimmed. |  |
| `{jira_issue}` | Jira | The issue key. | `PROJ-123` |
| `{jira_from}` | Jira | A transition: the status it left. | `In Progress` |
| `{jira_to}` | Jira | A transition: the status it reached. | `In Review` |
| `{jira_assignee}` | Jira | An assignment: who it is assigned to now; empty when unassigned. | `Ana Lima` |
| `{jira_assignee_id}` | Jira | Their account id. | `712020:abcd` |
| `{jira_previous_assignee}` | Jira | An assignment: who had it before; empty when nobody did. | `Bo Chen` |
| `{github_repo}` | GitHub | owner/name. | `acme/widgets` |
| `{github_number}` | GitHub | The PR or issue number. | `5` |
| `{github_item}` | GitHub | pull request or issue. | `pull request` |

**How it is filled in.** Each `{name}` in the list is replaced by its value;
a source-only one is empty on the other sources, and an event-only one (a
transition's `{jira_from}`, a command's `{arg_1}`) is empty on the others. A
command's pattern adds its named groups: `(?P<env>\w+)` is `{env}`, in that
command only. Anything else in braces is
left as written, so a command needs no escaping, but a `{word}` that looks
like a variable and is not one is refused on save, so a typo is caught before
anything runs.

**What it does not change.** The rendered command goes into the session's
prompt as `instructions`, and where it and the defaults disagree (where to
reply, whether to post), it wins. The rest of the prompt is always there: the
request id, the message quoted verbatim as the request, how to log actions, how
to decline, the signature, and the brain's rules and guardrails, which no run
command overrides. `{trigger_content}` and `{trigger_user}` are written by whoever
triggered it: put them in a command as their words, not as yours.

## Events

Each source lists what it notices, each with its own switch and what it does
when it happens: `<source>.events.<kind> = {on, do, action, agent, http, script}` (`agent`: the
[connector](agents.md#choosing-the-agent) a Claude action runs on, empty for the default). A
switched-on event is a **subscription**: the switch is the permission, so it
runs whoever caused it (you included: an event you fire is never ignored), and
it opens no conversation. Its session is told it is not answering anyone, gets
`<source>_event: <kind>`, and posts on that source only if its run command says
to. GitHub's events are in [GitHub — polling](#github--polling).

**Slack**

| Kind | When | Variables of its own | On by default |
|---|---|---|---|
| `mention` | someone mentions the bot. Always on: the gate decides | | yes |
| `joined_channel` | the bot is added to a channel (`member_joined_channel`) | `{slack_channel_name}`; `{trigger_user}` is who added it | no |
| `left_channel` | the bot is removed from one (`member_left_channel`) | `{slack_channel_name}` | no |

The two channel events arrive live only: Slack's history has nothing reliable to
catch up from after a restart. They need the app to subscribe to those bot
events (below). Being added does **not** open the channel; a run command can do
that on purpose with `trigger.py channels --add {slack_channel}`.

**Jira** — the changes count on the issues `jira.watch_jql` picks (default:
assigned to you, reported by you, or watched by you), ANDed with `jira.jql`.

| Kind | When | Variables of its own |
|---|---|---|
| `mention` | a comment carries the marker and mentions you, or is yours. Always on | |
| `transitioned` | the status changes | `{jira_from}`, `{jira_to}` |
| `assigned` | the assignee changes to someone (anyone, not only you) | `{jira_assignee}`, `{jira_assignee_id}`, `{jira_previous_assignee}` |
| `unassigned` | the assignee is cleared | `{jira_previous_assignee}` |
| `comment` | a new comment that is not a mention. Your own plain comments do not count: they are conversation | |

All four change kinds are off by default. `{trigger_user}` is who made the change.

## Commands

A Slack mention can carry a command: `@brain deploy {staging, v1.2}`. Commands
are a list in `slack.commands`:

```json
{"slack": {"commands": [
  {"name": "deploy", "action": "script", "script": {"name": "deploy.sh"}},
  {"name": "ticket", "pattern": "^ticket (?P<key>[A-Z]+-\\d+)", "anyone": true,
   "action": "http", "http": {"method": "POST", "url": "https://hooks.example/t/{key}",
                              "body": "{\"who\": \"{trigger_user}\"}"}},
  {"name": "rota", "anyone": true, "do": "Tell {trigger_user} who is on call for {arg_1}."}
]}}
```

- **Matching.** The message, with the bot's mention taken out, is matched
  against each switched-on command's `pattern` in order, case-blind, and the first
  that matches runs **instead of** the mention's handling. With no `pattern`, the
  name as the first word: `deploy` matches `deploy {x}`, not `deployment`.
- **Arguments.** The first `{...}` in the message is `{args}` as written, and
  split on commas and trimmed, `{arg_1}` … `{arg_9}`. A pattern's named groups
  are variables of that command. A group named like a variable is refused on
  save.
- **Who.** The same gate as a mention, and more: a command runs for **you
  only**, unless it is marked `anyone`, and then for whoever the gate lets in
  (an open thread or channel, an allowed user). Anyone else gets the not-allowed
  notice. Running a command never opens the thread.
- The session, for a Claude command, gets `slack_event: command`, `command:` and
  `command_args:` lines, and replies in the thread as for a mention.

## Wake phrases

Say it in a meeting and it is done: "**Brain will handle this**, send them the v2
deck." Nothing listens live. When the brain reads the meeting's transcript or
notes, it scans them for your **wake phrases**, and each one said starts a task
**at once**, without asking: `source: meeting`, `meeting_event: phrase`.

```json
{"meeting": {"enabled": true, "phrases": [
  {"phrase": "{brain} will handle"},
  {"phrase": "{brain} take note", "anyone": true,
   "do": "Add a note to the brain: {phrase_rest} (said by {phrase_speaker} in {meeting_title})"}
]}}
```

- **The name.** `{brain}` is the brain's name, the `brain_name` setting
  (Settings → Preferences; `$MEMORY_LEDGER_BRAIN_NAME` outranks it; default
  `Brain`). `{owner}` works too. Renaming the brain renames every phrase. The
  name is also `{brain}` in a signature, a notice or a run command, and the session
  is told it (`you_are`).
- **Matching.** Word by word, case- and accent-blind, whatever punctuation is
  between the words: `{brain} will handle` matches "Brain, will handle this" and
  "BRAIN will handle it", not "brain will handled". At least two words, so naming
  the brain alone fires nothing. One match per line: when several phrases fit one
  sentence ("{brain} handle this" and "I'll let {brain} handle this"), it is one
  ask, and the phrase with the most words takes it.
- **The transcript.** One turn per line, `Name: what they said` (Meet's format;
  a `[00:12]` timestamp in front is fine, and so is the Markdown a Gemini export
  writes, `**Name:** what they said`). A line with no name carries on the last
  speaker's turn, and a heading or rule (`### 00:12:00`, `-----`) ends it. A
  document with notes above a `# Transcript` heading (a "Notes by Gemini" Doc) is
  read from that heading on, so the summary is never taken for anyone's words.
- **Who.** You, as the transcript names you: the `owner` setting, in full or by
  first name ("Alex" is "Alex Doe"), or a name passed with `--me`. A phrase marked
  `anyone` runs for whoever says it. Anyone else's is logged **`refused`** and runs
  nothing, with no notice, as there is nobody to send one to.
- **Once.** A line already scanned is not run again: the request is keyed on the
  meeting (`--key`, else the link, else the title) and the line. Scanning the
  Gemini email and then the Doc, or re-ingesting, starts nothing twice.
- **What it does.** Each phrase has an action like any trigger (Claude, an API
  call, a script). The default run command says: work out from the lines around
  it what was put on the brain, do it now, and if it is not clear, add a task for
  you rather than guess. The session gets the line (`{phrase_said}`), what came
  after the phrase (`{phrase_rest}`), the speaker (`{phrase_speaker}`), the six
  lines before and two after (`{phrase_context}`, also in the prompt between
  `<<<CONTEXT` fences), and `{meeting_title}`. There is no thread: it posts
  nowhere unless the run command says to.

**Where scans come from.** The `ingest-context` skill passes every meeting it
ingests (a "Notes by Gemini" email, a Meet transcript Doc, pasted notes) to
`trigger.py scan`, which logs and starts each task in a process of its own and
returns at once. From anywhere else:

```bash
python3 scripts/trigger.py scan --file notes.txt --title "Acme sync" --link https://docs.google.com/… --dry-run
python3 scripts/trigger.py scan --file notes.txt --title "Acme sync" --me "Alex Doe"
pbpaste | python3 scripts/trigger.py scan --title "1:1"
```

`--dry-run` says what matches and whether it would run, logging nothing; `--wait`
runs each task here, to the end. Through the server it is `POST /api/trigger-scan`,
which runs the tasks on the trigger pool; Settings → Triggers → **Meetings** holds
the switch, the phrases, and a box to try a transcript. Both the master switch and
`meeting.enabled` must be on.

## Actions

What an event or a command does:

| `action` | Does | Costs a model call |
|---|---|---|
| `claude` (default) | the headless session below, told the run command (`do`) | yes |
| `http` | one HTTP request: `http.method`, `http.url`, `http.headers`, `http.body` | no |
| `script` | runs a file in `<home>/scripts`: `script.name`, `script.args` | no |

- **An API call** fills the variables in, escaped for where each lands:
  percent-encoded in the URL, JSON-escaped inside a JSON body (a body starting
  with `{` or `[`, or a JSON `Content-Type`), one line in a header. A value can
  fill a field; it cannot add one. The URL must start with `http(s)://` and a
  host, so a variable cannot choose where the request goes. A status of 400 or
  more is `failed`; the status and the start of the answer are the request's
  result. Headers are stored in `trigger.json`, mode 600, like the tokens.
- **A script** is linked by name from `<home>/scripts`
  ([CONVENTIONS §5c](../CONVENTIONS.md)), or written there from the page with
  **New script…** (`POST /api/create-script`, `graph.py create-script`). It runs
  as you, from `claude.cwd` or the brain home, with the session timeout. There
  is **no shell in between**: each of `script.args` is one argv item with the
  variables filled in, and with none, a command passes its own arguments, in
  order. Every variable is also in its environment as `BRAIN_<NAME>`
  (`BRAIN_TRIGGER_USER`, `BRAIN_ARG_1`, …), the command's arguments as
  `BRAIN_ARGS_JSON`, and `MEMORY_LEDGER_REQUEST_ID` plus
  `MEMORY_LEDGER_TRIGGER_SCRIPT` (the path of `trigger.py`) so it can log what it
  did: `python3 "$MEMORY_LEDGER_TRIGGER_SCRIPT" action --kind … --detail …`. A
  file that is not executable runs through its language's interpreter: this
  Python for a `.py`, the `bash` on PATH (Git Bash on Windows) for a shell
  script, `pwsh` or `powershell -File` for a `.ps1`, `cmd /c` for a `.bat` on
  Windows, and node, ruby or perl from PATH. On Windows every script goes
  through its interpreter, since only an `.exe` runs by itself there. Exit 0 is `done`; its output is the result.
- Both log an action on the request (`http` with the status, `script` with the
  exit code and argv), and a switched-on one that could not run (no URL, a
  script that is gone) is refused on save and reported by **Check**.

## Custom triggers

A trigger for a service the brain has no watcher for: a poll you write as a
short list of blocks. One file per trigger, `<home>/triggers/<id>.yaml`, the
file name being the `id`. Every event it emits becomes a task like any other,
in the same log, with the same dedupe.

```yaml
id: site-health
name: Site health
enabled: false          # trigger.py custom enable site-health
mode: poll              # the only mode in v1
interval: 5m            # 30s, 5m, 1h, 1d; at least 30s
do: "Tell me on Slack that {event_title}, with the payload."
config:
  url: https://example.com/health
secrets: [STATUS_TOKEN]  # names only; the values are in trigger.json
limits: {timeout_s: 30, memory_mb: 256, network: true}
retry: {attempts: 2, backoff_s: 5}
blocks:
  - id: check
    type: http_request
    url: "{{config.url}}"
    headers: {Authorization: "Bearer {{secrets.STATUS_TOKEN}}"}
    on_error: continue          # a failed call leaves prev.status empty
  - id: down
    type: condition
    if: "prev.status == None or prev.status >= 400"
    then: continue
    else: stop
  - id: alert
    type: emit
    event:
      id: "site-health:{{prev.status}}"   # stable: the same outage is one task
      title: "site down: {{prev.status}}"
      payload: {status: "{{prev.status}}"}
```

| Block | Does | `prev` after it |
|---|---|---|
| `http_request` | `method` (GET), `url`, `headers`, `body` (a mapping is sent as JSON), `timeout_s` (≤ 30). `on_error: continue` turns a failed call into `prev.status: None` instead of an error | `{status, ok, body, json}` |
| `condition` | `if`: comparisons, `and`/`or`/`not`, `in`, literals and paths. `then`/`else`: `continue` or `stop` (default continue / stop) | unchanged |
| `transform` | `pick` (keep these fields), `drop`, then `set` (field: value) | the new mapping |
| `script` | a Python file in `<home>/scripts`, run in the same isolated process | what it sets as `result` |
| `emit` | `event: {id, title, payload, permalink}`; with `each: "{{prev.items}}"`, one event per item, as `item`; `checkpoint:` moves the cursor | unchanged |
| `stop` | ends the run | |

- **References** are `{{path}}`: `prev`, `config`, `checkpoint`, `now`,
  `secrets.NAME` (a declared one) and, inside an `emit` with `each`, `item`.
  `a.b[0].c` walks; a missing field is empty, never an error. A string that is
  one reference and nothing else keeps the value's type. There is no `eval`:
  a condition is parsed with `ast` against a short list of allowed nodes, so a
  call, a lambda or a `__dunder__` is refused before anything runs.
- **A script block** sees `prev`, `config`, `checkpoint`, `now` and `secrets`
  as globals. It answers by setting `result`, may rebind `checkpoint`, and may
  set `stop = True`. What it prints goes to the run's stderr.
- **The checkpoint** is one opaque string per trigger (`custom:<id>` in
  `trigger_checkpoint`), set by an `emit`'s `checkpoint:` or a script. It is
  empty on the first run, and the trigger decides what that means: process the
  past, or start from now. It moves only **after** the run's tasks are logged,
  so a crash in between re-finds the same events, and `(source, event_id)`
  makes the second pass a no-op.
- **The task** is a request on `custom:<id>`, `reason: subscribed: custom`: you
  chose it by enabling the trigger, so no gate is asked, and it opens nothing
  for anyone. Its run command is the trigger's `do`, with four more variables:
  `{trigger_id}`, `{run_id}`, `{event_title}` and `{event_payload}` (JSON). The
  session is shown the payload fenced as data, never as instructions.

**Isolation.** The blocks run in a child, `python -I -X utf8
scripts/_trigger_block_runner.py`, with an empty environment (a `PATH`, and on
Windows what Python needs to start): nothing of the server's environment, no
token, reaches it. The context goes in on stdin, one JSON answer comes out on
stdout.

| Limit | Default | Held by |
|---|---|---|
| `timeout_s` 1–600 | 60 | the whole process tree is killed; the run is `error` |
| output | 1 MB | more is `error` |
| `memory_mb` 32–2048 | 256 | `RLIMIT_AS` on Linux. **Not enforced on Windows or macOS** (best effort; `validate` says so) |
| `network: false` | true | refuses `http_request`. **A script block can still open a socket**: Python has no sandbox for that; `validate` says so |
| events per run | 20 | `custom.max_events_per_run`; more is `error`, and nothing is created |
| `retry.attempts` 1–5 | 1 | each attempt is in the run's log |

**Secrets** live in `trigger.json`, under `custom.secrets` (`{"NAME":
"value"}`), mode 600 like the other tokens. The trigger file names them; the
child gets them on stdin only; and every value is replaced with `***` in
whatever comes back, the run log, the tasks and the CLI's output included.

```bash
python3 scripts/trigger.py custom list                 # every trigger, with its last run
python3 scripts/trigger.py custom show site-health     # the file, its state and checkpoint
python3 scripts/trigger.py custom validate site-health # exit 1 when it has errors
python3 scripts/trigger.py custom dry-run site-health  # runs the blocks; writes nothing
python3 scripts/trigger.py custom run site-health      # one real run, tasks run here
python3 scripts/trigger.py custom enable site-health   # rewrites only `enabled:`
python3 scripts/trigger.py custom runs site-health --limit 5
```

**Scheduling.** With `enabled` and `custom.enabled` on, the server runs a
scheduler (`_custom_loop` in `brain_api/trigger_runtime.py`) that wakes every
15 seconds and runs each trigger whose file says `enabled: true` and whose
`interval` has passed since its last run, one after the other. The files are
read on every pass, so an edit or an `enable` counts without a restart; a file
that does not parse is marked `invalid` in its state and skipped, and the
others still run. A trigger never runs twice at once in the server: a run by
hand from the page waits its turn (`busy`). A `custom run` from the CLI is
another process, and if it overlaps a scheduled run the dedupe keeps the
events to one task each. The scheduler's state is `runtime.custom` in
`GET /api/trigger`.

**Settings → Custom triggers** lists them with their last run and an on/off
box each, and opens a file in an editor: the YAML itself, the same format the
CLI and the skill write. **Validate** checks the text without saving, **Save**
refuses what does not validate, **Dry run** runs the saved file and creates
nothing, and the runs and the checkpoint are under it. A new trigger starts
from a template. Secret values are never shown, only their names.

The **`custom-trigger-builder`** skill writes a trigger with you: it shows the
YAML with one sentence per block, validates it, runs it dry, and saves it
switched off.

`run` needs `enabled` and `custom.enabled` in `trigger.json`; `dry-run` and
`validate` work with them off. YAML needs PyYAML (in the server's venv,
`scripts/requirements-server.txt`); on a Python without it, a JSON-shaped
trigger still loads, since JSON is YAML. Each run is a row in
`custom_trigger_run` (status, attempts, events found, tasks created, the
per-block log); `custom_trigger_state` keeps the last one per trigger.

## Loops

The brain answers as you, and on Jira the mention *is* your own account. So its
own answer would look like a new mention unless something stops it. Two things do:

- **Anything carrying the signature is ignored.** The signature is the one line
  the brain always adds and a person never types.
- **Every event is seen once.** `trigger_request` is unique on
  `(source, event_id)`; a mention delivered live and again by catch-up, or seen by
  two overlapping polls, runs once.

## How it works

### Slack — Socket Mode

A Slack app in **Socket Mode** opens an outbound websocket, so there is no public
endpoint. It subscribes to `app_mention` and listens while the server runs.

- **Scopes (bot):** `app_mentions:read`, `channels:history`, `groups:history`,
  and `channels:read` + `groups:read` for catch-up to list where it is (and for
  a channel's name); `users:read` is optional and only turns a requester's id
  into a name. **No `chat:write`.** An app-level token with `connections:write`
  opens the socket.
- **Bot events:** `app_mention`; add `member_joined_channel` and
  `member_left_channel` for the two channel events.
- **Live:** each `app_mention` is acked, then handed to the engine; so is a
  `member_joined_channel` or `member_left_channel` whose member is the bot.
- **Catch-up:** on every (re)connect, for each channel the bot is in (or only
  `slack.channels`), `conversations.history` reaching back
  `thread_lookback_days` (default 7) before the checkpoint, and
  `conversations.replies` for any thread whose latest reply is newer than it.
  Mentions inside threads are the common case, and a reply in a week-old thread
  does not show up in `history` from the checkpoint alone. Messages that do not
  contain `<@bot>` are skipped.
- **The checkpoint** (`trigger_checkpoint`) follows the clock while the socket
  is up, trailing it by two minutes, since then every mention arrives live. It
  only ever moves forward. A first run has no checkpoint and starts from now: it
  does not answer a backlog nobody expected it to.
- **Catch-up reads every channel the bot is in**, not only the open ones. Your
  own mention counts anywhere, and someone else's in a closed conversation still
  has to be logged and refused.

This half needs `slack_sdk` for the websocket, so it lives in the server
(`scripts/brain_api/trigger_runtime.py`) with the server's other dependencies.
Catch-up and the refusal notice are plain HTTPS and stay in the stdlib engine.

### Slack — your direct messages

People tag the brain in a chat **with you**: `@brain what's the status of the
export?` in your DM, and it answers there, as you, signed. The bot can never be a
member of a DM between two people, so Slack sends it nothing for one; these are
heard through **your user token** instead, as events "on behalf of" you.

- **Switch:** `slack.dms` (Settings: *Listen in your direct messages*). Needs the
  user token, plus in the Slack app: the **user** scopes `im:history` and
  `mpim:history` (and `chat:write` for refusal notices), and under **Event
  Subscriptions → Subscribe to events on behalf of users**, `message.im` and
  `message.mpim`. Reinstall the app after adding them.
- **What counts:** a message in one of your DMs or group DMs that tags the bot.
  Slack writes a tag outside the bot's channels as `<@U…|name>`; both forms
  count. Every other message in your DMs is ignored and never stored.
- **The gate:** a 1:1 DM is **one** conversation (`slack:D…`), since people rarely
  thread there. Your own tag in a DM with Ana opens that DM to Ana's tags from
  then on. `slack.dms_open` opens every DM with you at once, which lets anyone in
  the workspace start a session as you by messaging you; blocked people are still
  refused. A group DM keeps threads, like a channel.
- **The reply** goes in the DM as a new message, not a thread, unless the tag was
  in one.
- **Live only:** catch-up does not read your DMs, so a tag sent while the server
  was down is not answered.

### Jira — polling

There is no Jira bot yet (creating one is heavier; it is the obvious next step).
So the mention is **of you**, plus a **text marker** that says "this one is for
the brain" — e.g. `@Your Name @brain can you move this to review?`.

- Every `poll_seconds` (default 60) the poller searches
  `updated >= -<N>m` (relative, so the Jira profile's timezone cannot shift it),
  narrowed by your own `jql` if you set one, and reads each issue's newest
  comments back to the checkpoint. They are handled oldest first, as they were
  written, so your comment that opens an issue counts before a colleague's reply
  that follows it in the same pass. Only new comments count; editing an old one
  to add the marker does not.
- A comment is a mention when it contains the marker (compared without case or
  accents, so `@cérebro` matches `@Cerebro`) **and** either mentions your account
  or is yours.
- **Changes**, when a change kind is on: a second search,
  `updated >= -<N>m AND (<watch_jql>) [AND (<jql>)]`, with the changelog
  (`expand: changelog`, or `/issue/<key>/changelog` when the search does not carry
  it). Each status or assignee item newer than the window is an event, and so is
  each new comment that is not a mention. They are handled oldest first.
- The checkpoint is the time the last successful poll started; the next poll
  reaches back to it plus a margin. Missed a day with the laptop shut? The first
  poll reaches back to the checkpoint, capped at `max_catchup_hours`.

It is plain HTTPS with an API token, so it is stdlib and can also run without the
server: `python3 scripts/trigger.py poll-jira` does one pass (useful from cron).
A connection that breaks mid-response (Jira's chunked bodies are sometimes cut
off) is read again once for a GET, which changes nothing; a second break, or any
break on a write, is an error the caller reports, never a crash and never a
comment posted twice.

### GitHub — polling

GitHub is polled like Jira, with a token you add in Settings. There is no app
and no webhook, so nothing needs a public endpoint.

**What it notices.** Each kind is its own switch, and each carries the
instruction the session gets when it happens (`do`). Change both in
Settings → Triggers → GitHub.

| Kind | When | On by default |
|---|---|---|
| `mention` | someone writes `@you` in a comment, a review, a line comment or a description, on a PR or an issue | yes |
| `review_requested` | you are asked to review a PR, or add yourself as a reviewer | yes |
| `review` | someone reviews **your** PR (approved, changes requested, commented) | yes |
| `comment` | any new comment on your PR, mention or not | no |
| `pushed` | new commits on a PR you are involved in but did not open; one event per push | no |
| `ci_failed` | checks fail on your open PR; one event per head commit, however many checks fail. `cancelled` and `skipped` do not count | no |
| `merged` | your PR is merged | no |
| `closed` | your PR or issue is closed unmerged, by anyone, you included | no |
| `assigned` | you are assigned a PR or an issue, by someone or by yourself | no |

**The default instructions only write to the brain.** They add a task or a note
("add a task to my brain listing each change asked for") and say *post nothing
on GitHub*. An answer on GitHub is something you ask for by rewriting the
instruction, never a default. A `mention` has no default instruction: the
comment is the request, and it is answered with a comment, like a Jira mention.

**Your own comments.** A comment of yours carrying the marker (default
`/brain`, which is not an `@word`, so nobody gets pinged) runs, and it opens
that PR to everyone's mentions, like your mention in a Slack thread does. Your
comments without it are ignored.

**Who counts: `trusted_associations`.** A switch runs for comments from
`OWNER`, `MEMBER` and `COLLABORATOR` by default, the `author_association` GitHub
gives every comment. A stranger on a public repo who mentions you does not get a
session with your MCPs. Their mention goes to the gate like any other: it runs
in a PR you opened, and is refused elsewhere. For a change with no comment of
its own (a review request, a push), the PR author's association decides,
unless you made the change: what you fire yourself, adding yourself as a
reviewer, assigning yourself, closing your own PR, always runs, since it means
you have to act on it.

**Refusing is quiet.** On GitHub a refusal would be a public comment, and people
mention each other there all day for human reasons. So a refused GitHub mention
is logged and nothing is posted, unless `github.refusal_notice` is on. Then the
not-enabled notice goes out as a comment, once per PR, as with Jira.

**How a pass works:**

- Every `poll_seconds` (default 120) it searches `involves:<you> updated:>=<since>`,
  plus `review-requested:<you>` when that kind is on and `reviewed-by:<you>` when
  `pushed` is. `repos` narrows all of them: `owner/name` for one repo, a bare
  name for a whole org.
- For each hit it reads the timeline (`/issues/<n>/timeline`), which has the
  comments, reviews, line comments, requests, commits, merges and closes, each
  with its time. Only what happened since the checkpoint counts.
- `ci_failed` reads your open PRs every pass (up to 30), whatever the window,
  because a check finishing does not change a PR's `updated`. For each it reads
  the head commit's check runs.
- Everything found is handled oldest first, across PRs. The checkpoint and the
  catch-up cap (`max_catchup_hours`) work as they do for Jira, and so does the
  two-minute margin. `(source, event_id)` keeps an overlapping poll from running
  anything twice.
- The brain's own signed comments are ignored, so its answers never trigger it.

A subscription's session is told that it is not answering anyone. It gets
`github_event: <kind>` and `instructions`, its event's `do` with the variables
filled in ([Run commands](#run-commands)), and posts on GitHub only if the
instruction says to. It reads the PR with `gh` or a GitHub MCP, **with the
session's own GitHub auth**, not the polling token, so the polling token can
stay read-only.

GitHub Enterprise: set `api_url` to `https://<host>/api/v3`.

`python3 scripts/trigger.py poll-github` does one pass without the server.

### The headless session

Each allowed mention runs:

```bash
claude -p --output-format json --dangerously-skip-permissions \
       --session-id <uuid> [--mcp-config=<your config>] [--strict-mcp-config] [extra args] \
       < <the request>
```

The prompt goes in on stdin, never on the command line: Windows caps a command
line at about 32 KB, and a prompt with quotes and newlines passed through
`claude.cmd` is re-read by `cmd.exe` on the way. `claude` is the configured
`claude.bin`, else the first `claude` on PATH (`claude.cmd` or `claude.exe` on
Windows). The same holds for a notice and its translation.

`--dangerously-skip-permissions` is what makes it run end to end with no one at a
prompt. The prompt carries the three things the session cannot work without:

- **the text** of the mention,
- **the permalink** to that exact message or comment — the session reads the
  thread or issue from there with its own MCP, instead of being handed a channel,
- **the source** (`slack`, `jira` or `github`), which decides the reply
  convention: a thread reply, or a comment on the issue or PR. A GitHub
  subscription has none by default: it does what its instruction says.

It also carries the request id, in the prompt and as `$MEMORY_LEDGER_REQUEST_ID`,
tells the session to use the [`respond`](../skills/respond/SKILL.md) skill, to
answer in the language of the message, to DM the requester when it cannot post
in place, and how to decline a request that breaks a rule.
That skill is the contract: read the rules that apply, do what was asked, log
every outward action against the request id, reply in place, end with the
signature.

The session id is chosen before the session starts and stored on the request
with the full command and its working directory, so the session can be followed
while it runs. The UI's **Tasks** section does that, from two places. One is the
database, where the brain's own hooks write the conversation, every subagent and
its outcome, and each captured turn, all keyed by that session id; the actions the
session logs land there as well. The other is Claude Code's transcript for the
session, which holds every tool call with its input and result. A request from
before session ids were kept is matched to its transcript by the request id in
its prompt, and the id is stored once it is found.

The dispatcher records the exit code, the final text, stderr and the cost on the
request. A session that outlives `timeout_s` is killed and marked `timeout`; a
server restart that orphans one marks it `interrupted` on the next start.

A session or script runs as the leader of its own process group, and a kill
(timeout or cancel) takes the whole tree: `killpg` on macOS and Linux,
`taskkill /T /F` on Windows, so the MCP servers and tools `claude` started do
not outlive it. Being in its own group, it no longer gets the terminal's
Ctrl-C; the server kills every tree it still holds when it exits instead
(`_platform.track`).

### The audit log

Two tables in `graph.db`, and one node per request:

| Where | What |
|---|---|
| `trigger_request` | one row per mention or GitHub event noticed: `id` (UUID), `source`, `event_id`, `scope`, requester id and name, permalink, text, `received_at`, `status` (`queued` → `running` → `done` / `declined` / `failed` / `timeout` / `interrupted`, or `refused`, or `blocked` for a B2B message from a brain off the allowed list, or `cancelled` by the owner), exit code, result, stderr, cost, and what ran: `session_id`, `command` (JSON argv; for an `http` action only the method and URL, never the headers) and `cwd` |
| `trigger_action` | append-only: every action taken for a request — `kind` (`reply`, `reply-dm`, `declined`, `refusal-notice`, `refusal-notice-dm`, `jira-transition`, …), `target`, `detail`, `at` |
| `trigger_channel` | the Slack channels open to everyone, with who opened and closed each and when. Closed ones are kept |
| `trigger_person` | the Jira people who may tag the brain on any issue, keyed by `(source, user_id)`, with who allowed and took off each and when. Switched-off ones are kept |
| a `request` node | the same request in the graph, so `recall "who asked me to …"` finds it, and it mirrors to neo4j like any other node |

Refused mentions are logged too. A mention that was never logged never ran.

Read it:

```bash
python3 scripts/trigger.py status               # watchers, config, counts
python3 scripts/trigger.py requests --limit 20
python3 scripts/trigger.py show --id <request-id>
```

or `GET /api/trigger` and `GET /api/trigger-request?id=` on a running server, or
`GET /api/tasks` and `GET /api/task?id=` for what the Tasks section shows.

Each task keeps its cost, `cost_usd`, as claude reports it at the end of the
session. A session killed by the timeout, or one that crashed, never reports
one, so its cost is estimated from the token usage in its transcript and its
subagents' transcripts, at the list prices in `MODEL_PRICES` in
`scripts/trigger.py`, and flagged `cost_estimated`. The Tasks card shows it as
`~$1.23`. A failed or timed-out task from before the estimate existed gets one
the first time the Tasks section lists it.

### Reprocessing a task

A task that went wrong (`failed`, `timeout`, `interrupted`, `cancelled`), a B2B message
that the allowed list stopped (`blocked`), or a mention the gate turned away
(`refused`) has a **Reprocess** button on its Tasks card. A blocked or refused
one asks first, because running it means you vouch for someone the gate did not
let in. A refused Jira mention also offers **Allow <name> to tag the brain**,
which puts its author on the people list so the next one needs no vouching. Reprocess puts the request back to `queued` and
runs it right away. It runs on the trigger pool, or on its own thread when the
watchers are off, with the action the request was stamped with when it came
in. The last run's session, result, exit code, stderr and cost are cleared.
The actions it logged stay, and a `reprocess` action records who asked and
what the status was before. `done`, `declined` and live requests
cannot be reprocessed.

```bash
python3 scripts/trigger.py reprocess --request <id>         # queue it again
python3 scripts/trigger.py reprocess --request <id> --run   # and run it here, now
```

or `POST /api/trigger-reprocess {request_id}`.

### Cancelling a task

A `queued` or `running` task has a **Cancel** button on its Tasks card
(`POST /api/trigger-cancel {request_id}`). The request turns `cancelled` at once
and a `cancel` action is logged. A running session or script checks its row
every second (`CANCEL_POLL_S`) and is killed when it sees the cancel, so this
reaches a run in another process too (a meeting scan's detached run). An HTTP
call already sent is left to finish, but its answer is not kept. A cancelled
task can be reprocessed.

### The server log

The audit log is the record. The server log is what you watch: one line per
event, on the server's stderr and in `<home>/logs/trigger.log` (rotated at 2 MB,
three kept).

```
2026-09-23 18:39:06 INFO    trigger  triggers running — sources: slack, jira (poll 60s); up to 3 sessions at once; log: ~/.second_brain/logs/trigger.log
2026-09-23 18:39:07 INFO    trigger  slack: connected
2026-09-23 18:39:07 INFO    trigger  slack: checking data from 2026-09-23 18:31:02Z (catch-up)
2026-09-23 18:39:07 INFO    trigger  jira: checking data from 2026-09-23 18:36:04Z (updated >= -4m ORDER BY updated ASC)
2026-09-23 18:39:40 INFO    trigger  new request 0a255d52-6c1e-4f0b-9a57-2f3e8d41c9b0  slack slack:C1:650.1 from Ana (owner): 'summarise this'  -> queued (owner)
2026-09-23 18:39:40 INFO    trigger  task 0a255d52-6c1e-4f0b-9a57-2f3e8d41c9b0 started  slack slack:C1:650.1 from Ana
2026-09-23 18:40:52 INFO    trigger  task 0a255d52-6c1e-4f0b-9a57-2f3e8d41c9b0 done  in 72s, exit 0, $0.14
2026-09-23 18:41:03 INFO    trigger  new request aa92e015-3b7d-4e62-8f10-5c9a2d7e6b41  slack slack:C9:650.2 from Bo: 'hi'  -> refused: conversation not enabled, notice sent
```

It logs the state on every start, restart and stop (or why the triggers are not
running), each new request with its decision, the start and end of each task
(the headless session), a watcher that changes state or hits a new error, and
every poll pass: `<source>: checking data from <time>`, the start of the window
it reads, so you can see each poll run on its schedule. A mention seen before is
not logged.
The id is the request id, so `trigger.py show --id` takes it as it is. The
CLI logs nothing: only the server attaches the log.

## Setting it up

All of it is **per person** and lives in the brain home, never in this repo:
`<home>/trigger.json`, written mode 600 because it may hold tokens.

**The easy way is the web UI: Settings → Triggers.** It has a master switch, and
one panel each for Slack, Jira, GitHub and Claude. The Slack, Jira and GitHub
panels list every event kind, each with its switch and its action; the Slack
panel also has the allowed and blocked lists and the commands. **Everything
folds**: each panel by its title, each events list and command list by its
heading, and each switched-on event or command down to one line saying what it
does. What is folded is remembered in the browser. Each panel has its own on/off switch,
its fields, a **Save** and a **Check** that proves the stored tokens against the
real service without starting anything. Saving restarts the watchers on the new
file, so a switch flipped there takes effect at once. Tokens are write-only: the
page shows whether one is stored, and whether the environment has taken it over,
never what it is. A blank token field keeps the stored one, and **Remove**
deletes it. Behind the page are `POST /api/trigger-config` and
`POST /api/trigger-check` ([the API](api.md)).

The file itself, for editing by hand or scripting:

```json
{
  "enabled": true,
  "signature": "_sent by {owner}'s second brain_",
  "refusals": {
    "not_enabled": "I'm not enabled in this conversation, so I did nothing. {owner} can enable me by mentioning me here.",
    "policy": "I can't do that: it goes against a rule {owner} has set for me, so I did nothing. {reason}",
    "not_allowed": "You can't ask me that here, so I did nothing. Only {owner} can change who may."
  },
  "refuse_without_model": true,
  "translate_notices": true,
  "claude": {
    "bin": "claude",
    "notice_model": "haiku",
    "mcp_config": "~/.second_brain/trigger-mcp.json",
    "strict_mcp_config": false,
    "timeout_s": 900,
    "max_concurrent": 3,
    "extra_args": []
  },
  "slack": {
    "enabled": true,
    "owner_user_id": "U00000000",
    "thread_lookback_days": 7,
    "dms": true,
    "dms_open": false,
    "allowed_users": ["U0FRIEND1"],
    "blocked_users": [],
    "events": {
      "mention": {"do": "Read the thread, do what it asks, and reply there."},
      "joined_channel": {"on": true, "action": "script", "script": {"name": "joined.sh", "args": ["{slack_channel}"]}}
    },
    "commands": [
      {"name": "deploy", "action": "script", "script": {"name": "deploy.sh"}}
    ]
  },
  "jira": {
    "enabled": true,
    "base_url": "https://your-site.atlassian.net",
    "email": "you@example.com",
    "owner_account_id": "",
    "marker": "@brain",
    "poll_seconds": 60,
    "jql": "",
    "watch_jql": "assignee = currentUser() OR reporter = currentUser() OR watcher = currentUser()",
    "max_catchup_hours": 72,
    "events": {
      "transitioned": {"on": true, "do": "Note that {jira_issue} moved from {jira_from} to {jira_to}."},
      "assigned": {"on": true, "action": "http", "http": {"method": "POST", "url": "https://hooks.example/assigned", "body": "{\"issue\": \"{jira_issue}\", \"to\": \"{jira_assignee}\"}"}}
    }
  },
  "github": {
    "enabled": true,
    "api_url": "https://api.github.com",
    "login": "",
    "marker": "/brain",
    "poll_seconds": 120,
    "repos": ["acme/api", "acme"],
    "trusted_associations": ["OWNER", "MEMBER", "COLLABORATOR"],
    "refusal_notice": false,
    "max_catchup_hours": 72,
    "events": {
      "review": {"on": true, "do": "Add a task to my brain listing each change asked for."},
      "ci_failed": {"on": true, "do": "Read the failing log and add a task with the fix."}
    }
  }
}
```

Secrets are read from the environment first, then from the same file. A token
in the environment of the process running `serve.py` outranks anything saved
from the page, and the page says so:

| Secret | Env var | Key in the file |
|---|---|---|
| Slack app-level token (`xapp-…`, opens the socket) | `MEMORY_LEDGER_SLACK_APP_TOKEN` | `slack.app_token` |
| Slack bot token (`xoxb-…`, reads only) | `MEMORY_LEDGER_SLACK_BOT_TOKEN` | `slack.bot_token` |
| Slack user token (`xoxp-…`, **optional**: posts refusal notices directly; without it they go through your Slack MCP, or with `refuse_without_model` are only logged. **Required for `slack.dms`**, with `im:history` and `mpim:history`). Needs `chat:write` and `im:write` for the DM fallback | `MEMORY_LEDGER_SLACK_USER_TOKEN` | `slack.user_token` |
| Jira API token | `MEMORY_LEDGER_JIRA_TOKEN` | `jira.token` |
| GitHub token (`github_pat_…` fine-grained, or `ghp_…` classic). Read: Pull requests, Issues, Checks, Metadata (classic: `repo`). Write to issues and PRs only for `refusal_notice` | `MEMORY_LEDGER_GITHUB_TOKEN` | `github.token` |

- `signature` — `{owner}` is the brain's `owner` setting. Keep it distinctive: it
  is also the loop guard.
- `refusals` — the three notices; see [Saying no](#saying-no--three-notices).
- `refuse_without_model` — a refusal is posted as written through the source's
  API, or only logged, never through a model (default on).
- `translate_notices` — with `refuse_without_model` off, send notices in the
  sender's language (default on).
- `slack.allowed_users` / `slack.blocked_users` — Slack user ids; see
  [the gate](#who-may-summon-it--the-gate).
- `slack.events`, `jira.events` — only the kinds you name change; see
  [Events](#events). `slack.commands` is replaced whole on save; see
  [Commands](#commands). Each carries an [action](#actions).
- `jira.watch_jql` — the issues a change counts on.
- `claude.notice_model` — the model that translates a notice (default `haiku`).
- Open channels are **not** in this file: they are a list in `graph.db`, edited
  from the UI or `trigger.py channels`.
- `jira.owner_account_id` — left empty, it is looked up once (`/myself`).
- `jira.jql` — ANDed onto the search, e.g. `project in (PROJ, OPS)`.
- `github.login` — left empty, it is looked up once (`/user`).
- `github.events` — only the kinds you name change; the rest keep their
  defaults. See [GitHub — polling](#github--polling).
- `claude.mcp_config` — the MCP servers the session may use. Empty means the
  ones Claude Code would load anyway.

Then:

1. Create the Slack app (Socket Mode on, the scopes above, `app_mention` event
   subscription), install it to the workspace — that may need a workspace admin.
2. Invite it to the channels where it should listen.
3. For GitHub, create a token (fine-grained, read-only is enough; see the
   secrets table), paste it in Settings → Triggers → GitHub, and switch on the
   events you want.
4. **Check** each service in Settings → Triggers, or
   `python3 scripts/trigger.py check [--only slack|jira|github|claude]`. It validates
   the file, calls Slack, Jira and GitHub with the tokens, and runs `claude --version`,
   without starting anything.
5. `python3 scripts/serve.py` — the watchers start with the server.
   `--no-trigger` keeps them off; `--no-write` does too, since a read-only server
   must not be acting on your behalf.

## Risks, and what holds them

- **A mention is untrusted input to a session that skips permissions.** In an
  enabled conversation, anyone there can ask. The gate keeps it to conversations
  you opened, the `respond` skill treats the message as a *request* rather than
  as instructions that outrank the brain's rules, and personal material never
  crosses to an outward surface without your say-so
  (a rule of your own that says so, in a brain that has one).
  Open channels deliberately, not by habit.
- **On GitHub, text from strangers.** A public repo lets anyone comment, and a
  PR description or a review is input to the session like a mention is.
  `trusted_associations` keeps the switches to members and collaborators, the
  gate keeps everyone else to PRs you opened, and the default instructions post
  nothing on GitHub.
- **It acts while you are away.** That is the point; the audit log is what makes
  it answerable afterwards.
- **Tokens on disk.** Prefer the env vars; the file is mode 600 either way. So
  are an API action's headers.
- **Scripts and API calls act with no session to judge them.** That is their
  point, and why they are yours to aim: a command runs for you only unless you
  mark it `anyone`, arguments reach a script as argv items with no shell, and
  values are escaped where they land in a request. What the script does with a
  stranger's argument is the script's business, so treat `{trigger_content}`
  and `{arg_N}` as untrusted input in it.
- **A custom trigger fetches text from anywhere.** What it finds reaches the
  session as a payload fenced as data, and the task it starts is never the
  owner's (`requester_is_owner: false`). An `http_request` may call any URL,
  local and private addresses included: only you write the file, so that is
  documented rather than blocked. Each event is a session, which is why the
  interval has a floor and a run a ceiling on events.
- **A custom trigger's script is your code, run unattended.** It runs only from
  `<home>/scripts`, in a separate process with an empty environment, a timeout
  and an output cap; on Linux a memory cap too. It is not a sandbox: it can
  read your files and open a socket.
- **A refusal costs nothing.** With `refuse_without_model` on, someone the brain
  turns away never starts a model, however often they try.

## Not done yet

- GitHub webhooks. Polling is slower (a minute or two), but it needs no public
  endpoint.
- GitHub team review requests: only a request for you by name counts.
- A dedicated Jira bot user, so a mention does not have to be of you.
- A full Requests view in the web UI. Settings → Triggers lists only the newest
  five; `trigger.py requests` and `show` have the rest.
- DMs to the bot (`message.im`) — today only `app_mention` in channels.
- Catch-up for the Slack channel events and your DMs: they are live only.
- Allowed and blocked lists for Jira and GitHub; only Slack has them.
- Custom triggers: webhooks, branching, loops, parallel blocks, languages other
  than Python in a script block, and switching a failing trigger off by itself.
