# B2B relay (Brain 2 Brain)

A relay for sealed messages and files between brains. Python, FastAPI.

```bash
python3 services/b2b/serve_b2b.py [--host 127.0.0.1] [--port 8780]
```

A user id is an X25519 public key; the private key stays with its brain.

- `POST /challenge {user_id}` returns a secret sealed to that key.
- `POST /session {user_id, challenge_id, answer}` with the opened secret returns
  a retrieval token, valid for one hour.
- `POST /messages {from, to, envelope}` leaves an envelope the sender sealed to
  `to`. No token, and `to` need never have logged in. The relay cannot open it.
- `GET /messages` (bearer) returns your envelopes and deletes them from the relay.

- `POST /files?id&from&to&sha256` (body: the sealed file) leaves a file, up to 50 MB
  each and 500 MB waiting per recipient (`507` past that: the sender keeps it
  staged and retries). `GET /files/{id}` (bearer) downloads it; `DELETE /files/{id}`
  removes it once the hash checks. A file nobody downloads goes after 7 days.
- `POST /refusals {from}` (bearer) deletes every message and file that sender left
  for you.

There are no accounts. Messages and files are stored in PostgreSQL when all of
`B2B_DB_USER`, `B2B_DB_PASSWORD`, `B2B_DB_NAME`, `B2B_DB_URL` (host) and
`B2B_DB_PORT` are set and the connection works; otherwise, or on any failure at
start-up, in SQLite (`$B2B_SQLITE_PATH`, default
`~/.local/share/memory-ledger/b2b-relay.db`). `/health` says which. Retrieval
tokens (as hashes) stay in memory: a restart only makes brains log in again.

The design, the crypto, and how a brain uses it: [docs/b2b.md](../../docs/b2b.md).
