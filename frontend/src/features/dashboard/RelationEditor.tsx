import { useEffect, useState } from "react";
import { api } from "@/api/client";
import { useMeta } from "@/api/MetaContext";
import { Button, Select, StatusMessage, TextInput, Icon } from "@/components";
import { useDebounced } from "@/hooks/useDebounced";
import type { NodeDetail, SearchHit } from "@/api/types";
import "./RelationEditor.css";

export interface RelationEditorProps {
  node: NodeDetail;
  onChanged: () => void;
}

/** Existing links, each removable, plus a search box that builds a new one. */
export function RelationEditor({ node, onChanged }: RelationEditorProps) {
  const { meta, writable } = useMeta();
  const rels = meta?.rels ?? ["relates-to"];
  const [rel, setRel] = useState(rels.includes("relates-to") ? "relates-to" : rels[0] ?? "");
  const [term, setTerm] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const debounced = useDebounced(term, 260);

  useEffect(() => {
    if (debounced.trim().length < 2) {
      setHits([]);
      return;
    }
    const ctrl = new AbortController();
    api
      .search(debounced.trim(), ctrl.signal)
      .then((r) => setHits((r.results || []).filter((x) => x.id !== node.id)))
      .catch(() => {
        /* a stale search is not worth an error banner */
      });
    return () => ctrl.abort();
  }, [debounced, node.id]);

  const edges = node.edges ?? [];

  const unlink = async (dir: string, otherId: string, edgeRel: string) => {
    setBusy(true);
    setError(null);
    try {
      // Edges render from this node's perspective; unlink needs the real direction.
      const src = dir === "out" ? node.id : otherId;
      const dst = dir === "out" ? otherId : node.id;
      await api.unlink(src, dst, edgeRel);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const link = async (dstId: string) => {
    setBusy(true);
    setError(null);
    try {
      await api.link(node.id, dstId, rel);
      setTerm("");
      setHits([]);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="ml-relations">
      <h4 className="ml-relations__h">
        {edges.length ? `Related (${edges.length})` : "No links"}
      </h4>
      {edges.map((e, i) => (
        <div className="ml-relations__row" key={`${e.dir}-${e.rel}-${e.id}-${i}`}>
          <span>
            <Icon name={e.dir === "out" ? "arrow-right" : "arrow-left"} />{" "}
            {e.rel}: {e.title} <em>({e.type})</em>
          </span>
          {writable ? (
            <button
              type="button"
              className="ml-relations__x"
              title="remove this link"
              aria-label={`remove ${e.rel} link to ${e.title}`}
              disabled={busy}
              onClick={() => unlink(e.dir, e.id, e.rel)}
            >
              ×
            </button>
          ) : null}
        </div>
      ))}

      {writable ? (
        <div className="ml-relations__add">
          <TextInput
            value={term}
            placeholder="link to… (search nodes)"
            aria-label="search for a node to link"
            onChange={(e) => setTerm(e.target.value)}
          />
          <Select
            value={rel}
            aria-label="relation"
            onChange={(e) => setRel(e.target.value)}
            options={rels.map((r) => ({ value: r, label: r }))}
          />
        </div>
      ) : null}

      {hits.length ? (
        <div className="ml-relations__hits">
          {hits.map((h) => (
            <Button key={h.id} size="sm" disabled={busy} onClick={() => link(h.id)}>
              {h.title.slice(0, 58)} ({h.type})
            </Button>
          ))}
        </div>
      ) : debounced.trim().length >= 2 ? (
        <p className="ml-relations__none">no matches</p>
      ) : null}

      {error ? <StatusMessage tone="error">{error}</StatusMessage> : null}
    </div>
  );
}
