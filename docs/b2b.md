# B2B: Brain 2 Brain

One brain leaves a message for another, and the other brain picks it up on its
own schedule and acts on it, the way it acts on a Slack mention. Two parts:

- **The relay** (`services/b2b/`): a small FastAPI service that holds messages
  between brains **in memory only**, sealed by the sender so only the recipient
  can read them, and deletes each one the moment it is read.
- **The brain's side** (`scripts/b2b.py`): this brain's key pair (its public key
  is its user id), the **brokers** (relays) it uses, its **peers** (the brains
  it talks to, each linked to a person), sending, and a **poller** that runs
  with the [trigger](trigger.md) watchers and turns each message into a request
  in the same log, so it shows in **Tasks** and runs the action you picked.

```
brain A ── seal to B, sign as A ──► POST /messages ─┐
                                                    │  broker (memory only)
                                                    │  cannot open it
brain B ◄── trigger request ◄── open ◄── b2b poll ◄─┘  GET /messages (deletes on read)
          (Tasks, claude / http / script)
```

## The relay

```bash
python3 services/b2b/serve_b2b.py                    # 127.0.0.1:8780
python3 services/b2b/serve_b2b.py --host 0.0.0.0 --port 8780   # reachable by others
```

Like `scripts/serve.py`, the launcher is stdlib and builds its own venv on first
run from `services/b2b/requirements.txt` (FastAPI, uvicorn, cryptography), under
`~/.cache/memory-ledger/b2b-<hash>`. `$B2B_VENV` points it at your own venv.
It always runs one worker: the store is the process's memory, and a second
worker would be a second relay behind the same port. OpenAPI is at `/docs`.
A brain calls a relay it uses a **broker**, and can use several.

### Identity: the user id is the public key

Each brain makes an **X25519 key pair** itself. Its **user id is the public
key**, 32 bytes as unpadded base64url (43 characters): it is what others send
to, and it is safe to share. The **private key never leaves the brain**: the
relay never sees it, and holds nothing that opens a message.

The relay keeps **no accounts**. It does not know, or care, whether an id
ever logged in. In memory, it holds only the retrieval tokens it handed out
(hashed, for an hour) and the sealed messages waiting to be read; a mailbox is
forgotten as soon as it is emptied.

### Logging in: the challenge

```
brain                                   relay
  ── POST /challenge {user_id} ───────────►  seals a random secret to user_id
  ◄── {challenge_id, challenge} ──────────   (ephemeral X25519 + HKDF + AES-GCM)
  opens it with the private key
  ── POST /session {user_id, challenge_id, answer} ─►  compares (constant time)
  ◄── {token, expires_in: 3600} ──────────   the retrieval token, valid 1 hour
```

A challenge lives two minutes and is spent on the first answer, right or wrong.
The relay keeps a hash of each secret and token, never the value, and nothing
else about the id. Logging in is only for **reading** your mail: sending needs
no token, and any id can receive, logged in or not. A brain renews its token by
itself when it is near its end, or when the relay no longer knows it (it
restarted). Each broker gives the brain its own token; the user id and the
private key are the same for all of them.

### Endpoints

| Call | Body / headers | What it does |
|---|---|---|
| `POST /challenge` | `{user_id}` | a secret sealed to that public key: `{challenge_id, challenge: {eph, nonce, ct}, expires_at}`. `400` not a public key, `429` five open already |
| `POST /session` | `{user_id, challenge_id, answer}` | `201 {token, expires_at, expires_in}`; `401` wrong, expired or used |
| `POST /messages` | `{from, to, envelope}` | leaves a sealed envelope for `to`: `202 {id, to, sent_at}`. `400` malformed, or `from` does not match `envelope.from`; `413` too large; `429` their mailbox holds 500 unread; `503` the relay holds 10,000 mailboxes |
| `GET /messages` | `Authorization: Bearer <token>` | every envelope waiting for you, oldest first, as `{id, from, at, envelope}`, and **deletes them from the relay** |
| `GET /health` | | liveness; says nothing about who uses the relay |

Every refusal is `{"ok": false, "error": "..."}`.

### The crypto

A message is sealed **by the sender, before it is sent** (`scripts/b2b_crypto.py`):

- an envelope is `{v: 2, id, from, eph, nonce, ct}`: the sender's id, a fresh
  ephemeral public key, and AES-GCM ciphertext of `{text, sent_at}`;
- its key is HKDF-SHA256 over two X25519 exchanges, **ephemeral → recipient**
  (a fresh key per message) and **sender → recipient** (only the holder of
  `from`'s private key could have made it), bound to both ids, the ephemeral key
  and the message id.

So only the recipient opens it, and opening it proves who sent it, without
trusting the relay. The relay checks only that the envelope is well formed and
that `from` matches it; it cannot tell a forged `from`, the recipient can. An envelope that does not open is
dropped by the brain and counted `unreadable`, never believed.

What the relay does see: who sends to whom, when, and how long each message is.
Put it behind TLS (a reverse proxy) before it leaves localhost: the retrieval
token travels in a header.

### What it does not do

- **Persist anything.** A restart loses every session and unread message.
  Brains log in again by themselves on their next call.
- **Rate-limit sending.** Anyone who can reach it can leave mail for any id,
  up to 500 unread per id. A forged sender is dropped by the recipient
  (`unreadable`), and a stranger's message is `blocked`, but it still takes
  room until read. Keep it on a network you control.
- **Recover a key.** Lose `b2b.json`'s private key and that id is gone: make a
  new one and send your peers the new id.

## The brain's side

Configuration is `<home>/b2b.json`, written mode 600 (on Windows, which ignores the
mode, with an ACL that grants only the current user, through `icacls`). The private key is
write-only everywhere, like the trigger tokens:
`MEMORY_LEDGER_B2B_PRIVATE_KEY` outranks the file. The command line needs
`cryptography`; run from a plain `python3` it re-runs itself under the server's
venv (`scripts/requirements-server.txt`).

```bash
python3 scripts/b2b.py identity --generate                          # the key pair; prints your user id
python3 scripts/b2b.py brokers --add https://b2b.team-a.example --name "Team A relay"
python3 scripts/b2b.py brokers --add https://b2b.team-b.example --name "Team B relay"
python3 scripts/b2b.py brokers --check team-a-relay                 # log in now
python3 scripts/b2b.py config --interval 120 --enable               # poll every 2 minutes
python3 scripts/b2b.py config --action script --script b2b-note.sh \
    --arg '{b2b_peer_name}' --arg '{trigger_content}'               # run a script, not Claude
python3 scripts/b2b.py config --action claude                       # back to a Claude session
python3 scripts/b2b.py peers --add <their user id> --broker team-b-relay --person "Ana Souza"
python3 scripts/b2b.py allowed --add <their user id> --name Carla    # let a non-peer send to you
python3 scripts/b2b.py allowed --remove <their user id>              # block them again
python3 scripts/b2b.py allowed                                       # who is allowed, and who was
python3 scripts/b2b.py send --to "Ana Souza" --text "Can you review OS-123 today?"
python3 scripts/b2b.py status                                       # identity, brokers, peers, sent
python3 scripts/b2b.py poll                                         # one pass, by hand
```

The same identity on another machine: `b2b.py identity --import <private key>`.
Share your user id with the brains that should reach you, and the broker you
read from; never the private key.

| Key | Default | |
|---|---|---|
| `enabled` | `false` | poll the brokers |
| `private_key` | | this brain's identity; the user id is derived from it |
| `brokers` | `[]` | `[{id, name, url, enabled}]`, up to 20. Every enabled one is polled |
| `poll_seconds` | `300` | 30 to 86,400 |
| `do` | record it as a task or note, answer nothing | the run command for a received message |
| `action`, `http`, `script` | `claude` | what a message runs, as for a [trigger event](trigger.md#actions) |

The settings page (**Settings → B2B**) does all of this: identity, the broker
list, peers with the person each is, and the allowed list. Writing and
reading messages is the **Messages** section.

### What a message runs

Like every [trigger event](trigger.md#actions), a received message runs one of
three actions, picked under **Receiving** or with `b2b.py config --action`:

- **`claude`** (the default): a headless Claude session, told the run command
  (`do`).
- **`script`**: a file in `<brain>/scripts`, with the arguments you set,
  variables filled in. No model is called. Every variable is also in the
  script's environment as `BRAIN_<NAME>`: the message is
  `BRAIN_TRIGGER_CONTENT`, the sender `BRAIN_B2B_PEER_ID`.
- **`http`**: one API call, as for the other triggers.

Only an allowed sender's message runs any of them; a blocked one runs nothing,
whatever the action.

### Peers, and who gets through

Peers live in `graph.db` (`b2b_peer`): a user id, the **broker** that reaches
them (needed when there is more than one), a name, and optionally the
**person** node they are, by id or by a name that resolves to one person; with
no name, the person's is used. `send --to` takes a peer's id, name, or person.
Disconnecting a peer keeps its row, marked removed, and reconnecting restores
it.

**The allowed list** (`b2b_allowed`) says who may send to you, like friends on
a social network. **Only an allowed sender's message runs anything.** A message
from anyone else is logged **`blocked`**, with its text and the reason `b2b:
not on the allowed list`, and goes no further. It shows in Tasks, where
**Reprocess** runs it after all, once you confirm (see
[reprocessing](trigger.md#reprocessing-a-task)).

- Adding a peer also allows it (`peers --add … --no-allow`, or `allow: false`
  on the API, to skip that). Reconnecting one allows it again.
- The two lists are otherwise independent: you can allow a brain you never
  send to, and block a peer you still send to. Disconnecting a peer leaves it
  allowed; block it to stop its messages.
- Blocking keeps the row, marked removed, and allowing it again restores it.
- The list lives in this brain only. The relay delivers anything sealed to
  you, from anyone; the list decides what runs.
- The first time the list is created, every connected peer goes on it, so
  peers that could send before still can.

### Receiving

The poller starts with the trigger watchers in `serve.py` when `b2b.json` is
enabled and has an account, **and** the trigger master switch (`trigger.json`
`enabled`) is on: a message from another brain runs a session on your behalf,
so it sits behind the same switch. Saving the B2B config restarts it.

Each pass reads every enabled broker, which deletes what it returned, opens
each envelope, and logs each message as a request at once, `source: b2b`,
`scope: b2b:<peer id>`. A broker that fails is reported in `errors` and does not
stop the others. The request
row is the only copy left anywhere. A peer's message is queued and run like any
trigger, with `{trigger_user}` being the peer's name and these extra variables:

| Variable | |
|---|---|
| `{b2b_peer_id}` | the sender's user id (their public key) |
| `{b2b_peer_name}` | the name you gave them |
| `{b2b_person_id}` | the person node they are linked to, if any |
| `{b2b_broker}` | the broker it came through |
| `{b2b_message_id}` | the message's id |
| `{b2b_sent_at}` | when they sent it |

The headless session gets the message as a request from another person, never
as yours: the brain's rules and guardrails hold, as for any mention (the
`respond` skill). It posts nowhere by default. An answer, when the run command
asks for one, is a B2B message back, which the prompt spells out:

```bash
python3 scripts/b2b.py send --to <peer id> --text "..."
```

Sent from a session, it is also logged as a `b2b-send` action on that request.

### Sending

`b2b.py send` (or `POST /api/b2b-send`) only sends to a connected peer, by id,
name or person, through that peer's broker. Every sent message is kept in
`graph.db` (`b2b_sent`).

### Attachments

A message can carry files. They never travel in the envelope (40 KB, text only):

- **Sending.** `b2b.py send --to X --text … --file a.pdf --file b.png` (or `files` on
  `/api/b2b-send`, with names from `/api/agent-attach`). The files are zipped flat
  (at most 20, the zip at most 50 MB), sealed with a key of their own, and left on
  the relay with `POST /files`. The key, the file's id, its SHA-256 and the names
  travel **inside** the sealed message. The zip and a `message.json` are kept in
  `<home>/chat_attachments/<chat_id>/sent/<message_id>/` so a message can be sent
  again (`b2b.py resend <id>`). The brain never cleans that folder; that is the owner's.
- **Staged.** If the relay cannot be reached, or the recipient already has 500 MB
  waiting (`507`), nothing is lost: the message stays `staged` and every poll tries
  it again, under the same ids, so a retry is never a second message. Any other
  refusal marks it `failed`.
- **Receiving.** Only from an allowed sender. The file is downloaded, checked against
  the hash in the sealed message, opened with its key, and unpacked into
  `<home>/chat_attachments/<chat_id>/<message_id>/`; then it is deleted from the
  relay. The request's text says where the files are (`b2b_attachments_dir`). A zip
  that tries to leave its folder, holds a link, or unpacks to far more than it weighs
  (200 MB / 200 entries / a 200× ratio) is refused whole, and also deleted from the
  relay. A download that fails for lack of a connection is retried on later polls.
- **Refusing.** A message from a sender who is not allowed is logged `blocked`, and
  the brain posts a refusal to the relay (`POST /refusals`): everything that sender
  left for this brain, messages and files, is deleted there.

### Human or automated

Every message says whether a person wrote it. The flag (`human`) is sealed
with the text, so the recipient sees the same thing the sender does. Only a
message typed in the **Messages** chat is a person's. Everything else is
automated: a headless session's reply, a script, and `b2b.py send`. A message
from a brain older than the flag carries none, and reads as automated.

### The Messages section

**Messages** in the web UI shows B2B as conversations. On the left is every
brain this one wrote to or heard from, with a search and **＋ New** to start a
conversation with a connected peer. On the right is the thread, both
directions, each message stamped with when it was sent and flagged 🧬 (a
person) or 🤖 (automated). A received message is still a request: it runs its
action and shows in Tasks as before. A brain that is not a connected peer can
be read but not written to.

## The API

| Route | |
|---|---|
| `GET /api/b2b-conversations` | everyone this brain messaged or heard from, and connected peers not talked to yet |
| `GET /api/b2b-thread?peer=&limit=&before=` | a page of the conversation, both ways, oldest first: the latest 20, or the 20 before the time `before`; `has_more` and `next_before` walk back. Each is flagged `human`, stamped `at`, has a `status` (`sent`, `staged`, `failed`) and its `files` |
| `GET /api/b2b` | the config (the user id, never the private key), brokers with their session state, peers, the allowed list, sent messages, received ones by status, and the poller's state |
| `POST /api/b2b-config` | a patch of `b2b.json`; restarts the poller |
| `POST /api/b2b-identity` | `{force?}`: a new key pair |
| `POST /api/b2b-broker` | `{url, name?}` adds; `{id, url?, name?, enabled?}` changes; `{id, remove: true}` drops |
| `POST /api/b2b-broker-check` | `{id}`: log in to it now |
| `POST /api/b2b-peer` | `{peer_id, name?, broker_id?, person_id?, enabled, allow?}`: connect or disconnect a peer; connecting also allows it unless `allow` is false |
| `POST /api/b2b-allowed` | `{user_id, name?, enabled}`: allow a sender, or block it again |
| `POST /api/trigger-reprocess` | `{request_id}`: run a `blocked` (or failed) message again |
| `POST /api/b2b-send` | `{to, text, human?}`: send to a peer; `human` is the Messages chat |

See [the API](api.md).

## Tests

`python3 -m pytest tests/test_b2b.py` checks the crypto (only the recipient opens, a
forged sender does not), starts two real relays on free ports and drives one
from the outside (challenge, wrong and spent answers, session, send, read-once),
then runs the brain's side against a throwaway brain with a fake `claude`:
identity, two brokers, peers linked to a person, send, polling both brokers into
the request log, refusals for strangers, tampered envelopes, re-login after a
stale token, a broker that is down, and the prompt a session gets.
