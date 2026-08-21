import { fmtInterval, fmtWhen, humanizeSeconds } from "../format";
import type { StatusResponse } from "../types";

export function StatusFooter({ status }: { status: StatusResponse | null }) {
  if (!status) {
    return (
      <footer>
        <span className="statusline">checking status&hellip;</span>
      </footer>
    );
  }

  const parts = Object.entries(status.components ?? {}).map(([k, v]) => `${k}: ${v}`);
  if (status.scheduler?.enabled && status.scheduler.next_run) {
    parts.push(`next backup ${fmtWhen(status.scheduler.next_run)} UTC`);
  }
  const retention = status.retention;
  if (retention?.enabled) {
    parts.push(
      `retention: keep ${humanizeSeconds(retention.max_age_seconds)}, sweep ${fmtInterval(retention.interval_seconds)}`
    );
    const lastRun = retention.last_run as { at?: string; deleted?: number } | null;
    if (lastRun?.at) {
      parts.push(`last swept ${fmtWhen(lastRun.at)} UTC (${lastRun.deleted ?? 0} deleted)`);
    }
  }

  const healthy =
    Object.keys(status.components ?? {}).length > 0 &&
    Object.values(status.components).every((v) => v === "ok");

  return (
    <footer>
      <span className={`statusline ${healthy ? "ok" : "warn"}`}>{parts.join(" \u00b7 ")}</span>
    </footer>
  );
}
