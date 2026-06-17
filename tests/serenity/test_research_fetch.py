"""Phase 6: build_record(fetch_missing=...) wiring. Offline by default; fetches when
opted in; degrades (never raises) on a blocked/failed/erroring fetch; and a fetched
excerpt still must overlap the claim to substantiate (host alone never substantiates).
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.storage.models as m
from src.serenity import research
from src.serenity.fetch import FetchResult
from src.serenity.research import build_record

_CLAIM = "supplier bottleneck concentration"
_OVERLAP = "supplier bottleneck concentration is severe across the supply chain network today"
_NO_OVERLAP = "alpha beta gamma delta epsilon zeta eta theta"


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    m.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _refs():
    return [{"source_url": "https://sec.gov/doc", "claim_summary": _CLAIM}]  # no excerpt


def _evidence(session, rec):
    return session.query(m.EvidenceReference).filter_by(record_id=rec.id).one()


def test_fetch_missing_false_is_offline(session, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("fetch must not run when fetch_missing=False")
    monkeypatch.setattr(research, "fetch_excerpt", boom)
    rec = build_record(session, theme="t", references=_refs(), scorecard={}, fetch_missing=False)
    session.commit()
    assert _evidence(session, rec).substantiated is False


def test_provided_excerpt_never_refetched(session, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not fetch when an excerpt is provided")
    monkeypatch.setattr(research, "fetch_excerpt", boom)
    refs = [{"source_url": "https://sec.gov/doc", "claim_summary": _CLAIM, "excerpt": _OVERLAP}]
    rec = build_record(session, theme="t", references=refs, scorecard={}, fetch_missing=True)
    session.commit()
    assert _evidence(session, rec).substantiated is True


def test_fetch_blocked_degrades_not_raises(session, monkeypatch):
    monkeypatch.setattr(research, "fetch_excerpt", lambda url, **k: FetchResult(False, None, None, None, None, "blocked_redirect"))
    rec = build_record(session, theme="t", references=_refs(), scorecard={}, fetch_missing=True)
    session.commit()
    assert _evidence(session, rec).substantiated is False  # persisted, just unsubstantiated


def test_fetch_success_still_requires_overlap(session, monkeypatch):
    monkeypatch.setattr(research, "fetch_excerpt", lambda url, **k: FetchResult(True, _NO_OVERLAP, "https://sec.gov/doc", 200, "text/html", "ok", 40))
    rec = build_record(session, theme="t", references=_refs(), scorecard={}, fetch_missing=True)
    session.commit()
    assert _evidence(session, rec).substantiated is False  # trusted host + non-overlap text ≠ substantiated

    monkeypatch.setattr(research, "fetch_excerpt", lambda url, **k: FetchResult(True, _OVERLAP, "https://sec.gov/doc", 200, "text/html", "ok", 80))
    rec2 = build_record(session, theme="t", references=_refs(), scorecard={}, fetch_missing=True)
    session.commit()
    assert _evidence(session, rec2).substantiated is True


def test_fetch_unexpected_error_isolated(session, monkeypatch):
    def boom(url, **k):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(research, "fetch_excerpt", boom)
    rec = build_record(session, theme="t", references=_refs(), scorecard={}, fetch_missing=True)  # must not raise
    session.commit()
    assert _evidence(session, rec).substantiated is False


def test_fetch_headers_forwarded(session, monkeypatch):
    """fetch_headers (e.g. the EDGAR User-Agent) reaches the fetcher per reference."""
    captured = {}

    def fake(url, *, headers=None, **k):
        captured["headers"] = headers
        return FetchResult(True, _OVERLAP, "https://sec.gov/doc", 200, "text/html", "ok", 80)

    monkeypatch.setattr(research, "fetch_excerpt", fake)
    build_record(
        session,
        theme="t",
        references=_refs(),
        scorecard={},
        fetch_missing=True,
        fetch_headers={"User-Agent": "edgar/1.0"},
    )
    assert captured["headers"] == {"User-Agent": "edgar/1.0"}


def test_fetched_excerpt_persisted_and_record_graded(session, monkeypatch):
    monkeypatch.setattr(research, "fetch_excerpt", lambda url, **k: FetchResult(True, _OVERLAP, "https://sec.gov/doc", 200, "text/html", "ok", 80))
    rec = build_record(
        session,
        theme="t",
        references=_refs(),
        scorecard={"supplier_concentration": 4, "expansion_difficulty": 3},
        fetch_missing=True,
    )
    session.commit()
    ev = _evidence(session, rec)
    assert ev.excerpt == _OVERLAP  # the FETCHED text is persisted to the column (not just its bool side-effect)
    assert ev.substantiated is True
    assert rec.evidence_grade is not None  # grade computed + persisted, not just the substantiated flag
