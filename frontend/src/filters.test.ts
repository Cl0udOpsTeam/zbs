import { applyTimeFilter } from "./filters";
import type { BackupItem } from "./types";

const NOW = new Date("2024-05-01T12:00:00Z").getTime();

function item(key: string, iso: string): BackupItem {
  return { key, size: 1, last_modified: iso };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

/** datetime-local inputs are interpreted in LOCAL time by browsers, so build
 * both items and input strings from local Date objects to stay TZ-agnostic. */
function localInput(year: number, month1: number, day: number, hour: number, minute: number): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${year}-${pad(month1 + 1)}-${pad(day)}T${pad(hour)}:${pad(minute)}`;
}

function isoLocal(year: number, month1: number, day: number, hour: number, minute: number): string {
  return new Date(year, month1, day, hour, minute).toISOString();
}

describe("applyTimeFilter", () => {
  const items = [
    item("fresh", "2024-05-01T11:30:00Z"), // 30m old
    item("day", "2024-04-30T11:00:00Z"), // ~25h old
    item("week", "2024-04-24T00:00:00Z"), // ~7.5d old
    item("month", "2024-04-01T00:00:00Z"), // ~30d old
    item("ancient", "2020-01-01T00:00:00Z"),
    item("broken", "not-a-date"),
    item("future", "2030-01-01T00:00:00Z"),
  ];

  it("'all' keeps everything", () => {
    expect(applyTimeFilter(items, "all")).toHaveLength(items.length);
  });

  it("hour preset drops items older than one hour but keeps future ones", () => {
    const shown = applyTimeFilter(items, "3600");
    expect(shown.map((i) => i.key)).toEqual(["fresh", "broken", "future"]);
  });

  it.each([
    ["86400", ["fresh", "broken", "future"]], // 'day' is 25h old -> out
    ["604800", ["fresh", "day", "broken", "future"]],
    ["2592000", ["fresh", "day", "week", "broken", "future"]], // month just past 30d
  ])("preset %s selects the right subset", (preset, keys) => {
    expect(applyTimeFilter(items, preset).map((i) => i.key)).toEqual(keys);
  });

  it("boundary: exactly at the cutoff is kept", () => {
    const edge = [item("edge", new Date(NOW - 3600_000).toISOString())];
    expect(applyTimeFilter(edge, "3600")).toHaveLength(1);
  });

  describe("custom range (local-time semantics like real datetime-local inputs)", () => {
    it("includes both endpoints and the whole final minute", () => {
      const shown = applyTimeFilter(
        [
          item("early", isoLocal(2024, 4, 1, 9, 59)),
          item("start", isoLocal(2024, 4, 1, 10, 0)),
          item("middle", isoLocal(2024, 4, 1, 10, 30)),
          item("end-minute", isoLocal(2024, 4, 1, 10, 59)),
          item("late", isoLocal(2024, 4, 1, 11, 1)),
        ],
        "custom",
        localInput(2024, 4, 1, 10, 0),
        localInput(2024, 4, 1, 11, 0)
      );
      expect(shown.map((i) => i.key)).toEqual(["start", "middle", "end-minute"]);
    });

    it("open-ended ranges work in either direction", () => {
      const early = [
        item("a", isoLocal(2024, 4, 1, 8, 0)),
        item("b", isoLocal(2024, 4, 1, 9, 30)),
      ];
      expect(
        applyTimeFilter(early, "custom", "", localInput(2024, 4, 1, 9, 0)).map((i) => i.key)
      ).toEqual(["a"]);

      const late = [item("c", isoLocal(2024, 4, 1, 12, 0))];
      expect(
        applyTimeFilter(late, "custom", localInput(2024, 4, 1, 10, 0), "").map((i) => i.key)
      ).toEqual(["c"]);
    });
  });

  it("empty input yields empty output for any preset", () => {
    expect(applyTimeFilter([], "all")).toEqual([]);
    expect(applyTimeFilter([], "custom", "x", "y")).toEqual([]);
  });
});
