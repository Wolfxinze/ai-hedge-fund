"""Phase 8: APScheduler configuration. OFFLINE — assert job CONFIG (no real timer advance).
Verifies the refresh job is registered with no-pileup kwargs (max_instances=1, coalesce, misfire
grace), the env cron override, per-monitor registration, invalid-schedule skip (one bad monitor
doesn't kill the scheduler), a bad refresh cron raises, and start/stop is clean.
"""

import contextlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.storage.models as m
from src.scheduler.scheduler import (
    build_scheduler,
    REFRESH_JOB_ID,
    start_scheduler,
    stop_scheduler,
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


def test_refresh_job_registered_with_no_pileup_config(factory, monkeypatch):
    monkeypatch.delenv("OBSERVING_POOL_REFRESH_CRON", raising=False)
    sch = build_scheduler(session_factory=factory, run_analysts_factory=_raf, analyzing_flow=lambda t, d: None)
    job = sch.get_job(REFRESH_JOB_ID)
    assert job is not None
    assert job.max_instances == 1  # no second instance starts while one runs
    assert job.coalesce is True  # missed fires collapse into one catch-up
    assert job.misfire_grace_time == 3600


def test_refresh_cron_env_override(factory, monkeypatch):
    monkeypatch.setenv("OBSERVING_POOL_REFRESH_CRON", "0 9 * * 2")
    sch = build_scheduler(session_factory=factory, run_analysts_factory=_raf)
    assert "hour='9'" in str(sch.get_job(REFRESH_JOB_ID).trigger)


def test_invalid_refresh_cron_raises(factory, monkeypatch):
    """A misconfigured env cron fails loud (main.py catches it so the app still starts)."""
    monkeypatch.setenv("OBSERVING_POOL_REFRESH_CRON", "not-a-cron")
    with pytest.raises(ValueError):
        build_scheduler(session_factory=factory, run_analysts_factory=_raf)


def test_enabled_monitor_registers_a_job(factory, monkeypatch):
    monkeypatch.delenv("OBSERVING_POOL_REFRESH_CRON", raising=False)
    with factory() as s:
        mon = MonitorConfig(name="m1", tickers=["NVDA"], granularity="weekly", enabled=True)
        s.add(mon)
        s.flush()
        mid = mon.id
    sch = build_scheduler(session_factory=factory, run_analysts_factory=_raf)
    ids = {j.id for j in sch.get_jobs()}
    assert REFRESH_JOB_ID in ids and f"monitor_{mid}" in ids
    mjob = sch.get_job(f"monitor_{mid}")  # monitor jobs are as LLM-heavy as refresh → same no-pileup config
    assert mjob.max_instances == 1 and mjob.coalesce is True


def test_invalid_monitor_schedule_skipped_scheduler_survives(factory, monkeypatch, caplog):
    monkeypatch.delenv("OBSERVING_POOL_REFRESH_CRON", raising=False)
    with factory() as s:
        mon = MonitorConfig(name="bad", tickers=["NVDA"], granularity="garbage", schedule="garbage", enabled=True)
        s.add(mon)
        s.flush()
        mid = mon.id
    with caplog.at_level("WARNING", logger="src.scheduler.scheduler"):
        sch = build_scheduler(session_factory=factory, run_analysts_factory=_raf)
    ids = {j.id for j in sch.get_jobs()}
    assert f"monitor_{mid}" not in ids  # invalid schedule → not registered
    assert REFRESH_JOB_ID in ids  # one bad monitor must NOT take down the refresh job
    assert any("invalid schedule" in r.getMessage() for r in caplog.records)


def test_disabled_monitor_not_registered(factory, monkeypatch):
    monkeypatch.delenv("OBSERVING_POOL_REFRESH_CRON", raising=False)
    with factory() as s:
        s.add(MonitorConfig(name="off", tickers=["NVDA"], granularity="weekly", enabled=False))
        s.flush()
    sch = build_scheduler(session_factory=factory, run_analysts_factory=_raf)
    assert len(sch.get_jobs()) == 1  # only the refresh job


def test_start_then_stop_is_clean(factory, monkeypatch):
    monkeypatch.delenv("OBSERVING_POOL_REFRESH_CRON", raising=False)
    sch = build_scheduler(session_factory=factory, run_analysts_factory=_raf)
    start_scheduler(sch)
    try:
        assert sch.running
    finally:
        stop_scheduler(sch)
    assert not sch.running
