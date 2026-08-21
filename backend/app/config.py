"""Runtime configuration, sourced entirely from environment variables.

In Kubernetes these are injected from the ZBS ConfigMap (non-secret settings)
and Secret (credentials) - see chart/zbs/templates.
"""

import logging
import os
import re

log = logging.getLogger("zbs.config")

_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_DURATION_RE = re.compile(r"(\d+)\s*([smhdw]?)")


def parse_duration(value, default=None):
    """Parse a human-friendly duration into seconds.

    Accepts plain seconds ("90"), suffixed units ("45s", "5m", "2h", "7d",
    "1w") and compounds ("1d12h"). Returns `default` for None/"".
    Raises ValueError for unparseable input.
    """
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError(f"invalid duration: {value!r}")
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    if re.fullmatch(r"\d+", text):
        return int(text)
    if not re.fullmatch(r"(?:\d+\s*[smhdw]\s*)+", text):
        raise ValueError(f"invalid duration: {value!r}")
    return sum(int(n) * _UNIT_SECONDS[unit] for n, unit in _DURATION_RE.findall(text))


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        log.warning("%s=%r is not an integer; using default %s", name, os.environ.get(name), default)
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name, "true" if default else "false")
    value = raw.lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    log.warning("invalid boolean %s=%r; using %s", name, raw, default)
    return default


def _env_duration(name: str, default: int) -> int:
    try:
        result = parse_duration(_env(name, ""), default=default)
        assert result is not None
        return result
    except (ValueError, AssertionError):
        log.warning("%s=%r is not a valid duration; using default %ss", name, os.environ.get(name), default)
        return default


class Settings:
    def __init__(self) -> None:
        # Logging / HTTP server
        self.log_level = _env("ZBS_LOG_LEVEL", "INFO").upper()
        self.listen_host = _env("ZBS_LISTEN_HOST", "0.0.0.0")
        self.listen_port = self._clamp_int(
            _env_int("ZBS_LISTEN_PORT", 8080), 1, 65535, "ZBS_LISTEN_PORT", 8080
        )

        # ZooKeeper
        self.zk_hosts = _env("ZBS_ZK_HOSTS", "localhost:2181")
        root = _env("ZBS_ZK_ROOT", "/")
        if not root.startswith("/"):
            log.warning("ZBS_ZK_ROOT=%r does not start with '/'; prepending it", root)
            root = "/" + root
        self.zk_root = root.rstrip("/") or "/"
        self.zk_session_timeout = max(_env_int("ZBS_ZK_SESSION_TIMEOUT", 15), 1)
        # Secrets are read raw (no stripping) so values may contain whitespace.
        self.zk_username = os.environ.get("ZBS_ZK_USERNAME") or None
        self.zk_password = os.environ.get("ZBS_ZK_PASSWORD") or None

        # Backups ("3600" and "1h" both mean one hour).
        self.backup_interval_seconds = self._clamp_int(
            _env_duration("ZBS_BACKUP_INTERVAL_SECONDS", 0), 0, None, "backup interval", 0
        )

        # Retention: delete backups older than max age; 0 disables.
        self.retention_max_age_seconds = self._clamp_int(
            _env_duration("ZBS_RETENTION_MAX_AGE", 0), 0, None, "retention max age", 0
        )
        self.retention_interval_seconds = self._clamp_int(
            _env_duration("ZBS_RETENTION_INTERVAL", 3600), 1, None, "retention interval", 3600
        )
        self.retention_min_keep = self._clamp_int(
            _env_int("ZBS_RETENTION_MIN_KEEP", 1), 0, None, "retention min keep", 1
        )

        # Restore safety rails.
        self.restore_max_depth = self._clamp_int(
            _env_int("ZBS_RESTORE_MAX_DEPTH", 256), 2, None, "restore max depth", 256
        )
        self.restore_max_nodes = self._clamp_int(
            _env_int("ZBS_RESTORE_MAX_NODES", 0), 0, None, "restore max nodes", 0
        )  # 0 = unlimited
        self.restore_acls = _env_bool("ZBS_RESTORE_ACLS", False)

        # S3
        self.s3_endpoint = os.environ.get("ZBS_S3_ENDPOINT") or None
        self.s3_bucket = _env("ZBS_S3_BUCKET", "")
        self.s3_region = _env("ZBS_S3_REGION", "us-east-1")
        prefix = _env("ZBS_S3_PREFIX", "zbs/")
        if prefix and not prefix.endswith("/"):
            log.warning("ZBS_S3_PREFIX=%r lacks trailing '/'; appending it", prefix)
            prefix += "/"
        self.s3_prefix = prefix
        self.s3_access_key_id = os.environ.get("ZBS_S3_ACCESS_KEY_ID") or None
        self.s3_secret_access_key = os.environ.get("ZBS_S3_SECRET_ACCESS_KEY") or None

    @staticmethod
    def _clamp_int(value: int, minimum: int | None, maximum: int | None, label: str, fallback: int) -> int:
        if minimum is not None and value < minimum:
            log.warning("%s below minimum (%d); using %d", label, value, minimum)
            return minimum
        if maximum is not None and value > maximum:
            log.warning("%s above maximum (%d); using %d", label, value, maximum)
            return maximum
        return value

    @property
    def zk_auth_enabled(self) -> bool:
        return bool(self.zk_username and self.zk_password)

    def describe(self) -> dict[str, str]:
        """Non-secret effective settings, safe for the startup banner."""
        return {
            "log level": self.log_level,
            "listen": f"{self.listen_host}:{self.listen_port}",
            "zookeeper": self.zk_hosts,
            "zk root": self.zk_root,
            "zk auth": "enabled" if self.zk_auth_enabled else "disabled",
            "backup interval": f"{self.backup_interval_seconds}s" + (" (disabled)" if not self.backup_interval_seconds else ""),
            "retention": (
                f"delete after {self.retention_max_age_seconds}s, sweep every {self.retention_interval_seconds}s, min keep {self.retention_min_keep}"
                if self.retention_max_age_seconds
                else "disabled"
            ),
            "s3 endpoint": self.s3_endpoint or "AWS S3",
            "s3 bucket": self.s3_bucket or "(not configured)",
            "s3 prefix": self.s3_prefix,
            "restore acls": str(self.restore_acls),
            "restore limits": f"max depth {self.restore_max_depth}, max nodes {'unlimited' if not self.restore_max_nodes else self.restore_max_nodes}",
        }


settings = Settings()
