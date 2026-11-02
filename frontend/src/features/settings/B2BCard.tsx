import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/api/client";
import { useAsync } from "@/hooks/useAsync";
import {
  Badge,
  Button,
  Card,
  Checkbox,
  ConfirmDialog,
  Field,
  Select,
  Spinner,
  StatusMessage,
  TextInput,
} from "@/components";
import type { BadgeTone } from "@/components";
import type {
  B2BAllowed,
  B2BBroker,
  B2BConfig,
  B2BPeer,
  B2BStatus,
  Person,
  ScriptsPayload,
  TriggerEventSetting,
  TriggerStatus,
} from "@/api/types";
import { ActionEditor } from "./TriggerActions";
import type { ScriptsState } from "./TriggerActions";
import "./SettingsSection.css";

type Out = { text: string; tone: "info" | "error" | "success" } | null;

const msg = (err: unknown) =>
  err instanceof Error ? err.message : String(err);

/**
 * Brain to brain (docs/b2b.md): messages between two second brains, carried by
 * one or more brokers that only ever see ciphertext.
 *
 * Everything here writes `<home>/b2b.json` through the server, which restarts
 * its poller on the new file. The private key is WRITE-ONLY, like a trigger
 * token: the server says whether one is set and where from, never what it is,
 * so this card has no way to show it. A blank import field means "keep what is
 * stored".
 */
export function B2BCard({ writable }: { writable: boolean }) {
  const status = useAsync<B2BStatus>((signal) => api.b2b(signal), []);
  const people = useAsync<{ ok: boolean; people: Person[] }>(
    (signal) => api.people(signal),
    [],
  );

  if (status.loading && !status.data)
    return <Spinner label="reading brain to brain" />;
  if (status.error)
    return <StatusMessage tone="error">{status.error}</StatusMessage>;
  if (!status.data) return null;
  const { config, runtime, watching } = status.data;
  const reload = () => status.reload();

  return (
    <Card>
      <h2 className="ml-settings__h">
        B2B (Brain 2 Brain){" "}
        <Badge tone={config.active ? "success" : "neutral"}>
          {config.active ? "on" : "off"}
        </Badge>
      </h2>
      <p className="ml-settings__hint">
        Send a message to someone else&rsquo;s second brain, and receive theirs.
        Messages are encrypted with your key before they reach a broker. Saved
        to <code>{config.path}</code>, mode 600. Write and read them in{" "}
        <Link to="/messages">Messages</Link>.
      </p>

      {!watching ? (
        <StatusMessage tone="info">
          This server was started with <code>--no-trigger</code> or{" "}
          <code>--no-write</code>, so it does not poll. What you save here is
          kept for the next start without that.
        </StatusMessage>
      ) : null}
      {config.problem ? (
        <StatusMessage tone="error">{config.problem}</StatusMessage>
      ) : null}

      <IdentityPanel config={config} writable={writable} onSaved={reload} />
      <PollPanel
        config={config}
        runtime={runtime}
        writable={writable}
        onSaved={reload}
      />
      <BrokersPanel
        brokers={config.brokers}
        writable={writable}
        onSaved={reload}
      />
      <PeersPanel
        peers={status.data.peers}
        brokers={config.brokers}
        people={people.data?.people ?? []}
        writable={writable}
        onSaved={reload}
      />
      <AllowedPanel
        allowed={status.data.allowed ?? []}
        writable={writable}
        onSaved={reload}
      />

      {!writable ? (
        <span className="ml-settings__note">read-only server</span>
      ) : null}
    </Card>
  );
}

// ---------------------------------------------------------------- saving
/** One write at a time, with its outcome shown under the section that made it. */
function useWrite(onSaved: () => void) {
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState<Out>(null);
  const run = async (call: () => Promise<unknown>, done = "Saved.") => {
    setBusy(true);
    setOut(null);
    try {
      await call();
      setOut({ text: done, tone: "success" });
      onSaved();
      return true;
    } catch (err) {
      setOut({ text: msg(err), tone: "error" });
      return false;
    } finally {
      setBusy(false);
    }
  };
  return { busy, out, run, setOut };
}

// ---------------------------------------------------------------- identity
function IdentityPanel({
  config,
  writable,
  onSaved,
}: {
  config: B2BConfig;
  writable: boolean;
  onSaved: () => void;
}) {
  const { busy, out, run, setOut } = useWrite(onSaved);
  const [key, setKey] = useState("");
  const [confirming, setConfirming] = useState(false);
  const pk = config.private_key;
  const fromEnv = pk.source === "env";

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(config.user_id);
      setOut({ text: "Copied.", tone: "success" });
    } catch (err) {
      setOut({ text: msg(err), tone: "error" });
    }
  };
  const generate = async (force: boolean) => {
    const ok = await run(() => api.b2bIdentity(force), "Identity made.");
    if (ok) setConfirming(false);
  };
  const importKey = async () => {
    if (await run(() => api.saveB2B({ private_key: key }), "Key imported."))
      setKey("");
  };

  return (
    <section className="ml-settings__preview" aria-label="Identity">
      <h3 className="ml-settings__h3">
        Identity{" "}
        <Badge tone={pk.set ? "success" : "neutral"}>
          {pk.set ? "key set" : "no key"}
        </Badge>
      </h3>
      {config.user_id ? (
        <>
          <p className="ml-settings__hint">
            Your user id is your address. Give it to the people you want to hear
            from.
          </p>
          <div className="ml-settings__foot">
            <code className="ml-b2b__id">{config.user_id}</code>
            <Button size="sm" onClick={copy}>
              Copy
            </Button>
          </div>
        </>
      ) : (
        <p className="ml-settings__hint">
          No identity yet. Generate one to send and receive.
        </p>
      )}
      <p className="ml-settings__hint">
        Private key:{" "}
        <Badge tone={pk.set ? "success" : "neutral"}>
          {fromEnv ? "set (env)" : pk.set ? "set (file)" : "not set"}
        </Badge>{" "}
        {fromEnv
          ? `It comes from $${pk.env_var} in the server's environment, which outranks this page.`
          : "It is never shown. You can only replace it or remove it."}
      </p>
      <div className="ml-settings__foot">
        {config.user_id ? (
          <Button
            disabled={!writable || busy}
            onClick={() => setConfirming(true)}
          >
            Replace identity
          </Button>
        ) : (
          <Button
            variant="primary"
            disabled={!writable || busy}
            onClick={() => generate(false)}
          >
            Generate identity
          </Button>
        )}
        {pk.set && !fromEnv ? (
          <Button
            disabled={!writable || busy}
            onClick={() =>
              run(() => api.saveB2B({ clear: ["private_key"] }), "Key removed.")
            }
          >
            Remove key
          </Button>
        ) : null}
      </div>
      <Field
        label="Import a private key"
        hint="Paste one to use it instead. Leave blank to keep what is stored."
      >
        {(id) => (
          <div className="ml-settings__foot">
            <TextInput
              id={id}
              type="password"
              autoComplete="new-password"
              placeholder={fromEnv ? "from the environment" : "private key"}
              value={key}
              disabled={fromEnv || !writable}
              onChange={(e) => setKey(e.target.value)}
            />
            <Button
              disabled={!writable || busy || fromEnv || !key.trim()}
              onClick={importKey}
            >
              Import key
            </Button>
          </div>
        )}
      </Field>
      {out ? <StatusMessage tone={out.tone}>{out.text}</StatusMessage> : null}

      {confirming ? (
        <ConfirmDialog
          title="Replace your identity?"
          body={
            <p>
              A new key pair means a new user id. Messages sent to the old id
              can no longer be read, and everyone who has it must be given the
              new one.
            </p>
          }
          confirmLabel="Replace"
          danger
          busy={busy}
          onConfirm={() => generate(true)}
          onCancel={() => setConfirming(false)}
        />
      ) : null}
    </section>
  );
}

// ---------------------------------------------------------------- polling
const STATE_TONE: Record<string, BadgeTone> = {
  polling: "success",
  connected: "success",
  connecting: "accent",
  erroring: "danger",
  misconfigured: "danger",
  off: "neutral",
};

function PollPanel({
  config,
  runtime,
  writable,
  onSaved,
}: {
  config: B2BConfig;
  runtime: B2BStatus["runtime"];
  writable: boolean;
  onSaved: () => void;
}) {
  const { busy, out, run } = useWrite(onSaved);
  // The global run-command variables live with the triggers; the b2b ones come with this config.
  const trigger = useAsync<TriggerStatus>((signal) => api.trigger(signal), []);
  const scriptsReq = useAsync<ScriptsPayload>(
    (signal) => api.scripts(signal),
    [],
  );
  const scripts: ScriptsState = {
    list: scriptsReq.data?.scripts ?? [],
    dir: scriptsReq.data?.dir ?? "",
    reload: () => scriptsReq.reload(),
  };
  const fromConfig = (): TriggerEventSetting => ({
    on: true,
    do: config.do,
    action: config.action,
    agent: config.agent ?? "",
    http: config.http,
    script: config.script,
  });
  const [seconds, setSeconds] = useState(String(config.poll_seconds));
  const [setting, setSetting] = useState<TriggerEventSetting>(fromConfig);
  useEffect(() => {
    setSeconds(String(config.poll_seconds));
    setSetting(fromConfig());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    config.poll_seconds,
    config.do,
    config.action,
    config.agent,
    config.http,
    config.script,
  ]);
  const names = Object.keys(config.variables);
  const globals = (trigger.data?.config.variables ?? []).filter(
    (v) => v.global,
  );

  return (
    <section className="ml-settings__preview" aria-label="Receiving">
      <h3 className="ml-settings__h3">
        Receiving{" "}
        <Badge tone={STATE_TONE[runtime.state] ?? "neutral"}>
          {runtime.state}
        </Badge>
      </h3>
      {runtime.last_error ? (
        <StatusMessage tone="error">{runtime.last_error}</StatusMessage>
      ) : null}
      <div className="ml-settings__switch">
        <Checkbox
          label="Poll the brokers"
          checked={config.enabled}
          disabled={!writable || busy}
          onChange={(v) =>
            run(
              () => api.saveB2B({ enabled: v }),
              v ? "Polling on." : "Polling off.",
            )
          }
        />
        <p className="ml-settings__hint ml-settings__hint--switch">
          Off, nothing is fetched and nothing is run, whatever else is set here.
        </p>
      </div>
      <div className="ml-settings__grid">
        <Field label="Poll every (seconds)" hint="From 30 to 86400.">
          {(id) => (
            <TextInput
              id={id}
              type="number"
              min={30}
              max={86400}
              value={seconds}
              onChange={(e) => setSeconds(e.target.value)}
            />
          )}
        </Field>
      </div>
      <p className="ml-settings__hint">
        What a received message runs, as for the other triggers: a Claude
        session told the run command, an API call, or a script. The message
        itself is <code>{"{trigger_content}"}</code>.
      </p>
      <ActionEditor
        setting={setting}
        onChange={(patch) => setSetting((s) => ({ ...s, ...patch }))}
        variables={globals}
        extra={names}
        writable={writable && !busy}
        scripts={scripts}
        name="a received B2B message"
        argsPlaceholder={"{b2b_peer_name}\n{trigger_content}"}
        runHint="What the session is told to do when a message arrives. Put a variable in braces where a value belongs. Left empty, the built-in handling runs."
      />
      {names.length ? (
        <ul className="ml-settings__list">
          {names.map((n) => (
            <li key={n}>
              <code>{`{${n}}`}</code> {config.variables[n]}
            </li>
          ))}
        </ul>
      ) : null}
      <div className="ml-settings__foot">
        <Button
          variant="primary"
          disabled={!writable || busy}
          onClick={() =>
            run(() =>
              api.saveB2B({
                poll_seconds: Number(seconds),
                do: setting.do,
                action: setting.action,
                agent: setting.agent ?? "",
                http: setting.http,
                script: setting.script,
              }),
            )
          }
        >
          {busy ? "saving…" : "Save receiving"}
        </Button>
      </div>
      {out ? <StatusMessage tone={out.tone}>{out.text}</StatusMessage> : null}
    </section>
  );
}

// ---------------------------------------------------------------- brokers
function BrokersPanel({
  brokers,
  writable,
  onSaved,
}: {
  brokers: B2BBroker[];
  writable: boolean;
  onSaved: () => void;
}) {
  const { busy, out, run } = useWrite(onSaved);
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");

  const add = async () => {
    const body: Record<string, unknown> = { url: url.trim() };
    if (name.trim()) body.name = name.trim();
    if (await run(() => api.saveB2BBroker(body), "Broker added.")) {
      setUrl("");
      setName("");
    }
  };

  return (
    <section className="ml-settings__preview" aria-label="Brokers">
      <h3 className="ml-settings__h3">
        Brokers <Badge tone="neutral">{brokers.length}</Badge>
      </h3>
      <p className="ml-settings__hint">
        A broker holds messages until the other brain picks them up. You can use
        several.
      </p>
      {brokers.length ? (
        <ul className="ml-trigger__rows">
          {brokers.map((b) => (
            <BrokerRow
              key={b.id}
              broker={b}
              writable={writable}
              onSaved={onSaved}
            />
          ))}
        </ul>
      ) : (
        <p className="ml-settings__hint">No broker yet.</p>
      )}
      <div className="ml-settings__grid">
        <Field label="Broker URL" hint="Starts with http:// or https://.">
          {(id) => (
            <TextInput
              id={id}
              value={url}
              placeholder="https://"
              onChange={(e) => setUrl(e.target.value)}
            />
          )}
        </Field>
        <Field label="Broker name" hint="Optional.">
          {(id) => (
            <TextInput
              id={id}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          )}
        </Field>
      </div>
      <div className="ml-settings__foot">
        <Button disabled={!writable || busy || !url.trim()} onClick={add}>
          Add broker
        </Button>
      </div>
      {out ? <StatusMessage tone={out.tone}>{out.text}</StatusMessage> : null}
    </section>
  );
}

function BrokerRow({
  broker,
  writable,
  onSaved,
}: {
  broker: B2BBroker;
  writable: boolean;
  onSaved: () => void;
}) {
  const { busy, out, run } = useWrite(onSaved);
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(broker.name);
  const [url, setUrl] = useState(broker.url);
  const [removing, setRemoving] = useState(false);
  const [checking, setChecking] = useState(false);
  const [check, setCheck] = useState<Out>(null);
  const label = broker.name || broker.url;

  const runCheck = async () => {
    setChecking(true);
    setCheck(null);
    try {
      const r = await api.checkB2BBroker(broker.id);
      setCheck({
        text: r.session.open
          ? `Logged in${r.session.expires_at ? `, until ${r.session.expires_at}` : ""}.`
          : "Not logged in.",
        tone: r.session.open ? "success" : "error",
      });
      onSaved();
    } catch (err) {
      setCheck({ text: msg(err), tone: "error" });
    } finally {
      setChecking(false);
    }
  };

  return (
    <li className="ml-b2b__row" aria-label={`Broker ${label}`}>
      <div className="ml-trigger__event-head">
        <Checkbox
          label={label}
          checked={broker.enabled}
          disabled={!writable || busy}
          onChange={(v) =>
            run(
              () => api.saveB2BBroker({ id: broker.id, enabled: v }),
              v ? "Broker on." : "Broker off.",
            )
          }
        />
        <Badge tone={broker.session.open ? "success" : "neutral"}>
          {broker.session.open ? "logged in" : "no session"}
        </Badge>
        {broker.session.open && broker.session.expires_at ? (
          <span className="ml-settings__note">
            until {broker.session.expires_at}
          </span>
        ) : null}
      </div>
      {broker.name ? <code className="ml-b2b__id">{broker.url}</code> : null}
      {editing ? (
        <div className="ml-settings__grid">
          <Field label="Name">
            {(id) => (
              <TextInput
                id={id}
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            )}
          </Field>
          <Field label="URL">
            {(id) => (
              <TextInput
                id={id}
                value={url}
                onChange={(e) => setUrl(e.target.value)}
              />
            )}
          </Field>
          <div className="ml-settings__foot">
            <Button
              size="sm"
              variant="primary"
              disabled={!writable || busy || !url.trim()}
              onClick={async () => {
                if (
                  await run(() =>
                    api.saveB2BBroker({ id: broker.id, name, url: url.trim() }),
                  )
                )
                  setEditing(false);
              }}
            >
              Save broker
            </Button>
            <Button size="sm" onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </div>
        </div>
      ) : null}
      <div className="ml-settings__foot">
        <Button size="sm" disabled={!writable || checking} onClick={runCheck}>
          {checking ? "checking…" : "Check login"}
        </Button>
        <Button
          size="sm"
          disabled={!writable || busy}
          onClick={() => setEditing((e) => !e)}
        >
          Edit
        </Button>
        <Button
          size="sm"
          disabled={!writable || busy}
          onClick={() => setRemoving(true)}
        >
          Remove
        </Button>
      </div>
      {check ? (
        <StatusMessage tone={check.tone}>{check.text}</StatusMessage>
      ) : null}
      {out ? <StatusMessage tone={out.tone}>{out.text}</StatusMessage> : null}

      {removing ? (
        <ConfirmDialog
          title={`Remove ${label}?`}
          body={
            <p>
              Peers on this broker stop receiving until you point them at
              another one.
            </p>
          }
          confirmLabel="Remove"
          danger
          busy={busy}
          onConfirm={async () => {
            if (
              await run(
                () => api.saveB2BBroker({ id: broker.id, remove: true }),
                "Broker removed.",
              )
            ) {
              setRemoving(false);
            }
          }}
          onCancel={() => setRemoving(false)}
        />
      ) : null}
    </li>
  );
}

// ---------------------------------------------------------------- peers
const PEER_ID = /^[A-Za-z0-9_-]{43}$/;

function PeersPanel({
  peers,
  brokers,
  people,
  writable,
  onSaved,
}: {
  peers: B2BPeer[];
  brokers: B2BBroker[];
  people: Person[];
  writable: boolean;
  onSaved: () => void;
}) {
  const { busy, out, run } = useWrite(onSaved);
  const [peerId, setPeerId] = useState("");
  const [name, setName] = useState("");
  const [brokerId, setBrokerId] = useState("");
  const [personId, setPersonId] = useState("");
  const active = peers.filter((p) => !p.removed_at).length;

  const add = async () => {
    const body: {
      peer_id: string;
      enabled: boolean;
      name?: string;
      broker_id?: string;
      person_id?: string;
    } = {
      peer_id: peerId.trim(),
      enabled: true,
    };
    if (name.trim()) body.name = name.trim();
    if (brokerId) body.broker_id = brokerId;
    if (personId) body.person_id = personId;
    if (await run(() => api.setB2BPeer(body), "Peer connected.")) {
      setPeerId("");
      setName("");
      setBrokerId("");
      setPersonId("");
    }
  };

  return (
    <section className="ml-settings__preview" aria-label="Peers">
      <h3 className="ml-settings__h3">
        Peers <Badge tone="neutral">{active}</Badge>
      </h3>
      <p className="ml-settings__hint">
        The brains you exchange messages with. Disconnecting keeps the row, so
        you can reconnect.
      </p>
      {peers.length ? (
        <ul className="ml-trigger__rows">
          {peers.map((p) => (
            <li
              key={p.peer_id}
              className="ml-b2b__row"
              aria-label={`Peer ${p.name || p.peer_id}`}
            >
              <div className="ml-trigger__event-head">
                <strong>{p.name || "unnamed"}</strong>
                {p.removed_at ? (
                  <Badge tone="neutral">disconnected</Badge>
                ) : (
                  <Badge tone="success">connected</Badge>
                )}
                {p.person_title ? (
                  <Badge tone="accent">{p.person_title}</Badge>
                ) : null}
                <Button
                  size="sm"
                  disabled={!writable || busy}
                  onClick={() =>
                    run(
                      () =>
                        api.setB2BPeer({
                          peer_id: p.peer_id,
                          enabled: !!p.removed_at,
                          ...(p.name ? { name: p.name } : {}),
                          ...(p.broker_id ? { broker_id: p.broker_id } : {}),
                          ...(p.person_id ? { person_id: p.person_id } : {}),
                        }),
                      p.removed_at ? "Peer reconnected." : "Peer disconnected.",
                    )
                  }
                >
                  {p.removed_at ? "Reconnect" : "Disconnect"}
                </Button>
              </div>
              <code className="ml-b2b__id">{p.peer_id}</code>
            </li>
          ))}
        </ul>
      ) : (
        <p className="ml-settings__hint">No peer yet.</p>
      )}
      <div className="ml-settings__grid">
        <Field label="Peer user id" hint="Their 43-character public key.">
          {(id) => (
            <TextInput
              id={id}
              value={peerId}
              onChange={(e) => setPeerId(e.target.value)}
            />
          )}
        </Field>
        <Field label="Peer name" hint="Optional.">
          {(id) => (
            <TextInput
              id={id}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          )}
        </Field>
        <Field label="Peer broker" hint="Where their messages arrive.">
          {(id) => (
            <Select
              id={id}
              value={brokerId}
              onChange={(e) => setBrokerId(e.target.value)}
              options={[
                { value: "", label: "Any broker" },
                ...brokers.map((b) => ({
                  value: b.id,
                  label: b.name || b.url,
                })),
              ]}
            />
          )}
        </Field>
        <Field
          label="Linked person"
          hint="Optional. Ties them to a person in your brain."
        >
          {(id) => (
            <Select
              id={id}
              value={personId}
              onChange={(e) => setPersonId(e.target.value)}
              options={[
                { value: "", label: "No person" },
                ...people.map((p) => ({ value: p.id, label: p.title })),
              ]}
            />
          )}
        </Field>
      </div>
      <div className="ml-settings__foot">
        <Button
          disabled={!writable || busy || !PEER_ID.test(peerId.trim())}
          onClick={add}
        >
          Add peer
        </Button>
      </div>
      {out ? <StatusMessage tone={out.tone}>{out.text}</StatusMessage> : null}
    </section>
  );
}

// ---------------------------------------------------------------- allowed list
function AllowedPanel({
  allowed,
  writable,
  onSaved,
}: {
  allowed: B2BAllowed[];
  writable: boolean;
  onSaved: () => void;
}) {
  const { busy, out, run } = useWrite(onSaved);
  const [userId, setUserId] = useState("");
  const [name, setName] = useState("");
  const active = allowed.filter((a) => !a.removed_at).length;

  const add = async () => {
    const body: { user_id: string; enabled: boolean; name?: string } = {
      user_id: userId.trim(),
      enabled: true,
    };
    if (name.trim()) body.name = name.trim();
    if (await run(() => api.setB2BAllowed(body), "Sender allowed.")) {
      setUserId("");
      setName("");
    }
  };

  return (
    <section className="ml-settings__preview" aria-label="Allowed senders">
      <h3 className="ml-settings__h3">
        Allowed senders <Badge tone="neutral">{active}</Badge>
      </h3>
      <p className="ml-settings__hint">
        Like friends: only these brains' messages run anything. Adding a peer
        allows it too. A message from anyone else is logged as blocked in Tasks,
        where you can reprocess it.
      </p>
      {allowed.length ? (
        <ul className="ml-trigger__rows">
          {allowed.map((a) => (
            <li
              key={a.user_id}
              className="ml-b2b__row"
              aria-label={`Sender ${a.name || a.user_id}`}
            >
              <div className="ml-trigger__event-head">
                <strong>{a.name || "unnamed"}</strong>
                {a.removed_at ? (
                  <Badge tone="neutral">blocked</Badge>
                ) : (
                  <Badge tone="success">allowed</Badge>
                )}
                {a.is_peer ? <Badge tone="accent">peer</Badge> : null}
                <Button
                  size="sm"
                  disabled={!writable || busy}
                  onClick={() =>
                    run(
                      () =>
                        api.setB2BAllowed({
                          user_id: a.user_id,
                          enabled: !!a.removed_at,
                        }),
                      a.removed_at ? "Sender allowed." : "Sender blocked.",
                    )
                  }
                >
                  {a.removed_at ? "Allow" : "Block"}
                </Button>
              </div>
              <code className="ml-b2b__id">{a.user_id}</code>
            </li>
          ))}
        </ul>
      ) : (
        <p className="ml-settings__hint">
          Nobody yet: every message is blocked.
        </p>
      )}
      <div className="ml-settings__grid">
        <Field label="Sender user id" hint="Their 43-character public key.">
          {(id) => (
            <TextInput
              id={id}
              value={userId}
              onChange={(e) => setUserId(e.target.value)}
            />
          )}
        </Field>
        <Field label="Sender name" hint="Optional.">
          {(id) => (
            <TextInput
              id={id}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          )}
        </Field>
      </div>
      <div className="ml-settings__foot">
        <Button
          disabled={!writable || busy || !PEER_ID.test(userId.trim())}
          onClick={add}
        >
          Allow sender
        </Button>
      </div>
      {out ? <StatusMessage tone={out.tone}>{out.text}</StatusMessage> : null}
    </section>
  );
}
