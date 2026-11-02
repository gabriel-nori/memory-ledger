# Developer documentation

The [main README](../README.md) is the tour: what the brain is and how to use it.
These pages are the reference: how it works and how to change it.

| Page | Read it for |
|---|---|
| [algorithm.md](algorithm.md) | capture, memory strength, spreading activation, Hebbian wiring, consolidation, the genome |
| [queries.md](queries.md) | every read path, its flags, and which one to reach for |
| [search.md](search.md) | the FTS5 index, cue construction, budgets, sharp edges |
| [api.md](api.md) | the local HTTP server — the FastAPI app, its envelope, its venv, and how to extend it. Its interactive reference is `/api/docs` on a running server, generated from the routes |
| [frontend.md](frontend.md) | the React single-page app — layout, why its build is committed, how to work on it, and what the two test suites cover |
| [trigger.md](trigger.md) | summoning the brain from Slack, Jira and GitHub, and custom block-polls for anything else — Socket Mode and polling, the GitHub PR events you pick, the per-conversation gate, the request audit log, the headless `claude -p` dispatch, and setup |
| [agents.md](agents.md) | the Agents tab — one stateless call per turn, context from recall and a configurable window, read-only vs tools-on per conversation, connectors (Claude, Ollama, and Gemini and Codex behind `ai_providers`), the live log and cost |
| [usage.md](usage.md) | the usage ingest and report — session logs to `usage-week` nodes, the price-table estimate, the throttled SessionStart run, the checkpoint |
| [performance.md](performance.md) | measured latency, storage, growth projections, scaling, known limits |
| [neo4j.md](neo4j.md) | running a real graph database instead of the SQLite mirror — `neo4j_local.py up` (Docker, one command), the `backend` setting, and whether it is worth it |
| [comparison.md](comparison.md) | how this compares to MemPalace and other second brains, and measured SQLite vs neo4j vs ChromaDB |
| [roadmap.md](roadmap.md) | what is under way, what is next, and what was deliberately left out |
| [b2b.md](b2b.md) | brain-to-brain messages: the key pair, the relay, peers, and how a message becomes a trigger request |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | setup, layout, how to add a node type / command / endpoint / skill / custom-trigger block, and how commits and PRs are checked |

Also: [`CONVENTIONS.md`](../CONVENTIONS.md) is not developer documentation — it is
the operating contract every skill loads at runtime. Changing it changes behaviour.

## The one-paragraph version

Every interaction is written to a local SQLite graph (`nodes`, `edges`, and an
FTS5 index) by three hooks that never call a model. Retrieval seeds on full-text
search, spreads activation across edges with distance decay and fan-out
normalisation, reranks by a memory strength that rises with use and decays with
disuse, and returns a budgeted snippet payload. Nodes that keep co-activating get
wired together after the third repetition. Nothing is ever deleted — decay
changes rank, not existence. Optionally the whole graph mirrors to neo4j, where
SQLite becomes the local cache and neo4j the master.
