import type { BackupItem } from "./types";

/**
 * Filter backups by creation time.
 *
 * Presets are second-offsets ("3600" = last hour); "custom" uses the given
 * datetime-local strings. The `until` bound is inclusive of its whole minute,
 * matching what a datetime-local input can express.
 */
export function applyTimeFilter(
  items: BackupItem[],
  preset: string,
  from = "",
  until = ""
): BackupItem[] {
  let fromMs: number | null = null;
  let untilMs: number | null = null;
  if (preset === "custom") {
    if (from) fromMs = new Date(from).getTime();
    if (until) untilMs = new Date(until).getTime() + 59999; // include the whole minute
  } else {
    const seconds = parseInt(preset, 10);
    if (!isNaN(seconds)) fromMs = Date.now() - seconds * 1000;
  }
  return items.filter((b) => {
    const t = new Date(b.last_modified).getTime();
    if (isNaN(t)) return true; // unparseable timestamps are always shown
    if (fromMs != null && t < fromMs) return false;
    if (untilMs != null && t > untilMs) return false;
    return true;
  });
}
