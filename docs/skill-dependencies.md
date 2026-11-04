# The skill dependency tree

The other half of `## Depends on` (CONVENTIONS §5a). Both are views of one graph,
and `scripts/check-contracts.sh` fails the build when they disagree.

**This document is the built-in tree only, and it is not the only copy.**
`graph.py sync-skill-tree` projects the same declarations into the brain as nodes
and edges, where they can be queried and drawn: built-in skills under
`hub_skills_builtin`, user skills under `hub_skills_user`. This file covers the
first. The second cannot live here, because it is different in every brain.

Second-brain skills do not hand artifacts to each other the way a delivery
pipeline does. They hand off **through the graph**: one skill writes a node type,
another reads it, and neither has to name the other. So this tree is organised by
what each skill touches in the brain, not by run order.

## 1. Layers

```
                      the genome (rule nodes, §2i)
                                 |
                          every skill asks it
                                 |
  ┌──────────────┬───────────────┼────────────────┬──────────────────┐
  │              │               │                │                  │
CAPTURE        RECALL          STATE            OUTWARD            META
  │              │               │                │                  │
remember       recall         add-task         send-slack        learn-skill
ingest-context visualize      complete-task     calendar          writing-style
read-email     export-memory  follow-up         delegate          custom-trigger-builder
                                                respond
                                                inbound-http-respond
read-jira                     remind
slack-inbox
web-search
```

No skill `Requires` another. Every one of them is a valid entry point, because the
person can start anywhere: from Slack, from a question, from a task. What they
share is the graph, so the real coupling is in the read and write surfaces below.

## 2. The graph surface

| Skill | Writes | Reads |
|---|---|---|
| `add-task` | `task`, `subtask`, `has-subtask`, `assigned-to`, `part-of`, `blocked-by` | `search`, `list`, `areas` |
| `calendar` | `meeting` (optional), `relates-to` | Google Calendar, `task` props, `person.tz` |
| `complete-task` | status `done` via `complete` | `search`, `list --type task` |
| `custom-trigger-builder` | none in the graph: one file, `<home>/triggers/<id>.yaml`, saved off | `trigger.py custom`, `<home>/scripts`, the names in `custom.secrets` |
| `delegate` | `assigned-to`, `props.state`, `waiting_on` | `person`, `task` |
| `document-html`, `document-md`, `document-pdf`, `document-txt` | one file under `<home>/exports/documents/`; optionally a `note` (`Document style - <format>`) | the `Document style` note, `recall` |
| `export-memory` | none (files only) | the whole graph |
| `follow-up` | `set-state`, `pr`, `email`, `message` | `open-loops`, GitHub, Gmail, Slack, Jira |
| `ingest-context` | `meeting`, `person`, `decision`, `task`, `note`, `jira-issue` + edges | Gmail, Drive, Slack, Jira, `search` |
| `learn-skill` | `skill`, `learned-from`, `applies-to`, `relates-to` | `match-skills`, `get-node` |
| `read-email` | none directly | Gmail |
| `read-jira` | `jira-issue` | Atlassian MCP |
| `recall` | none | `search`, `get-node`, `neighbors`, `list`, `due` |
| `remember` | any node type + edges + `areas` | `search`, `areas` |
| `remind` | `reminder`, `about` | `person.tz`, the routine store |
| `inbound-http-respond` | one `note` through `remember` for a recruiter message; the answer through `trigger.py inbound-http reply` | nodes with `props.public = true`, through `public_nodes` only |
| `respond` | `trigger_action` rows (copied onto the `request` node), plus what the requested work produces | `request`, `person`, `jira-issue`, `task`; the thread or issue through its MCP |
| `send-slack` | `note` (optional) | `person` props (`slack`, `lang`), `writing-sample` |
| `slack-inbox` | none directly | Slack |
| `visualize` | none by the skill (the UI writes) | the whole graph |
| `web-search` | `resource` | the live web |
| `writing-style` | `writing-sample`, `authored-by` | `writing-sample`, `person` |

## 3. Where a skill hands to another

These are `Optional` declarations, never `Requires`. Each one is a hand-off the
skill offers, not a precondition it waits on.

- `add-task` offers `remind` when the task is time-bound.
- `complete-task` offers `learn-skill` when finishing revealed a repeatable procedure.
- `custom-trigger-builder` enriches the trigger through `recall`; the tasks the trigger starts later are run by the trigger engine, through `respond`.
- `delegate` reaches the owner through `send-slack` and sets the check-back through `remind`.
- `follow-up` reaches a stalled owner through `send-slack`.
- `read-email`, `slack-inbox` and `read-jira` hand ingestible content to `ingest-context`.
- `ingest-context` hands the owner's own action items to `add-task`.
- `remind` fires a routine that reaches the person through `send-slack`.
- `inbound-http-respond` reaches the owner through `send-slack` and `read-email` and records the contact through `remember`, for a recruiter message only; it is started by an `inbound-http` trigger, never by another skill.
- `respond` enriches its answer through `recall`; it is started by the trigger engine (`scripts/trigger.py`), never by another skill.
- `send-slack` drafts through `writing-style`.
- `slack-inbox` routes each message to whichever skill matches its intent.

## 4. Cycles

The requires-graph is empty, so it is trivially acyclic. The hand-offs in §3 do
contain loops, for example `follow-up` to `send-slack` to a reply that
`slack-inbox` reads back into `follow-up`. That is the brain working as intended:
a loop through the world and the graph, bounded by the person, not a scheduling
bug. The checker only walks `Requires`, which is why those loops do not fail it.
