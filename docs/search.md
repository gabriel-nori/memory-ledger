# Search patterns

The brain has one index and several ways to interrogate it. Choosing well is the
difference between a 1,500-char answer and a 9,000-char one.

## The index

A single FTS5 virtual table over `title` + `body` + `cues`:

```sql
-- cues surfaces the match-only props as a real column so FTS5 can reach them
ALTER TABLE nodes ADD COLUMN cues TEXT GENERATED ALWAYS AS (
    ifnull(json_extract(props,'$.scenario'),'')    || ' ' ||
    ifnull(json_extract(props,'$.triggers'),'')    || ' ' ||
    ifnull(json_extract(props,'$.description'),'')
) VIRTUAL;

CREATE VIRTUAL TABLE nodes_fts USING fts5(
    title, body, cues, content='nodes', content_rowid='rowid'
);
```

It is **contentless-synced**: the text lives in `nodes`, and three triggers
(`nodes_ai`, `nodes_ad`, `nodes_au`) keep the index aligned on insert, delete and
update. You never write to `nodes_fts` directly.

### Why `cues` exists

`add-rule --scenario` and a skill's `triggers` / `description` are written
precisely so a rule or skill fires on words its prose never uses. Until 2026-07-31
none of them were reachable: the index covered `title` and `body` only, so
`match-rules` and `match-skills` were matching on prose accident and every
authored cue word was dead weight. FTS5 external-content tables can only index
real columns of the content table, so the JSON is surfaced as one VIRTUAL
generated column and indexed like any other.

Notes for anyone touching this:

- Detect the column with `PRAGMA table_xinfo`, **not** `table_info` — the latter
  omits VIRTUAL generated columns, so the migration would re-run every connect.
- `SELECT *` returns `cues`. `_node_dict` strips it; it is an index input, not
  part of the node. The neo4j payload is built field-by-field and never sees it.
- Changing the FTS column list drops and rebuilds the index and its triggers
  automatically on next connect. `nodes` is the source of truth, so this is safe.
- SQLite older than 3.31 cannot add a generated column. There the index falls back
  to `title, body` and cue matching degrades to prose — it does not break.

Other consequences worth knowing:

- The rest of `props` is **not indexed**. Searching for an assignee or a Jira key
  only works if that text also appears in `title`, `body` or a cue field. Filter
  on props with `list --type` / the dashboard chips instead.
- Deleting a node cascades to its edges (`ON DELETE CASCADE`) and to the FTS row.
- The index is ~21% of the database file. That is the cost of instant search.

### Measuring whether cues work

Cue words are easy to tune into a number that means nothing, so there is a harness:

```bash
python3 scripts/eval_cues.py                 # every set
python3 scripts/eval_cues.py --set fresh     # one set
python3 scripts/eval_cues.py --min-hit3 80   # exit 1 below the threshold
```

Each case is a realistic phrasing plus the rule or skill that must come back. It
reports **hit@1** and **hit@3**; hit@3 is the one to optimise, because several
rules are returned and the model reads them all.

The cases are **not in this repo** — they name a person's own rules and skills.
They live at `~/.second_brain/eval_cases.json` (override with `--cases` or
`$SECOND_BRAIN_EVAL_CASES`); `eval-cases.example.json` documents the format.

**Keep a set you never tune against, and quote that one.** Measured on one real
brain, 2026-07-31, over three sets of ~19 rule and ~24 skill cases:

| | rules hit@1 | rules hit@3 | skills hit@1 | skills hit@3 |
|---|---|---|---|---|
| right after cues were first indexed | 68% | 89% | 75% | 95% |
| after two rounds of cue expansion, on the **tuned** sets | 89% | 100% | 79% | 100% |
| the same cues, on a **fresh** set written afterwards | 31% | 47% | 66% | 83% |

That last row is the honest one. Cue expansion largely memorises the phrasings you
thought of. It is still worth doing — synonym *families* were what moved the fresh
number at all — but the ceiling is real.

Skills generalise far better than rules, and the reason is structural: a skill
fires on a **request**, which has a predictable verb ("remind me", "check my
email"), so enumerating the verb family covers most of the space. A rule fires on a
**situation** in open-ended prose ("six calls today, zero breaks" should reach the
overload rule). There is no verb family to enumerate, and lexical FTS cannot bridge
it. For a rule that must never be missed, `--non-negotiable` is the reliable path;
it costs tokens on every request, so it stays rare.

## Term construction

Both read paths build prefix queries, but differently:

| Path | Query built | Effect |
|---|---|---|
| `search "a b"` | `"a"* "b"*` (implicit AND) | narrow — every term must appear |
| `recall "a b"` | `"a"* OR "b"*` | wide — any term seeds, ranking sorts it out |

Each term is quoted so punctuation and partial words are tolerated —
`ABC-3101` survives, `ops_driver` survives.

`recall` additionally drops terms shorter than 3 characters and strips a bilingual
stop-list (English + pt-BR) before building the cue. `search` does not: it
searches exactly what you typed.

### Writing a good cue for `recall`

Because the terms are OR-ed and then reranked, **more specific nouns beat more
words**. Two or three distinctive terms outperform a sentence:

```bash
# good — distinctive, few
recall "ops_driver depot"
recall "ABC-3101 region_name"

# worse — the stop-list eats most of it, the rest is generic
recall "what did we decide about the thing with the driver"
```

If a cue returns the owner's `person` node and a pile of channels, the cue was too
generic — the ubiquity penalty fights that, but it cannot invent specificity.

## Choosing a path

```
Do you know the exact string?           -> search
Do you want what's related to it too?   -> recall
Do you have the id?                     -> get-node / neighbors
Is it a date question?                  -> brain_query.py (agenda/week/...)
Is it "what must I act on"?             -> brain_query.py --mine
Is it "what's stuck"?                   -> open-loops
Do you need judgement about the answer? -> /ledger (this one costs tokens)
```

## Budgets and drilling in

Every budgeted response tells you it is a summary:

```json
{"ok": true, "count": 6, "of": 15, "chars": 2813, "budget": 2600,
 "truncated": 4, "hint": "full body: get-node --id · everything: --full",
 "results": [...]}
```

- `count` vs `of` — how many you got vs how many matched.
- `truncated` — how many bodies were clipped.
- `hint` — the command that returns full text.

The correct follow-up to a truncated result is `get-node --id <id>`, not a
re-search with different words. Re-searching is what burns tokens.

## Excerpts

`recall` returns a 150-char window **centred on the first matching term**, not the
first 150 chars of the body. A match 800 characters into a note still returns
useful context. `search` trims from the start instead, sharing its budget evenly
across results (minimum 160 chars each).

## The `why` field

`recall` labels every row:

- `"matched"` — a lexical hit, it earned its rank
- `linked to "<title>"` — reached by spreading activation only; the title is the
  lexical hit that fed it the most activation. (Falls back to `"associated"` when
  that seed cannot be resolved.)

Treat `linked to` rows as leads, not answers. They are the mechanism that
surfaces a blocker you did not ask about, and also the mechanism that occasionally
surfaces something irrelevant.

## Ranking recap

Full detail in [the algorithm](algorithm.md#recall--associative-retrieval):

```
seed        1 / (1 + fts_rank_position)
spread      × 0.45 per hop, ÷ degree (fan-out), 2 hops, convergent paths sum
strength    × (0.55 + 0.45 × strength)        strength = repetition, 21-day decay
ubiquity    ÷ (1 + log2(1 + degree))          associated nodes only
hub         ÷ sqrt(degree / 8) when degree > 8 associated nodes only
tail cut    drop associated rows under 15% of the top score
```

The cue is stemmed lightly (pt/en plurals and common suffixes) before the prefix
query, so `deploys` and `deploying` both reach `deploy…`.

## Known sharp edges

- **A broad cue on a large graph is the slow path.** FTS `ORDER BY rank` ranks
  every match before `LIMIT 40` applies, so cost scales with matches, not
  results. See [performance](performance.md#scaling).
- **`recall` writes.** Hebbian wiring means a read can create up to 2 edges. Pass
  `--no-wire` for a pure read.
- **Stop-words are a fixed list**, not learned. A genuinely important word that
  happens to be on the list (`"how"` in "how-to") is dropped from the cue.
- **No stemming or synonyms.** `deploys` does not match `deploying`. Prefix
  matching covers some of this (`deploy*`), which is why terms are prefixed.
- **No vector search.** Retrieval is lexical + structural. A cue that shares no
  words with the target finds it only through edges.
