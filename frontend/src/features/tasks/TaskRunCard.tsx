import { useState } from "react";
import { api } from "@/api/client";
import { usePolling } from "@/hooks/usePolling";
import { Badge, Button, Spinner, StatusMessage, Icon } from "@/components";
import { formatDate } from "@/lib/format";
import type { ActivityEvent, TaskActivity, TaskDetail, TriggerTask, TranscriptEvent } from "@/api/types";
import { ACTIVE, PERSON_SOURCES, REPROCESSABLE, ago, duration, requesterOf } from "./filter";

export interface TaskRunCardProps {
  task: TriggerTask;
  open: boolean;
  onToggle: () => void;
  /** The server's clock on the last poll, so "running for" does not drift. */
  now: number;
  /** Called after the card changed the task (a reprocess), to re-read the list. */
  onChanged?: () => void;
}

/**
 * One trigger task. Collapsed it says who asked, from where, what it is and
 * how it is going; open it is the whole run — the trigger, the command, the
 * output, what it logged and every tool call the session made.
 *
 * The detail is read only while the card is open, and re-read on the panel's
 * beat only while the task is still moving. A finished task is read once more
 * when it finishes, then left alone.
 */
export function TaskRunCard({ task, open, onToggle, now, onChanged }: TaskRunCardProps) {
  const active = ACTIVE.has(task.status);
  const took = duration(task.started_at, task.finished_at || undefined, now);
  const p = task.progress;

  return (
    <div className={`ml-run ml-run--${task.status}${open ? " is-open" : ""}`} role="listitem">
      <button
        type="button"
        className="ml-run__head"
        aria-expanded={open}
        aria-label={`${task.name} from ${requesterOf(task)}, ${task.status}`}
        onClick={onToggle}
      >
        <span className="ml-run__line">
          <span className={`ml-run__status ml-run__status--${task.status}`}>
            {task.status === "running" ? <span className="ml-run__pulse" aria-hidden="true" /> : null}
            {task.status}
          </span>
          <span className="ml-run__name">{task.name}</span>
          <Badge>{task.source}</Badge>
          {task.action !== "claude" ? <Badge tone="accent">{task.action}</Badge> : null}
          <span className="ml-run__who">
            {requesterOf(task)}
            {task.is_owner ? <span className="ml-run__owner"> (owner)</span> : null}
          </span>
          <span className="ml-run__meta">
            {took ? <span title="duration">{active ? `running ${took}` : took}</span> : null}
            {task.cost_usd != null ? (
              <span title={task.cost_estimated ? "estimated cost, from the session's token usage at list price" : "cost"}>
                {task.cost_estimated ? "~" : ""}${task.cost_usd.toFixed(2)}
              </span>
            ) : null}
            <span className="ml-run__when">
              <time dateTime={task.received_at}>{formatDate(task.received_at)}</time>
              <span className="ml-run__dim"> · {ago(task.received_at, now)}</span>
            </span>
            <span className="ml-run__chevron" aria-hidden="true"><Icon name={open ? "chevron-down" : "chevron-right"} /></span>
          </span>
        </span>
        {task.text ? <span className="ml-run__text">{task.text}</span> : null}
        {p ? (
          <>
            <span className="ml-run__progress">
              {p.tool_calls} tool call{p.tool_calls === 1 ? "" : "s"}
              {p.subagents ? ` · ${p.subagents_running}/${p.subagents} subagents running` : ""}
              {p.actions ? ` · ${p.actions} action${p.actions === 1 ? "" : "s"}` : ""}
              {p.last_action ? ` (last: ${p.last_action.kind})` : ""}
              {p.last_at ? ` · active ${ago(p.last_at, now)}` : ""}
              {p.last_tool ? (
                <>
                  {" "}· now <b>{p.last_tool}</b>{" "}
                  <span className="ml-run__progress-in">{oneLine(p.last_input)}</span>
                </>
              ) : null}
            </span>
            {p.last_message ? <span className="ml-run__result">{oneLine(p.last_message)}</span> : null}
          </>
        ) : null}
        {!open && !active && task.result ? (
          <span className="ml-run__result">{oneLine(task.result)}</span>
        ) : null}
      </button>
      {REPROCESSABLE.has(task.status) ? <Reprocess task={task} onDone={onChanged} /> : null}
      {active ? <CancelTask task={task} onDone={onChanged} /> : null}
      {open ? <TaskRunDetail id={task.id} status={task.status} /> : null}
    </div>
  );
}

/** Stop a queued or running task. A running one asks first: its session is
 * killed, and what it did so far stays done. Reprocess runs it again. */
function CancelTask({ task, onDone }: { task: TriggerTask; onDone?: () => void }) {
  const [busy, setBusy] = useState(false);
  const [sure, setSure] = useState(false);
  const [err, setErr] = useState("");

  const go = async () => {
    setBusy(true);
    setErr("");
    try {
      await api.cancelTask(task.id);
      setSure(false);
      onDone?.();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="ml-run__actions">
      {sure ? (
        <>
          <span className="ml-run__dim">Stop this task? Its session is killed; what it already did stays done.</span>
          <Button size="sm" variant="danger" disabled={busy} onClick={go}>
            {busy ? "Cancelling…" : "Stop it"}
          </Button>
          <Button size="sm" disabled={busy} onClick={() => setSure(false)}>Keep running</Button>
        </>
      ) : (
        <Button
          size="sm"
          disabled={busy}
          onClick={task.status === "running" ? () => setSure(true) : go}
          aria-label={`Cancel ${task.name}`}
        >
          {busy ? "Cancelling…" : "Cancel"}
        </Button>
      )}
      {err ? <StatusMessage tone="error">{err}</StatusMessage> : null}
    </div>
  );
}

/** Run a blocked, refused or failed task again, with the action it was
 * stamped with. A blocked one is a B2B message from someone off the allowed
 * list, a refused one a mention in a conversation nobody opened: running
 * either is the owner vouching for that one request, so it asks first.
 *
 * A refused Jira mention also offers to let its author in for good, so their
 * next one runs on any issue without this. */
function Reprocess({ task, onDone }: { task: TriggerTask; onDone?: () => void }) {
  const [busy, setBusy] = useState(false);
  const [sure, setSure] = useState(false);
  const [err, setErr] = useState("");
  const [allowed, setAllowed] = useState(false);
  const blocked = task.status === "blocked" || task.status === "refused";
  const canAllow = task.status === "refused" && !task.is_owner && !!task.requester_id
    && PERSON_SOURCES.has(task.source);

  const allow = async () => {
    setBusy(true);
    setErr("");
    try {
      await api.setTriggerPerson({
        source: task.source,
        user_id: task.requester_id,
        enabled: true,
        ...(task.requester_name ? { name: task.requester_name } : {}),
      });
      setAllowed(true);
      onDone?.();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const go = async () => {
    setBusy(true);
    setErr("");
    try {
      await api.reprocessTask(task.id);
      setSure(false);
      onDone?.();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="ml-run__actions">
      {blocked && sure ? (
        <>
          <span className="ml-run__dim">
            {task.status === "refused"
              ? `Run this mention from ${requesterOf(task)}, in a conversation nobody opened?`
              : `Run this message from ${requesterOf(task)}, who is not on the allowed list?`}
          </span>
          <Button size="sm" variant="primary" disabled={busy} onClick={go}>Run it</Button>
          <Button size="sm" disabled={busy} onClick={() => setSure(false)}>Cancel</Button>
        </>
      ) : (
        <Button
          size="sm"
          disabled={busy}
          onClick={blocked ? () => setSure(true) : go}
          aria-label={`Reprocess ${task.name}`}
        >
          {busy ? "Reprocessing…" : "Reprocess"}
        </Button>
      )}
      {canAllow && !sure ? (
        allowed ? (
          <span className="ml-run__dim">{requesterOf(task)} may now tag the brain on any issue.</span>
        ) : (
          <Button size="sm" disabled={busy} onClick={allow} aria-label={`Allow ${requesterOf(task)} to tag the brain`}>
            Allow {requesterOf(task)} to tag the brain
          </Button>
        )
      ) : null}
      {err ? <StatusMessage tone="error">{err}</StatusMessage> : null}
    </div>
  );
}

function TaskRunDetail({ id, status }: { id: string; status: string }) {
  const active = ACTIVE.has(status);
  const { data, error } = usePolling<TaskDetail>((signal) => api.task(id, signal), {
    intervalMs: active ? 5000 : 3_600_000,
    // A status change re-reads at once: the finished run has its output.
    key: `${id}:${status}`,
  });

  if (error && !data) return <div className="ml-run__body"><StatusMessage tone="error">{error}</StatusMessage></div>;
  if (!data) return <div className="ml-run__body"><Spinner label="reading the task" /></div>;

  const { task, actions, transcript } = data;
  const argv = Array.isArray(task.command) ? task.command : null;
  const promptAt = argv ? argv.indexOf("-p") : -1;
  const prompt = promptAt >= 0 ? argv![promptAt + 1] : "";
  const shown = argv
    ? argv.map((a, i) => (promptAt >= 0 && i === promptAt + 1 ? "<prompt>" : shellQuote(a))).join(" ")
    : typeof task.command === "string"
      ? task.command
      : "";

  return (
    <div className="ml-run__body">
      <Block title="Trigger">
        <dl className="ml-run__kv">
          <dt>source</dt>
          <dd>{task.source} · {task.kind}</dd>
          <dt>name</dt>
          <dd>{task.name}</dd>
          <dt>who</dt>
          <dd>
            {requesterOf(task)} <span className="ml-run__dim">{task.requester_id}</span>
            {task.is_owner ? " (owner)" : ""}
          </dd>
          <dt>where</dt>
          <dd className="ml-run__mono">{task.scope}</dd>
          {task.permalink ? (
            <>
              <dt>link</dt>
              <dd>
                <a href={task.permalink} target="_blank" rel="noreferrer">{task.permalink}</a>
              </dd>
            </>
          ) : null}
          <dt>received</dt>
          <dd>{formatDate(task.received_at)}</dd>
          {task.started_at ? (
            <>
              <dt>ran</dt>
              <dd>
                {formatDate(task.started_at)}
                {task.finished_at ? ` → ${formatDate(task.finished_at)}` : ""}
                {` (${duration(task.started_at, task.finished_at || undefined)})`}
              </dd>
            </>
          ) : null}
          {task.reason ? (
            <>
              <dt>reason</dt>
              <dd>{task.reason}</dd>
            </>
          ) : null}
          <dt>request</dt>
          <dd className="ml-run__mono">{task.id}</dd>
        </dl>
        <h5 className="ml-run__sub">Message</h5>
        <pre className="ml-run__pre">{task.text || "(empty)"}</pre>
        <details className="ml-run__details">
          <summary>trigger metadata</summary>
          <pre className="ml-run__pre">{JSON.stringify(task.meta, null, 2)}</pre>
        </details>
      </Block>

      <Block title="Command">
        {shown ? (
          <>
            <pre className="ml-run__pre ml-run__mono">{shown}</pre>
            <dl className="ml-run__kv">
              {task.cwd ? (<><dt>cwd</dt><dd className="ml-run__mono">{task.cwd}</dd></>) : null}
              {task.session_id ? (<><dt>session</dt><dd className="ml-run__mono">{task.session_id}</dd></>) : null}
              {task.exit_code != null ? (<><dt>exit</dt><dd>{task.exit_code}</dd></>) : null}
            </dl>
            {prompt ? (
              <details className="ml-run__details">
                <summary>prompt ({prompt.length} chars)</summary>
                <pre className="ml-run__pre">{prompt}</pre>
              </details>
            ) : null}
          </>
        ) : (
          <p className="ml-run__dim">
            {task.started_at
              ? "Not recorded: this task ran before commands were kept."
              : "It has not run."}
            {task.session_id ? ` Session ${task.session_id}.` : ""}
          </p>
        )}
      </Block>

      <Block title="Output">
        {task.result ? <pre className="ml-run__pre">{task.result}</pre> : <p className="ml-run__dim">{ACTIVE.has(task.status) ? "Still running." : "No output."}</p>}
        {task.stderr ? (
          <details className="ml-run__details">
            <summary>stderr</summary>
            <pre className="ml-run__pre">{task.stderr}</pre>
          </details>
        ) : null}
      </Block>

      <Block title="Actions" count={actions.length}>
        {actions.length ? (
          <ul className="ml-run__actions">
            {actions.map((a) => (
              <li key={a.id}>
                <span className="ml-run__dim">{formatDate(a.at)}</span> <b>{a.kind}</b>
                {a.target ? (
                  /^https?:\/\//.test(a.target) ? (
                    <> <a href={a.target} target="_blank" rel="noreferrer">{a.target}</a></>
                  ) : (
                    <> <span className="ml-run__mono">{a.target}</span></>
                  )
                ) : null}
                {a.detail ? <div className="ml-run__dim">{a.detail}</div> : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="ml-run__dim">Nothing logged.</p>
        )}
      </Block>

      <Block title="Live activity" count={data.activity?.timeline.length} countLabel="events from the database">
        {data.activity ? <Activity activity={data.activity} /> : (
          <p className="ml-run__dim">Nothing written to the database for this task yet.</p>
        )}
      </Block>

      <Block title="Session" count={transcript.tool_calls} countLabel="tool calls">
        {transcript.found ? (
          <>
            {transcript.truncated ? (
              <p className="ml-run__dim">
                The newest {transcript.events.length} of {transcript.of} events.
              </p>
            ) : null}
            <ol className="ml-run__events" aria-label="session">
              {transcript.events.map((e, i) => (
                <EventRow key={i} event={e} />
              ))}
            </ol>
            <p className="ml-run__dim ml-run__mono">{transcript.path}</p>
          </>
        ) : (
          <p className="ml-run__dim">
            {task.action !== "claude"
              ? `A ${task.action} action has no model session.`
              : task.started_at
                ? "No transcript found for this session."
                : "No session yet."}
          </p>
        )}
      </Block>
    </div>
  );
}

/** The hooks' view of the run: agents and a timeline, straight from the database. */
function Activity({ activity }: { activity: TaskActivity }) {
  const subs = activity.agents.filter((a) => a.role !== "main");
  return (
    <>
      <dl className="ml-run__kv">
        <dt>session</dt>
        <dd>
          {activity.conversation ? activity.conversation.status : "not captured"}
          {activity.updated ? <span className="ml-run__dim"> · last write {formatDate(activity.updated)}</span> : null}
        </dd>
        <dt>agents</dt>
        <dd>
          {activity.agents.length
            ? `${activity.agents.length} (${subs.length} subagent${subs.length === 1 ? "" : "s"}, ${activity.subagents_running} running)`
            : "none yet"}
        </dd>
      </dl>
      {subs.length ? (
        <ul className="ml-run__agents" aria-label="subagents">
          {subs.map((a) => (
            <li key={a.id} className={`ml-agent ml-agent--${a.status === "done" ? a.outcome || "done" : "running"}`}>
              <b>{a.role}</b>{" "}
              <span className="ml-run__dim">
                {a.status === "done" ? a.outcome || "done" : "running"}
                {` · ${duration(a.started_at, a.ended_at || undefined)}`}
              </span>
              {a.brief ? <div className="ml-run__dim">{a.brief}</div> : null}
            </li>
          ))}
        </ul>
      ) : null}
      <ol className="ml-run__timeline" aria-label="activity">
        {activity.timeline.map((e, i) => (
          <ActivityRow key={i} event={e} />
        ))}
      </ol>
    </>
  );
}

function ActivityRow({ event }: { event: ActivityEvent }) {
  const long = event.kind === "message" && (event.detail?.length ?? 0) > 160;
  return (
    <li className={`ml-act ml-act--${event.kind}`}>
      <span className="ml-act__at" title={formatDate(event.at)}>{clock(event.at)}</span>
      <span className="ml-act__title">{event.title}</span>
      {event.target ? (
        /^https?:\/\//.test(event.target) ? (
          <a href={event.target} target="_blank" rel="noreferrer" className="ml-act__target">{event.target}</a>
        ) : (
          <span className="ml-run__mono ml-act__target">{event.target}</span>
        )
      ) : null}
      {event.detail ? (
        long ? (
          <details className="ml-run__details ml-act__detail">
            <summary>{oneLine(event.detail).slice(0, 160)}</summary>
            <pre className="ml-run__pre">{event.detail}</pre>
          </details>
        ) : (
          <div className="ml-run__dim ml-act__detail">{event.detail}</div>
        )
      ) : null}
    </li>
  );
}

function EventRow({ event }: { event: TranscriptEvent }) {
  const [openIt, setOpen] = useState(false);
  if (event.kind === "text") {
    return (
      <li className={`ml-ev ml-ev--${event.role}`}>
        <span className="ml-ev__role">{event.role === "assistant" ? "said" : event.role}</span>
        {event.role === "assistant" ? (
          <div className="ml-ev__text">{event.text}</div>
        ) : (
          <details className="ml-run__details">
            <summary>{oneLine(event.text).slice(0, 120)}</summary>
            <pre className="ml-run__pre">{event.text}</pre>
          </details>
        )}
      </li>
    );
  }
  const pending = event.result === null;
  return (
    <li className={`ml-ev ml-ev--tool${event.is_error ? " is-error" : ""}${pending ? " is-pending" : ""}`}>
      <button type="button" className="ml-ev__tool" aria-expanded={openIt} onClick={() => setOpen(!openIt)}>
        <span className="ml-ev__chev" aria-hidden="true"><Icon name={openIt ? "chevron-down" : "chevron-right"} /></span>
        <b>{event.name}</b>
        <span className="ml-ev__in">{oneLine(event.input)}</span>
        <span className="ml-ev__state">
          {pending ? "running" : event.is_error ? "error" : ""}
          {event.done_at ? ` ${duration(event.at, event.done_at)}` : ""}
        </span>
      </button>
      {openIt ? (
        <div className="ml-ev__io">
          <h6>input</h6>
          <pre className="ml-run__pre">{event.input}</pre>
          <h6>result</h6>
          <pre className="ml-run__pre">{pending ? "(waiting)" : event.result || "(empty)"}</pre>
        </div>
      ) : null}
    </li>
  );
}

function Block({ title, count, countLabel, children }: { title: string; count?: number; countLabel?: string; children: React.ReactNode }) {
  return (
    <section className="ml-run__block">
      <h4 className="ml-run__h">
        {title}
        {count != null ? <span className="ml-run__count" title={countLabel}>{count}</span> : null}
      </h4>
      {children}
    </section>
  );
}

/** 14:02:35 — to the second, since this is a live log. */
function clock(iso: string): string {
  const d = new Date(iso);
  return iso && !Number.isNaN(d.getTime()) ? d.toLocaleTimeString(undefined, { hour12: false }) : "";
}

function oneLine(s: string): string {
  return s.replace(/\s+/g, " ").trim();
}

function shellQuote(a: string): string {
  return /^[\w@%+=:,./-]+$/.test(a) ? a : `'${a.replace(/'/g, `'\\''`)}'`;
}
