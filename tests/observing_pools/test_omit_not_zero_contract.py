"""Cross-module contract: an absent analyst is OMITTED from the mean, never folded in
as a zero (or any default). This is a shared invariant of two independently-evolving
call sites — ``agents_bridge.component_scores`` (composite components) and
``committee_flow._aggregate`` (monitor bands) — so it lives in one module that imports
BOTH. The referencing WATCH-POINT comments in those files name this test.

Mutation-sensitive by construction: each case has one high-scoring present analyst and
one absent analyst in the same group. Omit → the present score stands; fold-absent-as-0
→ the mean halves and the band/value flips. Any code change that scores an absent
analyst as 0 (or 50, or anything) fails at least one assertion here.
"""

from src.monitoring.committee_flow import _aggregate
from src.observing_pools.agents_bridge import component_scores
from src.storage.models import ReportLabel

_TICKER = "NVDA"
# bullish @ confidence 100 → signal_to_score = 100.0 (the maximum), so a folded-in 0
# drags a 2-analyst mean to 50.0 — a different band and a different component value.
_BULLISH_MAX = {"signal": "bullish", "confidence": 100}


def test_component_scores_omits_absent_analyst_not_zero():
    # innovation_growth = (cathie_wood, growth_analyst); only cathie_wood present.
    signals = {"cathie_wood_agent": {_TICKER: _BULLISH_MAX}}
    components, breakdown = component_scores(signals, _TICKER, platform_fit_score=None)
    # Present-only mean is 100.0. Folding the absent growth_analyst in as 0 → 50.0.
    assert components["innovation_growth"] == 100.0, "absent analyst must be omitted, not scored 0"
    # The absent analyst is not even recorded in the per-agent breakdown.
    assert set(breakdown["components"]["innovation_growth"]["agents"]) == {"cathie_wood"}


def test_aggregate_omits_absent_analyst_not_zero():
    committee = ["cathie_wood", "growth_analyst"]  # two valid keys, one with no signal
    signals = {"cathie_wood_agent": {_TICKER: _BULLISH_MAX}}
    result = _aggregate(_TICKER, committee, signals)
    # Omit → mean 100 → THESIS_SUPPORTIVE over 1 analyst. Fold-as-0 → mean 50 → MIXED over 2.
    assert result.label is ReportLabel.THESIS_SUPPORTIVE, "absent analyst must not drag the band via a 0"
    assert "100.0/100 over 1 analyst" in result.summary
    assert result.degraded is False


def test_both_call_sites_agree_on_omit_contract():
    """The two sites must reach the SAME verdict on the SAME single-present input — the
    coupling the WATCH-POINT comments warn about. If one folds absent→0 and the other
    omits, this cross-check breaks even if the per-site tests above were softened."""
    signals = {"cathie_wood_agent": {_TICKER: _BULLISH_MAX}}
    components, _ = component_scores(signals, _TICKER, platform_fit_score=None)
    result = _aggregate(_TICKER, ["cathie_wood", "growth_analyst"], signals)
    # component mean and committee mean are both the single present score (100), un-halved.
    assert components["innovation_growth"] == 100.0
    assert "100.0/100" in result.summary
