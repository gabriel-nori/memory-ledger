import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/api/client";
import { useAsync } from "@/hooks/useAsync";
import { Badge, Button, Card, Checkbox, Field, Select, Spinner, StatusMessage, TextInput } from "@/components";
import type { AgentConnector, AgentKind, AgentsStatus } from "@/api/types";
import "./SettingsSection.css";

type Draft = Partial<AgentConnector> & { kind: AgentKind; models_text?: string };

const NEW: Record<"claude" | "ollama", Draft> = {
  claude: { kind: "claude", name: "Claude", enabled: true, bin: "claude", timeout_s: 900 },
  ollama: { kind: "ollama", name: "Ollama", enabled: true, url: "http://127.0.0.1:11434", timeout_s: 600, max_rounds: 8 },
};

const toDraft = (c: AgentConnector): Draft => ({
  ...c,
  models_text: c.kind === "claude" ? c.models.join(", ") : undefined,
});

/**
 * The connectors the Agents tab can talk to (docs/agents.md), saved to
 * `<home>/agents.json`. A connector is one backend: a Claude CLI, or one Ollama
 * host. Two Ollama hosts are two connectors. The whole list is saved at once.
 */
export function AgentsCard({ writable }: { writable: boolean }) {
  const status = useAsync<AgentsStatus>((signal) => api.agents(signal), []);
  const [rows, setRows] = useState<Draft[]>([]);
  const [defaults, setDefaults] = useState({ default_connector: "", context_turns: 1, recall_limit: 8, max_concurrent: 2 });
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState<{ text: string; tone: "success" | "error" } | null>(null);

  useEffect(() => {
    if (!status.data) return;
    setRows(status.data.connectors.map(toDraft));
    const c = status.data.config;
    setDefaults({
      default_connector: c.default_connector,
      context_turns: c.context_turns,
      recall_limit: c.recall_limit,
      max_concurrent: c.max_concurrent,
    });
  }, [status.data]);

  if (status.loading && !status.data) return <Spinner label="reading agents" />;
  if (status.error) return <StatusMessage tone="error">{status.error}</StatusMessage>;
  if (!status.data) return null;

  const edit = (i: number, patch: Partial<Draft>) => setRows((r) => r.map((x, j) => (j === i ? { ...x, ...patch } : x)));

  const save = async () => {
    setBusy(true);
    setOut(null);
    try {
      await api.saveAgentsConfig({
        ...defaults,
        connectors: rows.map(({ models_text, ...row }) => {
          const c: Partial<AgentConnector> = { ...row };
          delete c.available;
          delete c.problem;
          return {
          ...c,
          ...(c.kind === "claude"
            ? { models: (models_text ?? "").split(",").map((m) => m.trim()).filter(Boolean) }
            : { models: undefined }),
          };
        }),
      });
      setOut({ text: "Saved.", tone: "success" });
      status.reload();
    } catch (err) {
      setOut({ text: err instanceof Error ? err.message : String(err), tone: "error" });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <h2 className="ml-settings__h">Agents</h2>
      <p className="ml-settings__hint">
        The models <Link to="/agents">Agents</Link> can talk to. Every turn is a fresh call: the message, the last few
        exchanges, and what the brain recalls for it. Saved to <code>agents.json</code> in the brain home.
      </p>
      {status.data.config._problem ? <StatusMessage tone="error">{status.data.config._problem}</StatusMessage> : null}

      <ul className="ml-agents-card__list" aria-label="connectors">
        {rows.map((c, i) => (
          <li key={i} className="ml-agents-card__row">
            <div className="ml-agents-card__top">
              <Badge tone="accent">{c.kind}</Badge>
              <Checkbox checked={!!c.enabled} onChange={(v) => edit(i, { enabled: v })} label="enabled" disabled={!writable} />
              {c.problem ? <Badge tone="warn">{c.problem}</Badge> : null}
              <Button size="sm" variant="danger" disabled={!writable} onClick={() => setRows((r) => r.filter((_, j) => j !== i))}>
                Remove
              </Button>
            </div>
            <div className="ml-agents-card__fields">
              <Field label="Name">
                {(id) => (
                <TextInput id={id} value={c.name ?? ""} onChange={(e) => edit(i, { name: e.target.value })} disabled={!writable} />)}
              </Field>
              {c.kind === "claude" ? (
                <>
                  <Field label="CLI" hint="The claude binary.">
                    {(id) => (
                    <TextInput id={id} value={c.bin ?? ""} onChange={(e) => edit(i, { bin: e.target.value })} disabled={!writable} />)}
                  </Field>
                  <Field label="Models" hint="Comma-separated model ids.">
                    {(id) => (
                    <TextInput id={id} value={c.models_text ?? ""} onChange={(e) => edit(i, { models_text: e.target.value })} disabled={!writable} />)}
                  </Field>
                  <Field label="Default model">
                    {(id) => (
                    <TextInput id={id} value={c.default_model ?? ""} onChange={(e) => edit(i, { default_model: e.target.value })} disabled={!writable} />)}
                  </Field>
                </>
              ) : c.kind !== "ollama" ? (
                <>
                  <Field label="CLI" hint={`The ${c.kind} binary. Set up in AI providers below.`}>
                    {(id) => (
                    <TextInput id={id} value={c.bin ?? ""} onChange={(e) => edit(i, { bin: e.target.value })} disabled={!writable} />)}
                  </Field>
                  <Field label="Default model" hint="Empty: the CLI picks its own.">
                    {(id) => (
                    <TextInput id={id} value={c.default_model ?? ""} onChange={(e) => edit(i, { default_model: e.target.value })} disabled={!writable} />)}
                  </Field>
                </>
              ) : (
                <>
                  <Field label="URL" hint="The Ollama host.">
                    {(id) => (
                    <TextInput id={id} value={c.url ?? ""} onChange={(e) => edit(i, { url: e.target.value })} disabled={!writable} />)}
                  </Field>
                  <Field label="Default model" hint={c.models?.length ? `${c.models.length} pulled` : "save, then pick"}>
                    {(id) => (
                    <Select id={id}
                      value={c.default_model ?? ""}
                      options={[{ value: "", label: "first available" }, ...(c.models ?? []).map((m) => ({ value: m, label: m }))]}
                      onChange={(e) => edit(i, { default_model: e.target.value })}
                      disabled={!writable}
                    />)}
                  </Field>
                </>
              )}
              <Field label="Timeout (s)">
                {(id) => (
                <TextInput id={id}
                  type="number"
                  value={String(c.timeout_s ?? "")}
                  onChange={(e) => edit(i, { timeout_s: Number(e.target.value) })}
                  disabled={!writable}
                />)}
              </Field>
            </div>
          </li>
        ))}
      </ul>
      <div className="ml-agents-card__add">
        <Button size="sm" disabled={!writable} onClick={() => setRows((r) => [...r, { ...NEW.claude, models_text: "claude-sonnet-5-5" }])}>
          ＋ Claude
        </Button>
        <Button size="sm" disabled={!writable} onClick={() => setRows((r) => [...r, { ...NEW.ollama }])}>
          ＋ Ollama
        </Button>
      </div>

      <div className="ml-agents-card__fields">
        <Field label="New conversations use">
          {(id) => (
          <Select id={id}
            value={defaults.default_connector}
            options={rows.filter((r) => r.id).map((r) => ({ value: r.id!, label: r.name ?? r.id! }))}
            onChange={(e) => setDefaults((d) => ({ ...d, default_connector: e.target.value }))}
            disabled={!writable}
          />)}
        </Field>
        <Field label="Earlier exchanges per turn" hint="The default; each conversation can change it.">
          {(id) => (
          <TextInput id={id}
            type="number"
            min={0}
            max={10}
            value={String(defaults.context_turns)}
            onChange={(e) => setDefaults((d) => ({ ...d, context_turns: Number(e.target.value) }))}
            disabled={!writable}
          />)}
        </Field>
        <Field label="Recalled nodes per turn">
          {(id) => (
          <TextInput id={id}
            type="number"
            min={0}
            max={30}
            value={String(defaults.recall_limit)}
            onChange={(e) => setDefaults((d) => ({ ...d, recall_limit: Number(e.target.value) }))}
            disabled={!writable}
          />)}
        </Field>
        <Field label="Replies at once">
          {(id) => (
          <TextInput id={id}
            type="number"
            min={1}
            max={8}
            value={String(defaults.max_concurrent)}
            onChange={(e) => setDefaults((d) => ({ ...d, max_concurrent: Number(e.target.value) }))}
            disabled={!writable}
          />)}
        </Field>
      </div>

      <div className="ml-agents-card__add">
        <Button variant="primary" disabled={!writable || busy} onClick={() => void save()}>
          {busy ? "saving…" : "Save agents"}
        </Button>
        {out ? <StatusMessage tone={out.tone}>{out.text}</StatusMessage> : null}
      </div>
    </Card>
  );
}
