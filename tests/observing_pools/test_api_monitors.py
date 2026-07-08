"""Phase 9 + Phase 9 hot-reload (Issue #21): monitor CRUD + manual run, plus GET /opportunity-reports/{id}.
Fully offline (StaticPool + injected analyzing_flow stub — no uv subprocess / LLM / network).

These prove: create is 409-on-duplicate (never the silent upsert-clobber), PATCH is a true partial
update (omitted fields untouched, explicit null clears a nullable col), a too-frequent schedule is
rejected 422 (Issue #18), the manual run reaches only run_monitor -> serialize_report so every
persisted report carries a disclaimer (research-only, never a trade), and a single failing ticker
degrades rather than aborting the run.

Hot-reload tests (Phase 9 / Issue #21) verify that POST/PATCH register/update/remove jobs on the
live scheduler and that a scheduling failure never fails a successful DB write (best-effort contract).
"""

import contextlib
from unittest.mock import patch

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
from src.scheduler.scheduler import build_scheduler, monitor_job_id, start_scheduler, stop_scheduler
from src.storage.models import MonitorConfig, OpportunityReport, ReportLabel

_DESCRIPTIVE = {label.value for label in ReportLabel}


def _ok_flow(seen=None):
    def flow(ticker, trade_date):
        if seen is not None:
            seen.append((ticker, trade_date))
        return AnalyzingFlowResult(ticker, ReportLabel.THESIS_SUPPORTIVE, 70.0, False, f"{ticker} thesis intact", raw_decision="Buy")

    return flow


def _raf_stub():
    """Stub run_analysts_factory for build_scheduler in tests — no LLM/network."""
    return lambda *a, **k: ({}, {})


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


@pytest.fixture
def env_with_scheduler(monkeypatch):
    """Like ``env`` but wires a NON-started BackgroundScheduler onto app.state.scheduler.
    The scheduler is built with a CM session_factory bound to the SAME in-memory engine so
    build_scheduler can query MonitorConfig rows. The scheduler is NOT started — config/job
    assertions only; no real timer fires, no LLM, no network."""
    monkeypatch.delenv("OBSERVING_POOL_REFRESH_CRON", raising=False)

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    m.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    @contextlib.contextmanager
    def cm_session_factory():
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

    scheduler = build_scheduler(session_factory=cm_session_factory, run_analysts_factory=_raf_stub)

    app = FastAPI()
    app.include_router(pools_router)
    app.include_router(monitors_router)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_analyzing_flow] = lambda: _ok_flow()
    app.state.scheduler = scheduler

    class Env:
        client = TestClient(app)
        SessionLocal = Session
        sch = scheduler

    return Env()


@pytest.fixture
def env_with_started_scheduler(monkeypatch):
    """Like ``env_with_scheduler`` but the BackgroundScheduler is STARTED (a live job store).
    PRODUCTION PARITY: routes always reschedule against a RUNNING scheduler, where two
    ``add_job(replace_existing=True)`` correctly dedup to ONE job with the latest trigger. A
    non-started scheduler instead uses ``_pending_jobs`` semantics that DIVERGE (two adds → two jobs,
    ``get_job`` returns the stale first). Starting the scheduler is the only way these route tests
    exercise the shipped wiring as it actually runs. Teardown GUARANTEES ``stop_scheduler`` so no
    scheduler thread leaks across tests."""
    monkeypatch.delenv("OBSERVING_POOL_REFRESH_CRON", raising=False)

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    m.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    @contextlib.contextmanager
    def cm_session_factory():
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

    scheduler = build_scheduler(session_factory=cm_session_factory, run_analysts_factory=_raf_stub)
    start_scheduler(scheduler)  # live job store: production reschedules against a RUNNING scheduler

    app = FastAPI()
    app.include_router(pools_router)
    app.include_router(monitors_router)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_analyzing_flow] = lambda: _ok_flow()
    app.state.scheduler = scheduler

    class Env:
        client = TestClient(app)
        SessionLocal = Session
        sch = scheduler

    try:
        yield Env()
    finally:
        stop_scheduler(scheduler)  # guaranteed: no scheduler thread leaks across tests


@pytest.fixture(autouse=True)
def _reset_run_semaphore():
    """The run semaphore is a MODULE GLOBAL — a test that acquires it (e.g. the 429 path) could
    poison siblings if it leaked a permit. After each test, drain any stray permits then refill to
    exactly _MAX_CONCURRENT_RUNS, so the global is left fully available no matter what."""
    yield
    from app.backend.routes import monitors as monitors_mod

    while True:  # drain to empty (non-blocking)
        if not monitors_mod._run_semaphore.acquire(blocking=False):
            break
    for _ in range(monitors_mod._MAX_CONCURRENT_RUNS):  # refill to full
        monitors_mod._run_semaphore.release()


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


# ── selected_analysts allowlist (Issue #21) ───────────────────────────────────
# Reject unknown analyst ids against ANALYST_CONFIG at the create/patch boundary (422), instead of
# silently storing a typo'd id that no run can ever resolve. None/empty/omitted carry no constraint.


def _valid_analyst_ids() -> list[str]:
    """Two real ids from the live ANALYST_CONFIG (so the test tracks the registry, not a hardcoded list)."""
    from src.utils.analysts import ANALYST_CONFIG

    return sorted(ANALYST_CONFIG)[:2]


def test_create_unknown_analyst_is_422(env):
    r = env.client.post("/monitors", json={"name": "a1", "tickers": ["NVDA"], "selected_analysts": ["not_a_real_analyst"]})
    assert r.status_code == 422
    assert "not_a_real_analyst" in str(r.json()["detail"])  # the offending id is surfaced


def test_create_valid_analysts_succeeds(env):
    ids = _valid_analyst_ids()
    r = env.client.post("/monitors", json={"name": "a2", "tickers": ["NVDA"], "selected_analysts": ids})
    assert r.status_code == 200
    assert r.json()["selected_analysts"] == ids


def test_create_none_analysts_succeeds(env):
    # explicit null and omitted both carry no constraint
    assert env.client.post("/monitors", json={"name": "a3", "tickers": ["NVDA"], "selected_analysts": None}).status_code == 200
    assert env.client.post("/monitors", json={"name": "a4", "tickers": ["NVDA"]}).status_code == 200


def test_patch_unknown_analyst_is_422(env):
    mid = env.client.post("/monitors", json={"name": "a5", "tickers": ["NVDA"]}).json()["id"]
    r = env.client.patch(f"/monitors/{mid}", json={"selected_analysts": ["nope_not_real"]})
    assert r.status_code == 422
    assert "nope_not_real" in str(r.json()["detail"])


def test_patch_valid_analysts_succeeds(env):
    ids = _valid_analyst_ids()
    mid = env.client.post("/monitors", json={"name": "a6", "tickers": ["NVDA"]}).json()["id"]
    r = env.client.patch(f"/monitors/{mid}", json={"selected_analysts": ids})
    assert r.status_code == 200 and r.json()["selected_analysts"] == ids


def test_create_mixed_valid_and_unknown_analysts_is_422(env):
    """One bad id among valid ones still 422s, and the detail names ONLY the unknown id (not the valid
    ones) — so the error points the caller at the actual typo, not the whole list."""
    valid = _valid_analyst_ids()
    r = env.client.post("/monitors", json={"name": "a7", "tickers": ["NVDA"], "selected_analysts": [*valid, "bogus_id"]})
    assert r.status_code == 422
    detail = str(r.json()["detail"])
    assert "bogus_id" in detail and not any(v in detail for v in valid)  # only the typo is surfaced


def test_patch_null_selected_analysts_clears_then_omit_leaves_unchanged(env):
    """selected_analysts is a NULLABLE column, so explicit null legitimately CLEARS it (unlike
    tickers/granularity/enabled). Create with analysts set → PATCH {selected_analysts: null} clears it
    (200, value None) → a later PATCH with the field OMITTED leaves it cleared (true partial update)."""
    ids = _valid_analyst_ids()
    mid = env.client.post("/monitors", json={"name": "clearable", "tickers": ["NVDA"], "selected_analysts": ids}).json()["id"]
    cleared = env.client.patch(f"/monitors/{mid}", json={"selected_analysts": None})
    assert cleared.status_code == 200 and cleared.json()["selected_analysts"] is None  # null clears
    # omitting selected_analysts on a later patch leaves it cleared (not re-populated)
    assert env.client.patch(f"/monitors/{mid}", json={"enabled": True}).json()["selected_analysts"] is None


@pytest.mark.parametrize("blank", ["", "   "])
def test_create_blank_analyst_id_is_422(env, blank):
    """A blank/whitespace analyst id is not in ANALYST_CONFIG, so it 422s (pins existing behavior: the
    allowlist check rejects it, no silent store of an unresolvable id)."""
    r = env.client.post("/monitors", json={"name": f"blank_{len(blank)}", "tickers": ["NVDA"], "selected_analysts": [blank]})
    assert r.status_code == 422


@pytest.mark.parametrize("blank", ["", "   "])
def test_patch_blank_analyst_id_is_422(env, blank):
    """Same blank-id rejection on the PATCH boundary."""
    mid = env.client.post("/monitors", json={"name": f"blankp_{len(blank)}", "tickers": ["NVDA"]}).json()["id"]
    assert env.client.patch(f"/monitors/{mid}", json={"selected_analysts": [blank]}).status_code == 422


def test_create_too_many_analysts_is_422(env, monkeypatch):
    """selected_analysts is capped at _MAX_ANALYSTS (sibling-consistency with tickers/platform_keys);
    a list above the cap 422s at the Field boundary BEFORE the route body runs. The allowlist check is
    neutralised to a no-op so ONLY the Field cap can produce the 422 — isolating the cap (drop the
    max_length and this reds, because the over-cap unknown ids would otherwise have been let through)."""
    import app.backend.routes.monitors as monitors_mod

    monkeypatch.setattr(monitors_mod, "_validate_selected_analysts", lambda *_a, **_k: None)
    over = [f"analyst_{i}" for i in range(monitors_mod._MAX_ANALYSTS + 1)]
    assert env.client.post("/monitors", json={"name": "manyanalysts", "tickers": ["NVDA"], "selected_analysts": over}).status_code == 422


def test_patch_too_many_analysts_is_422(env, monkeypatch):
    """Same _MAX_ANALYSTS cap on the PATCH boundary, with the allowlist neutralised so only the Field
    cap can 422 — isolating the cap from the unknown-id allowlist path."""
    import app.backend.routes.monitors as monitors_mod

    monkeypatch.setattr(monitors_mod, "_validate_selected_analysts", lambda *_a, **_k: None)
    mid = env.client.post("/monitors", json={"name": "growanalysts", "tickers": ["NVDA"]}).json()["id"]
    over = [f"analyst_{i}" for i in range(monitors_mod._MAX_ANALYSTS + 1)]
    assert env.client.patch(f"/monitors/{mid}", json={"selected_analysts": over}).status_code == 422


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


# ── concurrency semaphore + 429 (Issue #21) ──────────────────────────────────
# Manual runs are synchronous multi-minute jobs; cap concurrent runs with a bounded semaphore so the
# loopback surface can't be coerced into N parallel analyzing-flow storms. cap=2 is lenient (a single
# user is never throttled). The permit MUST be released on every exit path (success AND error).


def test_run_returns_429_when_at_concurrency_cap(env):
    """Exhaust the module-global semaphore deterministically (no thread race), then a run is 429."""
    from app.backend.routes import monitors as monitors_mod

    mid = env.client.post("/monitors", json={"name": "capped", "tickers": ["NVDA"]}).json()["id"]
    monitors_mod._run_semaphore.acquire()
    monitors_mod._run_semaphore.acquire()  # cap=2 → both permits taken
    try:
        r = env.client.post(f"/monitors/{mid}/run", json={})
        assert r.status_code == 429
        assert "concurrent" in r.json()["detail"].lower()
    finally:
        monitors_mod._run_semaphore.release()
        monitors_mod._run_semaphore.release()


def test_run_releases_permit_after_success(env):
    """A normal run does not leak a permit: after it returns, the semaphore is fully available again."""
    from app.backend.routes import monitors as monitors_mod

    mid = env.client.post("/monitors", json={"name": "leakcheck", "tickers": ["NVDA"]}).json()["id"]
    assert env.client.post(f"/monitors/{mid}/run", json={"trade_date": "2026-06-12"}).status_code == 200
    # all permits reclaimable → none leaked
    acquired = [monitors_mod._run_semaphore.acquire(blocking=False) for _ in range(monitors_mod._MAX_CONCURRENT_RUNS)]
    try:
        assert all(acquired), "every permit must be available after a successful run (no leak)"
    finally:
        for ok in acquired:
            if ok:
                monitors_mod._run_semaphore.release()


def test_run_releases_permit_on_error_path(env, monkeypatch):
    """If run_monitor raises, the error propagates (uncaught → TestClient re-raises) AND the permit is
    released in `finally` — proving an exception cannot strand a permit and wedge the cap permanently.
    The release is verified by reclaiming every permit directly (as the 404/422 paths do), so the
    `run_monitor` patch needs no manual undo — the fixture auto-undoes it at teardown."""
    import app.backend.routes.monitors as monitors_mod

    def boom(*_a, **_k):
        raise RuntimeError("flow exploded mid-run")

    mid = env.client.post("/monitors", json={"name": "errpath", "tickers": ["NVDA"]}).json()["id"]
    monkeypatch.setattr(monitors_mod, "run_monitor", boom)
    with pytest.raises(RuntimeError, match="flow exploded mid-run"):  # propagated, not swallowed
        env.client.post(f"/monitors/{mid}/run", json={"trade_date": "2026-06-12"})
    # permit released by `finally`: every permit is reclaimable (would not be if the error path leaked one)
    acquired = [monitors_mod._run_semaphore.acquire(blocking=False) for _ in range(monitors_mod._MAX_CONCURRENT_RUNS)]
    try:
        assert all(acquired), "every permit must be reclaimable after an error-path run (no leak)"
    finally:
        for ok in acquired:
            if ok:
                monitors_mod._run_semaphore.release()


def test_run_releases_permit_on_404(env):
    """A 404 (unknown monitor) is raised INSIDE the try, so `finally` must still reclaim the permit —
    guards against a future refactor that acquires then raises the lookup-guard BEFORE the try, which
    would leak a permit on every bad id and silently wedge the cap."""
    from app.backend.routes import monitors as monitors_mod

    assert env.client.post("/monitors/9999/run").status_code == 404
    acquired = [monitors_mod._run_semaphore.acquire(blocking=False) for _ in range(monitors_mod._MAX_CONCURRENT_RUNS)]
    try:
        assert all(acquired), "every permit must be reclaimable after a 404 run (no leak)"
    finally:
        for ok in acquired:
            if ok:
                monitors_mod._run_semaphore.release()


def test_run_releases_permit_on_422(env):
    """A 422 (bad trade_date) is raised INSIDE the try after the permit is acquired, so `finally` must
    reclaim it — the same acquire-then-raise-before-try refactor risk as the 404 path."""
    from app.backend.routes import monitors as monitors_mod

    mid = env.client.post("/monitors", json={"name": "permit422", "tickers": ["NVDA"]}).json()["id"]
    assert env.client.post(f"/monitors/{mid}/run", json={"trade_date": "not-a-date"}).status_code == 422
    acquired = [monitors_mod._run_semaphore.acquire(blocking=False) for _ in range(monitors_mod._MAX_CONCURRENT_RUNS)]
    try:
        assert all(acquired), "every permit must be reclaimable after a 422 run (no leak)"
    finally:
        for ok in acquired:
            if ok:
                monitors_mod._run_semaphore.release()


def test_run_releases_permit_on_503_db_locked(env, monkeypatch):
    """A 'database is locked' OperationalError inside the guarded run maps to 503 (via
    _db_locked_to_503) WITHOUT escaping the try whose `finally` releases — so the permit must be
    reclaimed on the 503 path too. Verified by reclaiming EVERY permit afterwards (sensitive to even a
    single stranded permit; a follow-up run alone would mask a one-permit leak since cap=2). The
    fixture auto-undoes the run_monitor patch at teardown."""
    import app.backend.routes.monitors as monitors_mod

    mid = env.client.post("/monitors", json={"name": "locked503", "tickers": ["NVDA"]}).json()["id"]
    monkeypatch.setattr(monitors_mod, "run_monitor", _raise_locked)  # exact type _db_locked_to_503 maps to 503
    assert env.client.post(f"/monitors/{mid}/run", json={"trade_date": "2026-06-12"}).status_code == 503
    # permit released by `finally` on the 503 path: every permit is reclaimable (none stranded)
    acquired = [monitors_mod._run_semaphore.acquire(blocking=False) for _ in range(monitors_mod._MAX_CONCURRENT_RUNS)]
    try:
        assert all(acquired), "every permit must be reclaimable after a 503 run (no leak)"
    finally:
        for ok in acquired:
            if ok:
                monitors_mod._run_semaphore.release()


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


# ── hot-reload tests (Phase 9 / Issue #21) ──────────────────────────────────────────────────────
# These prove: POST /monitors hot-registers the job; PATCH enabled=False removes it and
# enabled=True re-adds it; a scheduling failure never fails a successful DB write (best-effort);
# and app.state.scheduler=None is safe (no crash). Scheduler is NOT started — config assertions only.


def test_post_monitor_hot_registers_job(env_with_scheduler):
    """POST /monitors registers the new monitor's job on the live scheduler immediately.
    WHY: monitors must arm on create without requiring a restart (Phase-9 guarantee)."""
    r = env_with_scheduler.client.post("/monitors", json={"name": "hot_create", "tickers": ["NVDA"], "granularity": "weekly"})
    assert r.status_code == 200
    mid = r.json()["id"]
    job = env_with_scheduler.sch.get_job(monitor_job_id(mid))
    assert job is not None, "job must be registered after POST /monitors for an enabled monitor"


def test_patch_disable_removes_job_then_enable_readds(env_with_scheduler):
    """PATCH enabled=False removes the job; PATCH enabled=True re-adds it.
    WHY: disabling a monitor must stop firing immediately (no restart needed); re-enabling must
    re-arm immediately so the cadence resumes without operator intervention."""
    r = env_with_scheduler.client.post("/monitors", json={"name": "toggle", "tickers": ["NVDA"]})
    mid = r.json()["id"]

    # Disable: job must disappear
    env_with_scheduler.client.patch(f"/monitors/{mid}", json={"enabled": False})
    assert env_with_scheduler.sch.get_job(monitor_job_id(mid)) is None, "disabled monitor must have no job on the live scheduler"

    # Re-enable: job must re-appear
    env_with_scheduler.client.patch(f"/monitors/{mid}", json={"enabled": True})
    assert env_with_scheduler.sch.get_job(monitor_job_id(mid)) is not None, "re-enabled monitor must be re-registered on the live scheduler"


def test_post_monitor_scheduling_failure_does_not_fail_write(env_with_scheduler, monkeypatch):
    """If scheduler.add_job raises, POST still returns 200 and the monitor IS persisted.
    WHY: the DB write is the source of truth; a scheduling side-effect failure is best-effort
    (the next restart re-snapshots). A 500 on a successful write would be worse than a silent
    scheduler miss (which is loud in logs via logger.exception)."""

    # Patch add_job to always raise so _safe_reschedule triggers its exception path
    def boom(*args, **kwargs):
        raise RuntimeError("simulated scheduler failure")

    monkeypatch.setattr(env_with_scheduler.sch, "add_job", boom)

    r = env_with_scheduler.client.post("/monitors", json={"name": "boom_sched", "tickers": ["NVDA"]})
    assert r.status_code == 200, "POST must return 200 even if scheduler.add_job raises"

    # Row must be persisted (committed), not rolled back
    with contextlib.closing(env_with_scheduler.SessionLocal()) as s:
        row = s.query(MonitorConfig).filter_by(name="boom_sched").one_or_none()
        assert row is not None, "monitor row must be persisted even when scheduling fails"


def test_post_monitor_no_scheduler_returns_200(monkeypatch):
    """POST /monitors returns 200 when app.state.scheduler is None (scheduler not running).
    WHY: the scheduler is optional; the API must not crash when it failed to start."""
    monkeypatch.delenv("OBSERVING_POOL_REFRESH_CRON", raising=False)

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
    app.include_router(monitors_router)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_analyzing_flow] = lambda: _ok_flow()
    app.state.scheduler = None  # simulate scheduler start failure

    client = TestClient(app)
    r = client.post("/monitors", json={"name": "no_sch", "tickers": ["NVDA"]})
    assert r.status_code == 200, "POST must return 200 even when app.state.scheduler is None"


# ── started-scheduler route tests (Phase 9 / Issue #21) ──────────────────────────────────────────
# These exercise the route→reschedule wiring against a RUNNING BackgroundScheduler (live job store),
# matching production. On a non-started scheduler APScheduler's _pending_jobs semantics DIVERGE (two
# add_jobs accumulate, get_job returns the stale first), so the non-started hot-reload tests above do
# NOT validate the shipped behaviour under real conditions — these do.


def test_post_monitor_registers_exactly_one_live_job(env_with_started_scheduler):
    """POST /monitors yields EXACTLY ONE live job for the monitor on the running scheduler.
    WHY: production arms monitors against a RUNNING scheduler; on a started job store an add must
    produce exactly one job (no _pending_jobs duplication artefact)."""
    mid = env_with_started_scheduler.client.post("/monitors", json={"name": "live_one", "tickers": ["NVDA"], "granularity": "weekly"}).json()["id"]
    job_id = monitor_job_id(mid)
    matching = [j for j in env_with_started_scheduler.sch.get_jobs() if j.id == job_id]
    assert len(matching) == 1, "exactly one live job must exist for the monitor on a started scheduler"
    assert env_with_started_scheduler.sch.get_job(job_id) is not None


def test_patch_disable_then_enable_on_started_scheduler(env_with_started_scheduler):
    """PATCH disable removes the live job; PATCH re-enable re-adds it (real before/after).
    WHY: against a RUNNING scheduler — as in production — disabling must immediately remove the live
    job and re-enabling must re-arm it, so the cadence stops/resumes without a restart."""
    mid = env_with_started_scheduler.client.post("/monitors", json={"name": "live_toggle", "tickers": ["NVDA"]}).json()["id"]
    job_id = monitor_job_id(mid)
    assert env_with_started_scheduler.sch.get_job(job_id) is not None  # armed on create

    env_with_started_scheduler.client.patch(f"/monitors/{mid}", json={"enabled": False})
    assert env_with_started_scheduler.sch.get_job(job_id) is None, "disable must remove the live job"

    env_with_started_scheduler.client.patch(f"/monitors/{mid}", json={"enabled": True})
    assert env_with_started_scheduler.sch.get_job(job_id) is not None, "re-enable must re-add the live job"


def test_patch_cadence_change_replaces_not_duplicates_live_job(env_with_started_scheduler):
    """PATCH a still-enabled monitor's cadence → still EXACTLY ONE job, with the NEW trigger.
    WHY: production reschedules against a RUNNING scheduler, where add_job(replace_existing=True)
    must DEDUP to one job carrying the latest trigger — a non-started scheduler would instead
    accumulate two jobs and return the stale trigger, the exact divergence this fixture guards."""
    mid = env_with_started_scheduler.client.post("/monitors", json={"name": "live_cadence", "tickers": ["NVDA"], "granularity": "weekly"}).json()["id"]
    job_id = monitor_job_id(mid)
    trigger_before = str(env_with_started_scheduler.sch.get_job(job_id).trigger)

    # weekly → monthly: both at/above the #18 floor, so the PATCH is accepted; their cron triggers differ.
    env_with_started_scheduler.client.patch(f"/monitors/{mid}", json={"granularity": "monthly"})

    matching = [j for j in env_with_started_scheduler.sch.get_jobs() if j.id == job_id]
    assert len(matching) == 1, "a cadence change must REPLACE the job (no duplicate accumulation)"
    trigger_after = str(env_with_started_scheduler.sch.get_job(job_id).trigger)
    assert trigger_after != trigger_before, "the live trigger must reflect the NEW cadence after PATCH"


# ── DELETE /monitors/{id} (§14) ──────────────────────────────────────────────────────────────────
# SOFT delete: the row PERSISTS with enabled=False (reports reference it; the DB enabled-guard is
# authoritative), and the live scheduler job is disarmed. 404 only on unknown id; idempotent (a second
# DELETE of an already-disabled monitor is still 204); best-effort when the scheduler is absent.


def test_delete_monitor_soft_deletes_row(env):
    """DELETE returns 204 and the row PERSISTS with enabled=False (soft delete, not a hard row drop).
    WHY: opportunity reports reference the monitor; the row must survive so history stays intact, and
    the DB enabled-guard (authoritative) keeps it from firing."""
    mid = env.client.post("/monitors", json={"name": "to_delete", "tickers": ["NVDA"]}).json()["id"]
    r = env.client.delete(f"/monitors/{mid}")
    assert r.status_code == 204
    with contextlib.closing(env.SessionLocal()) as s:
        row = s.get(MonitorConfig, mid)
        assert row is not None, "soft delete must NOT drop the row"
        assert row.enabled is False, "soft delete must leave the row disabled"


def test_delete_unknown_monitor_404(env):
    """DELETE of an unknown id is 404 (distinct from the 204 no-op on an already-disabled monitor)."""
    assert env.client.delete("/monitors/9999").status_code == 404


def test_delete_monitor_is_idempotent(env):
    """A second DELETE of the same (already soft-deleted) monitor is still 204, not 404.
    WHY: the row still exists after the first delete, so the operation is a safe no-op — idempotent."""
    mid = env.client.post("/monitors", json={"name": "twice", "tickers": ["NVDA"]}).json()["id"]
    assert env.client.delete(f"/monitors/{mid}").status_code == 204
    assert env.client.delete(f"/monitors/{mid}").status_code == 204
    # The second delete must be a pure no-op: the row still exists and stays disabled — never
    # hard-dropped, resurrected, or re-enabled (a 204 alone would not catch such a regression).
    with contextlib.closing(env.SessionLocal()) as s:
        row = s.get(MonitorConfig, mid)
        assert row is not None and row.enabled is False, "a repeat delete must leave the soft-deleted row intact"


def test_delete_monitor_disarms_live_job(env_with_started_scheduler):
    """DELETE removes the live scheduler job (real before/after on a RUNNING scheduler).
    WHY: a soft-deleted monitor must stop firing immediately — same disarm guarantee as PATCH
    enabled=False, validated against a started job store as in production."""
    mid = env_with_started_scheduler.client.post("/monitors", json={"name": "del_live", "tickers": ["NVDA"]}).json()["id"]
    job_id = monitor_job_id(mid)
    assert env_with_started_scheduler.sch.get_job(job_id) is not None  # armed on create
    assert env_with_started_scheduler.client.delete(f"/monitors/{mid}").status_code == 204
    assert env_with_started_scheduler.sch.get_job(job_id) is None, "DELETE must remove the live job"


def test_delete_monitor_no_scheduler_still_204(monkeypatch):
    """DELETE returns 204 and soft-deletes even when app.state.scheduler is None (best-effort disarm).
    WHY: the DB write is the source of truth; the scheduler disarm is a best-effort side effect that
    must never fail the delete (mirrors POST/PATCH under a None scheduler)."""
    monkeypatch.delenv("OBSERVING_POOL_REFRESH_CRON", raising=False)

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
    app.include_router(monitors_router)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_analyzing_flow] = lambda: _ok_flow()
    app.state.scheduler = None  # simulate scheduler start failure

    client = TestClient(app)
    mid = client.post("/monitors", json={"name": "no_sch_del", "tickers": ["NVDA"]}).json()["id"]
    r = client.delete(f"/monitors/{mid}")
    assert r.status_code == 204, "DELETE must return 204 even when app.state.scheduler is None"
    with contextlib.closing(Session()) as s:
        assert s.get(MonitorConfig, mid).enabled is False


def test_delete_monitor_preserves_referencing_report(env):
    """A persisted opportunity_report outlives the soft-delete of its monitor: the report row keeps
    its monitor_id FK and GET /opportunity-reports/{id} still serves it after DELETE /monitors/{id}.
    WHY: reports are the research history the soft-delete exists to protect — deleting a monitor must
    never cascade to, unlink, or hide the evidence trail it produced."""
    mid = env.client.post("/monitors", json={"name": "history_keeper", "tickers": ["NVDA"]}).json()["id"]
    env.client.post(f"/monitors/{mid}/run", json={"trade_date": "2026-06-12"})
    report_id = env.client.get("/opportunity-reports").json()[0]["id"]
    with contextlib.closing(env.SessionLocal()) as s:
        assert s.get(OpportunityReport, report_id).monitor_id == mid  # genuinely references the monitor

    assert env.client.delete(f"/monitors/{mid}").status_code == 204

    got = env.client.get(f"/opportunity-reports/{report_id}")
    assert got.status_code == 200 and got.json()["id"] == report_id, "report must stay readable after the delete"
    with contextlib.closing(env.SessionLocal()) as s:
        row = s.get(OpportunityReport, report_id)
        assert row is not None and row.monitor_id == mid, "delete must not cascade to or unlink the report row"
        assert s.get(MonitorConfig, mid) is not None, "the referenced monitor row must survive (soft delete)"


# ── GET /monitors/{id} (§14) ─────────────────────────────────────────────────────────────────────
# Single-monitor read mirroring get_report in observing_pools.py: 200 via _monitor_to_dict, 404 on
# unknown. Read-only — no commit, no reschedule — and it must NOT shadow the GET /monitors list route.


def test_get_single_monitor_returns_it(env):
    """GET /monitors/{id} returns the single monitor's dict (same shape as the list rows)."""
    mid = env.client.post("/monitors", json={"name": "readme", "tickers": ["NVDA"], "granularity": "weekly"}).json()["id"]
    r = env.client.get(f"/monitors/{mid}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == mid and body["name"] == "readme" and body["tickers"] == ["NVDA"]


def test_get_single_unknown_404(env):
    """GET of an unknown monitor id is 404."""
    assert env.client.get("/monitors/9999").status_code == 404


def test_get_single_does_not_shadow_list(env):
    """GET /monitors still returns a LIST (not shadowed by the single-monitor route), and
    GET /monitors/{id} returns a single dict. WHY: the collection and item routes must coexist."""
    a = env.client.post("/monitors", json={"name": "one", "tickers": ["NVDA"]}).json()["id"]
    b = env.client.post("/monitors", json={"name": "two", "tickers": ["AMD"]}).json()["id"]
    listed = env.client.get("/monitors")
    assert listed.status_code == 200 and isinstance(listed.json(), list)
    assert {a, b}.issubset({row["id"] for row in listed.json()})
    single = env.client.get(f"/monitors/{a}")
    assert single.status_code == 200 and isinstance(single.json(), dict)


def test_get_single_monitor_is_read_only(env_with_started_scheduler):
    """GET /monitors/{id} does NOT reschedule or mutate: the live job is untouched AND no session
    commit happens during the read. WHY: a read must be free of side effects — no disarm/re-arm, no
    commit. The commit spy pins the route docstring's 'no commit' claim instead of trusting it."""
    mid = env_with_started_scheduler.client.post("/monitors", json={"name": "ro_read", "tickers": ["NVDA"]}).json()["id"]
    job_id = monitor_job_id(mid)
    assert env_with_started_scheduler.sch.get_job(job_id) is not None

    commits = []
    real_commit = SASession.commit

    def spying_commit(session):
        commits.append(session)
        real_commit(session)

    with patch.object(SASession, "commit", spying_commit):
        assert env_with_started_scheduler.client.get(f"/monitors/{mid}").status_code == 200
    assert commits == [], "GET /monitors/{id} must not commit"
    assert env_with_started_scheduler.sch.get_job(job_id) is not None, "a read must not disarm the live job"
