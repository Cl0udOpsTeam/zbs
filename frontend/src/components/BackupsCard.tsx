import { useMemo, useState, type ChangeEvent } from "react";
import { applyTimeFilter } from "../filters";
import { ageText, fmtBytes, fmtLocal, shortKey } from "../format";
import type { BackupItem } from "../types";

const PRESETS: Array<{ value: string; label: string }> = [
  { value: "all", label: "All time" },
  { value: "3600", label: "Last hour" },
  { value: "86400", label: "Last 24 hours" },
  { value: "604800", label: "Last 7 days" },
  { value: "2592000", label: "Last 30 days" },
  { value: "custom", label: "Custom range\u2026" },
];

interface Props {
  backups: BackupItem[];
  loadError: string | null;
  onRefresh: () => Promise<void>;
  onBackupNow: () => Promise<void>;
  onRestore: (key: string, wipe: boolean) => Promise<void>;
}

/** Props are expected to handle their own errors (App notifies); make sure a
 * rejected promise can never escape into an unhandled rejection here. */
function settled(promise: Promise<void>): void {
  void promise.catch(() => {});
}

export function BackupsCard({ backups, loadError, onRefresh, onBackupNow, onRestore }: Props) {
  const [preset, setPreset] = useState("all");
  const [from, setFrom] = useState("");
  const [until, setUntil] = useState("");
  const [wipe, setWipe] = useState(false);
  const [pendingRestore, setPendingRestore] = useState<string | null>(null);
  const [working, setWorking] = useState(false);

  const shown = useMemo(
    () => applyTimeFilter(backups, preset, from, until),
    [backups, preset, from, until]
  );

  async function handlePreset(event: ChangeEvent<HTMLSelectElement>) {
    setPreset(event.target.value);
  }

  async function handleBackupNow() {
    setWorking(true);
    try {
      await onBackupNow().catch(() => {});
    } finally {
      setWorking(false);
    }
  }

  async function handleRestore(backup: BackupItem) {
    const message = wipe
      ? `RESTORE WITH WIPE\n\n${backup.key}\n\nEverything under the configured root will be DELETED before this backup is restored. Continue?`
      : `Restore\n\n${backup.key}\n\nMissing znodes are recreated and existing data is overwritten from the backup. Continue?`;
    if (!window.confirm(message)) return;
    setPendingRestore(backup.key);
    try {
      await onRestore(backup.key, wipe).catch(() => {});
    } finally {
      setPendingRestore(null);
    }
  }

  return (
    <section className="card">
      <div className="row">
        <h2>Backups in S3</h2>
        <div className="actions">
          <label className="wipe" title="Delete the current tree under the configured root before restoring">
            <input
              type="checkbox"
              checked={wipe}
              onChange={(event) => setWipe(event.target.checked)}
            />
            wipe on restore
          </label>
          <button className="ghost" onClick={() => settled(onRefresh())}>
            Refresh
          </button>
          <button className="primary" onClick={() => void handleBackupNow()} disabled={working}>
            Back up now
          </button>
        </div>
      </div>

      <div className="filters">
        <span>Show:</span>
        <select
          value={preset}
          onChange={(event) => void handlePreset(event)}
          title="Filter backups by creation time"
        >
          {PRESETS.map((p) => (
            <option key={p.value} value={p.value}>
              {p.label}
            </option>
          ))}
        </select>
        {preset === "custom" && (
          <>
            <input type="datetime-local" value={from} onChange={(e) => setFrom(e.target.value)} />
            &ndash;
            <input type="datetime-local" value={until} onChange={(e) => setUntil(e.target.value)} />
          </>
        )}
        {!loadError && (
          <span className="count">
            {shown.length === backups.length
              ? `${backups.length} backup${backups.length === 1 ? "" : "s"}`
              : `${shown.length} of ${backups.length} backups`}
          </span>
        )}
      </div>

      <table>
        <thead>
          <tr>
            <th>Backup</th>
            <th>Size</th>
            <th>Created (local, hover for UTC)</th>
            <th className="right">Actions</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((b) => (
            <tr key={b.key}>
              <td className="mono">{shortKey(b.key)}</td>
              <td>{fmtBytes(b.size)}</td>
              <td title={`${b.last_modified} UTC`}>
                {fmtLocal(b.last_modified)}
                <div className="age">{ageText(b.last_modified)}</div>
              </td>
              <td className="right">
                <button
                  className="restore"
                  onClick={() => void handleRestore(b)}
                  disabled={pendingRestore === b.key}
                >
                  Restore
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {loadError ? (
        <p className="empty">Failed to list backups: {loadError}</p>
      ) : (
        shown.length === 0 && (
          <p className="empty">
            {backups.length > 0
              ? "No backups match the selected time range."
              : "No backups yet. Click \u201cBack up now\u201d."}
          </p>
        )
      )}
    </section>
  );
}
