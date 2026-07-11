"""Pipeline-level B1 risk-haircut wiring (task 003 / ISC-PIPE-3 · DARK-4 · DEGRADED-5).

The haircut is applied ONLY under the rh1 formula versions; the default (``v3-4comp``)
path must never touch prices (ship dark). Offline: a deterministic ``run_analysts``
stub, an injected ``fetch_closes``, and in-memory SQLite — mirrors
``tests/observing_pools/test_pipeline.py``. The exact band math is pinned in
``tests/quant``; here we assert the *wiring* (audit, adjusted column, rank-flip,
monotonicity, degraded→PARTIAL, ship-dark, fail-loud).
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.storage.models as m
from src.observing_pools.agents_bridge import COMPONENT_ANALYST_KEYS
from src.observing_pools.pipeline import RefreshConfig, refresh_pool
from src.observing_pools.scoring import FORMULA_4COMP, FORMULA_4COMP_RH1, FORMULA_5COMP_RH1

UNIVERSE = "data/universes/ai_seed.csv"
_MOMENTUM_KEYS = set(COMPONENT_ANALYST_KEYS["risk_adjusted_momentum"])


def _make_session():
    engine = create_engine("sqlite:///:memory:")
    m.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


@pytest.fixture
def session():
    return _make_session()


def _stub(quality, drop_momentum_for=frozenset()):
    """All-bullish ``run_analysts`` at per-ticker confidence=quality (default 50).

    Omitting the momentum agents for a ticker leaves ``risk_adjusted_momentum=None``
    (tests the skip-the-fetch-when-momentum-None spend-discipline path).
    """

    def stub(tickers, selected, end_date):
        signals: dict[str, dict] = {f"{k}_agent": {} for k in selected}
        for t in tickers:
            q = quality.get(t, 50)
            for k in selected:
                if t in drop_momentum_for and k in _MOMENTUM_KEYS:
                    continue
                signals[f"{k}_agent"][t] = {"signal": "bullish", "confidence": q, "reasoning": "stub"}
        return signals, {"calls": len(selected) * len(tickers)}

    return stub


def _closes(amp, n=60, start=100.0):
    """Deterministic closes whose daily returns alternate exactly ±amp → annualized σ
    grows monotonically with amp. amp=0 → σ=0 (haircut 0); amp≈0.04 → σ≥0.5 (cap 20)."""
    closes = [start]
    for i in range(n):
        closes.append(closes[-1] * ((1 + amp) if i % 2 == 0 else (1 - amp)))
    return closes


_LOW_VOL = _closes(0.0)  # σ=0 → haircut 0
_HIGH_VOL = _closes(0.04)  # σ≥0.5 → haircut capped at 20


def _rh1_config(**kw):
    base = dict(platform_key="ai", universe_csv=UNIVERSE, top_n=30, token_budget=100_000, formula_version=FORMULA_4COMP_RH1)
    base.update(kw)
    return RefreshConfig(**base)


def _entry(session, ticker):
    return session.query(m.ObservationPoolEntry).filter_by(ticker=ticker, platform_key="ai").one()


# ── ISC-DEGRADED-5: rh1 without a fetcher is a loud programming error ──────────


def test_requires_fetch_closes_raises(session):
    """An rh1 formula_version with fetch_closes=None fails loud, before scoring."""
    config = _rh1_config()
    with pytest.raises(ValueError, match="fetch_closes"):
        refresh_pool(session, config, _stub({}), end_date="2026-06-12")


# ── ISC-DARK-4: the default path never touches prices ─────────────────────────


def test_dark_default_never_fetches_prices_and_scores_identical():
    """A default (v3-4comp) run with a fail-loud fetch sentinel completes with scores
    byte-identical to a fetch_closes=None run — the dark path never calls prices."""
    stub = _stub({"NVDA": 90})
    sentinel = lambda *a, **k: pytest.fail("ship-dark violated: default path fetched prices")  # noqa: E731

    s1 = _make_session()
    run1 = refresh_pool(s1, RefreshConfig(platform_key="ai", universe_csv=UNIVERSE, top_n=30, token_budget=100_000), stub, end_date="2026-06-12", fetch_closes=sentinel)
    s1.commit()

    s2 = _make_session()
    refresh_pool(s2, RefreshConfig(platform_key="ai", universe_csv=UNIVERSE, top_n=30, token_budget=100_000), stub, end_date="2026-06-12", fetch_closes=None)
    s2.commit()

    assert run1.status == m.RefreshRunStatus.COMPLETE.value
    scores1 = {e.ticker: e.composite_score for e in s1.query(m.ObservationPoolEntry).all()}
    scores2 = {e.ticker: e.composite_score for e in s2.query(m.ObservationPoolEntry).all()}
    assert scores1 == scores2 and scores1  # non-empty and identical
    # No haircut audit is written under the default version.
    assert "risk_haircut" not in _entry(s1, "NVDA").score_breakdown["components"]["risk_adjusted_momentum"]


# ── ISC-PIPE-3: haircut applied + full audit under rh1 ────────────────────────


def test_rh1_applies_haircut_with_full_audit_and_adjusted_column(session):
    run = refresh_pool(session, _rh1_config(), _stub({"NVDA": 90}), end_date="2026-06-12", fetch_closes=lambda *a: _HIGH_VOL)
    session.commit()

    assert run.composite_formula_version == FORMULA_4COMP_RH1
    nvda = _entry(session, "NVDA")
    audit = nvda.score_breakdown["components"]["risk_adjusted_momentum"]["risk_haircut"]
    assert set(audit) >= {"raw_momentum", "haircut_points", "annualized_volatility", "degraded", "policy"}
    assert audit["raw_momentum"] == 95.0  # bullish@90 → (90+100)/2
    assert audit["haircut_points"] == 20.0  # σ≥0.5 → capped
    assert audit["annualized_volatility"] > 0.5
    assert audit["degraded"] is False
    # The ADJUSTED value (95−20=75) is what lands in the column and drives the composite.
    assert nvda.risk_adjusted_momentum_score == 75.0


def test_rank_flip_high_vol_ranks_strictly_lower():
    """Two candidates, identical momentum, different σ: identical under the base
    formula; the high-σ name strictly lower under rh1 (risk never improves a rank)."""
    stub = _stub({"NVDA": 90, "MSFT": 90})  # identical momentum for both

    s_base = _make_session()
    refresh_pool(s_base, RefreshConfig(platform_key="ai", universe_csv=UNIVERSE, top_n=30, token_budget=100_000, formula_version=FORMULA_4COMP), stub, end_date="2026-06-12")
    s_base.commit()
    c_nvda_base = _entry(s_base, "NVDA").composite_score
    c_msft_base = _entry(s_base, "MSFT").composite_score
    assert c_nvda_base == c_msft_base  # identical rank under the base formula

    s_rh1 = _make_session()
    refresh_pool(s_rh1, _rh1_config(), stub, end_date="2026-06-12", fetch_closes=lambda t, e: _HIGH_VOL if t == "NVDA" else _LOW_VOL)
    s_rh1.commit()
    c_nvda_rh1 = _entry(s_rh1, "NVDA").composite_score
    c_msft_rh1 = _entry(s_rh1, "MSFT").composite_score
    assert c_nvda_rh1 < c_msft_rh1  # the high-σ name is strictly lower
    assert c_msft_rh1 == c_msft_base  # low-σ name unchanged (haircut 0)
    assert c_nvda_rh1 < c_nvda_base  # high-σ name's own rank dropped


def test_pipeline_composite_monotone_non_increasing_in_sigma():
    """σ↑ never RAISES a composite at the pipeline level (I4-spirit invariant)."""
    composites = []
    for amp in (0.0, 0.01, 0.02, 0.03, 0.04):
        closes = _closes(amp)
        s = _make_session()
        refresh_pool(s, _rh1_config(), _stub({"NVDA": 90}), end_date="2026-06-12", fetch_closes=lambda t, e, c=closes: c)
        s.commit()
        composites.append(_entry(s, "NVDA").composite_score)
    assert all(composites[i] >= composites[i + 1] - 1e-9 for i in range(len(composites) - 1))
    assert composites[0] > composites[-1]  # a real haircut range, not a flat no-op


# ── ISC-DEGRADED-5: missing/short/raising price data ──────────────────────────


def test_degraded_short_history_zero_haircut_and_partial(session):
    run = refresh_pool(session, _rh1_config(), _stub({"NVDA": 90}), end_date="2026-06-12", fetch_closes=lambda *a: [100.0, 101.0])
    session.commit()

    assert run.status == m.RefreshRunStatus.PARTIAL.value
    assert "NVDA" in run.fetch_errors["haircut_degraded_tickers"]
    nvda = _entry(session, "NVDA")
    audit = nvda.score_breakdown["components"]["risk_adjusted_momentum"]["risk_haircut"]
    assert audit["degraded"] is True
    assert audit["haircut_points"] == 0.0
    assert audit["annualized_volatility"] is None
    assert nvda.risk_adjusted_momentum_score == 95.0  # unchanged — never fabricate a σ


def test_degraded_fetch_raises_zero_haircut_and_partial(session):
    def boom(ticker, end_date):
        raise RuntimeError("provider down")

    run = refresh_pool(session, _rh1_config(), _stub({"NVDA": 90}), end_date="2026-06-12", fetch_closes=boom)
    session.commit()

    assert run.status == m.RefreshRunStatus.PARTIAL.value
    assert "NVDA" in run.fetch_errors["haircut_degraded_tickers"]
    audit = _entry(session, "NVDA").score_breakdown["components"]["risk_adjusted_momentum"]["risk_haircut"]
    assert audit["degraded"] is True and audit["haircut_points"] == 0.0
    assert _entry(session, "NVDA").risk_adjusted_momentum_score == 95.0


def test_none_momentum_skips_price_fetch_and_is_not_degraded(session):
    """No momentum signal → nothing to haircut → no price call, and NOT flagged
    degraded (a missing signal is not a price-data failure)."""
    called: list[str] = []

    def fetch(ticker, end_date):
        called.append(ticker)
        return _LOW_VOL

    stub = _stub({"MSFT": 90, "NVDA": 90}, drop_momentum_for=frozenset({"MSFT"}))
    run = refresh_pool(session, _rh1_config(), stub, end_date="2026-06-12", fetch_closes=fetch)
    session.commit()

    assert "MSFT" not in called  # spend discipline — no fetch when momentum is None
    assert "NVDA" in called
    audit = _entry(session, "MSFT").score_breakdown["components"]["risk_adjusted_momentum"]["risk_haircut"]
    assert audit["raw_momentum"] is None
    assert audit["degraded"] is False
    # None-momentum skips the haircut entirely — must NOT count as a degraded ticker
    assert run.status == m.RefreshRunStatus.COMPLETE.value


# ── FORMULA_5COMP_RH1: the 5-component rh1 formula also applies the haircut ────


def test_rh1_5comp_applies_haircut(session):
    """The 5-component rh1 formula (v3-5comp-rh1) applies the haircut identically to
    the 4-comp path — the wiring keys off is_rh1, not the component count."""
    run = refresh_pool(session, _rh1_config(formula_version=FORMULA_5COMP_RH1), _stub({"NVDA": 90}), end_date="2026-06-12", fetch_closes=lambda *a: _HIGH_VOL)
    session.commit()

    assert run.status == m.RefreshRunStatus.COMPLETE.value
    assert run.composite_formula_version == FORMULA_5COMP_RH1
    nvda = _entry(session, "NVDA")
    audit = nvda.score_breakdown["components"]["risk_adjusted_momentum"]["risk_haircut"]
    assert audit["degraded"] is False
    assert audit["haircut_points"] == 20.0  # _HIGH_VOL σ ≥ 0.5 → cap, same as 4-comp path
    assert nvda.risk_adjusted_momentum_score == 75.0  # 95 − 20


def test_mixed_degrade_partial_run_only_flags_degraded_tickers(session):
    """One ticker with clean high-vol closes (haircut applied, not degraded) and one
    with too-short history (degraded → zero haircut): the run is PARTIAL, only the
    short-history name is flagged, and its momentum is preserved (never fabricate σ)."""
    called: list[str] = []

    def fetch(ticker: str, end_date: str) -> list[float]:
        called.append(ticker)
        return _HIGH_VOL if ticker == "NVDA" else [100.0, 101.0]

    stub = _stub({"NVDA": 90, "MSFT": 90})  # identical momentum → isolates the degrade path
    run = refresh_pool(session, _rh1_config(), stub, end_date="2026-06-12", fetch_closes=fetch)
    session.commit()

    assert "NVDA" in called  # high-vol fetch executed
    assert "MSFT" in called  # short-history fetch executed — spend discipline holds
    assert run.status == m.RefreshRunStatus.PARTIAL.value
    assert "MSFT" in run.fetch_errors["haircut_degraded_tickers"]
    assert "NVDA" not in run.fetch_errors["haircut_degraded_tickers"]
    assert _entry(session, "NVDA").risk_adjusted_momentum_score < 95.0  # clean haircut applied
    assert _entry(session, "MSFT").risk_adjusted_momentum_score == 95.0  # degraded → momentum preserved
