"""Phase 9: request-time PoolLock contention on the refresh endpoint, proven against a REAL
FILE-backed SQLite engine with two genuine connections.

Why file-backed (not StaticPool): StaticPool serialises every session onto ONE shared connection,
so two "concurrent" requests actually run sequentially and a cross-connection lock-visibility or
TOCTOU regression would ship green. Here, one request claims the platform's PoolLock (committed +
visible across connections) and blocks holding it inside a stubbed refresh; a concurrent request to
the SAME platform must see the live lock on its own connection and get 409 — never a second 200, and
never corruption. The atomic steal-if-expired claim itself is separately proven in
``test_pool_lock.py::test_claim_race_exactly_one_winner``.
"""

import contextlib
import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.observing_pools.pool_lock as plmod
import src.storage.models as m
from app.backend.database.connection import get_db
from app.backend.routes.observing_pools import (
    get_refresh_runner,
    get_session_factory,
    router,
)
from src.storage.models import PoolLock

_STUB_RUNNER = lambda tickers, selected, end_date: ({}, {"calls": 0})  # noqa: E731


def test_concurrent_same_platform_refresh_one_winner_one_409(tmp_path, monkeypatch):
    db = tmp_path / "refresh_race.db"
    engine = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False, "timeout": 30})
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

    def override_get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    # Blocking stub: the first refresh claims the lock (committed before this is reached) then parks
    # here holding it until released, so a concurrent request genuinely overlaps the held lock.
    holding = threading.Event()
    release = threading.Event()

    def blocking_refresh(session, config, run_analysts, *, end_date, provider_name="yfinance"):
        holding.set()
        assert release.wait(timeout=10), "release was never signalled — test would hang"
        return SimpleNamespace(status="complete", error=None, summary={"ranked": 1}, id=99)

    monkeypatch.setattr(plmod, "refresh_pool", blocking_refresh)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_refresh_runner] = lambda: _STUB_RUNNER
    client = TestClient(app)

    results: dict[str, int] = {}

    def first_request():
        results["first"] = client.post("/observing-pools/refresh", json={"platform_key": "ai", "dry_run": False}).status_code

    t1 = threading.Thread(target=first_request)
    t1.start()
    try:
        assert holding.wait(timeout=10), "first refresh never acquired the lock"
        # While the first holds the lock, a concurrent same-platform refresh must be contended.
        second = client.post("/observing-pools/refresh", json={"platform_key": "ai", "dry_run": False})
        assert second.status_code == 409  # exactly one loser, surfaced — never a second 200
    finally:
        release.set()
        t1.join(timeout=15)

    assert results["first"] == 200  # the holder ran to completion
    with session_factory() as s:
        assert s.get(PoolLock, "ai") is None  # the winner's fenced release freed the lock
    engine.dispose()
