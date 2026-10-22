import { useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import { Checkbox, StatusMessage, Icon } from "@/components";
import { truncate } from "@/lib/format";
import type { AgentCommand, AgentCommands } from "@/api/types";

/**
 * Fuzzy score of a query against a command: 0 is no match. A name that starts
 * with it beats one that contains it, which beats its letters in order, which
 * beats a match in the description.
 */
export function score(c: AgentCommand, query: string): number {
  const q = query.toLowerCase();
  if (!q) return 1;
  const name = c.name.toLowerCase();
  const bare = name.slice(name.indexOf(":") + 1);
  if (name.startsWith(q) || bare.startsWith(q)) return 4;
  if (name.includes(q)) return 3;
  let i = 0;
  for (const ch of name) if (ch === q[i]) i += 1;
  if (i === q.length) return 2;
  return c.description.toLowerCase().includes(q) ? 1 : 0;
}

export function search(commands: AgentCommand[], query: string): AgentCommand[] {
  return commands
    .map((c) => [c, score(c, query)] as const)
    .filter(([, s]) => s > 0)
    .sort((a, b) => b[1] - a[1] || a[0].name.localeCompare(b[0].name))
    .map(([c]) => c);
}

/** The picker is open while the message is `/` and a name being typed. */
export function slashQuery(text: string): string | null {
  return /^\/[^\s]*$/.test(text) ? text.slice(1) : null;
}

/** Skills and plugin commands, read from disk once the first `/` is typed. */
export function useCommands(wanted: boolean) {
  const [data, setData] = useState<AgentCommands | null>(null);
  const [error, setError] = useState<string | null>(null);
  const asked = useRef(false);
  useEffect(() => {
    if (!wanted || asked.current) return;
    asked.current = true;
    api.agentCommands().then(setData, (e: Error) => {
      asked.current = false;
      setError(e.message);
    });
  }, [wanted]);
  return { data, setData, error };
}

/**
 * The popup above the message box. Arrow keys move, Enter or Tab picks, Esc
 * closes. A command that writes is greyed out while the conversation is
 * read-only: its tools would be refused.
 */
export function CommandPopup({
  items,
  active,
  readOnly,
  onPick,
  onHover,
}: {
  items: AgentCommand[];
  active: number;
  readOnly: boolean;
  onPick: (c: AgentCommand) => void;
  onHover: (i: number) => void;
}) {
  const list = useRef<HTMLUListElement>(null);
  useEffect(() => {
    list.current?.querySelector(".is-active")?.scrollIntoView?.({ block: "nearest" });
  }, [active]);
  return (
    <ul className="ml-slash" role="listbox" aria-label="slash commands" ref={list}>
      {items.length ? (
        items.map((c, i) => {
          const blocked = readOnly && c.writes;
          return (
            <li
              key={c.name}
              role="option"
              aria-selected={i === active}
              aria-disabled={blocked}
              className={`ml-slash__item${i === active ? " is-active" : ""}${blocked ? " is-blocked" : ""}`}
              title={blocked ? "Writes things: untick Read-only to use it" : c.description}
              onPointerEnter={() => onHover(i)}
              onPointerDown={(e) => {
                e.preventDefault();
                if (!blocked) onPick(c);
              }}
            >
              <b>/{c.name}</b>
              <span className="ml-slash__kind">{c.kind}</span>
              <span className="ml-slash__desc">{truncate(c.description, 90)}</span>
            </li>
          );
        })
      ) : (
        <li className="ml-slash__none">No skill or command matches.</li>
      )}
    </ul>
  );
}

/** Installed plugins, each switched on or off in ~/.claude/settings.json. */
export function PluginsMenu({
  plugins,
  disabled,
  onChange,
}: {
  plugins: AgentCommands["plugins"];
  disabled: boolean;
  onChange: (d: AgentCommands) => void;
}) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const toggle = async (key: string, enabled: boolean) => {
    setError(null);
    try {
      onChange(await api.setAgentPlugin(key, enabled));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="ml-pick" ref={root}>
      <button type="button" className="ml-pick__btn" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        Plugins
        <span className="ml-pick__model">{plugins.filter((p) => p.enabled).length} on</span>
        <span className="ml-pick__caret" aria-hidden="true"><Icon name="chevron-up" /></span>
      </button>
      {open ? (
        <div className="ml-pick__menu ml-plugins" role="dialog" aria-label="plugins">
          <p className="ml-plugins__note">Applies to every Claude session, from the next message on.</p>
          {error ? <StatusMessage tone="error">{error}</StatusMessage> : null}
          {plugins.map((p) => (
            <Checkbox
              key={p.key}
              checked={p.enabled}
              disabled={disabled}
              label={`${p.name} ${p.version}`}
              count={p.commands}
              title={p.key}
              onChange={(on) => void toggle(p.key, on)}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}
