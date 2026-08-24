"""Retention sweeper: deletes backups older than ZBS_RETENTION_MAX_AGE.

Runs on its own background thread every ZBS_RETENTION_INTERVAL seconds.
Disabled when the max age is 0. The newest ZBS_RETENTION_MIN_KEEP backups
are always preserved, even if expired.
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from . import metrics, s3
from .config import settings

log = logging.getLogger("zbs.retention")

_thread: threading.Thread | None = None
_stop = threading.Event()
_state_lock = threading.Lock()
_last_run: dict | None = None
_next_run: str | None = None


def select_victims(items: list[dict], now: datetime, max_age_seconds: int, min_keep: int = 1) -> list[str]:
    """Pure helper: keys to delete from a listing sorted newest-first."""
    cutoff = now - timedelta(seconds=max_age_seconds)
    victims = []
    for item in items[min_keep:]:
        if datetime.fromisoformat(item["last_modified"]) < cutoff:
            victims.append(item["key"])
    return victims


def sweep_once(trigger: str = "schedule") -> dict:
    """One retention pass over every scoped folder; returns a UI-friendly summary."""
    started = time.monotonic()
    folders = settings.retention_scope()
    log.info(
        "retention sweep (%s) over %d folder(s): %s",
        trigger,
        len(folders),
        ", ".join(folders),
    )

    totals = {"scanned": 0, "deleted": 0, "kept": 0}
    per_folder: dict[str, dict] = {}
    for folder in folders:
        items = s3.list_backups(folder)
        victims = select_victims(
            items, datetime.now(timezone.utc), settings.retention_max_age_seconds, settings.retention_min_keep
        )
        deleted = s3.delete_backups(victims) if victims else []
        per_folder[folder] = {"scanned": len(items), "deleted": len(deleted)}
        totals["scanned"] += len(items)
        totals["deleted"] += len(deleted)
        totals["kept"] += len(items) - len(deleted)

    summary = {
        "at": datetime.now(timezone.utc).isoformat(),
        "trigger": trigger,
        **totals,
        "folders": per_folder,
        "duration_seconds": round(time.monotonic() - started, 2),
    }
    log.info("retention sweep finished: scanned=%(scanned)d deleted=%(deleted)d kept=%(kept)d", totals)
    metrics.record_retention(totals["deleted"])
    return summary


def _loop() -> None:
    global _last_run, _next_run
    interval = max(settings.retention_interval_seconds, 1)
    log.info(
        "retention enabled: delete after %ss, sweeping every %ss",
        settings.retention_max_age_seconds,
        interval,
    )
    while not _stop.wait(interval):
        with _state_lock:
            _next_run = None
        try:
            result = sweep_once(trigger="schedule")
            with _state_lock:
                _last_run = result
        except Exception:  # noqa: BLE001 - the loop must survive failed sweeps
            log.exception("retention sweep failed")
        finally:
            with _state_lock:
                _next_run = (
                    datetime.now(timezone.utc) + timedelta(seconds=interval)
                ).isoformat()


def start_retention() -> None:
    global _thread, _next_run
    if settings.retention_max_age_seconds <= 0:
        log.info("retention disabled (ZBS_RETENTION_MAX_AGE is unset or 0)")
        return
    with _state_lock:
        _next_run = (
            datetime.now(timezone.utc)
            + timedelta(seconds=max(settings.retention_interval_seconds, 1))
        ).isoformat()
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="zbs-retention", daemon=True)
    _thread.start()


def stop_retention(timeout: float = 10.0) -> None:
    global _thread
    _stop.set()
    thread = _thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
        if thread.is_alive():
            log.warning(
                "retention thread did not stop within %.1fs; abandoning it", timeout
            )
    _thread = None


def retention_snapshot() -> dict:
    with _state_lock:
        return {
            "enabled": settings.retention_max_age_seconds > 0,
            "max_age_seconds": settings.retention_max_age_seconds,
            "interval_seconds": settings.retention_interval_seconds,
            "min_keep": settings.retention_min_keep,
            "scope_folders": settings.retention_scope(),
            "last_run": dict(_last_run) if _last_run else None,
            "next_run": _next_run,
        }
