"""Metrics registry: exposition format, labels, thread safety basics."""

import threading

from app import metrics


class TestRender:
    def test_empty_registry_renders_nothing(self):
        assert metrics.render() == ""

    def test_counter_increments_and_escapes_labels(self):
        metrics.inc("zbs_test_total", "test counter", {"kind": 'we"ird\\path'})
        metrics.inc("zbs_test_total", "test counter", {"kind": 'we"ird\\path'})
        text = metrics.render()
        assert "# TYPE zbs_test_total counter" in text
        assert '# HELP zbs_test_total test counter' in text
        assert 'zbs_test_total{kind="we\\"ird\\\\path"} 2' in text

    def test_gauge_sets_absolute_value(self):
        metrics.gauge_set("zbs_test_gauge", "g", 0.75, {"kind": "backup"})
        metrics.gauge_set("zbs_test_gauge", "g", 0.25, {"kind": "backup"})
        assert "zbs_test_gauge{kind=\"backup\"} 0.25" in metrics.render()

    def test_label_keys_sorted_deterministically(self):
        metrics.inc("m", "h", {"b": "2"})
        metrics.inc("m", "h", {"a": "1"})
        lines = [ln for ln in metrics.render().splitlines() if ln.startswith("m{")]
        assert lines == ['m{a="1"} 1', 'm{b="2"} 1']

    def test_series_sorted_by_name(self):
        metrics.inc("zzz", "h")
        metrics.inc("aaa", "h")
        names = [
            ln.split()[2] for ln in metrics.render().splitlines()
            if ln.startswith("# TYPE")
        ]
        assert names == ["aaa", "zzz"]


class TestDomainHelpers:
    def test_record_job_counts_duration_and_bytes(self):
        metrics.record_job("backup", "success", 12.5, payload_bytes=2048)
        metrics.record_job("restore", "error", 1.0)
        text = metrics.render()
        assert 'zbs_jobs_total{kind="backup",result="success"} 1' in text
        assert 'zbs_jobs_total{kind="restore",result="error"} 1' in text
        assert 'zbs_last_job_duration_seconds{kind="backup"} 12.5' in text
        assert "zbs_backup_payload_bytes_total 2048" in text

    def test_retention_zero_deletes_not_recorded(self):
        metrics.record_retention(0)
        assert "zbs_retention_deleted_total" not in metrics.render()

    def test_http_status_bucketing(self):
        for status in (200, 201, 404, 500, 503):
            metrics.record_http("GET", "/api/backups", status)
        text = metrics.render()
        assert 'path="/api/backups",status="2xx"} 2' in text
        assert 'status="4xx"} 1' in text
        assert 'status="5xx"} 2' in text

    def test_engine_busy_gauge(self):
        metrics.set_engine_busy(True)
        assert "zbs_engine_busy 1" in metrics.render()
        metrics.set_engine_busy(False)
        assert "zbs_engine_busy 0" in metrics.render()


class TestConcurrency:
    def test_parallel_increments_lose_none(self):
        def hammer():
            for _ in range(500):
                metrics.inc("zbs_race_total", "race")

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert "zbs_race_total 4000" in metrics.render()
