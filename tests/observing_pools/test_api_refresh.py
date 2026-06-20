"""Phase 9: POST /observing-pools/refresh + GET /observing-pools/refresh-runs.

Fully offline (StaticPool in-memory + dependency overrides; refresh stubbed — no LLM/network). These
prove the API CONTRACT: the request reaches only refresh_pool_locked (research-only, never a trade
path), PoolLockContendedError -> 409 and PoolLockDatabaseLockedError -> 503 are surfaced (never
swallowed), dry_run runs UNLOCKED, and the provenance list serialises every PoolRefreshRun column.
The request-time TOCTOU under real connection concurrency is proven separately in
``test_api_refresh_concurrency.py`` (file-backed) — StaticPool serialises on one connection and
cannot catch it.
"""

import contextlib
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.observing_pools.pool_lock as plmod
import src.storage.models as m
from app.backend.database.connection import get_db
from app.backend.routes.observing_pools import (
    get_refresh_runner,
    get_session_factory,
    router,
)
from src.observing_pools.pool_lock import acquire_pool_lock, PoolLockDatabaseLockedError
from src.storage.models import PoolLock, PoolRefreshRun, RefreshRunStatus

_STUB_RUNNER = lambda tickers, selected, end_date: ({}, {"calls": 0})  # noqa: E731 — never hits the LLM


def _stub_run(**over):
    base = {"status": "complete", "error": None, "summary": {"ranked": 2, "data_unavailable": 0, "candidates": 2, "top_tickers": ["NVDA", "MSFT"]}, "id": 42}
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def env():
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

    def override_get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_refresh_runner] = lambda: _STUB_RUNNER
    return SimpleNamespace(client=TestClient(app), Session=Session, session_factory=session_factory)


# ── POST /observing-pools/refresh ────────────────────────────────────────────


def test_dry_run_is_unlocked_and_persists_nothing(env):
    r = env.client.post("/observing-pools/refresh", json={"platform_key": "ai", "dry_run": True})
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True and body["id"] is None and body["platform_key"] == "ai"
    assert "summary" in body
    with env.session_factory() as s:
        assert s.query(PoolRefreshRun).count() == 0  # dry-run wrote no run
        assert s.query(PoolLock).count() == 0  # and no lock row


def test_non_dry_run_returns_id_and_releases_lock(env, monkeypatch):
    monkeypatch.setattr(plmod, "refresh_pool", lambda *a, **k: _stub_run())
    r = env.client.post("/observing-pools/refresh", json={"platform_key": "ai", "dry_run": False})
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == 42 and body["status"] == "complete" and body["dry_run"] is False
    assert body["summary"]["top_tickers"] == ["NVDA", "MSFT"]
    with env.session_factory() as s:
        assert s.get(PoolLock, "ai") is None  # fenced release freed the lock after the run


def test_unknown_platform_is_404(env):
    r = env.client.post("/observing-pools/refresh", json={"platform_key": "not_a_platform"})
    assert r.status_code == 404
    assert "not_a_platform" in r.json()["detail"]


def test_contended_lock_is_409_and_leaves_holder_untouched(env, monkeypatch):
    monkeypatch.setattr(plmod, "refresh_pool", lambda *a, **k: _stub_run())  # must never be reached
    with env.session_factory() as s:  # pre-seed a LIVE lock held by someone else
        acquire_pool_lock(s, "ai", "other-runner", ttl_seconds=3600)
    r = env.client.post("/observing-pools/refresh", json={"platform_key": "ai", "dry_run": False})
    assert r.status_code == 409
    with env.session_factory() as s:
        held = s.get(PoolLock, "ai")
        assert held is not None and held.locked_by == "other-runner" and held.fence == 1  # claim raised BEFORE the body; finally did not clobber the other holder


def test_database_locked_is_503_not_swallowed(env, monkeypatch):
    def boom(*a, **k):
        raise PoolLockDatabaseLockedError("ai")

    monkeypatch.setattr(plmod, "acquire_pool_lock", boom)
    r = env.client.post("/observing-pools/refresh", json={"platform_key": "ai", "dry_run": False})
    assert r.status_code == 503


def test_top_n_is_bounded(env):
    assert env.client.post("/observing-pools/refresh", json={"platform_key": "ai", "top_n": 0, "dry_run": True}).status_code == 422
    assert env.client.post("/observing-pools/refresh", json={"platform_key": "ai", "top_n": 10000, "dry_run": True}).status_code == 422


def test_missing_platform_key_is_422(env):
    assert env.client.post("/observing-pools/refresh", json={"dry_run": True}).status_code == 422


# ── GET /observing-pools/refresh-runs ────────────────────────────────────────


def _seed_runs(env):
    with env.session_factory() as s:
        s.add(PoolRefreshRun(status=RefreshRunStatus.COMPLETE.value, provider_name="yfinance", platform_keys=["ai"], candidate_count=3, summary={"ranked": 3}))
        s.add(PoolRefreshRun(status=RefreshRunStatus.PARTIAL.value, provider_name="yfinance", platform_keys=["robotics"], candidate_count=1, fetch_errors={"degraded_analysts": ["x"]}))
        s.add(PoolRefreshRun(status=RefreshRunStatus.COMPLETE.value, provider_name="yfinance", platform_keys=["ai"], candidate_count=2))


def test_refresh_runs_newest_first_with_full_provenance(env):
    _seed_runs(env)
    r = env.client.get("/observing-pools/refresh-runs")
    assert r.status_code == 200
    runs = r.json()
    assert isinstance(runs, list) and len(runs) == 3
    assert [run["id"] for run in runs] == sorted([run["id"] for run in runs], reverse=True)  # newest (highest id) first
    first = runs[0]
    for key in ("id", "started_at", "completed_at", "status", "provider_name", "universe_source", "universe_version", "composite_formula_version", "platform_keys", "candidate_count", "fetch_errors", "rejected", "token_cost", "summary", "error"):
        assert key in first
    assert first["completed_at"] is None  # nullable timestamp serialises as null, not an error


def test_refresh_runs_limit_bounded(env):
    _seed_runs(env)
    assert len(env.client.get("/observing-pools/refresh-runs?limit=1").json()) == 1
    assert env.client.get("/observing-pools/refresh-runs?limit=0").status_code == 422
    assert env.client.get("/observing-pools/refresh-runs?limit=201").status_code == 422


def test_refresh_runs_platform_and_status_filters(env):
    _seed_runs(env)
    ai = env.client.get("/observing-pools/refresh-runs?platform_key=ai").json()
    assert len(ai) == 2 and all("ai" in run["platform_keys"] for run in ai)
    complete = env.client.get("/observing-pools/refresh-runs?status=complete").json()
    assert all(run["status"] == "complete" for run in complete) and len(complete) == 2
    assert env.client.get("/observing-pools/refresh-runs?platform_key=bogus").status_code == 404


def test_refresh_runs_route_not_shadowed_by_platform_key(env):
    """Regression: GET /observing-pools/refresh-runs must NOT be captured by GET
    /observing-pools/{platform_key}. A list response (not the {platform_key, count, entries} shape)
    proves the literal route is declared before the parameterised one."""
    r = env.client.get("/observing-pools/refresh-runs")
    assert r.status_code == 200 and isinstance(r.json(), list)


def test_module_import_does_not_pull_scoring_graph():
    """Importing the routes module must stay offline: the heavy scoring_graph/LLM stack is pulled
    only lazily inside get_refresh_runner, never at module import (protects offline-tests-only)."""
    code = "import sys, app.backend.routes.observing_pools; assert 'src.observing_pools.scoring_graph' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)
