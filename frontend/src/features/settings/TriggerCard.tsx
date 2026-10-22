import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { api } from "@/api/client";
import { useAsync } from "@/hooks/useAsync";
import { Badge, Button, Card, Checkbox, Field, Spinner, StatusMessage, TextArea, TextInput, Icon } from "@/components";
import type { BadgeTone } from "@/components";
import type {
  GitHubEventKind,
  ScanResult,
  JiraEventKind,
  ScriptsPayload,
  SlackCommand,
  SlackEventKind,
  TriggerChannelRow,
  TriggerChannels,
  TriggerCheck,
  TriggerConfig,
  TriggerEventSetting,
  TriggerPeople,
  TriggerPersonMatch,
  TriggerPersonRow,
  TriggerSecret,
  TriggerService,
  TriggerStatus,
  TriggerVariable,
  TriggerWatcher,
  WakePhrase,
} from "@/api/types";
import { CommandsEditor, EventsEditor, FoldButton, PhrasesEditor, spokenPhrase, useFold } from "./TriggerActions";
import type { EventKindInfo, ScriptsState } from "./TriggerActions";
import "./SettingsSection.css";

type Out = { text: string; tone: "info" | "error" | "success" } | null;

/**
 * Summoning the brain from Slack, Jira and GitHub (docs/trigger.md).
 *
 * Everything here writes `<home>/trigger.json` through the server, which
 * restarts its watchers on the new file — so a switch flipped here is a watcher
 * started or stopped, not a preference that waits for a restart.
 *
 * Tokens are WRITE-ONLY. The server says whether one is set and where from,
 * never what it is, so a blank token field means "keep what is stored", and
 * removing one is its own explicit button. A token set in the server's
 * environment outranks the file, and the field says so instead of pretending a
 * save would change it.
 */
export function TriggerCard({ writable }: { writable: boolean }) {
  const status = useAsync<TriggerStatus>((signal) => api.trigger(signal), []);
  const scriptsReq = useAsync<ScriptsPayload>((signal) => api.scripts(signal), []);
  const scripts: ScriptsState = {
    list: scriptsReq.data?.scripts ?? [],
    dir: scriptsReq.data?.dir ?? "",
    reload: () => scriptsReq.reload(),
  };

  if (status.loading && !status.data) return <Spinner label="reading triggers" />;
  if (status.error) return <StatusMessage tone="error">{status.error}</StatusMessage>;
  if (!status.data) return null;
  const { config, runtime, watching } = status.data;
  const reload = () => status.reload();

  return (
    <Card>
      <h2 className="ml-settings__h">
        Triggers{" "}
        <Badge tone={runtime.running ? "success" : "neutral"}>
          {runtime.running ? "listening" : "off"}
        </Badge>
      </h2>
      <p className="ml-settings__hint">
        Mention the brain in a Slack thread, or in a Jira comment that mentions you with the marker,
        and a headless Claude Code session answers there as you, signed as your second brain. On
        GitHub, the pull request changes you pick below start one too. Only conversations you opened
        with your own mention accept anyone else&rsquo;s. Saved to{" "}
        <code>{config.path}</code>, mode 600.
      </p>

      {!watching ? (
        <StatusMessage tone="info">
          This server was started with <code>--no-trigger</code> or <code>--no-write</code>, so it
          runs no watchers. What you save here is kept for the next start without them.
        </StatusMessage>
      ) : null}
      {config.problem ? <StatusMessage tone="error">{config.problem}</StatusMessage> : null}

      <VariablesGuide variables={config.variables} />
      <MasterSwitch config={config} writable={writable} onSaved={reload} />

      <SlackPanel
        config={config}
        channels={status.data.channels}
        watcher={runtime.slack}
        writable={writable}
        onSaved={reload}
        scripts={scripts}
      />
      <JiraPanel
        config={config}
        people={status.data.people}
        watcher={runtime.jira}
        writable={writable}
        onSaved={reload}
        scripts={scripts}
      />
      <GitHubPanel config={config} watcher={runtime.github} writable={writable} onSaved={reload} scripts={scripts} />
      <MeetingPanel config={config} writable={writable} onSaved={reload} scripts={scripts} />
      <ClaudePanel config={config} writable={writable} onSaved={reload} />
      <Recent status={status.data} />

      {!writable ? <span className="ml-settings__note">read-only server</span> : null}
    </Card>
  );
}

// ---------------------------------------------------------------- run commands
const SOURCE_NAME: Record<string, string> = {
  slack: "Slack",
  jira: "Jira",
  github: "GitHub",
  meeting: "Meetings",
};

/**
 * What a run command can say, at the top of the card: every trigger's command
 * uses the same names, so they are documented once, here.
 */
function VariablesGuide({ variables }: { variables: TriggerVariable[] }) {
  return (
    <details className="ml-trigger__vars">
      <summary>
        Run commands and their variables <Badge tone="neutral">{variables.length}</Badge>
      </summary>
      <p className="ml-settings__hint">
        Each trigger has a run command: what the session is told to do when it fires. Write it in
        your own words and put a variable in braces where a value belongs, for example{" "}
        <code>{"{trigger_source} sent this task. Pick it up and reply to {trigger_user} at {trigger_link}."}</code>{" "}
        Left empty, a trigger gets the built-in handling. Whatever the command says, the brain&rsquo;s
        rules still hold, and the message is still quoted to the session as the request, not as your
        instruction. <code>{"{trigger_content}"}</code> is written by whoever triggered it, so use
        it as their words.
      </p>
      <div className="ml-trigger__vars-table">
        <table>
          <thead>
            <tr>
              <th>Variable</th>
              <th>On</th>
              <th>What it is</th>
              <th>Example</th>
            </tr>
          </thead>
          <tbody>
            {variables.map((v) => (
              <tr key={v.name}>
                <td>
                  <code>{`{${v.name}}`}</code>
                </td>
                <td>{v.global ? "every trigger" : v.sources.map((x) => SOURCE_NAME[x] ?? x).join(", ")}</td>
                <td>{v.description}</td>
                <td>
                  <code>{v.example}</code>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

// ---------------------------------------------------------------- saving
function useSave(onSaved: () => void) {
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState<Out>(null);
  const save = async (patch: Record<string, unknown>, done = "Saved.") => {
    setBusy(true);
    setOut(null);
    try {
      await api.saveTrigger(patch);
      setOut({ text: done, tone: "success" });
      onSaved();
      return true;
    } catch (err) {
      setOut({ text: err instanceof Error ? err.message : String(err), tone: "error" });
      return false;
    } finally {
      setBusy(false);
    }
  };
  return { busy, out, save, setOut };
}

function MasterSwitch({
  config,
  writable,
  onSaved,
}: {
  config: TriggerConfig;
  writable: boolean;
  onSaved: () => void;
}) {
  const { busy, out, save } = useSave(onSaved);
  const [signature, setSignature] = useState(config.signature_template);
  const [notEnabled, setNotEnabled] = useState(config.refusals.not_enabled);
  const [policy, setPolicy] = useState(config.refusals.policy);
  const [notAllowed, setNotAllowed] = useState(config.refusals.not_allowed ?? "");
  useEffect(() => {
    setSignature(config.signature_template);
    setNotEnabled(config.refusals.not_enabled);
    setPolicy(config.refusals.policy);
    setNotAllowed(config.refusals.not_allowed ?? "");
  }, [config.signature_template, config.refusals.not_enabled, config.refusals.policy, config.refusals.not_allowed]);
  const noModel = config.refuse_without_model !== false;

  return (
    <>
      <div className="ml-settings__switch">
        <Checkbox
          label="Listen for mentions"
          checked={config.stored_enabled}
          disabled={!writable || busy}
          onChange={(v) => save({ enabled: v }, v ? "Triggers on." : "Triggers off.")}
        />
        <p className="ml-settings__hint ml-settings__hint--switch">
          The master switch. Off, no service listens, whatever its own switch says.
        </p>
      </div>

      <h3 className="ml-settings__h3">Replies</h3>
      <Field
        label="Signature"
        hint={
          <>
            The last line of every reply, never translated. <code>{"{owner}"}</code> is your
            name and <code>{"{brain}"}</code> the brain&rsquo;s; now it reads <em>{config.signature.replace(/^_|_$/g, "")}</em>. It is also how
            the brain knows its own words, so keep it distinctive.
          </>
        }
      >
        {(id) => <TextInput id={id} value={signature} onChange={(e) => setSignature(e.target.value)} />}
      </Field>
      <div className="ml-settings__grid">
        <Field
          label="Refusal: not enabled here"
          hint="For a mention in a conversation you have not opened. Sent by the brain itself, once per conversation."
        >
          {(id) => <TextInput id={id} value={notEnabled} onChange={(e) => setNotEnabled(e.target.value)} />}
        </Field>
        <Field
          label="Refusal: against a rule"
          hint={
            <>
              For a request that breaks one of your rules or guardrails. The session sends it when
              it declines. <code>{"{reason}"}</code> is the one-line why.
            </>
          }
        >
          {(id) => <TextInput id={id} value={policy} onChange={(e) => setPolicy(e.target.value)} />}
        </Field>
        <Field
          label="Refusal: not allowed"
          hint="For someone on the blocked list, and for a command only you may run. Sent by the brain itself."
        >
          {(id) => <TextInput id={id} value={notAllowed} onChange={(e) => setNotAllowed(e.target.value)} />}
        </Field>
      </div>
      <div className="ml-settings__switch">
        <Checkbox
          label="Refuse without a model call"
          checked={noModel}
          disabled={!writable || busy}
          onChange={(v) =>
            save({ refuse_without_model: v }, v ? "Refusals make no model call." : "Refusals may use a model.")
          }
        />
        <p className="ml-settings__hint ml-settings__hint--switch">
          On, someone the brain turns away costs nothing: the notice goes out exactly as written, through
          the Slack user token or the Jira and GitHub tokens, and without a Slack user token it is only
          logged. Off, it is translated, and without a user token posted through your Slack MCP.
        </p>
      </div>
      <div className="ml-settings__switch">
        <Checkbox
          label="Reply in the sender's language"
          checked={config.translate_notices && !noModel}
          disabled={!writable || busy || noModel}
          onChange={(v) => save({ translate_notices: v }, v ? "Notices will be translated." : "Notices go out as written.")}
        />
        <p className="ml-settings__hint ml-settings__hint--switch">
          The brain&rsquo;s answers always follow the language of the message. This is for the two
          notices above: on, each is rewritten in the sender&rsquo;s language (Hebrew for someone who
          wrote in Hebrew, English for the same person writing in English), which is a small model
          call. Off, they
          go out exactly as written, with none.
        </p>
      </div>
      <div className="ml-settings__foot">
        <Button
          disabled={!writable || busy}
          onClick={() => save({ signature, refusals: { not_enabled: notEnabled, policy, not_allowed: notAllowed } })}
        >
          Save replies
        </Button>
      </div>
      {out ? <StatusMessage tone={out.tone}>{out.text}</StatusMessage> : null}
    </>
  );
}

// ---------------------------------------------------------------- panels
const WATCH_TONE: Record<string, BadgeTone> = {
  connected: "success",
  polling: "success",
  connecting: "accent",
  reconnecting: "warn",
  misconfigured: "danger",
  erroring: "danger",
  unavailable: "danger",
  off: "neutral",
};

function Panel({
  title,
  service,
  watcher,
  switchLabel,
  switchHint,
  enabled,
  writable,
  busy,
  onToggle,
  onSave,
  out,
  children,
  after,
}: {
  title: string;
  service: TriggerService;
  watcher?: TriggerWatcher;
  switchLabel?: string;
  switchHint?: string;
  enabled?: boolean;
  writable: boolean;
  busy: boolean;
  onToggle?: (v: boolean) => void;
  onSave: () => void;
  out: Out;
  children: ReactNode;
  /** Below the fields: something with its own saves, like the channel list. */
  after?: ReactNode;
}) {
  const [check, setCheck] = useState<TriggerCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const [checkErr, setCheckErr] = useState<string | null>(null);
  const fold = useFold(`panel.${service}`);

  const runCheck = async () => {
    setChecking(true);
    setCheck(null);
    setCheckErr(null);
    try {
      setCheck(await api.checkTrigger(service));
    } catch (err) {
      setCheckErr(err instanceof Error ? err.message : String(err));
    } finally {
      setChecking(false);
    }
  };

  return (
    <section className="ml-settings__preview" aria-label={title}>
      <h3 className="ml-settings__h3">
        <FoldButton open={fold.open} onToggle={fold.toggle}>
          {title}
        </FoldButton>
        {switchLabel && !enabled ? <Badge tone="neutral">switched off</Badge> : null}
        {watcher ? (
          <Badge tone={WATCH_TONE[watcher.state] ?? "neutral"} title={watcher.at}>
            {watcher.state}
          </Badge>
        ) : null}
      </h3>
      {watcher?.last_error ? <StatusMessage tone="error">{watcher.last_error}</StatusMessage> : null}
      {fold.open ? (
        <>
      {switchLabel && onToggle ? (
        <div className="ml-settings__switch">
          <Checkbox
            label={switchLabel}
            checked={!!enabled}
            disabled={!writable || busy}
            onChange={onToggle}
          />
          {switchHint ? (
            <p className="ml-settings__hint ml-settings__hint--switch">{switchHint}</p>
          ) : null}
        </div>
      ) : null}

      <div className="ml-settings__grid">{children}</div>
      {after}

      <div className="ml-settings__foot">
        <Button variant="primary" disabled={!writable || busy} onClick={onSave}>
          {busy ? "saving…" : `Save ${title}`}
        </Button>
        <Button disabled={!writable || checking} onClick={runCheck}>
          {checking ? "checking…" : `Check ${title}`}
        </Button>
      </div>
      {out ? <StatusMessage tone={out.tone}>{out.text}</StatusMessage> : null}
      {checkErr ? <StatusMessage tone="error">{checkErr}</StatusMessage> : null}
      {check ? <CheckResult check={check} title={title} /> : null}
        </>
      ) : null}
    </section>
  );
}

function CheckResult({ check, title }: { check: TriggerCheck; title: string }) {
  return (
    <StatusMessage tone={check.ready ? "success" : "error"}>
      <strong>{check.ready ? `${title} is ready.` : `${title} is not ready.`}</strong>
      <ul className="ml-settings__list">
        {check.problems.map((p) => (
          <li key={`p-${p}`}>{p}</li>
        ))}
        {check.notes.map((n) => (
          <li key={`n-${n}`}>{n}</li>
        ))}
      </ul>
    </StatusMessage>
  );
}

/**
 * A token field. Blank keeps the stored one; typing replaces it; "Remove"
 * deletes it. Set in the environment, it cannot be changed from here at all,
 * and the field is disabled rather than silently ineffective.
 */
function SecretField({
  label,
  secret,
  value,
  onChange,
  onRemove,
  writable,
  placeholder,
  hint,
}: {
  label: string;
  secret?: TriggerSecret;
  value: string;
  onChange: (v: string) => void;
  onRemove: () => void;
  writable: boolean;
  placeholder: string;
  hint: string;
}) {
  const fromEnv = secret?.source === "env";
  const state = fromEnv
    ? `Set by $${secret?.env_var} in the server's environment, which outranks this page.`
    : secret?.stored
      ? "One is stored. Leave blank to keep it."
      : "None stored.";
  return (
    <Field
      label={
        <>
          {label}{" "}
          <Badge tone={secret?.set ? "success" : "neutral"}>
            {fromEnv ? "env" : secret?.stored ? "stored" : "not set"}
          </Badge>
        </>
      }
      hint={`${hint} ${state}`}
    >
      {(id) => (
        <div className="ml-settings__foot">
          <TextInput
            id={id}
            type="password"
            autoComplete="new-password"
            placeholder={fromEnv ? "from the environment" : placeholder}
            value={value}
            disabled={fromEnv || !writable}
            onChange={(e) => onChange(e.target.value)}
          />
          {secret?.stored && !fromEnv ? (
            <Button size="sm" disabled={!writable} onClick={onRemove}>
              Remove
            </Button>
          ) : null}
        </div>
      )}
    </Field>
  );
}

function txt(label: string, value: string, set: (v: string) => void, hint: string, placeholder = "") {
  return (
    <Field label={label} hint={hint}>
      {(id) => (
        <TextInput id={id} value={value} placeholder={placeholder} onChange={(e) => set(e.target.value)} />
      )}
    </Field>
  );
}

function num(label: string, value: string, set: (v: string) => void, hint: string, min: number, max: number) {
  return (
    <Field label={label} hint={hint}>
      {(id) => (
        <TextInput
          id={id}
          type="number"
          min={min}
          max={max}
          value={value}
          onChange={(e) => set(e.target.value)}
        />
      )}
    </Field>
  );
}

const list = (v: string) =>
  v
    .split(/[\s,]+/)
    .map((x) => x.trim())
    .filter(Boolean);

/** Local edits to a source's events: one kind, one patch at a time. */
function useEvents<K extends string>(stored: Record<K, TriggerEventSetting>) {
  const [events, setEvents] = useState(stored);
  useEffect(() => setEvents(stored), [stored]);
  const setEvent = (kind: K, patch: Partial<TriggerEventSetting>) =>
    setEvents((cur) => ({ ...cur, [kind]: { ...cur[kind], ...patch } }));
  return { events, setEvent };
}

const SLACK_EVENTS: EventKindInfo<SlackEventKind>[] = [
  {
    kind: "mention", label: "Someone @mentions the brain", always: true,
    hint: "Always on: the gate decides who gets through. A command below runs instead when one matches.",
    runHint: "What the session does with a Slack mention. Empty: read the thread, do what it asks, reply there.",
    placeholder: "{trigger_user} mentioned me in Slack. Read {trigger_link}, do what they ask, and reply in the thread.",
  },
  { kind: "joined_channel", label: "The brain is added to a channel", hint: "{trigger_user} is who added it, {slack_channel_name} where. The app must subscribe to member_joined_channel. Opens nothing to anyone." },
  { kind: "left_channel", label: "The brain is removed from a channel", hint: "The app must subscribe to member_left_channel. Live only: nothing to catch up from after a restart." },
];

function SlackPanel({
  config,
  channels,
  watcher,
  writable,
  onSaved,
  scripts,
}: {
  config: TriggerConfig;
  channels: TriggerChannels;
  watcher: TriggerWatcher;
  writable: boolean;
  onSaved: () => void;
  scripts: ScriptsState;
}) {
  const s = config.slack;
  const { busy, out, save } = useSave(onSaved);
  const [owner, setOwner] = useState(s.owner_user_id ?? "");
  const [lookback, setLookback] = useState(String(s.thread_lookback_days ?? 7));
  const [allowed, setAllowed] = useState((s.allowed_users ?? []).join(", "));
  const [blocked, setBlocked] = useState((s.blocked_users ?? []).join(", "));
  const [dms, setDms] = useState(!!s.dms);
  const [dmsOpen, setDmsOpen] = useState(!!s.dms_open);
  const { events, setEvent } = useEvents(s.events);
  const [commands, setCommands] = useState<SlackCommand[]>(s.commands ?? []);
  const [appToken, setAppToken] = useState("");
  const [botToken, setBotToken] = useState("");
  const [userToken, setUserToken] = useState("");
  useEffect(() => {
    setOwner(s.owner_user_id ?? "");
    setLookback(String(s.thread_lookback_days ?? 7));
    setAllowed((s.allowed_users ?? []).join(", "));
    setBlocked((s.blocked_users ?? []).join(", "));
    setCommands(s.commands ?? []);
    setDms(!!s.dms);
    setDmsOpen(!!s.dms_open);
  }, [s.owner_user_id, s.thread_lookback_days, s.allowed_users, s.blocked_users, s.commands, s.dms, s.dms_open]);

  const submit = async () => {
    const slack: Record<string, unknown> = {
      owner_user_id: owner,
      thread_lookback_days: Number(lookback),
      allowed_users: list(allowed).map((x) => x.toUpperCase()),
      blocked_users: list(blocked).map((x) => x.toUpperCase()),
      dms,
      dms_open: dmsOpen,
      events,
      commands,
    };
    // Only a typed token travels. Blank means keep, never "set to empty".
    if (appToken) slack.app_token = appToken;
    if (botToken) slack.bot_token = botToken;
    if (userToken) slack.user_token = userToken;
    if (await save({ slack }, "Slack saved.")) {
      setAppToken("");
      setBotToken("");
      setUserToken("");
    }
  };
  const remove = (name: string) => save({ clear: [name] }, `Removed ${name}.`);
  const noModel = config.refuse_without_model !== false;

  return (
    <Panel
      title="Slack"
      service="slack"
      watcher={watcher}
      switchLabel="Listen on Slack"
      switchHint="A Socket Mode connection: outbound only, no public endpoint. The app needs no chat:write; replies go out as you."
      enabled={s.enabled}
      writable={writable}
      busy={busy}
      onToggle={(v) => save({ slack: { enabled: v } }, v ? "Slack on." : "Slack off.")}
      onSave={submit}
      out={out}
      after={
        <>
          <div className="ml-settings__switch">
            <Checkbox
              label="Listen in your direct messages"
              checked={dms}
              disabled={!writable || busy}
              onChange={setDms}
            />
            <p className="ml-settings__hint ml-settings__hint--switch">
              Someone tags the brain in a DM or group DM with you, and it answers there as you. The bot can
              never be in those chats, so they are heard through your user token: it needs the user scopes
              im:history and mpim:history, and the app subscribed to message.im and message.mpim under
              &ldquo;Subscribe to events on behalf of users&rdquo;. Your own tag in a DM opens that DM to
              the other person.
            </p>
          </div>
          <div className="ml-settings__switch">
            <Checkbox
              label="Anyone who messages you directly may summon it"
              checked={dmsOpen}
              disabled={!writable || busy || !dms}
              onChange={setDmsOpen}
            />
            <p className="ml-settings__hint ml-settings__hint--switch">
              Every DM open at once, without your tag first. <strong>Anyone in the workspace</strong> can then
              run a session as you by messaging you. Blocked people still cannot.
            </p>
          </div>
          <ChannelsCard channels={channels} writable={writable} onChanged={onSaved} />
          <EventsEditor
            source="slack"
            title="Slack events"
            intro="Each is its own switch, with what happens when it does: a Claude session told the run command, a call to an API, or one of your scripts. The last two cost no model call."
            kinds={SLACK_EVENTS}
            events={events}
            onChange={setEvent}
            variables={config.variables}
            writable={writable}
            busy={busy}
            scripts={scripts}
          />
          <CommandsEditor
            commands={commands}
            onChange={setCommands}
            variables={config.variables}
            writable={writable}
            busy={busy}
            scripts={scripts}
          />
        </>
      }
    >
      {txt("Your Slack user id", owner, setOwner, "Your own U… id. Your mention opens a thread; nobody else's can.", "U0123ABCD")}
      {num("Thread look-back (days)", lookback, setLookback, "How far back catch-up looks for threads with new replies.", 0, 90)}
      {txt("Allowed people", allowed, setAllowed, "Slack user ids, comma-separated. They may summon the brain in any conversation, as if it were open.", "U0123ABCD, U0456EFGH")}
      {txt("Blocked people", blocked, setBlocked, "They get the not-allowed notice, even in an open channel, and nothing else happens. Never you.", "U0789IJKL")}
      <SecretField
        label="App token"
        secret={config.secrets["slack.app_token"]}
        value={appToken}
        onChange={setAppToken}
        onRemove={() => remove("slack.app_token")}
        writable={writable}
        placeholder="xapp-…"
        hint="App-level token with connections:write. Opens the socket."
      />
      <SecretField
        label="Bot token"
        secret={config.secrets["slack.bot_token"]}
        value={botToken}
        onChange={setBotToken}
        onRemove={() => remove("slack.bot_token")}
        writable={writable}
        placeholder="xoxb-…"
        hint="Reads mentions and history. Never writes."
      />
      <SecretField
        label="User token"
        secret={config.secrets["slack.user_token"]}
        value={userToken}
        onChange={setUserToken}
        onRemove={() => remove("slack.user_token")}
        writable={writable}
        placeholder="xoxp-…"
        hint={
          noModel
            ? "Posts refusal notices as you, directly, with no model call. Without it a refusal is only logged."
            : "Optional. Posts refusal notices as you, directly. Without it they go out through your Slack MCP instead, as you all the same, in a short headless session."
        }
      />
    </Panel>
  );
}

const JIRA_EVENTS: EventKindInfo<JiraEventKind>[] = [
  {
    kind: "mention", label: "A comment mentions you with the marker", always: true,
    hint: "Always on: the gate decides who gets through.",
    runHint: "What the session does with a marked Jira comment. Empty: read the issue, do what it asks, reply with a comment.",
    placeholder: "{trigger_user} asked for this on {jira_issue} ({trigger_title}). Do it and comment on the issue.",
  },
  { kind: "transitioned", label: "A watched issue changes status", hint: "{jira_from} and {jira_to} are the statuses; {trigger_user} who moved it, you included." },
  { kind: "assigned", label: "A watched issue is assigned", hint: "{jira_assignee} is whom to, {jira_previous_assignee} who had it, {trigger_user} who did it." },
  { kind: "unassigned", label: "A watched issue is unassigned", hint: "{jira_previous_assignee} is who had it." },
  { kind: "comment", label: "A new comment on a watched issue", hint: "Anyone's but yours, and not one that is already a mention." },
];

function JiraPanel({
  config,
  people,
  watcher,
  writable,
  onSaved,
  scripts,
}: {
  config: TriggerConfig;
  people: TriggerPeople;
  watcher: TriggerWatcher;
  writable: boolean;
  onSaved: () => void;
  scripts: ScriptsState;
}) {
  const j = config.jira;
  const { busy, out, save } = useSave(onSaved);
  const [baseUrl, setBaseUrl] = useState(j.base_url ?? "");
  const [email, setEmail] = useState(j.email ?? "");
  const [account, setAccount] = useState(j.owner_account_id ?? "");
  const [marker, setMarker] = useState(j.marker ?? "");
  const [poll, setPoll] = useState(String(j.poll_seconds ?? 60));
  const [jql, setJql] = useState(j.jql ?? "");
  const [watch, setWatch] = useState(j.watch_jql ?? "");
  const [catchUp, setCatchUp] = useState(String(j.max_catchup_hours ?? 72));
  const { events, setEvent } = useEvents(j.events);
  const [token, setToken] = useState("");
  useEffect(() => {
    setBaseUrl(j.base_url ?? "");
    setEmail(j.email ?? "");
    setAccount(j.owner_account_id ?? "");
    setMarker(j.marker ?? "");
    setPoll(String(j.poll_seconds ?? 60));
    setJql(j.jql ?? "");
    setWatch(j.watch_jql ?? "");
    setCatchUp(String(j.max_catchup_hours ?? 72));
  }, [j.base_url, j.email, j.owner_account_id, j.marker, j.poll_seconds, j.jql, j.watch_jql, j.max_catchup_hours]);

  const submit = async () => {
    const jira: Record<string, unknown> = {
      base_url: baseUrl,
      email,
      owner_account_id: account,
      marker,
      poll_seconds: Number(poll),
      jql,
      watch_jql: watch,
      max_catchup_hours: Number(catchUp),
      events,
    };
    if (token) jira.token = token;
    if (await save({ jira }, "Jira saved.")) setToken("");
  };

  return (
    <Panel
      title="Jira"
      service="jira"
      watcher={watcher}
      switchLabel="Poll Jira"
      switchHint="Reads new comments and changes on a timer with your API token. A comment is a mention when it mentions you AND carries the marker."
      enabled={j.enabled}
      writable={writable}
      busy={busy}
      onToggle={(v) => save({ jira: { enabled: v } }, v ? "Jira polling on." : "Jira polling off.")}
      onSave={submit}
      out={out}
      after={
        <>
          <PeopleCard people={people} writable={writable} onChanged={onSaved} />
          <EventsEditor
            source="jira"
            title="Jira events"
            intro="A change counts on the issues the watch JQL above picks. Each is its own switch, with what happens when it does."
            kinds={JIRA_EVENTS}
            events={events}
            onChange={setEvent}
            variables={config.variables}
            writable={writable}
            busy={busy}
            scripts={scripts}
          />
        </>
      }
    >
      {txt("Site URL", baseUrl, setBaseUrl, "Your Atlassian site.", "https://your-site.atlassian.net")}
      {txt("Email", email, setEmail, "The account the API token belongs to.", "you@example.com")}
      {txt("Your account id", account, setAccount, "Left empty, it is looked up from the token.", "optional")}
      {txt("Marker", marker, setMarker, "The text that says a comment is for the brain. Case and accents do not matter.", "@brain")}
      {num("Poll every (seconds)", poll, setPoll, "15 to 3600.", 15, 3600)}
      {num("Catch-up limit (hours)", catchUp, setCatchUp, "After a long gap, look back no further than this.", 1, 720)}
      {txt("Extra JQL", jql, setJql, "ANDed onto every search, to narrow which issues are read.", "project in (PROJ, OPS)")}
      {txt("Watch JQL", watch, setWatch, "Which issues a transition, an assignment or a comment counts on.", "assignee = currentUser() OR watcher = currentUser()")}
      <SecretField
        label="API token"
        secret={config.secrets["jira.token"]}
        value={token}
        onChange={setToken}
        onRemove={() => save({ clear: ["jira.token"] }, "Removed jira.token.")}
        writable={writable}
        placeholder="Atlassian API token"
        hint="From id.atlassian.com, for the email above."
      />
    </Panel>
  );
}

/** The GitHub events, in the order the panel lists them, and how each reads. */
const GITHUB_EVENTS: EventKindInfo<GitHubEventKind>[] = [
  { kind: "mention", label: "You are @mentioned", hint: "In a comment, a review or a description, on a pull request or an issue. The comment is the request; the instruction below is optional.", runHint: "Optional: how to handle a mention, and your own comment with the marker, on top of what it asks. Variables work here." },
  { kind: "review_requested", label: "Your review is requested", hint: "Someone asks you to review a pull request, or you add yourself as a reviewer." },
  { kind: "review", label: "Your pull request is reviewed", hint: "Approved, changes requested, or commented." },
  { kind: "comment", label: "A comment on your pull request", hint: "Any new comment or line comment, mention or not." },
  { kind: "pushed", label: "New commits on a pull request you review", hint: "One event per push, not per commit." },
  { kind: "ci_failed", label: "Checks fail on your pull request", hint: "One event per commit, however many checks fail. Cancelled and skipped do not count." },
  { kind: "merged", label: "Your pull request is merged", hint: "" },
  { kind: "closed", label: "Your pull request is closed unmerged", hint: "By anyone, you included." },
  { kind: "assigned", label: "You are assigned", hint: "A pull request or an issue, by someone else or by yourself." },
];

function GitHubPanel({
  config,
  watcher,
  writable,
  onSaved,
  scripts,
}: {
  config: TriggerConfig;
  watcher: TriggerWatcher;
  writable: boolean;
  onSaved: () => void;
  scripts: ScriptsState;
}) {
  const g = config.github;
  const { busy, out, save } = useSave(onSaved);
  const [apiUrl, setApiUrl] = useState(g.api_url ?? "");
  const [login, setLogin] = useState(g.login ?? "");
  const [marker, setMarker] = useState(g.marker ?? "");
  const [poll, setPoll] = useState(String(g.poll_seconds ?? 120));
  const [catchUp, setCatchUp] = useState(String(g.max_catchup_hours ?? 72));
  const [repos, setRepos] = useState((g.repos ?? []).join(", "));
  const [trusted, setTrusted] = useState((g.trusted_associations ?? []).join(", "));
  const [notice, setNotice] = useState(!!g.refusal_notice);
  const { events, setEvent } = useEvents(g.events);
  const [token, setToken] = useState("");
  useEffect(() => {
    setApiUrl(g.api_url ?? "");
    setLogin(g.login ?? "");
    setMarker(g.marker ?? "");
    setPoll(String(g.poll_seconds ?? 120));
    setCatchUp(String(g.max_catchup_hours ?? 72));
    setRepos((g.repos ?? []).join(", "));
    setTrusted((g.trusted_associations ?? []).join(", "));
    setNotice(!!g.refusal_notice);
  }, [g.api_url, g.login, g.marker, g.poll_seconds, g.max_catchup_hours, g.repos, g.trusted_associations, g.refusal_notice]);

  const submit = async () => {
    const github: Record<string, unknown> = {
      api_url: apiUrl,
      login,
      marker,
      poll_seconds: Number(poll),
      max_catchup_hours: Number(catchUp),
      repos: list(repos),
      trusted_associations: list(trusted).map((x) => x.toUpperCase()),
      refusal_notice: notice,
      events,
    };
    if (token) github.token = token;
    if (await save({ github }, "GitHub saved.")) setToken("");
  };

  return (
    <Panel
      title="GitHub"
      service="github"
      watcher={watcher}
      switchLabel="Poll GitHub"
      switchHint="Reads the pull requests and issues you are involved in on a timer, with your token. Nothing is exposed; nothing is posted unless an instruction asks for it."
      enabled={g.enabled}
      writable={writable}
      busy={busy}
      onToggle={(v) => save({ github: { enabled: v } }, v ? "GitHub polling on." : "GitHub polling off.")}
      onSave={submit}
      out={out}
      after={
        <EventsEditor
          source="github"
          title="GitHub events"
          intro={
            <>
              Your own comment with the marker always runs, and opens that pull request to everyone&rsquo;s
              mentions. Each change below is its own switch, with what the brain does when it happens.
            </>
          }
          kinds={GITHUB_EVENTS}
          events={events}
          onChange={setEvent}
          variables={config.variables}
          writable={writable}
          busy={busy}
          scripts={scripts}
        />
      }
    >
      <SecretField
        label="Token"
        secret={config.secrets["github.token"]}
        value={token}
        onChange={setToken}
        onRemove={() => save({ clear: ["github.token"] }, "Removed github.token.")}
        writable={writable}
        placeholder="github_pat_… or ghp_…"
        hint="A fine-grained token with read access to Pull requests, Issues, Checks and Metadata, or a classic one with repo. Write access only if refusal comments are on."
      />
      {txt("Your login", login, setLogin, "Left empty, it is looked up from the token.", "optional")}
      {txt("Marker", marker, setMarker, "Put it in your own comment to hand that pull request to the brain. Not an @word: that would ping someone.", "/brain")}
      {txt("Repositories", repos, setRepos, "owner/name for one repo, a bare name for a whole org, comma-separated. Empty is everything the token sees.", "acme/api, acme")}
      {txt("Trusted", trusted, setTrusted, "Whose comments a switch below runs for. Anyone else only in a pull request you opened.", "OWNER, MEMBER, COLLABORATOR")}
      {num("Poll every (seconds)", poll, setPoll, "30 to 3600. GitHub's search allows about 30 a minute.", 30, 3600)}
      {num("Catch-up limit (hours)", catchUp, setCatchUp, "After a long gap, look back no further than this.", 1, 720)}
      {txt("API URL", apiUrl, setApiUrl, "Change only for GitHub Enterprise: https://your-host/api/v3.", "https://api.github.com")}
      <div className="ml-settings__switch">
        <Checkbox
          label="Comment when refusing"
          checked={notice}
          disabled={!writable || busy}
          onChange={setNotice}
        />
        <p className="ml-settings__hint ml-settings__hint--switch">
          Off, a mention the brain may not run is only logged. On, it gets the not-enabled notice as a
          public comment, once per pull request.
        </p>
      </div>
    </Panel>
  );
}

function MeetingPanel({
  config,
  writable,
  onSaved,
  scripts,
}: {
  config: TriggerConfig;
  writable: boolean;
  onSaved: () => void;
  scripts: ScriptsState;
}) {
  const m = config.meeting;
  const { busy, out, save } = useSave(onSaved);
  const [phrases, setPhrases] = useState<WakePhrase[]>(m.phrases ?? []);
  useEffect(() => setPhrases(m.phrases ?? []), [m.phrases]);

  return (
    <Panel
      title="Meetings"
      service="meeting"
      switchLabel="Listen for wake phrases"
      switchHint={`When the brain reads a meeting's transcript or notes (ingest-context, or a scan below), every wake phrase said in it starts a task at once. Nothing listens live: it runs on the transcript.`}
      enabled={m.enabled}
      writable={writable}
      busy={busy}
      onToggle={(v) => save({ meeting: { enabled: v } }, v ? "Wake phrases on." : "Wake phrases off.")}
      onSave={() => save({ meeting: { phrases } }, "Wake phrases saved.")}
      out={out}
      after={
        <>
          <PhrasesEditor
            phrases={phrases}
            onChange={setPhrases}
            brainName={m.brain_name}
            defaultDo={m.default_do}
            variables={config.variables}
            writable={writable}
            busy={busy}
            scripts={scripts}
          />
          <ScanBox active={m.active} writable={writable} brainName={m.brain_name} phrases={m.phrases} />
        </>
      }
    >
      {null}
    </Panel>
  );
}

/**
 * Paste a transcript: "Try" says which lines would start a task, and who said
 * them, without logging anything; "Run" starts them, as a scan from
 * ingest-context would.
 */
function ScanBox({
  active,
  writable,
  brainName,
  phrases,
}: {
  active: boolean;
  writable: boolean;
  brainName: string;
  phrases: WakePhrase[];
}) {
  const fold = useFold("meeting.scan", false);
  const [text, setText] = useState("");
  const [title, setTitle] = useState("");
  const [res, setRes] = useState<ScanResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const example = spokenPhrase(phrases.find((p) => p.on)?.phrase ?? "{brain} will handle", brainName);

  const go = async (dry: boolean) => {
    setBusy(true);
    setErr(null);
    setRes(null);
    try {
      setRes(await api.scanTranscript({ text, title: title || undefined, dry_run: dry }));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="ml-trigger__events" role="group" aria-label="Scan a transcript">
      <h4 className="ml-trigger__events-h">
        <FoldButton open={fold.open} onToggle={fold.toggle}>
          Scan a transcript
        </FoldButton>
      </h4>
      {fold.open ? (
        <>
          <p className="ml-settings__hint">
            One line per turn, <code>Name: what they said</code>, as Meet writes it. Try it on a saved
            phrase list; Run starts what it finds.
          </p>
          <Field label="Meeting title" hint="Optional. Scanning the same meeting twice runs nothing twice.">
            {(id) => (
              <TextInput id={id} value={title} disabled={!writable} onChange={(e) => setTitle(e.target.value)} />
            )}
          </Field>
          <Field label="Transcript">
            {(id) => (
              <TextArea
                id={id}
                rows={5}
                value={text}
                disabled={!writable}
                placeholder={`Ana: can someone send the deck to Acme?\nYou: ${example} this, send them v2.`}
                onChange={(e) => setText(e.target.value)}
              />
            )}
          </Field>
          <div className="ml-settings__foot">
            <Button disabled={!writable || busy || !text.trim()} onClick={() => void go(true)}>
              {busy ? "reading…" : "Try"}
            </Button>
            <Button
              variant="primary"
              disabled={!writable || busy || !text.trim() || !active}
              title={active ? undefined : "Switch on Triggers and wake phrases first"}
              onClick={() => void go(false)}
            >
              Run
            </Button>
          </div>
          {err ? <StatusMessage tone="error">{err}</StatusMessage> : null}
          {res ? (
            res.matches.length ? (
              <ul className="ml-trigger__scan" aria-label="matches">
                {res.matches.map((mt, i) => (
                  <li key={i}>
                    <Badge tone={scanTone(mt)}>{scanLabel(mt)}</Badge> <b>{mt.speaker || "someone"}</b>:{" "}
                    {mt.said}
                  </li>
                ))}
              </ul>
            ) : (
              <StatusMessage tone="info">No wake phrase in it.</StatusMessage>
            )
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function scanLabel(m: ScanResult["matches"][number]) {
  if (m.status) return m.status === "queued" ? "started" : m.status;
  return m.runs ? "would start" : "would be refused";
}

function scanTone(m: ScanResult["matches"][number]): BadgeTone {
  const s = m.status ?? (m.runs ? "queued" : "refused");
  return s === "queued" ? "success" : s === "refused" ? "warn" : "neutral";
}

function ClaudePanel({
  config,
  writable,
  onSaved,
}: {
  config: TriggerConfig;
  writable: boolean;
  onSaved: () => void;
}) {
  const c = config.claude;
  const { busy, out, save } = useSave(onSaved);
  const [bin, setBin] = useState(c.bin ?? "claude");
  const [noticeModel, setNoticeModel] = useState(c.notice_model ?? "haiku");
  const [mcp, setMcp] = useState(c.mcp_config ?? "");
  const [strict, setStrict] = useState(!!c.strict_mcp_config);
  const [timeout, setTimeoutS] = useState(String(c.timeout_s ?? 900));
  const [concurrent, setConcurrent] = useState(String(c.max_concurrent ?? 3));
  useEffect(() => {
    setBin(c.bin ?? "claude");
    setNoticeModel(c.notice_model ?? "haiku");
    setMcp(c.mcp_config ?? "");
    setStrict(!!c.strict_mcp_config);
    setTimeoutS(String(c.timeout_s ?? 900));
    setConcurrent(String(c.max_concurrent ?? 3));
  }, [c.bin, c.notice_model, c.mcp_config, c.strict_mcp_config, c.timeout_s, c.max_concurrent]);

  return (
    <Panel
      title="Claude"
      service="claude"
      writable={writable}
      busy={busy}
      onSave={() =>
        save(
          {
            claude: {
              bin,
              notice_model: noticeModel,
              mcp_config: mcp,
              strict_mcp_config: strict,
              timeout_s: Number(timeout),
              max_concurrent: Number(concurrent),
            },
          },
          "Claude saved.",
        )
      }
      out={out}
    >
      {txt("Command", bin, setBin, "The claude binary each mention runs as `claude -p`.", "claude")}
      {txt("MCP config", mcp, setMcp, "The MCP servers the session may use: your Slack, Jira, GitHub, Gmail, Drive. Empty uses what Claude Code loads anyway.", "~/.second_brain/trigger-mcp.json")}
      {txt("Notice model", noticeModel, setNoticeModel, "The small model that translates a refusal notice. Only used with a Slack user token or on Jira; without a token the notice session does it.", "haiku")}
      {num("Timeout (seconds)", timeout, setTimeoutS, "A session running longer is killed and marked timeout.", 30, 7200)}
      {num("At once", concurrent, setConcurrent, "How many mentions may run at the same time.", 1, 16)}
      <div className="ml-settings__switch">
        <Checkbox
          label="Only these MCP servers"
          checked={strict}
          disabled={!writable || !mcp}
          onChange={setStrict}
        />
        <p className="ml-settings__hint ml-settings__hint--switch">
          --strict-mcp-config: ignore every other MCP server Claude Code would load.
        </p>
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------- channels
/**
 * The channels open to everyone's mentions, kept in the brain's database.
 *
 * Collapsed by default: it can grow long, and most visits to Settings are not
 * about it. A closed channel is not deleted. It moves to "Closed", with who
 * closed it and when, and can be reopened from there.
 */
export function ChannelsCard({
  channels,
  writable,
  onChanged,
}: {
  channels: TriggerChannels;
  writable: boolean;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [showClosed, setShowClosed] = useState(false);
  const [channel, setChannel] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState<Out>(null);

  const change = async (id: string, enabled: boolean, name = "") => {
    setBusy(true);
    setOut(null);
    try {
      await api.setTriggerChannel(id, enabled, name);
      const shown = name ? `#${name}` : id;
      setOut({ text: enabled ? `Opened ${shown}.` : `Closed ${shown}.`, tone: "success" });
      if (enabled) {
        setChannel("");
        setName("");
      }
      onChanged();
    } catch (err) {
      setOut({ text: err instanceof Error ? err.message : String(err), tone: "error" });
    } finally {
      setBusy(false);
    }
  };

  const count = channels.active.length;
  return (
    <div className="ml-settings__preview ml-trigger__channels">
      <button
        type="button"
        className="ml-trigger__toggle"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span aria-hidden="true"><Icon name={open ? "chevron-down" : "chevron-right"} /></span> Open channels{" "}
        <Badge tone={count ? "accent" : "neutral"}>{count}</Badge>
      </button>
      <p className="ml-settings__hint">
        Everyone&rsquo;s mentions run in these channels, in every thread. Elsewhere only a thread
        you opened with your own mention accepts them.
      </p>

      {open ? (
        <>
          {count ? (
            <ul className="ml-trigger__rows" aria-label="open channels">
              {channels.active.map((c) => (
                <ChannelRow key={c.channel_id} row={c}>
                  <Button
                    size="sm"
                    disabled={!writable || busy}
                    onClick={() => change(c.channel_id, false, c.name)}
                    aria-label={`Close ${label(c)}`}
                  >
                    Close
                  </Button>
                </ChannelRow>
              ))}
            </ul>
          ) : (
            <p className="ml-settings__hint">No channel is open to everyone yet.</p>
          )}

          <div className="ml-settings__grid">
            <Field label="Channel" hint="Its id (C0123ABCD), or paste a link to it or to any message in it.">
              {(id) => (
                <TextInput
                  id={id}
                  value={channel}
                  placeholder="C0123ABCD or https://…/archives/C0123ABCD"
                  onChange={(e) => setChannel(e.target.value)}
                />
              )}
            </Field>
            <Field label="Name" hint="Optional. Looked up from Slack when left empty.">
              {(id) => (
                <TextInput id={id} value={name} placeholder="#general" onChange={(e) => setName(e.target.value)} />
              )}
            </Field>
          </div>
          <div className="ml-settings__foot">
            <Button
              variant="primary"
              disabled={!writable || busy || !channel.trim()}
              onClick={() => change(channel.trim(), true, name.trim())}
            >
              Open channel
            </Button>
          </div>

          {channels.removed.length ? (
            <div className="ml-trigger__closed">
              <button
                type="button"
                className="ml-trigger__toggle ml-trigger__toggle--minor"
                aria-expanded={showClosed}
                onClick={() => setShowClosed((v) => !v)}
              >
                <span aria-hidden="true"><Icon name={showClosed ? "chevron-down" : "chevron-right"} /></span> Closed{" "}
                <Badge>{channels.removed.length}</Badge>
              </button>
              {showClosed ? (
                <ul className="ml-trigger__rows" aria-label="closed channels">
                  {channels.removed.map((c) => (
                    <ChannelRow key={c.channel_id} row={c} closed>
                      <Button
                        size="sm"
                        disabled={!writable || busy}
                        onClick={() => change(c.channel_id, true, c.name)}
                        aria-label={`Reopen ${label(c)}`}
                      >
                        Reopen
                      </Button>
                    </ChannelRow>
                  ))}
                </ul>
              ) : null}
            </div>
          ) : null}
        </>
      ) : null}
      {out ? <StatusMessage tone={out.tone}>{out.text}</StatusMessage> : null}
    </div>
  );
}

// ---------------------------------------------------------------- people
/** What a pasted Jira account id looks like: long, with digits, so a name never reads as one. */
const JIRA_ACCOUNT = /^(?=.*\d)[A-Za-z0-9:_-]{10,128}$/;

/**
 * The Jira people who may tag the brain on any issue, without your opening
 * each one first. Their comment still needs the marker and a mention of you.
 *
 * One switch per person: off keeps the row, so turning someone back on is one
 * click and the list is also the history of who was let in.
 */
export function PeopleCard({
  people,
  writable,
  onChanged,
}: {
  people: TriggerPeople;
  writable: boolean;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [found, setFound] = useState<TriggerPersonMatch[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState<Out>(null);
  const rows = [...people.active, ...people.removed];
  const known = new Set(people.active.map((p) => p.user_id));

  const change = async (user_id: string, enabled: boolean, name = "") => {
    setBusy(true);
    setOut(null);
    try {
      await api.setTriggerPerson({ user_id, enabled, ...(name ? { name } : {}) });
      const shown = name || user_id;
      setOut({ text: enabled ? `${shown} may tag the brain.` : `${shown} may no longer tag the brain.`, tone: "success" });
      onChanged();
    } catch (err) {
      setOut({ text: err instanceof Error ? err.message : String(err), tone: "error" });
    } finally {
      setBusy(false);
    }
  };

  const search = async () => {
    setBusy(true);
    setOut(null);
    try {
      setFound((await api.findTriggerPeople(query.trim())).people);
    } catch (err) {
      setFound(null);
      setOut({ text: err instanceof Error ? err.message : String(err), tone: "error" });
    } finally {
      setBusy(false);
    }
  };

  const typedId = JIRA_ACCOUNT.test(query.trim()) ? query.trim() : "";
  return (
    <div className="ml-settings__preview ml-trigger__channels">
      <button
        type="button"
        className="ml-trigger__toggle"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span aria-hidden="true"><Icon name={open ? "chevron-down" : "chevron-right"} /></span> People who may tag the brain{" "}
        <Badge tone={people.active.length ? "accent" : "neutral"}>{people.active.length}</Badge>
      </button>
      <p className="ml-settings__hint">
        Their marked comments that mention you run on any issue, as if you had opened it. Anyone
        else&rsquo;s run only on an issue you opened with your own marked comment.
      </p>

      {open ? (
        <>
          {rows.length ? (
            <ul className="ml-trigger__rows" aria-label="allowed people">
              {rows.map((p) => (
                <PersonRow key={p.user_id} row={p}>
                  <Checkbox
                    label="May tag the brain"
                    checked={!p.removed_at}
                    disabled={!writable || busy}
                    onChange={(v) => change(p.user_id, v, p.name)}
                  />
                </PersonRow>
              ))}
            </ul>
          ) : (
            <p className="ml-settings__hint">
              Nobody yet. Find someone below, or allow them from their refused task in Tasks.
            </p>
          )}

          <div className="ml-settings__grid">
            <Field label="Find a person" hint="Part of their name or email, or paste their account id.">
              {(id) => (
                <TextInput
                  id={id}
                  value={query}
                  placeholder="Isadora, or 62f3dd0ce50f…"
                  onChange={(e) => {
                    setQuery(e.target.value);
                    setFound(null);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && query.trim().length >= 2) void search();
                  }}
                />
              )}
            </Field>
          </div>
          <div className="ml-settings__foot">
            <Button disabled={!writable || busy || query.trim().length < 2} onClick={search}>
              Search Jira
            </Button>
            {typedId ? (
              <Button
                disabled={!writable || busy || known.has(typedId)}
                onClick={() => change(typedId, true)}
              >
                Allow this account id
              </Button>
            ) : null}
          </div>
          {found ? (
            found.length ? (
              <ul className="ml-trigger__rows" aria-label="people found">
                {found.map((m) => (
                  <li key={m.user_id} className="ml-trigger__row">
                    <span>
                      <strong>{m.name || m.user_id}</strong> {m.email ? <span className="ml-settings__note">{m.email}</span> : null}
                    </span>
                    <code>{m.user_id}</code>
                    <Button
                      size="sm"
                      variant="primary"
                      disabled={!writable || busy || known.has(m.user_id)}
                      onClick={() => change(m.user_id, true, m.name)}
                      aria-label={`Allow ${m.name || m.user_id}`}
                    >
                      {known.has(m.user_id) ? "Allowed" : "Allow"}
                    </Button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="ml-settings__hint">Nobody in Jira matches that.</p>
            )
          ) : null}
        </>
      ) : null}
      {out ? <StatusMessage tone={out.tone}>{out.text}</StatusMessage> : null}
    </div>
  );
}

function PersonRow({ row, children }: { row: TriggerPersonRow; children: ReactNode }) {
  const off = !!row.removed_at;
  const when = (off ? row.removed_at : row.added_at).slice(0, 10);
  const who = off ? row.removed_by : row.added_by;
  return (
    <li className="ml-trigger__row">
      <span>
        <strong>{row.name || "unnamed"}</strong> <code>{row.user_id}</code>
      </span>
      <span className="ml-settings__note">
        {off ? "off since" : "allowed"} {when}
        {who ? ` by ${who}` : ""}
      </span>
      {children}
    </li>
  );
}

/** How a channel reads to a person: `#name`, or its id when it has none. */
const label = (c: TriggerChannelRow) => (c.name ? `#${c.name}` : c.channel_id);

function ChannelRow({
  row,
  closed,
  children,
}: {
  row: TriggerChannelRow;
  closed?: boolean;
  children: ReactNode;
}) {
  const when = (closed ? row.removed_at : row.added_at).slice(0, 10);
  const who = closed ? row.removed_by : row.added_by;
  return (
    <li className="ml-trigger__row">
      <span>
        <strong>{label(row)}</strong>{" "}
        {row.name ? <code>{row.channel_id}</code> : null}
      </span>
      <span className="ml-settings__note">
        {closed ? "closed" : "opened"} {when}
        {who ? ` by ${who}` : ""}
      </span>
      {children}
    </li>
  );
}

// ---------------------------------------------------------------- recent
const REQUEST_TONE: Record<string, BadgeTone> = {
  done: "success",
  declined: "warn",
  running: "accent",
  queued: "accent",
  refused: "warn",
  failed: "danger",
  timeout: "danger",
  interrupted: "warn",
  cancelled: "neutral",
};

function Recent({ status }: { status: TriggerStatus }) {
  const total = Object.values(status.counts).reduce((a, b) => a + b, 0);
  return (
    <>
      <h3 className="ml-settings__h3">
        Requests <Badge>{total}</Badge>
      </h3>
      {status.recent.length ? (
        <ul className="ml-settings__list" aria-label="recent requests">
          {status.recent.map((r) => (
            <li key={r.id} title={r.id}>
              <Badge tone={REQUEST_TONE[r.status] ?? "neutral"}>{r.status}</Badge> {r.source} ·{" "}
              {r.requester_name || r.requester_id}: {r.text}{" "}
              {r.permalink ? (
                <a href={r.permalink} target="_blank" rel="noreferrer">
                  open
                </a>
              ) : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="ml-settings__hint">No mention has reached the brain yet.</p>
      )}
      <p className="ml-settings__hint">
        {status.enabled_scopes.length} open conversation
        {status.enabled_scopes.length === 1 ? "" : "s"}. Every request and each action taken for it:{" "}
        <code>trigger.py show --id &lt;id&gt;</code>.
      </p>
    </>
  );
}
