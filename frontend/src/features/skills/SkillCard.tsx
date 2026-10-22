import { useState } from "react";
import { api } from "@/api/client";
import { Badge, Checkbox, EmptyState, Icon } from "@/components";
import { Highlight } from "./Highlight";
import { RuleRow } from "./RuleRow";
import type { NodeDetail } from "@/api/types";
import type { KeptSkill } from "./filter";
import "./SkillCard.css";

export interface SkillCardProps {
  skill: KeptSkill;
  query: string;
  selectable: boolean;
  selected: Set<string>;
  onSelect: (id: string, on: boolean) => void;
}

/**
 * One skill's contract: what it reads and writes, what it depends on, the rules
 * it answers to, and its neighbours one level deep.
 */
export function SkillCard({ skill, query, selectable, selected, onSelect }: SkillCardProps) {
  const [open, setOpen] = useState(false);
  const [showAlways, setShowAlways] = useState(false);
  const [relations, setRelations] = useState<NodeDetail | null>(null);
  const [relError, setRelError] = useState<string | null>(null);

  const specific = skill.rules.filter((r) => r.why !== "always");
  const always = skill.rules.filter((r) => r.why === "always");

  const deps: string[] = [];
  if (skill.waits_for.length)
    deps.push(`waits for ${skill.waits_for.map(bare).join(", ")}`);
  if (skill.enriched_by.length)
    deps.push(`enriched by ${skill.enriched_by.map(bare).join(", ")}`);

  const expand = async () => {
    const next = !open;
    setOpen(next);
    if (!next || relations) return;
    try {
      // The same endpoint the dashboard's expander uses, so the neighbours shown
      // here and the neighbours shown there cannot disagree.
      setRelations(await api.node(skill.id));
    } catch (err) {
      setRelError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className={`ml-skill${skill.dim ? " is-dim" : ""}${open ? " is-open" : ""}`}>
      <div className="ml-skill__head">
        {selectable ? (
          <Checkbox
            checked={selected.has(skill.id)}
            onChange={(on) => onSelect(skill.id, on)}
            label={<span className="sr-only">select {skill.title}</span>}
          />
        ) : null}
        <button type="button" className="ml-skill__toggle" aria-expanded={open} onClick={expand}>
          <span className="ml-skill__tw" aria-hidden="true"><Icon name={open ? "chevron-down" : "chevron-right"} /></span>
          <span className="ml-skill__name">
            <Highlight text={skill.title} query={query} />
          </span>
          {skill.uses ? <Badge>used {skill.uses}×</Badge> : null}
          <Badge>{skill.rules.length} rules</Badge>
        </button>
      </div>

      {open ? (
        <div className="ml-skill__body">
          <p className="ml-skill__desc">{(skill.description || skill.triggers || "").slice(0, 400)}</p>

          {skill.reads.length || skill.writes.length || skill.reads_other || skill.writes_other ? (
            <>
              <h4 className="ml-skill__h">Graph surface</h4>
              <div className="ml-skill__chips">
                {skill.reads.map((t) => (
                  <Badge key={`r-${t}`} tone="accent">reads {t}</Badge>
                ))}
                {skill.writes.map((t) => (
                  <Badge key={`w-${t}`} tone="warn">writes {t}</Badge>
                ))}
              </div>
              {skill.reads_other ? <p className="ml-skill__desc">also reads {skill.reads_other}</p> : null}
              {skill.writes_other ? <p className="ml-skill__desc">also writes {skill.writes_other}</p> : null}
            </>
          ) : null}

          {deps.length ? (
            <>
              <h4 className="ml-skill__h">Depends on</h4>
              <div className="ml-skill__chips">
                {deps.map((d) => <Badge key={d}>{d}</Badge>)}
              </div>
            </>
          ) : null}

          <h4 className="ml-skill__h">Rules it answers to</h4>
          {specific.length ? (
            specific.map((r) => (
              <RuleRow
                key={r.id}
                id={r.id}
                title={r.title}
                why={r.why}
                nonNegotiable={r.non_negotiable}
                query={query}
              />
            ))
          ) : (
            <p className="ml-skill__empty">Nothing specific to this skill.</p>
          )}

          {always.length ? (
            <>
              <button
                type="button"
                className="ml-skill__more"
                aria-expanded={showAlways}
                onClick={() => setShowAlways((v) => !v)}
              >
                {showAlways ? "hide" : "show"} {always.length} always-on rules
              </button>
              {showAlways
                ? always.map((r) => (
                    <RuleRow
                      key={r.id}
                      id={r.id}
                      title={r.title}
                      why={r.why}
                      nonNegotiable={r.non_negotiable}
                      query={query}
                    />
                  ))
                : null}
            </>
          ) : null}

          {/* One level, never two. The point of showing a skill's neighbours is
              to answer "what else is this wired to"; a second hop answers "what
              is the graph", which is what the map is for. */}
          <h4 className="ml-skill__h">In the graph</h4>
          <Relations detail={relations} error={relError} />
        </div>
      ) : null}
    </div>
  );
}

function bare(id: string): string {
  return id.replace(/^skill_/, "");
}

function Relations({ detail, error }: { detail: NodeDetail | null; error: string | null }) {
  if (error) return <p className="ml-skill__empty">Could not read its relations: {error}</p>;
  if (!detail) return <p className="ml-skill__empty">Loading relations…</p>;
  const edges = detail.edges ?? [];
  if (!edges.length)
    return <p className="ml-skill__empty">Nothing points at it and it points at nothing.</p>;

  const out = edges.filter((e) => e.dir === "out");
  const inn = edges.filter((e) => e.dir === "in");

  const block = (label: string, list: typeof edges, arrow: "arrow-right" | "arrow-left") =>
    list.length ? (
      <div key={label}>
        <div className="ml-skill__relgrp">{label}</div>
        {list
          .slice()
          .sort((a, b) => a.rel.localeCompare(b.rel))
          .map((e, i) => (
            <div className="ml-skill__relln" key={`${label}-${e.id}-${i}`}>
              <span className="ml-skill__ar"><Icon name={arrow} /></span>
              <span className="ml-skill__rl">{e.rel}</span>
              <span className="ml-skill__tt">{e.title || e.id}</span>
              <span className="ml-skill__ty">{e.type || ""}</span>
            </div>
          ))}
      </div>
    ) : null;

  return (
    <>
      {block("Points at", out, "arrow-right")}
      {block("Pointed at by", inn, "arrow-left")}
    </>
  );
}

export function NoSkills() {
  return (
    <EmptyState
      title="No skills in this tree yet."
      hint={<>Run <code>graph.py sync-skill-tree</code> to build it from the declarations.</>}
    />
  );
}
