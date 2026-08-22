import { useCallback, useEffect, useState } from "react";
import { api, errorMessage } from "./api";
import { useInterval } from "./hooks";
import { BackupsCard } from "./components/BackupsCard";
import { Header } from "./components/Header";
import { JobsCard } from "./components/JobsCard";
import { StatusFooter } from "./components/StatusFooter";
import { ToastProvider, useNotify } from "./toast";
import type {
  AppConfig,
  BackupItem,
  ClusterFolder,
  Job,
  StatusResponse,
} from "./types";

function App() {
  const notify = useNotify();
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [clusters, setClusters] = useState<ClusterFolder[] | null>(null);
  const [selectedFolder, setSelectedFolder] = useState<string | null>(null);
  const [backups, setBackups] = useState<BackupItem[]>([]);
  const [backupsError, setBackupsError] = useState<string | null>(null);
  const [jobs, setJobs] = useState<Job[] | null>(null);

  // Pick a default cluster once both the config and the discovery result are in.
  useEffect(() => {
    if (selectedFolder || !clusters) return;
    const target = config?.backup_target_folder;
    if (target && clusters.some((c) => c.name === target)) {
      setSelectedFolder(target);
    } else if (clusters.length > 0) {
      setSelectedFolder(clusters[0].name);
    }
  }, [clusters, config, selectedFolder]);

  const loadBackups = useCallback(async () => {
    try {
      const data = await api.listBackups(selectedFolder ?? undefined);
      setBackups(data.backups);
      setBackupsError(null);
    } catch (error) {
      setBackups([]);
      setBackupsError(errorMessage(error));
    }
  }, [selectedFolder]);

  const loadClusters = useCallback(async () => {
    try {
      setClusters(await api.getClusters());
    } catch (error) {
      setClusters([]);
      notify(`Failed to list clusters: ${errorMessage(error)}`, true);
    }
  }, [notify]);

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
    void loadClusters();
    void refreshJobs();
    void loadStatus();
  }, [loadClusters, refreshJobs, loadStatus]);

  // Reload backups when the selected cluster changes.
  useEffect(() => {
    void loadBackups();
  }, [loadBackups]);

  // Polling. The backup list callback identity changes with the selected
  // folder, which also re-arms this interval after switching clusters.
  useInterval(loadBackups, 30000);
  useInterval(refreshJobs, 2500);
  useInterval(loadStatus, 5000);

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
          clusters={clusters ?? []}
          selectedFolder={selectedFolder}
          onFolderChange={setSelectedFolder}
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
