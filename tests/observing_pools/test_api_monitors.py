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
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.storage.models as m
from app.backend.database.connection import get_db
from app.backend.routes.monitors import get_analyzing_flow
from app.backend.routes.monitors import router as monitors_router
from app.backend.routes.observing_pools import router as pools_router
from src.integrations.tradingagents_adapter import AnalyzingFlowResult
from src.storage.models import MonitorConfig, ReportLabel

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
