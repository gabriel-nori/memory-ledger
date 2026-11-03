# Portal contract: open questions (ledger side)

Where section 1 and 5 did not say, the ledger did the smallest thing and
flagged it here. Change the code or the contract once decided. Contract v2
(2026-11-03) moved bans, strikes, appeals and chats to the portal; the
questions about them went with them.

1. **Validation error.** No code for malformed input; the ledger uses `bad_request` 400 (not in the contract's list).
2. **Unknown `after` id** is `404 not_found`. `GET messages` for a `user_id` with no messages is `200` and empty.
3. **Abuse class.** No consequence in the ledger; the skill refuses calmly. Whether `abuse` counts as a strike is the portal's call.
4. **Who writes the quota text.** With no tokens left no agent can. For the daily budget, and for a session that died on the plan limit, the ledger uses a fixed pt/en/es sentence; for the latter `resume_at` is read by ollama from the dead session's output.
5. **Retry after quota.** The message that hit the plan limit is marked `error`, not retried at `resume_at`.
6. **Budget accounting.** Spend is the sum of `cost_usd` of finished portal requests since local midnight; in-flight sessions count nothing until they finish.
7. **Ollama model** default is `llama3.2` (`portal.ollama.model`); an answer more than 8 days out is ignored (fallback: midnight).
8. **Throttle.** `throttle.per_minute` (default 300) counts every authenticated call on the trigger, polling included; one portal polling every 2 s per open chat uses 30 a minute per chat. Counted in memory: a restart clears it. Wrong tokens are held per peer at 20 a minute. Is 300 right?
9. **Call log for calls with no message** (health, polling) is in `portal_call` only (`portal calls`), not on a task, since no request exists.
10. **`user_id` format** is checked as 8-128 of `[A-Za-z0-9_-]`. The visitor's language is the last `lang` sent, `en` if none.
11. **`recruiter` is the only verdict that asks the skill to notify the owner.** Notifying on `forbidden` moved to the portal with the strikes.
