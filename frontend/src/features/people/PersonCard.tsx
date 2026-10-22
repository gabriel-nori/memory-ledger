import { useMemo } from "react";
import { api } from "@/api/client";
import { useAsync } from "@/hooks/useAsync";
import { Badge, Spinner, StatusMessage, Icon } from "@/components";
import { humanize } from "@/lib/format";
import type { NodeDetail, NodeEdge, Person } from "@/api/types";
import "./PeopleSection.css";

/**
 * One person, and — when you open it — everything one hop away.
 *
 * The edges are fetched on expand rather than up front. A person with a hundred
 * connections is normal, and loading every person's neighbourhood to render a
 * list of names would make the page cost more the better the brain gets.
 */
export function PersonCard({
  person,
  open,
  onToggle,
  onChanged,
}: {
  person: Person;
  open: boolean;
  onToggle: () => void;
  onChanged: () => void;
}) {
  // `open` is in the deps, so collapsing drops the request and reopening is a
  // fresh read — a merge done in another card must not leave stale neighbours.
  const detail = useAsync<NodeDetail | null>(
    (signal) => (open ? api.node(person.id, signal) : Promise.resolve(null)),
    [person.id, open],
  );

  const byRelation = useMemo(() => groupEdges(detail.data?.edges ?? []), [detail.data]);
  const superseded = (detail.data?.props as { superseded_by?: string } | undefined)?.superseded_by;

  return (
    <section className={`ml-person${open ? " is-open" : ""}`} role="listitem">
      <button
        type="button"
        className="ml-person__head"
        onClick={onToggle}
        aria-expanded={open}
        id={`person-${person.id}`}
      >
        <span className="ml-person__name">{person.title}</span>
        {person.realm ? <Badge tone="neutral">{person.realm}</Badge> : null}
        {person.aliases.length ? (
          <span className="ml-person__aliases" title={person.aliases.join(", ")}>
            also {person.aliases.slice(0, 3).join(", ")}
            {person.aliases.length > 3 ? ` +${person.aliases.length - 3}` : ""}
          </span>
        ) : null}
        <span className="ml-person__chevron" aria-hidden="true">
          <Icon name={open ? "chevron-down" : "chevron-right"} />
        </span>
      </button>

      {open ? (
        <div className="ml-person__body">
          {detail.loading ? <Spinner label="reading connections" /> : null}
          {detail.error ? <StatusMessage tone="error">{detail.error}</StatusMessage> : null}

          {superseded ? (
            <StatusMessage tone="info">
              Already merged into <code>{superseded}</code>. It is kept as a resolver so older
              mentions of this spelling still point somewhere.
            </StatusMessage>
          ) : null}

          {detail.data && !detail.data.edges.length ? (
            <p className="ml-people__hint">Nothing points at this person yet.</p>
          ) : null}

          {byRelation.map(([rel, edges]) => (
            <div key={rel} className="ml-person__rel">
              <h4 className="ml-person__relname">
                {humanize(rel)} <span className="ml-person__count">{edges.length}</span>
              </h4>
              <ul className="ml-person__links">
                {edges.map((e) => (
                  <li key={`${e.dir}-${e.rel}-${e.id}`}>
                    <span className="ml-person__dir" aria-hidden="true">
                      <Icon name={e.dir === "out" ? "arrow-right" : "arrow-left"} />
                    </span>
                    <span className="ml-person__type">{e.type || "node"}</span>
                    <span className="ml-person__title">{e.title}</span>
                  </li>
                ))}
              </ul>
            </div>
          ))}

          <AliasForm person={person} onAdded={onChanged} />
        </div>
      ) : null}
    </section>
  );
}

/** Groups the neighbours by relation, biggest group first — the shape of
 *  someone's involvement reads better than one flat list of thirty names. */
function groupEdges(edges: NodeEdge[]): [string, NodeEdge[]][] {
  const map = new Map<string, NodeEdge[]>();
  for (const e of edges) {
    const list = map.get(e.rel);
    if (list) list.push(e);
    else map.set(e.rel, [e]);
  }
  return [...map.entries()].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]));
}

/** Pin another spelling onto this person, without merging anything. */
function AliasForm({ person, onAdded }: { person: Person; onAdded: () => void }) {
  return (
    <form
      className="ml-person__alias-form"
      onSubmit={async (e) => {
        e.preventDefault();
        const input = e.currentTarget.elements.namedItem("alias") as HTMLInputElement | null;
        const alias = input?.value.trim();
        if (!alias) return;
        // The engine's add-alias, not a props write: it is the code that
        // already decides whether two spellings are the same name, and it
        // no-ops rather than duplicating one this person answers to.
        await api.addAlias(person.id, alias);
        if (input) input.value = "";
        onAdded();
      }}
    >
      <label className="ml-person__alias-label" htmlFor={`alias-${person.id}`}>
        Also known as
      </label>
      <input
        id={`alias-${person.id}`}
        name="alias"
        className="ml-input ml-person__alias-input"
        placeholder="another spelling"
      />
      <button type="submit" className="ml-btn">
        Add
      </button>
    </form>
  );
}
