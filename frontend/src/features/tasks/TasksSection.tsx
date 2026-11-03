import { useMemo, useState } from "react";
import { api } from "@/api/client";
import { usePolling } from "@/hooks/usePolling";
import { usePref } from "@/hooks/usePrefs";
import { EmptyState, FacetDropdown, LiveDot, SearchInput, Spinner, StatusMessage } from "@/components";
import type { FacetOption } from "@/components";
import { Section } from "@/features/shell/Section";
import type { TasksPayload } from "@/api/types";
import { STATUS_ORDER, facetCounts, filterTasks } from "./filter";
import type { Excluded, Facet } from "./filter";
import { TaskRunCard } from "./TaskRunCard";
import "./TasksSection.css";

/** How often the list is re-read, so a running task can be watched. */
export const TASKS_POLL_MS = 5000;

const FACETS: { facet: Facet; label: string }[] = [
  { facet: "status", label: "status" },
  { facet: "source", label: "source" },
  { facet: "requester", label: "who" },
  { facet: "name", label: "name" },
];

/**
 * Every task the triggers started — a Slack mention, a Jira comment, a GitHub
 * review request, a command — as a card coloured by how it is going.
 *
 * Re-read every five seconds. The filters keep what is switched off rather
 * than what is on, so a new requester arriving on the next poll is shown, not
 * silently filtered out by a choice made before they existed.
 */
export function TasksSection() {
  const { data, error, live, refresh } = usePolling<TasksPayload>((signal) => api.tasks(signal), {
    intervalMs: TASKS_POLL_MS,
  });
  const [q, setQ] = useState("");
  const [excluded, setExcluded] = usePref<Excluded>("tasks.excluded", {});
  const [expanded, setExpanded] = useState<string | null>(null);

  const tasks = useMemo(() => data?.tasks ?? [], [data]);
  const now = data?.now ? Date.parse(data.now) : Date.now();
  const shown = useMemo(() => filterTasks(tasks, q, excluded), [tasks, q, excluded]);

  const options = (facet: Facet): FacetOption[] => {
    const counts = facetCounts(tasks, facet);
    const values = [...counts.keys()];
    values.sort(
      facet === "status"
        ? (a, b) => rank(a) - rank(b)
        : (a, b) => (counts.get(b) ?? 0) - (counts.get(a) ?? 0) || a.localeCompare(b),
    );
    return values.map((v) => ({
      value: v,
      count: counts.get(v),
      swatch: facet === "status" ? `var(--task-${v}, var(--muted))` : undefined,
    }));
  };

  const active = (data?.counts.running ?? 0) + (data?.counts.queued ?? 0);
  const filtering = Boolean(q.trim()) || Object.values(excluded).some((v) => v?.length);

  return (
    <Section
      title="Tasks"
      width={1100}
      lead={
        <>
          Every task a trigger started, newest first, re-read every five seconds.{" "}
          {data ? (
            <>
              {data.total} in all{active ? `, ${active} running or queued` : ""}
              {data.truncated ? ` (the newest ${data.limit} are listed)` : ""}.
            </>
          ) : null}
        </>
      }
      actions={<LiveDot ok={live} label={live ? "live" : "offline"} />}
    >
      <div className="ml-tasks__bar">
        <SearchInput
          value={q}
          onChange={setQ}
          placeholder="search name, message, who, output, id"
          aria-label="search tasks"
        />
        {FACETS.map(({ facet, label }) => {
          const opts = options(facet);
          const off = new Set(excluded[facet] ?? []);
          return (
            <FacetDropdown
              key={facet}
              name={label}
              options={opts}
              selected={new Set(opts.map((o) => o.value).filter((v) => !off.has(v)))}
              onChange={(next) =>
                setExcluded({
                  ...excluded,
                  [facet]: opts.map((o) => o.value).filter((v) => !next.has(v)),
                })
              }
            />
          );
        })}
        {filtering ? (
          <button
            type="button"
            className="ml-tasks__clear"
            onClick={() => {
              setQ("");
              setExcluded({});
            }}
          >
            clear filters
          </button>
        ) : null}
      </div>

      {!data && !error ? <Spinner label="reading tasks" /> : null}
      {error ? <StatusMessage tone="error">{error}</StatusMessage> : null}

      {data ? (
        <p className="ml-tasks__count">
          {shown.length === tasks.length ? `${tasks.length} tasks` : `${shown.length} of ${tasks.length} tasks`}
        </p>
      ) : null}

      {data && !shown.length ? (
        <EmptyState
          title={tasks.length ? "No task matches" : "No tasks yet"}
          hint={
            tasks.length
              ? "Loosen the search or switch a filter back on."
              : "A task appears here when a Slack, Jira or GitHub trigger fires."
          }
        />
      ) : null}

      <div className="ml-tasks__list" role="list" aria-label="tasks">
        {shown.map((t) => (
          <TaskRunCard
            key={t.id}
            task={t}
            now={now}
            open={expanded === t.id}
            onToggle={() => setExpanded(expanded === t.id ? null : t.id)}
            onChanged={refresh}
          />
        ))}
      </div>
    </Section>
  );
}

function rank(status: string): number {
  const i = STATUS_ORDER.indexOf(status);
  return i < 0 ? STATUS_ORDER.length : i;
}
