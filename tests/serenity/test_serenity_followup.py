"""Issue #8: Serenity observability (substantiation reason) + status-clobber guard
+ single-graded-entry median behaviour. No new DB column / migration.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.storage.models as m
from src.serenity.evidence import classify_reference, substantiation_reason
from src.serenity.integrate import apply_serenity_to_pool
from src.storage.models import SourceType


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    m.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _entry(ticker, *, status="candidate", pf=90.0, val=80.0, grw=70.0, mom=60.0):
    return m.ObservationPoolEntry(
        ticker=ticker,
        platform_key="ai",
        status=status,
        platform_fit_score=pf,
        value_investor_score=val,
        innovation_growth_score=grw,
        risk_adjusted_momentum_score=mom,
    )


# ── substantiation reason (observability) ──────────────────────────────────────

def test_reason_unverified_host():
    out = classify_reference(source_url="https://evil.example.com/x", claim_summary="bottleneck supplier", excerpt="word " * 12)
    assert out["reason"] == "unverified_host"
    assert out["substantiated"] is False


def test_reason_no_excerpt_and_too_short():
    assert substantiation_reason("a real claim here", None, SourceType.FILING) == "no_excerpt"
    assert substantiation_reason("a real claim here", "short text", SourceType.FILING) == "excerpt_too_short"


def test_reason_no_overlap_vs_ok():
    unrelated = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    assert substantiation_reason("supplier concentration bottleneck", unrelated, SourceType.FILING) == "no_overlap"
    overlapping = "supplier concentration bottleneck is severe across the entire supply chain network today"
    assert substantiation_reason("supplier concentration bottleneck", overlapping, SourceType.FILING) == "ok"


# ── integrate.py guards ────────────────────────────────────────────────────────

def test_dropped_status_not_clobbered(session):
    # A manually DROPPED entry with no value_investor (→ composite None) must stay DROPPED,
    # not be resurrected to data_unavailable.
    dropped = _entry("DROP", status=m.PoolEntryStatus.DROPPED.value, val=None)
    session.add(dropped)
    session.flush()
    apply_serenity_to_pool(session, "ai")
    assert dropped.status == m.PoolEntryStatus.DROPPED.value
    assert dropped.rank is None


def test_dropped_with_scores_excluded_from_ranking(session):
    # A DROPPED entry that STILL has component scores (non-None composite) must not
    # be ranked back into the pool — the half-guard previously gave it rank=1.
    dropped = _entry("DROP", status=m.PoolEntryStatus.DROPPED.value)  # full scores
    keep = _entry("KEEP")
    session.add_all([dropped, keep])
    session.flush()
    apply_serenity_to_pool(session, "ai")
    assert dropped.status == m.PoolEntryStatus.DROPPED.value
    assert dropped.rank is None  # excluded from ranking despite a non-None composite
    assert keep.rank == 1


def test_single_graded_entry_imputes_its_value(session):
    # Exactly one graded entry → median == that single value, imputed to the ungraded entry.
    session.add_all([_entry("G"), _entry("U")])
    session.flush()
    session.add(
        m.SerenityResearchRecord(
            ticker="G", platform_key="ai", theme="t", serenity_score=42.0,
            evidence_grade="B", disclaimer="x", disclaimer_version="v",
        )
    )
    session.flush()
    summary = apply_serenity_to_pool(session, "ai")
    assert summary["graded"] == 1
    assert summary["median"] == 42.0
    u = session.query(m.ObservationPoolEntry).filter_by(ticker="U").one()
    assert u.score_breakdown["serenity"]["pool_median"] == 42.0  # lone graded value imputed
