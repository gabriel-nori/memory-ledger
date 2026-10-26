import { useEffect, useMemo, useRef, useState } from "react";
import type { IconName } from "@/components";
import type { ClipboardEvent, DragEvent, KeyboardEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "@/api/client";
import { useMeta } from "@/api/MetaContext";
import { usePolling } from "@/hooks/usePolling";
import {
  Badge,
  Button,
  Checkbox,
  EmptyState,
  Markdown,
  Menu,
  RichText,
  SearchInput,
  Select,
  Spinner,
  StatusMessage,
  TextArea, Icon } from "@/components";
import { Section } from "@/features/shell/Section";
import { UsageMeter } from "@/features/shell/UsageMeter";
import { formatDate, truncate } from "@/lib/format";
import { segments } from "./segments";
import { useSpeakBlocks } from "./voice/useSpeakBlocks";
import type { TurnEnd } from "./voice/useSpeakBlocks";
import { useVoiceSession } from "./voice/useVoiceSession";
import { VoiceBar } from "./voice/VoiceBar";
import { speech } from "./voice/speech";
import { useTranslated } from "./voice/translate";
import { splitAttachments } from "@/lib/markdown";
import { plainText } from "@/lib/richText";
import type {
  AgentConnector,
  AgentConvState,
  AgentConversation,
  AgentEvent,
  AgentMessage,
  VoiceConfig,
  VoiceStatus,
  VoiceVerbosity,
} from "@/api/types";
import { exportConversation, formatCost } from "./export";
import type { ExportFormat } from "./export";
import { CommandPopup, PluginsMenu, search, slashQuery, useCommands } from "./SlashCommands";
import "./AgentsSection.css";

/**
 * Conversations with an agent — Claude or an Ollama model — from the brain
 * itself (docs/agents.md).
 *
 * On the left, the conversations, each with what its last reply is doing: a
 * spinner while it runs, a ? when the agent is waiting on you, a check when it
 * answered. Hidden ones fold away under the active list. On the right, the
 * thread: yours on the right, the agent's on the left. What the agent says
 * while it works shows as it comes; every tool, skill and subagent it used is
 * logged between those, folded shut.
 *
 * Every turn is one stateless call, so switching the connector or the model
 * between turns is free: the next reply is written by whichever is picked.
 */
export function AgentsSection() {
  const { writable, meta } = useMeta();
  const agentName = `${meta?.brain_name ?? "Ledger"} agent`;
  const [params, setParams] = useSearchParams();
  const selected = params.get("c") ?? "";
  const [q, setQ] = useState("");
  const [showHidden, setShowHidden] = useState(false);

  const status = usePolling((signal) => api.agents(signal), { intervalMs: 60_000 });
  const voice = usePolling((signal) => api.voice(signal), { intervalMs: 60_000 });
  const [anyPending, setAnyPending] = useState(false);
  const convs = usePolling((signal) => api.agentConversations(signal), {
    intervalMs: anyPending ? 2_500 : 15_000,
  });
  const all = useMemo(() => convs.data?.conversations ?? [], [convs.data]);
  useEffect(() => setAnyPending(all.some((c) => c.state === "pending")), [all]);

  const needle = q.trim().toLowerCase();
  const matches = (c: AgentConversation) =>
    !needle ||
    c.title.toLowerCase().includes(needle) ||
    plainText(c.last?.text ?? "").toLowerCase().includes(needle);
  const active = all.filter((c) => !c.hidden && matches(c));
  const hidden = all.filter((c) => c.hidden && matches(c));

  const open = (id: string) => setParams(id ? { c: id } : {});
  const current = all.find((c) => c.id === selected);

  // Chats that are not on screen keep being read when asked to: a chat that is working
  // and speaks is watched until its turn ends, even after the list stops calling it pending.
  const vcfg = voice.data?.config ?? null;
  const [watch, setWatch] = useState<string[]>([]);
  useEffect(() => {
    if (!vcfg?.tts_background) return setWatch([]);
    const wanted = all.filter((c) => c.state === "pending" && c.id !== selected && speaksIn(c, vcfg)).map((c) => c.id);
    setWatch((w) => (wanted.every((id) => w.includes(id)) ? w : [...new Set([...w, ...wanted])]));
  }, [all, selected, vcfg]);

  return (
    <Section title="Agents" fill bare>
      {convs.error ? <StatusMessage tone="error">{convs.error}</StatusMessage> : null}
      {status.data?.config._problem ? <StatusMessage tone="error">{status.data.config._problem}</StatusMessage> : null}
      <div className="ml-agents">
        <aside className="ml-agents__side" aria-label="agent conversations">
          <div className="ml-agents__tools">
            <SearchInput value={q} onChange={setQ} placeholder="search conversations" aria-label="search conversations" />
            <Button size="sm" variant="primary" onClick={() => open("")} title="Start a new conversation" disabled={!writable}>
              ＋ New
            </Button>
            <Button
              size="sm"
              onClick={() => void api.openAgentPanel()}
              title={`Open the ${agentName}: a floating terminal running Claude Code (the first open builds it, ~1-2 min)`}
              aria-label={`pop out the ${agentName}`}
              disabled={!writable}
            >
              <Icon name="external" />
            </Button>
          </div>
          {!convs.data && !convs.error ? <Spinner label="reading conversations" /> : null}
          {convs.data && !active.length && !hidden.length ? (
            <EmptyState title={needle ? "Nothing matches" : "No conversations yet"} hint={needle ? undefined : "Write one on the right."} />
          ) : null}
          <div className="ml-agents__lists">
            <ConvList label="active conversations" items={active} selected={selected} onOpen={open} />
            {hidden.length ? (
              <div className="ml-agents__hidden">
                <button
                  type="button"
                  className="ml-agents__fold"
                  aria-expanded={showHidden}
                  onClick={() => setShowHidden((v) => !v)}
                >
                  <span aria-hidden="true"><Icon name={showHidden ? "chevron-down" : "chevron-right"} /></span> Hidden ({hidden.length})
                </button>
                {showHidden ? <ConvList label="hidden conversations" items={hidden} selected={selected} onOpen={open} /> : null}
              </div>
            ) : null}
          </div>
          <UsageMeter active />
        </aside>

        <section className="ml-agents__main" aria-label="agent conversation">
          {vcfg?.tts_background
            ? watch
                .filter((id) => id !== selected)
                .map((id) => {
                  const c = all.find((x) => x.id === id);
                  return c ? <BackgroundVoice key={id} conv={c} cfg={vcfg} onDone={() => setWatch((w) => w.filter((x) => x !== id))} /> : null;
                })
            : null}
          <Thread
            key={selected || "new"}
            id={selected}
            voice={voice.data}
            summary={current}
            connectors={status.data?.connectors ?? null}
            defaults={status.data?.config ?? null}
            writable={writable}
            onCreated={(id) => {
              open(id);
              convs.refresh();
            }}
            onChanged={() => convs.refresh()}
          />
        </section>
      </div>
    </Section>
  );
}

/** Speech is on for a chat: its own choice, else the global one. */
function speaksIn(c: Pick2, cfg: VoiceConfig): boolean {
  return c?.tts === "on" || (cfg.tts_enabled && c?.tts !== "off");
}
type Pick2 = { tts?: "on" | "off" | "" } | undefined;

const DONE: Record<string, string> = { pt: "concluiu", en: "finished", es: "terminó", it: "ha finito" };

/**
 * Reads a chat that is not on screen: polls its thread while it works and queues its
 * blocks, then says that it finished. Rendered only when "Speak background chats" is on.
 */
function BackgroundVoice({ conv, cfg, onDone }: { conv: AgentConversation; cfg: VoiceConfig; onDone: () => void }) {
  const thread = usePolling((signal) => api.agentThread(conv.id, signal), { intervalMs: 2_000, key: conv.id });
  useSpeakBlocks({
    id: conv.id,
    title: conv.title,
    messages: thread.data?.messages ?? null,
    turnEnd: thread.data?.conversation.turn_end,
    enabled: speaksIn(conv, cfg),
    translateTo: cfg.translate ? cfg.translate_target : null,
    onSettled: (end) => {
      const lang = cfg.translate ? cfg.translate_target : cfg.tts_language;
      speech.enqueue({ chat: conv.id, chatTitle: conv.title, text: `${DONE[lang] ?? DONE.en}${end.status === "ok" ? "" : ` (${end.status})`}` });
      onDone();
    },
  });
  return null;
}

function ConvList({
  label,
  items,
  selected,
  onOpen,
}: {
  label: string;
  items: AgentConversation[];
  selected: string;
  onOpen: (id: string) => void;
}) {
  return (
    <ul className="ml-agents__list" aria-label={label}>
      {items.map((c) => (
        <li key={c.id}>
          <button
            type="button"
            className={`ml-agents__conv${c.id === selected ? " is-active" : ""}`}
            aria-current={c.id === selected ? "true" : undefined}
            onClick={() => onOpen(c.id)}
          >
            <span className="ml-agents__conv-top">
              <StateIcon state={c.state ?? "empty"} />
              <span className="ml-agents__name">{c.title || "Untitled"}</span>
              <time className="ml-agents__when" dateTime={c.updated_at} title={c.updated_at}>
                {formatDate(c.updated_at)}
              </time>
            </span>
            <span className="ml-agents__preview">
              {c.last ? `${c.last.role === "user" ? "You: " : ""}${truncate(plainText(c.last.text), 70)}` : c.model}
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}

const STATE_LABEL: Record<AgentConvState, string> = {
  pending: "waiting for the agent",
  question: "the agent asked you something",
  done: "answered",
  error: "the reply failed",
  empty: "no messages yet",
};

/** Spinner while running, help mark when it waits on you, check when answered, close when failed. */
export function StateIcon({ state }: { state: AgentConvState }) {
  const label = STATE_LABEL[state];
  if (state === "pending")
    return (
      <span className="ml-agents__state ml-agents__state--pending" role="img" aria-label={label} title={label}>
        <span className="ml-spinner__dot" aria-hidden="true" />
      </span>
    );
  const glyph = <Icon name={state === "question" ? "help" : state === "done" ? "check" : state === "error" ? "close" : "dot"} />;
  return (
    <span className={`ml-agents__state ml-agents__state--${state}`} role="img" aria-label={label} title={label}>
      {glyph}
    </span>
  );
}

interface ThreadProps {
  id: string;
  voice: VoiceStatus | null;
  summary: AgentConversation | undefined;
  connectors: AgentConnector[] | null;
  defaults: { default_connector: string; context_turns: number } | null;
  writable: boolean;
  onCreated: (id: string) => void;
  onChanged: () => void;
}

interface Pick {
  connector: string;
  model: string;
  full_tools: boolean;
  context_turns: number;
}

function Thread({ id, voice, summary, connectors, defaults, writable, onCreated, onChanged }: ThreadProps) {
  const [fast, setFast] = useState(false);
  const thread = usePolling((signal) => api.agentThread(id, signal), {
    // Fast while a reply runs, so its log and its cost move as it works.
    intervalMs: fast || summary?.state === "pending" ? 1_500 : 10_000,
    enabled: !!id,
    key: id,
  });
  const conv = thread.data?.conversation ?? summary;
  const messages = useMemo(() => (thread.data ? thread.data.messages : null), [thread.data]);
  const live = !!messages?.some((m) => m.status === "queued" || m.status === "running");
  // The list polls slower than the thread; tell it when a reply lands.
  const wasLive = useRef(live);
  useEffect(() => setFast(live), [live]);
  useEffect(() => {
    if (wasLive.current && !live) onChanged();
    wasLive.current = live;
  }, [live, onChanged]);

  const vcfg = voice?.config ?? null;
  const ttsOn = !!vcfg && speaksIn(conv, vcfg);
  const settled = useSpeakBlocks({
    id,
    title: conv?.title ?? "",
    messages,
    turnEnd: thread.data?.conversation.turn_end,
    enabled: ttsOn,
    translateTo: vcfg?.translate ? vcfg.translate_target : null,
  });

  const [draft, setDraft] = useState<Pick | null>(null);
  const pick: Pick | null = conv
    ? { connector: conv.connector, model: conv.model, full_tools: conv.full_tools, context_turns: conv.context_turns }
    : draft ?? (connectors && defaults ? initialPick(connectors, defaults) : null);
  const [error, setError] = useState<string | null>(null);
  const [find, setFind] = useState("");
  const [finding, setFinding] = useState(false);

  const change = async (next: Partial<Pick>) => {
    if (!pick) return;
    const merged = { ...pick, ...next };
    if (next.connector && next.connector !== pick.connector && next.model === undefined) {
      merged.model = connectorOf(connectors, next.connector)?.default_model ?? "";
    }
    if (!conv) {
      setDraft(merged);
      return;
    }
    setError(null);
    try {
      await api.saveAgentConversation({ id: conv.id, ...next, ...(next.connector ? { model: merged.model } : {}) });
      thread.refresh();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  /** Voice settings of this chat (speech, narration): saved as its own, beside the model picks. */
  const change2 = async (patch: { tts?: string; verbosity?: string }) => {
    if (!conv) return;
    setError(null);
    try {
      await api.saveAgentConversation({ id: conv.id, ...patch });
      thread.refresh();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const setHidden = async (hidden: boolean) => {
    if (!conv) return;
    try {
      await api.saveAgentConversation({ id: conv.id, hidden });
      thread.refresh();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const needle = find.trim().toLowerCase();
  const shown = useMemo(() => {
    if (!messages || !needle) return messages;
    return messages.filter((m) =>
      [m.text, m.error, ...m.events.map((e) => `${e.name} ${e.input ?? ""} ${e.output ?? ""}`)]
        .join(" ")
        .toLowerCase()
        .includes(needle),
    );
  }, [messages, needle]);

  // Follow the conversation as it grows, unless you scrolled up to read.
  const list = useRef<HTMLOListElement>(null);
  const atBottom = useRef(true);
  const onScroll = () => {
    const el = list.current;
    if (el) atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
  };
  const toBottom = () => {
    const el = list.current;
    if (el) el.scrollTop = el.scrollHeight;
  };
  useEffect(() => {
    if (!needle && atBottom.current) toBottom();
  }, [thread.data, needle]);

  const local = connectorOf(connectors, conv?.connector ?? "")?.kind === "ollama";
  const doExport = (f: ExportFormat) => {
    if (conv && messages) exportConversation(f, conv, messages);
  };

  return (
    <>
      <header className="ml-agents__head">
        <span className="ml-agents__title">{conv ? conv.title || "Untitled" : "New conversation"}</span>
        {conv?.hidden ? <Badge tone="neutral">hidden</Badge> : null}
        {conv ? (
          <div className="ml-agents__actions" role="toolbar" aria-label="conversation tools">
            {finding ? (
              <SearchInput value={find} onChange={setFind} placeholder="search in this chat" aria-label="search in this chat" />
            ) : null}
            <Button
              size="sm"
              active={finding}
              onClick={() => {
                setFinding((v) => !v);
                setFind("");
              }}
              title="Search in this chat"
              aria-label="search in chat"
            >
              <Icon name="search" />
            </Button>
            <span
              className="ml-agents__cost"
              aria-label="conversation cost"
              title={`${conv.tokens_in ?? 0} tokens in · ${conv.tokens_out ?? 0} out${conv.cost_estimated ? " · estimated from list prices while a reply runs" : ""}`}
            >
              {formatCost(conv.cost_usd ?? 0, !!conv.cost_estimated, local)}
            </span>
            <Menu
              label="Export"
              groups={[
                {
                  items: [
                    { id: "pdf", label: "PDF", description: "Print view, save as PDF", onSelect: () => doExport("pdf") },
                    { id: "txt", label: "Text", description: "The messages, plain", onSelect: () => doExport("txt") },
                    { id: "json", label: "JSON", description: "Everything, tool logs included", onSelect: () => doExport("json") },
                  ],
                },
              ]}
            />
            <Button size="sm" disabled={!writable} onClick={() => void setHidden(!conv.hidden)}>
              {conv.hidden ? "Unhide" : "Hide"}
            </Button>
          </div>
        ) : null}
      </header>

      <ol className="ml-agents__thread" aria-label="agent messages" ref={list} onScroll={onScroll}>
        {thread.error ? <StatusMessage tone="error">{thread.error}</StatusMessage> : null}
        {id && !messages && !thread.error ? <Spinner label="reading messages" /> : null}
        {!id ? <EmptyState title="Start a conversation" hint="Pick an agent and a model below, then write." /> : null}
        {needle && shown ? (
          <li className="ml-agents__found" aria-live="polite">
            {shown.length} of {messages?.length ?? 0} messages match
          </li>
        ) : null}
        {shown?.map((m) => (
          <MessageItem
            key={m.id}
            m={m}
            translateTo={vcfg?.translate ? vcfg.translate_target : null}
            writable={writable}
            onRetry={() => void api.retryAgent(m.id).then(() => { thread.refresh(); onChanged(); }, (e: Error) => setError(e.message))}
            onCancel={() => void api.cancelAgent(m.id).then(() => { thread.refresh(); onChanged(); }, (e: Error) => setError(e.message))}
          />
        ))}
      </ol>

      <Composer
        conv={conv}
        voice={voice}
        settled={settled}
        ttsOn={ttsOn}
        onTts={(on) => void change2({ tts: on ? "on" : "off" })}
        onVerbosity={(v) => void change2({ verbosity: v })}
        pick={pick}
        connectors={connectors}
        writable={writable}
        busy={live}
        suggestion={live ? "" : [...(messages ?? [])].reverse().find((m) => m.role === "agent")?.next_action ?? ""}
        error={error}
        onError={setError}
        onPick={(p) => void change(p)}
        onSent={(res) => {
          atBottom.current = true;
          if (!conv) onCreated(res);
          else {
            thread.refresh();
            onChanged();
          }
        }}
      />
    </>
  );
}

function connectorOf(connectors: AgentConnector[] | null, id: string) {
  return connectors?.find((c) => c.id === id);
}

function initialPick(connectors: AgentConnector[], defaults: { default_connector: string; context_turns: number }): Pick {
  const c = connectorOf(connectors, defaults.default_connector) ?? connectors.find((x) => x.enabled) ?? connectors[0];
  return { connector: c?.id ?? "", model: c?.default_model ?? "", full_tools: false, context_turns: defaults.context_turns };
}

/** An agent's text, shown in the target language when translation is on, with the original a click away. */
function Said({ text, target }: { text: string; target: string | null }) {
  const { shown, original } = useTranslated(text, target);
  return (
    <>
      <Markdown text={shown} />
      {original ? (
        <details className="ml-agents__original">
          <summary>original</summary>
          <Markdown text={original} />
        </details>
      ) : null}
    </>
  );
}

function MessageItem({ m, writable, translateTo, onRetry, onCancel }: {
  m: AgentMessage;
  /** Show the agent's text translated into this language, or as written when null. */
  translateTo: string | null;
  writable: boolean;
  onRetry: () => void;
  /** Stop the reply while it is queued or running; it fails as cancelled. */
  onCancel: () => void;
}) {
  const side = m.role === "user" ? "out" : "in";
  const running = m.status === "queued" || m.status === "running";
  const segs = m.role === "agent" ? segments(m.events, !running && !!m.text.trim(), m.text) : [];
  return (
    <li className={`ml-agents__item ml-agents__item--${side}`} aria-label={m.role === "user" ? "your message" : "agent reply"}>
      {m.role === "agent"
        ? segs.map((seg, i) =>
            seg.said ? (
              <div key={i} className={`ml-agents__bubble ml-agents__bubble--said${seg.thought ? " ml-agents__bubble--thinking" : ""}`}>
                {seg.thought ? <div className="ml-agents__muted"><Icon name="thinking" /> thinking</div> : null}
                <Said text={seg.said} target={translateTo} />
              </div>
            ) : (
              <EventLog key={i} events={seg.events} running={running && i === segs.length - 1} />
            ),
          )
        : null}
      {m.role === "agent" && running ? (
        <div className="ml-agents__bubble ml-agents__bubble--wait">
          <Spinner label="the agent is working" /> {m.status === "queued" ? "queued…" : "working…"}
          <Button size="sm" variant="ghost" disabled={!writable} onClick={onCancel} aria-label="Cancel the reply">
            Cancel
          </Button>
        </div>
      ) : m.status === "error" ? (
        <div className="ml-agents__bubble ml-agents__bubble--error">
          <StatusMessage tone="error">{m.error || "the reply failed"}</StatusMessage>
          <Button size="sm" disabled={!writable} onClick={onRetry}>
            Retry
          </Button>
        </div>
      ) : (
        <div className="ml-agents__bubble">
          {m.role === "agent" ? <Said text={m.text} target={translateTo} /> : <UserText text={m.text} />}
        </div>
      )}
      <div className="ml-agents__meta">
        {m.role === "agent" ? (
          <>
            <StateIcon state={running ? "pending" : m.status === "question" ? "question" : m.status === "error" ? "error" : "done"} />
            <span title={m.connector}>{m.model}</span>
            {m.full_tools ? <Badge tone="warn">tools on</Badge> : null}
            {m.cost_usd !== null ? (
              <span title={`${m.tokens_in} in · ${m.tokens_out} out`}>{formatCost(m.cost_usd, m.cost_estimated, m.cost_usd === 0)}</span>
            ) : null}
            {m.context.length ? <span title={m.context.join("\n")}>{m.context.length} recalled</span> : null}
          </>
        ) : null}
        <time dateTime={m.at} title={m.at}>
          {formatDate(m.at)}
        </time>
      </div>
    </li>
  );
}

const KIND_ICON: Record<string, IconName> = { tool: "tool", skill: "cube", agent: "sparkle", mcp: "plug", thinking: "thinking", text: "message", note: "info" };

/** Every tool, skill and subagent the reply used, folded shut by default. */
function EventLog({ events, running }: { events: AgentEvent[]; running: boolean }) {
  const counts = new Map<string, number>();
  for (const e of events) counts.set(e.kind, (counts.get(e.kind) ?? 0) + 1);
  const summary = [...counts.entries()].map(([k, n], i) => (
    <span key={k}>
      {i ? " · " : ""}
      <Icon name={KIND_ICON[k] ?? "dot"} /> {n} {k}{n > 1 ? "s" : ""}
    </span>
  ));
  return (
    <details className="ml-agents__log">
      <summary>
        {summary}
        {running ? " …" : ""}
      </summary>
      <ul aria-label="agent activity">
        {events.map((e, i) => (
          <li key={e.id || i} className={e.parent ? "is-nested" : ""}>
            <details>
              <summary>
                <span aria-hidden="true"><Icon name={KIND_ICON[e.kind] ?? "dot"} /></span> <b>{e.name}</b>
                {e.tool && e.tool !== e.name ? <span className="ml-agents__muted"> {e.tool}</span> : null}
                {e.error ? <Badge tone="danger">failed</Badge> : null}
              </summary>
              {e.input ? <pre className="ml-agents__pre">{e.input}</pre> : null}
              {e.output ? <pre className="ml-agents__pre">{e.output}</pre> : null}
            </details>
          </li>
        ))}
      </ul>
    </details>
  );
}

/** What the owner wrote, then the files they attached as cards and thumbnails. */
function UserText({ text }: { text: string }) {
  const { body, files } = useMemo(() => splitAttachments(text), [text]);
  return (
    <>
      {body ? <RichText text={body} /> : null}
      {files.length ? <Markdown text={files.map((f) => `[${f.name}](attach:${f.path})`).join("\n\n")} /> : null}
    </>
  );
}

/** A file saved to the brain and waiting for the owner to send. */
interface Pending {
  key: number;
  name: string;
  path: string;
  /** An object URL for an image's thumbnail, freed when the chip goes. */
  preview: string;
  state: "saving" | "ready" | "failed";
}

const ATTACH_MAX = 25 * 1024 * 1024;
const ATTACH_LIMIT = 10;

const toBase64 = (file: File): Promise<string> =>
  new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onerror = () => reject(r.error ?? new Error("could not read the file"));
    r.onload = () => resolve(String(r.result).replace(/^data:[^,]*,/, ""));
    r.readAsDataURL(file);
  });

interface ComposerProps {
  conv: AgentConversation | undefined;
  voice: VoiceStatus | null;
  /** The latest turn end, once every block before it has been queued for speech. */
  settled: TurnEnd | null;
  ttsOn: boolean;
  onTts: (on: boolean) => void;
  onVerbosity: (v: VoiceVerbosity | "") => void;
  pick: Pick | null;
  connectors: AgentConnector[] | null;
  writable: boolean;
  busy: boolean;
  suggestion: string;
  error: string | null;
  onError: (e: string | null) => void;
  onPick: (p: Partial<Pick>) => void;
  onSent: (conversationId: string) => void;
}

const DRAFT_PREFIX = "ml.agent-draft.";

function readDraft(key: string): { text: string; at: string } {
  try {
    const v = JSON.parse(localStorage.getItem(DRAFT_PREFIX + key) ?? "null");
    return { text: typeof v?.text === "string" ? v.text : "", at: typeof v?.at === "string" ? v.at : "" };
  } catch {
    return { text: "", at: "" };
  }
}

function writeDraft(key: string, text: string, at: string) {
  try {
    localStorage.setItem(DRAFT_PREFIX + key, JSON.stringify({ text, at }));
  } catch {
    /* storage full or blocked: the server copy still holds it */
  }
}

const TURN_OPTIONS = Array.from({ length: 11 }, (_, n) => ({
  value: String(n),
  label: n === 0 ? "no history" : `last ${n} exchange${n > 1 ? "s" : ""}`,
}));

function Composer({ conv, voice, settled, ttsOn, onTts, onVerbosity, pick, connectors, writable, busy, suggestion, error, onError, onPick, onSent }: ComposerProps) {
  const draftKey = conv?.id ?? "";
  const [text, setText] = useState(() => readDraft(draftKey).text);
  const [sending, setSending] = useState(false);
  const box = useRef<HTMLTextAreaElement>(null);
  const cmds = useCommands(true);
  const [at, setAt] = useState(0);
  const [dismissed, setDismissed] = useState(false);
  const [files, setFiles] = useState<Pending[]>([]);
  const [dragging, setDragging] = useState(false);
  const picker = useRef<HTMLInputElement>(null);
  const keys = useRef(0);
  const query = slashQuery(text);
  const matches = useMemo(() => (query === null ? [] : search(cmds.data?.commands ?? [], query).slice(0, 40)), [cmds.data, query]);
  const popup = query !== null && !dismissed && !!cmds.data;
  const readOnly = !pick?.full_tools;
  const c = pick ? connectorOf(connectors, pick.connector) : undefined;
  const usable = !!c && c.enabled && !!pick?.model;
  const ready = files.filter((f) => f.state === "ready");
  const saving = files.some((f) => f.state === "saving");
  const canSend = writable && usable && !sending && !saving && (!!text.trim() || ready.length > 0);

  // Drafts are local first: every edit lands in localStorage at once and goes to
  // the brain a moment later; when the two differ, the one edited last wins.
  const pushed = useRef<ReturnType<typeof setTimeout>>();
  const shown = useRef({ key: draftKey, text });
  shown.current = { key: draftKey, text };
  const textRef = useRef(text);
  textRef.current = text;
  const edit = (next: string) => {
    textRef.current = next;
    setText(next);
    const at = new Date().toISOString();
    writeDraft(draftKey, next, at);
    clearTimeout(pushed.current);
    if (conv && writable) {
      pushed.current = setTimeout(() => void api.saveAgentConversation({ id: conv.id, draft: next, draft_at: at }).catch(() => {}), 800);
    }
  };
  useEffect(() => () => clearTimeout(pushed.current), [draftKey]);
  useEffect(() => {
    const local = readDraft(draftKey);
    if (shown.current.key === draftKey && conv && conv.draft_at > local.at && conv.draft !== local.text) {
      writeDraft(draftKey, conv.draft, conv.draft_at);
      setText(conv.draft);
    } else if (conv && local.at > conv.draft_at && local.text !== conv.draft && writable) {
      void api.saveAgentConversation({ id: conv.id, draft: local.text, draft_at: local.at }).catch(() => {});
    }
  }, [draftKey, conv?.draft, conv?.draft_at, writable]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (shown.current.text !== readDraft(draftKey).text) setText(readDraft(draftKey).text);
  }, [draftKey]);

  const drop = (f: Pending) => {
    if (f.preview) URL.revokeObjectURL(f.preview);
    setFiles((all) => all.filter((x) => x.key !== f.key));
  };

  /** Each file is saved at once, so it is in the brain's home before the message is sent. */
  const attach = (list: File[]) => {
    if (!writable || !list.length) return;
    const room = ATTACH_LIMIT - files.length;
    if (list.length > room) onError(`At most ${ATTACH_LIMIT} files go with one message.`);
    for (const file of list.slice(0, Math.max(room, 0))) {
      if (file.size > ATTACH_MAX) {
        onError(`${file.name || "A file"} is over 25 MB.`);
        continue;
      }
      const key = ++keys.current;
      const name = file.name || `pasted-${key}.${(file.type.split("/")[1] || "bin").replace(/\+.*/, "")}`;
      const preview = file.type.startsWith("image/") ? URL.createObjectURL(file) : "";
      setFiles((all) => [...all, { key, name, path: "", preview, state: "saving" }]);
      toBase64(file)
        .then((data) => api.attachFile(name, data))
        .then(
          (res) => setFiles((all) => all.map((x) => (x.key === key ? { ...x, path: res.path, state: "ready" } : x))),
          (err) => {
            onError(err instanceof Error ? err.message : String(err));
            setFiles((all) => all.map((x) => (x.key === key ? { ...x, state: "failed" } : x)));
          },
        );
    }
  };

  const hasFiles = (e: DragEvent<HTMLElement>) => Array.from(e.dataTransfer?.types ?? []).includes("Files");

  const send = async (): Promise<string> => {
    // From the ref, not the render: a voice session sends right after its last words landed.
    const body = textRef.current.trim();
    if (!(writable && usable && !sending && !saving && (!!body || ready.length > 0)) || !pick) return "";
    setSending(true);
    onError(null);
    try {
      const res = await api.sendAgent({
        text: body,
        ...(ready.length ? { attachments: ready.map((f) => f.path) } : {}),
        conversation: conv?.id,
        connector: pick.connector,
        model: pick.model,
        full_tools: pick.full_tools,
        context_turns: pick.context_turns,
      });
      if (conv) void api.saveAgentConversation({ id: conv.id, draft: "", draft_at: new Date().toISOString() }).catch(() => {});
      clearTimeout(pushed.current);
      writeDraft(draftKey, "", new Date().toISOString());
      textRef.current = "";
      setText("");
      files.forEach((f) => f.preview && URL.revokeObjectURL(f.preview));
      setFiles([]);
      onSent(res.conversation.id);
      box.current?.focus();
      return res.reply_id;
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err));
      return "";
    } finally {
      setSending(false);
    }
  };

  const session = useVoiceSession({
    config: voice?.config ?? null,
    getText: () => textRef.current,
    update: (fn) => edit(fn(textRef.current)),
    send,
    settled,
    onError,
    focus: () => box.current?.focus(),
  });

  const choose = (cmd: { name: string; writes: boolean }) => {
    if (readOnly && cmd.writes) return;
    edit(`/${cmd.name} `);
    setDismissed(true);
    box.current?.focus();
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Tab" && !e.shiftKey && !text && suggestion && !e.nativeEvent.isComposing) {
      e.preventDefault();
      edit(suggestion);
      return;
    }
    if (popup) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const n = matches.length || 1;
        setAt((i) => (i + (e.key === "ArrowDown" ? 1 : n - 1)) % n);
        return;
      }
      if ((e.key === "Enter" || e.key === "Tab") && matches[at] && !e.nativeEvent.isComposing) {
        e.preventDefault();
        choose(matches[at]!);
        return;
      }
      if (e.key === "Escape") {
        setDismissed(true);
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
    }
  };

  const why = !writable
    ? "This server is read-only (--no-write)."
    : connectors && !connectors.length
      ? "No connectors. Add one in Settings → Agents."
      : c && !c.enabled
        ? `The ${c.name} connector is turned off.`
        : c?.problem || (c && !pick?.model ? `Pick a model for ${c.name}.` : "");

  return (
    <div
      className={`ml-agents__compose${dragging ? " is-dragging" : ""}`}
      onDragEnter={(e) => {
        if (writable && hasFiles(e)) setDragging(true);
      }}
      onDragOver={(e) => {
        if (writable && hasFiles(e)) e.preventDefault();
      }}
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDragging(false);
      }}
      onDrop={(e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        setDragging(false);
        attach(Array.from(e.dataTransfer.files));
      }}
    >
      {dragging ? <div className="ml-agents__drop">Drop to attach</div> : null}
      {why ? (
        <p className="ml-agents__hint">
          {why} {writable ? <Link to="/settings">Settings</Link> : null}
        </p>
      ) : null}
      {error ? <StatusMessage tone="error">{error}</StatusMessage> : null}
      {pick && connectors ? (
        <div className="ml-agents__pickers">
          <ModelPicker pick={pick} connectors={connectors} disabled={!writable} onPick={onPick} />
          <Select
            aria-label="context"
            title="Earlier exchanges each turn carries, besides what the brain recalls"
            value={String(pick.context_turns)}
            options={TURN_OPTIONS}
            onChange={(e) => onPick({ context_turns: Number(e.target.value) })}
            disabled={!writable}
          />
          <span title="Off: the agent can read and look things up, and changes nothing. On: it may act (edit, run commands, write to the brain).">
            {/* The box is the label: ticked means read-only. It once ticked for
                "tools on" while saying "Read-only" unticked, which read as the opposite. */}
            <Checkbox
              checked={!pick.full_tools}
              onChange={(readOnly) => onPick({ full_tools: !readOnly })}
              label="Read-only"
              disabled={!writable}
            />
          </span>
          {cmds.data ? (
            <PluginsMenu plugins={cmds.data.plugins} disabled={!writable} onChange={cmds.setData} />
          ) : null}
        </div>
      ) : (
        <Spinner label="reading connectors" />
      )}
      {cmds.error ? <StatusMessage tone="error">{cmds.error}</StatusMessage> : null}
      <VoiceBar session={session} status={voice} conv={conv} ttsOn={ttsOn} writable={writable} onTts={onTts} onVerbosity={onVerbosity} />
      {files.length ? (
        <ul className="ml-agents__files" aria-label="attached files">
          {files.map((f) => (
            <li key={f.key} className={`ml-agents__file is-${f.state}`}>
              {f.preview ? <img src={f.preview} alt="" /> : <span aria-hidden="true"><Icon name="file" /></span>}
              <span className="ml-agents__file-name" title={f.name}>{f.name}</span>
              {f.state === "saving" ? <span className="ml-agents__muted">saving…</span> : null}
              {f.state === "failed" ? <Badge tone="danger">failed</Badge> : null}
              <button type="button" className="ml-agents__file-x" aria-label={`Remove ${f.name}`} onClick={() => drop(f)}>
                ×
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      <div className="ml-agents__compose-row">
        <input
          ref={picker}
          type="file"
          multiple
          hidden
          aria-label="choose files"
          onChange={(e) => {
            attach(Array.from(e.target.files ?? []));
            e.target.value = "";
          }}
        />
        <Button
          variant="ghost"
          aria-label="Attach files"
          title="Attach files (or drop or paste them here)"
          disabled={!writable}
          onClick={() => picker.current?.click()}
        >
          <Icon name="paperclip" />
        </Button>
        {popup ? <CommandPopup items={matches} active={at} readOnly={readOnly} onPick={choose} onHover={setAt} /> : null}
        <TextArea
          ref={box}
          rows={2}
          value={text}
          disabled={!writable}
          placeholder={busy ? "The agent is still answering. You can queue another message." : suggestion ? `${suggestion}  (Tab to use)` : "Write to the agent. Enter sends, Shift+Enter for a new line, / for a skill."}
          aria-label="message to the agent"
          onChange={(e) => {
            edit(e.target.value);
            setAt(0);
            setDismissed(false);
          }}
          onKeyDown={onKey}
          onPaste={(e: ClipboardEvent<HTMLTextAreaElement>) => {
            const pasted = Array.from(e.clipboardData?.files ?? []);
            if (!pasted.length) return;
            e.preventDefault();
            attach(pasted);
          }}
        />
        <Button variant="primary" disabled={!canSend} onClick={() => void send()}>
          {sending ? "sending…" : "Send"}
        </Button>
      </div>
    </div>
  );
}

/** A connector's models, with the conversation's own first if the host no longer lists it. */
function modelsOf(c: AgentConnector, pick: Pick): string[] {
  return c.id !== pick.connector || !pick.model || c.models.includes(pick.model) ? c.models : [pick.model, ...c.models];
}

/**
 * One button for the agent and its model. It opens the agents; pointing at
 * one (or tapping it) opens its models to the side.
 */
function ModelPicker({
  pick,
  connectors,
  disabled,
  onPick,
}: {
  pick: Pick;
  connectors: AgentConnector[];
  disabled: boolean;
  onPick: (p: Partial<Pick>) => void;
}) {
  const [open, setOpen] = useState(false);
  const [side, setSide] = useState("");
  const root = useRef<HTMLDivElement>(null);
  const current = connectorOf(connectors, pick.connector);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const toggle = () => {
    setSide(pick.connector);
    setOpen((v) => !v);
  };
  const choose = (connector: string, model: string) => {
    setOpen(false);
    if (connector !== pick.connector) onPick({ connector, model });
    else if (model !== pick.model) onPick({ model });
  };

  return (
    <div className="ml-pick" ref={root}>
      <button
        type="button"
        className="ml-pick__btn"
        aria-label="agent and model"
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={disabled}
        onClick={toggle}
      >
        <b>{current?.name ?? pick.connector}</b>
        <span className="ml-pick__model">{pick.model || "no model"}</span>
        <span className="ml-pick__caret" aria-hidden="true"><Icon name="chevron-up" /></span>
      </button>
      {open ? (
        <ul className="ml-pick__menu" role="menu" aria-label="agents">
          {connectors.map((x) => {
            const models = modelsOf(x, pick);
            const shown = side === x.id;
            return (
              <li key={x.id} role="none" className="ml-pick__row" onPointerEnter={() => setSide(x.id)}>
                <button
                  type="button"
                  role="menuitem"
                  className={`ml-pick__item${x.id === pick.connector ? " is-current" : ""}${shown ? " is-shown" : ""}`}
                  aria-haspopup="menu"
                  aria-expanded={shown}
                  onClick={() => setSide(x.id)}
                  onFocus={() => setSide(x.id)}
                >
                  <span>
                    {x.name}
                    {x.enabled ? "" : " (off)"}
                  </span>
                  <span aria-hidden="true"><Icon name="chevron-right" /></span>
                </button>
                {shown ? (
                  <ul className="ml-pick__sub" role="menu" aria-label={`${x.name} models`}>
                    {models.length ? (
                      models.map((m) => {
                        const on = x.id === pick.connector && m === pick.model;
                        return (
                          <li key={m} role="none">
                            <button
                              type="button"
                              role="menuitemradio"
                              aria-checked={on}
                              className={`ml-pick__item${on ? " is-current" : ""}`}
                              onClick={() => choose(x.id, m)}
                            >
                              <span>{m}</span>
                              {on ? <span aria-hidden="true"><Icon name="check" /></span> : null}
                            </button>
                          </li>
                        );
                      })
                    ) : (
                      <li role="none" className="ml-pick__none">
                        {x.problem || "no models"}
                      </li>
                    )}
                  </ul>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}
