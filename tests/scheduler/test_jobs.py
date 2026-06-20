"""Phase 8: scheduler job callables. Fully OFFLINE — StaticPool in-memory DB, stubbed refresh/flow,
injected dates. Verifies: refresh locks+releases every platform and skips an already-locked one;
a monitor job emits one disclaimer-bearing report per ticker and creates NO trade rows; a
disabled monitor is skipped; the monitor-schedule fallback.
"""

import contextlib
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.backend.database.models  # noqa: F401 - register HedgeFundFlowRun on the shared Base
import src.storage.models as m
from src.integrations.tradingagents_adapter import AnalyzingFlowResult
from src.observing_pools import pool_lock as pl
from src.observing_pools.platforms import PLATFORM_KEYS
from src.scheduler.jobs import (
    monitor_schedule,
    refresh_all_platforms_job,
    run_monitor_job,
)
from src.storage.models import MonitorConfig, OpportunityReport, PoolLock, ReportLabel


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


def _stub_run(**over):
    base = {"status": "complete", "error": None, "summary": {"ranked": 1}, "id": 1}
    base.update(over)
    return SimpleNamespace(**base)


# ── refresh job ──────────────────────────────────────────────────────────────


def test_refresh_all_platforms_locks_each_and_releases(factory, monkeypatch):
    seen = []

    def stub_refresh(session, config, run_analysts, *, end_date, provider_name="yfinance"):
        seen.append(config.platform_key)
        assert session.get(PoolLock, config.platform_key) is not None  # lock held during refresh
        return _stub_run()

    monkeypatch.setattr(pl, "refresh_pool", stub_refresh)
    refresh_all_platforms_job(
        run_analysts_factory=lambda: (lambda *a, **k: ({}, {})),
        session_factory=factory,
        end_date_fn=lambda: "2026-01-05",
    )
    assert sorted(seen) == sorted(PLATFORM_KEYS)  # every platform refreshed
    with factory() as s:
        assert s.query(PoolLock).count() == 0  # all locks released after the job


def test_refresh_skips_already_locked_platform(factory, monkeypatch):
    locked = PLATFORM_KEYS[0]
    with factory() as s:
        pl.acquire_pool_lock(s, locked, "other-runner", clock=pl._utc_now, ttl_seconds=3600)  # live lock

    seen = []

    def stub_refresh(session, config, run_analysts, *, end_date, provider_name="yfinance"):
        seen.append(config.platform_key)
        return _stub_run()

    monkeypatch.setattr(pl, "refresh_pool", stub_refresh)
    refresh_all_platforms_job(
        run_analysts_factory=lambda: (lambda *a, **k: ({}, {})), session_factory=factory, end_date_fn=lambda: "2026-01-05"
    )
    assert locked not in seen  # the live-locked platform was skipped (contended)
    assert len(seen) == len(PLATFORM_KEYS) - 1
    with factory() as s:
        assert s.get(PoolLock, locked).locked_by == "other-runner"  # other holder's lock untouched


# ── monitor job (no-trade boundary) ──────────────────────────────────────────


def test_run_monitor_job_emits_disclaimer_reports_and_no_trades(factory):
    with factory() as s:
        mon = MonitorConfig(name="m1", tickers=["NVDA", "TSM"], granularity="weekly", enabled=True)
        s.add(mon)
        s.flush()
        mid = mon.id

    def flow(ticker, trade_date):
        return AnalyzingFlowResult(ticker=ticker, label=ReportLabel.THESIS_SUPPORTIVE, confidence=0.7, degraded=False, summary="ok")

    run_monitor_job(mid, analyzing_flow=flow, session_factory=factory, trade_date_fn=lambda: "2026-01-05")
    with factory() as s:
        reports = s.query(OpportunityReport).filter_by(monitor_id=mid).all()
        assert len(reports) == 2  # one report per ticker
        assert all(r.disclaimer for r in reports)  # serialize_report chokepoint enforced the disclaimer
        # No-trade boundary: a scheduled monitor never creates a hedge-fund flow/trade run.
        assert s.query(app.backend.database.models.HedgeFundFlowRun).count() == 0


def test_run_monitor_job_skips_disabled(factory):
    with factory() as s:
        mon = MonitorConfig(name="m2", tickers=["NVDA"], granularity="weekly", enabled=False)
        s.add(mon)
        s.flush()
        mid = mon.id

    run_monitor_job(
        mid,
        analyzing_flow=lambda t, d: AnalyzingFlowResult(ticker=t, label=ReportLabel.MIXED, confidence=0.5, degraded=False, summary="x"),
        session_factory=factory,
    )
    with factory() as s:
        assert s.query(OpportunityReport).count() == 0  # disabled → no reports


def test_monitor_schedule_prefers_explicit_then_granularity():
    assert monitor_schedule(SimpleNamespace(schedule="0 9 * * 2", granularity="weekly")) == "0 9 * * 2"
    assert monitor_schedule(SimpleNamespace(schedule=None, granularity="daily")) == "daily"
    assert monitor_schedule(SimpleNamespace(schedule="", granularity="monthly")) == "monthly"
