"""Scoring-stability suite (PRD v4 §11.2-11.3). Pure deterministic math:
- composite() is reproducible (pass^k consistency) AND matches an independent
  weighted-mean reference (intent, not just behavior);
- a degraded analyst is EXCLUDED (None), never a masking 50 that outranks a real
  bearish candidate;
- a missing REQUIRED component excludes the entry (None), never scores it 0;
- the F2 bootstrap imputes absent serenity at the graded-only median (neutral),
  never favorably, and drops it uniformly when zero graded;
- degenerate/rank-inverting weight configs are rejected.
No DB, no LLM — pure functions called with in-memory fixtures.
"""

from __future__ import annotations

from src.evals.core import CodeGrader, EvalCase, Recorder, TARGET_REGRESSION
from src.evals.registry import suite
from src.observing_pools.agents_bridge import COMPONENT_ANALYST_KEYS, component_scores
from src.observing_pools.scoring import (
    build_components,
    COMPONENT_WEIGHTS,
    composite,
    FORMULA_4COMP,
    FORMULA_5COMP,
    validate_weights,
)

_SUITE = "scoring"
_VALUE_KEYS = COMPONENT_ANALYST_KEYS["value_investor"]


def _composite_deterministic_and_correct(rec: Recorder) -> bool:
    values = {"platform_fit": 90.0, "value_investor": 80.0, "innovation_growth": 70.0, "risk_adjusted_momentum": 60.0}
    comps = build_components(values, formula_version=FORMULA_4COMP)
    a = composite(comps, pool_serenity_median=None, formula_version=FORMULA_4COMP)
    b = composite(comps, pool_serenity_median=None, formula_version=FORMULA_4COMP)
    expected = sum(COMPONENT_WEIGHTS[k] * v for k, v in values.items()) / sum(COMPONENT_WEIGHTS[k] for k in values)
    rec.record("composite", a=a, b=b, expected=expected)
    return a == b and a is not None and abs(a - expected) < 1e-9


def _degraded_never_outranks_bearish(rec: Recorder) -> bool:
    # Ticker A: every value_investor analyst emits a garbage signal -> all degraded.
    degraded = {f"{k}_agent": {"A": {"signal": "garbage", "confidence": 50}} for k in _VALUE_KEYS}
    comps_a, _ = component_scores(degraded, "A", platform_fit_score=80.0)
    # All-degraded component must be None (excluded from the mean), NOT a masking 50.
    if comps_a["value_investor"] is not None:
        return False
    score_a = composite(build_components(comps_a, formula_version=FORMULA_4COMP), pool_serenity_median=None, formula_version=FORMULA_4COMP)

    # Ticker B: a genuine bearish value_investor read -> a real (low) score.
    bearish = {f"{k}_agent": {"B": {"signal": "bearish", "confidence": 90}} for k in _VALUE_KEYS}
    comps_b, _ = component_scores(bearish, "B", platform_fit_score=80.0)
    score_b = composite(build_components(comps_b, formula_version=FORMULA_4COMP), pool_serenity_median=None, formula_version=FORMULA_4COMP)

    rec.record("composite", A=score_a, B=score_b)
    # A is excluded (None, data_unavailable); B is a real number. A can never outrank B.
    # (If degraded scored 50, A's value_investor 50 > B's bearish 5 would invert the rank.)
    return score_a is None and score_b is not None


def _required_gate_excludes_not_zeroes(rec: Recorder) -> bool:
    comps = build_components(
        {"platform_fit": 80.0, "value_investor": None, "innovation_growth": 70.0, "risk_adjusted_momentum": 60.0},
        formula_version=FORMULA_4COMP,
    )
    out = composite(comps, pool_serenity_median=None, formula_version=FORMULA_4COMP)
    rec.record("composite", out=out)
    return out is None  # excluded (data_unavailable), not 0.0


def _bootstrap_no_reward_for_absent_evidence(rec: Recorder) -> bool:
    base = {"platform_fit": 80.0, "value_investor": 70.0, "innovation_growth": 60.0, "risk_adjusted_momentum": 50.0}

    # (1) Zero graded (median None): serenity dropped uniformly -> identical to 4-comp.
    dropped = composite(build_components({**base, "serenity_bottleneck": None}, formula_version=FORMULA_5COMP), pool_serenity_median=None, formula_version=FORMULA_5COMP)
    four = composite(build_components(base, formula_version=FORMULA_4COMP), pool_serenity_median=None, formula_version=FORMULA_4COMP)
    if dropped != four:
        return False

    # (2) Some graded (median 50): absent serenity is imputed AT the median (neutral),
    #     i.e. identical to passing serenity=50 explicitly — never favorable.
    imputed_absent = composite(build_components({**base, "serenity_bottleneck": None}, formula_version=FORMULA_5COMP), pool_serenity_median=50.0, formula_version=FORMULA_5COMP)
    explicit_median = composite(build_components({**base, "serenity_bottleneck": 50.0}, formula_version=FORMULA_5COMP), pool_serenity_median=50.0, formula_version=FORMULA_5COMP)
    if imputed_absent != explicit_median:
        return False

    # (3) A genuinely strong-evidence entry (serenity 90) beats an absent one ->
    #     absence is treated as neutral, never rewarded over real evidence.
    strong = composite(build_components({**base, "serenity_bottleneck": 90.0}, formula_version=FORMULA_5COMP), pool_serenity_median=50.0, formula_version=FORMULA_5COMP)
    rec.record("bootstrap", dropped=dropped, four=four, imputed_absent=imputed_absent, explicit_median=explicit_median, strong=strong)
    return strong is not None and imputed_absent is not None and strong > imputed_absent


def _weight_validation_rejects_degenerate(rec: Recorder) -> bool:
    bad = [
        {**COMPONENT_WEIGHTS, "platform_fit": 1.5},  # outside [0,1]
        {k: 0.0 for k in COMPONENT_WEIGHTS},  # zero sum
        {**COMPONENT_WEIGHTS, "value_investor": 0.0},  # REQUIRED weight <= 0
        {"platform_fit": 0.5},  # missing component keys
    ]
    for cfg in bad:
        try:
            validate_weights(cfg)
        except ValueError:
            continue
        return False  # a degenerate (rank-inverting) config was accepted
    validate_weights(COMPONENT_WEIGHTS)  # the real config must pass
    rec.record("validate_weights", rejected=len(bad))
    return True


@suite(_SUITE)
def build() -> list[EvalCase]:
    return [
        EvalCase("composite_deterministic_and_correct", _SUITE, CodeGrader("scoring.composite_deterministic_and_correct", _composite_deterministic_and_correct), trials=5, target=TARGET_REGRESSION, description="pass^k consistency + independent reference"),
        EvalCase("degraded_never_outranks_bearish", _SUITE, CodeGrader("scoring.degraded_never_outranks_bearish", _degraded_never_outranks_bearish)),
        EvalCase("required_gate_excludes_not_zeroes", _SUITE, CodeGrader("scoring.required_gate_excludes_not_zeroes", _required_gate_excludes_not_zeroes)),
        EvalCase("bootstrap_no_reward_for_absent_evidence", _SUITE, CodeGrader("scoring.bootstrap_no_reward_for_absent_evidence", _bootstrap_no_reward_for_absent_evidence)),
        EvalCase("weight_validation_rejects_degenerate", _SUITE, CodeGrader("scoring.weight_validation_rejects_degenerate", _weight_validation_rejects_degenerate)),
    ]
