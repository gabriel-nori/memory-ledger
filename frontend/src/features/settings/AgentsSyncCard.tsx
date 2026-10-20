import { useState } from "react";
import { api } from "@/api/client";
import { Badge, Card, StatusMessage } from "@/components";
import { SettingSwitch } from "./SettingSwitch";
import type { EffectiveSettings } from "@/api/types";
import "./SettingsSection.css";

/**
 * Agents chat sync: the `agents_sync` setting, which the mirror reads.
 *
 * OFF by default. On, the Agents tab conversations and messages go to the
 * master and come back on the other machines. Drafts and the running process id
 * never leave this machine.
 */
export function AgentsSyncCard({
  effective,
  writable,
  onChanged,
}: {
  effective: EffectiveSettings;
  writable: boolean;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const save = async (v: boolean) => {
    setBusy(true);
    setErr(null);
    try {
      await api.saveSettings({ agents_sync: v });
      onChanged();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <h2 className="ml-settings__h">
        Agents sync{" "}
        <Badge tone={effective.agents_sync ? "success" : "neutral"}>
          {effective.agents_sync ? "on" : "off"}
        </Badge>
      </h2>
      <p className="ml-settings__hint">
        Share the Agents tab chats between machines through the neo4j master. Drafts and the
        process that is answering stay on the machine that has them. It needs a master configured
        above.
      </p>

      <SettingSwitch
        label="Sync Agents chats across machines"
        hint="Off by default. A reply that is still running elsewhere can only be taken over after about 30 seconds without a heartbeat."
        checked={effective.agents_sync}
        source={effective.agents_sync_source}
        envVar="AGENTS_SYNC"
        disabled={!writable || busy}
        onChange={save}
      />
      {!writable ? <span className="ml-settings__note">read-only server</span> : null}
      {err ? <StatusMessage tone="error">{err}</StatusMessage> : null}
    </Card>
  );
}
