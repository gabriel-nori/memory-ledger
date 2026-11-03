import { useEffect, useState } from "react";
import { api } from "@/api/client";
import { useAsync } from "@/hooks/useAsync";
import { Badge, Button, Card, Checkbox, EmptyState, Field, Spinner, StatusMessage, TextArea, TextInput } from "@/components";
import type { InboundHttpDetail, InboundHttpTrigger, InboundHttpTriggersPayload, InboundHttpVerdict } from "@/api/types";
import "./SettingsSection.css";

const ID_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;

type Sure = { id: string; action: "regenerate" | "revoke" | "delete" };
type Note = { text: string; tone: "success" | "error" | "info" };

const SURE_TEXT: Record<Sure["action"], string> = {
  regenerate: "The old token dies now.",
  revoke: "Its caller gets 401 until you regenerate.",
  delete: "The file and the token go. The call log stays.",
};

/**
 * Inbound HTTP: the brain's HTTP door (docs/trigger.md#inbound-http-triggers). Each way in is one YAML
 * file with its own port, allowed IPs and CIDRs, token and throttle. This card lists them, switches them
 * on and off, edits the file, and mints, revokes or deletes. A token is shown once, here, and never read back.
 */
export function InboundHttpCard({ writable }: { writable: boolean }) {
  const list = useAsync<InboundHttpTriggersPayload>((signal) => api.inboundHttpTriggers(signal), []);
  const [open, setOpen] = useState<string | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [newId, setNewId] = useState("");
  const [busy, setBusy] = useState("");
  const [sure, setSure] = useState<Sure | null>(null);
  const [note, setNote] = useState<Note | null>(null);

  if (list.loading && !list.data) return <Spinner label="reading inbound HTTP triggers" />;
  if (list.error) return <StatusMessage tone="error">{list.error}</StatusMessage>;
  if (!list.data) return null;
  const data = list.data;

  const guard = async (key: string, fn: () => Promise<void>) => {
    setBusy(key);
    setNote(null);
    try {
      await fn();
    } catch (err) {
      setNote({ text: err instanceof Error ? err.message : String(err), tone: "error" });
    } finally {
      setBusy("");
    }
  };

  const toggle = (id: string, enabled: boolean) =>
    guard(id, async () => {
      await api.enableInboundHttpTrigger(id, enabled);
      setNote({ text: `${id} is ${enabled ? "on" : "off"}.`, tone: "success" });
      list.reload();
    });

  const listen = (enabled: boolean) =>
    guard("listen", async () => {
      await api.saveTrigger({ inbound_http: { enabled } });
      list.reload();
    });

  const act = (s: Sure) =>
    guard(s.id, async () => {
      const res = await api.inboundHttpAction(s);
      if (!res.ok) throw new Error(res.error || "refused");
      setSure(null);
      if (s.action === "delete" && open === s.id) setOpen(null);
      setNote(
        res.token
          ? { text: `New token for ${s.id} (shown once): ${res.token}`, tone: "info" }
          : { text: `${s.id}: ${s.action} done.`, tone: "success" },
      );
      list.reload();
    });

  const startNew = () => {
    if (!ID_RE.test(newId)) {
      setNote({ text: "An id is letters, digits, - and _.", tone: "error" });
      return;
    }
    if (data.triggers.some((t) => t.id === newId)) {
      setNote({ text: `${newId} exists already.`, tone: "error" });
      return;
    }
    setNote(null);
    setIsNew(true);
    setOpen(newId);
  };

  return (
    <Card>
      <h2 className="ml-settings__h">Inbound HTTP</h2>
      <p className="ml-settings__hint">
        The brain&apos;s HTTP door for your portal. Each way in is one file in <code>{data.dir}</code> with its own port (
        {data.port_range[0]}-{data.port_range[1]}), allowed IPs and CIDRs, token and throttle.{" "}
        {data.active ? (
          <Badge tone="success">listening here</Badge>
        ) : (
          <Badge tone="warn">not listening: {data.master ? "switch it on below" : "switch on triggers in trigger.json"}</Badge>
        )}
      </p>
      <Checkbox
        checked={data.enabled}
        onChange={(v) => void listen(v)}
        label="Open the listeners of the ways in that are on"
        disabled={!writable || busy === "listen"}
      />

      {data.triggers.length === 0 ? (
        <EmptyState title="No inbound HTTP triggers yet" hint="Start one below. Its token is shown once." />
      ) : (
        <table className="ml-settings__table ml-custom__table">
          <thead>
            <tr>
              <th>On</th>
              <th>Way in</th>
              <th>Port</th>
              <th>Allowed IPs</th>
              <th>Throttle</th>
              <th>Token</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {data.triggers.map((t) => (
              <Row
                key={t.id}
                t={t}
                writable={writable}
                busy={busy === t.id}
                opened={open === t.id}
                sure={sure?.id === t.id ? sure.action : null}
                onToggle={(v) => void toggle(t.id, v)}
                onEdit={() => {
                  setIsNew(false);
                  setOpen(open === t.id ? null : t.id);
                }}
                onAsk={(action) => setSure({ id: t.id, action })}
                onCancel={() => setSure(null)}
                onConfirm={() => sure && void act(sure)}
              />
            ))}
          </tbody>
        </table>
      )}

      {writable ? (
        <div className="ml-settings__foot">
          <Field label="New way in, id">
            {(id) => <TextInput id={id} value={newId} placeholder="site-chat" onChange={(e) => setNewId(e.target.value.trim())} />}
          </Field>
          <Button onClick={startNew}>New way in</Button>
        </div>
      ) : null}
      {note ? <StatusMessage tone={note.tone}>{note.text}</StatusMessage> : null}

      {open ? (
        <Editor
          key={open}
          id={open}
          isNew={isNew}
          template={data.template.split("my-way-in").join(open)}
          writable={writable}
          onSaved={(res) => {
            setIsNew(false);
            if (res.token) setNote({ text: `Token for ${open} (shown once): ${res.token}`, tone: "info" });
            list.reload();
          }}
        />
      ) : null}
    </Card>
  );
}

function Row({
  t,
  writable,
  busy,
  opened,
  sure,
  onToggle,
  onEdit,
  onAsk,
  onCancel,
  onConfirm,
}: {
  t: InboundHttpTrigger;
  writable: boolean;
  busy: boolean;
  opened: boolean;
  sure: Sure["action"] | null;
  onToggle: (v: boolean) => void;
  onEdit: () => void;
  onAsk: (a: Sure["action"]) => void;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <tr>
      <td>
        <Checkbox
          checked={t.enabled === true}
          onChange={onToggle}
          label={<span className="sr-only">{`${t.id} on`}</span>}
          disabled={!writable || !t.ok || busy}
        />
      </td>
      <td>
        <code>{t.id}</code>
        {t.ok ? null : <div className="ml-settings__note">{t.error}</div>}
        {t.quota && !t.quota.available ? <div className="ml-settings__note">quota out until {t.quota.resume_at}</div> : null}
      </td>
      <td>{t.port ?? ""}</td>
      <td>{t.bind_ips ? t.bind_ips.join(", ") : ""}</td>
      <td>{t.per_minute ? `${t.per_minute}/min` : ""}</td>
      <td>{t.ok ? <Badge tone={t.token === "set" ? "accent" : "warn"}>{t.token}</Badge> : null}</td>
      <td>
        {sure ? (
          <>
            <span className="ml-settings__note">{SURE_TEXT[sure]}</span>{" "}
            <Button size="sm" variant="danger" disabled={busy} onClick={onConfirm}>
              {`confirm ${sure}`}
            </Button>{" "}
            <Button size="sm" variant="ghost" disabled={busy} onClick={onCancel}>
              cancel
            </Button>
          </>
        ) : (
          <>
            <Button size="sm" onClick={onEdit}>
              {opened ? "Close" : "Edit"}
            </Button>
            {writable && t.ok ? (
              <>
                {" "}
                <Button size="sm" onClick={() => onAsk("regenerate")}>
                  Regenerate token
                </Button>{" "}
                <Button size="sm" variant="danger" onClick={() => onAsk("revoke")}>
                  Revoke token
                </Button>
              </>
            ) : null}
            {writable ? (
              <>
                {" "}
                <Button size="sm" variant="danger" onClick={() => onAsk("delete")}>
                  Delete
                </Button>
              </>
            ) : null}
          </>
        )}
      </td>
    </tr>
  );
}

function Editor({
  id,
  isNew,
  template,
  writable,
  onSaved,
}: {
  id: string;
  isNew: boolean;
  template: string;
  writable: boolean;
  onSaved: (res: InboundHttpVerdict) => void;
}) {
  const detail = useAsync<InboundHttpDetail | null>(
    (signal) => (isNew ? Promise.resolve(null) : api.inboundHttpTrigger(id, signal)),
    [id, isNew],
  );
  const [text, setText] = useState<string | null>(null);
  const [verdict, setVerdict] = useState<InboundHttpVerdict | null>(null);
  const [busy, setBusy] = useState("");
  const [out, setOut] = useState<{ text: string; tone: "success" | "error" } | null>(null);

  useEffect(() => {
    if (isNew) setText(template);
    else if (detail.data) {
      setText(detail.data.text);
      setVerdict({ ok: true, valid: detail.data.errors.length === 0, errors: detail.data.errors, warnings: detail.data.warnings });
    }
  }, [detail.data, isNew, template]);

  if (detail.loading && !detail.data && !isNew) return <Spinner label={`reading ${id}`} />;
  if (detail.error) return <StatusMessage tone="error">{detail.error}</StatusMessage>;
  if (text === null) return null;
  const saved = isNew ? null : detail.data;
  const dirty = saved ? text !== saved.text : true;

  const run = async (what: "validate" | "save") => {
    setBusy(what);
    setOut(null);
    try {
      if (what === "validate") {
        setVerdict(await api.saveInboundHttpTrigger(id, text, true));
      } else {
        const res = await api.saveInboundHttpTrigger(id, text);
        setVerdict({ ...res, valid: true });
        setOut({ text: res.created ? `Created ${id}.` : `Saved ${id}.`, tone: "success" });
        onSaved(res);
        detail.reload();
      }
    } catch (err) {
      setOut({ text: err instanceof Error ? err.message : String(err), tone: "error" });
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="ml-settings__preview">
      <h3 className="ml-settings__h3">
        <code>{id}.yaml</code>
        {isNew ? " (new, not saved)" : dirty ? " (changed, not saved)" : ""}
      </h3>
      <Field
        label="Way-in file"
        hint="YAML: port, bind_ips (IPs and CIDRs, checked against the TCP peer), throttle.per_minute, daily budget, attachments."
      >
        {(fid) => (
          <TextArea
            id={fid}
            className="ml-custom__editor"
            rows={14}
            spellCheck={false}
            value={text}
            onChange={(e) => {
              setText(e.target.value);
              setVerdict(null);
            }}
            disabled={!writable}
          />
        )}
      </Field>
      <div className="ml-settings__foot">
        <Button onClick={() => void run("validate")} disabled={!!busy}>
          {busy === "validate" ? "Validating…" : "Validate"}
        </Button>
        <Button variant="primary" onClick={() => void run("save")} disabled={!writable || !!busy || !dirty}>
          {busy === "save" ? "Saving…" : "Save"}
        </Button>
      </div>
      {out ? <StatusMessage tone={out.tone}>{out.text}</StatusMessage> : null}

      {verdict ? (
        verdict.valid === false || verdict.errors.length ? (
          <StatusMessage tone="error">
            Not valid:
            <ul className="ml-settings__list">
              {verdict.errors.map((e) => (
                <li key={e}>{e}</li>
              ))}
            </ul>
          </StatusMessage>
        ) : (
          <StatusMessage tone="success">Valid.</StatusMessage>
        )
      ) : null}
      {verdict?.warnings.length ? (
        <ul className="ml-settings__list" aria-label="warnings">
          {verdict.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      ) : null}

      {saved ? (
        saved.calls.length ? (
          <div className="ml-settings__scroll">
            <table className="ml-settings__table" aria-label="calls">
              <thead>
                <tr>
                  <th>At</th>
                  <th>Call</th>
                  <th>Peer</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {saved.calls.map((c) => (
                  <tr key={c.id}>
                    <td>{c.at}</td>
                    <td>{`${c.method} ${c.path}`}</td>
                    <td>{c.peer}</td>
                    <td>{c.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="ml-settings__note">No calls yet.</p>
        )
      ) : null}
    </div>
  );
}
