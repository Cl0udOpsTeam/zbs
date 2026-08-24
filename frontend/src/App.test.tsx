import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach } from "vitest";
import App from "./App";
import { ToastProvider } from "./toast";
import type {
  AppConfig,
  BackupItem,
  BackupListResponse,
  ClusterFolder,
  StatusResponse,
} from "./types";

// --------------------------------------------------------------------------
// api module mock (hoisted above the imports)
// --------------------------------------------------------------------------

vi.mock("./api", () => ({
  errorMessage: (error: unknown) =>
    error instanceof Error ? error.message : String(error),
  api: {
    getConfig: vi.fn(),
    getStatus: vi.fn(),
    getClusters: vi.fn(),
    listBackups: vi.fn(),
    createBackup: vi.fn(),
    restoreBackup: vi.fn(),
    listJobs: vi.fn(),
  },
}));

import { api } from "./api";

const mockedApi = vi.mocked(api);

// --------------------------------------------------------------------------
// helpers
// --------------------------------------------------------------------------

const baseConfig: AppConfig = {
  zk_hosts: "zk:2181",
  zk_root: "/",
  zk_auth_enabled: false,
  s3_endpoint: "AWS S3",
  s3_bucket: "bkt",
  s3_prefix: "zbs/",
  backup_target_folder: "dev-test-my-zookeeper",
  folders_explicitly_configured: false,
  backup_interval_seconds: 3600,
  retention: {
    enabled: false,
    max_age_seconds: 0,
    interval_seconds: 3600,
    min_keep: 1,
    scope_folders: ["dev-test-my-zookeeper"],
  },
};

const clusters: ClusterFolder[] = [
  {
    name: "dev-test-my-zookeeper",
    environment: "dev",
    namespace: "test",
    zkName: "my-zookeeper",
    isBackupTarget: true,
  },
  {
    name: "prod-payments-zk",
    environment: "prod",
    namespace: "payments",
    zkName: "zk",
    isBackupTarget: false,
  },
];

const okStatus: StatusResponse = {
  scheduler: {
    enabled: false,
    interval_seconds: 0,
    last_run: null,
    next_run: null,
  },
  retention: {
    enabled: false,
    max_age_seconds: 0,
    interval_seconds: 3600,
    min_keep: 1,
    scope_folders: ["dev-test-my-zookeeper"],
    last_run: null,
    next_run: null,
  },
  busy: false,
  components: { zookeeper: "ok", s3: "ok" },
};

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

interface RecordedCall {
  folder: string | undefined;
  d: Deferred<BackupListResponse>;
}

function backup(key: string): BackupItem {
  return { key, size: 1024, last_modified: new Date().toISOString() };
}

/** Every listBackups call returns a controllable deferred, recorded in order. */
function deferAllBackups(): RecordedCall[] {
  const calls: RecordedCall[] = [];
  mockedApi.listBackups.mockImplementation(
    (folder?: string, _signal?: AbortSignal) => {
      const entry: RecordedCall = { folder, d: deferred<BackupListResponse>() };
      calls.push(entry);
      return entry.d.promise;
    }
  );
  return calls;
}

function happyDefaults() {
  mockedApi.getConfig.mockResolvedValue(baseConfig);
  mockedApi.getClusters.mockResolvedValue(clusters);
  mockedApi.getStatus.mockResolvedValue(okStatus);
  mockedApi.listJobs.mockResolvedValue([]);
}

function renderApp() {
  return render(
    <ToastProvider>
      <App />
    </ToastProvider>
  );
}

async function flush() {
  await act(async () => {});
}

afterEach(() => {
  vi.restoreAllMocks();
});

beforeEach(() => {
  vi.resetAllMocks();
});

// --------------------------------------------------------------------------
// tests
// --------------------------------------------------------------------------

describe("App integration", () => {
  it("auto-selects the default cluster and renders its backups", async () => {
    happyDefaults();
    const calls = deferAllBackups();
    renderApp();
    await flush();

    // The mount-time load (no folder yet) plus the post-selection reload.
    expect(calls.length).toBeGreaterThanOrEqual(2);
    const selected = calls[calls.length - 1];
    expect(selected.folder).toBe("dev-test-my-zookeeper");

    selected.d.resolve({
      backups: [backup("dev-test-my-zookeeper/zbs-first.json.gz")],
      folder: "dev-test-my-zookeeper",
    });
    await flush();

    // Aborted earlier loads must never paint their result even if they land late.
    calls[0].d.resolve({
      backups: [backup("dev-test-my-zookeeper/zbs-stale-mount.json.gz")],
      folder: "",
    });
    await flush();

    expect(await screen.findByText("zbs-first.json.gz")).toBeTruthy();
    expect(screen.queryByText("zbs-stale-mount.json.gz")).toBeNull();
    expect(screen.getByText(/dev\/test \u00b7 my-zookeeper/)).toBeTruthy();
  });

  it("ignores a stale backups response that resolves after switching clusters", async () => {
    happyDefaults();
    const calls = deferAllBackups();
    renderApp();
    await flush();

    // Initial dev view.
    const initial = calls[calls.length - 1];
    initial.d.resolve({
      backups: [backup("dev-test-my-zookeeper/zbs-dev.json.gz")],
      folder: "dev-test-my-zookeeper",
    });
    await flush();
    expect(await screen.findByText("zbs-dev.json.gz")).toBeTruthy();

    const select = screen.getByTitle("Choose which cluster's backups to view");

    // Switch to prod: request goes out and stays pending.
    fireEvent.change(select, { target: { value: "prod-payments-zk" } });
    await flush();
    const prodLoad = calls[calls.length - 1];
    expect(prodLoad.folder).toBe("prod-payments-zk");

    // Switch back to dev quickly: prod request is superseded.
    fireEvent.change(select, { target: { value: "dev-test-my-zookeeper" } });
    await flush();
    const devReload = calls[calls.length - 1];
    expect(devReload).not.toBe(prodLoad);
    expect(devReload.folder).toBe("dev-test-my-zookeeper");

    // The STALE prod response arrives late - it must be ignored.
    prodLoad.d.resolve({
      backups: [backup("prod-payments-zk/zbs-prod.json.gz")],
      folder: "prod-payments-zk",
    });
    await flush();
    expect(screen.queryByText("zbs-prod.json.gz")).toBeNull();

    // The current dev response arrives and wins.
    devReload.d.resolve({
      backups: [backup("dev-test-my-zookeeper/zbs-dev-new.json.gz")],
      folder: "dev-test-my-zookeeper",
    });
    await flush();
    expect(screen.getByText("zbs-dev-new.json.gz")).toBeTruthy();
    expect(screen.queryByText("zbs-prod.json.gz")).toBeNull();
  });

  it("shows job-list unavailability instead of silently showing nothing", async () => {
    happyDefaults();
    mockedApi.listJobs.mockRejectedValue(new Error("jobs backend exploded"));
    const calls = deferAllBackups();
    renderApp();

    expect(await screen.findByText(/Job list unavailable \(retrying\)/)).toBeTruthy();
    expect(screen.getByText(/jobs backend exploded/)).toBeTruthy();

    // Backups still render while jobs are unavailable.
    calls[calls.length - 1].d.resolve({
      backups: [backup("dev-test-my-zookeeper/zbs-x.json.gz")],
      folder: "dev-test-my-zookeeper",
    });
    await flush();
    expect(await screen.findByText("zbs-x.json.gz")).toBeTruthy();
    expect(screen.getByText(/Job list unavailable \(retrying\)/)).toBeTruthy();
  });

  it("marks the footer degraded when a status poll fails, then recovers", async () => {
    vi.useFakeTimers();
    try {
      happyDefaults();
      mockedApi.getStatus
        .mockResolvedValueOnce(okStatus)
        .mockRejectedValue(new Error("boom"));
      deferAllBackups();
      renderApp();

      await act(async () => {});
      expect(screen.getByText(/zookeeper: ok/)).toBeTruthy();

      // Status polls every 5s; the next one fails.
      await vi.advanceTimersByTimeAsync(5001);
      expect(screen.getByText(/status refresh failed \(retrying\): boom/)).toBeTruthy();

      // And recovers on a later successful poll.
      mockedApi.getStatus.mockResolvedValue(okStatus);
      await vi.advanceTimersByTimeAsync(5001);
      expect(screen.getByText(/zookeeper: ok/)).toBeTruthy();
      expect(screen.queryByText(/status refresh failed/)).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});
