"""Retention: victim selection boundaries and sweep behavior."""

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import errors, retention
from conftest import FakeS3

pytestmark = pytest.mark.usefixtures("_restore_settings")


def iso(dt: datetime) -> str:
    return dt.isoformat()


class TestSelectVictims:
    def test_exact_boundary_is_kept(self):
        now = datetime(2024, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        items = [{"key": "edge", "last_modified": iso(now - timedelta(seconds=604800))}]
        assert retention.select_victims(items, now, 604800, min_keep=1) == []

    def test_one_second_past_boundary_is_deleted(self):
        now = datetime(2024, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        items = [
            {"key": "new", "last_modified": iso(now - timedelta(seconds=604800 - 1))},
            {"key": "old", "last_modified": iso(now - timedelta(seconds=604800 + 1))},
        ]
        assert retention.select_victims(items, now, 604800, min_keep=1) == ["old"]

    def test_timezone_aware_offsets_compared_correctly(self):
        now = datetime(2024, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        items = [
            # same instant as 10:00 UTC but written with a +02:00 offset
            {"key": "shifted", "last_modified": "2024-05-01T10:00:00+02:00"},
            {"key": "ancient", "last_modified": "2023-05-01T10:00:00+02:00"},
        ]
        victims = retention.select_victims(items, now, 86400, min_keep=0)
        assert victims == ["ancient"]

    def test_min_keep_beats_age(self):
        now = datetime.now(timezone.utc)
        items = [
            {"key": f"b{i}", "last_modified": iso(now - timedelta(days=400 + i))}
            for i in range(4)
        ]
        assert retention.select_victims(items, now, 86400, min_keep=0) == [i["key"] for i in items]
        assert retention.select_victims(items, now, 86400, min_keep=2) == ["b2", "b3"]
        assert retention.select_victims(items, now, 86400, min_keep=99) == []


class TestSweepOnce:
    def _wire(self, monkeypatch, objects):
        fake = FakeS3()
        for key, modified in objects.items():
            fake.objects[key] = {
                "body": b"x",
                "last_modified": datetime.fromisoformat(modified),
            }
        monkeypatch.setattr(retention.s3, "list_backups", lambda folder=None: [
            {
                "key": key,
                "size": len(meta["body"]),
                "last_modified": meta["last_modified"].replace(tzinfo=timezone.utc).isoformat(),
            }
            for key, meta in sorted(
                fake.objects.items(),
                key=lambda kv: kv[1]["last_modified"],
                reverse=True,
            )
        ])
        deleted = []
        monkeypatch.setattr(
            retention.s3, "delete_backups", lambda keys: deleted.extend(keys) or keys
        )
        return fake, deleted

    def test_deletes_only_expired_respecting_min_keep(self, monkeypatch):
        now = datetime.now(timezone.utc)
        retention.settings.retention_max_age_seconds = 86400
        retention.settings.retention_min_keep = 1
        fake, deleted = self._wire(
            monkeypatch,
            {
                "zbs/new.json.gz": iso(now - timedelta(hours=1)),
                "zbs/week.json.gz": iso(now - timedelta(days=7)),
                "zbs/month.json.gz": iso(now - timedelta(days=30)),
            },
        )
        summary = retention.sweep_once(trigger="test")
        assert sorted(deleted) == ["zbs/month.json.gz", "zbs/week.json.gz"]
        assert summary["deleted"] == 2 and summary["scanned"] == 3 and summary["kept"] == 1

    def test_empty_bucket_noop(self, monkeypatch):
        retention.settings.retention_max_age_seconds = 86400
        _, deleted = self._wire(monkeypatch, {})
        summary = retention.sweep_once()
        assert deleted == [] and summary["deleted"] == 0

    def test_s3_failure_propagates_to_loop_guard(self, monkeypatch):
        retention.settings.retention_max_age_seconds = 86400

        def boom(folder=None):
            raise errors.S3UnavailableError("endpoint down")

        monkeypatch.setattr(retention.s3, "list_backups", boom)
        with pytest.raises(errors.S3UnavailableError):
            retention.sweep_once()

    def test_disabled_when_max_age_zero(self, monkeypatch):
        retention.settings.retention_max_age_seconds = 0
        _, deleted = self._wire(
            monkeypatch, {"zbs/ancient.json.gz": "2020-01-01T00:00:00+00:00"}
        )
        # sweep_once itself doesn't check the flag (the loop won't start);
        # guard the invariant explicitly:
        assert retention.retention_snapshot()["enabled"] is False


class TestShutdown:
    def test_start_stop_joins_thread(self, monkeypatch):
        monkeypatch.setattr(retention.settings, "retention_max_age_seconds", 86400)
        monkeypatch.setattr(retention.settings, "retention_interval_seconds", 3600)
        monkeypatch.setattr(
            retention,
            "sweep_once",
            lambda trigger="schedule": {"at": "x", "trigger": trigger},
        )
        retention.start_retention()
        assert retention._thread is not None and retention._thread.is_alive()
        began = datetime.now(timezone.utc)
        retention.stop_retention(timeout=5)
        elapsed = (datetime.now(timezone.utc) - began).total_seconds()
        assert retention._thread is None
        assert elapsed < 3, f"stop_retention took {elapsed:.2f}s to join"

    def test_stop_abandons_thread_that_wont_die(self, monkeypatch):
        """A sweep stuck in S3 must not hang shutdown: join times out, the
        thread is abandoned loudly, and the caller moves on."""
        stuck = threading.Event()
        released = threading.Event()

        def blocked_sweep(trigger="schedule"):
            stuck.set()
            released.wait(timeout=10)

        monkeypatch.setattr(retention.settings, "retention_max_age_seconds", 86400)
        # First sweep happens after one full interval -> keep it tiny.
        monkeypatch.setattr(retention.settings, "retention_interval_seconds", 1)
        monkeypatch.setattr(retention, "sweep_once", blocked_sweep)

        retention.start_retention()
        assert stuck.wait(timeout=5), "loop never entered the sweep"
        retention.stop_retention(timeout=0.1)  # too short on purpose
        assert retention._thread is None  # abandoned
        released.set()  # let the daemon thread finish for cleanup
        time.sleep(0.05)
