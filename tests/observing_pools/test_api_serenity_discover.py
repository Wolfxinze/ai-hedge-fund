"""POST /serenity/discover — the UI-triggered evidence-discovery flow.

Fully offline (StaticPool in-memory + dependency overrides; the gatherer is stubbed so no
EDGAR/Federal-Register network call ever fires, and every stub reference carries an excerpt so
``build_record(fetch_missing=True)`` never reaches the fetcher). These prove the API CONTRACT:
one research record per non-empty source group (CLI ``discover`` parity), the disclaimer
invariant on every returned record, loud 422/404 boundary validation, all-sources-errored → 502,
and one failing group never sinking the others (partial success, surfaced not swallowed).
"""

import contextlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.backend.routes.observing_pools as op_routes
import src.storage.models as m
from app.backend.database.connection import get_db
from app.backend.routes.observing_pools import get_gatherer, router
from src.serenity.adapters.gather import GatherResult
from src.serenity.grading import SCORECARD_DIMENSIONS
from src.storage.models import SerenityResearchRecord

_SCORECARD = {dim: 3 for dim in SCORECARD_DIMENSIONS}

_EDGAR_HEADERS = {"User-Agent": "test-agent"}
_FEDREG_HEADERS = {"User-Agent": "test-agent-fr"}

# Allowlisted hosts + claim/excerpt keyword overlap so classify_reference substantiates them
# (same style as test_api_e2e.py) — and the present excerpt keeps fetch_missing a no-op.
_EDGAR_REFS = [
    {"source_url": "https://www.sec.gov/filing-1", "claim_summary": "CoWoS capacity constrains packaging supply", "excerpt": "CoWoS advanced packaging capacity constrains packaging supply per the 10-K filing"},
    {"source_url": "https://www.sec.gov/filing-2", "claim_summary": "CoWoS capacity constrains packaging supply", "excerpt": "The filing states CoWoS capacity constrains packaging supply through 2027"},
]
_FEDREG_REFS = [
    {"source_url": "https://www.federalregister.gov/doc-1", "claim_summary": "export controls on advanced packaging", "excerpt": "New export controls on advanced packaging equipment were announced"},
]


def _gather_result(**over):
    base = dict(
        references=_EDGAR_REFS + _FEDREG_REFS,
        headers_by_source={"edgar": _EDGAR_HEADERS, "federal_register": _FEDREG_HEADERS},
        groups=[(_EDGAR_HEADERS, list(_EDGAR_REFS)), (_FEDREG_HEADERS, list(_FEDREG_REFS))],
        errors={},
        counts={"edgar": 2, "federal_register": 1},
    )
    base.update(over)
    return GatherResult(**base)


def _body(**over):
    base = {
        "ticker": "TSM",
        "theme": "AI accelerator packaging",
        "keywords": ["CoWoS", "packaging"],
        "platform_key": "ai",
        "scorecard": _SCORECARD,
    }
    base.update(over)
    return base


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
    app.include_router(router)
    app.dependency_overrides[get_db] = override_get_db
    calls: list[dict] = []

    def set_gatherer(fn):
        app.dependency_overrides[get_gatherer] = lambda: fn

    def default_gatherer(ticker, *, keywords, sources, max_per_source):
        calls.append({"ticker": ticker, "keywords": keywords, "sources": tuple(sources), "max_per_source": max_per_source})
        return _gather_result()

    set_gatherer(default_gatherer)
    return SimpleNamespace(client=TestClient(app), Session=Session, set_gatherer=set_gatherer, calls=calls)


# ── happy path ───────────────────────────────────────────────────────────────


def test_discover_builds_one_record_per_source_group(env):
    r = env.client.post("/serenity/discover", json=_body())
    assert r.status_code == 200
    body = r.json()
    assert body["ticker"] == "TSM"
    assert len(body["records"]) == 2  # one per non-empty source group (CLI parity)
    assert body["reference_count"] == 3
    assert body["source_errors"] == {}
    assert body["failed_groups"] == 0
    for rec in body["records"]:
        assert rec["ticker"] == "TSM"
        assert rec["theme"] == "AI accelerator packaging"
        assert rec["disclaimer"]  # §9.9 disclaimer invariant on the write surface too
        assert rec["evidence_grade"] is not None
    # Persisted: the read route now serves them (the UI's follow-up search must see the records).
    with contextlib.closing(env.Session()) as s:
        assert s.query(SerenityResearchRecord).count() == 2
    read = env.client.get("/serenity/research/TSM")
    assert read.status_code == 200 and len(read.json()) == 2
    # The gatherer received the request's parameters (ticker uppercased).
    assert env.calls and env.calls[0]["ticker"] == "TSM" and env.calls[0]["keywords"] == ["CoWoS", "packaging"]


def test_discover_lowercase_ticker_is_uppercased(env):
    r = env.client.post("/serenity/discover", json=_body(ticker="tsm"))
    assert r.status_code == 200
    assert r.json()["ticker"] == "TSM"
    assert env.calls[0]["ticker"] == "TSM"


def test_discover_no_references_returns_empty_success(env):
    env.set_gatherer(lambda ticker, **kw: _gather_result(references=[], groups=[], counts={"edgar": 0, "federal_register": 0}))
    r = env.client.post("/serenity/discover", json=_body())
    assert r.status_code == 200
    body = r.json()
    assert body["records"] == [] and body["reference_count"] == 0
    with contextlib.closing(env.Session()) as s:
        assert s.query(SerenityResearchRecord).count() == 0


def test_discover_empty_group_is_skipped(env):
    env.set_gatherer(
        lambda ticker, **kw: _gather_result(
            references=list(_EDGAR_REFS),
            groups=[(_EDGAR_HEADERS, list(_EDGAR_REFS)), (_FEDREG_HEADERS, [])],
            counts={"edgar": 2, "federal_register": 0},
        )
    )
    r = env.client.post("/serenity/discover", json=_body())
    assert r.status_code == 200
    assert len(r.json()["records"]) == 1  # the empty group built nothing


# ── failure surfacing (never silent) ─────────────────────────────────────────


def test_discover_all_sources_errored_is_502(env):
    env.set_gatherer(
        lambda ticker, **kw: _gather_result(
            references=[], groups=[], errors={"edgar": "HTTPError", "federal_register": "Timeout"}, counts={"edgar": 0, "federal_register": 0}
        )
    )
    r = env.client.post("/serenity/discover", json=_body())
    assert r.status_code == 502
    assert "errored" in r.json()["detail"]


def test_discover_gatherer_raising_is_502_not_500_leak(env):
    def boom(ticker, **kw):
        raise RuntimeError("secret internal detail")

    env.set_gatherer(boom)
    r = env.client.post("/serenity/discover", json=_body())
    assert r.status_code == 502
    assert "secret internal detail" not in r.json()["detail"]  # no raw exception leak


def test_discover_partial_source_errors_still_builds_and_surfaces(env):
    env.set_gatherer(
        lambda ticker, **kw: _gather_result(
            references=list(_EDGAR_REFS),
            groups=[(_EDGAR_HEADERS, list(_EDGAR_REFS))],
            errors={"federal_register": "Timeout"},
            counts={"edgar": 2, "federal_register": 0},
        )
    )
    r = env.client.post("/serenity/discover", json=_body())
    assert r.status_code == 200
    body = r.json()
    assert len(body["records"]) == 1
    assert body["source_errors"] == {"federal_register": "Timeout"}  # degraded, surfaced not swallowed


def test_discover_one_group_failing_does_not_sink_the_other(env, monkeypatch):
    real = op_routes.build_record
    state = {"first": True}

    def flaky(session, **kwargs):
        if state["first"]:
            state["first"] = False
            raise RuntimeError("group 1 exploded")
        return real(session, **kwargs)

    monkeypatch.setattr(op_routes, "build_record", flaky)
    r = env.client.post("/serenity/discover", json=_body())
    assert r.status_code == 200
    body = r.json()
    assert len(body["records"]) == 1 and body["failed_groups"] == 1
    with contextlib.closing(env.Session()) as s:
        assert s.query(SerenityResearchRecord).count() == 1  # failed group rolled back, survivor committed


def test_discover_every_group_failing_is_500(env, monkeypatch):
    monkeypatch.setattr(op_routes, "build_record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = env.client.post("/serenity/discover", json=_body())
    assert r.status_code == 500
    with contextlib.closing(env.Session()) as s:
        assert s.query(SerenityResearchRecord).count() == 0


# ── boundary validation (fail loud at the surface) ───────────────────────────


@pytest.mark.parametrize("ticker", ["", "bad ticker!", "A" * 17, "../etc"])
def test_discover_invalid_ticker_is_422(env, ticker):
    assert env.client.post("/serenity/discover", json=_body(ticker=ticker)).status_code == 422


def test_discover_unknown_platform_is_404(env):
    r = env.client.post("/serenity/discover", json=_body(platform_key="nope"))
    assert r.status_code == 404


def test_discover_platform_is_optional(env):
    body = _body()
    del body["platform_key"]
    r = env.client.post("/serenity/discover", json=body)
    assert r.status_code == 200
    assert all(rec["platform_key"] is None for rec in r.json()["records"])


@pytest.mark.parametrize(
    "scorecard",
    [
        {},  # missing all dimensions
        {**_SCORECARD, "supplier_concentration": 5},  # out of range
        {**_SCORECARD, "supplier_concentration": -1},
        {dim: 3 for dim in list(SCORECARD_DIMENSIONS)[:-1]},  # one dimension missing
        {**_SCORECARD, "extra_dimension": 2},  # unknown key
    ],
)
def test_discover_invalid_scorecard_is_422(env, scorecard):
    assert env.client.post("/serenity/discover", json=_body(scorecard=scorecard)).status_code == 422


def test_discover_unknown_source_is_422(env):
    r = env.client.post("/serenity/discover", json=_body(sources=["edgar", "carrier_pigeon"]))
    assert r.status_code == 422
    assert "carrier_pigeon" in r.json()["detail"]


@pytest.mark.parametrize("keywords", [[], ["   "], ""])
def test_discover_empty_keywords_is_422(env, keywords):
    assert env.client.post("/serenity/discover", json=_body(keywords=keywords)).status_code == 422


def test_discover_blank_theme_is_422(env):
    assert env.client.post("/serenity/discover", json=_body(theme="   ")).status_code == 422
