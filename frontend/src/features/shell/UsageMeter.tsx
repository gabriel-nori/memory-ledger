import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { UsageSummary } from "@/api/types";
import "./UsageMeter.css";
import { Icon } from "@/components";

const REFRESH_MS = 120_000;

const money = (v: number) =>
  `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

/**
 * Estimated Claude spend, pinned under the conversations in the Agents tab: the last 5 hours and today
 * on the bar; a click opens the longer windows upward.
 *
 * Dollars are list price per token read from the local logs, not what a
 * subscription charges, so every figure carries "est." and the real quota
 * stays out of here.
 */
export function UsageMeter({ active }: { active: boolean }) {
  const [data, setData] = useState<UsageSummary | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!active) return;
    const ctl = new AbortController();
    const load = () =>
      api
        .usageSummary(ctl.signal)
        .then((d) => d.ok && setData(d))
        .catch(() => undefined);
    load();
    const timer = window.setInterval(load, REFRESH_MS);
    return () => {
      ctl.abort();
      window.clearInterval(timer);
    };
  }, [active]);

  if (!data) return null;
  const rows: [string, number][] = [
    ["Week", data.week],
    ["Month", data.month],
    ["Year", data.year],
    ["Total", data.total],
  ];
  return (
    <div className="ml-meter">
      {open ? (
        <dl className="ml-meter__more" aria-label="estimated usage by period">
          {rows.map(([label, v]) => (
            <div key={label} className="ml-meter__row">
              <dt>{label}</dt>
              <dd>{money(v)}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      <button
        type="button"
        className="ml-meter__bar"
        aria-expanded={open}
        title="Estimated from local logs at list price, not your subscription"
        onClick={() => setOpen((o) => !o)}
      >
        <span className="ml-meter__cell">
          <span className="ml-meter__label">5h est.</span>
          <b>{money(data.fiveHours)}</b>
        </span>
        <span className="ml-meter__cell">
          <span className="ml-meter__label">Today est.</span>
          <b>{money(data.today)}</b>
        </span>
        <span aria-hidden="true" className="ml-meter__chev"><Icon name={open ? "chevron-down" : "chevron-up"} /></span>
      </button>
    </div>
  );
}
