import { useState } from "react";
import { api } from "@/api/client";
import { useMeta } from "@/api/MetaContext";
import { Badge, Button, KeyValue, Panel, StatusMessage, Icon } from "@/components";
import { colorForType } from "@/lib/colors";
import { isCompletable, isDone } from "@/lib/vocab";
import { NodeEditor } from "./NodeEditor";
import type { GraphEdge, GraphNode } from "@/api/types";
import "./NodeCard.css";

export interface NodeCardProps {
  node: GraphNode;
  edges: GraphEdge[];
  byId: Map<string, GraphNode>;
  onClose: () => void;
  onFocus: (id: string) => void;
  /** Re-reads the graph after a write, so the card shows what was saved. */
  onChanged: () => void;
}

/**
 * The inspector for one node: its fields, what it connects to, and everything
 * the card can DO.
 *
 * Every action lives in this bar and nowhere else — there is no action on the
 * canvas itself, where a stray click is a pan or a select.
 */
export function NodeCard({ node, edges, byId, onClose, onFocus, onChanged }: NodeCardProps) {
  const { writable, error: metaError } = useMeta();
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const outgoing = edges.filter((e) => e.src === node.id);
  const incoming = edges.filter((e) => e.dst === node.id);

  const complete = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.complete(node.id);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const props = Object.entries(node.props ?? {}).filter(([k]) => k !== "path");

  return (
    <Panel className="ml-nodecard">
      <button type="button" className="ml-nodecard__x" onClick={onClose} aria-label="close"><Icon name="close" /></button>
      <Badge tone="type" color={colorForType(node.type)}>{node.type}</Badge>
      <h3 className="ml-nodecard__title">{node.title || node.id}</h3>

      <KeyValue
        pairs={[
          ["id", node.id],
          ["realm", node.realm],
          ["kind", node.kind && node.kind !== "(none)" ? node.kind : ""],
          ["status", node.status],
          ["due", node.due],
          ["connections", outgoing.length + incoming.length],
          ...props.map(([k, v]) => [k, String(v).slice(0, 120)] as [string, string]),
        ]}
      />

      {editing ? (
        <NodeEditor
          node={node}
          onCancel={() => setEditing(false)}
          onDone={() => {
            setEditing(false);
            onChanged();
          }}
        />
      ) : (
        <>
          <div className="ml-nodecard__actions">
            {!writable ? (
              // Say which kind of no it is: a read-only server is a choice, a
              // failed /api/meta is a fault the next poll may clear.
              <span className="ml-nodecard__note">
                {metaError ? "writes unavailable" : "read-only server (--no-write)"}
              </span>
            ) : (
              <>
                {isCompletable(node.type) ? (
                  isDone(node.status) ? (
                    <span className="ml-nodecard__note"><Icon name="check" /> archived</span>
                  ) : (
                    <Button size="sm" onClick={complete} disabled={busy}>
                      {busy ? "saving…" : <><Icon name="check" /> Complete</>}
                    </Button>
                  )
                ) : null}
                <Button size="sm" onClick={() => setEditing(true)}><Icon name="pencil" /> Edit</Button>
              </>
            )}
          </div>
          {error ? <StatusMessage tone="error">{error}</StatusMessage> : null}
          {node.body ? <div className="ml-nodecard__body">{node.body.slice(0, 600)}</div> : null}
        </>
      )}

      {incoming.length ? (
        <>
          <h4 className="ml-nodecard__h"><Icon name="arrow-left" /> incoming ({incoming.length})</h4>
          {incoming.map((e, i) => (
            <button
              type="button"
              key={`in-${e.src}-${e.rel}-${i}`}
              className="ml-rel"
              onClick={() => onFocus(e.src)}
            >
              <span className="ml-rel__arrow"><Icon name="arrow-left" /></span>
              <span>
                {byId.get(e.src)?.title || e.src} <em className="ml-rel__r">{e.rel}</em>
              </span>
            </button>
          ))}
        </>
      ) : null}

      {outgoing.length ? (
        <>
          <h4 className="ml-nodecard__h">outgoing <Icon name="arrow-right" /> ({outgoing.length})</h4>
          {outgoing.map((e, i) => (
            <button
              type="button"
              key={`out-${e.dst}-${e.rel}-${i}`}
              className="ml-rel"
              onClick={() => onFocus(e.dst)}
            >
              <span className="ml-rel__arrow"><Icon name="arrow-right" /></span>
              <span>
                <em className="ml-rel__r">{e.rel}</em> {byId.get(e.dst)?.title || e.dst}
              </span>
            </button>
          ))}
        </>
      ) : null}
    </Panel>
  );
}
