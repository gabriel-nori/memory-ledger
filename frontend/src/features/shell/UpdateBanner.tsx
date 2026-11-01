import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { UpdateInfo } from "@/api/types";
import { Button, StatusMessage } from "@/components";
import "./UpdateBanner.css";

const CHECK_EVERY_MS = 8 * 60 * 60 * 1000;
const DISMISSED_KEY = "ml:update-dismissed";

/**
 * "A new version is out", with Update and Later.
 *
 * Asks on load and every 8 hours; the server caches GitHub's answer for the same
 * time. Later hides that one version for good (a newer one shows again). Update
 * has the server swap its own code and restart: the page reloads itself when the
 * server's boot id changes, so nothing here waits for it. A git checkout is told
 * to `git pull` instead of being offered a button that cannot work.
 */
export function UpdateBanner() {
  const [info, setInfo] = useState<UpdateInfo | null>(null);
  const [dismissed, setDismissed] = useState(() => localStorage.getItem(DISMISSED_KEY) ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let live = true;
    const ask = () =>
      api
        .update()
        .then((r) => live && setInfo(r))
        .catch(() => undefined); // no network is not worth a banner
    ask();
    const t = setInterval(ask, CHECK_EVERY_MS);
    return () => {
      live = false;
      clearInterval(t);
    };
  }, []);

  if (!info?.available || info.latest === dismissed) return null;

  const later = () => {
    localStorage.setItem(DISMISSED_KEY, info.latest);
    setDismissed(info.latest);
  };
  const install = () => {
    setBusy(true);
    setError("");
    api.updateApply().catch((e: Error) => {
      setBusy(false);
      setError(e.message);
    });
  };

  return (
    <div className="ml-update" role="status">
      <StatusMessage tone={error ? "error" : "info"}>
        {error
          ? `Update failed: ${error}`
          : busy
            ? `Installing ${info.latest}… the page reloads when it is done.`
            : `Version ${info.latest} is out (you have ${info.current}).`}
      </StatusMessage>
      {!busy && info.installable ? (
        <Button variant="primary" size="sm" onClick={install}>
          Update
        </Button>
      ) : null}
      {!busy && !info.installable && info.why_not ? <span>{info.why_not}</span> : null}
      {!busy ? (
        <Button size="sm" onClick={later}>
          Later
        </Button>
      ) : null}
    </div>
  );
}
