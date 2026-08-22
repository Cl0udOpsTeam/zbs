"""ZBS API server (back end).

Serves only the JSON API. The web UI is a separate application (frontend/)
whose nginx proxies /api to this service, so the browser stays same-origin.
Keep this Service internal - put your auth proxy in front of the UI instead.
"""

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import errors, jobs, retention, s3, zk
from .config import settings
from .logging_setup import log_banner, setup_logging
from .scheduler import scheduler_snapshot, start_scheduler, stop_scheduler

# Logging must be configured before any module logs anything meaningful.
RESOLVED_LOG_LEVEL = setup_logging()
log = logging.getLogger("zbs.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info("zbs api starting (version %s)", app.version)
    log_banner(settings.describe())
    start_scheduler()
    retention.start_retention()
    yield
    stop_scheduler()
    retention.stop_retention()
    log.info("zbs api stopped")


app = FastAPI(
    title="ZooKeeper Backup System (ZBS) API",
    version="0.4.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
)


@app.middleware("http")
async def _observability(request: Request, call_next):
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.critical(
            "unhandled error while serving %s %s",
            request.method,
            request.url.path,
            exc_info=True,
        )
        raise
    duration_ms = (time.perf_counter() - started) * 1000
    log.debug(
        "%s %s -> %d (%.1f ms)",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


@app.exception_handler(errors.ZbsError)
async def _zbs_error_handler(_: Request, exc: errors.ZbsError):
    """Expected failures become honest 4xx/5xx responses instead of crashes."""
    if isinstance(exc, errors.BackupNotFoundError):
        status = 404
    elif isinstance(exc, (errors.ConfigurationError, errors.BackupValidationError, errors.UnsupportedBackupError)):
        status = 400
    elif isinstance(exc, (errors.S3UnavailableError, errors.ZooKeeperUnavailableError)):
        status = 503
    else:
        status = 500
    level = logging.WARNING if status < 500 else logging.ERROR
    log.log(level, "%s: %s", type(exc).__name__, exc)
    return JSONResponse(status_code=status, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def _unhandled_error_handler(_: Request, exc: Exception):
    """Last-resort handler so clients always receive JSON, never HTML."""
    # The observability middleware already logged this at CRITICAL.
    log.error("unhandled error surfaced to client: %s", exc)
    return JSONResponse(status_code=500, content={"detail": f"internal error: {exc}"})


class RestoreRequest(BaseModel):
    key: str
    wipe: bool = False


# --------------------------------------------------------------------------
# Kubernetes probes (kept cheap and dependency-free on purpose)
# --------------------------------------------------------------------------

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    problems = []
    if not settings.s3_bucket:
        problems.append("ZBS_S3_BUCKET is not set")
    if not settings.zk_hosts:
        problems.append("ZBS_ZK_HOSTS is not set")
    for problem in problems:
        log.warning("readiness degraded: %s", problem)
    return {"status": "degraded" if problems else "ok", "problems": problems}


# --------------------------------------------------------------------------
# API consumed by the front end
# --------------------------------------------------------------------------

@app.get("/api/config")
async def api_config():
    """Non-secret runtime configuration for display in the UI."""
    return {
        "zk_hosts": settings.zk_hosts,
        "zk_root": settings.zk_root,
        "zk_auth_enabled": settings.zk_auth_enabled,
        "s3_endpoint": settings.s3_endpoint or "AWS S3",
        "s3_bucket": settings.s3_bucket,
        "s3_prefix": settings.s3_prefix,
        "backup_target_folder": settings.backup_target_folder,
        "folders_explicitly_configured": bool(settings.s3_folders),
        "backup_interval_seconds": settings.backup_interval_seconds,
        "retention": {
            "enabled": settings.retention_max_age_seconds > 0,
            "max_age_seconds": settings.retention_max_age_seconds,
            "interval_seconds": settings.retention_interval_seconds,
            "min_keep": settings.retention_min_keep,
            "scope_folders": settings.retention_scope(),
        },
    }


@app.get("/api/status")
async def api_status():
    """Deep health check of the two dependencies, used by the UI banner."""
    status = {
        "scheduler": scheduler_snapshot(),
        "retention": retention.retention_snapshot(),
        "busy": jobs.busy(),
        "components": {},
    }
    try:
        conn = zk.connect()
        try:
            conn.exists(settings.zk_root)
        finally:
            zk.close(conn)
        status["components"]["zookeeper"] = "ok"
    except Exception as exc:  # noqa: BLE001 - report, don't crash
        log.error("status probe: zookeeper unreachable: %s", exc)
        status["components"]["zookeeper"] = f"error: {type(exc).__name__}: {exc}"
    try:
        s3.check_connection()
        status["components"]["s3"] = "ok"
    except Exception as exc:  # noqa: BLE001
        log.error("status probe: s3 unreachable: %s", exc)
        status["components"]["s3"] = f"error: {type(exc).__name__}: {exc}"
    return status


@app.get("/api/clusters")
async def api_clusters():
    """Cluster folders in the bucket (ENVIRONMENT-NAMESPACE-ZOOKEEPER_NAME)."""
    try:
        return {"clusters": s3.discover_folders()}
    except errors.ConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except errors.ZbsError as exc:
        raise HTTPException(status_code=502, detail=f"S3 discovery failed: {exc}") from exc


@app.get("/api/backups")
async def api_list_backups(folder: str | None = None):
    if folder is not None:
        try:
            s3.validate_folder(folder)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        managed = set(settings.s3_folders) | {settings.backup_target_folder}
        if settings.s3_folders and folder not in managed:
            log.warning("listing refused for unknown cluster folder %r", folder)
            raise HTTPException(status_code=404, detail=f"unknown cluster folder {folder!r}")
    try:
        backups = s3.list_backups(folder)
    except errors.ConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except errors.ZbsError as exc:
        raise HTTPException(status_code=502, detail=f"S3 list failed: {exc}") from exc
    effective = folder or settings.backup_target_folder
    log.debug("serving %d backup(s) for folder %r", len(backups), effective)
    return {"backups": backups, "folder": effective}


@app.post("/api/backups", status_code=202)
async def api_create_backup():
    log.info("manual backup requested")
    try:
        return jobs.run_backup_now(trigger="manual")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/restore", status_code=202)
async def api_restore(request: RestoreRequest):
    log.info("restore requested: key=%r wipe=%s", request.key, request.wipe)
    try:
        s3.validate_key(request.key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        return jobs.run_restore(request.key, wipe=request.wipe)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/jobs")
async def api_jobs(limit: int = 25):
    clamped = min(max(limit, 1), 100)
    return {"jobs": jobs.list_jobs(limit=clamped)}


@app.get("/api/jobs/{job_id}")
async def api_job(job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        log.warning("unknown job requested: %s", job_id)
        raise HTTPException(status_code=404, detail="no such job")
    return job
