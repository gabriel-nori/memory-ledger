# Agents

The **Agents** tab is a chat with a model from inside the brain UI: Claude
through the `claude` CLI, or any model on an Ollama host. The code is
`scripts/agents.py`, the routes are `/api/agent*`, and the UI is
`frontend/src/features/agents/`.

These are the only routes on the server that start a model call. Every other
endpoint stays token-free, and so does reading a thread.

## A turn is one stateless call

No session carries over from one turn to the next. Each reply is one fresh call,
and it is given:

1. **The message.**
2. **The last N exchanges** of this conversation. N is the conversation's
   `context_turns`, from 0 to 10, and defaults to 1. With 1, a reply like "yes,
   do it" still knows what "it" is.
3. **What `recall` returns for the message.** That is the same spreading-activation
   ranking as `graph.py recall` (`graph.api_recall`), capped by `recall_limit` and
   `recall_budget`. Bookkeeping types (conversation, agent, capture, …) are dropped.

Each finished exchange goes back into the graph as a `message` node, linked
`part-of` the conversation's own `conversation` node. The next turn's recall
finds earlier turns like any other memory, and so does every other session on
any machine. The exchanges already carried in step 2 are left out of recall, so
nothing appears twice.

Claude gets the turn's prompt on stdin and its system prompt from a temporary
file (`--append-system-prompt-file`, removed when the turn ends), so neither is
capped by the command line (about 32 KB on Windows) nor re-read by
`claude.cmd`'s shell. `claude` is the connector's `bin`, else the first one on
PATH.

The system prompt also names the conversation: its id, its graph node, and how
to read the whole thread back when the owner refers to something the carried
exchanges do not cover. Claude runs `python3 scripts/agents.py thread <id>
[--limit N]`; Ollama calls its `read_thread` tool. Both print the thread oldest
first, the reply being written left out.

Because each turn is stateless, switching the **connector or model** mid-conversation
costs nothing. In the composer they are one button: it opens the connectors, and
pointing at one (or tapping it) opens its models to the side. The next reply is written by whichever one is picked, and every
reply records its own `connector` and `model`.

## Badges

The list shows what each conversation's latest reply is doing:

| Badge | State | Meaning |
|---|---|---|
| spinner | `pending` | queued or running. **Cancel**, on the reply, stops it |
| `?` | `question` | the agent is waiting on you |
| ✓ | `done` | answered |
| ✕ | `error` | failed. **Retry** reruns it with the conversation's current connector |

The agent decides `question` itself. The system prompt, and a reminder at the end
of the prompt, tell it to make the last line of its reply exactly `[[needs-answer]]`
when it cannot go on without you. The marker is stripped before the text is stored.

## Suggested next message

The prompt also asks the agent to end every reply with a line of its own, `[[next: <one short sentence, in your voice>]]`,
suggesting what you are most likely to say next. When it asked you a question, the suggestion is the answer it expects, and
`[[needs-answer]]` stays the very last line. The agent leaves it out when nothing obvious follows.

The marker is stripped wherever it appears in the reply, and from recalled context, so a model cannot copy an old
suggestion. The sentence is stored on the reply as `next_action` and offered as a suggestion in the composer once the
reply is done.

## Tools: read-only or on, per conversation

The composer's **Read-only** box is ticked by default. Untick it to turn tools on
from the next turn; the reply then carries a *tools on* badge.

Read-only limits what the agent does **outside** the brain. The brain itself is
read and written in both modes, without asking: that is the offload rule
(CONVENTIONS §0), and a reply that skipped it would leave nothing behind.

| | Read-only (default) | Tools on |
|---|---|---|
| Claude | `--tools=Read,Grep,Glob,WebSearch,WebFetch,Skill,Bash`, `--permission-mode dontAsk`. `--allowedTools` holds those tools, the read-only connector tools (`CLAUDE_READ_MCP`: Jira and Confluence reads, Slack, Gmail, Calendar and Drive reads, Claude Docs `read`, Sentry searches, and `ToolSearch` to load them), and Bash for the brain's own commands only (`graph.py`, `brain_query.py`, `agents.py thread`). No other Bash, no edits, nothing that posts | `--dangerously-skip-permissions`, like a trigger session |
| Ollama | `recall`, `search`, `get_node`, `read_thread`, `add_note`, `add_task` | the same: Ollama has no tools outside the brain |

The system prompt gives Claude those commands with their absolute paths:
`match-rules` before acting, `recall`, `search`, `get-node` and `brain_query.py open --mine` to look
things up, and `add-node` and `add-edge` to store what the turn produced. They
must run as written, not after a `cd`, or the allow rule does not match.

The plugin's own hooks run in these sessions too, since the `claude` CLI loads them in
`-p` mode: the prompt and answer captures, and the SessionStart bootstrap.

A claude.ai artifact link cannot be read here. The `claude` CLI has no artifact
tool in `-p` mode, whatever the tools mode, and Claude Docs `read` refuses an HTML
artifact. Read it from an interactive Claude Code session.

Ollama tools run inside the server, against the graph. A model that does not
support tools (Ollama answers *does not support tools*) is asked again without
them. The reply then logs a `no tools` note and answers from the recalled context.

The write routes are unauthenticated (see [api.md](api.md)). A server started
with `--no-write` refuses `agent-send` like any other POST.

## The log, the cost

Each reply logs every tool call, skill, subagent, MCP call and thinking block,
with input and output clipped to 2,000 characters. In the UI, what the agent
says while it works shows as its own message, as it comes; the tool calls
between those fold shut. The tab fills the window and the chat scrolls on its
own, with no heading above it (the search sits just under the ☰ button): it follows new messages while you are at the bottom, and stays put when
you scrolled up to read. Claude's events come from `--output-format stream-json --verbose`,
and the row is updated about twice a second while the reply runs, so the log
and the **cost** move live. While a reply runs, its cost is estimated from the
streamed token usage at list price (`trigger.MODEL_PRICES`), marked `≈`. When it
finishes, Claude's own `total_cost_usd` replaces the estimate. Ollama is local:
$0, with tokens counted.

## The chat header

At the top right of a conversation:

- **Search**: filters the thread to the messages whose text, error or tool log
  match.
- **Cost**: the conversation's running total. Hover it for the tokens.
- **Export**: **PDF** opens a print view to save, **Text** is the messages as
  plain text, and **JSON** is everything, tool logs included. All three are built
  in the browser.
- **Hide**: moves the conversation to the folded **Hidden** group under the active
  list. **Unhide** brings it back. Nothing is deleted.

## Connectors

`<home>/agents.json`, edited in **Settings → Agents**:

```json
{
  "default_connector": "claude",
  "context_turns": 1,
  "recall_limit": 8,
  "recall_budget": 2600,
  "max_concurrent": 2,
  "connectors": [
    {"id": "claude", "name": "Claude", "kind": "claude", "enabled": true, "bin": "claude",
     "models": ["claude-opus-5-5", "claude-sonnet-5-5"], "default_model": "claude-sonnet-5-5", "timeout_s": 900},
    {"id": "ollama", "name": "Ollama", "kind": "ollama", "enabled": true,
     "url": "http://127.0.0.1:11434", "default_model": "", "timeout_s": 600, "max_rounds": 8}
  ]
}
```

A connector is one backend. Two Ollama hosts are two connectors. An Ollama
connector's models are whatever its host has pulled (`/api/tags`), asked for
each time the tab loads. To add a new `kind` (an OpenAI-compatible API, say),
add it to `KINDS`, give `_clean_connector` its fields, and register a
`run_<kind>(con, reply, connector, turn)` in `RUNNERS` that returns
`{text, events, tokens, cost_usd, cost_estimated}`.

## Slash commands

Type `/` as the first character of a message and a popup lists the skills and
plugin commands, fuzzy-searched by name and description (`GET /api/agent-commands`,
read from disk, no model call). Arrow keys move, Enter or Tab inserts
`/name ` into the box, Esc closes it. The agent's own Claude session expands it,
so there is no new execution path. A `/` later in the text opens nothing.

Commands come from the enabled plugins (`plugin:name`), `~/.claude/skills` and the
agent's `.claude/skills`. The **Plugins** button lists every installed plugin with
a checkbox: it edits that plugin's key under `enabledPlugins` in
`~/.claude/settings.json` (`POST /api/agent-plugin`), so it changes every Claude
session, from the next message on.

`writes` is a guess from the name and description (send, post, create, merge...),
not a declaration. While the conversation is read-only those commands are greyed
out; untick Read-only to use them. `python3 scripts/agent_commands.py` prints the
same list.

## Running

Replies run on threads in the server process, at most `max_concurrent` at once,
oldest first. When the server starts, any reply still marked `running` belonged to
a process that is gone. It becomes an `error` ("interrupted"), one click from
Retry, and the queue starts again.

While a reply runs, a thread writes `beat_at` on its row every 10s. On startup
`recover()` leaves alone any running reply with a beat under 30s old, even when its
pid is not visible to this process (another server, a sandbox); the pid check stays
as the second guard. Only a reply with a stale beat and a dead pid is failed.

**Cancel** on a queued or running reply (`POST /api/agent-cancel {id}`) fails it
at once as `cancelled by you`, so Retry runs it again. A running Claude session is
killed; an Ollama chat stops before its next round, and whatever the call in
flight returns is dropped. A cancelled reply writes no `message` node.

A Claude session runs in its own process group, and a cancel or a timeout kills
the whole tree (`killpg` on macOS and Linux, `taskkill /T /F` on Windows), so
the MCP servers and tools it started go with it. Whether a reply's server is
still alive is asked with `_platform.pid_alive`, never `os.kill(pid, 0)`, which
on Windows would terminate it.

```bash
python3 scripts/agents.py status          # connectors, models, what is running
python3 scripts/agents.py run <reply-id>  # run one queued reply in this process
python3 -m pytest tests/test_agents.py   # fake claude + fake Ollama, no model call
```

Storage is two tables in the brain's database, `agent_conv` and `agent_msg`. The
graph holds the memory (`conversation` and `message` nodes). The tables hold the
UI's view: status, event log, cost, tokens.

## Syncing chats across machines

Off by default. With it on, `agent_conv` and `agent_msg` ride along with the
Neo4j mirror (see [neo4j.md](neo4j.md)), so a conversation started on one
machine shows up on another. Turn it on in **Settings → Sync Agents chats across
machines** (the `agents_sync` setting), or set `AGENTS_SYNC=1` in the server's
environment. The variable wins over the setting: the switch shows locked, with
the source of the value (`default`, `settings` or `env`) beside it.

- **Local stays local.** The unsent `draft` (and `draft_at`) and a reply's
  `owner_pid` never leave the machine. A pid means nothing anywhere else.
- **No collisions.** Ids are unique per message and a message is only appended
  to, so two machines cannot overwrite each other.
- **No rollback.** A reply this machine finished or is running is never put back
  to `queued` or `running` by a pull.
- **Who runs a reply.** With sync on, a `running` reply may belong to another
  machine. It is claimable only once its heartbeat is older than 30s
  (`BEAT_STALE_S`), and "still in flight" is decided by that heartbeat, not by
  the pid. Two machines can still pick up the same `queued` reply at the same
  moment; closing that would take a lock in Neo4j.

## Markdown and documents in the chat

The agent's replies are read as Markdown: headings, lists, quotes, tables and
rules, on top of the inline marks (`frontend/src/lib/markdown.ts`, which hands
every block's text to the safe inline reader of `lib/richText`). Your own
messages stay plain rich text. Nothing becomes HTML: a reply can only ever be
text, links and the cards below.

A headless run cannot write an artifact, so it writes a **document** instead.
The `document-html`, `document-md`, `document-pdf` and `document-txt` skills pipe
the content into `scripts/make_document.py`, which saves it under
`<home>/exports/documents/` and prints a line of its own:

    [Q3 report](doc:documents/20261007-1030-q3-report.html)

On a line of its own, that link shows as a card: the name, an **Expand**
button and a **Download** button (saves the file as is). Expanded, the file overlays the interface; the cross at the top right,
Escape, or a click outside closes it. Markdown is rendered, text is shown as is,
HTML and PDF show in a frame (HTML sandboxed: it can run its own script, but
cannot reach this server's API).

- `GET /api/document?path=&area=` reads one (name, kind, size, text). `area` is `exports` (default) or `attachments`.
- `GET /api/document-file?path=&area=` serves the raw file for the frame or the image.

Only files under `<home>/exports`, of a known type (md, txt, html, pdf, plus log,
csv and json as text) and under 5 MB; anything else, a `..`, an absolute path or
a symlink out is refused (`scripts/documents.py`). Each skill reads the owner's
`Document style - <format>` note, so how documents look is customized once and
remembered, or per request.

## Attaching files

The paperclip in the composer, a drop on the composer, or a paste (a screenshot,
copied files) adds a file to the chat. Each is saved at once under
`<home>/attachments/` (`POST /api/agent-attach`, base64, any type, up to 25 MB)
and waits as a chip above the box, with a thumbnail for an image and a cross to
take it back. Nothing is sent until the owner sends: the message then ends with
one `[name](attach:<path>)` line per file (`attachments` on `/api/agent-send`).

The agent is given each file's place on disk next to its name, so one with file
tools opens it (an image is read as an image). In the chat an image attachment
shows as a thumbnail that opens in the viewer; any other file is a card with
Expand (text, Markdown, PDF) or Download. An agent can show an image the same
way with `[name](doc:path.png)` for a file under `exports/`, or `attach:`.

## The draft

What is typed in the composer is kept per conversation. Every keystroke goes to
`localStorage` (`ml.agent-draft.<conversationId>`) with a timestamp; about 800 ms
later the same text and timestamp go to the brain (`draft`, `draft_at` on
`/api/agent-conversation`). The newest edit wins both ways: a newer brain draft
replaces the local one, a newer local one is pushed up, and the server ignores a
write older than what it holds. Sending clears the draft in both places. A new
conversation that does not exist yet keeps only a local draft.

Texts from subagents show as bubbles like the agent's own; only their thinking
stays folded in the tool log. The last text is dropped only when it equals the
final reply exactly.

## Voice

Dictation, reading aloud and a hands-free conversation mode, in the chat and in
**Settings → Voice** (saved to `<home>/voice.json`). Everything is off until asked:
the microphone opens only on a click, speech defaults off, narration defaults to
today's behaviour.

**Dictation.** The 🎤 button opens `WS /api/voice-stt` and streams the microphone as
PCM16 16 kHz mono (an `AudioWorklet`, `voice/worklet.ts`). The server cuts segments on
pauses (`voice.Segmenter`: an energy threshold that follows the room's noise floor, so
silence is never sent to Whisper) and answers `partial` (the open segment, replaced as
it grows) and `final` (a closed one). The text goes into the box at once. **Stop** keeps
it; **Cancel** takes back only what that session inserted (`voice/insert.ts` finds it
again wherever typing moved it). One voice session per user: a second tab is refused.
A dropped socket is reopened once, then the session ends as a Stop.

Messages: client `start {language: "pt"|"auto"}`, binary frames, `stop`, `cancel`;
server `ready`, `partial`, `final`, `language_detected`, `stopped`, `error`.

**STT runs outside the server's environment.** `scripts/stt_worker.py` is a child process
under a Python that has `faster-whisper` (`$BRAIN_VOICE_PYTHON`, else the first that can
import it of: the server's own Python, `py -3` on Windows, `python3`/`python` on PATH, and
the usual `/usr/bin`, Homebrew and `/usr/local` ones), so the server's venv gains only `websockets`. The model is
`$BRAIN_STT_MODEL` (default `medium`; `$BRAIN_STT_DEVICE`, `$BRAIN_STT_COMPUTE` for a GPU),
loaded on the first session and kept. The first load downloads the model: it needs the
network once. A load failure comes back as an `error` on the socket and in Settings.

**Speech.** `POST /api/voice-speak` cleans the block (markdown, code, URLs, long paths,
emoji out; nothing left is a 204), picks the language (a stopword vote per block, the
default when unsure) and the voice, and answers a WAV. The engine is a `TTSEngine`
(`scripts/voice.py`). Shipped: `say` (macOS, + ffmpeg for the WAV), `sapi` (the Windows
voices, through PowerShell's System.Speech), `espeak-ng` (Linux, or `espeak`) and `piper`
(`pip install piper-tts` and a voice, on any OS). `$BRAIN_TTS_ENGINE` names one; otherwise
the first that can speak here wins, in the platform's order (`voice.CHAINS`: macOS
say → piper → espeak-ng, Windows sapi → piper, Linux espeak-ng → piper), and when none can,
Settings shows what the first one needs. Another engine is one more subclass in `ENGINES`. The browser keeps **one global FIFO
queue** (`voice/speech.ts`): each block is synthesised the moment it is queued, so the
next is ready when the current one ends. **Stop speaking** cuts the audio and clears the
queue and never touches the agent. By default only the open chat speaks; with
**Read chats that are not on screen** the others join the same queue, announced by name
when the chat changes, and a short "finished" follows their turn. Opening a chat never reads
its history: a block is spoken only if it arrived after the chat was first seen.

**Conversation mode.** Listening → after `silence_s` (2 to 15, default 5) without new words
and with text, the message is sent and the microphone shut → waiting while the agent works
and the voice reads → listening again only when the turn ended **and** the queue is empty.
Never an empty message. `turn_end` is derived, not pushed: `conversation.turn_end` on
`/api/agent-thread` is `{id, status: ok|error|cancelled}` from the latest reply, or null
while it runs, so an error or a cancel also hands the microphone back. Needs an existing
conversation (a new chat becomes one on its first send; dictation works before that).

**Narration.** `verbosity` (`default`, `verbose`, `verbose_tts`) is global in `voice.json`
and overridden per chat (`/api/agent-conversation`). `verbose_tts` narrates only while
speech is on for that chat. The effective one adds a short instruction to the agent's
system prompt (`voice.verbosity_instruction`).

**Translation.** With *Translate every block* on, each block is translated into the target
language before it shows and before it is spoken (`POST /api/voice-translate`: one Claude
Haiku call per block, names, commands and identifiers kept; skipped when the block is
already in the target). The original is behind an "original" expander. It is the only voice
feature that calls a model, so it is off by default.
