import { render } from "@testing-library/react";
import { useInterval } from "./hooks";

function setVisibility(state: DocumentVisibilityState) {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => state,
  });
}

function Harness({ cb }: { cb: () => void }) {
  useInterval(cb, 50);
  return null;
}

describe("useInterval visibility gating", () => {
  afterEach(() => {
    vi.useRealTimers();
    setVisibility("visible");
  });

  it("ticks while visible, pauses while hidden, catches up on re-visible", async () => {
    vi.useFakeTimers();
    setVisibility("visible");
    // Callback identity churns every render; useInterval must cope via its ref.
    const calls = vi.fn();
    let flip = false;
    const churning = () => {
      flip = !flip;
      calls();
    };
    render(<Harness cb={churning} />);
    void flip;

    // Visible: several ticks at 50ms cadence.
    await vi.advanceTimersByTimeAsync(160);
    const afterVisible = calls.mock.calls.length;
    expect(afterVisible).toBeGreaterThanOrEqual(3);

    // Hidden: interval stops.
    setVisibility("hidden");
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(300);
    expect(calls.mock.calls.length).toBe(afterVisible);

    // Re-visible: one immediate catch-up call, then the cadence resumes.
    setVisibility("visible");
    document.dispatchEvent(new Event("visibilitychange"));
    expect(calls.mock.calls.length).toBe(afterVisible + 1);
    await vi.advanceTimersByTimeAsync(120);
    expect(calls.mock.calls.length).toBeGreaterThan(afterVisible + 1);
  });
});
