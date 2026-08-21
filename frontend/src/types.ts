export interface RetentionInfo {
  enabled: boolean;
  max_age_seconds: number;
  interval_seconds: number;
  min_keep: number;
}

export interface AppConfig {
  zk_hosts: string;
  zk_root: string;
  zk_auth_enabled: boolean;
  s3_endpoint: string;
  s3_bucket: string;
  s3_prefix: string;
  backup_interval_seconds: number;
  retention: RetentionInfo;
}

export interface BackupItem {
  key: string;
  size: number;
  last_modified: string;
}

export type JobStatus = "running" | "success" | "error";

export interface Job {
  id: string;
  kind: "backup" | "restore" | "retention";
  description: string;
  status: JobStatus;
  started_at: string;
  finished_at: string | null;
  error: string | null;
  result: Record<string, unknown> | null;
}

export interface SchedulerSnapshot {
  enabled: boolean;
  interval_seconds: number;
  last_run: string | null;
  next_run: string | null;
}

export interface StatusResponse {
  scheduler: SchedulerSnapshot;
  retention: RetentionInfo & { last_run: Record<string, unknown> | null; next_run: string | null };
  busy: boolean;
  components: Record<string, string>;
}
