"""Phase 9 / Issue #21: hot-reload — reschedule_monitor adds/removes/updates live scheduler jobs.

OFFLINE — assert job CONFIG only (scheduler not started, no real timer fires).
WHY these tests matter: without hot-reload a created/edited monitor arms only on next restart, which
breaks the UX contract (live create/enable/disable must take effect immediately). These tests pin
that the hot-path produces the SAME no-pileup config as a restart-registered job (max_instances=1,
coalesce=True) and that the job lifecycle is idempotent and error-safe.
"""

import contextlib
import logging

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.storage.models as m
from src.scheduler.scheduler import (
    build_scheduler,
    monitor_job_id,
    reschedule_monitor,
)
from src.storage.models import MonitorConfig


@pytest.fixture
def factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    m.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    @contextlib.contextmanager
    def session_factory():
        s = Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    return session_factory


def _raf():
    return lambda *a, **k: ({}, {})


@pytest.fixture
def sch(factory, monkeypatch):
    monkeypatch.delenv("OBSERVING_POOL_REFRESH_CRON", raising=False)
    return build_scheduler(session_factory=factory, run_analysts_factory=_raf)


def _make_monitor(factory, *, name="hot_mon", schedule=None, enabled=True, granularity="weekly"):
    """Insert a MonitorConfig row and return a simple mutable namespace mirroring its attributes.
    Returned outside the session so tests can mutate fields (e.g. enabled=False) without triggering
    SQLAlchemy dirty-tracking. The monitor_schedule() helper reads only .schedule and .granularity."""
    with factory() as s:
        mon = MonitorConfig(name=name, tickers=["NVDA"], granularity=granularity, schedule=schedule, enabled=enabled)
        s.add(mon)
        s.flush()
        mid = mon.id
        mon_name = mon.name
        mon_schedule = mon.schedule
        mon_granularity = mon.granularity
        mon_enabled = mon.enabled

    from types import SimpleNamespace

    return SimpleNamespace(id=mid, name=mon_name, schedule=mon_schedule, granularity=mon_granularity, enabled=mon_enabled)


def test_reschedule_adds_job_for_enabled_monitor(sch, factory):
    """reschedule_monitor ADDS a job for an enabled monitor with the correct no-pileup config.
    WHY: a new monitor must arm immediately, not wait for a restart (Phase-9 guarantee).
    The no-pileup config (max_instances=1, coalesce=True) must match the build-time path — one
    code path, not two separate implementations."""
    mon = _make_monitor(factory, name="new_mon", enabled=True)
    mid = mon.id
    job_id = monitor_job_id(mid)

    assert sch.get_job(job_id) is None  # pre-condition: build had no monitors in DB

    reschedule_monitor(sch, mon, session_factory=factory)

    job = sch.get_job(job_id)
    assert job is not None, "job must be registered after reschedule_monitor for an enabled monitor"
    assert job.id == job_id
    assert job.max_instances == 1, "no-pileup: at most one concurrent LLM run per monitor"
    assert job.coalesce is True, "missed fires collapse; never pile up"


def test_reschedule_removes_job_when_disabled(sch, factory):
    """reschedule_monitor REMOVES the job when the monitor is disabled.
    WHY: a disabled monitor must stop firing immediately — leaving a stale job would continue
    consuming LLM/API credits and producing unwanted reports."""
    mon = _make_monitor(factory, name="disable_me", enabled=True)

    # First arm the job
    reschedule_monitor(sch, mon, session_factory=factory)
    assert sch.get_job(monitor_job_id(mon.id)) is not None

    # Now disable and reschedule
    mon.enabled = False
    reschedule_monitor(sch, mon, session_factory=factory)

    assert sch.get_job(monitor_job_id(mon.id)) is None, "disabled monitor must have no registered job"


def test_reschedule_updates_trigger_on_schedule_change(sch, factory):
    """reschedule_monitor UPDATES the trigger when the schedule changes.
    WHY: an edited cadence (e.g. weekly → monthly) must take effect on the live scheduler, not just
    in the DB; without hot-reload the old schedule keeps firing at the wrong cadence.
    NOTE: add_job(..., replace_existing=True) only replaces a job in the running job store;
    we start the scheduler briefly to observe the live-store trigger-update behaviour."""
    from src.scheduler.scheduler import start_scheduler, stop_scheduler

    mon = _make_monitor(factory, name="change_cron", granularity="weekly", enabled=True)

    start_scheduler(sch)
    try:
        reschedule_monitor(sch, mon, session_factory=factory)
        trigger_before = str(sch.get_job(monitor_job_id(mon.id)).trigger)

        # Change to monthly (a different cron expression)
        mon.granularity = "monthly"
        reschedule_monitor(sch, mon, session_factory=factory)

        trigger_after = str(sch.get_job(monitor_job_id(mon.id)).trigger)
        assert trigger_before != trigger_after, "trigger must change when schedule changes; weekly and monthly produce different cron expressions"
    finally:
        stop_scheduler(sch)


def test_reschedule_invalid_schedule_does_not_raise_and_logs_warning_and_leaves_no_job(sch, factory, caplog):
    """reschedule_monitor on an INVALID schedule: does NOT raise, logs a WARNING, and leaves NO job.
    WHY: a scheduling side-effect failure (e.g. a stored bad cron) must not 500 the API write path
    (best-effort contract); the warning is loud so ops sees it; no stale job is left behind."""
    mon = _make_monitor(factory, name="bad_sched", schedule="not-a-cron", enabled=True)

    with caplog.at_level(logging.WARNING, logger="src.scheduler.scheduler"):
        reschedule_monitor(sch, mon, session_factory=factory)  # must NOT raise

    assert sch.get_job(monitor_job_id(mon.id)) is None, "invalid schedule must leave no job"
    assert any("hot-reload" in r.getMessage() or "invalid schedule" in r.getMessage() for r in caplog.records), "a WARNING must be logged for an invalid schedule"


def test_reschedule_idempotent_for_enabled_monitor(sch, factory):
    """reschedule_monitor is IDEMPOTENT for enabled: calling twice yields exactly one job.
    WHY: add_job(..., replace_existing=True) deduplicates on a LIVE job store. A double-call
    (e.g. two concurrent API requests) must not create duplicate jobs that fire twice per cadence.
    NOTE: replace_existing=True only deduplicates once jobs are flushed to the running job store;
    we must start the scheduler briefly to verify the live-store behaviour."""
    from src.scheduler.scheduler import start_scheduler, stop_scheduler

    mon = _make_monitor(factory, name="idem_on", enabled=True)

    start_scheduler(sch)
    try:
        reschedule_monitor(sch, mon, session_factory=factory)
        reschedule_monitor(sch, mon, session_factory=factory)

        jobs = [j for j in sch.get_jobs() if j.id == monitor_job_id(mon.id)]
        assert len(jobs) == 1, "exactly one job must exist after two reschedule calls for an enabled monitor"
    finally:
        stop_scheduler(sch)


def test_reschedule_idempotent_for_disabled_monitor(sch, factory):
    """reschedule_monitor is IDEMPOTENT for disabled: calling twice on a disabled monitor leaves no
    job and raises no error.
    WHY: the remove path (get_job → remove if present) must not fail when already absent."""
    mon = _make_monitor(factory, name="idem_off", enabled=False)

    reschedule_monitor(sch, mon, session_factory=factory)  # no-op (was never added)
    reschedule_monitor(sch, mon, session_factory=factory)  # must not raise

    assert sch.get_job(monitor_job_id(mon.id)) is None


def test_reschedule_evicts_existing_job_when_reenabled_with_invalid_schedule(sch, factory):
    """reschedule_monitor EVICTS a previously-armed LIVE job when the monitor is re-enabled with a
    now-invalid schedule.
    WHY (#48): distinct from the no-job-start case — an existing armed job (a monitor that was valid
    and is in the RUNNING job store) must be removed when an edit makes its schedule invalid, so a
    stale, out-of-date job can never keep firing. Started scheduler because the first job must be
    flushed to the live store for this to exercise the running-layer eviction (a non-started
    scheduler's _pending_jobs has divergent get_job semantics).
    MUTATION-PROOF: deleting the except-branch ``_remove_monitor_job`` call in reschedule_monitor
    leaves the stale valid job in the store → this assertion FAILS."""
    from src.scheduler.scheduler import start_scheduler, stop_scheduler

    mon = _make_monitor(factory, name="reenable_bad", granularity="weekly", enabled=True)
    job_id = monitor_job_id(mon.id)

    start_scheduler(sch)
    try:
        # Arm a VALID job and confirm it is live in the running store.
        reschedule_monitor(sch, mon, session_factory=factory)
        assert sch.get_job(job_id) is not None, "pre-condition: a valid job must be armed in the live store"

        # Re-enable with an INVALID schedule (simulates an edit that stored a bad cron).
        # monitor_schedule() returns .schedule when set, so this overrides the weekly granularity.
        mon.enabled = True
        mon.schedule = "not-a-cron"
        reschedule_monitor(sch, mon, session_factory=factory)  # must NOT raise

        assert sch.get_job(job_id) is None, "a previously-armed live job must be EVICTED when re-enabled with an invalid schedule"
    finally:
        stop_scheduler(sch)


def test_remove_monitor_job_warns_if_job_persists_after_removal(sch, factory, caplog, monkeypatch):
    """_remove_monitor_job logs a WARNING if the job is STILL present after remove_job (observability
    only — the authoritative disarm is the DB-checked guard in run_monitor_job, not the in-memory
    job table; #48).
    MUTATION-PROOF / verification approach: monkeypatch remove_job to a no-op so get_job still returns
    the job post-remove → assert the warning fires. Deleting the warning line in _remove_monitor_job
    makes this assertion FAIL (the no-op remove leaves the job and no warning is emitted)."""
    from src.scheduler.scheduler import _add_monitor_job, _remove_monitor_job

    mon = _make_monitor(factory, name="stuck_job", enabled=True)
    job_id = monitor_job_id(mon.id)

    _add_monitor_job(
        sch,
        monitor_id=mon.id,
        name=mon.name,
        schedule="weekly",
        analyzing_flow=None,
        session_factory=factory,
    )
    assert sch.get_job(job_id) is not None  # pre-condition: job is present

    # Make remove_job a no-op so the job survives the removal attempt.
    monkeypatch.setattr(sch, "remove_job", lambda *a, **k: None)

    with caplog.at_level(logging.WARNING, logger="src.scheduler.scheduler"):
        _remove_monitor_job(sch, mon.id)

    assert any("persist" in r.getMessage().lower() and str(job_id) in r.getMessage() for r in caplog.records), "a WARNING must be logged when a job persists after remove_job"
