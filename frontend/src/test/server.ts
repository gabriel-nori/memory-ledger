import { setupServer } from "msw/node";
import { http, HttpResponse } from "msw";
import {
  agendaFixture,
  agentsFixture,
  voiceFixture,
  agentCommandsFixture,
  agentConversationsFixture,
  agentThreadFixture,
  b2bFixture,
  b2bConversationsFixture,
  b2bThreadFixture,
  backupsFixture,
  captureFixture,
  customTriggerDetailFixture,
  customTriggersFixture,
  consolidationPreviewFixture,
  doctorFixture,
  graphFixture,
  metaFixture,
  nodeDetailFixture,
  personDetailFixture,
  personDuplicatesFixture,
  settingsFixture,
  ruleProposalsFixture,
  scriptsFixture,
  skillsFixture,
  statusFixture,
  triggerFixture,
  tasksFixture,
  taskDetailFixture,
  usageReportFixture,
} from "./fixtures";

/** Every write the tests observed, so a test can assert what was actually sent. */
export const writes: { path: string; body: unknown }[] = [];

export function resetWrites(): void {
  writes.length = 0;
}

const ok = (body: Record<string, unknown> = {}) => HttpResponse.json({ ok: true, ...body });

/** Records a write and answers ok, which is what most of them do. */
function write(path: string, respond: (body: never) => Response = () => ok()) {
  return http.post(path, async ({ request }) => {
    const body = await request.json().catch(() => ({}));
    writes.push({ path, body });
    return respond(body as never);
  });
}

export const handlers = [
  http.get("/api/meta", () => HttpResponse.json(metaFixture)),
  http.get("/api/graph", () => HttpResponse.json(graphFixture)),
  http.get("/api/views", () =>
    ok({ views: ["agenda", "open", "today", "tomorrow", "week", "closed", "range"] }),
  ),
  http.get("/api/assignees", () => ok({ assignees: { alex: 3, unassigned: 1 } })),
  write("/api/rename-realm", (b: { old: string; new: string }) =>
    ok({ from: b.old, to: b.new, moved: 42, merged_into_existing: b.new === "personal", default: b.new }),
  ),
  http.get("/api/realms", () => ok({ realms: metaFixture.realms, default: "work" })),
  http.get("/api/kinds", () => ok({ kinds: metaFixture.kinds, none_token: "(none)" })),
  http.get("/api/people", () =>
    ok({
      people: [
        { id: "person_alex", title: "Alex", aliases: ["gabe"], realm: "work" },
        { id: "person_maria", title: "Maria", aliases: ["mari"], realm: "work" },
      ],
    }),
  ),
  // A person id answers with a person; anything else with the task fixture.
  // One endpoint returning a task for `?id=person_…` is how a test ends up
  // asserting on a node that merely shares someone's name.
  http.get("/api/node", ({ request }) => {
    const id = new URL(request.url).searchParams.get("id") || "";
    return HttpResponse.json(id.startsWith("person_") ? personDetailFixture : nodeDetailFixture);
  }),
  http.get("/api/skills", () => HttpResponse.json(skillsFixture)),
  http.get("/api/scripts", () => HttpResponse.json(scriptsFixture)),
  http.get("/api/script", ({ request }) => {
    const name = new URL(request.url).searchParams.get("name") || "";
    const entry = scriptsFixture.scripts.find((x) => x.name === name);
    return entry
      ? ok({ script: { ...entry, text: `#!/bin/sh\n# ${entry.summary}\necho ${name}\n` } })
      : HttpResponse.json({ ok: false, error: "no such script" });
  }),
  http.get("/api/rule", ({ request }) => {
    const id = new URL(request.url).searchParams.get("id");
    return ok({ rule: { id, title: "a rule", body: "THE RULE\n\nDo the thing." } });
  }),
  http.get("/api/settings", () => HttpResponse.json(settingsFixture)),
  http.get("/api/rule-proposals", () => HttpResponse.json(ruleProposalsFixture)),
  http.get("/api/status", () => HttpResponse.json(statusFixture)),
  http.get("/api/trigger", () => HttpResponse.json(triggerFixture)),
  http.get("/api/agents", () => HttpResponse.json(agentsFixture)),
  http.get("/api/voice", () => HttpResponse.json(voiceFixture)),
  http.get("/api/agent-commands", () => HttpResponse.json(agentCommandsFixture)),
  http.get("/api/agent-conversations", () => HttpResponse.json(agentConversationsFixture)),
  http.get("/api/agent-thread", ({ request }) => {
    const id = new URL(request.url).searchParams.get("id") ?? "";
    const c = agentConversationsFixture.conversations.find((x) => x.id === id);
    if (id === agentThreadFixture.conversation.id) return HttpResponse.json(agentThreadFixture);
    return c
      ? HttpResponse.json({ ok: true, conversation: c, messages: [] })
      : HttpResponse.json({ ok: false, error: `no conversation ${id}` }, { status: 200 });
  }),
  http.get("/api/b2b", () => HttpResponse.json(b2bFixture)),
  http.get("/api/b2b-conversations", () => HttpResponse.json(b2bConversationsFixture)),
  http.get("/api/b2b-thread", ({ request }) => {
    const peer = new URL(request.url).searchParams.get("peer") ?? "";
    return HttpResponse.json(
      peer === b2bThreadFixture.peer_id ? b2bThreadFixture : { ok: true, peer_id: peer, messages: [], has_more: false, next_before: "" },
    );
  }),
  http.get("/api/tasks", () => HttpResponse.json(tasksFixture)),
  http.get("/api/task", () => HttpResponse.json(taskDetailFixture)),
  http.get("/api/usage-report", ({ request }) => {
    const q = new URL(request.url).searchParams;
    return HttpResponse.json(usageReportFixture(q.get("from") || "", q.get("to") || ""));
  }),
  http.get("/api/usage-summary", () =>
    HttpResponse.json({
      ok: true, estimated: true, fiveHours: 1.5, today: 4.25, week: 30, month: 120,
      year: 900, total: 1500, weeks: 40, coarseWeeks: 0,
    }),
  ),
  http.get("/api/timezones", () => ok({ zones: ["UTC", "America/Sao_Paulo", "Europe/Lisbon"] })),
  http.get("/api/capture", () => HttpResponse.json(captureFixture)),
  http.get("/api/doctor", () => HttpResponse.json(doctorFixture)),
  http.get("/api/backups", () => HttpResponse.json(backupsFixture)),
  http.get("/api/consolidate-preview", () => HttpResponse.json(consolidationPreviewFixture)),
  http.get("/api/person-duplicates", () => HttpResponse.json(personDuplicatesFixture)),
  // Nothing stored by default: the brain's copy is opt-in, and a test that did
  // not ask for synced preferences must see the same defaults a fresh browser
  // does. A test that wants one overrides this with server.use().
  http.get("/api/ui-prefs", () => ok({ synced: false, path: "/home/brain/ui_prefs.json", prefs: {} })),
  http.get("/api/search", ({ request }) => {
    const q = (new URL(request.url).searchParams.get("q") || "").toLowerCase();
    const hits = graphFixture.nodes
      .filter((n) => n.title.toLowerCase().includes(q))
      .map((n) => ({ id: n.id, title: n.title, type: n.type, realm: n.realm }));
    return ok({ results: hits });
  }),
  http.get("/api/suggest-links", ({ request }) => {
    const text = (new URL(request.url).searchParams.get("text") || "").toLowerCase();
    const suggestions = text.includes("alex")
      ? [
          {
            id: "person_alex",
            title: "Alex",
            type: "person",
            rel: "mentions",
            cue: "alex",
          },
        ]
      : [];
    return ok({ count: suggestions.length, suggestions });
  }),

  // the date views all answer from one fixture
  ...["agenda", "open", "today", "tomorrow", "week", "closed", "range"].map((v) =>
    http.get(`/api/${v}`, () => HttpResponse.json({ ...agendaFixture, view: v })),
  ),

  write("/api/add-task"),
  write("/api/set-due"),
  write("/api/complete"),
  write("/api/set-status"),
  write("/api/set-assignee"),
  write("/api/update-node"),
  write("/api/assign"),
  write("/api/link"),
  write("/api/unlink"),
  write("/api/propose-idea", (body: { title?: string; detail?: string; realm?: string }) =>
    ok({
      node: {
        id: "idea_new_0001", type: "idea", title: body.title, body: body.detail ?? "",
        status: "proposed", realm: body.realm || "work",
      },
    }),
  ),
  write("/api/settings"),
  write("/api/neo4j-config"),
  write("/api/neo4j-test"),
  write("/api/b2b-config", () => ok({ config: b2bFixture.config, runtime: b2bFixture.runtime })),
  write("/api/b2b-broker", () => ok({ config: b2bFixture.config })),
  write("/api/b2b-broker-check", (body: { id?: string }) =>
    ok({ broker: body.id, user_id: b2bFixture.config.user_id, session: { open: true, expires_at: "2026-09-30T18:00:00Z" } }),
  ),
  write("/api/b2b-identity", () => ok({ user_id: b2bFixture.config.user_id, note: "Key pair made." })),
  write("/api/b2b-peer", () => ok({ peers: b2bFixture.peers })),
  write("/api/b2b-allowed", () => ok({ allowed: b2bFixture.allowed })),
  http.get("/api/custom-triggers", () => HttpResponse.json(customTriggersFixture)),
  http.get("/api/custom-trigger", () => HttpResponse.json(customTriggerDetailFixture)),
  write("/api/custom-trigger", (body: { id?: string; check_only?: boolean; text?: string }) =>
    body.check_only
      ? ok({ id: body.id, valid: !(body.text ?? "").includes("interval: 1s"),
             errors: (body.text ?? "").includes("interval: 1s") ? ["interval must be at least 30s"] : [],
             warnings: [] })
      : ok({ id: body.id, saved: true, created: body.id !== "site-health", errors: [], warnings: [] }),
  ),
  write("/api/custom-trigger-dry-run", (body: { id?: string }) =>
    ok({
      id: body.id, dry_run: true, status: "ok", error: "",
      events: [{ event_id: "site-health:503", text: "site down: 503", meta: { payload: { status: 503 } } }],
      checkpoint: { now: "", would_be: null },
    }),
  ),
  write("/api/custom-trigger-enable", (body: { id?: string; enabled?: boolean }) =>
    ok({ id: body.id, enabled: body.enabled }),
  ),
  write("/api/trigger-reprocess", (body: { request_id?: string }) =>
    ok({ request_id: body.request_id, status: "queued", was: "failed" }),
  ),
  write("/api/trigger-cancel", (body: { request_id?: string }) =>
    ok({ request_id: body.request_id, status: "cancelled", was: "running" }),
  ),
  write("/api/agent-send", (body: { conversation?: string }) =>
    ok({
      conversation: agentConversationsFixture.conversations.find((c) => c.id === body.conversation) ??
        { ...agentConversationsFixture.conversations[2]!, id: "c9", title: "New one" },
      message_id: "u9",
      reply_id: "a9",
    }),
  ),
  write("/api/agent-attach", (body: { name?: string }) =>
    ok({ name: body.name, path: `20261007-101010-ab12-${body.name}`, kind: "img", size: 4 }),
  ),
  write("/api/agent-conversation", (body: { id?: string }) =>
    ok({ conversation: agentConversationsFixture.conversations.find((c) => c.id === body.id), created: false }),
  ),
  write("/api/agent-retry", (body: { id?: string }) => ok({ reply_id: body.id })),
  write("/api/agent-cancel", (body: { id?: string }) => ok({ reply_id: body.id, was: "running" })),
  write("/api/agent-plugin", () => HttpResponse.json(agentCommandsFixture)),
  write("/api/agent-config", () => HttpResponse.json(agentsFixture)),
  write("/api/voice-config", () => HttpResponse.json(voiceFixture)),
  write("/api/voice-translate", (body: { text?: string; target?: string }) =>
    ok({ text: `[${body.target}] ${body.text}`, translated: true, language: body.target }),
  ),
  write("/api/b2b-send", () =>
    ok({ id: "m2", to: "A".repeat(43), name: "Dana", broker: "b1", sent_at: "2026-09-30T10:00:00Z" }),
  ),
  write("/api/trigger-config", () => ok({ config: triggerFixture.config, runtime: triggerFixture.runtime })),
  write("/api/trigger-channel", () => ok({ channels: triggerFixture.channels })),
  write("/api/trigger-person", () => ok({ people: triggerFixture.people })),
  http.get("/api/trigger-people", () =>
    ok({ people: [{ user_id: "62f3dd0ce50f2f2a39573991", name: "Isadora", email: "isa@example.com" },
                  { user_id: "557058:cccc-dddd", name: "Isaac", email: "" }] }),
  ),
  write("/api/trigger-scan", (body: { dry_run?: boolean }) =>
    ok({
      matched: 2,
      dry_run: !!body.dry_run,
      matches: [
        { phrase: "{brain} will handle", speaker: "Alex", said: "Jarvis will handle this", is_owner: true,
          runs: true, ...(body.dry_run ? {} : { status: "queued", request_id: "r-1" }) },
        { phrase: "{brain} will handle", speaker: "Bo", said: "jarvis will handle it", is_owner: false,
          runs: false, ...(body.dry_run ? {} : { status: "refused", request_id: "r-2" }) },
      ],
    }),
  ),
  write("/api/trigger-check", (body: { only?: string }) =>
    ok({ ready: true, target: body.only || "all", problems: [], notes: [`${body.only}: fine`] }),
  ),
  write("/api/flush"),
  write("/api/prune", () =>
    ok({ keep_days: 2, days_consolidated: 1, newly_consolidated: 12, still_loose: 0,
         backup: { path: "/home/brain/backups/maintain-20260922T120000Z.db", bytes: 102400 } }),
  ),
  write("/api/consolidate"),
  write("/api/capture", () => ok({ capture: { ...captureFixture, enabled: false, marker_present: true, source: "marker" } })),
  write("/api/ui-prefs", () => ok({ synced: true })),
  write("/api/backup", () => ok({ created: backupsFixture.backups[0], removed: [], backups: backupsFixture.backups })),
  write("/api/merge-person", () => ok({ moved: 2, dropped: 0 })),
  write("/api/add-alias", () => ok({ added: true })),
  write("/api/create-rule"),
  write("/api/propose-rules", () => ok({ enabled: true, ran: true, proposed: [] })),
  write("/api/accept-rule", () => ok({ rule: "rule_learned_x", source: "learned", precedence: 50 })),
  write("/api/reject-rule", () => ok({ status: "rejected", kept: true })),
  write("/api/create-skill"),
  write("/api/create-process", () =>
    ok({ node: { id: "process_new_0001", realm: "work" }, action: "created" }),
  ),
  write("/api/export-skills", () => ok({ dir: "/home/brain/learned_skills", skills: 2, rules: 1 })),
  write("/api/ingest-skills", () => ok({ count: 2, ingested: [], rules: [] })),
  write("/api/export-bundle", () =>
    ok({ skills: 1, rules: 1, path: "/home/brain/bundles/b.zip", name: "b.zip", download: "/api/bundle?name=b.zip" }),
  ),
  write("/api/upload-bundle", () =>
    ok({
      token: "tok-1",
      counts: { skills: 1, rules: 0 },
      items: [{ id: "skill_new", title: "new skill", kind: "skill", exists: false }],
    }),
  ),
  write("/api/import-bundle", () => ok({ applied: [{ id: "skill_new", action: "created" }] })),
  write("/api/export-scripts", () =>
    ok({
      path: "/home/brain/bundles/scripts-1.zip",
      name: "scripts-1.zip",
      download: "/api/bundle?name=scripts-1.zip",
      count: 2,
      scripts: ["dev_env.py", "mithra_token.sh"],
    }),
  ),
  write("/api/create-script", () => ok({ name: "new.sh", action: "created" })),
  write("/api/upload-scripts", () =>
    ok({
      token: "tok-s",
      dir: "/home/brain/scripts",
      count: 2,
      items: [
        { ...scriptsFixture.scripts[0]!, exists: true, identical: false, existing_bytes: 100 },
        { ...scriptsFixture.scripts[1]!, name: "new.sh", exists: false },
      ],
    }),
  ),
  write("/api/import-scripts", () =>
    ok({
      dir: "/home/brain/scripts",
      applied: [
        { name: "dev_env.py", action: "kept" },
        { name: "new.sh", action: "created" },
      ],
      not_in_zip: [],
    }),
  ),
  write("/api/delete-contracts", () => ok({ count: 1, deleted: [{ id: "rule_x", type: "rule" }], refused: [], not_found: [] })),
];

export const server = setupServer(...handlers);

/** Makes one endpoint fail, for the "what does the UI say when it breaks" tests. */
export function failWith(method: "get" | "post", path: string, status: number, error: string) {
  server.use(
    http[method](path, () => HttpResponse.json({ ok: false, error }, { status })),
  );
}
