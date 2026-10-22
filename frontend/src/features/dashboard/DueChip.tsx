import { useRef, useState } from "react";
import { api } from "@/api/client";
import { useMeta } from "@/api/MetaContext";
import type { TaskRow } from "@/api/types";
import "./DueChip.css";
import { Icon } from "@/components";

export interface DueChipProps {
  row: TaskRow;
  today: string;
  onChanged: () => void;
}

/** The due date, as a chip that turns into a picker when you click it. */
export function DueChip({ row, today, onChanged }: DueChipProps) {
  const { writable } = useMeta();
  const [editing, setEditing] = useState(false);
  const committed = useRef(false);

  const has = !!row.due_local && row.due_local !== "--";
  const current = has ? row.due_local! : "";
  const overdue = has && current < today;

  const commit = async (value: string) => {
    if (committed.current) return;
    committed.current = true;
    setEditing(false);
    // Unchanged means no write at all — a no-op POST would churn `updated` and
    // the mirror sync for nothing.
    if (value === current) return;
    try {
      await api.setDue(row.id, value ? `${value}T12:00:00` : "");
      onChanged();
    } catch {
      onChanged();
    }
  };

  if (editing) {
    return (
      <input
        type="date"
        className="ml-due__picker"
        defaultValue={current}
        autoFocus
        aria-label={`due date for ${row.title}`}
        onChange={(e) => void commit(e.target.value)}
        onBlur={() => setEditing(false)}
      />
    );
  }

  return (
    <button
      type="button"
      className={`ml-due${overdue ? " is-over" : ""}${writable ? " is-editable" : ""}`}
      title={writable ? "click to set/change due date" : undefined}
      disabled={!writable}
      onClick={() => {
        committed.current = false;
        setEditing(true);
      }}
    >
      {has ? <><Icon name="clock" /> {current}</> : <><Icon name="calendar" /> not set</>}
    </button>
  );
}
