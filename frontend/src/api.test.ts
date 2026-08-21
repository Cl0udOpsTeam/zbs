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
  it("parses successful JSON and unwraps list payloads", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ backups: [{ key: "k", size: 1, last_modified: "z" }] }))
    );
    await expect(api.listBackups()).resolves.toEqual([
      { key: "k", size: 1, last_modified: "z" },
    ]);
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
