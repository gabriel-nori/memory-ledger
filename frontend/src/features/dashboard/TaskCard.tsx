import { useState } from "react";
import { api } from "@/api/client";
import { useMeta } from "@/api/MetaContext";
import { useAsync } from "@/hooks/useAsync";
import { Badge, Button, Card, Select, StatusMessage, TextArea, TextInput, Icon } from "@/components";
import { RelationEditor } from "./RelationEditor";
import { DueChip } from "./DueChip";
import type { NodeDetail, TaskRow } from "@/api/types";
import "./TaskCard.css";

export interface TaskCardProps {
  row: TaskRow;
  today: string;
  /** Re-runs the view — a completed task leaves an "open" list. */
  onChanged: () => void;
  /** Re-reads the chips, so a new assignee or realm becomes filterable at once. */
  onFacetsChanged: () => void;
}

/** One row of a date view: the chips you read, and everything you can do to it. */
export function TaskCard({ row, today, onChanged, onFacetsChanged }: TaskCardProps) {
  const [open, setOpen] = useState(false);

  return (
    <Card className="ml-task">
      <button
        type="button"
        className="ml-task__title"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <Icon name={open ? "chevron-down" : "chevron-right"} />{" "}
        {row.title || "(untitled)"}
      </button>

      <div className="ml-task__chips">
        <DueChip row={row} today={today} onChanged={onChanged} />
        {row.type ? <Badge>{row.type}</Badge> : null}
        {row.status ? <Badge tone={toneForStatus(row.status)}>{row.status}</Badge> : null}
        {row.assignee ? <Badge><Icon name="user" /> {row.assignee}</Badge> : null}
        {row.realm ? <Badge>{row.realm}</Badge> : null}
        {row.kind && row.kind !== "(none)" ? <Badge>{row.kind}</Badge> : null}
        {row.delegated ? <Badge><Icon name="forward" /> {row.delegated}</Badge> : null}
        {row.blocked_by?.length ? (
          <Badge tone="danger"><Icon name="lock" /> blocked ×{row.blocked_by.length}</Badge>
        ) : null}
        {row.blocks?.length ? <Badge tone="warn"><Icon name="blocks" /> blocks ×{row.blocks.length}</Badge> : null}
        {row.related?.length ? <Badge><Icon name="link" /> {row.related.length}</Badge> : null}
      </div>

      {row.waiting_on ? (
        <p className="ml-task__waiting">
          waiting on: <b>{row.waiting_on}</b>
        </p>
      ) : null}

      {open ? (
        <TaskDetail id={row.id} onChanged={onChanged} onFacetsChanged={onFacetsChanged} />
      ) : null}
    </Card>
  );
}

function toneForStatus(status: string) {
  if (status === "done") return "success" as const;
  if (status === "blocked") return "danger" as const;
  if (status === "waiting") return "warn" as const;
  return "neutral" as const;
}

interface TaskDetailProps {
  id: string;
  onChanged: () => void;
  onFacetsChanged: () => void;
}

/** The expanded card: complete, status, assignee, rename, and the relations. */
function TaskDetail({ id, onChanged, onFacetsChanged }: TaskDetailProps) {
  const { meta, writable } = useMeta();
  const detail = useAsync<NodeDetail>((signal) => api.node(id, signal), [id]);
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (detail.loading) return <p className="ml-task__loading">loading…</p>;
  if (detail.error) return <StatusMessage tone="error">{detail.error}</StatusMessage>;
  const d = detail.data;
  if (!d) return null;

  const refresh = () => {
    detail.reload();
    onChanged();
  };

  const run = async (fn: () => Promise<unknown>, alsoFacets = false) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      refresh();
      if (alsoFacets) onFacetsChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const status = d.status || "";
  const done = status === "done" || status === "cancelled";
  const statuses = meta?.statuses ?? [];
  // An unrecognised stored status is real data — offer it rather than
  // silently re-pointing the select at the first legal value.
  const statusOptions = (statuses.includes(status) ? statuses : [status || "—", ...statuses]).map(
    (s) => ({ value: s, label: s }),
  );

  return (
    <div className="ml-task__detail">
      {d.body ? <div className="ml-task__body">{d.body}</div> : null}

      {writable && !editing ? (
        <div className="ml-task__actions">
          {!done ? (
            <Button size="sm" disabled={busy} onClick={() => run(() => api.complete(d.id))}>
              <Icon name="check" /> Complete
            </Button>
          ) : null}
          <Select
            value={status}
            aria-label="status"
            disabled={busy}
            options={statusOptions}
            onChange={(e) => {
              // Never write a value the person did not change: a change handler
              // on a pre-filled control fires on a bare focus+blur, and a no-op
              // write would churn `updated` and the mirror sync for nothing.
              if (e.target.value === status) return;
              void run(() => api.setStatus(d.id, e.target.value));
            }}
          />
          <TextInput
            defaultValue={String((d.props as Record<string, string>)?.assignee ?? "")}
            placeholder="assignee"
            aria-label="assignee"
            disabled={busy}
            onBlur={(e) => {
              const was = String((d.props as Record<string, string>)?.assignee ?? "");
              if (e.target.value.trim() === was.trim()) return;
              void run(() => api.setAssignee(d.id, e.target.value.trim()), true);
            }}
          />
          <Button size="sm" disabled={busy} onClick={() => setEditing(true)}><Icon name="pencil" /> Edit</Button>
        </div>
      ) : null}

      {editing ? (
        <InlineEditor
          detail={d}
          onCancel={() => setEditing(false)}
          onSaved={() => {
            setEditing(false);
            refresh();
          }}
        />
      ) : null}

      {error ? <StatusMessage tone="error">{error}</StatusMessage> : null}

      <RelationEditor node={d} onChanged={refresh} />
    </div>
  );
}

function InlineEditor({
  detail,
  onCancel,
  onSaved,
}: {
  detail: NodeDetail;
  onCancel: () => void;
  onSaved: () => void;
}) {
  const [title, setTitle] = useState(detail.title || "");
  const [body, setBody] = useState(detail.body || "");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const save = async () => {
    if (!title.trim()) {
      setError("title cannot be empty");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await api.updateNode({ id: detail.id, title: title.trim(), body });
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setSaving(false);
    }
  };

  return (
    <div className="ml-task__editor">
      <TextInput
        value={title}
        autoFocus
        aria-label="title"
        onChange={(e) => setTitle(e.target.value)}
      />
      <TextArea value={body} aria-label="details" onChange={(e) => setBody(e.target.value)} />
      {error ? <StatusMessage tone="error">{error}</StatusMessage> : null}
      <div className="ml-task__editor-foot">
        <Button variant="primary" size="sm" disabled={saving} onClick={save}>
          {saving ? "saving…" : "Save"}
        </Button>
        <Button size="sm" disabled={saving} onClick={onCancel}>Cancel</Button>
      </div>
    </div>
  );
}
