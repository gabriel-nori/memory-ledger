import { useState } from "react";
import { api } from "@/api/client";
import { usePolling } from "@/hooks/usePolling";
import { Badge, Button, StatusMessage } from "@/components";
import type { PortalTrigger } from "@/api/types";

/**
 * The portal triggers' controls: regenerate or revoke the token. Shown only when a portal trigger exists. A new token is shown
 * once, here, and never read back.
 */
export function PortalPanel() {
  const { data, refresh } = usePolling((signal) => api.portalTriggers(signal), { intervalMs: 15000 });
  const list = data?.triggers.filter((t) => t.ok) ?? [];
  if (!list.length) return null;
  return (
    <div className="ml-portal" role="region" aria-label="portal triggers">
      {list.map((t) => (
        <PortalRow key={t.id} t={t} onChanged={refresh} />
      ))}
    </div>
  );
}

function PortalRow({ t, onChanged }: { t: PortalTrigger; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [sure, setSure] = useState<"" | "regenerate" | "revoke">("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const run = async (action: "regenerate" | "revoke") => {
    setBusy(true);
    setErr("");
    setMsg("");
    try {
      const res = await api.portalAction({ id: t.id, action });
      if (!res.ok) throw new Error(res.error || "refused");
      setMsg(res.token ? `New token (shown once): ${res.token}` : `${action} done`);
      setSure("");
      onChanged();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="ml-portal__row">
      <div className="ml-portal__head">
        <b>{t.id}</b>
        <Badge>{t.enabled ? "on" : "off"}</Badge>
        <Badge tone={t.token === "set" ? "accent" : undefined}>token {t.token}</Badge>
        {t.bind_ips ? <Badge>{t.bind_ips.join(", ")}</Badge> : null}
        {t.per_minute ? <Badge>{t.per_minute}/min</Badge> : null}
        {t.quota && !t.quota.available ? <Badge>quota out until {t.quota.resume_at}</Badge> : null}
      </div>
      <div className="ml-portal__actions">
        {sure ? (
          <>
            <span>{sure === "regenerate" ? "The old token dies now." : "The portal stops accepting its token."}</span>
            <Button size="sm" variant="danger" disabled={busy} onClick={() => run(sure)}>confirm {sure}</Button>
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => setSure("")}>cancel</Button>
          </>
        ) : (
          <>
            <Button size="sm" onClick={() => setSure("regenerate")}>Regenerate token</Button>
            <Button size="sm" variant="danger" onClick={() => setSure("revoke")}>Revoke token</Button>
          </>
        )}
      </div>
      {msg ? <StatusMessage tone="info">{msg}</StatusMessage> : null}
      {err ? <StatusMessage tone="error">{err}</StatusMessage> : null}
    </div>
  );
}
