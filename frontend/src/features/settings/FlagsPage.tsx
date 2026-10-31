import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "@/api/client";
import { useAsync } from "@/hooks/useAsync";
import { Badge, Card, Checkbox, Spinner, StatusMessage } from "@/components";
import { Section } from "@/features/shell/Section";
import type { FlagsPayload } from "@/api/types";
import "./SettingsSection.css";

/**
 * Settings > Flags: the feature flags `scripts/flags.py` defines, each switched on or off.
 *
 * The list is the code's, not the person's: a flag exists because a line in `FLAGS` says
 * so, and goes away with that line. What is stored is only which ones are on.
 */
export function FlagsPage() {
  const flags = useAsync<FlagsPayload>((signal) => api.flags(signal), []);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState<string | null>(null);

  const toggle = async (name: string, on: boolean) => {
    setBusy(name);
    setErr(null);
    try {
      await api.setFlag(name, on);
      flags.reload();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };

  return (
    <Section
      title="Flags"
      lead={
        <>
          Defined in code (<code>scripts/flags.py</code>), switched here. A flag wraps new behaviour
          at every place it is used, so retiring one is deleting its name.{" "}
          <Link to="/settings">Back to settings</Link>
        </>
      }
    >
      {flags.loading ? <Spinner label="reading flags" /> : null}
      {flags.error ? <StatusMessage tone="error">{flags.error}</StatusMessage> : null}
      {err ? <StatusMessage tone="error">{err}</StatusMessage> : null}
      {flags.data?.flags.map((f) => (
        <Card key={f.name}>
          <h2 className="ml-settings__h">
            {f.title} <Badge tone={f.enabled ? "success" : "neutral"}>{f.enabled ? "on" : "off"}</Badge>
          </h2>
          <p className="ml-settings__hint">
            <code>{f.name}</code> · {f.description}
          </p>
          <Checkbox
            checked={f.enabled}
            disabled={busy === f.name}
            onChange={(v) => toggle(f.name, v)}
            label={f.saved ? "Enabled" : `Enabled (default ${f.default ? "on" : "off"})`}
          />
        </Card>
      ))}
    </Section>
  );
}
