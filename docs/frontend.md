# The frontend

The brain UI is a **React single-page app** in `frontend/`, built with Vite and
TypeScript. `scripts/serve.py` builds it on first start and serves it from
`frontend/dist`, which is **not** in git — see below.

```
frontend/
  src/
    api/          the typed client and the wire shapes — one way to reach the server
    components/   the component library: everything shared lives here
    features/     one directory per section, plus the shell they sit in
    hooks/        polling, one-shot fetches, debounce, shortcuts
    lib/          pure logic with no React in it — filters, physics, colours, formatting
    styles/       the design tokens everything paints with
    test/         fixtures, the mock server, and the render helpers
  dist/           the built app. NOT in git — the server produces it. See below.
```

## The server builds it

`frontend/dist` is a build artifact and is not committed: a generated artifact
in git is a second copy of the source that drifts from it silently — a UI change
lands, the bundle is not rebuilt, and the repo quietly ships behaviour nobody
wrote. `.gitignore` covers it, and CI fails if it is ever tracked again.

So `scripts/serve.py` builds it. On startup, before it binds the port, it checks
whether the bundle is current and produces one when it is not:

1. no `frontend/dist/index.html` → build;
2. any file in `frontend/src/`, or `package.json` / `package-lock.json` /
   `vite.config.ts` / `tsconfig.json` / `index.html`, more than two seconds
   newer than the bundle → rebuild;
3. `frontend/node_modules` missing → `npm ci` first (`npm install` when there is
   no lockfile).

A `*.test.tsx` edit is deliberately **not** a rebuild: tests never reach the
bundle, and rebuilding on every one would put a compile in front of every start
while someone is writing them. The two-second margin keeps files written
together (a copy, a restore) from reading as an edit; a real edit lands minutes
after a build.

```bash
python3 scripts/serve.py              # builds if needed, then serves
python3 scripts/serve.py --rebuild    # build even when it looks current
python3 scripts/serve.py --no-build   # never run npm; the page says why if there is none
```

**This means npm is required for the UI.** It is not required for the API, and
that split is deliberate: with no node on the machine the server still starts,
every endpoint answers, `/api/docs` works, and the page returns a 503 that names
the reason and the command that fixes it (`cd frontend && npm install && npm run
build`). When a bundle is already on disk and cannot be refreshed, it keeps
being served and the terminal says it may be stale. The server never deletes the
bundle to force a rebuild, because on a machine that cannot rebuild the trade
would be a possibly-stale page for no page at all.

`tests/test_contracts.py` tests the *decision* rather than running npm, so the
Python suite stays stdlib-only. When a bundle is on disk it also checks that
`index.html` references only assets that exist and that the script parses
(`node --check`); with no bundle, that check is skipped.

## Working on it

```bash
cd frontend
npm install
npm run dev          # localhost:5173, proxying /api to a real serve.py on 8770
npm test             # every suite
npm run test:unit    # components, hooks, pure logic
npm run test:integration   # whole sections against a mock server
npm run typecheck
npm run lint         # .eslintrc.cjs: typescript-eslint + react-hooks, zero warnings
npm run build        # typecheck + build; this is what CI runs
```

`npm run dev` proxies `/api` straight to `scripts/serve.py`, so the dev server is
a hot-reloading front end over your **actual brain**, not a mock. Start the
backend in one terminal and Vite in another.

## How it is put together

**Nothing hard-codes vocabulary.** Realms, kinds, node types, edge relations,
task statuses and the two contract shapes all come from `/api/meta` at runtime.
A realm invented today is a filter chip on the next poll, with no code change. A
heading written into a component would be a second definition of CONVENTIONS
§5.1 — and the one nothing checks.

**Components are shared, not copied.** `src/components/` is the whole visual
vocabulary: one `Button` with variants rather than the eleven button styles the
old single-file page had drifted into, one `Dropdown` that closes on Escape and
on an outside click, one `Modal` that stacks and traps focus, one `Field` that
actually connects its label to its control. Features import from
`@/components`, never from a file path.

**The interesting logic is pure.** `src/lib/` holds the filter cascade, the force
simulation, the colour derivation and the date formatting, with no React and no
canvas in any of it. That is what makes them testable directly rather than
through the DOM — and it is where the tests that would actually catch a
regression live.

**The canvas does not re-render.** `GraphCanvas` runs the simulation and the
paint on one `requestAnimationFrame` loop against refs; React re-renders it only
when the data or the filters change. Pushing 800 nodes through the virtual DOM
sixty times a second is the one thing a canvas exists to avoid.

**The layout is placed, not discovered.** `lib/egoLayout.ts` puts the owner's
node (`meta.ego`) at the origin and every other node on a ring by hop distance,
before any force runs; `lib/physics.ts` then only corrects. Random seeding plus
all-pairs repulsion turned a few thousand nodes into a hairball that never stopped
moving. Reheats are keyed on what changed, never on identity: every poll hands down
fresh arrays and Sets, and reheating on those kept the map twitching.

**The section is in the URL.** `/map`, `/dashboard`, `/tasks`, `/people`, `/messages`, `/capabilities`, `/settings` (and `/api-docs`), on a
hash router. A reload lands where you were and a link to the dashboard is a link
to the dashboard — neither was true of the old page, which kept the section in a
body class. The hash means `serve.py` never has to learn about the app's routes.
`/skills` is kept as a redirect to `/capabilities`, the name the tab took when it
grew rules, processes and scripts beside the skills (its code still lives in
`features/skills/`).

**API docs** (`/api-docs`) frames the server's Swagger UI (`/api/docs`) in an
iframe, so reading the API never leaves the app: the drawer, the URL and your
place in every other section stay put. It is framed rather than rebuilt because
the document is generated from the running routes, and a copy in the app would
be a second description to go stale. Like the map, it owns the viewport
(`body[data-section="api-docs"]` does not scroll; the frame scrolls inside
itself). Beside its drawer row sits a plain `<a>` arrow — a real navigation, not
a `NavLink` — that opens `/api/docs` in a tab of its own, and the page's heading
row carries the same link. The two are siblings, since a link inside a link is
invalid HTML.

**The icon is the person's to set.** Settings > Preferences uploads one image
(PNG, JPG, ICO or SVG, 512 KB) that the tab, the sidebar and the Ledger Agent box
all show; with none set, the default below applies. The tab's `<link rel="icon">`
is repointed at `/api/agent-icon?v=<mtime>` from `AppShell` once `/api/meta` says
one is set.

**The default favicon is the map, not a brain.** `public/favicon.svg` draws a hub with
three satellites in the app's own token colours — the one shape from the graph
that survives being drawn at 16px, where a brain outline is a grey smudge. Its
ground is painted rather than transparent so it holds up on a light tab strip;
the `href` is relative, like every other asset here, so the same `dist` works
from a sub-path.

**A setting and a preference are different things, and they live in different
places.** A setting changes what the engine computes — `owner` decides what
`--mine` returns, `tz` decides which day a due date lands on — so it goes to
`<home>/settings.json` through `/api/settings`, where the CLI, the hooks and any
cron read the same answer. What you were *looking at* computes nothing, so
`src/hooks/usePrefs.tsx` keeps it in `localStorage`: which dashboard view was
open, which filters were on, what the map had hidden.

`PrefsProvider` sits above the router, because those sections mount and unmount
as you navigate and a provider held any lower would re-read on every change —
each section spending its first render showing a default it is about to replace.
`usePref(key, fallback)` is the whole API, shaped like `useState` plus a `ready`
flag: a view that restores stored state waits for `ready` before applying it,
since the brain's copy may still be in flight.

Syncing to the brain (`ui_sync`) is opt-in and covers one case `localStorage`
cannot — the same person on a second machine. Local always wins the race: every
change is written to `localStorage` synchronously and pushed afterwards,
debounced, so a brain that is unreachable costs you cross-machine sync and never
your own UI. Writes are compared by content, not identity, because a view
persisting a derived object hands the provider a fresh object every render and
each one would otherwise be a write that says nothing.

Two gotchas worth knowing: `Set` does not survive `JSON.stringify` — it comes
back as `{}` — so the map's filter goes through `serializeFilter` /
`deserializeFilter` in `src/lib/graphFilter.ts` rather than being stored
directly; and the dashboard deliberately does not persist `from`/`to`, which are
a rolling window, because storing the literal dates would restore a fortnight
that ended weeks ago and call it your default.

## Testing

Two suites, both with vitest and Testing Library, and one mock server (MSW)
shared between them. `vitest.setup.ts` clears `localStorage` after every test:
preferences persisting between visits is the feature, and between *tests* it
would mean one test's last filter silently becomes the next one's starting state.

| Suite | What it covers |
|---|---|
| **unit** — `src/components`, `src/hooks`, `src/lib`, `src/api` | One thing at a time: does the cascade unwind a chain, does a facet say `4/9`, does polling ever overlap two requests, does an aborted fetch get reported as an error. |
| **integration** — `src/features` | A whole section, rendered against the mock server, driven the way a person drives it: switch the view, tick a chip, expand a card, complete a task, and assert on the write that actually reached the wire. |

The mock server is configured with `onUnhandledRequest: "error"`, so a component
that quietly calls an endpoint nobody stubbed fails the run rather than passing
silently. Its fixtures are the shapes `serve.py` really sends — they were read
off `brain_query._slim`, `graph.skill_trees` and `settings_payload`, and
`tests/test_api.py` pins those shapes against the real server.

What the integration tests are actually for is the behaviour that is easy to
break and invisible when broken:

- a write is never sent for a value the person did not change (a change handler
  on a pre-filled field fires on a bare focus-and-blur);
- `unlink` sends the edge's real direction, not the one it happens to be
  rendered from;
- a relation the person re-pointed survives further typing in the composer;
- `--no-write` and "could not read `/api/meta`" are reported as the different
  things they are;
- a refused contract names the section that is missing, before the round trip.

## Accessibility and theming

The palette is the same one the old page used — this refactor changed how the UI
is built, not what it looks like. Tokens live in `src/styles/tokens.css` and
nothing outside that file names a hex value.

Every interactive element is a real control with a real accessible name, because
the tests drive the app by role and name and a control nobody can name is a
control nobody can test. `prefers-reduced-motion` is honoured.
