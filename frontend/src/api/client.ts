/**
 * One way to reach the server.
 *
 * Every call in the app goes through `get` or `post` here, so retry, error
 * shaping, the write-disabled (`--no-write`) 403 and the abort-on-unmount path
 * are decided once instead of at each of the fifty call sites the old page had.
 */
import type {
  Envelope,
  ExportBundleResult,
  GraphNode,
  GraphPayload,
  ImportBundleResult,
  ImportChoice,
  LinkSuggestion,
  Meta,
  NodeDetail,
  Person,
  RealmCount,
  SearchHit,
  Settings,
  SkillsPayload,
  Status,
  SyncResult,
  Timezones,
  UploadBundleResult,
  ScriptsPayload,
  ScriptDetail,
  ExportScriptsResult,
  UploadScriptsResult,
  ScriptChoice,
  ImportScriptsResult,
  ViewPayload,
} from "./types";

/** A failed call, carrying the server's own reason rather than a status code. */
export class ApiError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** The server is same-origin in production; the dev server proxies /api to it. */
export const BASE = "";

type Query = Record<string, string | number | boolean | undefined | null>;

export function queryString(params: Query = {}): string {
  const usp = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    usp.set(key, String(value));
  }
  const s = usp.toString();
  return s ? `?${s}` : "";
}

async function unwrap<T>(res: Response): Promise<T> {
  let payload: unknown;
  try {
    payload = await res.json();
  } catch {
    throw new ApiError(
      `${res.status} ${res.statusText || "unreadable response"}`,
      res.status,
    );
  }
  const body = payload as Partial<Envelope>;
  // `ok: false` is the server's own refusal and carries the reason worth
  // showing. An HTTP failure with no body of its own falls back to the status.
  if (!res.ok || body?.ok === false) {
    throw new ApiError(body?.error || `request failed (${res.status})`, res.status);
  }
  return payload as T;
}

export async function get<T>(path: string, params?: Query, signal?: AbortSignal): Promise<T> {
  const res = await fetch(`${BASE}${path}${queryString(params)}`, {
    signal,
    headers: { accept: "application/json" },
  });
  return unwrap<T>(res);
}

export async function post<T = Envelope>(
  path: string,
  body: unknown = {},
  signal?: AbortSignal,
): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    signal,
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return unwrap<T>(res);
}

/**
 * The named surface. Everything the UI can ask the brain, in one place, so a
 * component never builds a URL and a renamed endpoint is one edit.
 */
export const api = {
  // reads
  graph: (signal?: AbortSignal) => get<GraphPayload>("/api/graph", undefined, signal),
  meta: (signal?: AbortSignal) => get<Meta>("/api/meta", undefined, signal),
  views: (signal?: AbortSignal) =>
    get<{ ok: boolean; views: string[] }>("/api/views", undefined, signal),
  view: (name: string, params: Query, signal?: AbortSignal) =>
    get<ViewPayload>(`/api/${name}`, params, signal),
  node: (id: string, signal?: AbortSignal) => get<NodeDetail>("/api/node", { id }, signal),
  people: (signal?: AbortSignal) =>
    get<{ ok: boolean; people: Person[] }>("/api/people", undefined, signal),
  personDuplicates: (signal?: AbortSignal) =>
    get<import("./types").PersonDuplicates>("/api/person-duplicates", undefined, signal),
  assignees: (signal?: AbortSignal) =>
    get<{ ok: boolean; assignees: Record<string, number> }>("/api/assignees", undefined, signal),
  realms: (signal?: AbortSignal) =>
    get<{ ok: boolean; realms: Record<string, RealmCount>; default: string }>(
      "/api/realms",
      undefined,
      signal,
    ),
  kinds: (signal?: AbortSignal) =>
    get<{ ok: boolean; kinds: Record<string, RealmCount>; none_token: string }>(
      "/api/kinds",
      undefined,
      signal,
    ),
  skills: (signal?: AbortSignal) => get<SkillsPayload>("/api/skills", undefined, signal),
  rule: (id: string, signal?: AbortSignal) =>
    get<{ ok: boolean; rule: import("./types").RuleNode | null }>("/api/rule", { id }, signal),
  search: (q: string, signal?: AbortSignal) =>
    get<{ ok: boolean; results: SearchHit[] }>("/api/search", { q }, signal),
  suggestLinks: (text: string, exclude?: string, signal?: AbortSignal) =>
    get<{ ok: boolean; count: number; suggestions: LinkSuggestion[] }>(
      "/api/suggest-links",
      { text, exclude },
      signal,
    ),
  ruleProposals: (signal?: AbortSignal) =>
    get<import("./types").RuleProposals>("/api/rule-proposals", undefined, signal),
  settings: (signal?: AbortSignal) => get<Settings>("/api/settings", undefined, signal),
  timezones: (signal?: AbortSignal) => get<Timezones>("/api/timezones", undefined, signal),
  status: (signal?: AbortSignal) => get<Status>("/api/status", undefined, signal),
  tasks: (signal?: AbortSignal) =>
    get<import("./types").TasksPayload>("/api/tasks", { limit: 500 }, signal),
  task: (id: string, signal?: AbortSignal) =>
    get<import("./types").TaskDetail>("/api/task", { id }, signal),
  /** Estimated spend for the sidebar meter: 5 hours, today, week, month, year, total. */
  usageSummary: (signal?: AbortSignal) =>
    get<import("./types").UsageSummary>("/api/usage-summary", {}, signal),
  /** The usage report over a range of ISO weeks ("" leaves a bound open). */
  usageReport: (range: { from?: string; to?: string; weeks?: number }, signal?: AbortSignal) =>
    get<import("./types").UsageReportPayload>("/api/usage-report", range, signal),
  trigger: (signal?: AbortSignal) =>
    get<import("./types").TriggerStatus>("/api/trigger", { limit: 5 }, signal),
  agents: (signal?: AbortSignal) =>
    get<import("./types").AgentsStatus>("/api/agents", undefined, signal),
  /** The slash commands (skills, plugin commands) and the installed plugins, read from disk. */
  agentCommands: (signal?: AbortSignal) =>
    get<import("./types").AgentCommands>("/api/agent-commands", undefined, signal),
  agentConversations: (signal?: AbortSignal) =>
    get<import("./types").AgentConversations>("/api/agent-conversations", undefined, signal),
  /** A document an agent wrote, by its path under <home>/exports. */
  document: (path: string, area: "exports" | "attachments" = "exports", signal?: AbortSignal) =>
    get<import("./types").AgentDocument>("/api/document", area === "exports" ? { path } : { path, area }, signal),
  voice: (signal?: AbortSignal) => get<import("./types").VoiceStatus>("/api/voice", undefined, signal),
  customTriggers: (signal?: AbortSignal) =>
    get<import("./types").CustomTriggerList>("/api/custom-triggers", undefined, signal),
  customTrigger: (id: string, signal?: AbortSignal) =>
    get<import("./types").CustomTriggerDetail>("/api/custom-trigger", { id }, signal),
  agentThread: (id: string, signal?: AbortSignal) =>
    get<import("./types").AgentThread>("/api/agent-thread", { id }, signal),
  b2b: (signal?: AbortSignal) =>
    get<import("./types").B2BStatus>("/api/b2b", undefined, signal),
  b2bConversations: (signal?: AbortSignal) =>
    get<import("./types").B2BConversations>("/api/b2b-conversations", undefined, signal),
  /** A page of 20, the latest or the one before `before` (a time). */
  b2bThread: (peer: string, before = "", signal?: AbortSignal) =>
    get<import("./types").B2BThread>("/api/b2b-thread", before ? { peer, before } : { peer }, signal),
  capture: (signal?: AbortSignal) =>
    get<import("./types").CaptureState>("/api/capture", undefined, signal),
  doctor: (signal?: AbortSignal) =>
    get<import("./types").DoctorReport>("/api/doctor", undefined, signal),
  backups: (signal?: AbortSignal) =>
    get<import("./types").BackupList>("/api/backups", undefined, signal),
  uiPrefs: (signal?: AbortSignal) =>
    get<import("./types").UiPrefsPayload>("/api/ui-prefs", undefined, signal),
  /** What consolidation WOULD do. A read: it changes nothing. */
  consolidatePreview: (keepDays: number, signal?: AbortSignal) =>
    get<import("./types").ConsolidationPreview>(
      "/api/consolidate-preview",
      { keep_days: String(keepDays) },
      signal,
    ),
  bundleUrl: (name: string) => `${BASE}/api/bundle${queryString({ name })}`,

  // writes
  addTask: (body: Record<string, unknown>) => post("/api/add-task", body),
  setDue: (id: string, due: string) => post("/api/set-due", { id, due }),
  complete: (id: string, note?: string) => post("/api/complete", { id, note }),
  setStatus: (id: string, status: string, waiting_on?: string) =>
    post("/api/set-status", { id, status, waiting_on }),
  setAssignee: (id: string, assignee: string) => post("/api/set-assignee", { id, assignee }),
  updateNode: (body: Record<string, unknown>) => post("/api/update-node", body),
  link: (src: string, dst: string, rel: string) => post("/api/link", { src, dst, rel }),
  unlink: (src: string, dst: string, rel: string) => post("/api/unlink", { src, dst, rel }),
  proposeIdea: (body: { title: string; detail?: string; realm?: string }) =>
    post<{ ok: boolean; node: GraphNode }>("/api/propose-idea", body),
  assign: (body: Record<string, unknown>) => post("/api/assign", body),
  /** Named, not a loose record: the server reads `canonical` and `duplicate`,
   *  and an object literal is exactly the kind of thing that drifts silently. */
  mergePerson: (canonical: string, duplicate: string) =>
    post("/api/merge-person", { canonical, duplicate }),
  addAlias: (id: string, alias: string) => post("/api/add-alias", { id, alias }),
  /** The brain's icon; `v` busts the cache when a new one is uploaded. */
  agentIconUrl: (v: number | string = 0) => `${BASE}/api/agent-icon${queryString({ v })}`,
  /** `data` is the image, base64; an empty string resets to the default favicon. */
  saveAgentIcon: (data: string) => post("/api/agent-icon", { data }),
  saveSettings: (body: Partial<import("./types").StoredSettings>) => post("/api/settings", body),
  saveNeo4j: (body: Record<string, unknown>) => post("/api/neo4j-config", body),
  testNeo4j: () => post("/api/neo4j-test", {}),
  /** A patch. Secrets are write-only: send one to replace it, list it in
   *  `clear` to remove it, leave it out to keep what is stored. */
  saveTrigger: (body: Record<string, unknown>) =>
    post<{ ok: boolean; config: import("./types").TriggerConfig; runtime: import("./types").TriggerRuntime }>(
      "/api/trigger-config",
      body,
    ),
  /** Open (`enabled: true`) or close a channel. `channel` may be a link. */
  setTriggerChannel: (channel: string, enabled: boolean, name = "") =>
    post<{ ok: boolean; channels: import("./types").TriggerChannels }>("/api/trigger-channel", {
      channel,
      name,
      enabled,
    }),
  /** Let a person tag the brain anywhere on `source` (`enabled: true`), or take them off. */
  setTriggerPerson: (body: { user_id: string; enabled: boolean; name?: string; source?: string }) =>
    post<{ ok: boolean; people: import("./types").TriggerPeople }>("/api/trigger-person", {
      source: "jira",
      ...body,
    }),
  /** Jira accounts whose name or email matches `q`. */
  findTriggerPeople: (q: string, source = "jira") =>
    get<{ ok: boolean; people: import("./types").TriggerPersonMatch[] }>("/api/trigger-people", { q, source }),
  /** Start the tasks a meeting's wake phrases ask for; `dry_run` only says what matches. */
  scanTranscript: (body: { text: string; title?: string; link?: string; dry_run?: boolean }) =>
    post<import("./types").ScanResult>("/api/trigger-scan", body),
  checkTrigger: (only: import("./types").TriggerService) =>
    post<import("./types").TriggerCheck>("/api/trigger-check", { only }),
  /** Make this brain's key pair. Refused when one exists, unless `force`. */
  /** Put a blocked or failed task back in the queue and run it now. */
  reprocessTask: (request_id: string) =>
    post<{ ok: boolean; request_id: string; status: string; was: string }>("/api/trigger-reprocess", {
      request_id,
    }),
  /** Stop a queued or running task; a running session is killed. */
  cancelTask: (request_id: string) =>
    post<{ ok: boolean; request_id: string; status: string; was: string }>("/api/trigger-cancel", {
      request_id,
    }),
  b2bIdentity: (force = false) =>
    post<{ ok: boolean; user_id: string; note: string }>("/api/b2b-identity", force ? { force } : {}),
  /** A patch. The private key is write-only: send one to import it, list it
   *  in `clear` to remove it, leave it out to keep what is stored. */
  saveB2B: (body: Record<string, unknown>) =>
    post<{ ok: boolean; config: import("./types").B2BConfig; runtime: import("./types").B2BRuntime }>(
      "/api/b2b-config",
      body,
    ),
  /** Add (`url`), change (`id`) or remove (`id` and `remove`) a broker. */
  saveB2BBroker: (body: Record<string, unknown>) =>
    post<{ ok: boolean; config: import("./types").B2BConfig }>("/api/b2b-broker", body),
  checkB2BBroker: (id: string) =>
    post<import("./types").B2BBrokerCheck>("/api/b2b-broker-check", { id }),
  /** Connect (`enabled: true`) or disconnect a peer. The row is kept either way. */
  setB2BPeer: (body: {
    peer_id: string;
    enabled: boolean;
    name?: string;
    broker_id?: string;
    person_id?: string;
  }) =>
    post<{ ok: boolean; peer?: import("./types").B2BPeer; peers: import("./types").B2BPeer[] }>(
      "/api/b2b-peer",
      body,
    ),
  /** Let a user id send to this brain (`enabled: true`), or block it again. */
  setB2BAllowed: (body: { user_id: string; enabled: boolean; name?: string }) =>
    post<{ ok: boolean; allowed: import("./types").B2BAllowed[] }>("/api/b2b-allowed", body),
  /** `human`: typed in the Messages chat. Every other sender is shown as automated. */
  sendB2B: (to: string, text: string, human = false, files: string[] = []) =>
    post<import("./types").B2BSendResult>("/api/b2b-send", {
      to,
      text,
      ...(human ? { human } : {}),
      ...(files.length ? { files } : {}),
    }),
  /** Save a file the owner attached to the chat under <home>/attachments (base64). No model call. */
  attachFile: (name: string, data: string) =>
    post<import("./types").AgentAttachResult>("/api/agent-attach", { name, data }),
  /** Opens the Ledger Agent floating box on this Mac. No model call. */
  openAgentPanel: (body: { cwd?: string } = {}) =>
    post<{ ok: boolean; cwd?: string; building?: boolean; already_open?: boolean; error?: string }>("/api/agent-panel", body),
  /** Starts a model call: the reply runs in the background, poll the thread. */
  sendAgent: (body: {
    text: string;
    attachments?: string[];
    conversation?: string;
    connector?: string;
    model?: string;
    full_tools?: boolean;
    context_turns?: number;
  }) => post<import("./types").AgentSendResult>("/api/agent-send", body),
  /** Create (no id) or change a conversation; only the keys sent change. */
  saveAgentConversation: (body: {
    id?: string;
    title?: string;
    connector?: string;
    model?: string;
    full_tools?: boolean;
    context_turns?: number;
    hidden?: boolean;
    verbosity?: string;
    tts?: string;
    draft?: string;
    draft_at?: string;
  }) =>
    post<{ ok: boolean; conversation: import("./types").AgentConversation; created: boolean }>(
      "/api/agent-conversation",
      body,
    ),
  /** Validate (check_only) or save a custom trigger's YAML; never written when it does not validate. */
  saveCustomTrigger: (id: string, text: string, checkOnly = false) =>
    post<import("./types").CustomTriggerVerdict>("/api/custom-trigger", { id, text, check_only: checkOnly }),
  /** Run a saved trigger's blocks and say what it would create. Creates nothing. */
  dryRunCustomTrigger: (id: string) =>
    post<import("./types").CustomTriggerDryRun>("/api/custom-trigger-dry-run", { id }),
  enableCustomTrigger: (id: string, enabled: boolean) =>
    post("/api/custom-trigger-enable", { id, enabled }),
  saveVoice: (body: Partial<import("./types").VoiceConfig>) =>
    post<import("./types").VoiceStatus>("/api/voice-config", body),
  /** Translate one block; skipped by the server when it is already in the target. Starts a model call. */
  translateBlock: (text: string, target: string, signal?: AbortSignal) =>
    post<import("./types").VoiceTranslation>("/api/voice-translate", { text, target }, signal),
  /** One block as audio (WAV), or null when nothing is left to say after cleaning it. */
  speak: async (text: string, language = "", signal?: AbortSignal): Promise<Blob | null> => {
    const res = await fetch(`${BASE}/api/voice-speak`, {
      method: "POST",
      signal,
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text, language }),
    });
    if (res.status === 204) return null;
    if (!res.ok) {
      const body = (await res.json().catch(() => ({}))) as { error?: string };
      throw new ApiError(body.error || `could not speak (${res.status})`, res.status);
    }
    return res.blob();
  },
  retryAgent: (id: string) => post<{ ok: boolean; reply_id: string }>("/api/agent-retry", { id }),
  /** Stop a queued or running reply; it fails as cancelled, so Retry runs it again. */
  cancelAgent: (id: string) => post<{ ok: boolean; reply_id: string; was: string }>("/api/agent-cancel", { id }),
  /** Turn an installed plugin on or off in ~/.claude/settings.json; answers the new list. */
  setAgentPlugin: (key: string, enabled: boolean) =>
    post<import("./types").AgentCommands>("/api/agent-plugin", { key, enabled }),
  saveAgentsConfig: (body: Partial<{
    connectors: Partial<import("./types").AgentConnector>[];
    default_connector: string;
    context_turns: number;
    recall_limit: number;
    recall_budget: number;
    max_concurrent: number;
  }>) => post<import("./types").AgentsStatus>("/api/agent-config", body),
  flush: () => post("/api/flush", {}),
  /**
   * Consolidate. Refused without `confirm`, by the server — the preview is not
   * decoration, it is the step before this one.
   */
  prune: (keep_days: number, confirm: boolean) =>
    post<import("./types").MaintainResult>("/api/prune", { keep_days, confirm }),
  proposeRules: () => post("/api/propose-rules", {}),
  acceptRule: (id: string) => post("/api/accept-rule", { id }),
  rejectRule: (id: string) => post("/api/reject-rule", { id }),
  setCapture: (enabled: boolean) =>
    post<{ ok: boolean; capture: import("./types").CaptureState }>("/api/capture", { enabled }),
  saveUiPrefs: (prefs: import("./types").UiPrefs) =>
    post<{ ok: boolean; synced: boolean }>("/api/ui-prefs", { prefs }),
  renameRealm: (old: string, name: string) =>
    post<{
      ok: boolean;
      error?: string;
      from: string;
      to: string;
      moved: number;
      merged_into_existing: boolean;
      default?: string;
      warning?: string;
    }>("/api/rename-realm", { old, new: name }),
  backup: (label?: string) =>
    post<import("./types").BackupList & { created: import("./types").Backup }>(
      "/api/backup",
      { label },
    ),
  createRule: (body: Record<string, unknown>) => post("/api/create-rule", body),
  createSkill: (body: Record<string, unknown>) => post("/api/create-skill", body),
  exportSkills: (body: Record<string, unknown> = {}) => post<SyncResult>("/api/export-skills", body),
  ingestSkills: (body: Record<string, unknown> = {}) => post<SyncResult>("/api/ingest-skills", body),
  createProcess: (body: { title: string; body?: string; trigger?: string; realm?: string; id?: string }) =>
    post<{ ok: boolean; node: { id: string; realm: string }; action: string }>("/api/create-process", body),
  exportBundle: (skills: string[], rules: string[], processes: string[] = []) =>
    post<ExportBundleResult>("/api/export-bundle", { skills, rules, processes }),
  uploadBundle: (filename: string, data: string) =>
    post<UploadBundleResult>("/api/upload-bundle", { filename, data }),
  importBundle: (token: string, select: Record<string, ImportChoice>, processRealm?: string) =>
    post<ImportBundleResult>(
      "/api/import-bundle",
      processRealm ? { token, select, process_realm: processRealm } : { token, select },
    ),
  scripts: (signal?: AbortSignal) => get<ScriptsPayload>("/api/scripts", undefined, signal),
  script: (name: string) => get<ScriptDetail>("/api/script", { name }),
  exportScripts: (names: string[] = []) => post<ExportScriptsResult>("/api/export-scripts", { names }),
  uploadScripts: (filename: string, data: string) =>
    post<UploadScriptsResult>("/api/upload-scripts", { filename, data }),
  createScript: (name: string, text: string, executable = true, replace = false) =>
    post<import("./types").CreateScriptResult>("/api/create-script", { name, text, executable, replace }),
  importScripts: (token: string, select: Record<string, ScriptChoice>) =>
    post<ImportScriptsResult>("/api/import-scripts", { token, select }),
  deleteContracts: (ids: string[], force = false) =>
    post<import("./types").DeleteContractsResult>("/api/delete-contracts", { ids, force }),
};

export type Api = typeof api;

/** The raw bytes of a document, for an iframe. */
export const documentFileUrl = (path: string, area: "exports" | "attachments" = "exports") =>
  `${BASE}/api/document-file${queryString(area === "exports" ? { path } : { path, area })}`;
