"""Minimal Prometheus metrics registry (text exposition format 0.0.4).

Deliberately dependency-free: ZBS needs a handful of counters and gauges,
and a lock-guarded dict keeps the module offline-testable like everything
else in this codebase. Rendered at GET /metrics.

Cardinality rule: label VALUES must come from small fixed sets (job kind,
job result, HTTP method, route template, status class). Never attach
high-cardinality values (keys, folders, ids) as labels.
"""

import threading

_lock = threading.Lock()
# name -> {"type": str, "help": str, "values": {labels_key: float}}
_series: dict[str, dict] = {}

# Metric names used across the codebase.
JOBS_TOTAL = "zbs_jobs_total"
JOB_LAST_DURATION = "zbs_last_job_duration_seconds"
BACKUP_PAYLOAD_BYTES = "zbs_backup_payload_bytes_total"
RETENTION_DELETED_TOTAL = "zbs_retention_deleted_total"
SCHEDULER_TIMEOUTS_TOTAL = "zbs_scheduler_timeouts_total"
HTTP_REQUESTS_TOTAL = "zbs_http_requests_total"
ENGINE_BUSY = "zbs_engine_busy"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _label_key(labels: dict[str, str] | None) -> str:
    if not labels:
        return ""
    return "{" + ",".join(
        f'{key}="{_escape_label(str(labels[key]))}"' for key in sorted(labels)
    ) + "}"


def inc(name: str, help_text: str, labels: dict[str, str] | None = None, amount: float = 1.0) -> None:
    """Increment a counter series (created on first use)."""
    key = _label_key(labels)
    with _lock:
        entry = _series.setdefault(
            name, {"type": "counter", "help": help_text, "values": {}}
        )
        entry["values"][key] = entry["values"].get(key, 0.0) + amount


def gauge_set(
    name: str, help_text: str, value: float, labels: dict[str, str] | None = None
) -> None:
    """Set a gauge series to an absolute value."""
    key = _label_key(labels)
    with _lock:
        entry = _series.setdefault(
            name, {"type": "gauge", "help": help_text, "values": {}}
        )
        entry["values"][key] = float(value)


def render() -> str:
    """Serialize all series in Prometheus text exposition format 0.0.4."""
    lines: list[str] = []
    with _lock:
        for name in sorted(_series):
            entry = _series[name]
            lines.append(f"# HELP {name} {entry['help']}")
            lines.append(f"# TYPE {name} {entry['type']}")
            for key in sorted(entry["values"]):
                value = entry["values"][key]
                lines.append(f"{name}{key} {_format_value(value)}")
    return ("\n".join(lines) + "\n") if lines else ""


def _format_value(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def reset_for_tests() -> None:
    with _lock:
        _series.clear()


# ---------------------------------------------------------------------------
# domain helpers - one call site per metric keeps label conventions consistent
# ---------------------------------------------------------------------------

_JOBS_HELP = "Backup/restore jobs by kind and terminal status."
_JOB_DURATION_HELP = "Wall-clock duration of the most recent job per kind."
_BACKUP_BYTES_HELP = "Compressed bytes uploaded to S3 by successful backups."
_RETENTION_HELP = "Backups deleted by the retention sweeper."
_SCHEDULER_TIMEOUTS_HELP = (
    "Scheduled jobs whose completion wait exceeded the scheduler deadline."
)
_HTTP_HELP = "HTTP requests by method, route template and status code."
_ENGINE_BUSY_HELP = "1 while a backup/restore job is running, else 0."


def record_job(kind: str, result: str, duration_seconds: float, payload_bytes: int | None = None) -> None:
    inc(JOBS_TOTAL, _JOBS_HELP, {"kind": kind, "result": result})
    gauge_set(JOB_LAST_DURATION, _JOB_DURATION_HELP, duration_seconds, {"kind": kind})
    if payload_bytes is not None:
        inc(BACKUP_PAYLOAD_BYTES, _BACKUP_BYTES_HELP, amount=payload_bytes)


def record_retention(deleted: int) -> None:
    if deleted:
        inc(RETENTION_DELETED_TOTAL, _RETENTION_HELP, amount=deleted)


def record_scheduler_timeout() -> None:
    inc(SCHEDULER_TIMEOUTS_TOTAL, _SCHEDULER_TIMEOUTS_HELP)


def record_http(method: str, path_template: str, status: int) -> None:
    # Status is bucketed to keep label cardinality bounded.
    status_class = f"{status // 100}xx" if 100 <= status < 600 else "other"
    inc(HTTP_REQUESTS_TOTAL, _HTTP_HELP, {"method": method, "path": path_template, "status": status_class})


def set_engine_busy(busy: bool) -> None:
    gauge_set(ENGINE_BUSY, _ENGINE_BUSY_HELP, 1 if busy else 0)
