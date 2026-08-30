"""Duration parser + Settings parsing, clamping and normalization."""

import logging

import pytest

from app.config import Settings, parse_duration


class TestParseDuration:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("90", 90),
            (" 90 ", 90),
            ("45s", 45),
            ("5m", 300),
            ("2h", 7200),
            ("24h", 86400),
            ("7d", 604800),
            ("1w", 604800),
            ("2w", 1209600),
            ("1d12h", 129600),
            ("1d 12h", 129600),
            ("0", 0),
            ("0s", 0),
            (42, 42),
            ("", None),
            (None, None),
        ],
    )
    def test_valid(self, value, expected):
        if expected is None:
            assert parse_duration(value, default=expected) == expected
        else:
            assert parse_duration(value) == expected

    def test_default_used_for_empty(self):
        assert parse_duration("", default=99) == 99
        assert parse_duration(None, default=99) == 99

    @pytest.mark.parametrize("value", ["banana", "12x", "-5m", "1.5h", "h", "m5", "1d1x"])
    def test_invalid_raises(self, value):
        with pytest.raises(ValueError):
            parse_duration(value)

    def test_bool_rejected(self):
        with pytest.raises(ValueError):
            parse_duration(True)


class TestSettings:
    def test_defaults(self, make_settings):
        s = make_settings()
        assert s.zk_hosts == "localhost:2181"
        assert s.zk_root == "/"
        assert s.backup_interval_seconds == 0
        assert s.retention_max_age_seconds == 0
        assert s.retention_min_keep == 1
        assert s.restore_acls is False
        assert s.s3_prefix == "zbs/"
        assert s.listen_port == 8080

    def test_zk_root_normalization(self):
        for raw, expected in [("app", "/app"), ("/app", "/app"), ("/app/", "/app"), ("/", "/")]:
            s = Settings.__new__(Settings)
            import os

            os.environ["ZBS_ZK_ROOT"] = raw
            try:
                s = Settings()
                assert s.zk_root == expected, raw
            finally:
                del os.environ["ZBS_ZK_ROOT"]

    def test_s3_prefix_gets_trailing_slash(self, make_settings):
        s = make_settings(ZBS_S3_PREFIX="backups")
        assert s.s3_prefix == "backups/"

    def test_durations_accept_units(self, make_settings):
        s = make_settings(
            ZBS_BACKUP_INTERVAL_SECONDS="1h",
            ZBS_RETENTION_MAX_AGE="7d",
            ZBS_RETENTION_INTERVAL="6h",
        )
        assert s.backup_interval_seconds == 3600
        assert s.retention_max_age_seconds == 604800
        assert s.retention_interval_seconds == 21600

    def test_bad_values_fall_back_to_safe_defaults(self, make_settings):
        """Every invalid value warns AND lands on a safe effective value."""
        records = []
        cfg_logger = logging.getLogger("zbs.config")

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = Capture(level=logging.WARNING)
        cfg_logger.addHandler(handler)
        try:
            s = make_settings(
                ZBS_BACKUP_INTERVAL_SECONDS="weekly",
                ZBS_RETENTION_MIN_KEEP="lots",
                ZBS_RESTORE_MAX_DEPTH="-3",
                ZBS_LISTEN_PORT="http",
                ZBS_RESTORE_ACLS="maybe",
            )
        finally:
            cfg_logger.removeHandler(handler)

        assert s.backup_interval_seconds == 0
        assert s.retention_min_keep == 1
        assert s.restore_max_depth == 2  # clamped to the minimum rail
        assert s.listen_port == 8080
        assert s.restore_acls is False
        warnings = [r for r in records if r.levelno >= logging.WARNING]
        assert len(warnings) >= 4, f"expected warnings for each bad value, got {records}"

    def test_clamping(self, make_settings):
        s = make_settings(ZBS_LISTEN_PORT="99999", ZBS_RETENTION_MIN_KEEP="-2")
        assert s.listen_port == 65535
        assert s.retention_min_keep == 0

    def test_describe_never_contains_secrets(self, make_settings):
        s = make_settings(
            ZBS_S3_ACCESS_KEY_ID="AKIASECRET",
            ZBS_S3_SECRET_ACCESS_KEY="supersecret",
            ZBS_ZK_PASSWORD="hunter2",
        )
        blob = repr(s.describe())
        assert "AKIASECRET" not in blob
        assert "supersecret" not in blob
        assert "hunter2" not in blob

    def test_s3_tls_defaults(self, make_settings):
        s = make_settings()
        assert s.s3_verify_ssl is True
        assert s.s3_ca_bundle is None

    @pytest.mark.parametrize("raw", ["false", "0", "no", "off"])
    def test_s3_verify_ssl_can_be_disabled(self, make_settings, raw):
        s = make_settings(ZBS_S3_VERIFY_SSL=raw)
        assert s.s3_verify_ssl is False

    @pytest.mark.parametrize("raw", ["true", "1", "yes", "on"])
    def test_s3_verify_ssl_defaults_to_enabled(self, make_settings, raw):
        s = make_settings(ZBS_S3_VERIFY_SSL=raw)
        assert s.s3_verify_ssl is True

    def test_s3_ca_bundle_parsed(self, make_settings):
        s = make_settings(ZBS_S3_CA_BUNDLE="/etc/ssl/zbs/ca-bundle.pem")
        assert s.s3_ca_bundle == "/etc/ssl/zbs/ca-bundle.pem"

    def test_s3_ca_bundle_empty_is_none(self, make_settings):
        s = make_settings(ZBS_S3_CA_BUNDLE="   ")
        assert s.s3_ca_bundle is None

    def test_describe_includes_tls_state(self, make_settings):
        s = make_settings(
            ZBS_S3_VERIFY_SSL="false",
            ZBS_S3_CA_BUNDLE="/etc/ssl/zbs/ca-bundle.pem",
        )
        blob = repr(s.describe())
        assert "s3 tls verify" in blob
        assert "off" in blob
        assert "/etc/ssl/zbs/ca-bundle.pem" in blob
