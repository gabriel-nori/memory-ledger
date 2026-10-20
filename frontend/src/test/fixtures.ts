/**
 * One brain, shared by every test.
 *
 * The shapes here are the shapes `serve.py` actually sends — they were read off
 * `brain_query._slim`, `graph.skill_trees` and `settings_payload`, and
 * `tests/test_api.py` pins those shapes against the real server.
 */
import type {
  BackupList,
  CaptureState,
  ConsolidationPreview,
  DoctorReport,
  GraphPayload,
  Meta,
  NodeDetail,
  PersonDuplicates,
  Settings,
  ScriptsPayload,
  SkillsPayload,
  Status,
  ViewPayload,
} from "@/api/types";

export const metaFixture: Meta = {
  ok: true,
  rels: ["relates-to", "blocked-by", "blocks", "part-of", "mentions", "caused-by"],
  statuses: ["open", "doing", "blocked", "waiting", "done", "cancelled"],
  realms: {
    work: { nodes: 42, open_tasks: 7 },
    personal: { nodes: 11, open_tasks: 2 },
  },
  default_realm: "work",
  kinds: {
    "(none)": { nodes: 50, open_tasks: 8 },
    postmortem: { nodes: 3, open_tasks: 1 },
  },
  no_kind_token: "(none)",
  types: { task: 9, person: 4, note: 6 },
  brain_name: "Jarvis",
  owner: "Alex",
  skill_sections: [
    { field: "when_to_use", heading: "## When to use", required: true, generated: false },
    { field: "given", heading: "## Given", required: true, generated: false },
    { field: "rules_honored", heading: "## Rules honored", required: false, generated: true },
    { field: "depends_on", heading: "## Depends on", required: false, generated: true },
    { field: "deliverables", heading: "## Deliverables", required: true, generated: false },
    { field: "judged_against", heading: "## Judged against", required: true, generated: false },
    { field: "guardrails", heading: "## Guardrails", required: true, generated: false },
    { field: "reply", heading: "## Reply", required: true, generated: false },
  ],
  rule_sections: [
    { field: "the_rule", heading: "THE RULE", required: true },
    { field: "why", heading: "WHY", required: false },
    { field: "how_to_apply", heading: "HOW TO APPLY", required: false },
    { field: "exceptions", heading: "EXCEPTIONS", required: false },
    { field: "judged_against", heading: "JUDGED AGAINST", required: true },
    { field: "related", heading: "RELATED", required: false },
  ],
  decl_labels: [
    { field: "requires", label: "Requires" },
    { field: "waits_for", label: "Waits for" },
    { field: "enriched_by", label: "Enriched by" },
    { field: "reads", label: "Reads" },
    { field: "writes", label: "Writes" },
  ],
  rule_sources: ["plugin", "user", "learned"],
  node_types: ["task", "person", "note", "decision", "skill"],
  writable: true,
  boot: "1234-1700000000",
};

export const graphFixture: GraphPayload = {
  ok: true,
  nodes: [
    { id: "task_ship-ui_a1", title: "Ship the new UI", type: "task", realm: "work", status: "open", degree: 2, kind: "(none)" },
    { id: "person_alex", title: "Alex", type: "person", realm: "work", degree: 3, kind: "(none)" },
    { id: "note_retro_b2", title: "Retro notes", type: "note", realm: "personal", degree: 1, kind: "postmortem" },
    { id: "task_done_c3", title: "Old finished thing", type: "task", realm: "work", status: "done", degree: 1, kind: "(none)" },
  ],
  edges: [
    { src: "person_alex", dst: "task_ship-ui_a1", rel: "relates-to" },
    { src: "task_ship-ui_a1", dst: "note_retro_b2", rel: "mentions" },
    { src: "person_alex", dst: "task_done_c3", rel: "relates-to" },
  ],
  meta: {
    count: 4,
    edges: 3,
    by_type: { task: 2, person: 1, note: 1 },
    by_realm: { work: 3, personal: 1 },
    by_kind: { "(none)": 3, postmortem: 1 },
    boot: "1234-1700000000",
    backend: "sqlite",
  },
};

export const agendaFixture: ViewPayload = {
  ok: true,
  view: "agenda",
  title: "Agenda",
  ref: "2026-09-22",
  count: 2,
  results: [
    {
      id: "task_ship-ui_a1",
      title: "Ship the new UI",
      type: "task",
      status: "open",
      realm: "work",
      due: "2026-09-20T12:00:00",
      due_local: "2026-09-20",
      assignee: "alex",
      kind: "(none)",
      mine: true,
      blocked_by: [{ id: "task_review_d4", title: "Review it" }],
      blocks: [],
      related: [],
    },
    {
      id: "task_write-docs_e5",
      title: "Write the docs",
      type: "task",
      status: "doing",
      realm: "work",
      due: "",
      due_local: "--",
      assignee: "",
      kind: "(none)",
      mine: false,
      blocked_by: [],
      blocks: [],
      related: [],
    },
  ],
};

export const nodeDetailFixture: NodeDetail = {
  ok: true,
  id: "task_ship-ui_a1",
  type: "task",
  title: "Ship the new UI",
  body: "The whole viz, as a real app.",
  status: "open",
  realm: "work",
  due: "2026-09-20T12:00:00",
  props: { assignee: "alex", priority: "P2" },
  edges: [
    { dir: "in", rel: "relates-to", id: "person_alex", title: "Alex", type: "person" },
    { dir: "out", rel: "mentions", id: "note_retro_b2", title: "Retro notes", type: "note" },
  ],
};

/**
 * A PERSON node with its neighbourhood — what `/api/node?id=person_…` answers.
 *
 * Separate from nodeDetailFixture, which is a task: a person's card is about
 * what they are involved in, and reusing a task's edges made the test assert on
 * a node that happened to share the person's name.
 */
export const personDetailFixture: NodeDetail = {
  ok: true,
  id: "person_alex",
  type: "person",
  title: "Alex",
  body: "",
  realm: "work",
  props: { aliases: ["gabe"] },
  edges: [
    { dir: "in", rel: "assigned-to", id: "task_ship-ui_a1", title: "Ship the new UI", type: "task" },
    { dir: "in", rel: "assigned-to", id: "task_docs_c3", title: "Write the docs", type: "task" },
    { dir: "in", rel: "mentions", id: "note_retro_b2", title: "Retro notes", type: "note" },
  ],
};

export const skillsFixture: SkillsPayload = {
  ok: true,
  trees: [
    {
      kind: "plugin",
      hub: "hub_skills_plugin",
      title: "Built-in skills",
      count: 2,
      skills: [
        {
          id: "skill_recall",
          title: "recall",
          description: "Read the graph back.",
          triggers: "what do I know about",
          kind: "plugin",
          uses: 12,
          requires: [],
          waits_for: [],
          enriched_by: ["skill_ingest-context"],
          reads: ["task", "person"],
          writes: [],
          reads_other: "",
          writes_other: "",
          domains: ["hub_process"],
          declared: true,
          rules: [
            { id: "rule_s2_the-memory-graph-api", title: "The memory graph API", why: "declared", non_negotiable: false },
            { id: "rule_s0_always-on-capture", title: "Always-on capture", why: "always", non_negotiable: true },
          ],
          children: [
            {
              id: "skill_visualize",
              title: "visualize",
              description: "Open the live brain UI.",
              triggers: "show me the graph",
              kind: "plugin",
              uses: 3,
              requires: ["skill_recall"],
              waits_for: [],
              enriched_by: [],
              reads: ["task"],
              writes: [],
              reads_other: "",
              writes_other: "",
              domains: ["hub_process"],
              declared: false,
              rules: [],
              children: [],
            },
          ],
        },
      ],
    },
    {
      kind: "learned",
      hub: "hub_skills_learned",
      title: "Learned skills",
      count: 0,
      skills: [],
    },
  ],
  rules: [
    {
      id: "rule_user_plain-text-only",
      title: "Never use emoji",
      source: "user",
      section: "user",
      non_negotiable: true,
      scenario: "writing anything for a person",
      domains: ["hub_writing"],
      chars: 420,
    },
    {
      id: "rule_s2_the-memory-graph-api",
      title: "The memory graph API",
      source: "plugin",
      section: "2",
      non_negotiable: false,
      scenario: "reading or writing the graph",
      domains: ["hub_process"],
      chars: 900,
    },
  ],
  node_types: [
    { type: "task", description: "Something someone has to do.", count: 9, readers: ["skill_recall"], writers: [] },
    { type: "person", description: "A human.", count: 4, readers: ["skill_recall"], writers: [] },
  ],
  areas: { hub_process: "Process", hub_writing: "Writing" },
  realm: "work",
  processes: [
    {
      id: "process_epic-refinement_854d",
      title: "Epic-lead refinement process",
      body: "1. CATCH UP FIRST.\n2. Refine each child.",
      realm: "work",
      trigger: "refine the issues in my epics",
      established: "2026-09-09",
      category: "process",
      domains: ["hub_process"],
      edges: 3,
      updated: "2026-09-10T10:00:00+00:00",
    },
    {
      id: "process_weekly-review_aa11",
      title: "Weekly personal review",
      body: "Look back at the week.",
      realm: "personal",
      trigger: "sunday evening",
      established: "",
      category: "process",
      domains: ["hub_process"],
      edges: 1,
      updated: "2026-09-01T10:00:00+00:00",
    },
  ],
};

export const settingsFixture: Settings = {
  ok: true,
  writable: true,
  keys: [
    "tz",
    "realm",
    "owner",
    "backend",
    "autosync",
    "consolidate",
    "map_poll_seconds",
    "dash_refresh_seconds",
    "ui_sync",
    "learned_rules",
    "prompt_recall",
    "agents_sync",
  ],
  settings: { tz: "America/Sao_Paulo", realm: "work" },
  env: {},
  path: "/home/brain/settings.json",
  effective: {
    tz: "America/Sao_Paulo",
    tz_source: "settings",
    system_tz: "UTC",
    now: "2026-09-22T09:00:00-03:00",
    realm: "work",
    owner: "Alex",
    owner_source: "default",
    brain_name: "Jarvis",
    brain_name_source: "settings",
    backend: "auto",
    backends: ["sqlite", "neo4j", "auto"],
    autosync: true,
    autosync_source: "default",
    consolidate: true,
    consolidate_source: "default",
    ui_sync: false,
    ui_sync_source: "default",
    learned_rules: false,
    learned_rules_source: "default",
    prompt_recall: false,
    prompt_recall_source: "default",
    agents_sync: false,
    agents_sync_source: "default",
    map_poll_seconds: 2,
    dash_refresh_seconds: 120,
  },
  capture: {
    enabled: true,
    marker: "/home/brain/.disabled",
    marker_present: false,
    env_off: false,
    source: "on",
  },
};

export const captureFixture: CaptureState = settingsFixture.capture;

export const doctorFixture: DoctorReport = {
  ok: true,
  healthy: true,
  problems: [],
  status: { ok: true, enabled: true, disabled: false },
  paths: { ok: true, home: "/home/brain", db: "/home/brain/graph.db", db_exists: true },
  config: { ok: true, tz: "America/Sao_Paulo", realm: "work", owner: "Alex" },
  content: { ok: true, nodes: 4, open_loops: 2 },
  hooks: { ok: true, hooks_json: "/home/.claude/settings.json", events: {} },
  mirror: { ok: true, configured: false },
};

export const consolidationPreviewFixture: ConsolidationPreview = {
  ok: true,
  dry_run: true,
  keep_days: 2,
  cutoff: "2026-09-20",
  still_loose: 12,
  backup_keep_days: 5,
  days: [
    {
      day: "2026-09-19",
      digest: "note_digest_2026-09-19",
      digest_exists: false,
      action: "create",
      captures: 12,
      prompts: 6,
      newly_consolidated: 12,
      already_consolidated: 0,
      chars_behind_it: 4200,
    },
  ],
  totals: {
    captures: 12,
    new_digests: 1,
    updated_digests: 0,
    newly_consolidated: 12,
    chars: 4200,
  },
};

export const backupsFixture: BackupList = {
  ok: true,
  dir: "/home/brain/backups",
  keep_days: 5,
  backups: [
    {
      name: "maintain-20260922T120000Z.db",
      path: "/home/brain/backups/maintain-20260922T120000Z.db",
      bytes: 102400,
      created: "2026-09-22T12:00:00+00:00",
      expires: "2026-09-27T12:00:00+00:00",
      expired: false,
    },
  ],
};

export const personDuplicatesFixture: PersonDuplicates = {
  ok: true,
  duplicates: [
    {
      canonical: { id: "person_maria", title: "Maria", edges: 9 },
      duplicates: [{ id: "person_isa", title: "Mari", edges: 2, confidence: "likely" }],
    },
  ],
};

export const statusFixture: Status = {
  ok: true,
  writable: true,
  boot: "1234-1700000000",
  db: "/home/brain/graph.db",
  home: "/home/brain",
  nodes: 4,
  edges: 3,
  pending_ops: 0,
  loose_captures: 1,
  neo4j: {
    url: "",
    db: "neo4j",
    user: "neo4j",
    password_set: false,
    configured: false,
    up: false,
    reading_from: "sqlite (local mirror)",
    file: "/home/brain/neo4j.json",
    env: [],
  },
};

/** One event's setting, the Claude action with an empty API call and script. */
function ev(on: boolean, run: string): import("@/api/types").TriggerEventSetting {
  return {
    on, do: run, action: "claude",
    http: { method: "POST", url: "", headers: {}, body: "" },
    script: { name: "", args: [] },
  };
}

export const triggerFixture: import("@/api/types").TriggerStatus = {
  ok: true,
  watching: true,
  config: {
    path: "/home/brain/trigger.json",
    exists: true,
    problem: "",
    enabled: true,
    stored_enabled: true,
    signature: "_sent by Alex's second brain_",
    signature_template: "_sent by {owner}'s second brain_",
    refusals: {
      not_enabled: "I'm not enabled in this conversation, so I did nothing.",
      policy: "I can't do that: it goes against a rule {owner} has set for me. {reason}",
      not_allowed: "You can't ask me that here, so I did nothing.",
    },
    refuse_without_model: true,
    translate_notices: true,
    claude: {
      bin: "claude", notice_model: "haiku", mcp_config: "", strict_mcp_config: false, timeout_s: 900,
      max_concurrent: 3, extra_args: [], cwd: "",
    },
    slack: {
      enabled: true, active: true, prompt: "", owner_user_id: "U0OWNER", thread_lookback_days: 7,
      allowed_users: ["U0FRIEND"], blocked_users: [], dms: false, dms_open: false,
      events: {
        mention: ev(true, ""),
        joined_channel: ev(false, "Note who added me to #{slack_channel_name}."),
        left_channel: ev(false, "Note that I was removed."),
      },
      commands: [
        {
          ...ev(true, ""), name: "deploy", anyone: false, pattern: "", description: "Ship a build",
          action: "script", script: { name: "dev_env.py", args: [] },
        },
      ],
    },
    jira: {
      enabled: false, active: false, prompt: "", base_url: "https://j.example", email: "me@example.com",
      owner_account_id: "", marker: "@brain", poll_seconds: 60, jql: "", max_catchup_hours: 72,
      watch_jql: "assignee = currentUser()",
      events: {
        mention: ev(true, ""),
        transitioned: ev(false, "Note {jira_from} to {jira_to}."),
        assigned: ev(false, "Add a task."),
        unassigned: ev(false, "Note it."),
        comment: ev(false, "Answer as a task."),
      },
    },
    github: {
      enabled: false, active: false, api_url: "https://api.github.com", login: "", marker: "/brain",
      poll_seconds: 120, repos: ["acme/api"], trusted_associations: ["OWNER", "MEMBER", "COLLABORATOR"],
      refusal_notice: false, max_catchup_hours: 72,
      events: {
        mention: ev(true, ""),
        review_requested: ev(true, "Add a task to my brain to review it."),
        review: ev(true, "List each change asked for."),
        comment: ev(false, "Answer questions as a task."),
        pushed: ev(false, "Note what changed."),
        ci_failed: ev(false, "Find the likely cause."),
        merged: ev(false, "Close my task."),
        closed: ev(false, "Note who closed it."),
        assigned: ev(false, "Add a task."),
      },
    },
    meeting: {
      enabled: true,
      active: true,
      brain_name: "Jarvis",
      default_do: "Work out what they put on {brain}, and do it now.",
      phrases: [
        {
          phrase: "{brain} will handle",
          anyone: false,
          description: "",
          on: true,
          do: "",
          action: "claude",
          http: { method: "POST", url: "", headers: {}, body: "" },
          script: { name: "", args: [] },
        },
      ],
    },
    secrets: {
      "slack.app_token": { set: true, stored: true, source: "file", env_var: "MEMORY_LEDGER_SLACK_APP_TOKEN" },
      "slack.bot_token": { set: true, stored: false, source: "env", env_var: "MEMORY_LEDGER_SLACK_BOT_TOKEN" },
      "slack.user_token": { set: false, stored: false, source: "", env_var: "MEMORY_LEDGER_SLACK_USER_TOKEN" },
      "jira.token": { set: false, stored: false, source: "", env_var: "MEMORY_LEDGER_JIRA_TOKEN" },
      "github.token": { set: false, stored: false, source: "", env_var: "MEMORY_LEDGER_GITHUB_TOKEN" },
    },
    variables: [
      { name: "trigger_source", sources: ["slack", "jira", "github"], global: true, description: "Where it came from.", example: "github" },
      { name: "trigger_user", sources: ["slack", "jira", "github"], global: true, description: "Who caused it.", example: "octocat" },
      { name: "trigger_link", sources: ["slack", "jira", "github"], global: true, description: "A link to it.", example: "https://github.com/o/r/pull/5" },
      { name: "github_repo", sources: ["github"], global: false, description: "owner/name.", example: "o/r" },
      { name: "jira_from", sources: ["jira"], global: false, description: "The status it left.", example: "In Progress" },
      { name: "jira_to", sources: ["jira"], global: false, description: "The status it reached.", example: "In Review" },
      { name: "arg_1", sources: ["slack"], global: false, description: "Argument 1.", example: "staging" },
    ],
    actions: ["claude", "http", "script"],
    http_methods: ["GET", "POST", "PUT", "PATCH", "DELETE"],
    scripts_dir: "/home/brain/scripts",
  },
  runtime: {
    running: true, reason: "", started_at: "2026-09-23T12:00:00+00:00",
    slack: { state: "connected" },
    jira: { state: "off" },
    github: { state: "off" },
  },
  counts: { done: 3, refused: 1 },
  enabled_scopes: [{ scope: "slack:C1:1.0", by: "U0OWNER", at: "2026-09-23T12:00:00+00:00" }],
  channels: {
    active: [
      { channel_id: "C0GENERAL", name: "general", added_by: "web-ui", added_at: "2026-09-20T10:00:00+00:00", removed_by: "", removed_at: "" },
      { channel_id: "C0OPS", name: "ops", added_by: "web-ui", added_at: "2026-09-21T10:00:00+00:00", removed_by: "", removed_at: "" },
    ],
    removed: [
      { channel_id: "C0OLD", name: "old-project", added_by: "cli", added_at: "2026-09-01T10:00:00+00:00", removed_by: "web-ui", removed_at: "2026-09-10T10:00:00+00:00" },
    ],
  },
  people: {
    active: [
      { source: "jira", user_id: "62f3dd0ce50f2f2a39573991", name: "Isadora", added_by: "web-ui", added_at: "2026-09-25T10:00:00+00:00", removed_by: "", removed_at: "" },
    ],
    removed: [
      { source: "jira", user_id: "557058:aaaa-bbbb", name: "Former", added_by: "cli", added_at: "2026-09-01T10:00:00+00:00", removed_by: "web-ui", removed_at: "2026-09-10T10:00:00+00:00" },
    ],
  },
  recent: [
    {
      id: "0b7c6f2e-1111-4222-8333-944455556666", source: "slack", scope: "slack:C1:1.0",
      requester_name: "Maria", requester_id: "U2", status: "done",
      received_at: "2026-09-23T12:01:00+00:00", text: "can you summarise this thread?",
      permalink: "https://x.slack.com/p1",
    },
  ],
};

export const scriptsFixture: ScriptsPayload = {
  ok: true,
  dir: "/home/brain/scripts",
  exists: true,
  count: 2,
  skipped: [],
  scripts: [
    {
      name: "dev_env.py",
      bytes: 14127,
      executable: true,
      sha256: "a".repeat(64),
      language: "python",
      summary: "Is the dev environment usable right now.",
      modified: "2026-09-18T19:21:54+00:00",
    },
    {
      name: "mithra_token.sh",
      bytes: 2644,
      executable: true,
      sha256: "b".repeat(64),
      language: "shell",
      summary: "Mint a token without the slash command.",
      modified: "2026-09-23T15:23:30+00:00",
    },
  ],
};

const taskBase = {
  scope: "slack:C1:1.1",
  event_id: "C1:1.1",
  requester_id: "U1",
  is_owner: false,
  permalink: "https://slack.example/p1",
  reason: "",
  node_id: "",
  exit_code: null,
  cost_usd: null,
  session_id: "",
  cwd: "",
  kind: "mention",
  action: "claude",
  result: "",
  started_at: "",
  finished_at: "",
  actions: 0,
};

export const tasksFixture: import("@/api/types").TasksPayload = {
  ok: true,
  now: "2026-09-24T12:10:00+00:00",
  total: 3,
  limit: 500,
  truncated: false,
  counts: { running: 1, done: 1, failed: 1 },
  tasks: [
    {
      ...taskBase,
      id: "t-running",
      source: "slack",
      requester_name: "Maria",
      status: "running",
      name: "mention",
      text: "summarise the release thread",
      received_at: "2026-09-24T12:08:00+00:00",
      started_at: "2026-09-24T12:08:01+00:00",
      session_id: "s-1",
      progress: {
        tool_calls: 3,
        last_tool: "Bash",
        last_input: '{"command": "ls"}',
        last_at: "",
        subagents: 2,
        subagents_running: 1,
        actions: 1,
        last_action: { kind: "reply", target: "https://slack.example/r", at: "2026-09-24T12:09:00+00:00" },
        last_message: "",
      },
    },
    {
      ...taskBase,
      id: "t-done",
      source: "github",
      requester_name: "tomas",
      status: "done",
      kind: "review_requested",
      name: "Duty Cards: report the route-group error",
      text: "tomas asked you to review acme/widgets#7625",
      received_at: "2026-09-24T11:40:00+00:00",
      started_at: "2026-09-24T11:40:01+00:00",
      finished_at: "2026-09-24T11:45:00+00:00",
      cost_usd: 1.25,
      result: "posted the review",
    },
    {
      ...taskBase,
      id: "t-failed",
      source: "jira",
      requester_name: "Alex",
      is_owner: true,
      status: "failed",
      name: "mention",
      text: "triage ABC-1",
      received_at: "2026-09-24T10:00:00+00:00",
      result: "it broke",
      cost_usd: 1.234,
      cost_estimated: true,
    },
    {
      ...taskBase,
      id: "t-blocked",
      source: "b2b",
      requester_name: "Stranger",
      requester_id: "E".repeat(43),
      is_owner: false,
      status: "blocked",
      reason: "b2b: not on the allowed list",
      name: "message",
      text: "hello from nowhere",
      received_at: "2026-09-23T10:00:00+00:00",
      result: "",
      cost_usd: null,
      cost_estimated: false,
    },
  ],
};

export const taskDetailFixture: import("@/api/types").TaskDetail = {
  ok: true,
  task: {
    ...tasksFixture.tasks[0],
    meta: { kind: "mention", channel: "C1" },
    command: ["claude", "-p", "You were summoned by a mention.", "--output-format", "json", "--session-id", "s-1"],
    cwd: "/home/brain",
    stderr: "",
  },
  activity: {
    session_id: "s-1",
    conversation: { id: "conversation_s-1", status: "open" },
    agents: [
      { id: "agent_main", role: "main", status: "open", outcome: "", started_at: "2026-09-24T12:08:02+00:00", ended_at: "", depth: 1, tokens_out: null, brief: "" },
      { id: "agent_sub", role: "pr-reviewer", status: "open", outcome: "", started_at: "2026-09-24T12:08:30+00:00", ended_at: "", depth: 2, tokens_out: null, brief: "review the PR" },
    ],
    subagents_running: 1,
    messages: 1,
    actions: 1,
    last_action: { kind: "reply", target: "https://slack.example/r", at: "2026-09-24T12:09:00+00:00" },
    last_message: "",
    updated: "2026-09-24T12:09:00+00:00",
    timeline: [
      { at: "2026-09-24T12:08:02+00:00", kind: "session", title: "session started" },
      { at: "2026-09-24T12:08:30+00:00", kind: "agent", title: "subagent pr-reviewer started", detail: "review the PR" },
      { at: "2026-09-24T12:09:00+00:00", kind: "action", title: "reply", target: "https://slack.example/r", detail: "answered" },
    ],
  },
  actions: [{ id: 1, request_id: "t-running", at: "2026-09-24T12:09:00+00:00", kind: "reply", target: "https://slack.example/r", detail: "answered" }],
  transcript: {
    found: true,
    path: "/home/.claude/projects/-home-brain/s-1.jsonl",
    tool_calls: 1,
    truncated: false,
    of: 2,
    events: [
      { kind: "text", role: "assistant", at: "", text: "Reading the thread now." },
      { kind: "tool", at: "2026-09-24T12:08:05Z", id: "tu1", name: "mcp__slack__read_thread", input: '{"channel": "C1"}', result: "THREAD: release is friday", is_error: false, done_at: "2026-09-24T12:08:06Z" },
    ],
  },
};

/** `/api/usage-report`, as scripts/usage_report.py answers it. The html is a
 *  stand-in: the real page is pinned by tests/test_usage_report.py. */
export const usageWeeksFixture: import("@/api/types").UsageWeek[] = [
  { week: "2026-W01", from: "2025-12-29", to: "2026-01-04", costUSD: 100, sessions: 10 },
  { week: "2026-W02", from: "2026-01-05", to: "2026-01-11", costUSD: 300, sessions: 20 },
  { week: "2026-W03", from: "2026-01-12", to: "2026-01-18", costUSD: 200, sessions: 10 },
];

export function usageReportFixture(from = "", to = ""): import("@/api/types").UsageReportPayload {
  const selected = usageWeeksFixture
    .map((w) => w.week)
    .filter((w) => (!from || w >= from) && (!to || w <= to));
  return {
    ok: true,
    weeks: usageWeeksFixture,
    selected,
    html: `<!doctype html><html><body><h1>Where the Quota Went</h1><p>${selected.join(",")}</p></body></html>`,
  };
}

export const ruleProposalsFixture = {
  ok: true,
  enabled: true,
  status: "proposed",
  count: 1,
  of: 1,
  budget: 2600,
  chars: 300,
  truncated: false,
  hint: "",
  proposals: [
    {
      id: "proposal_0123456789",
      status: "proposed",
      title: "Learned: no, never use emojis in commit messages",
      text: "no, never use emojis in commit messages",
      count: 3,
      sessions: 3,
      evidence: [{ id: "capture_a", snippet: "No, never use emojis in commit messages" }],
    },
  ],
};

export const b2bFixture: import("@/api/types").B2BStatus = {
  ok: true,
  watching: true,
  config: {
    path: "/home/brain/b2b.json",
    exists: true,
    problem: "",
    enabled: true,
    active: true,
    user_id: "MCowBQYDK2VwAyEAmyOwnPublicKeyAaaaaaaaaaaaaaaaaaaaa",
    private_key: { set: true, source: "file", env_var: "MEMORY_LEDGER_B2B_PRIVATE_KEY" },
    brokers: [
      {
        id: "b1",
        name: "Optibus broker",
        url: "https://broker.example.com",
        enabled: true,
        session: { open: true, expires_at: "2026-09-30T18:00:00Z" },
      },
      { id: "b2", name: "Lab broker", url: "https://lab.example.com", enabled: false, session: { open: false } },
    ],
    poll_seconds: 60,
    do: "{b2b_peer} wrote: {b2b_content}",
    action: "claude",
    http: { url: "", method: "POST", headers: {}, body: "" },
    script: { name: "", args: [] },
    variables: { b2b_peer: "who sent it", b2b_content: "what they wrote" },
  },
  peers: [
    {
      peer_id: "A".repeat(43),
      name: "Dana",
      added_at: "2026-09-01T09:00:00Z",
      removed_at: null,
      broker_id: "b1",
      person_id: "person_alex",
      person_title: "Alex",
    },
    {
      peer_id: "B".repeat(43),
      name: "Old friend",
      added_at: "2026-08-01T09:00:00Z",
      removed_at: "2026-09-10T09:00:00Z",
      broker_id: "b1",
      person_id: "",
      person_title: "",
    },
  ],
  allowed: [
    { user_id: "A".repeat(43), name: "Dana", added_at: "2026-09-01T09:00:00Z", removed_at: "", is_peer: true },
    { user_id: "D".repeat(43), name: "Spammer", added_at: "2026-08-01T09:00:00Z", removed_at: "2026-09-12T09:00:00Z", is_peer: false },
  ],
  sent: [
    {
      id: "m1",
      peer_id: "A".repeat(43),
      text: "Can you review my PR?",
      sent_at: "2026-09-29T10:00:00Z",
      request_id: "r1",
      broker_id: "b1",
    },
  ],
  received: { done: 2 },
  runtime: { state: "polling", last_ok: "2026-09-30T09:00:00Z", every_s: 60 },
};

export const b2bConversationsFixture: import("@/api/types").B2BConversations = {
  ok: true,
  conversations: [
    {
      peer_id: "A".repeat(43),
      name: "Dana",
      person_id: "person_alex",
      connected: true,
      allowed: true,
      messages: 2,
      last: { direction: "in", text: "Sure, after lunch.", at: "2026-09-29T11:00:00Z", human: true },
    },
    {
      peer_id: "D".repeat(43),
      name: "Spammer",
      person_id: "",
      connected: false,
      allowed: false,
      messages: 1,
      last: { direction: "in", text: "buy now", at: "2026-09-20T08:00:00Z", human: false },
    },
    {
      peer_id: "E".repeat(43),
      name: "Eli",
      person_id: "",
      connected: true,
      allowed: true,
      messages: 0,
      last: null,
    },
  ],
};

export const b2bThreadFixture: import("@/api/types").B2BThread = {
  ok: true,
  peer_id: "A".repeat(43),
  messages: [
    {
      id: "m1",
      peer_id: "A".repeat(43),
      direction: "out",
      text: "Can you review my PR?",
      at: "2026-09-29T10:00:00Z",
      human: false,
      status: "sent",
      request_id: "r1",
    },
    {
      id: "m9",
      peer_id: "A".repeat(43),
      direction: "in",
      text: "Sure, after lunch.",
      at: "2026-09-29T11:00:00Z",
      human: true,
      status: "done",
      request_id: "r9",
    },
  ],
  has_more: false,
  next_before: "",
};

// ------------------------------------------------------------------ agents
const agentConnectors: import("@/api/types").AgentConnector[] = [
  {
    id: "claude", name: "Claude", kind: "claude", enabled: true, bin: "claude", timeout_s: 900,
    models: ["claude-opus-5-5", "claude-sonnet-5-5"], default_model: "claude-sonnet-5-5",
    available: true, problem: "",
  },
  {
    id: "ollama", name: "Ollama", kind: "ollama", enabled: true, url: "http://127.0.0.1:11434",
    timeout_s: 600, max_rounds: 8, models: ["llama3:latest"], default_model: "llama3:latest",
    available: true, problem: "",
  },
];

export const agentsFixture: import("@/api/types").AgentsStatus = {
  ok: true,
  config: { default_connector: "claude", context_turns: 1, recall_limit: 8, recall_budget: 2600, max_concurrent: 2 },
  connectors: agentConnectors,
  running: 0,
  question_mark: "[[needs-answer]]",
};

const conv = (id: string, title: string, extra: Partial<import("@/api/types").AgentConversation> = {}) => ({
  id, title, created_at: "2026-09-30T10:00:00Z", updated_at: "2026-09-30T10:00:00Z",
  connector: "claude", model: "claude-sonnet-5-5", full_tools: false, context_turns: 1,
  hidden: false, draft: "", draft_at: "", node_id: "", messages: 2, cost_usd: 0.0123, cost_estimated: false,
  tokens_in: 1000, tokens_out: 50, last: null, state: "done" as const, ...extra,
});

export const agentConversationsFixture: import("@/api/types").AgentConversations = {
  ok: true,
  conversations: [
    conv("c1", "Parking permit", { state: "question", last: { role: "agent", text: "Should I add a task?", at: "2026-09-30T10:01:00Z" } }),
    conv("c2", "Release notes", { state: "pending" }),
    conv("c3", "Trip plan", { state: "done" }),
    conv("c4", "Old idea", { hidden: true }),
  ],
};

export const agentThreadFixture: import("@/api/types").AgentThread = {
  ok: true,
  conversation: conv("c1", "Parking permit", { state: "question" }),
  messages: [
    {
      id: "u1", conv_id: "c1", role: "user", text: "What do I have to renew?", at: "2026-09-30T10:00:00Z",
      status: "done", reply_to: "", connector: "", model: "", full_tools: false, events: [], context: [],
      error: "", session_id: "", cost_usd: null, cost_estimated: false, tokens_in: 0, tokens_out: 0,
      started_at: "", finished_at: "", node_id: "", next_action: "",
    },
    {
      id: "a1", conv_id: "c1", role: "agent", text: "The parking permit, by Oct 5. Should I add a task?",
      at: "2026-09-30T10:00:01Z", status: "question", reply_to: "u1", connector: "claude",
      model: "claude-sonnet-5-5", full_tools: false,
      events: [
        { id: "x1", kind: "text", name: "said", output: "Let me look for permits.", error: false },
        { id: "t1", kind: "tool", name: "Grep", tool: "Grep", input: '{"pattern":"permit"}', output: "graph.db: 1 match", error: false },
        { id: "t2", kind: "skill", name: "recall", tool: "Skill", input: "{}", output: "ok", error: false },
        { id: "x2", kind: "text", name: "said", output: "The parking permit, by Oct 5. Should I add a task?", error: false },
      ],
      context: ["task_renew-the-optibus-parking-permit_7ba4"], error: "", session_id: "s1",
      cost_usd: 0.0123, cost_estimated: false, tokens_in: 1000, tokens_out: 50,
      started_at: "2026-09-30T10:00:01Z", finished_at: "2026-09-30T10:00:09Z", node_id: "", next_action: "",
    },
  ],
};

export const agentCommandsFixture: import("@/api/types").AgentCommands = {
  ok: true,
  commands: [
    { name: "memory-ledger:recall", plugin: "memory-ledger", kind: "skill", description: "Retrieve things from the memory graph", writes: false },
    { name: "memory-ledger:send-slack", plugin: "memory-ledger", kind: "command", description: "Send or schedule a Slack message", writes: true },
    { name: "ccalyze", plugin: "", kind: "skill", description: "Analyze Claude Code usage and cost", writes: false },
  ],
  plugins: [
    { key: "memory-ledger@memory-ledger", name: "memory-ledger", version: "0.9.0", enabled: true, commands: 2 },
    { key: "chrome-devtools-mcp@chrome-devtools-plugins", name: "chrome-devtools-mcp", version: "0.21.0", enabled: false, commands: 6 },
  ],
};

export const voiceFixture: import("@/api/types").VoiceStatus = {
  ok: true,
  config: {
    stt_language: "pt",
    stt_auto: false,
    tts_language: "pt",
    tts_auto: true,
    translate: false,
    translate_target: "en",
    tts_enabled: false,
    tts_background: false,
    silence_s: 5,
    tts_voices: {},
    verbosity: "default",
  },
  stt: { available: true, model: "medium", problem: "" },
  tts: {
    available: true,
    engine: "say",
    problem: "",
    voices: [
      { id: "Luciana", name: "Luciana", language: "pt" },
      { id: "Samantha", name: "Samantha", language: "en" },
    ],
  },
};

export const customTriggersFixture: import("@/api/types").CustomTriggerList = {
  ok: true,
  dir: "/home/alex/.second_brain/triggers",
  active: true,
  secrets: ["STATUS_TOKEN"],
  template: "id: my-trigger\ninterval: 5m\nblocks:\n  - id: alert\n    type: emit\n    event:\n      id: my-trigger:fixed\n",
  triggers: [
    {
      id: "site-health",
      file: "/home/alex/.second_brain/triggers/site-health.yaml",
      ok: true,
      name: "Site health",
      enabled: false,
      interval: "5m",
      blocks: 3,
      state: { last_run_at: "2026-10-12T21:00:00+00:00", last_status: "ok", last_error: "" },
    },
    {
      id: "broken",
      file: "/home/alex/.second_brain/triggers/broken.yaml",
      ok: false,
      error: "broken.yaml: not valid YAML",
      state: { last_status: "invalid", last_error: "broken.yaml: not valid YAML" },
    },
  ],
};

export const customTriggerDetailFixture: import("@/api/types").CustomTriggerDetail = {
  ok: true,
  id: "site-health",
  text: "id: site-health\nname: Site health\nenabled: false\ninterval: 5m\nblocks: []\n",
  errors: [],
  warnings: ["the last block is not an emit"],
  state: { last_status: "ok" },
  checkpoint: "",
  runs: [
    {
      run_id: "run-1",
      started_at: "2026-10-12T21:00:00+00:00",
      finished_at: "2026-10-12T21:00:01+00:00",
      status: "ok",
      attempts: 1,
      events: 1,
      created: 1,
      error: "",
    },
  ],
};
