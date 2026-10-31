import { useState } from "react";
import { Badge, Button, Icon, Markdown, Spinner, TextArea } from "@/components";
import type { AgentMessage } from "@/api/types";
import { formatCost } from "./export";
import { EventLog } from "./EventLog";

/** The sub-agents of one message, as blocks under it, each with the ones it started. */
export function SubAgentBlocks({ parentId, subs, writable, onCancel }: {
  parentId: string;
  subs: AgentMessage[];
  writable: boolean;
  onCancel: (id: string) => void;
}) {
  const mine = subs.filter((s) => s.parent_msg_id === parentId);
  if (!mine.length) return null;
  return (
    <ul className="ml-agents__subs" aria-label="sub-agents">
      {mine.map((s) => (
        <SubAgentBlock key={s.id} sub={s} subs={subs} writable={writable} onCancel={onCancel} />
      ))}
    </ul>
  );
}

function SubAgentBlock({ sub, subs, writable, onCancel }: {
  sub: AgentMessage;
  subs: AgentMessage[];
  writable: boolean;
  onCancel: (id: string) => void;
}) {
  // Folded to what it is doing and how it is going; the sub-agents it started stay in view.
  const [open, setOpen] = useState(false);
  const running = sub.status === "queued" || sub.status === "running";
  const tone = sub.status === "error" ? "danger" : running ? "neutral" : "success";
  const state = running ? sub.status : sub.status === "error" ? "failed" : "done";
  return (
    <li className="ml-agents__sub">
      <div className="ml-agents__sub-head">
        <button
          type="button"
          className="ml-agents__sub-toggle"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
        >
          <Icon name={open ? "chevron-down" : "chevron-right"} />
          <Icon name="sparkle" /> <b>{sub.label || "sub-agent"}</b>
          {!open && sub.prompt ? <span className="ml-agents__muted"> {sub.prompt.slice(0, 120)}</span> : null}
        </button>
        {running ? <Spinner label="the sub-agent is working" /> : null}
        <Badge tone={tone}>{state}</Badge>
        {running ? (
          <Button size="sm" variant="ghost" disabled={!writable} onClick={() => onCancel(sub.id)} aria-label="Cancel the sub-agent">
            Cancel
          </Button>
        ) : null}
      </div>
      {open ? (
        <div className="ml-agents__sub-body">
          {sub.prompt ? <pre className="ml-agents__pre">{sub.prompt}</pre> : null}
          {sub.events.length ? <EventLog events={sub.events} running={running} /> : null}
          {sub.status === "error" ? <p className="ml-agents__muted">{sub.error || "it failed"}</p> : null}
          {sub.text ? <Markdown text={sub.text} /> : null}
          <div className="ml-agents__meta">
            {sub.cost_usd !== null ? (
              <span title={`${sub.tokens_in} in · ${sub.tokens_out} out`}>{formatCost(sub.cost_usd, sub.cost_estimated, sub.cost_usd === 0)}</span>
            ) : null}
            <time dateTime={sub.at}>{sub.at}</time>
          </div>
        </div>
      ) : null}
      <SubAgentBlocks parentId={sub.id} subs={subs} writable={writable} onCancel={onCancel} />
    </li>
  );
}

/** Start a sub-agent from a message: what to do, then it appears as a block under it. */
export function SpawnForm({ writable, onSpawn }: { writable: boolean; onSpawn: (prompt: string) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  if (!open) {
    return (
      <Button size="sm" variant="ghost" disabled={!writable} onClick={() => setOpen(true)}>
        Sub-agent
      </Button>
    );
  }
  const start = async () => {
    setBusy(true);
    try {
      await onSpawn(text);
      setText("");
      setOpen(false);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="ml-agents__spawn">
      <TextArea
        value={text}
        rows={2}
        placeholder="What should the sub-agent do?"
        aria-label="Sub-agent task"
        onChange={(e) => setText(e.target.value)}
      />
      <Button size="sm" disabled={busy || !text.trim()} onClick={() => void start()}>
        Start
      </Button>
      <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>
        Cancel
      </Button>
    </div>
  );
}
