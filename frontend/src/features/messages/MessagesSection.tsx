import { useLayoutEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "@/api/client";
import { useMeta } from "@/api/MetaContext";
import { usePolling } from "@/hooks/usePolling";
import { Badge, Button, EmptyState, RichText, SearchInput, Spinner, StatusMessage, TextArea } from "@/components";
import { Section } from "@/features/shell/Section";
import { formatDate, truncate } from "@/lib/format";
import { applyMark, plainText } from "@/lib/richText";
import type { Mark } from "@/lib/richText";
import type { B2BConversation, B2BMessage, B2BThread } from "@/api/types";
import "./MessagesSection.css";

/**
 * Brain-to-brain messages as conversations (docs/b2b.md).
 *
 * On the left, every brain this one wrote to or heard from, plus connected
 * peers not talked to yet, with a search and a way to start a conversation. On
 * the right, the thread with the one picked, both directions, and a box to
 * write in.
 *
 * Every message says who wrote it: 🧬 a person, 🤖 a session or a script.
 * Only what is typed here counts as a person's; the flag is sealed with the
 * text, so the other brain shows it the same way.
 */
export function MessagesSection() {
  const { writable } = useMeta();
  const [params, setParams] = useSearchParams();
  const selected = params.get("peer") ?? "";
  const [q, setQ] = useState("");
  const [picking, setPicking] = useState(false);

  const convs = usePolling((signal) => api.b2bConversations(signal), { intervalMs: 15_000 });
  const thread = usePolling((signal) => api.b2bThread(selected, "", signal), {
    intervalMs: 10_000,
    enabled: !!selected,
    key: selected,
  });

  const all = useMemo(() => convs.data?.conversations ?? [], [convs.data]);
  const needle = q.trim().toLowerCase();
  const matches = (c: B2BConversation) =>
    !needle ||
    c.name.toLowerCase().includes(needle) ||
    c.peer_id.toLowerCase().includes(needle) ||
    plainText(c.last?.text ?? "").toLowerCase().includes(needle);
  const shown = picking ? all.filter((c) => c.connected).filter(matches) : all.filter((c) => c.messages && matches(c));

  const open = (peer: string) => {
    setPicking(false);
    setQ("");
    setParams(peer ? { peer } : {});
  };

  const current = all.find((c) => c.peer_id === selected);
  // The previous peer's thread stays in state until the new one lands.
  const latest = thread.data?.peer_id === selected ? thread.data : null;

  return (
    <Section
      title="Messages"
      lead="Conversations with other second brains. 🧬 a person wrote it, 🤖 a session or a script did."
      width={1120}
    >
      {convs.error ? <StatusMessage tone="error">{convs.error}</StatusMessage> : null}
      <div className="ml-msg">
        <aside className="ml-msg__side" aria-label="conversations">
          <div className="ml-msg__tools">
            <SearchInput
              value={q}
              onChange={setQ}
              placeholder={picking ? "find a peer" : "search conversations"}
              aria-label={picking ? "find a peer" : "search conversations"}
            />
            <Button
              size="sm"
              variant={picking ? "default" : "primary"}
              active={picking}
              onClick={() => {
                setPicking((v) => !v);
                setQ("");
              }}
              title="Start a new conversation"
            >
              {picking ? "Cancel" : "＋ New"}
            </Button>
          </div>
          {picking ? <p className="ml-msg__hint">Start a conversation with a connected peer.</p> : null}
          {!convs.data && !convs.error ? <Spinner label="reading conversations" /> : null}
          {convs.data && !shown.length ? (
            <EmptyState
              title={needle ? "Nothing matches" : picking ? "No connected peers" : "No conversations yet"}
              hint={
                needle ? undefined : picking ? (
                  <>
                    Connect one in <Link to="/settings">Settings → B2B</Link>.
                  </>
                ) : (
                  "Start one with ＋ New."
                )
              }
            />
          ) : null}
          <ul className="ml-msg__list" aria-label={picking ? "peers" : "conversation list"}>
            {shown.map((c) => (
              <li key={c.peer_id}>
                <button
                  type="button"
                  className={`ml-msg__conv${c.peer_id === selected ? " is-active" : ""}`}
                  aria-current={c.peer_id === selected ? "true" : undefined}
                  onClick={() => open(c.peer_id)}
                >
                  <span className="ml-msg__conv-top">
                    <span className="ml-msg__name">{displayName(c)}</span>
                    {c.last ? (
                      <time className="ml-msg__when" dateTime={c.last.at} title={c.last.at}>
                        {formatDate(c.last.at)}
                      </time>
                    ) : null}
                  </span>
                  <span className="ml-msg__preview">
                    {c.last ? (
                      <>
                        <AuthorIcon human={c.last.human} />
                        {c.last.direction === "out" ? "You: " : ""}
                        {truncate(plainText(c.last.text), 70)}
                      </>
                    ) : (
                      "no messages yet"
                    )}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </aside>

        <section className="ml-msg__main" aria-label="conversation">
          {!selected ? (
            <EmptyState title="Pick a conversation" hint="Or start a new one with ＋ New." />
          ) : (
            <Thread
              key={selected}
              peer={selected}
              conversation={current}
              latest={latest}
              error={thread.error}
              writable={writable}
              onSent={() => {
                thread.refresh();
                convs.refresh();
              }}
            />
          )}
        </section>
      </div>
    </Section>
  );
}

function displayName(c: Pick<B2BConversation, "name" | "peer_id">): string {
  return c.name || `${c.peer_id.slice(0, 10)}…`;
}

/** 🧬 for a person, 🤖 for anything automated. */
export function AuthorIcon({ human }: { human: boolean }) {
  const label = human ? "written by a person" : "automated";
  return (
    <span className="ml-msg__author" role="img" aria-label={label} title={label}>
      {human ? "🧬" : "🤖"}
    </span>
  );
}

interface ThreadProps {
  peer: string;
  conversation: B2BConversation | undefined;
  /** The newest page, refreshed by the poll. */
  latest: B2BThread | null;
  error: string | null;
  writable: boolean;
  onSent: () => void;
}

const ATTACH_MAX = 25 * 1024 * 1024;
const ATTACH_LIMIT = 20;

const toBase64 = (file: File): Promise<string> =>
  new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onerror = () => reject(r.error ?? new Error("could not read the file"));
    r.onload = () => resolve(String(r.result).replace(/^data:[^,]*,/, ""));
    r.readAsDataURL(file);
  });

interface Picked {
  key: number;
  name: string;
  path: string;
  state: "saving" | "ready" | "failed";
}

const msgKey = (m: B2BMessage) => `${m.direction}-${m.id}`;

function Thread({ peer, conversation, latest, error, writable, onSent }: ThreadProps) {
  // The poll gives the latest page; older ones are read on demand and kept here.
  const [older, setOlder] = useState<B2BMessage[]>([]);
  const [cursor, setCursor] = useState<{ more: boolean; before: string } | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [picked, setPicked] = useState<Picked[]>([]);
  const keys = useRef(0);
  const fileInput = useRef<HTMLInputElement>(null);
  const messages = useMemo(() => {
    if (!latest) return null;
    const seen = new Set(latest.messages.map(msgKey));
    return [...older.filter((m) => !seen.has(msgKey(m))), ...latest.messages];
  }, [older, latest]);
  const more = cursor ? cursor.more : !!latest?.has_more;
  const before = cursor ? cursor.before : (latest?.next_before ?? "");

  const loadOlder = async () => {
    if (!more || !before || loadingOlder) return;
    setLoadingOlder(true);
    stick.current = false;
    try {
      const page = await api.b2bThread(peer, before);
      setOlder((all) => [...page.messages, ...all]);
      setCursor({ more: page.has_more, before: page.next_before });
    } catch (err) {
      setSendError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoadingOlder(false);
    }
  };

  const pick = (list: File[]) => {
    const room = ATTACH_LIMIT - picked.length;
    if (list.length > room) setSendError(`At most ${ATTACH_LIMIT} files go with one message.`);
    for (const file of list.slice(0, Math.max(room, 0))) {
      if (file.size > ATTACH_MAX) {
        setSendError(`${file.name || "A file"} is over 25 MB.`);
        continue;
      }
      const key = ++keys.current;
      const name = file.name || `file-${key}`;
      setPicked((all) => [...all, { key, name, path: "", state: "saving" }]);
      toBase64(file)
        .then((data) => api.attachFile(name, data))
        .then(
          (res) => setPicked((all) => all.map((x) => (x.key === key ? { ...x, path: res.path, state: "ready" } : x))),
          (err) => {
            setSendError(err instanceof Error ? err.message : String(err));
            setPicked((all) => all.map((x) => (x.key === key ? { ...x, state: "failed" } : x)));
          },
        );
    }
  };
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  // Where the selection goes after a mark rewrote the text, set once React has.
  const pendingSel = useRef<[number, number] | null>(null);
  useLayoutEffect(() => {
    const el = box.current;
    if (el && pendingSel.current) {
      el.setSelectionRange(...pendingSel.current);
      pendingSel.current = null;
    }
  }, [text]);

  const mark = (m: Mark) => {
    const el = box.current;
    if (!el) return;
    const next = applyMark(text, el.selectionStart, el.selectionEnd, m);
    pendingSel.current = [next.start, next.end];
    setText(next.text);
    el.focus();
  };
  // Newest at the bottom: open scrolled to the end, and follow new messages
  // only while the reader is already near the end (not while reading history).
  const list = useRef<HTMLOListElement>(null);
  const stick = useRef(true);
  useLayoutEffect(() => {
    const el = list.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [messages]);
  const onScroll = () => {
    const el = list.current;
    if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };
  const connected = !!conversation?.connected;
  const ready = picked.filter((f) => f.state === "ready");
  const saving = picked.some((f) => f.state === "saving");
  const canSend = writable && connected && !busy && !saving && (!!text.trim() || ready.length > 0);

  const send = async () => {
    if (!canSend) return;
    setBusy(true);
    setSendError(null);
    try {
      const res = await api.sendB2B(
        peer,
        text.trim(),
        true,
        ready.map((f) => f.path),
      );
      if (res.staged) setSendError(`Waiting to send: ${res.reason ?? "the peer's box is full or the relay is out of reach"}. It goes on its own.`);
      setText("");
      setPicked([]);
      onSent();
      box.current?.focus();
    } catch (err) {
      setSendError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends, Shift+Enter is a new line, as in every chat.
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
      return;
    }
    // ⌘/Ctrl + B, I, U, K, as in every editor; ⇧ + X strikes through.
    if ((e.metaKey || e.ctrlKey) && !e.altKey) {
      const k = e.key.toLowerCase();
      const m: Mark | null =
        k === "b" ? "b" : k === "i" ? "i" : k === "u" ? "u" : k === "k" ? "link" : k === "x" && e.shiftKey ? "s" : null;
      if (m) {
        e.preventDefault();
        mark(m);
      }
    }
  };

  const why = !writable
    ? "This server is read-only (--no-write)."
    : !connected
      ? "Not a connected peer, so this brain cannot write to them. Connect them in Settings → B2B."
      : "";

  return (
    <>
      <header className="ml-msg__head">
        <span className="ml-msg__name">{conversation ? displayName(conversation) : displayName({ name: "", peer_id: peer })}</span>
        <code className="ml-msg__id" title={peer}>
          {truncate(peer, 18)}
        </code>
        {conversation && !conversation.allowed ? <Badge tone="warn">not allowed to send to you</Badge> : null}
      </header>

      <ol className="ml-msg__thread" aria-label="messages" ref={list} onScroll={onScroll}>
        {error ? <StatusMessage tone="error">{error}</StatusMessage> : null}
        {!messages && !error ? <Spinner label="reading messages" /> : null}
        {messages && !messages.length ? <EmptyState title="No messages yet" hint="Say hello below." /> : null}
        {more ? (
          <li className="ml-msg__older">
            <Button size="sm" disabled={loadingOlder} onClick={() => void loadOlder()}>
              {loadingOlder ? "loading…" : "Load earlier messages"}
            </Button>
          </li>
        ) : null}
        {messages?.map((m) => (
          <li key={msgKey(m)} className={`ml-msg__item ml-msg__item--${m.direction}`}>
            <div className="ml-msg__bubble">
              {m.text ? <RichText text={m.text} /> : null}
              {m.files?.length ? (
                <ul className="ml-msg__files" aria-label="attached files">
                  {m.files.map((f) => (
                    <li key={f}>📎 {f}</li>
                  ))}
                </ul>
              ) : null}
            </div>
            <div className="ml-msg__meta">
              <AuthorIcon human={m.human} />
              <time dateTime={m.at} title={m.at}>
                {formatDate(m.at)}
              </time>
              {m.direction === "in" && m.status === "blocked" ? <Badge tone="warn">blocked</Badge> : null}
              {m.direction === "out" && m.status === "staged" ? (
                <Badge tone="warn" title="Waiting: the peer's box is full or the relay is out of reach. Tried again on every poll.">
                  waiting to send
                </Badge>
              ) : null}
              {m.direction === "out" && m.status === "failed" ? <Badge tone="danger">not sent</Badge> : null}
              {m.direction === "in" && m.attachment?.state === "pending" ? (
                <Badge tone="warn">files downloading</Badge>
              ) : null}
              {m.direction === "in" && m.attachment?.state === "failed" ? (
                <Badge tone="danger" title={m.attachment.error}>
                  files refused
                </Badge>
              ) : null}
            </div>
          </li>
        ))}
      </ol>

      <div className="ml-msg__compose">
        {why ? <p className="ml-msg__hint">{why}</p> : null}
        {sendError ? <StatusMessage tone="error">{sendError}</StatusMessage> : null}
        <FormatBar onMark={mark} disabled={!writable || !connected} />
        {picked.length ? (
          <ul className="ml-msg__picked" aria-label="files to send">
            {picked.map((f) => (
              <li key={f.key} className={`ml-msg__chip is-${f.state}`}>
                📎 {f.name}
                {f.state === "saving" ? " …" : f.state === "failed" ? " (failed)" : ""}
                <button
                  type="button"
                  aria-label={`remove ${f.name}`}
                  onClick={() => setPicked((all) => all.filter((x) => x.key !== f.key))}
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
        ) : null}
        <div className="ml-msg__compose-row">
          <TextArea
            ref={box}
            rows={2}
            value={text}
            disabled={!writable || !connected}
            placeholder="Write a message. Enter sends, Shift+Enter for a new line. **bold** *italic* __underline__"
            aria-label="message"
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKey}
          />
          <input
            ref={fileInput}
            type="file"
            multiple
            hidden
            aria-label="attach files"
            onChange={(e) => {
              pick(Array.from(e.target.files ?? []));
              e.target.value = "";
            }}
          />
          <Button
            disabled={!writable || !connected}
            title="Attach files (up to 25 MB each)"
            onClick={() => fileInput.current?.click()}
          >
            📎
          </Button>
          <Button variant="primary" disabled={!canSend} onClick={() => void send()}>
            {busy ? "sending…" : "Send"}
          </Button>
        </div>
      </div>
    </>
  );
}

const MARKS: { mark: Mark; label: ReactNode; title: string }[] = [
  { mark: "b", label: <b>B</b>, title: "Bold (⌘B): **text**" },
  { mark: "i", label: <i>I</i>, title: "Italic (⌘I): *text*" },
  { mark: "u", label: <u>U</u>, title: "Underline (⌘U): __text__" },
  { mark: "s", label: <s>S</s>, title: "Strikethrough (⌘⇧X): ~~text~~" },
  { mark: "code", label: <code>{"</>"}</code>, title: "Code: `text`" },
  { mark: "link", label: "Link", title: "Link (⌘K): [text](https://…)" },
];

/** Buttons that wrap the selection in a mark; the message is sent as typed. */
function FormatBar({ onMark, disabled }: { onMark: (m: Mark) => void; disabled: boolean }) {
  return (
    <div className="ml-msg__format" role="toolbar" aria-label="formatting">
      {MARKS.map((m) => (
        <button
          key={m.mark}
          type="button"
          className="ml-msg__fmt"
          title={m.title}
          aria-label={m.title.split(" (")[0]!.split(":")[0]}
          disabled={disabled}
          // Keep the textarea's selection: a click would otherwise blur it first.
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => onMark(m.mark)}
        >
          {m.label}
        </button>
      ))}
    </div>
  );
}
