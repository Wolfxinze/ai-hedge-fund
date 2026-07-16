"""POST /serenity/seek — the UNKNOWN-ticker keyword→candidate flow.

Fully offline: the edgar_fts seek adapter is overridden with a stub (a NAMED function plus a
``calls`` list, per the repo's spend-discipline lesson — never a bare lambda) so no
efts.sec.gov network call ever fires. These prove the API CONTRACT: ranked candidates passed
through verbatim in the frontend field names, zero candidates is still a 200 with any adapter
errors surfaced (adapter degradation is NEVER turned into a 5xx — the frontend renders the empty
state), loud pydantic boundary validation on keywords/max_candidates, and the request's
keywords + max_candidates reaching the adapter unchanged.
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.backend.routes.observing_pools import get_seeker, router
from src.serenity.adapters.edgar_fts import seek_candidates


def _candidate(**over):
    base = dict(cik="0001046179", company="NVIDIA Corp", tickers=("NVDA",), hits=5, latest_filing_date="2026-01-15")
    base.update(over)
    return SimpleNamespace(**base)


def _result(candidates=None, errors=None):
    return SimpleNamespace(candidates=list(candidates or []), errors=list(errors or []))


@pytest.fixture
def env():
    app = FastAPI()
    app.include_router(router)
    calls: list[dict] = []

    def default_seeker(keywords, *, max_candidates=10, user_agent=None):
        calls.append({"keywords": keywords, "max_candidates": max_candidates})
        return _result(
            candidates=[
                _candidate(),
                _candidate(cik="0000320193", company="Apple Inc", tickers=("AAPL", "AAPL.PR"), hits=3, latest_filing_date="2026-02-01"),
            ]
        )

    def set_seeker(fn):
        app.dependency_overrides[get_seeker] = lambda: fn

    set_seeker(default_seeker)
    return SimpleNamespace(client=TestClient(app), calls=calls, set_seeker=set_seeker)


# ── happy path ───────────────────────────────────────────────────────────────


def test_seek_returns_ranked_candidates_verbatim(env):
    r = env.client.post("/serenity/seek", json={"keywords": ["CoWoS", "packaging"], "max_candidates": 10})
    assert r.status_code == 200
    body = r.json()
    assert body["errors"] == []
    assert [c["cik"] for c in body["candidates"]] == ["0001046179", "0000320193"]  # rank order preserved
    first = body["candidates"][0]
    assert first == {"cik": "0001046179", "company": "NVIDIA Corp", "tickers": ["NVDA"], "hits": 5, "latest_filing_date": "2026-01-15"}
    assert body["candidates"][1]["tickers"] == ["AAPL", "AAPL.PR"]  # tuple → list, contents verbatim


def test_seek_forwards_keywords_and_max_candidates_unchanged(env):
    env.client.post("/serenity/seek", json={"keywords": ["gallium nitride", "SiC"], "max_candidates": 7})
    assert env.calls[0]["keywords"] == ["gallium nitride", "SiC"]
    assert env.calls[0]["max_candidates"] == 7
    assert isinstance(env.calls[0]["max_candidates"], int)


def test_seek_default_max_candidates_is_10(env):
    env.client.post("/serenity/seek", json={"keywords": ["photonics"]})
    assert env.calls[0]["max_candidates"] == 10


# ── adapter degradation is a 200, never a 5xx ────────────────────────────────


def test_seek_zero_candidates_is_200_with_errors_surfaced(env):
    env.set_seeker(lambda keywords, **kw: _result(candidates=[], errors=["'CoWoS': efts blocked (HTML block page)"]))
    r = env.client.post("/serenity/seek", json={"keywords": ["CoWoS"]})
    assert r.status_code == 200  # degradation surfaced, not a 5xx — the frontend renders the empty state
    body = r.json()
    assert body["candidates"] == []
    assert body["errors"] == ["'CoWoS': efts blocked (HTML block page)"]


def test_seek_no_ticker_field_accepted_but_ignored(env):
    # seek is the UNKNOWN-ticker flow: a stray ticker key is simply not part of the model.
    r = env.client.post("/serenity/seek", json={"keywords": ["ArF immersion"], "ticker": "TSM"})
    assert r.status_code == 200
    assert "keywords" in env.calls[0] and env.calls[0]["keywords"] == ["ArF immersion"]


# ── boundary validation (fail loud at the surface) ───────────────────────────


def test_seek_empty_keywords_is_422(env):
    # Explicit [] must 422 (mirrors the discover `sources: []` lesson), never a vacuous 200.
    r = env.client.post("/serenity/seek", json={"keywords": []})
    assert r.status_code == 422
    assert env.calls == []  # never reached the adapter


def test_seek_missing_keywords_is_422(env):
    assert env.client.post("/serenity/seek", json={}).status_code == 422


@pytest.mark.parametrize("keyword", ["", "A" * 81])
def test_seek_keyword_out_of_bounds_is_422(env, keyword):
    r = env.client.post("/serenity/seek", json={"keywords": [keyword]})
    assert r.status_code == 422
    assert env.calls == []


def test_seek_keyword_at_max_len_is_ok(env):
    r = env.client.post("/serenity/seek", json={"keywords": ["A" * 80]})
    assert r.status_code == 200


@pytest.mark.parametrize("max_candidates", [0, 26, -1])
def test_seek_max_candidates_out_of_bounds_is_422(env, max_candidates):
    r = env.client.post("/serenity/seek", json={"keywords": ["k"], "max_candidates": max_candidates})
    assert r.status_code == 422
    assert env.calls == []


@pytest.mark.parametrize("max_candidates", [1, 25])
def test_seek_max_candidates_at_bounds_is_ok(env, max_candidates):
    r = env.client.post("/serenity/seek", json={"keywords": ["k"], "max_candidates": max_candidates})
    assert r.status_code == 200


# ── real dependency wiring ───────────────────────────────────────────────────


def test_get_seeker_returns_real_seek_candidates():
    # Every other test overrides get_seeker; this executes its real body so a rename of
    # seek_candidates fails here instead of shipping green and 500ing in production.
    assert get_seeker() is seek_candidates
