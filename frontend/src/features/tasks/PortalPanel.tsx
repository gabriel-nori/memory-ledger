import { useState } from "react";
import { api } from "@/api/client";
import { usePolling } from "@/hooks/usePolling";
import { Badge, Button, StatusMessage } from "@/components";
import type { PortalTrigger } from "@/api/types";

/**
 * The portal triggers' controls: regenerate or revoke the token, and lift or
 * place a ban. Shown only when a portal trigger exists. A new token is shown
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
  const [who, setWho] = useState("");

  const run = async (action: "regenerate" | "revoke" | "ban" | "unban", target = "") => {
    setBusy(true);
    setErr("");
    setMsg("");
    try {
      const v = target || who.trim();
      const body = v.includes("@") ? { email: v } : { ip: v };
      const res = await api.portalAction({ id: t.id, action, ...(action === "ban" || action === "unban" ? body : {}) });
      if (!res.ok) throw new Error(res.error || "refused");
      setMsg(res.token ? `New token (shown once): ${res.token}` : `${action} done`);
      setSure("");
      if (action === "ban") setWho("");
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
        <input
          className="ml-portal__who"
          value={who}
          onChange={(e) => setWho(e.target.value)}
          placeholder="email or IP"
          aria-label="email or IP to ban"
        />
        <Button size="sm" variant="danger" disabled={busy || !who.trim()} onClick={() => run("ban")}>Ban</Button>
      </div>
      {t.ban_list?.filter((b) => !b.lifted_at).map((b) => (
        <div key={b.id} className="ml-portal__ban">
          <span>{b.email || b.ip} — {b.reason}</span>
          <Button size="sm" disabled={busy} onClick={() => run("unban", b.email || b.ip)}>Unban</Button>
        </div>
      ))}
      {t.strikes?.length ? (
        <p className="ml-portal__strikes">
          Strikes: {t.strikes.map((s) => `${s.email} ${s.count}/3`).join(" · ")}
        </p>
      ) : null}
      {msg ? <StatusMessage tone="info">{msg}</StatusMessage> : null}
      {err ? <StatusMessage tone="error">{err}</StatusMessage> : null}
    </div>
  );
}
