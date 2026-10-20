/**
 * The wire shapes of the memory-ledger HTTP API.
 *
 * These mirror what the server sends. Its OpenAPI document (`/api/openapi.json`)
 * is generated from the routes at runtime, and `tests/test_api.py` pins the
 * response shapes and the route inventory against the real server — so a route
 * that grows or changes shape fails CI rather than quietly drifting from these.
 */

/** Every response carries `ok`; a failure carries `error` with it. */
export interface Envelope {
  ok: boolean;
  error?: string;
}

// ---------------------------------------------------------------------------
// Graph / map
// ---------------------------------------------------------------------------

export interface GraphNode {
  id: string;
  title: string;
  type: string;
  realm: string;
  status?: string;
  due?: string;
  assignee?: string;
  body?: string;
  kind?: string;
  degree?: number;
  props?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface GraphEdge {
  src: string;
  dst: string;
  rel: string;
}

export interface GraphMeta {
  count?: number;
  edges?: number;
  by_type?: Record<string, number>;
  by_realm?: Record<string, number>;
  by_kind?: Record<string, number>;
  boot?: string;
  backend?: string;
  /** The person node the owner setting resolves to — the map's centre. */
  ego?: string;
  error?: string;
}

export interface GraphPayload extends Envelope {
  nodes: GraphNode[];
  edges: GraphEdge[];
  meta: GraphMeta;
}

// ---------------------------------------------------------------------------
// Vocabulary
// ---------------------------------------------------------------------------

export interface SectionShape {
  field: string;
  heading: string;
  required: boolean;
  generated?: boolean;
}

export interface DeclLabel {
  field: string;
  label: string;
}

export interface Meta extends Envelope {
  rels: string[];
  statuses: string[];
  /** realm -> two counts. A map, because it is read by name everywhere. */
  realms: Record<string, RealmCount>;
  default_realm: string;
  kinds: Record<string, RealmCount>;
  no_kind_token: string;
  /** type -> how many of it the brain holds. */
  types: Record<string, number>;
  skill_sections: SectionShape[];
  rule_sections: SectionShape[];
  decl_labels: DeclLabel[];
  rule_sources: string[];
  node_types: string[];
  /** What the brain is called: the sidebar, the tab, `{brain}` in a wake phrase. */
  brain_name: string;
  owner: string;
  writable: boolean;
  boot: string;
}

/**
 * Two counts per realm because they answer different questions: `nodes` is what
 * the map filters over, `open_tasks` is what the dashboard filters over. A realm
 * with 40 nodes and 0 open tasks is real, and the chip says so rather than
 * showing a number that filters down to nothing.
 */
export interface RealmCount {
  nodes: number;
  open_tasks: number;
}

// ---------------------------------------------------------------------------
// Dashboard
// ---------------------------------------------------------------------------

/** The slim row `brain_query._slim` sends — small on purpose. */
export interface TaskRow {
  id: string;
  title: string;
  type: string;
  status?: string;
  realm?: string;
  due?: string;
  /** The due date already resolved into the brain's timezone, or "--". */
  due_local?: string;
  waiting_on?: string;
  assignee?: string;
  kind?: string;
  delegated?: string;
  mine?: boolean;
  blocked_by?: RelatedRef[];
  blocks?: RelatedRef[];
  related?: RelatedRef[];
  [key: string]: unknown;
}

export interface RelatedRef {
  id: string;
  title: string;
  type?: string;
  status?: string;
}

export interface ViewPayload extends Envelope {
  view: string;
  /** The view's own heading — "Agenda", "Due this week", … */
  title: string;
  /** The date the view was computed against. */
  ref?: string;
  count: number;
  results: TaskRow[];
}

/** `/api/node` returns the node's fields inline, plus its edges either way. */
export interface NodeDetail extends Envelope {
  id: string;
  type: string;
  title: string;
  body?: string;
  status?: string;
  realm?: string;
  due?: string;
  props?: Record<string, unknown>;
  /** Named neighbours, each carrying its own direction. */
  edges: NodeEdge[];
  [key: string]: unknown;
}

export interface NodeEdge {
  dir: "out" | "in";
  rel: string;
  id: string;
  title: string;
  type?: string;
}

export interface Person {
  id: string;
  title: string;
  aliases: string[];
  realm: string;
}

export interface LinkSuggestion {
  id: string;
  title: string;
  type: string;
  rel: string;
  /** The wording in the text that produced this guess. */
  cue?: string;
}

export interface SearchHit {
  id: string;
  title: string;
  type: string;
  realm?: string;
  status?: string;
}

// ---------------------------------------------------------------------------
// Skills and rules
// ---------------------------------------------------------------------------

/** Why a rule is on a skill's card — the three are genuinely different. */
export type RuleWhy = "declared" | "matched" | "always";

export interface SkillRuleRef {
  id: string;
  title: string;
  source?: string;
  non_negotiable?: boolean;
  linked?: boolean;
  why: RuleWhy;
}

export interface SkillNode {
  id: string;
  title: string;
  description: string;
  triggers: string;
  kind: string;
  uses: number;
  requires: string[];
  waits_for: string[];
  enriched_by: string[];
  reads: string[];
  writes: string[];
  reads_other: string;
  writes_other: string;
  domains: string[];
  declared: boolean;
  rules: SkillRuleRef[];
  /** Nesting is the `requires` edge and nothing else (CONVENTIONS §5a). */
  children: SkillNode[];
}

export interface SkillTree {
  kind: string;
  hub: string;
  title: string;
  count: number;
  skills: SkillNode[];
}

export interface RuleNode {
  id: string;
  title: string;
  source: string;
  section: string;
  non_negotiable: boolean;
  /** What match-rules actually fires on — filtering by trigger has to see it. */
  scenario: string;
  domains: string[];
  chars?: number;
  body?: string;
  rule?: string;
  sections?: Record<string, string>;
  [key: string]: unknown;
}

/** A way of working: a `process` node, part-of `hub_process`, scoped to a realm. */
export interface ProcessNode {
  id: string;
  title: string;
  body: string;
  realm: string;
  /** When this process applies. */
  trigger: string;
  established: string;
  category: string;
  domains: string[];
  edges: number;
  updated: string;
}

export interface NodeTypeInfo {
  type: string;
  description?: string;
  count?: number;
  readers?: string[];
  writers?: string[];
}

export interface SkillsPayload extends Envelope {
  trees: SkillTree[];
  rules: RuleNode[];
  /** Every realm's processes; the page narrows, defaulting to `realm`. */
  processes?: ProcessNode[];
  /** The realm a new or imported process lands in. */
  realm?: string;
  node_types: NodeTypeInfo[];
  /** hub id -> the human title a filter labels itself with. */
  areas: Record<string, string>;
}

// ---------------------------------------------------------------------------
// Bundles
// ---------------------------------------------------------------------------

export interface ExportBundleResult extends Envelope {
  skills: number;
  rules: number;
  processes?: number;
  /** Where the zip landed on disk — the "two machines" case wants this. */
  path: string;
  name: string;
  /** A same-origin URL, so the browser downloads it too. */
  download: string;
  /** Required by something exported, but not carried — those links will not form. */
  missing_skills?: string[];
  cycles?: string[][];
}

export interface BundleItem {
  id: string;
  title: string;
  description?: string;
  kind: "skill" | "rule" | "process";
  /** For a process: the realm it was exported from. */
  realm?: string;
  /** The only interesting case: this brain already holds it. */
  exists: boolean;
}

export interface UploadBundleResult extends Envelope {
  token: string;
  counts: { skills?: number; rules?: number; processes?: number };
  items: BundleItem[];
}

export type ImportChoice = "update" | "replace" | "ignore";

/** One file in `<home>/scripts`. The brain runs one only when a trigger links it. */
export interface ScriptEntry {
  name: string;
  bytes: number;
  executable: boolean;
  sha256: string;
  language: string;
  /** The first docstring or comment line, past the shebang. */
  summary: string;
  modified?: string;
}

export interface ScriptsPayload extends Envelope {
  dir: string;
  exists: boolean;
  count: number;
  scripts: ScriptEntry[];
  skipped: { name: string; why: string }[];
}

export interface ScriptDetail extends Envelope {
  script?: ScriptEntry & { text: string };
}

export interface ExportScriptsResult extends Envelope {
  path: string;
  name: string;
  download: string;
  count: number;
  scripts: string[];
}

export interface StagedScript extends ScriptEntry {
  /** A local file by this name is already in `<home>/scripts`. */
  exists: boolean;
  /** Only when it exists: are the bytes the same? Then there is nothing to decide. */
  identical?: boolean;
  existing_bytes?: number;
}

export interface UploadScriptsResult extends Envelope {
  token: string;
  dir: string;
  count: number;
  items: StagedScript[];
}

/** An existing script is overwritten only for `replace`; a new one is written either way. */
export type ScriptChoice = "replace" | "keep";

export interface ImportScriptsResult extends Envelope {
  dir: string;
  applied: { name: string; action: string; why?: string }[];
  not_in_zip: string[];
  /** Where the bytes of every replaced script were copied first. */
  backup?: string;
}

export interface ImportBundleResult extends Envelope {
  applied: { id: string; action: string; rules_not_found?: string[] }[];
  tree?: { added: number; pruned: number };
  missing_skills?: string[];
}

export interface SyncResult extends Envelope {
  /** export-skills */
  dir?: string;
  skills?: number;
  rules?: { id: string; action: string }[] | number;
  /** ingest-skills */
  count?: number;
  ingested?: { id: string; action: string; rules_not_found?: string[] }[];
  tree?: { added: number; pruned: number };
}

export interface DeleteContractsResult extends Envelope {
  count: number;
  deleted: { id: string; type: string }[];
  refused: { id: string; why: string }[];
  not_found: string[];
}

// ---------------------------------------------------------------------------
// Settings and status
// ---------------------------------------------------------------------------

export interface Settings extends Envelope {
  writable: boolean;
  keys: string[];
  /** What the file holds. Empty for a key nobody ever set. */
  settings: Partial<StoredSettings>;
  /**
   * Every key a shell variable has taken over. A settings page that hides this
   * is lying about what a save will do.
   */
  env: Record<string, string>;
  path: string;
  effective: EffectiveSettings;
  capture: CaptureState;
}

export interface StoredSettings {
  tz: string;
  realm: string;
  /** Who "me" is — what `--mine` filters on. */
  owner: string;
  /** What the brain answers to. */
  brain_name: string;
  backend: string;
  /** Push each write to the neo4j mirror, instead of only queueing it. */
  autosync: boolean;
  /** Let the daily hook fold closed days into digests. */
  consolidate: boolean;
  /** Keep this browser's view preferences in the brain, not just locally. */
  ui_sync: boolean;
  /** The learned-rules detector. Off by default; it only ever proposes. */
  learned_rules: boolean;
  /** Gated per-prompt recall. Off by default; it spends context tokens when it fires. */
  prompt_recall: boolean;
  /** Mirror Agents tab chats to the master. Off by default. */
  agents_sync: boolean;
  map_poll_seconds: number;
  dash_refresh_seconds: number;
}

/** Where a resolved value came from. The page says so, because a setting whose
 *  env var has taken over is a field that cannot do what it looks like it does. */
export type SettingSource = "env" | "settings" | "default";

export interface EffectiveSettings {
  tz: string;
  tz_source: SettingSource | "system";
  system_tz: string;
  now: string;
  realm: string;
  owner: string;
  owner_source: SettingSource;
  brain_name: string;
  brain_name_source: SettingSource;
  backend: string;
  backends: string[];
  autosync: boolean;
  autosync_source: SettingSource;
  consolidate: boolean;
  consolidate_source: SettingSource;
  ui_sync: boolean;
  ui_sync_source: SettingSource;
  learned_rules: boolean;
  learned_rules_source: SettingSource;
  prompt_recall: boolean;
  prompt_recall_source: SettingSource;
  agents_sync: boolean;
  agents_sync_source: SettingSource;
  map_poll_seconds: number;
  dash_refresh_seconds: number;
}

/**
 * Whether the ledger is recording, and which switch decided it.
 *
 * `env_off` matters: $MEMORY_LEDGER_OFF outranks the marker file, so when it is
 * set the UI must not offer to turn capture back on — it cannot.
 */
export interface CaptureState {
  enabled: boolean;
  marker: string;
  marker_present: boolean;
  env_off: boolean;
  source: "env" | "marker" | "on";
}

export interface Neo4jStatus {
  url: string;
  db: string;
  user: string;
  /** The password itself is NEVER sent to the browser. */
  password_set: boolean;
  configured: boolean;
  up: boolean;
  reading_from: string;
  file: string;
  env: string[];
}

export interface Status extends Envelope {
  writable: boolean;
  boot: string;
  db: string;
  home: string;
  nodes: number;
  edges: number;
  pending_ops: number;
  loose_captures: number;
  neo4j: Neo4jStatus;
}

export interface Timezones extends Envelope {
  zones: string[];
}

/** One person node in a duplicate group, with how much history it carries. */
export interface DuplicateCandidate {
  id: string;
  title: string;
  edges: number;
  /** How sure the name match is. Only on the duplicates, not the canonical. */
  confidence?: "certain" | "likely";
}

/**
 * A group of person nodes that look like one human.
 *
 * The canonical is the most connected node, because merging into it moves the
 * fewest edges. Detected by name matching over every alias — no model call.
 */
export interface PersonDuplicate {
  canonical: DuplicateCandidate;
  duplicates: DuplicateCandidate[];
}

export interface PersonDuplicates extends Envelope {
  duplicates: PersonDuplicate[];
}

// ---------------------------------------------------------------------------
// Upkeep: consolidation, its preview, and the snapshots it leaves
// ---------------------------------------------------------------------------

/** What one day's consolidation would do. Computed by the engine, not guessed. */
export interface ConsolidationDay {
  day: string;
  digest: string;
  digest_exists: boolean;
  action: "create" | "update";
  captures: number;
  prompts: number;
  newly_consolidated: number;
  already_consolidated: number;
  chars_behind_it: number;
}

export interface ConsolidationPreview extends Envelope {
  dry_run: true;
  keep_days: number;
  cutoff: string;
  still_loose: number;
  backup_keep_days: number;
  days: ConsolidationDay[];
  totals: {
    captures: number;
    new_digests: number;
    updated_digests: number;
    newly_consolidated: number;
    chars: number;
  };
}

export interface Backup {
  name: string;
  path: string;
  bytes: number;
  created: string;
  expires: string;
  expired: boolean;
}

export interface BackupList extends Envelope {
  dir: string;
  keep_days: number;
  backups: Backup[];
}

export interface MaintainResult extends Envelope {
  keep_days?: number;
  cutoff?: string;
  days_consolidated?: number;
  newly_consolidated?: number;
  still_loose?: number;
  read_reduction?: string;
  backup?: { path: string; bytes: number } | null;
  /** Set when the write was refused for want of an explicit confirmation. */
  needs_confirmation?: boolean;
}

// ---------------------------------------------------------------------------
// Doctor — the health check, in the browser
// ---------------------------------------------------------------------------

export interface DoctorReport extends Envelope {
  /** The verdict. Distinct from `ok`, which only says the call succeeded. */
  healthy: boolean;
  problems: string[];
  status: Record<string, unknown>;
  paths: Record<string, unknown>;
  config: Record<string, unknown>;
  content: Record<string, unknown>;
  hooks: Record<string, unknown>;
  mirror: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// UI preferences — the browser's own state, optionally kept in the brain
// ---------------------------------------------------------------------------

/** An opaque blob: the shape belongs to the frontend, and the server stores it
 *  without opinions so a new toggle needs no server change. */
export type UiPrefs = Record<string, unknown>;

export interface UiPrefsPayload extends Envelope {
  synced: boolean;
  path: string;
  prefs: UiPrefs;
}

// ---------------------------------------------------------------- triggers
// docs/trigger.md. The config is what `<home>/trigger.json` holds, minus the
// secrets: for those the server only ever says whether one is set, and where
// from, so a page that shows "one is stored" never holds the token itself.

export type TriggerService = "claude" | "slack" | "jira" | "github" | "meeting";

/** What a GitHub poll can notice. Each has its own switch and instruction. */
export type GitHubEventKind =
  | "mention"
  | "review_requested"
  | "review"
  | "comment"
  | "pushed"
  | "ci_failed"
  | "merged"
  | "closed"
  | "assigned";

/** What Slack notices besides a mention: the bot added to or removed from a channel. */
export type SlackEventKind = "mention" | "joined_channel" | "left_channel";

/** What a Jira poll notices on a watched issue, besides a marked mention. */
export type JiraEventKind = "mention" | "transitioned" | "assigned" | "unassigned" | "comment";

/**
 * What a trigger does when it fires: a headless Claude session told the run
 * command, a call to an API, or a script in `<home>/scripts`. The last two cost
 * no model call.
 */
export type TriggerAction = "claude" | "http" | "script";

export interface TriggerHttp {
  method: string;
  /** Values are percent-encoded where they land. Must start with http(s)://. */
  url: string;
  headers: Record<string, string>;
  /** A JSON body gets its values JSON-escaped. */
  body: string;
}

export interface TriggerScriptRef {
  /** A bare filename in `<home>/scripts`. */
  name: string;
  /** One argv item each, variables filled in. Empty: a command's own arguments. */
  args: string[];
}

export interface TriggerEventSetting {
  on: boolean;
  /** The run command for a Claude action. Empty for a mention: the message is the request. */
  do: string;
  action: TriggerAction;
  http: TriggerHttp;
  script: TriggerScriptRef;
}

export type GitHubEventSetting = TriggerEventSetting;

/** `@brain <name> {a, b}`, matched by `pattern` (a regex), or the name as the first word. */
export interface SlackCommand extends TriggerEventSetting {
  name: string;
  /** Anyone the gate lets in may run it. Off: only you. */
  anyone: boolean;
  pattern: string;
  description: string;
}

/** "<brain> will handle this", said in a meeting. Matched word by word. */
export interface WakePhrase extends TriggerEventSetting {
  /** Words, with `{brain}` and `{owner}` filled in before matching. */
  phrase: string;
  /** Anyone in the meeting saying it runs it. Off: only you. */
  anyone: boolean;
  description: string;
}

export interface ScanMatch {
  phrase: string;
  speaker: string;
  said: string;
  is_owner?: boolean;
  runs?: boolean;
  status?: string;
  request_id?: string;
}

export interface ScanResult extends Envelope {
  matched: number;
  matches: ScanMatch[];
  dry_run?: boolean;
  queued?: string[];
  refused?: string[];
  seen?: number;
}

export interface TriggerSecret {
  set: boolean;
  stored: boolean;
  /** "env" outranks the file, and then this page cannot change it. */
  source: "env" | "file" | "";
  env_var: string;
}

export interface TriggerConfig {
  path: string;
  exists: boolean;
  problem: string;
  /** In effect: the master switch AND a file that parses. */
  enabled: boolean;
  stored_enabled: boolean;
  signature: string;
  signature_template: string;
  refusals: { not_enabled: string; policy: string; not_allowed: string };
  /** A refusal goes out as written through the source's API, or not at all: never a model call. */
  refuse_without_model: boolean;
  translate_notices: boolean;
  claude: {
    bin: string;
    notice_model: string;
    mcp_config: string;
    strict_mcp_config: boolean;
    timeout_s: number;
    max_concurrent: number;
    extra_args: string[];
    cwd: string;
  };
  slack: {
    enabled: boolean;
    active: boolean;
    /** The run command, with {variables}. Empty is the built-in handling. */
    prompt: string;
    owner_user_id: string;
    thread_lookback_days: number;
    /** Your DMs and group DMs, heard through the user token. */
    dms: boolean;
    /** Every DM with you open to the other person's tag. */
    dms_open: boolean;
    /** Slack user ids that may summon the brain anywhere. */
    allowed_users: string[];
    /** Slack user ids that may not, even in an open channel. */
    blocked_users: string[];
    events: Record<SlackEventKind, TriggerEventSetting>;
    commands: SlackCommand[];
  };
  jira: {
    enabled: boolean;
    active: boolean;
    prompt: string;
    base_url: string;
    email: string;
    owner_account_id: string;
    marker: string;
    poll_seconds: number;
    jql: string;
    /** Which issues a transition, an assignment or a comment counts on. */
    watch_jql: string;
    max_catchup_hours: number;
    events: Record<JiraEventKind, TriggerEventSetting>;
  };
  github: {
    enabled: boolean;
    active: boolean;
    api_url: string;
    /** Empty: looked up from the token. */
    login: string;
    marker: string;
    poll_seconds: number;
    /** owner/name for a repo, a bare name for a whole org. Empty is everything. */
    repos: string[];
    trusted_associations: string[];
    /** A refusal on GitHub is a public comment, so it is off unless asked for. */
    refusal_notice: boolean;
    max_catchup_hours: number;
    events: Record<GitHubEventKind, GitHubEventSetting>;
  };
  meeting: {
    enabled: boolean;
    active: boolean;
    phrases: WakePhrase[];
    /** What a phrase does when its own run command is empty. */
    default_do: string;
    brain_name: string;
  };
  secrets: Record<string, TriggerSecret>;
  /** What a run command can say in braces. */
  variables: TriggerVariable[];
  actions: TriggerAction[];
  http_methods: string[];
  scripts_dir: string;
}

export interface CreateScriptResult extends Envelope {
  name?: string;
  action?: "created" | "replaced";
  exists?: boolean;
  script?: ScriptEntry;
}

export interface TriggerVariable {
  name: string;
  /** The sources it has a value on; `global` when that is all of them. */
  sources: TriggerService[];
  global: boolean;
  description: string;
  example: string;
}

export interface TriggerWatcher {
  state: string;
  last_error?: string;
  at?: string;
  [key: string]: unknown;
}

export interface TriggerRuntime {
  running: boolean;
  reason: string;
  started_at: string;
  slack: TriggerWatcher;
  jira: TriggerWatcher;
  github: TriggerWatcher;
}

export interface TriggerRequestRow {
  id: string;
  source: "slack" | "jira" | "github";
  scope: string;
  requester_name: string;
  requester_id: string;
  status: string;
  received_at: string;
  text: string;
  permalink: string;
}

/** A Slack channel open to everyone's mentions. Closing one keeps the row. */
export interface TriggerChannelRow {
  channel_id: string;
  name: string;
  added_by: string;
  added_at: string;
  removed_by: string;
  removed_at: string;
}

export interface TriggerChannels {
  active: TriggerChannelRow[];
  removed: TriggerChannelRow[];
}

/** Someone who may tag the brain in any conversation on a source. */
export interface TriggerPersonRow {
  source: "jira" | string;
  /** Their id on the source: a Jira account id. */
  user_id: string;
  name: string;
  added_by: string;
  added_at: string;
  removed_by: string;
  removed_at: string;
}

export interface TriggerPeople {
  active: TriggerPersonRow[];
  removed: TriggerPersonRow[];
}

/** A Jira account the search found, to allow. */
export interface TriggerPersonMatch {
  user_id: string;
  name: string;
  email: string;
}

export interface TriggerStatus extends Envelope {
  config: TriggerConfig;
  runtime: TriggerRuntime;
  /** False when the server was started with --no-trigger or --no-write. */
  watching: boolean;
  counts: Record<string, number>;
  enabled_scopes: { scope: string; by: string; at: string }[];
  channels: TriggerChannels;
  people: TriggerPeople;
  recent: TriggerRequestRow[];
}

export interface TriggerCheck extends Envelope {
  ready: boolean;
  target: string;
  problems: string[];
  notes: string[];
}

/** A trigger task as the Tasks panel lists it (`/api/tasks`). */
export interface TriggerTask {
  id: string;
  source: "slack" | "jira" | "github" | string;
  scope: string;
  event_id: string;
  requester_id: string;
  requester_name: string;
  is_owner: boolean;
  permalink: string;
  status: string;
  reason: string;
  node_id: string;
  received_at: string;
  started_at: string;
  finished_at: string;
  exit_code: number | null;
  cost_usd: number | null;
  /** cost_usd was worked out from the transcript: the run died before claude reported it. */
  cost_estimated?: boolean;
  session_id: string;
  cwd: string;
  /** What fired it: a command, a title, or the event kind. */
  name: string;
  kind: string;
  /** claude | http | script */
  action: string;
  text: string;
  result: string;
  actions?: number;
  /** A running task only: read from its session transcript. */
  progress?: {
    tool_calls: number;
    last_tool: string;
    last_input: string;
    last_at: string;
    /** From the database, written by the brain's hooks as the session runs. */
    subagents: number;
    subagents_running: number;
    actions: number;
    last_action: { kind: string; target: string; at: string } | null;
    last_message: string;
  } | null;
}

export interface TasksPayload extends Envelope {
  tasks: TriggerTask[];
  counts: Record<string, number>;
  total: number;
  limit: number;
  truncated: boolean;
  now: string;
}

export interface TaskAction {
  id: number;
  request_id: string;
  at: string;
  kind: string;
  target: string;
  detail: string;
}

export type TranscriptEvent =
  | { kind: "text"; role: "prompt" | "user" | "assistant"; at: string; text: string }
  | {
      kind: "tool";
      at: string;
      id: string;
      name: string;
      input: string;
      result: string | null;
      is_error: boolean;
      done_at?: string;
    };

export interface TaskDetail extends Envelope {
  task: TriggerTask & {
    meta: Record<string, unknown>;
    /** The argv (or `[method, url]`) it ran with; null before it started. */
    command: string[] | string | null;
    stderr: string;
  };
  actions: TaskAction[];
  /** What the hooks wrote to the database for this session; live while it runs. */
  activity: TaskActivity | null;
  transcript: {
    found: boolean;
    path: string;
    events: TranscriptEvent[];
    tool_calls: number;
    of?: number;
    truncated: boolean;
  };
}

export interface TaskAgent {
  id: string;
  role: string;
  status: string;
  outcome: string;
  started_at: string;
  ended_at: string;
  depth: number | null;
  tokens_out: number | null;
  brief: string;
}

export interface ActivityEvent {
  at: string;
  kind: "session" | "agent" | "message" | "action";
  title: string;
  role?: string;
  target?: string;
  detail?: string;
  status?: string;
}

export interface TaskActivity {
  session_id: string;
  conversation: { id: string; status: string } | null;
  agents: TaskAgent[];
  subagents_running: number;
  messages: number;
  actions: number;
  last_action: { kind: string; target: string; at: string } | null;
  last_message: string;
  updated: string;
  timeline: ActivityEvent[];
}

/** One stored `usage-week` node, slim — what the Usage section's picker needs. */
export interface UsageWeek {
  week: string;
  from: string;
  to: string;
  costUSD: number;
  sessions: number;
}

/** `/api/usage-summary`: estimated dollars for the sidebar meter. */
export interface UsageSummary {
  ok: boolean;
  estimated: boolean;
  fiveHours: number;
  today: number;
  week: number;
  month: number;
  year: number;
  total: number;
  weeks: number;
  /** Weeks stored without hour buckets, counted whole on their first day. */
  coarseWeeks: number;
}

/** `/api/usage-report`: the report, rendered by the server with no model call. */
export interface UsageReportPayload {
  ok: boolean;
  /** Every stored week, oldest first. */
  weeks: UsageWeek[];
  /** The weeks the page covers. Empty when there is nothing to draw. */
  selected: string[];
  /** One self-contained HTML document. */
  html: string;
}

/** One learned-rule proposal: what the detector found, not yet a rule. */
export interface RuleProposal {
  id: string;
  status: "proposed" | "accepted" | "rejected";
  title: string;
  text: string;
  count: number;
  sessions: number;
  evidence: { id: string; snippet: string }[];
}

export interface RuleProposals extends Envelope {
  enabled: boolean;
  count: number;
  of: number;
  truncated: boolean;
  proposals: RuleProposal[];
}

// ---------------------------------------------------------------- brain to brain
// docs/b2b.md. The config is what `<home>/b2b.json` holds. The private key is
// never in it: the server says whether one is set and where from.

export interface B2BBroker {
  id: string;
  name: string;
  url: string;
  enabled: boolean;
  /** A login the poller holds right now, and when it lapses. */
  session: { open: boolean; expires_at?: string };
}

export interface B2BConfig {
  path: string;
  exists: boolean;
  problem: string;
  /** In effect: the switch AND a file that parses. */
  enabled: boolean;
  active: boolean;
  /** This brain's public key, which is its address. Safe to share. */
  user_id: string;
  private_key: { set: boolean; source: "env" | "file" | ""; env_var: string };
  brokers: B2BBroker[];
  poll_seconds: number;
  /** The run command for a received message, with `{variables}`. */
  do: string;
  action: TriggerAction;
  http: TriggerHttp;
  script: TriggerScriptRef;
  variables: Record<string, string>;
}

export interface B2BPeer {
  peer_id: string;
  name: string;
  added_at: string;
  removed_at: string | null;
  broker_id: string;
  person_id: string;
  person_title: string;
}

/** A user id allowed to send to this brain, like a friend. */
export interface B2BAllowed {
  user_id: string;
  name: string;
  added_at: string;
  /** Set when they were blocked again; the row is kept. */
  removed_at: string;
  /** Also a connected peer: this brain can send to them. */
  is_peer: boolean;
}

export interface B2BSent {
  id: string;
  peer_id: string;
  text: string;
  sent_at: string;
  request_id: string;
  broker_id: string;
  /** 1 when typed in the Messages chat; 0 for a session, a script or the CLI. */
  human?: number;
}

/** One message in a conversation, either way. */
export interface B2BMessage {
  id: string;
  peer_id: string;
  direction: "in" | "out";
  text: string;
  /** When the sender sent it. */
  at: string;
  /** A person typed it, rather than a session or a script. */
  human: boolean;
  /** Ours: `sent`, `staged` (the peer's box is full or the relay is out of reach; tried again
   * on every poll) or `failed`. Theirs: the request's status. */
  status: string;
  /** Names of the files that came with it. */
  files?: string[];
  /** Theirs, with files: whether they were downloaded (`pending` is retried) and where. */
  attachment?: { state: "pending" | "done" | "failed"; error: string; dir: string };
  /** Ours: the session it was sent for, if any. Theirs: the request it became. */
  request_id: string;
  received_at?: string;
  broker_id?: string;
}

export interface B2BConversation {
  peer_id: string;
  name: string;
  person_id: string;
  /** A connected peer, so this brain can write to them. */
  connected: boolean;
  allowed: boolean;
  messages: number;
  last: Pick<B2BMessage, "direction" | "text" | "at" | "human"> | null;
}

export interface B2BConversations extends Envelope {
  conversations: B2BConversation[];
}

export interface B2BThread extends Envelope {
  peer_id: string;
  /** Oldest first: the latest page, or the one before `before`. */
  messages: B2BMessage[];
  /** Older messages exist; pass `next_before` as `before` to read them. */
  has_more: boolean;
  next_before: string;
}

export interface B2BRuntime {
  state: string;
  last_error?: string;
  last_ok?: string;
  every_s?: number;
}

export interface B2BStatus extends Envelope {
  config: B2BConfig;
  peers: B2BPeer[];
  allowed: B2BAllowed[];
  sent: B2BSent[];
  /** Received messages by status. */
  received: Record<string, number>;
  runtime: B2BRuntime;
  /** False when the server runs no poller (--no-trigger or --no-write). */
  watching: boolean;
}

export interface B2BBrokerCheck extends Envelope {
  broker: string;
  user_id: string;
  session: { open: boolean; expires_at?: string };
}

export interface B2BSendResult extends Envelope {
  id: string;
  to: string;
  name: string;
  broker: string;
  sent_at: string;
  human?: boolean;
  /** True when the peer cannot take it yet; it goes on its own at a later poll. */
  staged?: boolean;
  reason?: string;
}

// ------------------------------------------------------------------ agents
export type AgentKind = "claude" | "ollama";

export interface AgentConnector {
  id: string;
  name: string;
  kind: AgentKind;
  enabled: boolean;
  default_model: string;
  timeout_s: number;
  /** claude: the configured list; ollama: what the host has pulled right now. */
  models: string[];
  bin?: string;
  url?: string;
  max_rounds?: number;
  /** False when the host could not be asked (an Ollama that is down). */
  available: boolean;
  problem: string;
}

/** A skill or plugin command the composer offers after a `/`. `writes` is a guess. */
export interface AgentCommand {
  name: string;
  plugin: string;
  kind: "skill" | "command";
  description: string;
  writes: boolean;
}

export interface AgentPlugin {
  key: string;
  name: string;
  version: string;
  enabled: boolean;
  commands: number;
}

export interface AgentCommands extends Envelope {
  commands: AgentCommand[];
  plugins: AgentPlugin[];
}

export interface AgentsStatus extends Envelope {
  config: {
    default_connector: string;
    context_turns: number;
    recall_limit: number;
    recall_budget: number;
    max_concurrent: number;
    _problem?: string;
  };
  connectors: AgentConnector[];
  running: number;
  question_mark: string;
}

/** The badge: a spinner, a ?, a check, a cross, or nothing yet. */
export type AgentConvState = "pending" | "question" | "done" | "error" | "empty";

export interface AgentConversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  connector: string;
  model: string;
  full_tools: boolean;
  context_turns: number;
  hidden: boolean;
  /** The unsent message in the composer and when it was edited (ISO); the newest of this and the local copy wins. */
  draft: string;
  draft_at: string;
  node_id: string;
  /** Voice: "" follows the global setting. */
  verbosity?: VoiceVerbosity | "";
  tts?: "on" | "off" | "";
  /** How the latest turn ended, or null while it runs (thread only). */
  turn_end?: { id: string; status: "ok" | "error" | "cancelled" } | null;
  state?: AgentConvState;
  last?: { role: "user" | "agent"; text: string; at: string } | null;
  messages?: number;
  cost_usd?: number;
  cost_estimated?: boolean;
  tokens_in?: number;
  tokens_out?: number;
}

export type AgentEventKind = "tool" | "skill" | "agent" | "mcp" | "thinking" | "text" | "note";

export interface AgentEvent {
  id?: string;
  kind: AgentEventKind;
  name: string;
  tool?: string;
  input?: string;
  output?: string;
  error?: boolean;
  parent?: string;
  at?: string;
}

export type AgentMsgStatus = "queued" | "running" | "done" | "question" | "error";

export interface AgentMessage {
  id: string;
  conv_id: string;
  role: "user" | "agent";
  text: string;
  at: string;
  status: AgentMsgStatus;
  reply_to: string;
  connector: string;
  model: string;
  full_tools: boolean;
  events: AgentEvent[];
  context: string[];
  error: string;
  session_id: string;
  /** null: not known; 0: free (a local model). */
  cost_usd: number | null;
  cost_estimated: boolean;
  tokens_in: number;
  tokens_out: number;
  started_at: string;
  finished_at: string;
  node_id: string;
  /** The agent's suggested next message; Tab on an empty input takes it. */
  next_action: string;
}

export interface AgentConversations extends Envelope {
  conversations: AgentConversation[];
}

export interface AgentThread extends Envelope {
  conversation: AgentConversation;
  messages: AgentMessage[];
}

export interface AgentSendResult extends Envelope {
  conversation: AgentConversation;
  message_id: string;
  reply_id: string;
}

/** A file saved by POST /api/agent-attach, under <home>/attachments. */
export interface AgentAttachResult extends Envelope {
  name: string;
  path: string;
  kind: AgentDocument["kind"];
  size: number;
}

/** A document an agent wrote under <home>/exports (GET /api/document). */
export interface AgentDocument extends Envelope {
  name: string;
  path: string;
  kind: "md" | "txt" | "html" | "pdf" | "img" | "file";
  size: number;
  /** Markdown, text and HTML only; a PDF is shown from /api/document-file. */
  text: string | null;
}

export type VoiceLang = "pt" | "en" | "es" | "it";
export type VoiceVerbosity = "default" | "verbose" | "verbose_tts";

/** `<home>/voice.json`: the voice settings (GET/POST /api/voice, /api/voice-config). */
export interface VoiceConfig {
  stt_language: VoiceLang;
  stt_auto: boolean;
  tts_language: VoiceLang;
  tts_auto: boolean;
  translate: boolean;
  translate_target: VoiceLang;
  tts_enabled: boolean;
  tts_background: boolean;
  silence_s: number;
  tts_voices: Partial<Record<VoiceLang, string>>;
  verbosity: VoiceVerbosity;
}

export interface VoiceStatus extends Envelope {
  config: VoiceConfig;
  stt: { available: boolean; model: string; problem: string };
  tts: {
    available: boolean;
    engine: string;
    problem: string;
    voices: { id: string; name: string; language: VoiceLang }[];
  };
}

export interface VoiceTranslation extends Envelope {
  text: string;
  translated: boolean;
  language: VoiceLang;
}

/** Custom triggers (docs/trigger.md#custom-triggers): one YAML file each in `<home>/triggers`. */
export interface CustomTriggerState {
  trigger_id?: string;
  last_run_at?: string;
  last_status?: string;
  last_error?: string;
  last_run_id?: string;
  updated?: string;
}

export interface CustomTriggerSummary {
  id: string;
  file: string;
  ok: boolean;
  error?: string;
  name?: string;
  enabled?: boolean;
  interval?: string | number;
  blocks?: number;
  state: CustomTriggerState;
}

export interface CustomTriggerList {
  ok: boolean;
  dir: string;
  /** The master switch and `custom.enabled`: whether the scheduler runs them here. */
  active: boolean;
  /** The secret names in trigger.json; never a value. */
  secrets: string[];
  triggers: CustomTriggerSummary[];
  template: string;
}

export interface CustomTriggerRun {
  run_id: string;
  started_at: string;
  finished_at: string;
  status: string;
  attempts: number;
  events: number;
  created: number;
  error: string;
}

export interface CustomTriggerDetail {
  ok: boolean;
  id: string;
  text: string;
  errors: string[];
  warnings: string[];
  state: CustomTriggerState;
  checkpoint: string;
  runs: CustomTriggerRun[];
}

export interface CustomTriggerVerdict {
  ok: boolean;
  /** check_only: the verdict (`ok` only says the check ran). */
  valid?: boolean;
  id?: string;
  errors: string[];
  warnings: string[];
  saved?: boolean;
  created?: boolean;
  error?: string;
}

export interface CustomTriggerDryRun {
  ok: boolean;
  id: string;
  dry_run: true;
  status: string;
  events: { event_id: string; text: string; meta: { payload: unknown } }[];
  checkpoint: { now: string; would_be: string | null };
  error: string;
  warnings?: string[];
}
