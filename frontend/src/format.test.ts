import {
  ageText,
  fmtBytes,
  fmtDuration,
  fmtInterval,
  fmtLocal,
  fmtWhen,
  humanizeSeconds,
  shortKey,
  summarizeJob,
} from "./format";
import type { Job } from "./types";

function makeJob(overrides: Partial<Job> = {}): Job {
  return {
    id: "j1",
    kind: "backup",
    description: "Backup /",
    status: "success",
    started_at: "2024-05-01T12:00:00Z",
    finished_at: null,
    error: null,
    result: null,
    ...overrides,
  };
}

describe("fmtBytes", () => {
  it.each([
    [0, "0 B"],
    [1, "1 B"],
    [1023, "1023 B"],
    [1024, "1 KB"],
    [1536, "1.5 KB"],
    [1048576, "1 MB"],
    [5 * 1024 ** 3, "5 GB"],
    [1024 ** 4, "1 TB"],
    [null, "\u2014"],
    [undefined, "\u2014"],
  ])("formats %p", (input, expected) => {
    expect(fmtBytes(input as number | null | undefined)).toBe(expected);
  });
});

describe("fmtWhen / fmtLocal / ageText", () => {
  it("fmtWhen renders UTC-ish compact form", () => {
    expect(fmtWhen("2024-05-01T12:34:56Z")).toBe("2024-05-01 12:34:56");
  });

  it("fmtWhen handles null/undefined/garbage", () => {
    expect(fmtWhen(null)).toBe("\u2014");
    expect(fmtWhen(undefined)).toBe("\u2014");
    expect(fmtWhen("not-a-date")).toBe("not-a-date");
  });

  it("fmtLocal returns a string for valid dates and dash for garbage", () => {
    expect(fmtLocal("2024-05-01T12:00:00Z")).not.toBe("\u2014");
    expect(fmtLocal("garbage")).toBe("\u2014");
  });

  it("ageText buckets by elapsed time", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2024-05-01T12:00:00Z"));
    expect(ageText("2024-05-01T11:59:45Z")).toBe("just now");
    expect(ageText("2024-05-01T11:30:00Z")).toBe("30m ago");
    expect(ageText("2024-05-01T06:00:00Z")).toBe("6h ago");
    expect(ageText("2024-04-25T12:00:00Z")).toBe("6d ago");
    vi.useRealTimers();
  });
});

describe("humanizeSeconds / fmtInterval", () => {
  it.each([
    [0, "off"],
    [59, "59s"],
    [60, "1m"],
    [3599, "3599s"],
    [3600, "1h"],
    [86399, "86399s"],
    [86400, "1d"],
    [604800, "1w"],
    [1209600, "2w"],
    [3660, "61m"],
  ])("humanizes %p", (seconds, expected) => {
    expect(humanizeSeconds(seconds)).toBe(expected);
  });

  it("fmtInterval disables at zero", () => {
    expect(fmtInterval(0)).toBe("disabled");
    expect(fmtInterval(3600)).toBe("every 1h");
  });
});

describe("shortKey", () => {
  it("returns the last path segment", () => {
    expect(shortKey("zbs/backups/zbs-20240501T000000Z.json.gz")).toBe(
      "zbs-20240501T000000Z.json.gz"
    );
    expect(shortKey("plain.json.gz")).toBe("plain.json.gz");
    expect(shortKey("trailing/")).toBe("");
  });
});

describe("fmtDuration", () => {
  it("uses ms below one second and seconds above", () => {
    expect(
      fmtDuration(makeJob({ started_at: "2024-05-01T12:00:00Z", finished_at: "2024-05-01T12:00:00.500Z" }))
    ).toBe("500ms");
    expect(
      fmtDuration(makeJob({ started_at: "2024-05-01T12:00:00Z", finished_at: "2024-05-01T12:00:02.400Z" }))
    ).toBe("2.4s");
  });

  it("is empty while unfinished or with broken/negative timestamps", () => {
    expect(fmtDuration(makeJob())).toBe("");
    expect(fmtDuration(makeJob({ started_at: "", finished_at: "nope" }))).toBe("");
    expect(
      fmtDuration(makeJob({ started_at: "2024-05-01T12:00:02Z", finished_at: "2024-05-01T12:00:00Z" }))
    ).toBe("");
  });
});

describe("summarizeJob", () => {
  it("summarizes backups", () => {
    const job = makeJob({ result: { nodes: 7, key: "zbs/a.json.gz", bytes: 2048 } });
    expect(summarizeJob(job)).toBe("7 nodes \u2192 a.json.gz (2 KB)");
  });

  it("summarizes restores including wipe deletions", () => {
    expect(
      summarizeJob(makeJob({ kind: "restore", result: { created: 1, updated: 2 } }))
    ).toBe("created 1, updated 2");
    expect(
      summarizeJob(
        makeJob({ kind: "restore", result: { created: 1, updated: 2, wipe: true, deleted_on_wipe: 3 } })
      )
    ).toBe("created 1, updated 2, deleted 3");
  });

  it("summarizes retention sweeps", () => {
    expect(
      summarizeJob(makeJob({ kind: "retention", result: { scanned: 9, deleted: 2, kept: 7 } }))
    ).toBe("scanned 9, deleted 2, kept 7");
  });

  it("tolerates missing/null results and unknown kinds", () => {
    expect(summarizeJob(makeJob({ result: null }))).toBe("0 nodes \u2192  (0 B)");
    expect(summarizeJob(makeJob({ kind: "mystery" as Job["kind"] }))).toBe("");
  });
});
