"""Phase 8: PoolLock claim-row concurrency (PRD §10 / X1). Fully OFFLINE — in-memory SQLite via
StaticPool so the separate short transactions (claim / refresh / release) share one DB; an
injectable clock drives expiry with no wall-clock sleeps; refresh_pool is stubbed (no LLM).

Tests encode WHY: (1) same platform serialises, different platforms proceed; (2) an expired lock
is stolen with the fence bumped; (3) the FENCE makes a stale holder's release a no-op so it can't
clobber the new holder (the lost-update guard); (4) 'database is locked' is surfaced, never
swallowed; (5) refresh_pool_locked releases the lock even when the refresh raises.
"""

import contextlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.storage.models as m
from src.observing_pools import pool_lock as pl
from src.observing_pools.pipeline import RefreshConfig
from src.observing_pools.scoring import FORMULA_4COMP_RH1
from src.observing_pools.pool_lock import (
    acquire_pool_lock,
    PoolLockContendedError,
    PoolLockDatabaseLockedError,
    refresh_pool_locked,
    release_pool_lock,
)
from src.storage.models import PoolLock

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def factory():
    """A session_scope-shaped factory over one shared in-memory DB (StaticPool)."""
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


def _at(t: datetime):
    return lambda: t


# ── claim / contention / steal ───────────────────────────────────────────────


def test_fresh_acquire_returns_fence_one(factory):
    with factory() as s:
        assert acquire_pool_lock(s, "ai", "A", clock=_at(_T0)) == 1
    with factory() as s:
        lock = s.get(PoolLock, "ai")
        assert lock.locked_by == "A" and lock.fence == 1


def test_same_platform_live_lock_is_contended(factory):
    with factory() as s:
        acquire_pool_lock(s, "ai", "A", clock=_at(_T0), ttl_seconds=3600)
    with factory() as s:
        with pytest.raises(PoolLockContendedError):
            acquire_pool_lock(s, "ai", "B", clock=_at(_T0 + timedelta(seconds=5)), ttl_seconds=3600)
    with factory() as s:
        assert s.get(PoolLock, "ai").locked_by == "A"  # original holder unchanged


def test_different_platforms_are_independent(factory):
    with factory() as s:
        acquire_pool_lock(s, "ai", "A", clock=_at(_T0))
    with factory() as s:
        assert acquire_pool_lock(s, "robotics", "B", clock=_at(_T0)) == 1  # no contention
    with factory() as s:
        assert s.get(PoolLock, "ai").locked_by == "A"
        assert s.get(PoolLock, "robotics").locked_by == "B"


def test_expired_lock_is_stolen_with_fence_bumped(factory):
    with factory() as s:
        assert acquire_pool_lock(s, "ai", "A", clock=_at(_T0), ttl_seconds=10) == 1  # expires T0+10
    with factory() as s:
        # now is past expiry → steal; fence 1 → 2
        assert acquire_pool_lock(s, "ai", "B", clock=_at(_T0 + timedelta(seconds=20)), ttl_seconds=10) == 2
    with factory() as s:
        assert s.get(PoolLock, "ai").locked_by == "B"


# ── fenced release (the lost-update guard) ───────────────────────────────────


def test_fenced_release_owner_succeeds(factory):
    with factory() as s:
        fence = acquire_pool_lock(s, "ai", "A", clock=_at(_T0))
    with factory() as s:
        assert release_pool_lock(s, "ai", fence, "A") is True
    with factory() as s:
        assert s.get(PoolLock, "ai") is None


def test_fenced_release_after_steal_is_noop(factory):
    """A slow holder A whose expired lock was stolen by B must NOT clobber B's lock on release."""
    with factory() as s:
        f_a = acquire_pool_lock(s, "ai", "A", clock=_at(_T0), ttl_seconds=10)  # fence 1
    with factory() as s:
        f_b = acquire_pool_lock(s, "ai", "B", clock=_at(_T0 + timedelta(seconds=20)), ttl_seconds=10)  # steal → fence 2
    with factory() as s:
        assert release_pool_lock(s, "ai", f_a, "A") is False  # stale fence → no-op
    with factory() as s:
        lock = s.get(PoolLock, "ai")
        assert lock is not None and lock.fence == f_b and lock.locked_by == "B"  # B's lock intact


def test_database_locked_is_surfaced(factory, monkeypatch):
    """SQLite 'database is locked' under contention must surface, never be silently swallowed."""
    with factory() as s:
        def boom(*a, **k):
            raise OperationalError("UPDATE pool_locks ...", {}, Exception("database is locked"))

        monkeypatch.setattr(s, "execute", boom)
        with pytest.raises(PoolLockDatabaseLockedError):
            acquire_pool_lock(s, "ai", "A", clock=_at(_T0))


# ── refresh_pool_locked orchestration ────────────────────────────────────────


def _stub_run(**over):
    base = {"status": "complete", "error": None, "summary": {"ranked": 1}, "id": 7}
    base.update(over)
    return SimpleNamespace(**base)


def test_refresh_pool_locked_acquires_runs_releases(factory, monkeypatch):
    seen = {}

    def stub_refresh(session, config, run_analysts, *, end_date, provider_name="yfinance", fetch_closes=None):
        seen["platform"] = config.platform_key
        # lock row must be present + committed DURING the refresh (the long op runs holding the claim row)
        assert session.get(PoolLock, "ai") is not None
        return _stub_run()

    monkeypatch.setattr(pl, "refresh_pool", stub_refresh)
    cfg = RefreshConfig(platform_key="ai", universe_csv="x.csv")
    outcome = refresh_pool_locked(cfg, lambda *a, **k: ({}, {}), end_date="2026-01-01", run_id="A", session_factory=factory)
    assert outcome.status == "complete" and outcome.fence == 1 and outcome.db_run_id == 7
    assert seen["platform"] == "ai"
    with factory() as s:
        assert s.get(PoolLock, "ai") is None  # released after the run


def test_refresh_pool_locked_releases_on_exception(factory, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("refresh exploded")

    monkeypatch.setattr(pl, "refresh_pool", boom)
    cfg = RefreshConfig(platform_key="ai", universe_csv="x.csv")
    with pytest.raises(RuntimeError):
        refresh_pool_locked(cfg, lambda *a, **k: ({}, {}), end_date="2026-01-01", run_id="A", session_factory=factory)
    with factory() as s:
        assert s.get(PoolLock, "ai") is None  # finally released the lock despite the crash


def test_refresh_pool_locked_second_caller_contended(factory, monkeypatch):
    """Same platform: while A holds the lock, a second locked refresh raises Contended (serialise)."""
    monkeypatch.setattr(pl, "refresh_pool", lambda *a, **k: _stub_run())
    cfg = RefreshConfig(platform_key="ai", universe_csv="x.csv")
    # Pre-seed a live lock for "ai" held by someone else.
    with factory() as s:
        acquire_pool_lock(s, "ai", "other", clock=pl._utc_now, ttl_seconds=3600)
    with pytest.raises(PoolLockContendedError):
        refresh_pool_locked(cfg, lambda *a, **k: ({}, {}), end_date="2026-01-01", run_id="A", session_factory=factory)


def _bullish_run(tickers, selected, end_date):
    """All-bullish committee → every candidate has a non-None momentum component, so the
    rh1 haircut path actually consults fetch_closes (spend-disciplined skip only on None)."""
    signals = {f"{k}_agent": {t: {"signal": "bullish", "confidence": 90, "reasoning": "stub"} for t in tickers} for k in selected}
    return signals, {"calls": 1}


def _closes60():
    closes = [100.0]
    for i in range(60):
        closes.append(closes[-1] * (1.02 if i % 2 == 0 else 0.98))
    return closes


def test_refresh_pool_locked_forwards_fetch_closes_for_rh1(factory):
    """An rh1 formula_version routed through refresh_pool_locked MUST forward the injected
    fetch_closes into refresh_pool; otherwise refresh_pool fails loud (rh1 + fetch_closes=None
    → ValueError). Uses the REAL refresh_pool (not stubbed) so the forward is proven end-to-end:
    the run does NOT raise ValueError AND the fake fetch_closes is actually invoked by the
    haircut path (not merely accepted and ignored). Fails before this task's change."""
    called: list[str] = []

    def fake_fetch(ticker, end_date):
        called.append(ticker)
        return _closes60()

    cfg = RefreshConfig(
        platform_key="ai",
        universe_csv="data/universes/ai_seed.csv",
        top_n=30,
        token_budget=100_000,
        formula_version=FORMULA_4COMP_RH1,
    )
    outcome = refresh_pool_locked(
        cfg, _bullish_run, end_date="2026-06-12", run_id="A", session_factory=factory, fetch_closes=fake_fetch
    )
    assert outcome.status in ("complete", "partial")  # ran to completion — no rh1 ValueError
    assert called  # the fake reached refresh_pool's haircut path and was actually invoked
    with factory() as s:
        assert s.get(PoolLock, "ai") is None  # lock released after the run


# ── real concurrency + error-typing ──────────────────────────────────────────


def test_non_lock_operational_error_reraises(factory, monkeypatch):
    """A non-'database is locked' OperationalError must propagate AS-IS, never be masked as a lock
    error — else a real DB fault (e.g. missing table) would look like a benign 'contended' skip."""
    with factory() as s:
        def boom(*a, **k):
            raise OperationalError("SELECT ...", {}, Exception("no such table: pool_locks"))

        monkeypatch.setattr(s, "execute", boom)
        with pytest.raises(OperationalError):  # NOT PoolLockDatabaseLockedError
            acquire_pool_lock(s, "ai", "A", clock=_at(_T0))


def test_claim_race_exactly_one_winner(tmp_path):
    """Two threads race to steal the SAME expired lock on a real FILE-backed SQLite (two real
    connections + busy_timeout). The atomic steal-if-expired UPDATE must yield EXACTLY one winner
    — this is the only test that would catch a SELECT→UPDATE TOCTOU regression (which a
    single-threaded test cannot)."""
    import threading

    db = tmp_path / "lock_race.db"
    engine = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False, "timeout": 30})
    m.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    @contextlib.contextmanager
    def factory():
        s = Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    with factory() as s:  # seed an expired lock
        acquire_pool_lock(s, "ai", "A", clock=_at(_T0), ttl_seconds=10)  # expires T0+10

    now = _at(_T0 + timedelta(seconds=100))  # well past expiry
    barrier = threading.Barrier(2)
    results: list = []
    rlock = threading.Lock()

    def contend(run_id):
        barrier.wait()  # maximise the overlap
        try:
            with factory() as s:
                fence = acquire_pool_lock(s, "ai", run_id, clock=now, ttl_seconds=10)
            with rlock:
                results.append(("won", run_id, fence))
        except (PoolLockContendedError, PoolLockDatabaseLockedError) as exc:
            with rlock:
                results.append(("lost", run_id, type(exc).__name__))

    threads = [threading.Thread(target=contend, args=(rid,)) for rid in ("B", "C")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    won = [r for r in results if r[0] == "won"]
    assert len(won) == 1  # EXACTLY one winner — the atomicity invariant
    with factory() as s:
        held = s.get(PoolLock, "ai")
        assert held.locked_by == won[0][1] and held.fence == 2  # the sole winner owns the bumped fence
    engine.dispose()
