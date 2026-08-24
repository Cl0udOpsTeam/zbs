"""Periodic backup loop driven by ZBS_BACKUP_INTERVAL_SECONDS (0 = disabled).

Cadence is *fixed*: ticks are scheduled every interval of wall-clock time,
independently of how long the previous backup ran (a slow job no longer
pushes subsequent runs later; if a slot was missed because a job overran,
the next tick fires immediately).

Shutdown is cooperative: stop_scheduler() sets the stop event and joins the
loop thread so pod termination does not silently abandon it mid-wait.
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from . import jobs, metrics
from .config import settings

log = logging.getLogger("zbs.scheduler")

_thread: threading.Thread | None = None
_stop = threading.Event()
_state_lock = threading.Lock()
_last_run: str | None = None
_next_run: str | None = None
_last_status: str | None = None

# How often _wait_job polls job completion; module-level so tests can speed
# the loop up without changing behavior.
_POLL_SECONDS = 1.0

# Upper bound on how long we watch a single scheduled job before giving up
# on waiting (the job itself keeps running; only the scheduler stops watching).
_JOB_WAIT_TIMEOUT_SECONDS = 6 * 3600


def _wait_job(job_id: str) -> str:
    """Block until the job leaves 'running'. Returns the outcome:
    'success' | 'error' | 'timeout' | 'stopped' (scheduler shut down)."""
    deadline = time.monotonic() + _JOB_WAIT_TIMEOUT_SECONDS
    while True:
        if _stop.is_set():
            log.info("scheduler stopping: no longer watching job %s", job_id)
            return "stopped"
        job = jobs.get_job(job_id)
        if job and job["status"] != "running":
            if job["status"] == "error":
                log.error("scheduled backup %s failed: %s", job_id, job["error"])
            else:
                log.info("scheduled backup %s finished: %s", job_id, job["result"])
            return job["status"]
        if time.monotonic() >= deadline:
            log.warning(
                "scheduled backup %s still running after %ds; "
                "continuing without waiting (job keeps running)",
                job_id,
                int(_JOB_WAIT_TIMEOUT_SECONDS),
            )
            metrics.record_scheduler_timeout()
            return "timeout"
        time.sleep(_POLL_SECONDS)


def _loop() -> None:
    global _last_run, _next_run, _last_status
    interval = max(settings.backup_interval_seconds, 1)
    log.info("scheduled backups enabled, interval=%ss", interval)
    next_tick = time.monotonic() + interval
    while not _stop.wait(max(0.0, next_tick - time.monotonic())):
        log.debug("scheduler tick: starting scheduled backup")
        with _state_lock:
            _next_run = None
        status = "start-failed"
        try:
            job = jobs.run_backup_now(trigger="schedule")
            status = _wait_job(job["id"])
        except Exception:  # noqa: BLE001 - the loop must survive failed runs
            log.exception("scheduled backup could not be started")
        finally:
            # Fixed cadence: advance to the next slot; if this run overran
            # one or more slots, fire the make-up tick immediately.
            while next_tick <= time.monotonic():
                next_tick += interval
            with _state_lock:
                _last_run = datetime.now(timezone.utc).isoformat()
                _last_status = status
                delay = max(0.0, next_tick - time.monotonic())
                _next_run = (
                    datetime.now(timezone.utc) + timedelta(seconds=delay)
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


def stop_scheduler(timeout: float = 10.0) -> None:
    global _thread
    _stop.set()
    thread = _thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
        if thread.is_alive():
            log.warning(
                "scheduler thread did not stop within %.1fs; abandoning it", timeout
            )
    _thread = None


def scheduler_snapshot() -> dict:
    with _state_lock:
        return {
            "enabled": settings.backup_interval_seconds > 0,
            "interval_seconds": settings.backup_interval_seconds,
            "last_run": _last_run,
            "next_run": _next_run,
            "last_status": _last_status,
        }
