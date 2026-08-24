import { useEffect, useRef } from "react";

/** True when the error came from an aborted fetch we cancelled ourselves. */
export function isAbortError(error: unknown): boolean {
  return (
    (error instanceof DOMException && error.name === "AbortError") ||
    (error instanceof Error && error.name === "AbortError")
  );
}

/**
 * setInterval with automatic cleanup and always-fresh callback.
 * Ticks are paused while the tab is hidden; becoming visible fires the
 * callback once immediately, then resumes the normal cadence.
 */
export function useInterval(callback: () => void, delayMs: number | null): void {
  const saved = useRef(callback);
  useEffect(() => {
    saved.current = callback;
  }, [callback]);
  useEffect(() => {
    if (delayMs == null) return;
    let id = 0;
    const start = () => {
      if (!id) id = window.setInterval(() => saved.current(), delayMs);
    };
    const stop = () => {
      if (id) {
        window.clearInterval(id);
        id = 0;
      }
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        saved.current();
        start();
      } else {
        stop();
      }
    };
    if (document.visibilityState === "visible") start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      stop();
    };
  }, [delayMs]);
}
