"""Scheduler: fixed cadence, honest job waiting, cooperative shutdown."""

import threading
import time

import pytest

from app import jobs as jobs_mod
from app import metrics, scheduler
from app.config import settings

pytestmark = pytest.mark.usefixtures("_restore_settings", "_reset_metrics")


@pytest.fixture(autouse=True)
def _clean_scheduler():
    """Isolate module-level scheduler state between tests."""
    yield
    scheduler._stop.set()
    thread = scheduler._thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=5)
    scheduler._thread = None
    scheduler._stop.clear()
    scheduler._last_run = None
    scheduler._next_run = None
    scheduler._last_status = None


def _install_fake_engine(monkeypatch, *, job_running=lambda: True):
    """Replace the job engine with a controllable fake."""
    calls: list[float] = []

    def fake_run(trigger: str = "manual"):
        calls.append(time.monotonic())
        return {"id": f"job{len(calls)}"}

    def fake_get(job_id: str):
        status = "running" if job_running() else "success"
        return {"id": job_id, "status": status, "result": None}

    monkeypatch.setattr(jobs_mod, "run_backup_now", fake_run)
    monkeypatch.setattr(jobs_mod, "get_job", fake_get)
    return calls


class TestLifecycle:
    def test_disabled_when_interval_zero(self, monkeypatch):
        monkeypatch.setattr(settings, "backup_interval_seconds", 0, raising=False)
        scheduler.start_scheduler()
        assert scheduler._thread is None
        snap = scheduler.scheduler_snapshot()
        assert snap["enabled"] is False

    def test_snapshot_reports_success_and_next_run(self, monkeypatch):
        monkeypatch.setattr(scheduler, "_POLL_SECONDS", 0.005)
        monkeypatch.setattr(settings, "backup_interval_seconds", 1, raising=False)
        _install_fake_engine(monkeypatch, job_running=lambda: False)
        scheduler.start_scheduler()
        deadline = time.monotonic() + 5
        while scheduler.scheduler_snapshot()["last_status"] != "success":
            assert time.monotonic() < deadline, "scheduler never completed a tick"
            time.sleep(0.02)
        snap = scheduler.scheduler_snapshot()
        assert snap["enabled"] is True
        assert snap["next_run"] is not None
        assert snap["last_run"] is not None


class TestFixedCadence:
    def test_make_up_tick_fires_after_overrun(self, monkeypatch):
        """A job that overruns its slot must not push the schedule later:
        the next backup starts immediately after the overrun completes."""
        monkeypatch.setattr(scheduler, "_POLL_SECONDS", 0.005)
        monkeypatch.setattr(settings, "backup_interval_seconds", 1, raising=False)
        release = threading.Event()
        first_started = threading.Event()

        calls: list[float] = []

        def fake_run(trigger: str = "manual"):
            calls.append(time.monotonic())
            if len(calls) == 1:
                first_started.set()
                release.wait(timeout=10)  # overrun the next slot
            return {"id": f"job{len(calls)}"}

        state = {"released": False}

        def fake_get(job_id: str):
            done = state["released"] or len(calls) > 1
            return {"id": job_id, "status": "success" if done else "running"}

        def do_release():
            time.sleep(1.4)  # let the second slot pass while the job "runs"
            state["released"] = True
            release.set()

        monkeypatch.setattr(jobs_mod, "run_backup_now", fake_run)
        monkeypatch.setattr(jobs_mod, "get_job", fake_get)

        releaser = threading.Thread(target=do_release, daemon=True)
        releaser.start()
        scheduler.start_scheduler()

        assert first_started.wait(timeout=5), "first tick never ran"
        deadline = time.monotonic() + 6
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(calls) >= 2, "make-up tick never fired"
        gap = calls[1] - calls[0]
        # Drift-style cadence would wait another full interval after the
        # 1.4s job (gap ~= 2.4s). Fixed cadence fires the make-up slot
        # immediately (gap ~= 1.4s).
        assert gap < 2.0, f"cadence drifted (gap={gap:.2f}s)"
        releaser.join(timeout=5)


class TestWaitJob:
    def test_timeout_is_honest_and_measured(self, monkeypatch):
        monkeypatch.setattr(scheduler, "_POLL_SECONDS", 0.005)
        monkeypatch.setattr(scheduler, "_JOB_WAIT_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(
            jobs_mod, "get_job", lambda job_id: {"id": job_id, "status": "running"}
        )
        status = scheduler._wait_job("xyz")
        assert status == "timeout"
        assert "zbs_scheduler_timeouts_total 1" in metrics.render()

    def test_stops_waiting_on_shutdown(self, monkeypatch):
        monkeypatch.setattr(scheduler, "_POLL_SECONDS", 0.005)
        monkeypatch.setattr(
            jobs_mod, "get_job", lambda job_id: {"id": job_id, "status": "running"}
        )
        result: dict = {}

        def waiter():
            result["status"] = scheduler._wait_job("xyz")

        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        time.sleep(0.05)
        scheduler._stop.set()
        thread.join(timeout=2)
        assert not thread.is_alive(), "_wait_job ignored the stop event"
        assert result["status"] == "stopped"


class TestShutdown:
    def test_stop_joins_promptly_mid_wait(self, monkeypatch):
        monkeypatch.setattr(scheduler, "_POLL_SECONDS", 0.005)
        monkeypatch.setattr(settings, "backup_interval_seconds", 1, raising=False)
        _install_fake_engine(monkeypatch, job_running=lambda: True)
        scheduler.start_scheduler()
        # Let the first tick enter _wait_job (interval is 1s).
        time.sleep(1.15)
        assert scheduler._thread is not None and scheduler._thread.is_alive()

        began = time.monotonic()
        scheduler.stop_scheduler(timeout=5)
        elapsed = time.monotonic() - began

        assert scheduler._thread is None
        assert elapsed < 3, f"stop_scheduler took {elapsed:.2f}s to join"
        snap = scheduler.scheduler_snapshot()
        assert snap["enabled"] is True  # config unchanged; engine just stopped
