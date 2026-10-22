import { Button, Pills, Select, TextInput, Icon } from "@/components";
import { pluralize } from "@/lib/format";
import type { Facets } from "./useFacets";
import type { DashboardState } from "./state";
import "./DashboardFilters.css";

export interface DashboardFiltersProps {
  state: DashboardState;
  onChange: (next: DashboardState) => void;
  facets: Facets;
}

/**
 * The chips over the date views.
 *
 * None selected means every value, which is why there is no "all" pseudo-chip
 * to get out of sync with the real ones.
 */
export function DashboardFilters({ state, onChange, facets }: DashboardFiltersProps) {
  const toggle = (key: "assignees" | "realms" | "kinds", value: string) => {
    const on = state[key].includes(value);
    onChange({
      ...state,
      [key]: on ? state[key].filter((x) => x !== value) : [...state[key], value],
    });
  };

  const realmKeys = Object.keys(facets.realms).sort();
  // Only kinds that actually appear on open tasks are offered — the dashboard
  // lists tasks, so a kind with none would be dead UI here.
  const kindKeys = Object.keys(facets.kinds)
    .filter((k) => (facets.kinds[k]?.open_tasks ?? 0) > 0)
    .sort();

  return (
    <div className="ml-dashfilters">
      <Button
        size="sm"
        active={state.mine}
        onClick={() => onChange({ ...state, mine: !state.mine })}
      >
        <Icon name="flag" /> Mine · needs action
      </Button>

      {Object.keys(facets.assignees)
        .sort()
        .map((a) => (
          <Button
            key={a}
            size="sm"
            active={state.assignees.includes(a)}
            onClick={() => toggle("assignees", a)}
          >
            {a} ({facets.assignees[a]})
          </Button>
        ))}

      {realmKeys.length > 1 ? (
        <>
          <span className="ml-dashfilters__sep" aria-hidden="true">·</span>
          {realmKeys.map((r) => (
            <Button
              key={r}
              size="sm"
              active={state.realms.includes(r)}
              title={`${r} — ${pluralize(facets.realms[r]?.nodes ?? 0, "node")} total, ${pluralize(
                facets.realms[r]?.open_tasks ?? 0,
                "open task",
              )}`}
              onClick={() => toggle("realms", r)}
            >
              {r} ({facets.realms[r]?.open_tasks ?? 0})
            </Button>
          ))}
        </>
      ) : null}

      {kindKeys.length > 1 ? (
        <>
          <span className="ml-dashfilters__sep" aria-hidden="true">·</span>
          {kindKeys.map((k) => (
            <Button
              key={k}
              size="sm"
              active={state.kinds.includes(k)}
              title={`${k} — ${pluralize(facets.kinds[k]?.nodes ?? 0, "node")} total`}
              onClick={() => toggle("kinds", k)}
            >
              {k} ({facets.kinds[k]?.open_tasks})
            </Button>
          ))}
        </>
      ) : null}

      {state.view === "range" ? (
        <>
          <label className="ml-dashfilters__box">
            <span>from</span>
            <TextInput
              type="date"
              value={state.from}
              onChange={(e) => onChange({ ...state, from: e.target.value })}
            />
          </label>
          <label className="ml-dashfilters__box">
            <span>to</span>
            <TextInput
              type="date"
              value={state.to}
              onChange={(e) => onChange({ ...state, to: e.target.value })}
            />
          </label>
          <label className="ml-dashfilters__box">
            <span>status</span>
            <Select
              value={state.status}
              onChange={(e) => onChange({ ...state, status: e.target.value })}
              options={["any", "open", "done"].map((o) => ({ value: o, label: o }))}
            />
          </label>
        </>
      ) : null}

      {state.view === "closed" ? (
        <label className="ml-dashfilters__box">
          <span>since (days)</span>
          <TextInput
            type="number"
            min={1}
            value={state.sinceDays}
            style={{ width: 72 }}
            onChange={(e) => onChange({ ...state, sinceDays: Number(e.target.value) || 7 })}
          />
        </label>
      ) : null}
    </div>
  );
}

export interface ViewTabsProps {
  views: string[];
  value: string;
  onChange: (view: string) => void;
}

export function ViewTabs({ views, value, onChange }: ViewTabsProps) {
  return (
    <Pills
      aria-label="date views"
      value={value}
      onChange={onChange}
      options={views.map((v) => ({ value: v, label: v }))}
    />
  );
}
