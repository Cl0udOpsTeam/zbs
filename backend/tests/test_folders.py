"""Multi-cluster folders: naming convention, discovery, scoping, API surface."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import errors, jobs, main, retention, s3 as s3_module
from conftest import FakeS3, FakeZK, seed_tree


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def bucket():
    s3_module.settings.s3_bucket = "test-bucket"
    fake = FakeS3()
    s3_module._client = fake
    return fake


class TestFolderNaming:
    def test_parse_convention(self):
        assert s3_module.parse_folder_name("dev-test-my-zookeeper") == {
            "environment": "dev",
            "namespace": "test",
            "zkName": "my-zookeeper",
        }

    @pytest.mark.parametrize(
        "name", ["zbs", "onlytwo-parts", "", "-leading-dash"]
    )
    def test_parse_non_conforming_returns_none(self, name):
        assert s3_module.parse_folder_name(name) is None

    def test_settings_reject_unsafe_names(self, make_settings):
        with pytest.raises(errors.ConfigurationError):
            make_settings(ZBS_S3_FOLDERS="ok-name,../escape")
        with pytest.raises(errors.ConfigurationError):
            make_settings(ZBS_CLUSTER_FOLDER="has space")

    def test_cluster_folder_must_be_in_allowlist(self, make_settings):
        with pytest.raises(errors.ConfigurationError, match="must be listed"):
            make_settings(ZBS_S3_FOLDERS="a-b-c", ZBS_CLUSTER_FOLDER="x-y-z")

    def test_backup_target_fallbacks(self, make_settings):
        assert make_settings().backup_target_folder == "zbs"  # legacy default
        assert (
            make_settings(ZBS_CLUSTER_FOLDER="dev-test-my-zookeeper").backup_target_folder
            == "dev-test-my-zookeeper"
        )
        # explicit folders without a cluster folder still target the old prefix
        assert (
            make_settings(ZBS_S3_FOLDERS="dev-test-my-zookeeper").backup_target_folder == "zbs"
        )

    def test_retention_scope_rules(self, make_settings):
        # default (legacy): own folder only
        assert make_settings().retention_scope() == ["zbs"]
        pinned = make_settings(ZBS_RETENTION_FOLDERS="dev-test-my-zookeeper")
        assert pinned.retention_scope() == ["dev-test-my-zookeeper"]

    def test_shared_bucket_deployments_never_touch_each_other(self, make_settings):
        """Regression: two deployments, one bucket. The browsing allow-list
        must NOT grant deletion rights over other clusters' folders."""
        dev = make_settings(
            ZBS_S3_FOLDERS="dev-test-my-zookeeper,staging-demo-zk",
            ZBS_CLUSTER_FOLDER="dev-test-my-zookeeper",
        )
        staging = make_settings(
            ZBS_S3_FOLDERS="dev-test-my-zookeeper,staging-demo-zk",
            ZBS_CLUSTER_FOLDER="staging-demo-zk",
        )
        assert dev.retention_scope() == ["dev-test-my-zookeeper"]
        assert staging.retention_scope() == ["staging-demo-zk"]

        # widening is only possible by explicitly opting in
        widened = make_settings(
            ZBS_RETENTION_FOLDERS="dev-test-my-zookeeper,staging-demo-zk"
        )
        assert widened.retention_scope() == ["dev-test-my-zookeeper", "staging-demo-zk"]


class TestDiscovery:
    def test_discovers_parses_and_marks_target(self, bucket):
        now = datetime.now(timezone.utc)
        for key in [
            "dev-test-my-zookeeper/zbs-1.json.gz",
            "prod-payments-zk/zbs-2.json.gz",
            "zbs/legacy.json.gz",
            "loose-file.json.gz",  # not inside a folder -> ignored
        ]:
            bucket.objects[key] = {"body": b"x", "last_modified": now}
        s3_module.settings.cluster_folder = "dev-test-my-zookeeper"

        clusters = {c["name"]: c for c in s3_module.discover_folders()}
        assert set(clusters) == {"dev-test-my-zookeeper", "prod-payments-zk", "zbs"}
        dev = clusters["dev-test-my-zookeeper"]
        assert (dev["environment"], dev["namespace"], dev["zkName"]) == ("dev", "test", "my-zookeeper")
        assert dev["isBackupTarget"] is True
        assert clusters["prod-payments-zk"]["isBackupTarget"] is False
        assert clusters["zbs"]["environment"] is None  # legacy folder has no parts

    def test_explicit_folders_appear_even_when_empty(self, bucket):
        s3_module.settings.s3_folders = ["dev-test-my-zookeeper", "staging-demo-zk"]
        clusters = [c["name"] for c in s3_module.discover_folders()]
        assert clusters == ["dev-test-my-zookeeper", "staging-demo-zk"]

    def test_dangerous_prefixes_are_ignored(self, bucket):
        now = datetime.now(timezone.utc)
        bucket.objects["weird name/x.json.gz"] = {"body": b"x", "last_modified": now}
        assert s3_module.discover_folders() == []


class TestScopedOperations:
    def test_listing_is_scoped_to_folder(self, bucket):
        now = datetime.now(timezone.utc)
        bucket.objects = {
            "dev-test-my-zookeeper/a.json.gz": {"body": b"1", "last_modified": now},
            "prod-payments-zk/b.json.gz": {"body": b"2", "last_modified": now},
        }
        items = s3_module.list_backups("dev-test-my-zookeeper")
        assert [i["key"] for i in items] == ["dev-test-my-zookeeper/a.json.gz"]
        assert bucket.paginate_calls[-1]["Prefix"] == "dev-test-my-zookeeper/"

    def test_default_listing_targets_own_folder(self, bucket):
        now = datetime.now(timezone.utc)
        bucket.objects = {
            "zbs/mine.json.gz": {"body": b"1", "last_modified": now},
            "other/theirs.json.gz": {"body": b"2", "last_modified": now},
        }
        items = s3_module.list_backups()  # None -> backup target folder
        assert [i["key"] for i in items] == ["zbs/mine.json.gz"]

    def test_upload_goes_to_backup_target(self, bucket):
        s3_module.settings.cluster_folder = "dev-test-my-zookeeper"
        key = s3_module.upload_backup(b"payload")
        assert key.startswith("dev-test-my-zookeeper/zbs-")
        assert key.endswith(".json.gz")

    def test_validate_key_matrix(self):
        # traversal and non-artifacts are always rejected
        with pytest.raises(ValueError):
            s3_module.validate_key("../traversal/x.json.gz")
        with pytest.raises(ValueError):
            s3_module.validate_key("folder/not-a-backup.txt")
        with pytest.raises(ValueError, match="must not be empty"):
            s3_module.validate_key("")

        # auto-discovery mode (no allow-list): any well-formed folder works,
        # so clusters dropped into the bucket are immediately usable
        s3_module.settings.s3_folders = []
        s3_module.validate_key("dev-test-my-zookeeper/x.json.gz")

        # explicit allow-list narrows acceptance again
        s3_module.settings.s3_folders = ["dev-test-my-zookeeper"]
        s3_module.validate_key("dev-test-my-zookeeper/x.json.gz")
        with pytest.raises(ValueError, match="managed prefixes"):
            s3_module.validate_key("prod-other-zk/x.json.gz")


class TestMultiFolderRetention:
    def test_sweep_covers_scope_and_aggregates(self, monkeypatch):
        retention.settings.retention_max_age_seconds = 86400
        retention.settings.retention_folders = ["dev-test-my-zookeeper", "prod-payments-zk"]

        calls = []

        def fake_list(folder):
            calls.append(folder)
            if folder.startswith("dev-"):
                return [
                    {"key": f"{folder}/new.json.gz", "size": 1, "last_modified": datetime.now(timezone.utc).isoformat()},
                    {"key": f"{folder}/old.json.gz", "size": 1, "last_modified": "2020-01-01T00:00:00+00:00"},
                ]
            return []  # empty prod folder

        deleted_log = []
        monkeypatch.setattr(retention.s3, "list_backups", fake_list)
        monkeypatch.setattr(
            retention.s3,
            "delete_backups",
            lambda keys: deleted_log.extend(keys) or keys,
        )
        summary = retention.sweep_once(trigger="test")

        assert calls == ["dev-test-my-zookeeper", "prod-payments-zk"]
        assert deleted_log == ["dev-test-my-zookeeper/old.json.gz"]
        assert summary["scanned"] == 2 and summary["deleted"] == 1 and summary["kept"] == 1
        assert summary["folders"]["dev-test-my-zookeeper"]["deleted"] == 1
        assert summary["folders"]["prod-payments-zk"]["scanned"] == 0


class TestClustersAPI:
    def test_discovery_endpoint(self, client, monkeypatch):
        monkeypatch.setattr(
            s3_module,
            "discover_folders",
            lambda: [{"name": "dev-test-my-zookeeper", "environment": "dev", "namespace": "test", "zkName": "my-zookeeper", "isBackupTarget": True}],
        )
        body = client.get("/api/clusters").json()
        assert body["clusters"][0]["name"] == "dev-test-my-zookeeper"
        assert body["clusters"][0]["isBackupTarget"] is True

    def test_discovery_s3_failure_maps_to_502(self, client, monkeypatch):
        def boom():
            raise errors.S3UnavailableError("endpoint down")

        monkeypatch.setattr(s3_module, "discover_folders", boom)
        response = client.get("/api/clusters")
        assert response.status_code == 502
        assert "discovery failed" in response.json()["detail"]


class TestFolderScopedBackupsAPI:
    def test_folder_param_forwarded(self, client, monkeypatch):
        seen = {}

        def fake_list(folder=None):
            seen["folder"] = folder
            return [{"key": f"{folder}/x.json.gz", "size": 1, "last_modified": "2024-05-01T00:00:00+00:00"}]

        monkeypatch.setattr(s3_module, "list_backups", fake_list)
        body = client.get("/api/backups?folder=dev-test-my-zookeeper").json()
        assert seen["folder"] == "dev-test-my-zookeeper"
        assert body["folder"] == "dev-test-my-zookeeper"

    def test_unknown_folder_404_when_allowlist_set(self, client, monkeypatch):
        s3_module.settings.s3_folders = ["dev-test-my-zookeeper"]
        monkeypatch.setattr(s3_module, "list_backups", lambda folder=None: [])
        response = client.get("/api/backups?folder=not-in-list")
        assert response.status_code == 404

    def test_malformed_folder_400(self, client):
        response = client.get("/api/backups?folder=..%2Fescape")
        assert response.status_code == 400

    def test_default_lists_backup_target(self, client, monkeypatch):
        s3_module.settings.cluster_folder = "dev-test-my-zookeeper"
        seen = {}
        monkeypatch.setattr(
            s3_module,
            "list_backups",
            lambda folder=None: seen.setdefault("folder", folder) or [],
        )
        body = client.get("/api/backups").json()
        assert seen["folder"] is None
        assert body["folder"] == "dev-test-my-zookeeper"


class TestBackupJobTargetsFolder:
    def test_perform_backup_reports_folder(self, monkeypatch):
        source = FakeZK()
        seed_tree(source)
        monkeypatch.setattr(jobs.zk, "connect", lambda: source)
        monkeypatch.setattr(
            jobs.s3,
            "upload_backup",
            lambda payload, key=None: "dev-test-my-zookeeper/zbs-x.json.gz",
        )
        s3_module.settings.cluster_folder = "dev-test-my-zookeeper"

        result = jobs.perform_backup()
        assert result["folder"] == "dev-test-my-zookeeper"
