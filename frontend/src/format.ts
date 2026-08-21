import type { Job } from "./types";

export function fmtBytes(n: number | null | undefined): string {
  if (n == null || isNaN(n)) return "\u2014";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = n;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i++;
  }
  const text = value.toFixed(value >= 100 || i === 0 ? 0 : 1);
  return `${text.replace(/\.0$/, "")} ${units[i]}`;
}

export function fmtWhen(iso: string | null | undefined): string {
  if (!iso) return "\u2014";
  const d = new Date(iso);
  return isNaN(d.getTime()) ? iso : d.toISOString().slice(0, 19).replace("T", " ");
}

export function fmtLocal(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "\u2014";
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function ageText(iso: string): string {
  const t = new Date(iso).getTime();
  if (isNaN(t)) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function humanizeSeconds(seconds: number): string {
  if (!seconds) return "off";
  if (seconds >= 604800 && seconds % 604800 === 0) return `${seconds / 604800}w`;
  if (seconds >= 86400 && seconds % 86400 === 0) return `${seconds / 86400}d`;
  if (seconds >= 3600 && seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds >= 60 && seconds % 60 === 0) return `${seconds / 60}m`;
  return `${seconds}s`;
}

export function fmtInterval(seconds: number): string {
  return seconds ? `every ${humanizeSeconds(seconds)}` : "disabled";
}

export function fmtDuration(job: Job): string {
  if (!job.finished_at || !job.started_at) return "";
  const ms = new Date(job.finished_at).getTime() - new Date(job.started_at).getTime();
  if (isNaN(ms) || ms < 0) return "";
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

export function shortKey(key: string): string {
  return key.split("/").pop() ?? key;
}

function num(value: unknown, fallback = 0): number {
  return typeof value === "number" ? value : fallback;
}

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

export function summarizeJob(job: Job): string {
  const r = job.result ?? {};
  switch (job.kind) {
    case "backup":
      return `${num(r.nodes)} nodes \u2192 ${shortKey(str(r.key))} (${fmtBytes(num(r.bytes))})`;
    case "restore":
      return (
        `created ${num(r.created)}, updated ${num(r.updated)}` +
        (r.wipe ? `, deleted ${num(r.deleted_on_wipe)}` : "")
      );
    case "retention":
      return `scanned ${num(r.scanned)}, deleted ${num(r.deleted)}, kept ${num(r.kept)}`;
    default:
      return "";
  }
}
