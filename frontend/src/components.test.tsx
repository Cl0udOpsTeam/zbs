import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { BackupsCard, clusterLabel } from "./components/BackupsCard";
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

// --------------------------------------------------------------------------
// helpers
// --------------------------------------------------------------------------

const HOUR = 3600_000;

function backup(key: string, ageMs: number): BackupItem {
  return { key, size: 1024, last_modified: new Date(Date.now() - ageMs).toISOString() };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

const noopAsync = async () => {};

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

function makeJob(overrides: Partial<Job> = {}): Job {
  return {
    id: "j1",
    kind: "backup",
    description: "Backup /",
    status: "success",
    started_at: "2024-05-01T12:00:00Z",
    finished_at: "2024-05-01T12:00:01Z",
    error: null,
    result: { nodes: 7, key: "zbs/a.json.gz", bytes: 2048 },
    ...overrides,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

// --------------------------------------------------------------------------
// Header
// --------------------------------------------------------------------------

describe("Header", () => {
  it("renders configuration chips including retention when enabled", () => {
    render(
      <Header
        config={{
          ...baseConfig,
          zk_auth_enabled: true,
          retention: {
            enabled: true,
            max_age_seconds: 604800,
            interval_seconds: 21600,
            min_keep: 1,
            scope_folders: ["dev-test-my-zookeeper"],
          },
        }}
      />
    );
    expect(screen.getByText(/zk:2181/)).toBeTruthy();
    expect(screen.getByText(/\(auth\)/)).toBeTruthy();
    expect(screen.getByText(/bkt\/dev-test-my-zookeeper/)).toBeTruthy();
    expect(screen.getByText(/every 1h/)).toBeTruthy();
    expect(screen.getByText(/keep 1w/)).toBeTruthy();
  });

  it("shows retention as off when disabled and renders nothing weird without config", () => {
    const { rerender, container } = render(<Header config={baseConfig} />);
    expect(screen.getByText(/^Retentionoff$|^off$/)).toBeTruthy();
    rerender(<Header config={null} />);
    expect(container.querySelector(".chip")).toBeNull();
  });
});

// --------------------------------------------------------------------------
// BackupsCard
// --------------------------------------------------------------------------

describe("BackupsCard", () => {
  function setup(props: Partial<Parameters<typeof BackupsCard>[0]> = {}) {
    const onRefresh = vi.fn(noopAsync);
    const onBackupNow = vi.fn(noopAsync);
    const onRestore = vi.fn(noopAsync);
    const onFolderChange = vi.fn();
    render(
      <BackupsCard
        backups={props.backups ?? []}
        loadError={props.loadError ?? null}
        clusters={"clusters" in props ? props.clusters! : []}
        selectedFolder={props.selectedFolder ?? null}
        onFolderChange={onFolderChange}
        onRefresh={onRefresh}
        onBackupNow={onBackupNow}
        onRestore={onRestore}
      />
    );
    return { onRefresh, onBackupNow, onRestore, onFolderChange };
  }

  it("lists backups with name, size, creation time and restore buttons", () => {
    setup({ backups: [backup("zbs/a.json.gz", HOUR), backup("zbs/b.json.gz", 25 * HOUR)] });
    expect(screen.getByText("a.json.gz")).toBeTruthy();
    expect(screen.getByText("b.json.gz")).toBeTruthy();
    expect(screen.getAllByText("1 KB")).toHaveLength(2);
    expect(screen.getByText("2 backups")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Restore" })).toHaveLength(2);
  });

  it("shows a friendly message when there are no backups at all", () => {
    setup({});
    expect(screen.getByText(/No backups yet/)).toBeTruthy();
  });

  it("hides the cluster selector when discovery returned nothing", () => {
    setup({});
    expect(screen.queryByTitle(/Choose which cluster/)).toBeNull();
  });

  it("renders cluster options with labels and marks the backup target", () => {
    setup({ clusters, selectedFolder: "dev-test-my-zookeeper" });
    const options = screen.getByTitle(/Choose which cluster/).querySelectorAll("option");
    expect(options).toHaveLength(2);
    expect(options[0].textContent).toContain("dev/test \u00b7 my-zookeeper");
    expect(options[0].textContent).toContain("(this cluster)");
    expect(options[1].textContent).toContain("prod/payments \u00b7 zk");
  });

  it("falls back to the raw folder name when it does not match the convention", () => {
    expect(
      clusterLabel({ name: "zbs", environment: null, namespace: null, zkName: null, isBackupTarget: false })
    ).toBe("zbs");
  });

  it("reports folder changes to the parent", () => {
    const { onFolderChange } = setup({ clusters, selectedFolder: "dev-test-my-zookeeper" });
    fireEvent.change(screen.getByTitle(/Choose which cluster/), {
      target: { value: "prod-payments-zk" },
    });
    expect(onFolderChange).toHaveBeenCalledWith("prod-payments-zk");
  });

  it("distinguishes 'no match' from 'no backups' when filtering", () => {
    const old = [backup("zbs/old.json.gz", 48 * HOUR)];
    const { container } = render(
      <BackupsCard
        backups={old}
        loadError={null}
        clusters={clusters}
        selectedFolder={"dev-test-my-zookeeper"}
        onFolderChange={() => {}}
        onRefresh={noopAsync}
        onBackupNow={noopAsync}
        onRestore={noopAsync}
      />
    );
    fireEvent.change(screen.getByTitle(/Filter backups/), { target: { value: "3600" } });
    expect(container.textContent).toContain("No backups match the selected time range.");
    expect(container.textContent).toContain("0 of 1 backups");
  });

  it("surfaces listing errors instead of pretending everything is fine", () => {
    setup({ loadError: "S3 list failed: endpoint down" });
    expect(screen.getByText(/Failed to list backups: S3 list failed/)).toBeTruthy();
    expect(screen.queryByText(/No backups yet/)).toBeNull();
  });

  it("reveals custom range inputs only for the custom preset", () => {
    render(
      <BackupsCard
        backups={[]}
        loadError={null}
        clusters={clusters}
        selectedFolder={"dev-test-my-zookeeper"}
        onFolderChange={() => {}}
        onRefresh={noopAsync}
        onBackupNow={noopAsync}
        onRestore={noopAsync}
      />
    );
    expect(document.querySelectorAll('input[type="datetime-local"]')).toHaveLength(0);
    fireEvent.change(screen.getByTitle(/Filter backups/), { target: { value: "custom" } });
    expect(document.querySelectorAll('input[type="datetime-local"]')).toHaveLength(2);
  });

  it("asks for confirmation before restoring and passes wipe through", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const onRestore = vi.fn(noopAsync);
    render(
      <BackupsCard
        backups={[backup("zbs/a.json.gz", HOUR)]}
        loadError={null}
        clusters={clusters}
        selectedFolder={"dev-test-my-zookeeper"}
        onFolderChange={() => {}}
        onRefresh={noopAsync}
        onBackupNow={noopAsync}
        onRestore={onRestore}
      />
    );
    const restoreButton = screen.getByRole("button", { name: "Restore" }) as HTMLButtonElement;

    fireEvent.click(restoreButton);
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining("overwritten"));
    await waitFor(() => expect(onRestore).toHaveBeenCalledWith("zbs/a.json.gz", false));

    // enable wipe, wait until the previous request fully settled, then retry
    fireEvent.click(screen.getByRole("checkbox"));
    await waitFor(() => expect(restoreButton.disabled).toBe(false));
    fireEvent.click(restoreButton);
    await waitFor(() => expect(onRestore).toHaveBeenLastCalledWith("zbs/a.json.gz", true));
  });

  it("does not restore when the confirmation is dismissed", () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const { onRestore } = setup({ backups: [backup("zbs/a.json.gz", HOUR)] });
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    expect(onRestore).not.toHaveBeenCalled();
  });

  it("includes the wipe warning in the confirm text when wiping", () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(
      <BackupsCard
        backups={[backup("zbs/a.json.gz", HOUR)]}
        loadError={null}
        clusters={clusters}
        selectedFolder={"dev-test-my-zookeeper"}
        onFolderChange={() => {}}
        onRefresh={noopAsync}
        onBackupNow={noopAsync}
        onRestore={noopAsync}
      />
    );
    fireEvent.click(screen.getByLabelText(/wipe on restore/));
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining("DELETED"));
  });

  it("disables the restore button while its request is in flight", async () => {
    const gate = deferred<void>();
    const onRestore = vi.fn(() => gate.promise);
    render(
      <BackupsCard
        backups={[backup("zbs/a.json.gz", HOUR)]}
        loadError={null}
        clusters={clusters}
        selectedFolder={"dev-test-my-zookeeper"}
        onFolderChange={() => {}}
        onRefresh={noopAsync}
        onBackupNow={noopAsync}
        onRestore={onRestore}
      />
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    expect((screen.getByRole("button", { name: "Restore" }) as HTMLButtonElement).disabled).toBe(
      true
    );
    gate.resolve(undefined);
    await waitFor(() =>
      expect((screen.getByRole("button", { name: "Restore" }) as HTMLButtonElement).disabled).toBe(
        false
      )
    );
  });

  it("re-enables Back up now even after a failed request", async () => {
    const onBackupNow = vi.fn(async () => {
      throw new Error("boom");
    });
    render(
      <BackupsCard
        backups={[]}
        loadError={null}
        clusters={clusters}
        selectedFolder={"dev-test-my-zookeeper"}
        onFolderChange={() => {}}
        onRefresh={noopAsync}
        onBackupNow={onBackupNow}
        onRestore={noopAsync}
      />
    );
    fireEvent.click(screen.getByRole("button", { name: "Back up now" }));
    await waitFor(() =>
      expect((screen.getByRole("button", { name: "Back up now" }) as HTMLButtonElement).disabled)
        .toBe(false)
    );
  });
});

// --------------------------------------------------------------------------
// JobsCard
// --------------------------------------------------------------------------

describe("JobsCard", () => {
  it("shows loading, then empty, then populated states", () => {
    const { rerender, container } = render(<JobsCard jobs={null} />);
    expect(screen.getByText(/Loading/)).toBeTruthy();

    rerender(<JobsCard jobs={[]} />);
    expect(screen.getByText("No jobs yet.")).toBeTruthy();

    rerender(<JobsCard jobs={[makeJob()]} />);
    expect(container.textContent).toContain("Backup /");
    expect(container.textContent).toContain("success");
    expect(container.textContent).toContain("7 nodes");
  });

  it("renders failed jobs with their error message", () => {
    render(
      <JobsCard
        jobs={[
          makeJob({
            id: "j2",
            status: "error",
            error: "ChecksumMismatchError: data corrupted",
            result: null,
          }),
        ]}
      />
    );
    expect(screen.getByText(/data corrupted/)).toBeTruthy();
    expect(screen.getByText("error")).toBeTruthy();
  });

  it("keeps running jobs visually distinct", () => {
    const { container } = render(
      <JobsCard jobs={[makeJob({ status: "running", finished_at: null })]} />
    );
    expect(container.querySelector(".job.running .pill")?.textContent).toBe("running");
  });
});

// --------------------------------------------------------------------------
// StatusFooter
// --------------------------------------------------------------------------

describe("StatusFooter", () => {
  const status: StatusResponse = {
    scheduler: { enabled: true, interval_seconds: 3600, last_run: null, next_run: "2024-05-01T13:00:00+00:00" },
    retention: {
      enabled: true,
      max_age_seconds: 604800,
      interval_seconds: 21600,
      min_keep: 1,
      scope_folders: ["dev-test-my-zookeeper"],
      last_run: { at: "2024-05-01T12:00:00+00:00", deleted: 3, scanned: 10, kept: 7 },
      next_run: null,
    },
    busy: false,
    components: { zookeeper: "ok", s3: "ok" },
  };

  it("shows a placeholder before the first poll", () => {
    const { container } = render(<StatusFooter status={null} />);
    expect(container.textContent).toContain("checking status");
  });

  it("marks healthy systems ok and lists upcoming work", () => {
    const { container } = render(<StatusFooter status={status} />);
    expect(container.querySelector(".statusline.ok")).toBeTruthy();
    expect(container.textContent).toContain("zookeeper: ok");
    expect(container.textContent).toContain("next backup");
    expect(container.textContent).toContain("retention: keep 1w");
    expect(container.textContent).toContain("retention scope: dev-test-my-zookeeper");
    expect(container.textContent).toContain("(3 deleted)");
  });

  it("discloses the retention scope so shared-bucket safety is visible", () => {
    const { container } = render(
      <StatusFooter
        status={{
          ...status,
          retention: {
            ...status.retention,
            scope_folders: ["dev-test-my-zookeeper", "staging-demo-zk"],
          },
        }}
      />
    );
    expect(container.textContent).toContain(
      "retention scope: dev-test-my-zookeeper, staging-demo-zk"
    );
  });

  it("warns when any dependency degrades", () => {
    const degraded: StatusResponse = {
      ...status,
      components: { zookeeper: "error: KazooTimeoutError", s3: "ok" },
    };
    const { container } = render(<StatusFooter status={degraded} />);
    expect(container.querySelector(".statusline.warn")).toBeTruthy();
    expect(container.textContent).toContain("KazooTimeoutError");
  });
});

// --------------------------------------------------------------------------
// Toasts
// --------------------------------------------------------------------------

function Probe() {
  const notify = useNotify();
  return (
    <>
      <button onClick={() => notify("saved!")}>good</button>
      <button onClick={() => notify("it broke", true)}>bad</button>
    </>
  );
}

describe("ToastProvider", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows a toast on notify and removes it after 4 seconds", () => {
    render(
      <ToastProvider>
        <Probe />
      </ToastProvider>
    );
    fireEvent.click(screen.getByText("good"));
    expect(screen.getByText("saved!")).toBeTruthy();

    act(() => {
      vi.advanceTimersByTime(4000);
    });
    expect(screen.queryByText("saved!")).toBeNull();
  });

  it("marks error toasts with the error style", () => {
    render(
      <ToastProvider>
        <Probe />
      </ToastProvider>
    );
    fireEvent.click(screen.getByText("bad"));
    expect(document.querySelector(".toast.error")?.textContent).toBe("it broke");
  });

  it("supports several simultaneous toasts", () => {
    render(
      <ToastProvider>
        <Probe />
      </ToastProvider>
    );
    fireEvent.click(screen.getByText("good"));
    fireEvent.click(screen.getByText("bad"));
    expect(document.querySelectorAll(".toast")).toHaveLength(2);
  });
});
