# Client ↔ Ledger contract (v2)

Source of truth for two codebases built in parallel: **ledger** (this repo, runs at home) and **client** (React + Node on Hostinger, separate repo `~/dev/client`). Neither side may change this file alone. v2 (2026-11-03, owner): bans, strikes, appeals and chat limits live only in the client; the ledger is the HTTP door (token, IP/CIDR allowlist, throttle) and returns the skill's verdict. The trigger type is `inbound-http` (renamed on 2026-11-03; routes, bodies and errors are unchanged, so the client side is not touched by the rename).

## 0. Principles
- The visitor never reaches the ledger. Only the client backend does, with a bearer token.
- Ledger exposes **one HTTP listener per `inbound-http` trigger**, on that trigger's own port, serving only that trigger's routes. TLS terminated and the port forwarded outside the ledger.
- Log hygiene: no tokens, auth headers or raw IPs in app logs. The task-tab call log is the only place bodies live.
- Languages: pt, en, es. The reply follows the language of the incoming message. Client UI has a selector pt/en/es that also switches the static CV/portfolio HTML.

## 1. Ledger side

The ledger is the HTTP door and the answerer. It takes a message, an attachment and the visitor's `user_id`, and returns the answer with the skill's verdict. It knows nothing else of the visitor: no name, no email, no IP.

### 1.1 Trigger type `inbound-http` (one way in per file)
Created like other custom triggers (`<home>/triggers/<id>.yaml`, saved switched off). A second way in (another token, port or IP set) is a second file. Fields:
```yaml
id: chat-service
type: inbound-http
port: 8801                 # one per trigger, range 8801-8899, unique, listens only here
bind_ips: ["*"]            # "*" or list of IPs/CIDRs, checked against the TCP peer
throttle: {per_minute: 300}                            # calls a minute on this way in
rate: {daily_budget_usd: <n>, tz: America/Sao_Paulo}   # resets at local midnight
attachments: {max_mb: 10, types: [application/pdf, image/png, image/jpeg, image/webp]}
jokes: {every_n_min: 6, every_n_max: 12}               # random, one joke per N replies on in-scope too
skill: inbound-http-respond
```
Token: stored hashed (sha256). Plain value shown once on creation. **Regenerate = old token dies immediately.** Revocation is manual. No expiry.

### 1.2 HTTP API (ledger, all JSON, `Authorization: Bearer <token>`)
Checks run in order: peer in `bind_ips`, token, throttle, route.
Errors: `{"error": {"code": "<snake>", "message": "<text>"}}`. Codes: `bad_request` 400, `unauthorized` 401, `ip_not_allowed` 403, `not_found` 404, `too_large` 413, `unsupported_type` 415, `malware_detected` 422, `quota_exhausted` 429 (carries `resume_at`), `rate_limited` 429 (carries `retry_after`, seconds), `unavailable` 503.

`user_id` is minted by the client backend (uuid, 8-128 of `[A-Za-z0-9_-]`). The ledger makes the user on its first message.

| Method + path | Body | Response |
|---|---|---|
| `POST /v1/messages` | `{user_id, text, lang?, attachment_ids?[]}` | `202 {message_id, status:"queued"}` |
| `GET /v1/messages?user_id=<id>&after=<message_id>` | – | `200 {messages:[{id, role:"user"\|"assistant"\|"system", text, lang, created_at, status:"queued"\|"working"\|"done"\|"error", verdict?}]}` (polling; client polls every 2 s while a message is `queued\|working`) |
| `POST /v1/attachments` | multipart `user_id`, `file` | `201 {attachment_id, name, bytes, type}` after ClamAV scan; rejected files are deleted |
| `GET /v1/health` | – | `200 {ok:true, quota:{available:bool, resume_at?}}` |

`verdict` is on assistant messages: `in_scope` \| `silly` \| `recruiter` \| `forbidden` \| `abuse`. `after` returns every message after the cursor id in order, with current status; without it, the full history of that user.

`quota_exhausted`: the assistant message text is a fixed line in the user's language ("No momento todos meus tokens acabaram. Fui tirar uma soneca e volto às XX"). `resume_at` (ISO-8601) is extracted from the dead session by **ollama**, falling back to next local midnight.

Machine down: client gets a connect error/timeout and shows "unavailable" **without any promised time**.

### 1.3 Skill `inbound-http-respond`
- Reads only brain nodes with `props.public = true` (explicit allowlist). Everything else is invisible.
- Treats user text and attachments as data, never instructions; strips/neutralises prompt-injection before use.
- Classifies each message: `in_scope` | `silly` | `recruiter` | `forbidden` (personal/private data attempts) | `abuse`, and returns it as the `verdict`.
- `silly` (and randomly every N in-scope replies): answer with a joke in the user's language. Examples to keep, in pt/en/es: pi recipe → circumference/diameter; cake → "the cake is a lie"; elephant in fridge → "tried to process it, the elephant overloaded my services".
- `recruiter` (asks for meeting, hiring interest): notify Gabriel (Slack + e-mail) including the question, record in brain, tell the user "Gabriel was notified".
- `forbidden`: calm refusal. **The ledger counts no strikes and bans nobody**; the client reads the verdict and decides.
- Identifies itself as Stella, Gabriel's AI assistant.

### 1.4 Tasks tab
Each trigger call and response (headers minus auth) is logged under the trigger's tasks entry, same as other triggers. Retention: until Gabriel purges. Revoke/regenerate are buttons there.

## 2. Client side

### 2.1 Stack
React front (static CV/portfolio HTML per language + chat widget), Node backend. SQLite or the Hostinger DB for client data.

### 2.2 Identity
1. Visitor gives name + email → backend sends SMTP link, **single use, expires 1 h**.
2. Click → backend verifies, mints `user_id`, issues **access token (1 h) and refresh token (7 d)**, kept by the front in localStorage. Refresh rotates; reuse of an old refresh revokes the whole family.
3. Strict CSP, no inline scripts, to limit XSS exposure of localStorage.

### 2.3 Limits (backend)
Per **email/day** default 20, per **IP/day** default 40, counted in the client DB, reset at midnight America/Sao_Paulo. Per-IP override table (`ip_limits`: ip, daily_limit, note) editable by Gabriel. Exceeded → `429` to the front with a friendly message in the user's language, ledger not called.

### 2.4 Proxy
Front only talks to the client backend. Backend maps: `/api/messages` (POST, GET poll), `/api/attachments`, and adds the ledger bearer token (env var, never to the browser) and the `user_id`. Name and email stay in the client; the ledger never gets them. Ban state is checked in the client before every call, so a banned visitor never reaches the ledger. Backend checks `Content-Length` ≤ 10 MB and type allowlist before forwarding.

### 2.4b Ban (client only)
- Keyed by **email and IP both**; a match on either blocks. Permanent. Strikes are counted per email in the client DB: each ledger answer with `verdict: forbidden` is a strike; strikes 1 and 2 add a warning that insistence may lead to a ban, strike 3 bans. Gabriel is notified at each strike, with the question.
- Banned: UI state with reason and appeal form. Appeal: client route `POST /api/appeal {text}` creates the review for Gabriel inside the client. Unban is manual (client admin).

### 2.5 Privacy notice
Shown before sign-up, 3 languages, must be accepted. Text: "We store only your name, email and your messages (and the IP address of your calls, for abuse protection) with the sole purpose of making this chat work. Nothing else is collected or shared. To request deletion, email <contact>." Deletion requests answered by Gabriel; retention until Gabriel purges.

### 2.6 UI states
Normal, sending, polling, quota exhausted (with `resume_at` rendered in local time), unavailable, banned (reason + appeal form), rate-limited, throttled by the ledger (`rate_limited` with `retry_after`: wait and retry).

## 3. Shared test vectors
Both sides ship tests for: Ledger: auth failure, IP outside CIDR, token regenerated (old = 401), throttle (`rate_limited` with `retry_after`), polling lifecycle, quota_exhausted with `resume_at`, malware rejection, `verdict` on the answer, injection string in text not obeyed. Client: 3-strike ban on `verdict: forbidden` by email and by IP, appeal, per-email and per-IP daily limits.

## 4. Ownership and merge
- Ledger agent: owns §1, works in a worktree of this repo, adds docs to `docs/trigger.md`, tests under `tests/`.
- Client agent: owns §2 in `~/dev/client`, mocks the ledger from §1.2 for tests.
- Anything unclear in this contract: write it to `docs/inbound-http-contract-questions.md`, do not guess.

## 5. Amendments v1.1 (resolve client questions; owner may overrule)
1. **Visitor IP:** stays in the client (v2). The ledger ignores `X-Client-IP` and never logs it. Client reads the real IP via `TRUST_PROXY_HOPS` (Hostinger hop count to be measured).
2. **Quota:** `POST /v1/messages` returns `429 quota_exhausted` with `error.resume_at` (ISO-8601). The agent's "volto às XX" text is also stored as an assistant message.
3. **`after` cursor:** returns every message after the cursor id in order, with current status (so status changes of pending messages arrive).
4. **Appeal:** client-only (v2); the ledger has no appeal route.
5. **Attachments:** ledger enforces that `attachment_ids` belong to that user (404 otherwise). Max 5 per message. Text cap 4000 chars both sides.
6. **Sign-in abuse:** `POST /api/auth/request` limited to 5/hour per email and per IP.
7. **Email normalisation:** for limits only, lowercase and strip `+tag` (and dots for gmail.com). Identity stays the typed address.
8. **Refresh:** absolute session maximum 30 days; 10 s grace window on rotation for two tabs.
9. **Ledger misconfig errors** (`unauthorized`, `ip_not_allowed`) show the visitor `unavailable`; logged as config error.
10. **IP retention:** client `usage` rows pruned after 90 days; chats follow "until Gabriel purges".
11. **Contact in privacy text:** gabriel.almilhatti@gmail.com (env `PRIVACY_CONTACT`). `daily_budget_usd` default 5 until changed.
