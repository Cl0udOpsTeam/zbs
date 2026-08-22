import type {
  AppConfig,
  BackupListResponse,
  ClusterFolder,
  Job,
  StatusResponse,
} from "./types";

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let body: unknown = {};
  try {
    body = await response.json();
  } catch {
    /* non-JSON error body */
  }
  if (!response.ok) {
    const detail = (body as { detail?: unknown }).detail;
    throw new Error(
      typeof detail === "string"
        ? detail
        : `${response.status} ${response.statusText}`
    );
  }
  return body as T;
}

export const api = {
  getConfig: () => request<AppConfig>("/api/config"),
  getStatus: () => request<StatusResponse>("/api/status"),
  getClusters: async (): Promise<ClusterFolder[]> =>
    (await request<{ clusters: ClusterFolder[] }>("/api/clusters")).clusters,
  listBackups: async (folder?: string): Promise<BackupListResponse> => {
    const query = folder ? `?folder=${encodeURIComponent(folder)}` : "";
    return request<BackupListResponse>(`/api/backups${query}`);
  },
  createBackup: () => request<Job>("/api/backups", { method: "POST" }),
  restoreBackup: (key: string, wipe: boolean) =>
    request<Job>("/api/restore", { method: "POST", body: JSON.stringify({ key, wipe }) }),
  listJobs: async (limit = 25): Promise<Job[]> =>
    (await request<{ jobs: Job[] }>(`/api/jobs?limit=${limit}`)).jobs,
};

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
