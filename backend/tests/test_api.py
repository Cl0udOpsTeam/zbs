"""HTTP layer: status codes, honest error mapping, no secret leakage."""

import pytest
from fastapi.testclient import TestClient

from app import errors, jobs, main, retention, s3 as s3_module, zk as zk_module
from conftest import FakeZK, seed_tree


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


class TestProbes:
    def test_healthz_ok(self, client):
        body = client.get("/healthz").json()
        assert body == {"status": "ok"}

    def test_readyz_degraded_without_bucket(self, client):
        s3_module.settings.s3_bucket = ""
        body = client.get("/readyz").json()
        assert body["status"] == "degraded"
        assert any("ZBS_S3_BUCKET" in p for p in body["problems"])

    def test_readyz_ok_when_configured(self, client):
        s3_module.settings.s3_bucket = "bkt"
        body = client.get("/readyz").json()
        assert body == {"status": "ok", "problems": []}


class TestConfigEndpoint:
    def test_shape_and_no_secrets(self, client):
        zk_module.settings.zk_password = "hunter2"
        s3_module.settings.s3_secret_access_key = "wJalrXUtnFEMI"
        body = client.get("/api/config").json()
        blob = repr(body)
        assert body["retention"]["min_keep"] >= 0
        assert "hunter2" not in blob
        assert "wJalrXUtnFEMI" not in blob
        assert "password" not in blob.lower()

    def test_retention_block_present(self, client):
        retention.settings.retention_max_age_seconds = 604800
        retention.settings.retention_interval_seconds = 21600
        body = client.get("/api/config").json()
        assert body["retention"] == {
            "enabled": True,
            "max_age_seconds": 604800,
            "interval_seconds": 21600,
            "min_keep": retention.settings.retention_min_keep,
            "scope_folders": retention.settings.retention_scope(),
        }


class TestStatusEndpoint:
    def test_components_reported_when_deps_down(self, client, monkeypatch):
        def dead_connect():
            raise errors.ZooKeeperUnavailableError("cannot reach")

        def dead_s3():
            raise errors.S3UnavailableError("cannot reach s3")

        monkeypatch.setattr(zk_module, "connect", dead_connect)
        monkeypatch.setattr(s3_module, "check_connection", dead_s3)

        body = client.get("/api/status").json()
        assert body["components"]["zookeeper"].startswith("error:")
        assert body["components"]["s3"].startswith("error:")
        assert body["busy"] is False

    def test_components_ok_when_up(self, client, monkeypatch):
        target = FakeZK()
        seed_tree(target)
        monkeypatch.setattr(zk_module, "connect", lambda: target)
        monkeypatch.setattr(s3_module, "check_connection", lambda: None)
        body = client.get("/api/status").json()
        assert body["components"] == {"zookeeper": "ok", "s3": "ok"}


class TestBackupsEndpoint:
    def test_lists_backups(self, client, monkeypatch):
        monkeypatch.setattr(
            s3_module,
            "list_backups",
            lambda folder=None: [{"key": "zbs/x.json.gz", "size": 5, "last_modified": "2024-05-01T00:00:00+00:00"}],
        )
        body = client.get("/api/backups").json()
        assert body["backups"][0]["key"] == "zbs/x.json.gz"

    def test_configuration_error_maps_to_400(self, client, monkeypatch):
        def boom(folder=None):
            raise errors.ConfigurationError("ZBS_S3_BUCKET is not configured")

        monkeypatch.setattr(s3_module, "list_backups", boom)
        response = client.get("/api/backups")
        assert response.status_code == 400
        assert "ZBS_S3_BUCKET" in response.json()["detail"]

    def test_unavailable_maps_to_502(self, client, monkeypatch):
        def boom(folder=None):
            raise errors.S3UnavailableError("endpoint unreachable")

        monkeypatch.setattr(s3_module, "list_backups", boom)
        response = client.get("/api/backups")
        assert response.status_code == 502
        assert "S3 list failed" in response.json()["detail"]

    def test_unexpected_error_maps_to_500(self, monkeypatch):
        def boom(folder=None):
            raise ZeroDivisionError("bug")

        monkeypatch.setattr(s3_module, "list_backups", boom)
        # ServerErrorMiddleware always re-raises after handling; a real server
        # would still deliver our JSON 500 body. Tell the test client not to
        # surface the exception so we can assert on that response instead.
        plain = TestClient(main.app, raise_server_exceptions=False)
        response = plain.get("/api/backups")
        assert response.status_code == 500
        assert "bug" in response.json()["detail"]


class TestBackupCreation:
    def test_accepted_202_with_job(self, client, monkeypatch):
        monkeypatch.setattr(jobs, "run_backup_now", lambda trigger: {"id": "abc123"})
        response = client.post("/api/backups")
        assert response.status_code == 202
        assert response.json()["id"] == "abc123"

    def test_busy_conflict_maps_to_409(self, client, monkeypatch):
        def busy(trigger):
            raise RuntimeError("another backup/restore job is already running")

        monkeypatch.setattr(jobs, "run_backup_now", busy)
        response = client.post("/api/backups")
        assert response.status_code == 409
        assert "already running" in response.json()["detail"]


class TestRestoreEndpoint:
    def test_accepted_202(self, client, monkeypatch):
        seen = {}

        def fake_run(key, wipe=False):
            seen.update({"key": key, "wipe": wipe})
            return {"id": "job42"}

        monkeypatch.setattr(jobs, "run_restore", fake_run)
        response = client.post("/api/restore", json={"key": "zbs/a.json.gz", "wipe": True})
        assert response.status_code == 202
        assert seen == {"key": "zbs/a.json.gz", "wipe": True}

    def test_bad_prefix_maps_to_400(self, client):
        response = client.post("/api/restore", json={"key": "../escape.json.gz"})
        assert response.status_code == 400
        assert "prefix" in response.json()["detail"]

    def test_busy_conflict_maps_to_409(self, client, monkeypatch):
        def busy(key, wipe=False):
            raise RuntimeError("another backup/restore job is already running")

        monkeypatch.setattr(jobs, "run_restore", busy)
        response = client.post("/api/restore", json={"key": "zbs/a.json.gz"})
        assert response.status_code == 409

    def test_missing_key_field_is_422(self, client):
        assert client.post("/api/restore", json={"wipe": True}).status_code == 422


class TestJobsEndpoints:
    def test_limit_clamped_to_valid_range(self, client, monkeypatch):
        seen = {}

        def capture(limit):
            seen["limit"] = limit
            return []

        monkeypatch.setattr(jobs, "list_jobs", capture)
        assert client.get("/api/jobs?limit=-5").json() == {"jobs": []}
        assert seen["limit"] == 1
        client.get("/api/jobs?limit=9999")
        assert seen["limit"] == 100

    def test_unknown_job_is_404(self, client):
        response = client.get("/api/jobs/deadbeef")
        assert response.status_code == 404
        assert response.json()["detail"] == "no such job"

    def test_existing_job_returned(self, client, monkeypatch):
        monkeypatch.setattr(jobs, "get_job", lambda job_id: {"id": job_id, "status": "running"})
        assert client.get("/api/jobs/fresh").json() == {"id": "fresh", "status": "running"}
