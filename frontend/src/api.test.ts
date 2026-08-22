import { api, errorMessage } from "./api";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("api client", () => {
  it("parses successful JSON and returns folder-scoped backup lists", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      jsonResponse({
        backups: [{ key: "dev-test-my-zookeeper/k.json.gz", size: 1, last_modified: "z" }],
        folder: "dev-test-my-zookeeper",
      })
    );
    vi.stubGlobal("fetch", fetchMock);
    const result = await api.listBackups("dev-test-my-zookeeper");
    expect(result.backups[0].key).toContain("dev-test-my-zookeeper");
    expect(result.folder).toBe("dev-test-my-zookeeper");
    const [path] = fetchMock.mock.calls[0];
    expect(String(path)).toBe("/api/backups?folder=dev-test-my-zookeeper");
  });

  it("requests the default scope when no folder is given", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      jsonResponse({ backups: [], folder: "zbs" })
    );
    vi.stubGlobal("fetch", fetchMock);
    await api.listBackups();
    const [path] = fetchMock.mock.calls[0];
    expect(String(path)).toBe("/api/backups");
  });

  it("unwraps cluster discovery payloads", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({
          clusters: [
            {
              name: "dev-test-my-zookeeper",
              environment: "dev",
              namespace: "test",
              zkName: "my-zookeeper",
              isBackupTarget: true,
            },
            { name: "zbs", environment: null, namespace: null, zkName: null, isBackupTarget: false },
          ],
        })
      )
    );
    const clusters = await api.getClusters();
    expect(clusters).toHaveLength(2);
    expect(clusters[0].zkName).toBe("my-zookeeper");
    expect(clusters[0].isBackupTarget).toBe(true);
  });

  it("sends JSON content type and body on restore", async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      jsonResponse({ id: "j" })
    );
    vi.stubGlobal("fetch", fetchMock);
    await api.restoreBackup("zbs/a.json.gz", true);
    const [path, init] = fetchMock.mock.calls[0];
    expect(String(path)).toBe("/api/restore");
    expect(init?.method).toBe("POST");
    expect(init?.body).toBe(JSON.stringify({ key: "zbs/a.json.gz", wipe: true }));
  });

  it("surfaces string detail fields from error bodies", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ detail: "another job is running" }, 409))
    );
    await expect(api.createBackup()).rejects.toThrow("another job is running");
  });

  it("falls back to status text for non-string details (e.g. validation arrays)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({ detail: [{ loc: ["key"], msg: "required" }] }, 422)
      )
    );
    await expect(api.restoreBackup("", false)).rejects.toThrow(/422/);
  });

  it("handles non-JSON success bodies gracefully", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("ok", { status: 200 }))
    );
    await expect(api.getStatus()).resolves.toEqual({});
  });

  it("propagates network failures as errors", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("fetch failed");
      })
    );
    await expect(api.listBackups()).rejects.toThrow("fetch failed");
  });
});

describe("errorMessage", () => {
  it("unwraps Error instances and stringifies the rest", () => {
    expect(errorMessage(new Error("boom"))).toBe("boom");
    expect(errorMessage(42)).toBe("42");
    expect(errorMessage({ weird: true })).toBe("[object Object]");
  });
});
