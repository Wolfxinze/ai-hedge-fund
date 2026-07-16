"""Lock-window regression: refresh_pool must NOT hold the SQLite write lock across
the multi-minute committee (run_analysts) phase.

Root cause guarded here: ``upsert_candidates`` flushes DML, so the write lock was
acquired pre-committee and held until the caller's commit — every concurrent writer
(e.g. POST /serenity/discover) died with 'database is locked' after the 30s busy
timeout. The fix commits the RUNNING run row BEFORE run_analysts, releasing the lock.

These tests use a REAL tmp_path file-backed SQLite engine — in-memory DBs do not
exercise file-level locking, so a genuinely separate connection is needed to prove
the lock was released.
"""

import sqlite3
from unittest import mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.storage.models as m
from src.observing_pools.pipeline import RefreshConfig, refresh_pool

UNIVERSE = "data/universes/ai_seed.csv"


@pytest.fixture
def db(tmp_path):
    """A file-backed SQLite engine + its path (file locks require a real file)."""
    path = tmp_path / "lockwin.db"
    engine = create_engine(f"sqlite:///{path}")
    m.Base.metadata.create_all(engine)
    return engine, str(path)


def _bullish_signals(tickers, selected):
    signals: dict[str, dict] = {f"{k}_agent": {} for k in selected}
    for t in tickers:
        for k in selected:
            signals[f"{k}_agent"][t] = {"signal": "bullish", "confidence": 70, "reasoning": "stub"}
    return signals


def test_write_lock_released_during_committee(db):
    """A concurrent writer on a SEPARATE connection must succeed mid-committee.

    FAILS against the pre-fix pipeline: the write lock from upsert_candidates is held
    across run_analysts, so the separate INSERT hits the busy timeout and 'database is
    locked'. GREEN once refresh_pool commits before the committee phase.
    """
    engine, path = db

    # A scratch table on a raw, already-committed connection (not in engine metadata).
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE scratch (id INTEGER PRIMARY KEY)")
    raw.commit()
    raw.close()

    outcome: dict = {"ok": False, "error": None}

    def concurrent_writer_stub(tickers, selected, end_date):
        # Mid-committee: a genuinely separate connection tries to write. If the pipeline
        # still holds the write lock this blocks for busy_timeout then raises 'locked'.
        other = sqlite3.connect(path, timeout=1.0)
        try:
            other.execute("INSERT INTO scratch (id) VALUES (1)")
            other.commit()
            outcome["ok"] = True
        except sqlite3.OperationalError as exc:  # 'database is locked'
            outcome["error"] = str(exc)
        finally:
            other.close()
        return _bullish_signals(tickers, selected), {"calls": len(selected) * len(tickers)}

    session = sessionmaker(bind=engine)()
    config = RefreshConfig(platform_key="ai", universe_csv=UNIVERSE, top_n=5)
    refresh_pool(session, config, concurrent_writer_stub, end_date="2026-06-12")
    session.commit()
    session.close()

    assert outcome["error"] is None, f"concurrent write blocked (lock held across committee): {outcome['error']}"
    assert outcome["ok"] is True

    # And the scratch row is durable — the concurrent commit really landed.
    check = sqlite3.connect(path)
    assert check.execute("SELECT COUNT(*) FROM scratch").fetchone()[0] == 1
    check.close()


def test_run_analysts_crash_marks_failed_and_reraises(db):
    """run_analysts raising must re-raise AND leave a committed, non-RUNNING run row
    with a non-empty error — no orphaned RUNNING rows on crash."""
    engine, path = db

    def boom_stub(tickers, selected, end_date):
        raise RuntimeError("committee exploded")

    session = sessionmaker(bind=engine)()
    config = RefreshConfig(platform_key="ai", universe_csv=UNIVERSE, top_n=5)

    with pytest.raises(RuntimeError, match="committee exploded"):
        refresh_pool(session, config, boom_stub, end_date="2026-06-12")
    session.close()

    # A FRESH session (separate connection) must see the committed failure row.
    fresh = sessionmaker(bind=engine)()
    runs = fresh.query(m.PoolRefreshRun).all()
    assert len(runs) == 1
    run = runs[0]
    assert run.status != m.RefreshRunStatus.RUNNING.value
    assert run.error  # non-empty summary
    fresh.close()


def test_dry_run_never_commits(db):
    """dry_run=True: the pipeline must never call session.commit() (byte-identical no-op)."""
    engine, _ = db
    session = sessionmaker(bind=engine)()
    spy = mock.Mock(wraps=session.commit)
    session.commit = spy

    config = RefreshConfig(platform_key="ai", universe_csv=UNIVERSE, top_n=5, dry_run=True)
    refresh_pool(session, config, lambda t, s, e: (_bullish_signals(t, s), {"calls": 0}), end_date="2026-06-12")
    session.close()

    spy.assert_not_called()
