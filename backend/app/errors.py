"""Typed exceptions shared across ZBS.

Keeping a small hierarchy lets the API layer translate failures into honest
status codes and lets tests assert on specific failure modes.
"""


class ZbsError(Exception):
    """Base class for expected, user-facing failures."""


class ConfigurationError(ZbsError):
    """Mandatory configuration is missing or invalid."""


# --- backups ---------------------------------------------------------------


class BackupFormatError(ZbsError):
    """The artifact is not a ZBS backup at all (truncated, not gzip, ...)."""


class BackupCorruptedError(ZbsError):
    """The artifact parsed but failed its integrity check (checksum mismatch)."""


class BackupValidationError(ZbsError):
    """The document decoded but its structure/content is unusable or unsafe."""


class UnsupportedBackupError(ZbsError):
    """The artifact was produced by an incompatible version."""


class BackupNotFoundError(ZbsError):
    """The requested backup object does not exist in the bucket."""


# --- dependencies ----------------------------------------------------------


class ZooKeeperError(ZbsError):
    """ZooKeeper operation failed (auth, permissions, unexpected state)."""


class ZooKeeperUnavailableError(ZooKeeperError):
    """Could not reach the ZooKeeper ensemble at all."""


class S3Error(ZbsError):
    """S3 operation failed for a reason other than connectivity."""


class S3UnavailableError(S3Error):
    """Could not reach the S3 endpoint at all."""
