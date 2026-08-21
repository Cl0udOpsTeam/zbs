"""Job engine: lifecycle recording, concurrency guard, and the restore
safety ordering (verify everything BEFORE contacting ZooKeeper)."""

import time

import pytest

from app import errors, jobs
from conftest import FakeZK, seed_tree

pytestmark = pytest.mark.usefixtures("_restore_settings", "_clean_jobs")


@pytest.fixture(autouse=True)
def _clean_jobs():
    """Isolate the module-level job registry from other tests."""
    jobs._jobs.clear()
    jobs._order.clear()
    yield
    jobs._jobs.clear()
    jobs._order.clear()


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestSubmit:
    def test_success_is_recorded(self):
        job = jobs.submit("backup", "test backup", lambda: {"nodes": 1})
        assert wait_for(lambda: jobs.get_job(job["id"])["status"] == "success")
        assert jobs.get_job(job["id"])["result"] == {"nodes": 1}

    def test_failure_is_recorded_not_raised(self):
        def boom():
            raise ValueError("nope")

        job = jobs.submit("backup", "failing", boom)
        assert wait_for(lambda: jobs.get_job(job["id"])["status"] == "error")
        assert "nope" in jobs.get_job(job["id"])["error"]

    def test_concurrent_job_rejected_with_runtime_error(self):
        started = __import__("threading").Event()
        release = __import__("threading").Event()

        def blocker():
            started.set()
            release.wait(timeout=5)

        first = jobs.submit("backup", "first", blocker)
        try:
            with pytest.raises(RuntimeError, match="already running"):
                jobs.submit("restore", "second", lambda: None)
        finally:
            release.set()
            assert wait_for(lambda: jobs.get_job(first["id"])["status"] == "success")

    def test_busy_lock_free_once_success_visible(self):
        """Regression: seeing a terminal status implies the engine is free."""
        job = jobs.submit("backup", "quick", lambda: {})
        assert wait_for(lambda: jobs.get_job(job["id"])["status"] == "success")
        assert not jobs.busy(), "lock must be released before success is visible"
        # so an immediate follow-up submission must be accepted
        followup = jobs.submit("backup", "follow-up", lambda: {})
        assert wait_for(lambda: jobs.get_job(followup["id"])["status"] == "success")

    def test_job_history_order_newest_first(self):
        job_a = jobs.submit("backup", "a", lambda: {})
        assert wait_for(lambda: jobs.get_job(job_a["id"])["status"] == "success")
        job_b = jobs.submit("backup", "b", lambda: {})
        assert wait_for(lambda: jobs.get_job(job_b["id"])["status"] == "success")
        history = jobs.list_jobs(limit=2)
        assert [j["id"] for j in history][:2] == [job_b["id"], job_a["id"]]


class TestPerformBackup:
    def test_happy_path(self, monkeypatch):
        source = FakeZK()
        seed_tree(source)
        monkeypatch.setattr(jobs.zk, "connect", lambda: source)
        uploaded = {}

        def fake_upload(payload, key=None):
            uploaded["payload"] = payload
            return "zbs/x.json.gz"

        monkeypatch.setattr(jobs.s3, "upload_backup", fake_upload)

        result = jobs.perform_backup()
        assert result["key"] == "zbs/x.json.gz"
        assert result["nodes"] == 6  # /, /app, config, users, queue-, blob
        assert source.closed, "session must be closed after backup"
        # artifact must deserialize back to the same document
        document = jobs.zk.deserialize(uploaded["payload"])
        assert document["source_root"] == "/"

    def test_dump_failure_still_closes_session(self, monkeypatch):
        class HalfBroken(FakeZK):
            def exists(self, path):
                raise errors.ZooKeeperError("connection lost")

        broken = HalfBroken()
        monkeypatch.setattr(jobs.zk, "connect", lambda: broken)
        with pytest.raises(errors.ZooKeeperError):
            jobs.perform_backup()
        assert broken.closed


class TestPerformRestore:
    def _happy_setup(self, monkeypatch):
        source = FakeZK()
        seed_tree(source)
        raw = jobs.zk.serialize(jobs.zk.dump_tree(source, "/"))

        target = FakeZK()
        monkeypatch.setattr(jobs.s3, "download_backup", lambda key: raw)
        monkeypatch.setattr(jobs.zk, "connect", lambda: target)
        return target, raw

    def test_happy_path_restores(self, monkeypatch):
        target, _ = self._happy_setup(monkeypatch)
        stats = jobs.perform_restore("zbs/some.json.gz", wipe=True)
        assert stats["created"] == 5 and stats["updated"] == 1
        assert target.closed

    def test_corrupted_artifact_never_connects(self, monkeypatch):
        """THE guarantee: corrupt data never reaches zookeeper."""
        connect_calls = []

        def sentinel_connect():
            connect_calls.append(1)
            return FakeZK()

        monkeypatch.setattr(
            jobs.s3, "download_backup", lambda key: b"this is not a gzip file at all"
        )
        monkeypatch.setattr(jobs.zk, "connect", sentinel_connect)

        with pytest.raises(errors.ZbsError):
            jobs.perform_restore("zbs/corrupt.json.gz")
        assert connect_calls == []

    def test_checksum_tamper_never_connects(self, monkeypatch):
        good_document_source = FakeZK()
        seed_tree(good_document_source)
        document = jobs.zk.dump_tree(good_document_source, "/")

        import gzip
        import json as jsonlib

        tampered = dict(document)
        tampered["tree"] = {**document["tree"], "data_b64": "AAAA"}
        envelope = {
            "format": "zbs-backup-v1",
            "checksum": "0" * 64,
            "document": tampered,
        }
        raw = gzip.compress(jsonlib.dumps(envelope).encode())

        calls = []
        monkeypatch.setattr(jobs.s3, "download_backup", lambda key: raw)

        def sentinel():
            calls.append(1)
            return FakeZK()

        monkeypatch.setattr(jobs.zk, "connect", sentinel)
        with pytest.raises(errors.BackupCorruptedError):
            jobs.perform_restore("zbs/t.json.gz")
        assert calls == []

    def test_s3_failure_surfaces_before_zk(self, monkeypatch):
        def s3_down(key):
            raise errors.S3UnavailableError("endpoint down")

        monkeypatch.setattr(jobs.s3, "download_backup", s3_down)
        with pytest.raises(errors.S3UnavailableError):
            jobs.perform_restore("zbs/x.json.gz")

    def test_run_restore_bad_key_rejected_synchronously(self):
        with pytest.raises(ValueError, match="prefix"):
            jobs.run_restore("../escape.json.gz")


class TestRunRestoreAsync:
    def test_end_to_end_job_records_success(self, monkeypatch):
        source = FakeZK()
        seed_tree(source)
        raw = jobs.zk.serialize(jobs.zk.dump_tree(source, "/"))
        monkeypatch.setattr(jobs.s3, "download_backup", lambda key: raw)
        target = FakeZK()
        monkeypatch.setattr(jobs.zk, "connect", lambda: target)

        job = jobs.run_restore("zbs/full.json.gz")
        assert wait_for(lambda: (jobs.get_job(job["id"]) or {}).get("status") == "success")
        final = jobs.get_job(job["id"])
        assert final["result"]["key"] == "zbs/full.json.gz"
