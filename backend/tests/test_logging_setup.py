"""Logging configuration: level selection, fallbacks, idempotency."""

import logging

import pytest

from app.logging_setup import DEFAULT_LEVEL, resolve_level, setup_logging


class TestResolveLevel:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("DEBUG", "DEBUG"),
            ("debug", "DEBUG"),
            ("Info", "INFO"),
            ("WARNING", "WARNING"),
            ("error", "ERROR"),
            ("CRITICAL", "CRITICAL"),
            (None, "INFO"),
            ("", "INFO"),
            ("banana", "INFO"),
        ],
    )
    def test_resolution(self, raw, expected):
        assert resolve_level(raw)[0] == expected

    def test_invalid_writes_warning_to_stderr(self, capsys):
        resolve_level("banana")
        err = capsys.readouterr().err
        assert "invalid ZBS_LOG_LEVEL" in err
        assert DEFAULT_LEVEL in err


class TestSetupLogging:
    def test_returns_resolved_level(self, monkeypatch):
        monkeypatch.setenv("ZBS_LOG_LEVEL", "warning")
        assert setup_logging() == "WARNING"
        assert logging.getLogger("zbs").level == logging.WARNING

    def test_invalid_env_falls_back_to_info(self, monkeypatch, capsys):
        monkeypatch.setenv("ZBS_LOG_LEVEL", "shouting")
        assert setup_logging() == "INFO"
        assert "invalid" in capsys.readouterr().err.lower()

    def test_idempotent_no_duplicate_handlers(self):
        setup_logging("INFO")
        setup_logging("INFO")
        logger = logging.getLogger("zbs")
        handlers = [h for h in logger.handlers]
        assert len(handlers) == 1

    def test_level_actually_filters_records(self, monkeypatch):
        monkeypatch.setenv("ZBS_LOG_LEVEL", "ERROR")
        setup_logging()
        log = logging.getLogger("zbs.testprobe")

        records: list[logging.LogRecord] = []
        probe_handler = logging.Handler()
        probe_handler.emit = lambda record: records.append(record)
        log.addHandler(probe_handler)
        try:
            log.debug("invisible")
            log.info("also invisible")
            log.error("visible")
        finally:
            log.removeHandler(probe_handler)

        assert [r.getMessage() for r in records] == ["visible"]

    def test_debug_enables_third_party_chatter(self, monkeypatch):
        monkeypatch.setenv("ZBS_LOG_LEVEL", "DEBUG")
        setup_logging()
        assert logging.getLogger("botocore").level == logging.DEBUG
        monkeypatch.setenv("ZBS_LOG_LEVEL", "INFO")
        setup_logging()
        assert logging.getLogger("botocore").level == logging.WARNING
