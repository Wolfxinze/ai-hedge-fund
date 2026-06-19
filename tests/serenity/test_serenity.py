"""Serenity-lite unit + integration tests (PRD v4 §9.6, §11.5, §17).

Covers host-derived source typing, substantiation (incl. the 200-but-irrelevant
trap), deterministic grading with per-host compounding caps, the min-grade gate,
record persistence with a non-null disclaimer, and the F2 pool bootstrap.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.storage.models as m
from src.serenity.evidence import (
    classify_reference,
    host_of,
    is_substantiated,
    source_type_for_host,
)
from src.serenity.grading import (
    EvidenceGrade,
    grade_evidence,
    normalize_scorecard,
    recommended_action,
    serenity_score,
)
from src.serenity.integrate import apply_serenity_to_pool
from src.serenity.research import build_record
from src.storage.models import RecommendedAction, SourceType


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    m.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


# ── evidence typing + substantiation ────────────────────────────────────────


def test_source_type_is_host_derived():
    assert source_type_for_host("www.sec.gov") == SourceType.FILING  # subdomain
    assert source_type_for_host("sec.gov") == SourceType.FILING
    assert source_type_for_host("patents.google.com") == SourceType.PATENT
    assert source_type_for_host("randomblog.com") == SourceType.UNVERIFIED


def test_host_of_rejects_userinfo():
    """A userinfo-bearing URL with an allowlisted post-@ host must return None (not the host) so it
    classifies UNVERIFIED — parity with the fetcher's '@'-reject; a userinfo URL is never a real
    allowlisted source, only record pollution."""
    assert host_of("https://evil.com@sec.gov/filing.htm") is None
    assert source_type_for_host(host_of("https://evil.com@sec.gov/filing.htm")) == SourceType.UNVERIFIED
    assert host_of("https://www.sec.gov/filing.htm") == "www.sec.gov"  # clean URL unchanged


def test_substantiation_overlap():
    claim = "NVIDIA H100 GPU supply constrained by TSMC CoWoS packaging capacity"
    good = "TSMC CoWoS advanced packaging capacity limits NVIDIA H100 GPU supply this quarter."
    irrelevant = "Please enable cookies and sign in to continue to your account dashboard."
    assert is_substantiated(claim, good) is True
    assert is_substantiated(claim, irrelevant) is False  # 200-but-irrelevant fails
    assert is_substantiated(claim, "too short") is False


def test_unverified_host_never_substantiated():
    r = classify_reference(source_url="https://randomblog.com/x", claim_summary="TSMC CoWoS capacity", excerpt="TSMC CoWoS capacity is constrained for NVIDIA packaging supply")
    assert r["source_type"] == SourceType.UNVERIFIED
    assert r["substantiated"] is False  # off-allowlist can't count even if text matches


# ── grading ─────────────────────────────────────────────────────────────────


def _ref(host, stype, sub):
    return {"source_host": host, "source_type": stype.value, "substantiated": sub}


def test_grade_two_filings_distinct_hosts_is_A():
    refs = [_ref("sec.gov", SourceType.FILING, True), _ref("uspto.gov", SourceType.PATENT, True)]
    # 3 + 2 = 5 → B (needs 6 for A)
    assert grade_evidence(refs) == EvidenceGrade.B


def test_per_host_cap_blocks_flooding():
    # Five substantiated news from the SAME host: cap=2 → weight 1+1=2 → C, not higher.
    refs = [_ref("reuters.com", SourceType.NEWS, True) for _ in range(5)]
    assert grade_evidence(refs) == EvidenceGrade.C


def test_zero_substantiated_is_F():
    refs = [_ref("sec.gov", SourceType.FILING, False), _ref("randomblog.com", SourceType.UNVERIFIED, True)]
    assert grade_evidence(refs) == EvidenceGrade.F


def test_serenity_score_gated_by_min_grade():
    full = {d: 4 for d in ("supplier_concentration", "validation_cycle", "capacity_expansion", "certification_strictness", "purity_precision")}
    assert serenity_score(full, EvidenceGrade.A) == 100.0
    assert serenity_score(full, EvidenceGrade.F) is None
    assert serenity_score(full, EvidenceGrade.D, min_grade=EvidenceGrade.C) is None  # D < C → withheld


@pytest.mark.parametrize(
    "score,grade,expected",
    [
        (None, EvidenceGrade.F, RecommendedAction.DEMOTE),  # withheld + F → demote
        (70.0, EvidenceGrade.A, RecommendedAction.PROMOTE),  # strong grade + high score → promote
        (50.0, EvidenceGrade.B, RecommendedAction.HOLD),  # grade ok but score < 60 → hold
        (70.0, EvidenceGrade.C, RecommendedAction.HOLD),  # high score but grade < B → hold
    ],
)
def test_recommended_action_branches(score, grade, expected):
    assert recommended_action(score, grade) == expected


def test_normalize_scorecard_clamps_and_defaults_missing():
    # Partial scorecard: out-of-range 9 clamps to 4, negative clamps to 0, the two
    # unmentioned dimensions default to 0. Raw = 4 + 0 + 3 = 7 over max 20 → 35.0.
    raw7 = {"supplier_concentration": 9, "validation_cycle": -2, "capacity_expansion": 3}
    assert normalize_scorecard(raw7) == pytest.approx(7 / 20 * 100.0)
    assert normalize_scorecard({}) == 0.0  # all dimensions missing → 0


# ── record persistence ──────────────────────────────────────────────────────


def test_build_record_persists_with_disclaimer(session):
    record = build_record(
        session,
        theme="AI accelerator packaging",
        ticker="NVDA",
        platform_key="ai",
        chain_layer="advanced packaging",
        bottleneck_hypothesis="CoWoS capacity gates H100 supply",
        scorecard={"supplier_concentration": 4, "validation_cycle": 3, "capacity_expansion": 4, "certification_strictness": 2, "purity_precision": 3},
        references=[
            {"source_url": "https://www.sec.gov/x", "claim_summary": "CoWoS capacity constrains NVIDIA packaging", "excerpt": "CoWoS advanced packaging capacity constrains NVIDIA H100 supply per the filing"},
            {"source_url": "https://patents.google.com/y", "claim_summary": "CoWoS packaging method", "excerpt": "A CoWoS advanced packaging method for high bandwidth memory integration on NVIDIA accelerators"},
        ],
    )
    session.commit()
    assert record.disclaimer and record.disclaimer_version  # non-null invariant
    assert record.evidence_grade == EvidenceGrade.B.value  # filing(3)+patent(2)=5 → B
    assert record.serenity_score is not None
    assert session.query(m.EvidenceReference).filter_by(record_id=record.id).count() == 2


# ── pool integration / bootstrap ────────────────────────────────────────────


def _entry(ticker, pf, val, grw, mom):
    return m.ObservationPoolEntry(
        ticker=ticker,
        platform_key="ai",
        status=m.PoolEntryStatus.CANDIDATE.value,
        platform_fit_score=pf,
        value_investor_score=val,
        innovation_growth_score=grw,
        risk_adjusted_momentum_score=mom,
        composite_formula_version="v3-4comp",
    )


def test_zero_graded_drops_serenity_uniformly(session):
    session.add_all([_entry("T1", 90, 80, 70, 60), _entry("T2", 90, 60, 60, 60), _entry("T3", 90, 40, 40, 40)])
    session.commit()

    summary = apply_serenity_to_pool(session, "ai")
    session.commit()
    assert summary == {"graded": 0, "median": None, "reranked": 3}

    entries = {e.ticker: e for e in session.query(m.ObservationPoolEntry).all()}
    assert all(e.serenity_bottleneck_score is None for e in entries.values())
    assert all(e.composite_formula_version == "v3-5comp" for e in entries.values())
    # Serenity dropped uniformly → equals the 4-comp composite. T1 = 66.5/.85.
    assert entries["T1"].composite_score == pytest.approx(66.5 / 0.85)
    assert entries["T1"].rank == 1 and entries["T3"].rank == 3


def test_some_graded_imputes_median_and_reranks(session):
    session.add_all([_entry("T1", 90, 80, 70, 60), _entry("T2", 90, 60, 60, 60), _entry("T3", 90, 40, 40, 40)])
    session.flush()
    # Graded records: T1 weak (20), T3 strong (100); T2 has none → absent → impute median.
    session.add(m.SerenityResearchRecord(ticker="T1", platform_key="ai", theme="t", serenity_score=20.0, evidence_grade="C", disclaimer="x", disclaimer_version="v"))
    session.add(m.SerenityResearchRecord(ticker="T3", platform_key="ai", theme="t", serenity_score=100.0, evidence_grade="A", disclaimer="x", disclaimer_version="v"))
    session.commit()

    summary = apply_serenity_to_pool(session, "ai")
    session.commit()
    assert summary["graded"] == 2 and summary["median"] == pytest.approx(60.0)

    entries = {e.ticker: e for e in session.query(m.ObservationPoolEntry).all()}
    assert entries["T1"].serenity_bottleneck_score == 20.0
    assert entries["T2"].serenity_bottleneck_score is None  # absent
    assert entries["T2"].composite_score is not None  # but composite still computed via imputed median
    # 5-comp composites: T1=69.5, T2=67.5, T3=61.5 (W=1.0).
    assert entries["T1"].composite_score == pytest.approx(69.5)
    assert entries["T2"].composite_score == pytest.approx(67.5)
    assert entries["T3"].composite_score == pytest.approx(61.5)
    assert entries["T1"].rank == 1 and entries["T2"].rank == 2 and entries["T3"].rank == 3


def test_required_missing_resets_to_data_unavailable(session):
    # Stale state: a REQUIRED component (value_investor) is missing, yet the entry
    # still carries a rank + CANDIDATE status from a prior refresh. apply_serenity
    # must recompute composite=None → strip the rank and flag data_unavailable.
    stale = _entry("T1", 90, None, 70, 60)
    stale.rank = 1
    stale.status = m.PoolEntryStatus.CANDIDATE.value
    session.add(stale)
    session.commit()

    apply_serenity_to_pool(session, "ai")
    session.commit()

    e = session.query(m.ObservationPoolEntry).filter_by(ticker="T1").one()
    assert e.composite_score is None  # REQUIRED missing → not rankable
    assert e.rank is None
    assert e.status == m.PoolEntryStatus.DATA_UNAVAILABLE.value
