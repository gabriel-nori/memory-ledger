# Portal contract: open questions (ledger side)

Where section 1 and 5 did not say, the ledger did the smallest thing and
flagged it here. Change the code or the contract once decided.

1. **Create chat status.** An idempotent repeat returns `201` too (contract literal). Should an existing chat be `200`?
2. **Appeal response and conditions.** Body is `{appeal_id, status:"received"}`. An appeal on a chat that is not banned is `400 bad_request`.
3. **Validation error.** No code for malformed input; the ledger uses `bad_request` 400 (not in the contract's list).
4. **Unknown `after` id** is `404 not_found`.
5. **New chat for a banned email or IP:** `POST /v1/chats` returns `403 banned` with `error.appeal:true`.
6. **Abuse class.** No consequence defined; the skill refuses calmly and counts no strike.
7. **Who writes the quota text.** With no tokens left no agent can. For the daily budget, and for a session that died on the plan limit, the ledger uses a fixed pt/en/es sentence; for the latter `resume_at` is read by ollama from the dead session's output. Is that the intent?
8. **Retry after quota.** The message that hit the plan limit is marked `error`, not retried at `resume_at`.
9. **Budget accounting.** Spend is the sum of `cost_usd` of finished portal requests since local midnight; in-flight sessions count nothing until they finish.
10. **Ban scope.** Bans, strikes and chats are per trigger, not shared across portal triggers.
11. **Ban IP.** Uses the salted hash of `X-Client-IP` (amendment 5.1). Without the header only the email is banned. `bind_ips` still checks the TCP peer (the portal backend). `portal ban --ip` takes a raw address or a listed hash.
12. **Strike 3.** No assistant message is stored; the ban notice is the only answer. Appeals open a `task` node.
13. **Ollama model** default is `llama3.2` (`portal.ollama.model`); an answer more than 8 days out is ignored (fallback: midnight).
14. **Ban notice text** is fixed in pt/en/es, not agent-written.
15. **Tasks-tab buttons** (revoke, regenerate, ban, unban): the CLI and data exist; the UI buttons are not built here.
16. **Call log for calls with no message** (health, create chat, polling) is in `portal_call` only (`portal calls`), not on a task, since no request exists.
17. **`user_id` format** is checked as 8-128 of `[A-Za-z0-9_-]`; same `user_id` with another email returns the existing chat unchanged.
