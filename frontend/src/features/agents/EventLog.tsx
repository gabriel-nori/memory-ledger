import type { IconName } from "@/components";
import { Badge, Icon } from "@/components";
import type { AgentEvent } from "@/api/types";

const KIND_ICON: Record<string, IconName> = { tool: "tool", skill: "cube", agent: "sparkle", mcp: "plug", thinking: "thinking", text: "message", note: "info" };

/** Every tool, skill and subagent the reply used, folded shut by default. */
export function EventLog({ events, running }: { events: AgentEvent[]; running: boolean }) {
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
