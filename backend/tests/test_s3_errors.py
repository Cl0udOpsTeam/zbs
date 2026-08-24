"""S3 layer: listing, prefix guards, batched deletes, typed error translation."""

from datetime import datetime, timedelta, timezone

import botocore.exceptions
import pytest

from app import errors, s3 as s3_module
from conftest import FakeS3

pytestmark = pytest.mark.usefixtures("_restore_settings")


@pytest.fixture
def bucket(settings_snapshot=None):
    s3_module.settings.s3_bucket = "test-bucket"
    s3_module.settings.s3_prefix = "zbs/"
    fake = FakeS3()
    s3_module._client = fake
    return fake


class TestListing:
    def test_filters_and_sorts_newest_first(self, bucket):
        old = {"body": b"old", "last_modified": datetime(2024, 1, 1, tzinfo=timezone.utc)}
        new = {"body": b"new", "last_modified": datetime(2024, 6, 1, tzinfo=timezone.utc)}
        bucket.objects = {
            "zbs/a.json.gz": new,
            "zbs/b.json.gz": old,
            "zbs/not-a-backup.txt": {"body": b"x", "last_modified": new["last_modified"]},
            "other/c.json.gz": {"body": b"x", "last_modified": new["last_modified"]},
        }
        items = s3_module.list_backups()
        assert [i["key"] for i in items] == ["zbs/a.json.gz", "zbs/b.json.gz"]
        assert all(i["last_modified"].endswith("+00:00") for i in items)

    def test_pagination_across_pages(self, bucket):
        for i in range(7):  # FakeS3 page size is 2 -> 4 pages
            bucket.objects[f"zbs/b{i}.json.gz"] = {
                "body": bytes([i]),
                "last_modified": datetime(2024, 1, 1 + i, tzinfo=timezone.utc),
            }
        items = s3_module.list_backups()
        assert len(items) == 7
        assert len(bucket.paginate_calls) == 1
        assert bucket.paginate_calls[0]["Prefix"] == "zbs/"


class TestPrefixGuard:
    def test_download_rejects_traversal_and_non_artifacts(self, bucket):
        for bad in ("../escape.json.gz", "folder/not-a-backup.txt", ""):
            with pytest.raises(ValueError):
                s3_module.download_backup(bad)

    def test_empty_key_rejected(self, bucket):
        with pytest.raises(ValueError):
            s3_module.validate_key("")

    def test_auto_discovery_accepts_wellformed_folders(self, bucket):
        # no allow-list configured -> any well-formed single-segment folder
        s3_module.validate_key("dev-test-my-zookeeper/some-backup.json.gz")

    def test_allowlist_rejects_unknown_folder(self, bucket):
        s3_module.settings.s3_folders = ["dev-test-my-zookeeper"]
        with pytest.raises(ValueError, match="managed prefixes"):
            s3_module.download_backup("prod-other-zk/x.json.gz")

    def test_delete_rejects_malformed_keys(self, bucket):
        for bad in ("../escape.json.gz", "folder/notes.txt"):
            with pytest.raises(ValueError):
                s3_module.delete_backups([bad])


class TestDownloadCap:
    def test_oversized_object_rejected(self, bucket):
        """Bodies beyond max_bytes (+64KiB gzip-framing allowance) are refused."""
        from app.config import settings as app_settings

        app_settings.restore_max_bytes = 1
        bucket.objects["zbs/big.json.gz"] = {
            "body": b"x" * ((1 << 16) + 2048),
            "last_modified": datetime(2024, 1, 1, tzinfo=timezone.utc),
        }
        with pytest.raises(errors.BackupValidationError, match="maximum size"):
            s3_module.download_backup("zbs/big.json.gz")

    def test_object_within_cap_downloads(self, bucket):
        from app.config import settings as app_settings

        app_settings.restore_max_bytes = 1 << 20
        bucket.objects["zbs/ok.json.gz"] = {
            "body": b"x" * 1024,
            "last_modified": datetime(2024, 1, 1, tzinfo=timezone.utc),
        }
        assert len(s3_module.download_backup("zbs/ok.json.gz")) == 1024

    def test_cap_disabled_reads_everything(self, bucket):
        from app.config import settings as app_settings

        app_settings.restore_max_bytes = 0
        bucket.objects["zbs/ok.json.gz"] = {
            "body": b"x" * 4096,
            "last_modified": datetime(2024, 1, 1, tzinfo=timezone.utc),
        }
        assert len(s3_module.download_backup("zbs/ok.json.gz")) == 4096


class TestDeletes:
    def test_batch_over_1000_is_chunked(self, bucket):
        keys = [f"zbs/k{i}.json.gz" for i in range(2500)]
        deleted = s3_module.delete_backups(keys)
        assert sorted(deleted) == sorted(keys)
        assert len(bucket.deleted) == 2500

    def test_partial_failures_excluded_from_result(self, bucket):
        bucket.delete_errors = ["zbs/bad.json.gz"]
        deleted = s3_module.delete_backups(["zbs/good.json.gz", "zbs/bad.json.gz"])
        assert deleted == ["zbs/good.json.gz"]

    def test_empty_list_is_noop(self, bucket):
        assert s3_module.delete_backups([]) == []


class TestErrorTranslation:
    def test_endpoint_unreachable(self, bucket):
        bucket.fail_on["head_bucket"] = botocore.exceptions.EndpointConnectionError(
            endpoint_url="http://127.0.0.1:1"
        )
        with pytest.raises(errors.S3UnavailableError, match="cannot reach S3 endpoint"):
            s3_module.check_connection()

    def test_missing_credentials(self, bucket):
        bucket.fail_on["put_object"] = botocore.exceptions.NoCredentialsError()
        with pytest.raises(errors.ConfigurationError, match="credentials"):
            s3_module.upload_backup(b"data")

    def test_access_denied(self, bucket):
        bucket.fail_on["get_object"] = botocore.exceptions.ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "GetObject"
        )
        bucket.objects["zbs/x.json.gz"] = {
            "body": b"x",
            "last_modified": datetime.now(timezone.utc),
        }
        with pytest.raises(errors.ConfigurationError, match="access denied"):
            s3_module.download_backup("zbs/x.json.gz")

    def test_missing_key_maps_to_not_found(self, bucket):
        with pytest.raises(errors.BackupNotFoundError):
            s3_module.download_backup("zbs/nope.json.gz")

    def test_no_such_bucket(self, bucket):
        bucket.fail_on["list_objects"] = None
        bucket.fail_on["head_bucket"] = botocore.exceptions.ClientError(
            {"Error": {"Code": "NoSuchBucket", "Message": "gone"}}, "HeadBucket"
        )
        # head_bucket path: NoSuchBucket -> ConfigurationError
        with pytest.raises(errors.ConfigurationError, match="does not exist"):
            s3_module.check_connection()

    def test_generic_client_error(self, bucket):
        bucket.fail_on["delete_objects"] = botocore.exceptions.ClientError(
            {"Error": {"Code": "SlowDown", "Message": "slow down"}}, "DeleteObjects"
        )
        with pytest.raises(errors.S3Error, match="SlowDown"):
            s3_module.delete_backups(["zbs/x.json.gz"])

    def test_bucket_not_configured(self):
        s3_module.settings.s3_bucket = ""
        with pytest.raises(errors.ConfigurationError, match="ZBS_S3_BUCKET"):
            s3_module.client()

    def test_zbs_errors_pass_through_untouched(self, bucket):
        class Marker(errors.S3Error):
            pass

        bucket.fail_on["put_object"] = Marker("pre-translated")
        with pytest.raises(Marker):
            s3_module.upload_backup(b"data")
