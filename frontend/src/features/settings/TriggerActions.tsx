import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { api } from "@/api/client";
import {
  Badge,
  Button,
  Checkbox,
  Field,
  Pills,
  Select,
  StatusMessage,
  TextArea,
  TextInput, Icon } from "@/components";
import type {
  ScriptEntry,
  SlackCommand,
  TriggerAction,
  TriggerEventSetting,
  TriggerVariable,
  WakePhrase,
} from "@/api/types";

/**
 * What a trigger DOES, shared by every source (docs/trigger.md, "Actions"):
 * the run command a Claude session is told, an API call, or a script in
 * `<home>/scripts`. Plus the folding every section of the Triggers card uses.
 */

// ---------------------------------------------------------------- folding
const FOLD_KEY = "ml.trigger.folded";

function readFolded(): Set<string> {
  try {
    return new Set(
      JSON.parse(localStorage.getItem(FOLD_KEY) || "[]") as string[],
    );
  } catch {
    return new Set();
  }
}

/**
 * Folded or open, remembered per section in this browser. Open by default:
 * folding is for putting away what you are not working on, so the rest reads.
 */
export function useFold(id: string, initiallyOpen = true) {
  const [open, setOpen] = useState(() => {
    const folded = readFolded();
    return folded.has(id) ? false : folded.has(`!${id}`) ? true : initiallyOpen;
  });
  const toggle = () =>
    setOpen((was) => {
      const now = !was;
      try {
        const folded = readFolded();
        folded.delete(id);
        folded.delete(`!${id}`);
        if (now !== initiallyOpen) folded.add(now ? `!${id}` : id);
        localStorage.setItem(FOLD_KEY, JSON.stringify([...folded]));
      } catch {
        /* storage unavailable: it just is not remembered */
      }
      return now;
    });
  return { open, toggle };
}

export function FoldButton({
  open,
  onToggle,
  children,
  minor,
}: {
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
  minor?: boolean;
}) {
  return (
    <button
      type="button"
      className={`ml-trigger__toggle${minor ? " ml-trigger__toggle--minor" : ""}`}
      aria-expanded={open}
      onClick={onToggle}
    >
      <span aria-hidden="true"><Icon name={open ? "chevron-down" : "chevron-right"} /></span> {children}
    </button>
  );
}

// ---------------------------------------------------------------- run commands
export const VAR_RE = /\{([a-z][a-z0-9_]*)\}/g;

/** The template with each variable swapped for its example, and the names it does not know. */
export function preview(
  template: string,
  variables: TriggerVariable[],
  extra: string[] = [],
) {
  const known = new Map(variables.map((v) => [v.name, v.example]));
  for (const e of extra) if (!known.has(e)) known.set(e, `<${e}>`);
  const unknown = new Set<string>();
  const text = template.replace(VAR_RE, (whole, name: string) => {
    if (known.has(name)) return known.get(name) ?? "";
    unknown.add(name);
    return whole;
  });
  return { text, unknown: [...unknown] };
}

/** The named groups of a command's pattern, Python's `(?P<x>)` or JavaScript's `(?<x>)`. */
export function patternGroups(pattern: string): string[] {
  return [...pattern.matchAll(/\(\?P?<([a-z][a-z0-9_]*)>/g)].map((m) => m[1]!);
}

function VarCheck({
  value,
  variables,
  extra,
}: {
  value: string;
  variables: TriggerVariable[];
  extra: string[];
}) {
  const { text, unknown } = preview(value, variables, extra);
  if (unknown.length)
    return (
      <StatusMessage tone="error">
        Not a variable: {unknown.map((u) => `{${u}}`).join(", ")}. The list is
        at the top of Triggers.
      </StatusMessage>
    );
  if (value.match(VAR_RE))
    return (
      <p className="ml-trigger__preview">
        <span>With example values:</span> {text}
      </p>
    );
  return null;
}

export function RunCommandField({
  label = "Run command",
  hint,
  value,
  onChange,
  variables,
  extra = [],
  writable,
  placeholder,
}: {
  label?: string;
  hint: string;
  value: string;
  onChange: (v: string) => void;
  variables: TriggerVariable[];
  extra?: string[];
  writable: boolean;
  placeholder?: string;
}) {
  return (
    <Field label={label} hint={hint}>
      {(id) => (
        <>
          <TextArea
            id={id}
            rows={3}
            value={value}
            placeholder={placeholder}
            disabled={!writable}
            onChange={(e) => onChange(e.target.value)}
          />
          <VarCheck value={value} variables={variables} extra={extra} />
        </>
      )}
    </Field>
  );
}

// ---------------------------------------------------------------- actions
const ACTION_OPTIONS: { value: TriggerAction; label: string; title: string }[] =
  [
    {
      value: "claude",
      label: "Claude session",
      title: "A headless Claude Code session, told the run command.",
    },
    {
      value: "http",
      label: "Call an API",
      title: "One HTTP request. No model call.",
    },
    {
      value: "script",
      label: "Run a script",
      title: "A file in your scripts directory. No model call.",
    },
  ];

const headersText = (h: Record<string, string>) =>
  Object.entries(h)
    .map(([k, v]) => `${k}: ${v}`)
    .join("\n");

function parseHeaders(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const i = line.indexOf(":");
    if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return out;
}

export interface ScriptsState {
  list: ScriptEntry[];
  dir: string;
  reload: () => void;
}

/**
 * The action half of one event or command. The Claude run command is the
 * default; the other two never start a model, which is the point of them.
 */
export function ActionEditor({
  setting,
  onChange,
  variables,
  extra = [],
  writable,
  scripts,
  name,
  runHint,
  placeholder,
  argsPlaceholder,
  isCommand,
}: {
  setting: TriggerEventSetting;
  onChange: (patch: Partial<TriggerEventSetting>) => void;
  variables: TriggerVariable[];
  extra?: string[];
  writable: boolean;
  scripts: ScriptsState;
  /** Names the controls for a screen reader: "Action for Your review is requested". */
  name: string;
  runHint: string;
  placeholder?: string;
  /** What the script's arguments box shows when empty. */
  argsPlaceholder?: string;
  isCommand?: boolean;
}) {
  const [headers, setHeaders] = useState(headersText(setting.http.headers));
  const [args, setArgs] = useState(setting.script.args.join("\n"));
  useEffect(
    () => setHeaders(headersText(setting.http.headers)),
    [setting.http.headers],
  );
  useEffect(
    () => setArgs(setting.script.args.join("\n")),
    [setting.script.args],
  );
  const http = (patch: Partial<TriggerEventSetting["http"]>) =>
    onChange({ http: { ...setting.http, ...patch } });
  const script = (patch: Partial<TriggerEventSetting["script"]>) =>
    onChange({ script: { ...setting.script, ...patch } });

  return (
    <div className="ml-trigger__action">
      <Pills
        aria-label={`Action for ${name}`}
        options={ACTION_OPTIONS}
        value={setting.action}
        onChange={(a) => writable && onChange({ action: a })}
      />
      {setting.action === "claude" ? (
        <RunCommandField
          hint={runHint}
          value={setting.do}
          onChange={(v) => onChange({ do: v })}
          variables={variables}
          extra={extra}
          writable={writable}
          placeholder={placeholder}
        />
      ) : null}

      {setting.action === "http" ? (
        <div className="ml-settings__grid ml-trigger__http">
          <Field label="Method" hint="The request's method.">
            {(id) => (
              <Select
                id={id}
                value={setting.http.method}
                disabled={!writable}
                options={["GET", "POST", "PUT", "PATCH", "DELETE"].map((m) => ({
                  value: m,
                  label: m,
                }))}
                onChange={(e) => http({ method: e.target.value })}
              />
            )}
          </Field>
          <Field
            label="URL"
            hint="http:// or https://. A variable in it is percent-encoded."
          >
            {(id) => (
              <>
                <TextInput
                  id={id}
                  value={setting.http.url}
                  disabled={!writable}
                  placeholder="https://hooks.example.com/brain?who={trigger_user_id}"
                  onChange={(e) => http({ url: e.target.value })}
                />
                <VarCheck
                  value={setting.http.url}
                  variables={variables}
                  extra={extra}
                />
              </>
            )}
          </Field>
          <Field
            label="Headers"
            hint="One per line, Name: value. Stored in trigger.json, mode 600."
          >
            {(id) => (
              <TextArea
                id={id}
                rows={2}
                value={headers}
                disabled={!writable}
                placeholder="Authorization: Bearer …"
                onChange={(e) => {
                  setHeaders(e.target.value);
                  http({ headers: parseHeaders(e.target.value) });
                }}
              />
            )}
          </Field>
          <Field
            label="Body"
            hint="Sent for every method but GET. In a JSON body each value is JSON-escaped, so it cannot break out of its string."
          >
            {(id) => (
              <>
                <TextArea
                  id={id}
                  rows={3}
                  value={setting.http.body}
                  disabled={!writable}
                  placeholder={'{"text": "{trigger_user}: {trigger_content}"}'}
                  onChange={(e) => http({ body: e.target.value })}
                />
                <VarCheck
                  value={setting.http.body}
                  variables={variables}
                  extra={extra}
                />
              </>
            )}
          </Field>
        </div>
      ) : null}

      {setting.action === "script" ? (
        <>
          <ScriptPicker
            value={setting.script.name}
            onChange={(n) => script({ name: n })}
            scripts={scripts}
            writable={writable}
            name={name}
          />
          <Field
            label="Arguments"
            hint={
              isCommand
                ? "One per line, variables filled in, each passed as one argument with no shell in between. Empty: the command's own arguments, in order."
                : "One per line, variables filled in, each passed as one argument with no shell in between. Every variable is also in the environment as BRAIN_<NAME>."
            }
          >
            {(id) => (
              <>
                <TextArea
                  id={id}
                  rows={2}
                  value={args}
                  disabled={!writable}
                  placeholder={
                    argsPlaceholder ??
                    (isCommand
                      ? "(the command's own arguments)"
                      : "{jira_issue}\n{jira_to}")
                  }
                  onChange={(e) => {
                    setArgs(e.target.value);
                    script({
                      args: e.target.value
                        .split("\n")
                        .filter((a) => a.trim() !== ""),
                    });
                  }}
                />
                <VarCheck value={args} variables={variables} extra={extra} />
              </>
            )}
          </Field>
        </>
      ) : null}
    </div>
  );
}

const TEMPLATES: Record<string, string> = {
  sh: `#!/bin/sh
# What this script does, in one line.
# Every trigger variable is in the environment as BRAIN_<NAME>:
# $BRAIN_TRIGGER_USER, $BRAIN_TRIGGER_CONTENT, $BRAIN_COMMAND, $BRAIN_ARG_1 ...
# A command's arguments are also "$@". Log what you did against the request:
#   python3 "$MEMORY_LEDGER_TRIGGER_SCRIPT" action --kind note --detail "..."
set -eu
echo "triggered by $BRAIN_TRIGGER_USER: $*"
`,
  py: `#!/usr/bin/env python3
"""What this script does, in one line."""
import os
import sys

# Every trigger variable is in the environment as BRAIN_<NAME>; a command's
# arguments are also sys.argv[1:].
who = os.environ.get("BRAIN_TRIGGER_USER", "")
print(f"triggered by {who}: {sys.argv[1:]}")
`,
};

function templateFor(name: string) {
  return name.endsWith(".py") ? TEMPLATES.py! : TEMPLATES.sh!;
}

/** Link a script that is already in `<home>/scripts`, or write a new one there. */
function ScriptPicker({
  value,
  onChange,
  scripts,
  writable,
  name,
}: {
  value: string;
  onChange: (name: string) => void;
  scripts: ScriptsState;
  writable: boolean;
  name: string;
}) {
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [text, setText] = useState(TEMPLATES.sh!);
  const [edited, setEdited] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const known = scripts.list.some((s) => s.name === value);
  const options = [
    {
      value: "",
      label: scripts.list.length ? "Pick a script…" : "No script yet",
    },
    ...scripts.list.map((s) => ({
      value: s.name,
      label: `${s.name}${s.summary ? ` — ${s.summary}` : ""}`,
    })),
    ...(value && !known ? [{ value, label: `${value} (not found)` }] : []),
  ];

  const create = async () => {
    setBusy(true);
    setErr(null);
    try {
      await api.createScript(newName.trim(), text, true);
      onChange(newName.trim());
      scripts.reload();
      setCreating(false);
      setNewName("");
      setEdited(false);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="ml-trigger__script">
      <Field
        label="Script"
        hint={
          <>
            A file in <code>{scripts.dir || "<brain>/scripts"}</code>. It runs
            as you, from the brain's directory.
          </>
        }
      >
        {(id) => (
          <div className="ml-settings__foot">
            <Select
              id={id}
              value={value}
              disabled={!writable}
              options={options}
              aria-label={`Script for ${name}`}
              onChange={(e) => onChange(e.target.value)}
            />
            <Button
              size="sm"
              disabled={!writable}
              onClick={() => setCreating((v) => !v)}
            >
              {creating ? "Cancel" : "New script…"}
            </Button>
          </div>
        )}
      </Field>
      {value && !known && scripts.list.length ? (
        <StatusMessage tone="error">
          {value} is not in the scripts directory any more.
        </StatusMessage>
      ) : null}
      {creating ? (
        <div
          className="ml-trigger__new-script"
          role="group"
          aria-label="New script"
        >
          <Field
            label="File name"
            hint="A bare file name. .sh or .py picks the starting text."
          >
            {(id) => (
              <TextInput
                id={id}
                value={newName}
                placeholder="deploy.sh"
                onChange={(e) => {
                  setNewName(e.target.value);
                  if (!edited) setText(templateFor(e.target.value));
                }}
              />
            )}
          </Field>
          <Field
            label="Script text"
            hint="Written executable. Edit it later on disk or from Capabilities."
          >
            {(id) => (
              <TextArea
                id={id}
                rows={8}
                className="ml-trigger__code"
                value={text}
                onChange={(e) => {
                  setText(e.target.value);
                  setEdited(true);
                }}
              />
            )}
          </Field>
          <div className="ml-settings__foot">
            <Button
              variant="primary"
              disabled={!writable || busy || !newName.trim()}
              onClick={create}
            >
              {busy ? "writing…" : "Create and link"}
            </Button>
          </div>
          {err ? <StatusMessage tone="error">{err}</StatusMessage> : null}
        </div>
      ) : null}
    </div>
  );
}

/** One line on what an event or command does, for its folded row. */
export function actionSummary(s: TriggerEventSetting) {
  if (s.action === "http")
    return `calls ${s.http.method} ${s.http.url || "(no URL yet)"}`;
  if (s.action === "script")
    return `runs ${s.script.name || "(no script yet)"}`;
  return s.do ? "Claude, with a run command" : "Claude, built-in handling";
}

// ---------------------------------------------------------------- events
export interface EventKindInfo<K extends string> {
  kind: K;
  label: string;
  hint: string;
  /** No switch: it is always on (a Slack or Jira mention). */
  always?: boolean;
  runHint?: string;
  placeholder?: string;
}

/**
 * Every event of one source: a switch each, and when it is on, what it does.
 * The whole list folds, and so does each event's configuration.
 */
export function EventsEditor<K extends string>({
  source,
  title,
  intro,
  kinds,
  events,
  onChange,
  variables,
  writable,
  busy,
  scripts,
}: {
  source: string;
  title: string;
  intro: ReactNode;
  kinds: EventKindInfo<K>[];
  events: Record<K, TriggerEventSetting>;
  onChange: (kind: K, patch: Partial<TriggerEventSetting>) => void;
  variables: TriggerVariable[];
  writable: boolean;
  busy: boolean;
  scripts: ScriptsState;
}) {
  const fold = useFold(`${source}.events`);
  const on = kinds.filter((k) => events[k.kind]?.on).length;
  return (
    <div className="ml-trigger__events" role="group" aria-label={title}>
      <h4 className="ml-trigger__events-h">
        <FoldButton open={fold.open} onToggle={fold.toggle}>
          What starts the flow{" "}
          <Badge tone={on ? "accent" : "neutral"}>{on}</Badge>
        </FoldButton>
      </h4>
      {fold.open ? (
        <>
          <p className="ml-settings__hint">{intro}</p>
          {kinds.map((k) => (
            <EventRow
              key={k.kind}
              source={source}
              info={k}
              setting={events[k.kind]}
              onChange={(patch) => onChange(k.kind, patch)}
              variables={variables}
              writable={writable}
              busy={busy}
              scripts={scripts}
            />
          ))}
        </>
      ) : null}
    </div>
  );
}

function EventRow<K extends string>({
  source,
  info,
  setting,
  onChange,
  variables,
  writable,
  busy,
  scripts,
}: {
  source: string;
  info: EventKindInfo<K>;
  setting: TriggerEventSetting | undefined;
  onChange: (patch: Partial<TriggerEventSetting>) => void;
  variables: TriggerVariable[];
  writable: boolean;
  busy: boolean;
  scripts: ScriptsState;
}) {
  const fold = useFold(`${source}.event.${info.kind}`);
  const ev = setting ?? {
    on: false,
    do: "",
    action: "claude" as const,
    http: { method: "POST", url: "", headers: {}, body: "" },
    script: { name: "", args: [] },
  };
  return (
    <div className="ml-trigger__event">
      <div className="ml-trigger__event-head">
        {info.always ? (
          <strong className="ml-trigger__event-label">{info.label}</strong>
        ) : (
          <Checkbox
            label={info.label}
            checked={ev.on}
            disabled={!writable || busy}
            onChange={(v) => onChange({ on: v })}
          />
        )}
        {ev.on ? (
          <FoldButton open={fold.open} onToggle={fold.toggle} minor>
            {fold.open ? "hide" : actionSummary(ev)}
          </FoldButton>
        ) : null}
      </div>
      {info.hint ? (
        <p className="ml-settings__hint ml-settings__hint--switch">
          {info.hint}
        </p>
      ) : null}
      {ev.on && fold.open ? (
        <ActionEditor
          setting={ev}
          onChange={onChange}
          variables={variables}
          writable={writable}
          scripts={scripts}
          name={info.label}
          runHint={
            info.runHint ??
            "What the session does when this happens. Variables work here."
          }
          placeholder={info.placeholder}
        />
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------- commands
const blankCommand = (): SlackCommand => ({
  name: "",
  anyone: false,
  pattern: "",
  description: "",
  on: true,
  do: "",
  action: "claude",
  http: { method: "POST", url: "", headers: {}, body: "" },
  script: { name: "", args: [] },
});

/**
 * Slack commands: `@brain deploy {staging, v1.2}`. Each is matched by its
 * pattern, or its name as the first word; the braces become `{args}`,
 * `{arg_1}`…, and a pattern's named groups become variables of their own.
 */
export function CommandsEditor({
  commands,
  onChange,
  variables,
  writable,
  busy,
  scripts,
}: {
  commands: SlackCommand[];
  onChange: (next: SlackCommand[]) => void;
  variables: TriggerVariable[];
  writable: boolean;
  busy: boolean;
  scripts: ScriptsState;
}) {
  const fold = useFold("slack.commands");
  const set = (i: number, patch: Partial<SlackCommand>) =>
    onChange(commands.map((c, j) => (j === i ? { ...c, ...patch } : c)));
  return (
    <div
      className="ml-trigger__events"
      role="group"
      aria-label="Slack commands"
    >
      <h4 className="ml-trigger__events-h">
        <FoldButton open={fold.open} onToggle={fold.toggle}>
          Commands{" "}
          <Badge tone={commands.length ? "accent" : "neutral"}>
            {commands.length}
          </Badge>
        </FoldButton>
      </h4>
      {fold.open ? (
        <>
          <p className="ml-settings__hint">
            Mention the brain with a command and its values in braces, for
            example <code>@brain deploy {"{staging, v1.2}"}</code>. The first
            command that matches runs instead of the mention&rsquo;s handling,
            behind the same gate. The values are <code>{"{args}"}</code>,{" "}
            <code>{"{arg_1}"}</code>, <code>{"{arg_2}"}</code>…; a named group
            in the pattern, like <code>{"(?P<env>\\w+)"}</code>, is{" "}
            <code>{"{env}"}</code>. A command never opens the thread to anyone
            else.
          </p>
          {commands.map((c, i) => (
            <CommandRow
              key={i}
              cmd={c}
              onChange={(patch) => set(i, patch)}
              onRemove={() => onChange(commands.filter((_, j) => j !== i))}
              variables={variables}
              writable={writable}
              busy={busy}
              scripts={scripts}
            />
          ))}
          <div className="ml-settings__foot">
            <Button
              size="sm"
              disabled={!writable || busy}
              onClick={() => onChange([...commands, blankCommand()])}
            >
              Add a command
            </Button>
          </div>
        </>
      ) : null}
    </div>
  );
}

function CommandRow({
  cmd,
  onChange,
  onRemove,
  variables,
  writable,
  busy,
  scripts,
}: {
  cmd: SlackCommand;
  onChange: (patch: Partial<SlackCommand>) => void;
  onRemove: () => void;
  variables: TriggerVariable[];
  writable: boolean;
  busy: boolean;
  scripts: ScriptsState;
}) {
  const [open, setOpen] = useState(!cmd.name);
  const groups = patternGroups(cmd.pattern);
  const label = cmd.name || "new command";
  return (
    <div
      className="ml-trigger__event"
      aria-label={`Command ${label}`}
      role="group"
    >
      <div className="ml-trigger__event-head">
        <Checkbox
          label={<code>{label}</code>}
          checked={cmd.on}
          disabled={!writable || busy}
          onChange={(v) => onChange({ on: v })}
        />
        <FoldButton open={open} onToggle={() => setOpen((v) => !v)} minor>
          {open
            ? "hide"
            : `${actionSummary(cmd)}${cmd.anyone ? "" : ", only you"}`}
        </FoldButton>
        <Button
          size="sm"
          disabled={!writable || busy}
          onClick={onRemove}
          aria-label={`Remove ${label}`}
        >
          Remove
        </Button>
      </div>
      {cmd.description && !open ? (
        <p className="ml-settings__hint ml-settings__hint--switch">
          {cmd.description}
        </p>
      ) : null}
      {open ? (
        <>
          <div className="ml-settings__grid">
            <Field
              label="Name"
              hint="What follows the mention: letters, digits, - and _."
            >
              {(id) => (
                <TextInput
                  id={id}
                  value={cmd.name}
                  placeholder="deploy"
                  disabled={!writable}
                  onChange={(e) => onChange({ name: e.target.value })}
                />
              )}
            </Field>
            <Field
              label="Pattern"
              hint="Optional regex, matched without the mention, case-blind. Empty: the name as the first word."
            >
              {(id) => (
                <TextInput
                  id={id}
                  value={cmd.pattern}
                  placeholder={
                    cmd.name ? `^\\s*${cmd.name}\\b` : "^deploy (?P<env>\\w+)"
                  }
                  disabled={!writable}
                  onChange={(e) => onChange({ pattern: e.target.value })}
                />
              )}
            </Field>
            <Field
              label="Description"
              hint="For you, when this list is folded."
            >
              {(id) => (
                <TextInput
                  id={id}
                  value={cmd.description}
                  disabled={!writable}
                  onChange={(e) => onChange({ description: e.target.value })}
                />
              )}
            </Field>
          </div>
          <div className="ml-settings__switch">
            <Checkbox
              label="Anyone the brain lets in may run it"
              checked={cmd.anyone}
              disabled={!writable || busy}
              onChange={(v) => onChange({ anyone: v })}
            />
            <p className="ml-settings__hint ml-settings__hint--switch">
              Off, only you: anyone else gets the not-allowed notice, even in an
              open channel. On, whoever may summon the brain in that
              conversation.
            </p>
          </div>
          {groups.length ? (
            <p className="ml-settings__hint">
              From the pattern:{" "}
              {groups.map((g) => (
                <code key={g}>{`{${g}} `}</code>
              ))}
            </p>
          ) : null}
          <ActionEditor
            setting={cmd}
            onChange={onChange}
            variables={variables}
            extra={groups}
            writable={writable}
            scripts={scripts}
            name={`command ${label}`}
            isCommand
            runHint="What the session does with this command. {args} and {arg_1}… are what was in the braces."
            placeholder="Deploy {arg_1} at version {arg_2} and reply in the thread with the result."
          />
        </>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------- wake phrases
const blankPhrase = (): WakePhrase => ({
  phrase: "{brain} ",
  anyone: false,
  description: "",
  on: true,
  do: "",
  action: "claude",
  http: { method: "POST", url: "", headers: {}, body: "" },
  script: { name: "", args: [] },
});

/** The words a phrase is said with: what the engine matches, `{brain}` filled in. */
export function spokenPhrase(phrase: string, brain: string, owner = "") {
  return phrase
    .replace(/\{brain\}/g, brain)
    .replace(/\{owner\}/g, owner || "{owner}")
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * Wake phrases: "Brain will handle this", said in a meeting. A transcript the
 * brain reads is scanned for each, and every one said starts a task at once.
 */
export function PhrasesEditor({
  phrases,
  onChange,
  brainName,
  defaultDo,
  variables,
  writable,
  busy,
  scripts,
}: {
  phrases: WakePhrase[];
  onChange: (next: WakePhrase[]) => void;
  brainName: string;
  defaultDo: string;
  variables: TriggerVariable[];
  writable: boolean;
  busy: boolean;
  scripts: ScriptsState;
}) {
  const fold = useFold("meeting.phrases");
  const set = (i: number, patch: Partial<WakePhrase>) =>
    onChange(phrases.map((c, j) => (j === i ? { ...c, ...patch } : c)));
  return (
    <div className="ml-trigger__events" role="group" aria-label="Wake phrases">
      <h4 className="ml-trigger__events-h">
        <FoldButton open={fold.open} onToggle={fold.toggle}>
          Wake phrases{" "}
          <Badge tone={phrases.length ? "accent" : "neutral"}>{phrases.length}</Badge>
        </FoldButton>
      </h4>
      {fold.open ? (
        <>
          <p className="ml-settings__hint">
            Say one in a meeting, for example{" "}
            <code>{spokenPhrase("{brain} will handle this", brainName)}</code>, and the task starts
            as soon as the brain reads the transcript. <code>{"{brain}"}</code> is the brain&rsquo;s
            name (Preferences), so renaming it renames every phrase. Matched word by word, whatever
            the case or the punctuation between the words. The session gets the line it was said in
            and the lines around it, so &ldquo;this&rdquo; can be found.
          </p>
          {phrases.map((ph, i) => (
            <PhraseRow
              key={i}
              phrase={ph}
              brainName={brainName}
              defaultDo={defaultDo}
              onChange={(patch) => set(i, patch)}
              onRemove={() => onChange(phrases.filter((_, j) => j !== i))}
              variables={variables}
              writable={writable}
              busy={busy}
              scripts={scripts}
            />
          ))}
          <div className="ml-settings__foot">
            <Button
              size="sm"
              disabled={!writable || busy}
              onClick={() => onChange([...phrases, blankPhrase()])}
            >
              Add a phrase
            </Button>
            {!phrases.length ? (
              <Button
                size="sm"
                disabled={!writable || busy}
                onClick={() => onChange([{ ...blankPhrase(), phrase: "{brain} will handle" }])}
              >
                Start with &ldquo;{spokenPhrase("{brain} will handle", brainName)}&rdquo;
              </Button>
            ) : null}
          </div>
        </>
      ) : null}
    </div>
  );
}

function PhraseRow({
  phrase,
  brainName,
  defaultDo,
  onChange,
  onRemove,
  variables,
  writable,
  busy,
  scripts,
}: {
  phrase: WakePhrase;
  brainName: string;
  defaultDo: string;
  onChange: (patch: Partial<WakePhrase>) => void;
  onRemove: () => void;
  variables: TriggerVariable[];
  writable: boolean;
  busy: boolean;
  scripts: ScriptsState;
}) {
  const [open, setOpen] = useState(!phrase.phrase.replace("{brain}", "").trim());
  const spoken = spokenPhrase(phrase.phrase, brainName) || "new phrase";
  return (
    <div className="ml-trigger__event" aria-label={`Phrase ${spoken}`} role="group">
      <div className="ml-trigger__event-head">
        <Checkbox
          label={<code>{spoken}</code>}
          checked={phrase.on}
          disabled={!writable || busy}
          onChange={(v) => onChange({ on: v })}
        />
        <FoldButton open={open} onToggle={() => setOpen((v) => !v)} minor>
          {open ? "hide" : `${actionSummary(phrase)}${phrase.anyone ? ", anyone" : ", only you"}`}
        </FoldButton>
        <Button size="sm" disabled={!writable || busy} onClick={onRemove} aria-label={`Remove ${spoken}`}>
          Remove
        </Button>
      </div>
      {phrase.description && !open ? (
        <p className="ml-settings__hint ml-settings__hint--switch">{phrase.description}</p>
      ) : null}
      {open ? (
        <>
          <div className="ml-settings__grid">
            <Field
              label="Phrase"
              hint={`At least two words. {brain} and {owner} are filled in. Said as: “${spoken}”.`}
            >
              {(id) => (
                <TextInput
                  id={id}
                  value={phrase.phrase}
                  placeholder="{brain} will handle"
                  disabled={!writable}
                  onChange={(e) => onChange({ phrase: e.target.value })}
                />
              )}
            </Field>
            <Field label="Description" hint="For you, when this list is folded.">
              {(id) => (
                <TextInput
                  id={id}
                  value={phrase.description}
                  disabled={!writable}
                  onChange={(e) => onChange({ description: e.target.value })}
                />
              )}
            </Field>
          </div>
          <div className="ml-settings__switch">
            <Checkbox
              label="Anyone in the meeting may say it"
              checked={phrase.anyone}
              disabled={!writable || busy}
              onChange={(v) => onChange({ anyone: v })}
            />
            <p className="ml-settings__hint ml-settings__hint--switch">
              Off, only you: the transcript has to name you as the speaker. Anyone else saying it
              is logged as refused and runs nothing.
            </p>
          </div>
          <ActionEditor
            setting={phrase}
            onChange={onChange}
            variables={variables}
            writable={writable}
            scripts={scripts}
            name={`phrase ${spoken}`}
            runHint="What the session does. {phrase_said} is the line, {phrase_rest} what came after the phrase, {phrase_context} the lines around it. Empty: the default below."
            placeholder={defaultDo}
          />
        </>
      ) : null}
    </div>
  );
}
