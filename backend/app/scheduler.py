"""Periodic backup loop driven by ZBS_BACKUP_INTERVAL_SECONDS (0 = disabled)."""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from . import jobs
from .config import settings

log = logging.getLogger("zbs.scheduler")

_thread: threading.Thread | None = None
_stop = threading.Event()
_state_lock = threading.Lock()
_last_run: str | None = None
_next_run: str | None = None


def _wait_job(job_id: str, timeout: float = 6 * 3600) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = jobs.get_job(job_id)
        if job and job["status"] != "running":
            if job["status"] == "error":
                log.error("scheduled backup %s failed: %s", job_id, job["error"])
            else:
                log.info("scheduled backup %s finished: %s", job_id, job["result"])
            return
        time.sleep(1)


def _loop() -> None:
    global _last_run, _next_run
    interval = max(settings.backup_interval_seconds, 1)
    log.info("scheduled backups enabled, interval=%ss", interval)
    while not _stop.wait(interval):
        log.debug("scheduler tick: starting scheduled backup")
        with _state_lock:
            _next_run = None
        try:
            job = jobs.run_backup_now(trigger="schedule")
            _wait_job(job["id"])
        except Exception:  # noqa: BLE001 - the loop must survive failed runs
            log.exception("scheduled backup could not be started")
        finally:
            with _state_lock:
                _last_run = datetime.now(timezone.utc).isoformat()
                _next_run = (
                    datetime.now(timezone.utc) + timedelta(seconds=interval)
                ).isoformat()
                log.debug("scheduler sleeping until %s", _next_run)


def start_scheduler() -> None:
    global _thread, _next_run
    if settings.backup_interval_seconds <= 0:
        log.info("scheduled backups disabled (ZBS_BACKUP_INTERVAL_SECONDS=0)")
        return
    with _state_lock:
        _next_run = (
            datetime.now(timezone.utc)
            + timedelta(seconds=settings.backup_interval_seconds)
        ).isoformat()
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="zbs-scheduler", daemon=True)
    _thread.start()


def stop_scheduler() -> None:
    _stop.set()


def scheduler_snapshot() -> dict:
    with _state_lock:
        return {
            "enabled": settings.backup_interval_seconds > 0,
            "interval_seconds": settings.backup_interval_seconds,
            "last_run": _last_run,
            "next_run": _next_run,
        }
