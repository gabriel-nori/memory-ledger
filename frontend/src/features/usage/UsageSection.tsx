import { useEffect, useMemo, useRef } from "react";
import { api } from "@/api/client";
import { useAsync } from "@/hooks/useAsync";
import { usePref } from "@/hooks/usePrefs";
import { Button, Select, Spinner, StatusMessage, Icon } from "@/components";
import type { UsageReportPayload } from "@/api/types";
import { OPEN_RANGE, downloadText, lastWeeks, normalizeRange, printReport, reportFilename } from "./report";
import type { WeekRange } from "./report";
import "./UsageSection.css";

/**
 * "Where the Quota Went", over the brain's usage-week nodes.
 *
 * The server renders the page (scripts/usage_report.py, no model call) and
 * this frames it, the way API docs frames Swagger: the report is one
 * self-contained HTML document, so the file you download is byte for byte the
 * page you are reading, and a copy rebuilt in React would be a second report
 * to keep in step. The frame is sandboxed without scripts — the charts are
 * static SVG and need none — but same-origin, so the print dialog can be
 * opened on the report alone.
 */
export function UsageSection() {
  const [stored, setRange] = usePref<WeekRange>("usage.range", OPEN_RANGE);
  const frame = useRef<HTMLIFrameElement>(null);

  // The first read learns which weeks exist; the range is then checked
  // against them, so a stored bound for a week that is gone reads as open.
  const { data, error, loading } = useAsync<UsageReportPayload>(
    (signal) => api.usageReport({ from: stored.from, to: stored.to }, signal),
    [stored.from, stored.to],
  );
  const weeks = useMemo(() => data?.weeks ?? [], [data]);
  const range = normalizeRange(stored, weeks);

  // A stored bound for a week that is no longer stored (or a range saved
  // backwards) is corrected once the weeks are known, so the pickers and the
  // page never disagree about what is shown.
  const { from, to } = range;
  useEffect(() => {
    if (weeks.length && (from !== stored.from || to !== stored.to)) setRange({ from, to });
  }, [weeks, from, to, stored.from, stored.to, setRange]);

  const options = (open: string) => [
    { value: "", label: open },
    ...weeks.map((w) => ({ value: w.week, label: `${w.week} · ${w.from}` })),
  ];
  const set = (next: WeekRange) => setRange(normalizeRange(next, weeks));
  const html = data?.html ?? "";
  const selected = data?.selected ?? [];

  return (
    <div className="ml-usage">
      <div className="ml-usage__bar">
        <h1 className="ml-usage__title">Usage</h1>
        {weeks.length ? (
          <div className="ml-usage__range" role="group" aria-label="week range">
            <Select
              aria-label="from week"
              value={range.from}
              options={options("first week")}
              onChange={(e) => set({ ...range, from: e.target.value })}
            />
            <span aria-hidden="true"><Icon name="arrow-right" /></span>
            <Select
              aria-label="to week"
              value={range.to}
              options={options("newest week")}
              onChange={(e) => set({ ...range, to: e.target.value })}
            />
            <Button size="sm" onClick={() => set(lastWeeks(weeks, 4))}>last 4</Button>
            <Button size="sm" onClick={() => set(OPEN_RANGE)}>all</Button>
          </div>
        ) : null}
        {loading && data ? <Spinner label="rendering the report" /> : null}
        <div className="ml-usage__actions">
          <Button
            size="sm"
            disabled={!html}
            onClick={() => downloadText(html, reportFilename(selected))}
          >
            Download HTML
          </Button>
          <Button
            size="sm"
            variant="primary"
            disabled={!html}
            title="opens the print dialog: choose Save as PDF"
            onClick={() => printReport(frame.current, html, reportFilename(selected, "pdf"))}
          >
            Save as PDF
          </Button>
        </div>
      </div>
      {error ? (
        <div className="ml-usage__msg">
          <StatusMessage tone="error">{error}</StatusMessage>
        </div>
      ) : null}
      {!data && !error ? (
        <div className="ml-usage__msg">
          <Spinner label="rendering the report" />
        </div>
      ) : null}
      {data ? (
        <iframe
          ref={frame}
          className="ml-usage__frame"
          title="usage report"
          sandbox="allow-same-origin allow-modals"
          srcDoc={html}
        />
      ) : null}
    </div>
  );
}
