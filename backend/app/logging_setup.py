"""Central logging configuration for ZBS.

Log level is chosen with the ZBS_LOG_LEVEL environment variable
(DEBUG, INFO, WARNING, ERROR, CRITICAL - default INFO). An invalid value
falls back to INFO with a warning instead of refusing to start.
"""

import logging
import os
import sys

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
VALID_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
DEFAULT_LEVEL = "INFO"

# Libraries that are very chatty at DEBUG; quiet them unless the operator
# explicitly asked for DEBUG.
_NOISY_LOGGERS = ("botocore", "urllib3", "kazoo")


def resolve_level(name: str | None) -> tuple[str, int]:
    """Map a level name to (resolved_name, numeric_level), falling back safely."""
    requested = (name or "").strip().upper()
    if not requested:
        return DEFAULT_LEVEL, logging.getLevelName(DEFAULT_LEVEL)
    if requested in VALID_LEVELS:
        return requested, logging.getLevelName(requested)
    sys.stderr.write(
        f"zbs: invalid ZBS_LOG_LEVEL={name!r}; "
        f"valid levels: {', '.join(VALID_LEVELS)}. Using {DEFAULT_LEVEL}.\n"
    )
    return DEFAULT_LEVEL, logging.getLevelName(DEFAULT_LEVEL)


def setup_logging(level_name: str | None = None) -> str:
    """Configure the 'zbs' logger tree. Idempotent; returns the level used."""
    resolved, numeric = resolve_level(
        level_name if level_name is not None else os.environ.get("ZBS_LOG_LEVEL")
    )

    logger = logging.getLogger("zbs")
    logger.setLevel(numeric)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(handler)
    logger.propagate = False

    # Third-party chatter is only useful when someone is actually debugging.
    third_party_level = logging.DEBUG if resolved == "DEBUG" else logging.WARNING
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(third_party_level)

    logger.debug("logging configured at level %s", resolved)
    return resolved


def log_banner(settings_summary: dict) -> None:
    """Emit one INFO line per configured area (secrets never appear here)."""
    log = logging.getLogger("zbs.startup")
    for key, value in settings_summary.items():
        log.info("%-24s %s", key + ":", value)
