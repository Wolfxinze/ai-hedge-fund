"""Phase 9: monitor CRUD + manual run, plus GET /opportunity-reports/{id}. Fully offline
(StaticPool + injected analyzing_flow stub — no uv subprocess / LLM / network).

These prove: create is 409-on-duplicate (never the silent upsert-clobber), PATCH is a true partial
update (omitted fields untouched, explicit null clears a nullable col), a too-frequent schedule is
rejected 422 (Issue #18), the manual run reaches only run_monitor -> serialize_report so every
persisted report carries a disclaimer (research-only, never a trade), and a single failing ticker
degrades rather than aborting the run.
"""

import contextlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session as SASession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.storage.models as m
from app.backend.database.connection import get_db
from app.backend.routes.monitors import get_analyzing_flow
from app.backend.routes.monitors import router as monitors_router
from app.backend.routes.observing_pools import router as pools_router
from src.integrations.tradingagents_adapter import AnalyzingFlowResult
from src.monitoring.serialize import DisclaimerError
from src.storage.models import MonitorConfig, OpportunityReport, ReportLabel

_DESCRIPTIVE = {label.value for label in ReportLabel}


def _ok_flow(seen=None):
    def flow(ticker, trade_date):
        if seen is not None:
            seen.append((ticker, trade_date))
        return AnalyzingFlowResult(ticker, ReportLabel.THESIS_SUPPORTIVE, 70.0, False, f"{ticker} thesis intact", raw_decision="Buy")

    return flow


@pytest.fixture
def env():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    m.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def override_get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI()
    app.include_router(pools_router)
    app.include_router(monitors_router)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_analyzing_flow] = lambda: _ok_flow()

    class Env:
        client = TestClient(app)
        SessionLocal = Session

        @staticmethod
        def set_flow(flow):
            app.dependency_overrides[get_analyzing_flow] = lambda: flow

    return Env()


# ── create / list ────────────────────────────────────────────────────────────


def test_create_and_list_monitor(env):
    r = env.client.post("/monitors", json={"name": "AI weekly", "tickers": ["nvda", "msft"], "granularity": "weekly"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "AI weekly" and body["tickers"] == ["NVDA", "MSFT"] and body["enabled"] is True
    # persisted (committed, visible to a fresh session) — not just flushed
    with contextlib.closing(env.SessionLocal()) as s:
        assert s.query(MonitorConfig).filter_by(name="AI weekly").one().granularity == "weekly"
    listed = env.client.get("/monitors").json()
    assert any(mon["name"] == "AI weekly" for mon in listed)


def test_create_duplicate_name_is_409_and_does_not_clobber(env):
    env.client.post("/monitors", json={"name": "dup", "tickers": ["NVDA"], "granularity": "weekly"})
    r = env.client.post("/monitors", json={"name": "dup", "tickers": ["TSLA"], "granularity": "daily"})
    assert r.status_code == 409
    with contextlib.closing(env.SessionLocal()) as s:  # original untouched (no upsert-clobber)
        mon = s.query(MonitorConfig).filter_by(name="dup").one()
        assert mon.tickers == ["NVDA"] and mon.granularity == "weekly"


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "x", "tickers": ["bad ticker!"]},
        {"name": "x", "tickers": ["NVDA"], "granularity": "yearly"},
        {"name": "x", "tickers": ["NVDA"], "platform_keys": ["not_a_platform"]},
        {"name": "x", "tickers": ["NVDA"], "schedule": "*/5 * * * *"},  # Issue #18: sub-floor schedule
        {"name": "", "tickers": ["NVDA"]},
        {"name": "x", "tickers": []},
    ],
)
def test_create_validation_422(env, payload):
    assert env.client.post("/monitors", json=payload).status_code == 422


def test_create_too_frequent_schedule_message(env):
    r = env.client.post("/monitors", json={"name": "spammy", "tickers": ["NVDA"], "schedule": "*/2 * * * *"})
    assert r.status_code == 422
    assert "MONITOR_MIN_INTERVAL_SECONDS" in str(r.json()["detail"])


# ── patch ──────────────────────────────────────────────────────────────────────


def test_patch_partial_leaves_other_fields(env):
    mid = env.client.post("/monitors", json={"name": "m1", "tickers": ["NVDA"], "granularity": "weekly", "platform_keys": ["ai"]}).json()["id"]
    r = env.client.patch(f"/monitors/{mid}", json={"enabled": False})
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False and body["tickers"] == ["NVDA"] and body["granularity"] == "weekly" and body["platform_keys"] == ["ai"]


def test_patch_explicit_null_clears_nullable(env):
    mid = env.client.post("/monitors", json={"name": "m2", "tickers": ["NVDA"], "platform_keys": ["ai"]}).json()["id"]
    assert env.client.patch(f"/monitors/{mid}", json={"platform_keys": None}).json()["platform_keys"] is None
    # omitting it on a later patch leaves it cleared
    assert env.client.patch(f"/monitors/{mid}", json={"enabled": True}).json()["platform_keys"] is None


def test_patch_unknown_id_404(env):
    assert env.client.patch("/monitors/9999", json={"enabled": False}).status_code == 404


def test_patch_too_frequent_schedule_422(env):
    mid = env.client.post("/monitors", json={"name": "m3", "tickers": ["NVDA"], "granularity": "weekly"}).json()["id"]
    assert env.client.patch(f"/monitors/{mid}", json={"schedule": "*/1 * * * *"}).status_code == 422


# ── run + report read ────────────────────────────────────────────────────────


def test_run_persists_reports_each_with_disclaimer(env):
    mid = env.client.post("/monitors", json={"name": "runnable", "tickers": ["NVDA", "MSFT", "TSLA"]}).json()["id"]
    r = env.client.post(f"/monitors/{mid}/run", json={"trade_date": "2026-06-12"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["reports"]) == 3 and body["degraded_count"] == 0 and body["any_degraded"] is False
    for report in body["reports"]:
        assert report["disclaimer"].strip() and report["disclaimer_version"].strip()  # chokepoint invariant survives to the API
        assert report["label"] in _DESCRIPTIVE  # descriptive, never buy/sell wording


def test_run_one_failing_ticker_degrades_not_aborts(env):
    def flaky(ticker, trade_date):
        if ticker == "BOOM":
            raise RuntimeError("flow exploded")
        return AnalyzingFlowResult(ticker, ReportLabel.MIXED, 50.0, False, "ok")

    env.set_flow(flaky)
    mid = env.client.post("/monitors", json={"name": "flaky", "tickers": ["NVDA", "BOOM", "MSFT"]}).json()["id"]
    body = env.client.post(f"/monitors/{mid}/run").json()
    assert body["degraded_count"] == 1 and body["any_degraded"] is True and len(body["reports"]) == 3
    boom = next(rep for rep in body["reports"] if rep["ticker"] == "BOOM")
    assert boom["degraded"] is True and boom["label"] == ReportLabel.INSUFFICIENT_EVIDENCE.value


def test_run_default_trade_date_and_passthrough(env):
    seen: list = []
    env.set_flow(_ok_flow(seen))
    mid = env.client.post("/monitors", json={"name": "dated", "tickers": ["NVDA"]}).json()["id"]
    env.client.post(f"/monitors/{mid}/run", json={"trade_date": "2026-01-15"})
    assert seen[0][1] == "2026-01-15"
    seen.clear()
    env.client.post(f"/monitors/{mid}/run")  # no body → default today
    assert seen and len(seen[0][1]) == 10  # an ISO date was passed


def test_run_invalid_trade_date_422(env):
    mid = env.client.post("/monitors", json={"name": "baddate", "tickers": ["NVDA"]}).json()["id"]
    assert env.client.post(f"/monitors/{mid}/run", json={"trade_date": "not-a-date"}).status_code == 422


def test_run_unknown_monitor_404(env):
    assert env.client.post("/monitors/9999/run").status_code == 404


def test_get_opportunity_report_by_id(env):
    mid = env.client.post("/monitors", json={"name": "reportable", "tickers": ["NVDA"]}).json()["id"]
    env.client.post(f"/monitors/{mid}/run", json={"trade_date": "2026-06-12"})
    report_id = env.client.get("/opportunity-reports").json()[0]["id"]
    got = env.client.get(f"/opportunity-reports/{report_id}")
    assert got.status_code == 200 and got.json()["id"] == report_id and got.json()["disclaimer"].strip()
    assert env.client.get("/opportunity-reports/999999").status_code == 404


def test_get_report_without_disclaimer_is_refused(env):
    """The serialize_report chokepoint must REFUSE a disclaimer-less report at the GET-by-id route too
    (§9.9) — fail loud, not a blanked 200. Guards against someone weakening serialize_report.

    The seed uses a non-breaking space: the Phase-11 DB CHECK (whose SQLite trim set is ASCII
    whitespace) admits it, but serialize_report's Unicode-aware .strip() still refuses it — so this
    exercises the serialize layer independently of the DB CHECK (the two compose)."""
    with contextlib.closing(env.SessionLocal()) as s:
        s.add(OpportunityReport(ticker="NVDA", label="mixed", disclaimer="\xa0", disclaimer_version="2026-06"))
        s.commit()
        rid = s.query(OpportunityReport).one().id
    with pytest.raises(DisclaimerError):
        env.client.get(f"/opportunity-reports/{rid}")


# ── review-fold regression tests (security + silent-failure findings) ─────────


def test_patch_null_on_not_null_fields_is_422(env):
    """Explicit null on a NOT-NULL column (granularity/enabled) must be a 422 at the boundary, not an
    opaque IntegrityError 500 (silent-failure F1)."""
    mid = env.client.post("/monitors", json={"name": "nn", "tickers": ["NVDA"], "granularity": "weekly"}).json()["id"]
    assert env.client.patch(f"/monitors/{mid}", json={"granularity": None}).status_code == 422
    assert env.client.patch(f"/monitors/{mid}", json={"enabled": None}).status_code == 422
    assert env.client.patch(f"/monitors/{mid}", json={"tickers": None}).status_code == 422


def test_create_too_many_tickers_is_422(env):
    assert env.client.post("/monitors", json={"name": "big", "tickers": [f"T{i}" for i in range(101)]}).status_code == 422


def test_patch_too_many_tickers_is_422(env):
    mid = env.client.post("/monitors", json={"name": "growable", "tickers": ["NVDA"]}).json()["id"]
    assert env.client.patch(f"/monitors/{mid}", json={"tickers": [f"T{i}" for i in range(101)]}).status_code == 422


def test_list_monitors_limit_is_bounded(env):
    env.client.post("/monitors", json={"name": "one", "tickers": ["NVDA"]})
    assert len(env.client.get("/monitors?limit=1").json()) == 1
    assert env.client.get("/monitors?limit=0").status_code == 422
    assert env.client.get("/monitors?limit=501").status_code == 422


def test_reenable_subfloor_monitor_is_422(env):
    """A sub-floor schedule stored out-of-band (direct DB / CLI, before the #18 floor) must not be
    re-armed via PATCH {enabled: true} — the API re-validates the effective schedule on enable (sec F4)."""
    with contextlib.closing(env.SessionLocal()) as s:
        s.add(MonitorConfig(name="legacy", tickers=["NVDA"], granularity="weekly", schedule="*/5 * * * *", enabled=False))
        s.commit()
        mid = s.query(MonitorConfig).filter_by(name="legacy").one().id
    assert env.client.patch(f"/monitors/{mid}", json={"enabled": True}).status_code == 422


def test_patch_name_is_ignored(env):
    """Name is immutable on PATCH (no `name` field) — a name in the body is silently dropped, never
    honored (locks the documented invariant against the unique-collision path reopening)."""
    mid = env.client.post("/monitors", json={"name": "original", "tickers": ["NVDA"]}).json()["id"]
    assert env.client.patch(f"/monitors/{mid}", json={"name": "renamed", "enabled": False}).json()["name"] == "original"


def test_create_invalid_granularity_message_precedes_schedule(env):
    """An unknown granularity 422s on GRANULARITY grounds (validation order), not a confusing 'invalid
    schedule' message — locks the _validate_granularity-before-_validate_schedule order."""
    r = env.client.post("/monitors", json={"name": "g", "tickers": ["NVDA"], "granularity": "yearly"})
    assert r.status_code == 422 and "granularity" in str(r.json()["detail"])


def test_run_all_tickers_degraded_still_commits(env):
    """Even a 100%-degraded run persists every degraded report (durable provenance) and returns 200
    with degraded_count == len(tickers) — degrade is surfaced, never a silent skip."""
    env.set_flow(lambda ticker, trade_date: (_ for _ in ()).throw(RuntimeError("all fail")))
    mid = env.client.post("/monitors", json={"name": "alldead", "tickers": ["NVDA", "MSFT"]}).json()["id"]
    body = env.client.post(f"/monitors/{mid}/run").json()
    assert body["degraded_count"] == 2 and body["any_degraded"] is True
    assert len(env.client.get("/opportunity-reports").json()) == 2  # committed + readable in a fresh request


def test_create_commit_integrityerror_maps_to_409():
    """The concurrent-create unique-name race (pre-check passes, commit fails) maps to a clean 409 +
    rollback, not a raw 500 (silent-failure F2 / code-review LOW-1)."""

    class _BoomCommit(SASession):
        def commit(self):
            raise IntegrityError("INSERT INTO monitor_configs", {}, Exception("UNIQUE constraint failed: monitor_configs.name"))

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    m.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, class_=_BoomCommit)

    def override_get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI()
    app.include_router(monitors_router)
    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    assert client.post("/monitors", json={"name": "racy", "tickers": ["NVDA"]}).status_code == 409


# ── database-locked → 503 (issue #21: parity with the refresh route) ──────────
# These prove the ERROR-MAPPING only: a SQLite 'database is locked' on a monitor write commit is
# surfaced as 503 (retryable), not an opaque 500. This is NOT a concurrency-atomicity claim — the
# real two-writer race guarantees live in PoolLock's file-backed threaded test.

class _LockedCommit(SASession):
    def commit(self):
        raise OperationalError("commit", {}, Exception("database is locked"))


def _locked_client():
    """(client whose DB session raises 'database is locked' on commit, seed-sessionmaker)."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    m.Base.metadata.create_all(engine)
    Seed = sessionmaker(bind=engine)  # normal session for seeding rows (commits fine)
    Locked = sessionmaker(bind=engine, class_=_LockedCommit)

    def override_get_db():
        s = Locked()
        try:
            yield s
        finally:
            s.close()

    app = FastAPI()
    app.include_router(monitors_router)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_analyzing_flow] = lambda: _ok_flow()
    return TestClient(app), Seed


def _seed_monitor(Seed, name="m1"):
    s = Seed()
    try:
        mon = MonitorConfig(name=name, tickers=["NVDA"], granularity="weekly", enabled=True)
        s.add(mon)
        s.commit()
        s.refresh(mon)
        return mon.id
    finally:
        s.close()


def test_create_returns_503_on_db_locked():
    client, _ = _locked_client()
    r = client.post("/monitors", json={"name": "x", "tickers": ["NVDA"]})
    assert r.status_code == 503
    assert "locked" in r.json()["detail"].lower()


def test_patch_returns_503_on_db_locked():
    client, Seed = _locked_client()
    mid = _seed_monitor(Seed)
    assert client.patch(f"/monitors/{mid}", json={"granularity": "monthly"}).status_code == 503


def test_run_returns_503_on_db_locked():
    client, Seed = _locked_client()
    mid = _seed_monitor(Seed)
    assert client.post(f"/monitors/{mid}/run", json={"trade_date": "2026-06-12"}).status_code == 503


# ── flush-locked path: run_monitor/create_monitor flush BEFORE the route commit, so a locked DB
# on the FLUSH (not just the commit) must also map to 503. These FAIL without the context-manager
# guard around the whole write — _LockedCommit (which overrides only commit) cannot catch them.
def _raise_locked(*_a, **_k):
    raise OperationalError("flush", {}, Exception("database is locked"))


def test_run_returns_503_on_flush_locked(env, monkeypatch):
    import app.backend.routes.monitors as mon

    s = env.SessionLocal()
    row = MonitorConfig(name="m", tickers=["NVDA"], granularity="weekly", enabled=True)
    s.add(row)
    s.commit()
    mid = row.id
    s.close()
    monkeypatch.setattr(mon, "run_monitor", _raise_locked)  # realistic mid-run flush-locked failure
    assert env.client.post(f"/monitors/{mid}/run", json={"trade_date": "2026-06-12"}).status_code == 503


def test_create_returns_503_on_flush_locked(env, monkeypatch):
    import app.backend.routes.monitors as mon

    monkeypatch.setattr(mon, "create_monitor", _raise_locked)  # locked on create's flush
    assert env.client.post("/monitors", json={"name": "x", "tickers": ["NVDA"]}).status_code == 503
