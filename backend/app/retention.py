"""Retention sweeper: deletes backups older than ZBS_RETENTION_MAX_AGE.

Runs on its own background thread every ZBS_RETENTION_INTERVAL seconds.
Disabled when the max age is 0. The newest ZBS_RETENTION_MIN_KEEP backups
are always preserved, even if expired.
"""

import logging
import threading
from datetime import datetime, timedelta, timezone

from . import s3
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
    """One retention pass over the bucket; returns a summary for logs/UI."""
    items = s3.list_backups()
    now = datetime.now(timezone.utc)
    victims = select_victims(
        items, now, settings.retention_max_age_seconds, settings.retention_min_keep
    )
    deleted = s3.delete_backups(victims) if victims else []
    summary = {
        "trigger": trigger,
        "scanned": len(items),
        "deleted": len(deleted),
        "kept": len(items) - len(deleted),
    }
    log.info("retention sweep finished: %s", summary)
    return {"at": now.isoformat(), **summary}


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


def stop_retention() -> None:
    _stop.set()


def retention_snapshot() -> dict:
    with _state_lock:
        return {
            "enabled": settings.retention_max_age_seconds > 0,
            "max_age_seconds": settings.retention_max_age_seconds,
            "interval_seconds": settings.retention_interval_seconds,
            "min_keep": settings.retention_min_keep,
            "last_run": dict(_last_run) if _last_run else None,
            "next_run": _next_run,
        }
