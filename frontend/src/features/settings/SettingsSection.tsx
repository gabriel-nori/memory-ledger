import { useEffect, useState } from "react";
import { api } from "@/api/client";
import { useAsync } from "@/hooks/useAsync";
import { useMeta } from "@/api/MetaContext";
import {
  Badge,
  Button,
  Card,
  Field,
  KeyValue,
  Select,
  Spinner,
  StatusMessage,
  TextInput,
} from "@/components";
import { Section } from "@/features/shell/Section";
import { SettingSwitch } from "./SettingSwitch";
import { CaptureCard } from "./CaptureCard";
import { UpkeepCard } from "./UpkeepCard";
import { DoctorCard } from "./DoctorCard";
import { BrowserCard } from "./BrowserCard";
import { TriggerCard } from "./TriggerCard";
import { CustomTriggersCard } from "./CustomTriggersCard";
import { B2BCard } from "./B2BCard";
import { AgentsCard } from "./AgentsCard";
import { VoiceCard } from "./VoiceCard";
import { RealmsCard } from "./RealmsCard";
import { LearnedRulesCard } from "./LearnedRulesCard";
import { PromptRecallCard } from "./PromptRecallCard";
import { AgentsSyncCard } from "./AgentsSyncCard";
import type { RealmCount, Settings, Status, StoredSettings, Timezones } from "@/api/types";
import "./SettingsSection.css";

/**
 * Preferences, the switches, the health of the store — and what belongs where.
 *
 * The page is grouped by LIFETIME, because three different things used to sit in
 * one flat card and behave differently without saying so:
 *
 *   engine config   `<home>/settings.json` — read by the CLI, the hooks and any
 *                   cron over the same brain, so a change here is a change
 *                   everywhere (§2g).
 *   machine state   the `.disabled` marker, the neo4j credentials, the snapshots
 *                   on disk. Per machine, not per person.
 *   view state      this browser's own, in localStorage, optionally mirrored
 *                   into the brain — see BrowserCard for why that is a separate
 *                   question.
 */
export function SettingsSection() {
  const settings = useAsync<Settings>((signal) => api.settings(signal), []);
  const status = useAsync<Status>((signal) => api.status(signal), []);
  const zones = useAsync<Timezones>((signal) => api.timezones(signal), []);
  const realms = useAsync<{ ok: boolean; realms: Record<string, RealmCount>; default: string }>(
    (signal) => api.realms(signal),
    [],
  );

  return (
    <Section
      title="Settings"
      lead="Saved to the brain's home directory, so every path into it — CLI, hooks, cron, this page — reads the same answer."
    >
      {settings.loading ? <Spinner label="reading settings" /> : null}
      {settings.error ? <StatusMessage tone="error">{settings.error}</StatusMessage> : null}

      {settings.data ? (
        <>
          <PreferencesCard
            settings={settings.data}
            zones={zones.data?.zones ?? []}
            realms={Object.keys(realms.data?.realms ?? {})}
            onSaved={() => settings.reload()}
          />
          <RealmsCard
            realms={realms.data?.realms ?? {}}
            current={settings.data.effective.realm}
            writable={settings.data.writable}
            onChanged={() => {
              settings.reload();
              realms.reload();
            }}
          />
          <CaptureCard
            capture={settings.data.capture}
            writable={settings.data.writable}
            onChanged={() => settings.reload()}
          />
          <PromptRecallCard
            effective={settings.data.effective}
            writable={settings.data.writable}
            onChanged={() => settings.reload()}
          />
          <AgentsSyncCard
            effective={settings.data.effective}
            writable={settings.data.writable}
            onChanged={() => settings.reload()}
          />
          <LearnedRulesCard
            effective={settings.data.effective}
            writable={settings.data.writable}
            onChanged={() => settings.reload()}
          />
          <BrowserCard
            effective={settings.data.effective}
            writable={settings.data.writable}
            onChange={async (key, value) => {
              await api.saveSettings({ [key]: value } as Partial<StoredSettings>);
              // The provider holds the sync flag; tell it directly rather than
              // making it poll, so the next click is already being stored.
              if (key === "ui_sync") {
                window.dispatchEvent(new CustomEvent("ml:ui-sync", { detail: value }));
              }
              settings.reload();
            }}
          />
        </>
      ) : null}

      {status.data ? (
        <UpkeepCard writable={status.data.writable} onChanged={() => status.reload()} />
      ) : null}

      <DoctorCard />

      {settings.data ? <TriggerCard writable={settings.data.writable} /> : null}
      {settings.data ? <CustomTriggersCard writable={settings.data.writable} /> : null}

      {settings.data ? <B2BCard writable={settings.data.writable} /> : null}
      {settings.data ? <AgentsCard writable={settings.data.writable} /> : null}

      {settings.data ? <VoiceCard writable={settings.data.writable} /> : null}

      {status.data ? <StoreCard status={status.data} onChanged={() => status.reload()} /> : null}
      {status.error ? <StatusMessage tone="error">{status.error}</StatusMessage> : null}
    </Section>
  );
}

function PreferencesCard({
  settings,
  zones,
  realms,
  onSaved,
}: {
  settings: Settings;
  zones: string[];
  realms: string[];
  onSaved: () => void;
}) {
  const eff = settings.effective;
  const [tz, setTz] = useState(settings.settings.tz ?? "");
  const [realm, setRealm] = useState(settings.settings.realm ?? "");
  const [owner, setOwner] = useState(settings.settings.owner ?? "");
  const [brainName, setBrainName] = useState(settings.settings.brain_name ?? "");
  const meta = useMeta();
  const [backend, setBackend] = useState(settings.settings.backend ?? "");
  const [mapPoll, setMapPoll] = useState(String(eff.map_poll_seconds));
  const [dashRefresh, setDashRefresh] = useState(String(eff.dash_refresh_seconds));
  const [out, setOut] = useState<{ text: string; tone: "info" | "error" | "success" } | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setTz(settings.settings.tz ?? "");
    setRealm(settings.settings.realm ?? "");
    setOwner(settings.settings.owner ?? "");
    setBrainName(settings.settings.brain_name ?? "");
    setBackend(settings.settings.backend ?? "");
    setMapPoll(String(eff.map_poll_seconds));
    setDashRefresh(String(eff.dash_refresh_seconds));
  }, [settings, eff.map_poll_seconds, eff.dash_refresh_seconds]);

  const save = async (patch?: Partial<StoredSettings>) => {
    setBusy(true);
    setOut(null);
    try {
      // "" REMOVES a key, falling back to the env var or the system default —
      // which is why the fields send empty strings rather than skipping them.
      await api.saveSettings(
        patch ?? {
          tz,
          realm,
          owner,
          brain_name: brainName,
          backend,
          map_poll_seconds: Number(mapPoll) || 0,
          dash_refresh_seconds: Number(dashRefresh) || 0,
        },
      );
      setOut({ text: `Saved to ${settings.path}.`, tone: "success" });
      onSaved();
      // The name is in the sidebar and the tab, read from the vocabulary.
      meta.reload();
    } catch (err) {
      setOut({ text: err instanceof Error ? err.message : String(err), tone: "error" });
    } finally {
      setBusy(false);
    }
  };

  const overridden = Object.keys(settings.env);

  return (
    <Card>
      <h2 className="ml-settings__h">Preferences</h2>
      <p className="ml-settings__hint">
        Precedence is the same everywhere in the engine: explicit flag &gt; environment variable &gt;
        this file &gt; built-in default.
      </p>

      {overridden.length ? (
        <StatusMessage tone="info">
          The shell has taken over {overridden.join(", ")} — saving here will not change what the
          engine actually uses until those variables are unset.
        </StatusMessage>
      ) : null}

      <Field
        label="Your name"
        hint={`Who "me" is: what --mine filters on, and what an unassigned task falls back to. In effect: ${eff.owner} (from ${eff.owner_source}).`}
      >
        {(id) => (
          <TextInput
            id={id}
            value={owner}
            placeholder={eff.owner}
            onChange={(e) => setOwner(e.target.value)}
          />
        )}
      </Field>

      <Field
        label="The brain's name"
        hint={`What it answers to: {brain} in a wake phrase ("${
          eff.brain_name
        } will handle this"), a signature or a run command, and the name at the top of the page. In effect: ${
          eff.brain_name
        } (from ${eff.brain_name_source}).`}
      >
        {(id) => (
          <TextInput
            id={id}
            value={brainName}
            maxLength={40}
            placeholder={eff.brain_name}
            onChange={(e) => setBrainName(e.target.value)}
          />
        )}
      </Field>

      <Field
        label="Timezone"
        hint={`Decides which calendar day a due date falls on. In effect: ${
          eff.tz || eff.system_tz
        } (from ${eff.tz_source}); the system says ${eff.system_tz}.`}
      >
        {(id) => (
          <>
            <TextInput
              id={id}
              list="ml-zones"
              value={tz}
              placeholder={eff.system_tz}
              onChange={(e) => setTz(e.target.value)}
            />
            <datalist id="ml-zones">
              {zones.map((z) => (
                <option key={z} value={z} />
              ))}
            </datalist>
          </>
        )}
      </Field>

      <div className="ml-settings__grid">
        <Field
          label="Default realm"
          hint={`New nodes land here when nothing else says. Per machine, so work on one and personal on another is exactly what this is for. In effect: ${eff.realm}.`}
        >
          {(id) => (
            <Select
              id={id}
              value={realm}
              onChange={(e) => setRealm(e.target.value)}
              // The realms the graph actually has, plus whatever is stored —
              // a realm set before its first node exists must not vanish from
              // its own dropdown.
              options={[
                { value: "", label: `default (${eff.realm})` },
                ...Array.from(new Set([...realms, realm].filter(Boolean)))
                  .sort()
                  .map((r) => ({ value: r, label: r })),
              ]}
            />
          )}
        </Field>

        <Field
          label="Read from"
          hint={`Which store a CLI read uses when given no --backend. Writes always hit SQLite first and mirror onward, whatever this says. In effect: ${eff.backend}.`}
        >
          {(id) => (
            <Select
              id={id}
              value={backend}
              onChange={(e) => setBackend(e.target.value)}
              options={[
                { value: "", label: `default (${eff.backend})` },
                ...eff.backends.map((b) => ({
                  value: b,
                  label:
                    b === "auto" ? "auto — neo4j when it answers, else the local mirror" : b,
                })),
              ]}
            />
          )}
        </Field>
      </div>

      <div className="ml-settings__grid">
        <Field label="Map poll (seconds)" hint="How often the map re-reads the graph.">
          {(id) => (
            <TextInput id={id} type="number" min={1} value={mapPoll} onChange={(e) => setMapPoll(e.target.value)} />
          )}
        </Field>
        <Field label="Dashboard refresh (seconds)" hint="How often a date view re-runs.">
          {(id) => (
            <TextInput
              id={id}
              type="number"
              min={5}
              value={dashRefresh}
              onChange={(e) => setDashRefresh(e.target.value)}
            />
          )}
        </Field>
      </div>

      <h3 className="ml-settings__h3">Behaviour</h3>
      <SettingSwitch
        label="Push every write to the mirror"
        hint="Off, writes still queue for neo4j — they just wait for a flush instead of going out one at a time. Useful for a bulk load; the usual cause of a mirror that looks stale."
        checked={eff.autosync}
        source={eff.autosync_source}
        envVar="MEMORY_LEDGER_NO_AUTOSYNC"
        disabled={!settings.writable || busy}
        onChange={(v) => save({ autosync: v })}
      />
      <SettingSwitch
        label="Consolidate closed days automatically"
        hint="Once a day, at session start, yesterday and earlier are folded into per-day digests so recall stays cheap. Non-destructive, and it never touches the day you are still living in."
        checked={eff.consolidate}
        source={eff.consolidate_source}
        envVar="MEMORY_LEDGER_NO_CONSOLIDATE"
        disabled={!settings.writable || busy}
        onChange={(v) => save({ consolidate: v })}
      />

      <div className="ml-settings__foot">
        <Button variant="primary" onClick={() => save()} disabled={busy || !settings.writable}>
          {busy ? "saving…" : "Save"}
        </Button>
        {!settings.writable ? <span className="ml-settings__note">read-only server</span> : null}
      </div>
      {out ? <StatusMessage tone={out.tone}>{out.text}</StatusMessage> : null}
    </Card>
  );
}

function StoreCard({ status, onChanged }: { status: Status; onChanged: () => void }) {
  const [url, setUrl] = useState(status.neo4j.url);
  const [db, setDb] = useState(status.neo4j.db);
  const [user, setUser] = useState(status.neo4j.user);
  const [password, setPassword] = useState("");
  const [out, setOut] = useState<{ text: string; tone: "info" | "error" | "success" } | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(true);
    setOut({ text: `${label}…`, tone: "info" });
    try {
      const res = (await fn()) as Record<string, unknown>;
      setOut({ text: describe(label, res), tone: "success" });
      onChanged();
    } catch (err) {
      setOut({ text: `${label} failed: ${err instanceof Error ? err.message : String(err)}`, tone: "error" });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <h2 className="ml-settings__h">Where the data lives</h2>
      <KeyValue
        pairs={[
          ["database", status.db],
          ["home", status.home],
          ["nodes", status.nodes],
          ["edges", status.edges],
          ["queued writes", status.pending_ops],
          ["loose captures", status.loose_captures],
          ["reading from", status.neo4j.reading_from],
        ]}
      />

      <h3 className="ml-settings__h3">
        The neo4j master{" "}
        <Badge tone={status.neo4j.up ? "success" : status.neo4j.configured ? "danger" : "neutral"}>
          {status.neo4j.up ? "reachable" : status.neo4j.configured ? "unreachable" : "not configured"}
        </Badge>
      </h3>
      <p className="ml-settings__hint">
        Optional. The brain is one local SQLite file and works offline; a master is a mirror, not a
        dependency. The password is written mode 600 and never sent back to the browser — leave it
        blank to keep the stored one, and clear the url to go sqlite-only.
      </p>
      {status.neo4j.env.length ? (
        <StatusMessage>
          These are set in the environment and win over the file: {status.neo4j.env.join(", ")}.
        </StatusMessage>
      ) : null}

      <div className="ml-settings__grid">
        <Field label="URL">
          {(id) => (
            <TextInput id={id} value={url} placeholder="neo4j+s://…" onChange={(e) => setUrl(e.target.value)} />
          )}
        </Field>
        <Field label="Database">
          {(id) => <TextInput id={id} value={db} placeholder="neo4j" onChange={(e) => setDb(e.target.value)} />}
        </Field>
        <Field label="User">
          {(id) => <TextInput id={id} value={user} placeholder="neo4j" onChange={(e) => setUser(e.target.value)} />}
        </Field>
        <Field
          label="Password"
          hint={status.neo4j.password_set ? "One is stored. Blank keeps it." : "None stored yet."}
        >
          {(id) => (
            <TextInput
              id={id}
              type="password"
              value={password}
              autoComplete="new-password"
              onChange={(e) => setPassword(e.target.value)}
            />
          )}
        </Field>
      </div>

      <div className="ml-settings__foot">
        <Button
          disabled={busy || !status.writable}
          onClick={() => run("Saving", () => api.saveNeo4j({ url, db, user, password }))}
        >
          Save connection
        </Button>
        <Button disabled={busy || !status.writable} onClick={() => run("Testing", () => api.testNeo4j())}>
          Test
        </Button>
        <Button
          disabled={busy || !status.writable}
          title={`${status.pending_ops} write${status.pending_ops === 1 ? "" : "s"} queued`}
          onClick={() => run("Flushing", () => api.flush())}
        >
          Flush queue ({status.pending_ops})
        </Button>
      </div>

      {out ? <StatusMessage tone={out.tone}>{out.text}</StatusMessage> : null}
    </Card>
  );
}

/** Turns a write result into a sentence, rather than showing raw JSON. */
function describe(label: string, res: Record<string, unknown>): string {
  const parts = Object.entries(res)
    .filter(([k]) => k !== "ok")
    .map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : String(v)}`);
  return parts.length ? `${label} done — ${parts.join(", ")}` : `${label} done.`;
}

export { PreferencesCard, StoreCard };
