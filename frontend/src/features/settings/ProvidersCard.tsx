import { useEffect, useState } from "react";
import { api } from "@/api/client";
import { useAsync } from "@/hooks/useAsync";
import {
  Badge, Button, Card, Checkbox, ConfirmDialog, Field, Select, Spinner, StatusMessage, TextArea, TextInput,
} from "@/components";
import type { AgentDelegation, AgentProvider, AgentsStatus } from "@/api/types";
import "./SettingsSection.css";

type Note = { text: string; tone: "success" | "error" };

/**
 * The provider setup wizard (docs/agents.md#setup-wizard): per provider, is its
 * CLI installed, how to sign in, an optional API key, a real test call, and the
 * connector. Below it, whether agents may hand tasks to another provider. A
 * saved key is never shown again, only that one is saved.
 */
const isHttp = (kind: string) => kind === "huggingface";

export function ProvidersCard({ writable }: { writable: boolean }) {
  const providers = useAsync((signal) => api.agentProviders(signal), []);
  const status = useAsync<AgentsStatus>((signal) => api.agents(signal), []);
  const [keys, setKeys] = useState<Record<string, string>>({});
  const [notes, setNotes] = useState<Record<string, Note>>({});
  const [busy, setBusy] = useState("");
  const [confirm, setConfirm] = useState<AgentProvider | null>(null);
  const [brainFor, setBrainFor] = useState<AgentProvider | null>(null);
  const [deleg, setDeleg] = useState<AgentDelegation | null>(null);
  const [out, setOut] = useState<Note | null>(null);
  // Gemini: an individual account signs in through the Antigravity CLI (Google closed the
  // Gemini CLI sign-in for it); an organization keeps the Gemini CLI and its API key.
  const [acct, setAcct] = useState<"individual" | "org">("individual");

  useEffect(() => {
    if (status.data) setDeleg(status.data.config.delegation);
  }, [status.data]);

  if (providers.loading && !providers.data) return <Spinner label="reading providers" />;
  if (providers.error) return <StatusMessage tone="error">{providers.error}</StatusMessage>;
  if (!providers.data) return null;

  const note = (kind: string, n: Note) => setNotes((x) => ({ ...x, [kind]: n }));
  const run = async (kind: string, what: string, fn: () => Promise<Note>) => {
    setBusy(`${kind}:${what}`);
    try {
      note(kind, await fn());
    } catch (err) {
      note(kind, { text: err instanceof Error ? err.message : String(err), tone: "error" });
    } finally {
      setBusy("");
    }
  };

  const connectors = status.data?.connectors ?? [];
  const has = (kind: string) => connectors.some((c) => c.kind === kind);

  const addConnector = (p: AgentProvider) =>
    run(p.kind, "connect", async () => {
      await api.saveAgentsConfig({
        connectors: [
          ...connectors.map(({ available, problem, ...c }) => c),
          { kind: p.kind, name: p.title, enabled: true },
        ],
      });
      status.reload();
      return { text: "Connector added.", tone: "success" };
    });

  const connectorOf = (kind: string) => connectors.find((c) => c.kind === kind);
  // The per-connector switch inside a provider block: saves the target list at once.
  const toggleTarget = (kind: string, on: boolean) =>
    run(kind, "delegate", async () => {
      const c = connectorOf(kind);
      if (!c || !deleg) return { text: "Add the connector first.", tone: "error" };
      const next = { ...deleg, targets: on ? [...deleg.targets, c.id] : deleg.targets.filter((t) => t !== c.id) };
      await api.saveAgentsConfig({ delegation: next });
      setDeleg(next);
      status.reload();
      return { text: on ? "Agents may hand it tasks." : "Agents no longer hand it tasks.", tone: "success" };
    });

  const saveDelegation = async () => {
    if (!deleg) return;
    setBusy("delegation");
    setOut(null);
    try {
      await api.saveAgentsConfig({ delegation: deleg });
      setOut({ text: "Saved.", tone: "success" });
      status.reload();
    } catch (err) {
      setOut({ text: err instanceof Error ? err.message : String(err), tone: "error" });
    } finally {
      setBusy("");
    }
  };

  const enabled = providers.data.enabled;
  const space = providers.data.providers.find((p) => p.kind === "hfspace");
  const cliProviders = providers.data.providers.filter(
    (p) => p.kind !== "ollama" && p.kind !== "hfspace" && p.kind !== (acct === "individual" ? "gemini" : "antigravity"),
  );

  return (
    <Card>
      <h2 className="ml-settings__h">AI providers</h2>
      <p className="ml-settings__hint">
        Set up Claude, Gemini and ChatGPT (Codex) through their own CLIs, and Hugging Face (text and images) through its API. The default sign-in is the CLI&apos;s own;
        an API key is optional and kept in a private file, never in <code>agents.json</code>. Ollama is set up above.
        {providers.data.enabled ? null : " Gemini and ChatGPT appear once the ai_providers flag is on (Settings > Flags)."}
      </p>
      <ul className="ml-agents-card__list" aria-label="providers">
        {cliProviders.map((p) => {
          const n = notes[p.kind];
          return (
            <li key={p.kind} className="ml-agents-card__row">
              <div className="ml-agents-card__top">
                <strong>{p.title}</strong>
                {isHttp(p.kind) ? <Badge>HTTP API</Badge> : <Badge tone={p.installed ? "accent" : "warn"}>{p.installed ? `installed ${p.version ?? ""}`.trim() : "not installed"}</Badge>}
                {p.has_key ? <Badge tone="accent">API key saved</Badge> : <Badge>CLI sign-in</Badge>}
                {p.installed && !isHttp(p.kind) ? <Badge tone={p.brain ? "accent" : "warn"}>{p.brain ? "brain connected" : "brain not connected"}</Badge> : null}
              </div>
              {p.kind === "gemini" || p.kind === "antigravity" ? (
                <div className="ml-agents-card__fields">
                  <Field label="Gemini account" hint="Individual signs in through Antigravity (agy); an organization uses the Gemini CLI.">
                    {(id) => (
                      <Select id={id} value={acct} disabled={!writable || !!busy}
                        options={[{ value: "individual", label: "Individual" }, { value: "org", label: "Organization" }]}
                        onChange={(e) => setAcct(e.target.value as "individual" | "org")} />
                    )}
                  </Field>
                </div>
              ) : null}
              {isHttp(p.kind) ? (
              <p className="ml-settings__hint">
                No CLI: save a Hugging Face token (<code>{p.key_env}</code>, with the Inference Providers permission)
                below, run a test call, and add the connector. Text only: it answers, it does not edit files or run commands.
              </p>
              ) : (
              <p className="ml-settings__hint">
                1. Install: <code>{p.install}</code> &nbsp; 2. Sign in: run <code>{p.login}</code> in a terminal
                {p.key_env ? <>, or save an API key (<code>{p.key_env}</code>) below</> : " (it opens the browser)"}. 3. Connect the brain, so the CLI can read and write it. 4. Test call.
                5. Add the connector.
              </p>
              )}
              {p.key_env ? (
              <div className="ml-agents-card__fields">
                <Field label="API key" hint="Optional. Sent to the CLI as an environment variable.">
                  {(id) => (
                    <TextInput id={id} type="password" autoComplete="off" value={keys[p.kind] ?? ""}
                      onChange={(e) => setKeys((k) => ({ ...k, [p.kind]: e.target.value }))} disabled={!writable} />
                  )}
                </Field>
              </div>
              ) : null}
              <div className="ml-agents-card__add">
                {isHttp(p.kind) ? null : <Button size="sm" disabled={!writable || !!busy || p.installed} onClick={() => setConfirm(p)}>Install</Button>}
                {p.key_env ? <Button size="sm" disabled={!writable || !!busy || !(keys[p.kind] ?? "").trim()}
                  onClick={() => run(p.kind, "key", async () => {
                    await api.saveProviderKey(p.kind, keys[p.kind]);
                    setKeys((k) => ({ ...k, [p.kind]: "" }));
                    providers.reload();
                    return { text: "Key saved.", tone: "success" };
                  })}>Save key</Button> : null}
                {p.has_key ? (
                  <Button size="sm" variant="danger" disabled={!writable || !!busy}
                    onClick={() => run(p.kind, "key", async () => {
                      await api.saveProviderKey(p.kind, "", true);
                      providers.reload();
                      return { text: "Key removed.", tone: "success" };
                    })}>Remove key</Button>
                ) : null}
                {isHttp(p.kind) ? null : <Button size="sm" disabled={!writable || !!busy || !p.installed || !!p.brain}
                  onClick={() => setBrainFor(p)}>{p.brain ? "Brain connected" : "Connect brain"}</Button>}
                <Button size="sm" disabled={!writable || !!busy || !p.installed}
                  onClick={() => run(p.kind, "test", async () => {
                    const r = await api.testProvider(p.kind);
                    return { text: `Answered: ${r.answer ?? "ok"}`, tone: "success" };
                  })}>{busy === `${p.kind}:test` ? "Testing…" : "Test call"}</Button>
                <Button size="sm" variant="primary" disabled={!writable || !!busy || has(p.kind)}
                  onClick={() => addConnector(p)}>{has(p.kind) ? "Connector added" : "Add connector"}</Button>
              </div>
              {enabled && deleg?.enabled && has(p.kind) && p.kind !== "claude" ? (
                <Checkbox checked={deleg.targets.includes(connectorOf(p.kind)?.id ?? "")} disabled={!writable || !!busy}
                  label="Agents may hand this connector tasks"
                  onChange={(v) => toggleTarget(p.kind, v)} />
              ) : null}
              {n ? <StatusMessage tone={n.tone}>{n.text}</StatusMessage> : null}
              {p.kind === "huggingface" && space ? (
                <div className="ml-agents-card__row" aria-label="Hugging Face Space (image)">
                  <div className="ml-agents-card__top">
                    <strong>Images on a Space</strong>
                    <Badge>HTTP API</Badge>
                    <Badge tone={p.has_key ? "accent" : "warn"}>{p.has_key ? "same token" : "needs the token above"}</Badge>
                  </div>
                  <p className="ml-settings__hint">
                    Makes images on a Hugging Face Space with the token above. The test call is free (it checks the token and that the Space is up);
                    the model list shows only Spaces running on free hardware right now. Each image spends your daily ZeroGPU quota, and the prompt goes to the Space owner&apos;s server.
                    Delegated briefs get the smallest size and fewest steps that fit.
                  </p>
                  <div className="ml-agents-card__add">
                    <Button size="sm" disabled={!writable || !!busy || !p.has_key}
                      onClick={() => run(space.kind, "test", async () => {
                        const r = await api.testProvider(space.kind);
                        return { text: `Answered: ${r.answer ?? "ok"}`, tone: "success" };
                      })}>{busy === `${space.kind}:test` ? "Testing…" : "Test call"}</Button>
                    <Button size="sm" variant="primary" disabled={!writable || !!busy || has(space.kind)}
                      onClick={() => addConnector(space)}>{has(space.kind) ? "Connector added" : "Add connector"}</Button>
                  </div>
                  {enabled && deleg?.enabled && has(space.kind) ? (
                    <Checkbox checked={deleg.targets.includes(connectorOf(space.kind)?.id ?? "")} disabled={!writable || !!busy}
                      label="Agents may hand this connector tasks"
                      onChange={(v) => toggleTarget(space.kind, v)} />
                  ) : null}
                  {notes[space.kind] ? <StatusMessage tone={notes[space.kind].tone}>{notes[space.kind].text}</StatusMessage> : null}
                </div>
              ) : null}
            </li>
          );
        })}
      </ul>

      {confirm ? (
        <ConfirmDialog
          title={`Install ${confirm.title}?`}
          body={<>This runs <code>{confirm.install}</code> on this machine, then connects the brain to it (the
            plugin for Claude, a context block for Gemini and Codex), so it can read and write the brain.</>}
          confirmLabel="Run it"
          busy={busy.endsWith(":install")}
          onCancel={() => setConfirm(null)}
          onConfirm={async () => {
            const p = confirm;
            await run(p.kind, "install", async () => {
              const r = await api.installProvider(p.kind);
              providers.reload();
              return r.brain_error
                ? { text: `Installed, but the brain was not connected: ${r.brain_error}`, tone: "error" }
                : { text: "Installed, brain connected.", tone: "success" };
            });
            setConfirm(null);
          }}
        />
      ) : null}

      {brainFor ? (
        <ConfirmDialog
          title={`Connect the brain to ${brainFor.title}?`}
          body={brainFor.kind === "claude"
            ? <>This installs the memory-ledger plugin into Claude Code from this checkout.</>
            : <>This adds a marked block to <code>{brainFor.kind === "gemini" || brainFor.kind === "antigravity" ? "~/.gemini/GEMINI.md" : "~/.codex/AGENTS.md"}</code> with
              the commands to read and write the brain. The rest of the file is left alone.</>}
          confirmLabel="Connect"
          busy={busy.endsWith(":brain")}
          onCancel={() => setBrainFor(null)}
          onConfirm={async () => {
            const p = brainFor;
            await run(p.kind, "brain", async () => {
              const r = await api.installProviderBrain(p.kind);
              providers.reload();
              return { text: r.note ?? "Brain connected.", tone: "success" };
            });
            setBrainFor(null);
          }}
        />
      ) : null}

      {providers.data.enabled && deleg ? (
        <>
          <h3 className="ml-settings__h">Delegation</h3>
          <p className="ml-settings__hint">
            Let an agent hand a self-contained task to another provider. The other agent sees only the brief, runs
            read-only, and its provider is an external service: a brief naming a node from a blocked realm is refused.
          </p>
          <Checkbox checked={deleg.enabled} label="Allow delegation" disabled={!writable}
            onChange={(v) => setDeleg({ ...deleg, enabled: v })} />
          <div className="ml-agents-card__add">
            {connectors.map((c) => (
              <Checkbox key={c.id} label={`${c.name} may receive tasks`} disabled={!writable}
                checked={deleg.targets.includes(c.id)}
                onChange={(v) => setDeleg({
                  ...deleg, targets: v ? [...deleg.targets, c.id] : deleg.targets.filter((t) => t !== c.id),
                })} />
            ))}
          </div>
          <div className="ml-agents-card__fields">
            <Field label="When to delegate" hint="Free text, goes into the agent's instructions.">
              {(id) => <TextArea id={id} rows={3} value={deleg.rules} disabled={!writable}
                onChange={(e) => setDeleg({ ...deleg, rules: e.target.value })} />}
            </Field>
            <Field label="Never delegate" hint="Free text, same.">
              {(id) => <TextArea id={id} rows={3} value={deleg.never} disabled={!writable}
                onChange={(e) => setDeleg({ ...deleg, never: e.target.value })} />}
            </Field>
            <Field label="Blocked realms" hint="Comma-separated.">
              {(id) => <TextInput id={id} value={deleg.blocked_realms.join(", ")} disabled={!writable}
                onChange={(e) => setDeleg({ ...deleg, blocked_realms: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) })} />}
            </Field>
            <Field label="Tasks per turn">
              {(id) => <TextInput id={id} type="number" min={1} max={10} value={String(deleg.max_per_turn)} disabled={!writable}
                onChange={(e) => setDeleg({ ...deleg, max_per_turn: Number(e.target.value) })} />}
            </Field>
            <Field label="Timeout (s)">
              {(id) => <TextInput id={id} type="number" min={30} max={3600} value={String(deleg.timeout_s)} disabled={!writable}
                onChange={(e) => setDeleg({ ...deleg, timeout_s: Number(e.target.value) })} />}
            </Field>
          </div>
          <div className="ml-agents-card__add">
            <Button variant="primary" disabled={!writable || busy === "delegation"} onClick={saveDelegation}>Save delegation</Button>
            {out ? <StatusMessage tone={out.tone}>{out.text}</StatusMessage> : null}
          </div>
        </>
      ) : null}
    </Card>
  );
}
