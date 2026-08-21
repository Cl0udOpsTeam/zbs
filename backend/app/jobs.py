"""In-memory job engine.

ZBS is designed to run as a single replica; job history lives in process
memory and only one backup/restore may run at a time. Backup artifacts
themselves are durable in S3, so a pod restart only loses the job list.

Restore safety order (a corrupted artifact must never touch ZooKeeper):
  1. download from S3            - may raise S3 errors
  2. deserialize (gzip+checksum) - may raise BackupCorruptedError
  3. validate_document structure - may raise BackupValidationError
  4. only now connect & mutate ZooKeeper
"""

import logging
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone

from . import s3, zk
from . import errors
from .config import settings

log = logging.getLogger("zbs.jobs")

_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_order: deque[str] = deque(maxlen=100)
_busy = threading.Lock()  # held for the whole duration of a backup/restore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def busy() -> bool:
    return _busy.locked()


def get_job(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def list_jobs(limit: int = 25) -> list[dict]:
    with _lock:
        ids = list(_order)[:limit]
        return [dict(_jobs[job_id]) for job_id in ids]


def submit(kind: str, description: str, fn) -> dict:
    """Run fn() on a worker thread; refuses when another job is active."""
    if not _busy.acquire(blocking=False):
        log.warning("rejected %s job: another job is already running", kind)
        raise RuntimeError("another backup/restore job is already running")
    job = {
        "id": uuid.uuid4().hex[:12],
        "kind": kind,
        "description": description,
        "status": "running",
        "started_at": _now(),
        "finished_at": None,
        "error": None,
        "result": None,
    }
    with _lock:
        _jobs[job["id"]] = job
        _order.appendleft(job["id"])
    log.info("job %s started: %s", job["id"], description)

    def runner() -> None:
        started = time.monotonic()
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - failures belong in job history
            duration = time.monotonic() - started
            if isinstance(exc, errors.ZbsError):
                log.error("job %s failed after %.2fs: %s", job["id"], duration, exc)
            else:
                # Not an expected failure class - this is a bug, be loud.
                log.critical(
                    "job %s crashed after %.2fs with unexpected error",
                    job["id"],
                    duration,
                    exc_info=True,
                )
            result = None
            failure = f"{type(exc).__name__}: {exc}"
        else:
            failure = None

        # Free the engine BEFORE the terminal status becomes visible: once a
        # poller observes success/error, submitting the next job must work
        # (releasing afterwards caused spurious 409s).
        _busy.release()
        job["finished_at"] = _now()
        if failure is None:
            job["status"] = "success"
            job["result"] = result
            log.info(
                "job %s succeeded in %.2fs: %s",
                job["id"],
                time.monotonic() - started,
                result,
            )
        else:
            job["status"] = "error"
            job["error"] = failure

    threading.Thread(target=runner, name=f"zbs-{kind}-{job['id']}", daemon=True).start()
    return job


# --------------------------------------------------------------------------
# the actual operations (kept synchronous + injectable for tests)
# --------------------------------------------------------------------------

def perform_backup(connect=None) -> dict:
    """Dump ZooKeeper and upload a verified archive. Raises on any failure."""
    connect = connect or zk.connect
    conn = connect()
    try:
        document = zk.dump_tree(conn, settings.zk_root)
    finally:
        zk.close(conn)
    payload = zk.serialize(document)
    key = s3.upload_backup(payload)
    return {
        "key": key,
        "bytes": len(payload),
        "nodes": zk.count_nodes(document["tree"]),
        "root": settings.zk_root,
    }


def run_backup_now(trigger: str = "manual") -> dict:
    def fn() -> dict:
        result = perform_backup()
        result["trigger"] = trigger
        return result

    return submit("backup", f"Backup {settings.zk_root} ({trigger})", fn)


def perform_restore(key: str, wipe: bool = False, connect=None) -> dict:
    """Download, verify, validate - then restore. Raises before mutating ZK."""
    raw = s3.download_backup(key)          # S3 errors surface here
    document = zk.deserialize(raw)         # gzip / JSON / checksum errors here
    expected_nodes = zk.validate_document(document)  # structure errors here

    connect = connect or zk.connect
    conn = connect()
    try:
        stats = zk.restore_tree(conn, document, wipe=wipe)
    finally:
        zk.close(conn)
    stats["key"] = key
    stats["wipe"] = wipe
    stats["expected_nodes"] = expected_nodes
    return stats


def run_restore(key: str, wipe: bool = False) -> dict:
    try:
        s3.validate_key(key)
    except ValueError as exc:
        log.warning("restore rejected for bad key %r: %s", key, exc)
        raise
    description = f"Restore {key}" + (" (wipe)" if wipe else "")
    return submit("restore", description, lambda: perform_restore(key, wipe))
