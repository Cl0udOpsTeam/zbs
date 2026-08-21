import { useCallback, useEffect, useState } from "react";
import { api, errorMessage } from "./api";
import { useInterval } from "./hooks";
import { BackupsCard } from "./components/BackupsCard";
import { Header } from "./components/Header";
import { JobsCard } from "./components/JobsCard";
import { StatusFooter } from "./components/StatusFooter";
import { ToastProvider, useNotify } from "./toast";
import type { AppConfig, BackupItem, Job, StatusResponse } from "./types";

function App() {
  const notify = useNotify();
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [backups, setBackups] = useState<BackupItem[]>([]);
  const [backupsError, setBackupsError] = useState<string | null>(null);
  const [jobs, setJobs] = useState<Job[] | null>(null);

  const loadBackups = useCallback(async () => {
    try {
      setBackups(await api.listBackups());
      setBackupsError(null);
    } catch (error) {
      setBackups([]);
      setBackupsError(errorMessage(error));
    }
  }, []);

  const refreshJobs = useCallback(async () => {
    try {
      setJobs(await api.listJobs());
    } catch {
      /* transient */
    }
  }, []);

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await api.getStatus());
    } catch {
      /* transient */
    }
  }, []);

  useEffect(() => {
    api
      .getConfig()
      .then(setConfig)
      .catch((error) => notify(`Failed to load config: ${errorMessage(error)}`, true));
  }, [notify]);

  // Initial loads.
  useEffect(() => {
    void loadBackups();
    void refreshJobs();
    void loadStatus();
  }, [loadBackups, refreshJobs, loadStatus]);

  // Polling.
  useInterval(refreshJobs, 2500);
  useInterval(loadStatus, 5000);
  // Keep the backup list fresh (ages, new scheduled backups, retention deletions).
  useInterval(loadBackups, 30000);

  const handleBackupNow = useCallback(async () => {
    try {
      await api.createBackup();
      notify("Backup queued");
      void refreshJobs();
    } catch (error) {
      notify(errorMessage(error), true);
    }
  }, [notify, refreshJobs]);

  const handleRestore = useCallback(
    async (key: string, wipe: boolean) => {
      try {
        await api.restoreBackup(key, wipe);
        notify("Restore queued");
        void refreshJobs();
      } catch (error) {
        notify(errorMessage(error), true);
      }
    },
    [notify, refreshJobs]
  );

  return (
    <>
      <Header config={config} />
      <main>
        <BackupsCard
          backups={backups}
          loadError={backupsError}
          onRefresh={loadBackups}
          onBackupNow={handleBackupNow}
          onRestore={handleRestore}
        />
        <JobsCard jobs={jobs} />
      </main>
      <StatusFooter status={status} />
    </>
  );
}

export default function Root() {
  return (
    <ToastProvider>
      <App />
    </ToastProvider>
  );
}
