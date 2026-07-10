"""Unit tests for the observing-pool scoring contract (PRD v4 §11, §17).

Tests encode *why* the math matters: no KeyError on any signal, confidence
clamping, the REQUIRED floor that excludes (never zero-scores) incomplete
entries, the versioned 4-comp vs 5-comp formulas, and the F2 anti-gaming
bootstrap (no reward for absent evidence).
"""

import pytest

from src.observing_pools.scoring import (
    COMPONENT_WEIGHTS,
    FORMULA_4COMP,
    FORMULA_4COMP_RH1,
    FORMULA_5COMP,
    FORMULA_5COMP_RH1,
    AgentSignal,
    base_formula_version,
    build_components,
    composite,
    mean_or_none,
    signal_to_score,
    validate_weights,
)


class TestSignalToScore:
    @pytest.mark.parametrize(
        "signal,confidence,expected",
        [
            (AgentSignal.NEUTRAL, 100, 50.0),
            (AgentSignal.NEUTRAL, 0, 50.0),
            (AgentSignal.BULLISH, 100, 100.0),
            (AgentSignal.BEARISH, 100, 0.0),
            (AgentSignal.BULLISH, 50, 75.0),
            (AgentSignal.BEARISH, 50, 25.0),
        ],
    )
    def test_every_signal_maps(self, signal, confidence, expected):
        assert signal_to_score(signal, confidence) == expected

    def test_confidence_clamped_high_and_low(self):
        assert signal_to_score(AgentSignal.BULLISH, 150) == 100.0  # clamp 150→100
        assert signal_to_score(AgentSignal.BULLISH, -10) == 50.0  # clamp -10→0

    def test_string_signal_is_coerced(self):
        # Agents emit raw strings; the function must accept them.
        assert signal_to_score("bullish", 100) == 100.0

    def test_int_and_float_confidence(self):
        assert signal_to_score(AgentSignal.BULLISH, 80) == 90.0
        assert signal_to_score(AgentSignal.BULLISH, 80.0) == 90.0

    def test_invalid_signal_raises_not_keyerror(self):
        # "watch"/"degraded"/"insufficient-evidence" must never reach this fn;
        # if they do, it's a loud ValueError, never a silent KeyError.
        with pytest.raises(ValueError):
            signal_to_score("watch", 50)


class TestMeanOrNone:
    def test_empty_is_none(self):
        assert mean_or_none([]) is None
        assert mean_or_none([None, None]) is None

    def test_skips_none(self):
        assert mean_or_none([50.0, None, 100.0]) == 75.0


class TestValidateWeights:
    def test_valid_passes(self):
        validate_weights(
            {
                "platform_fit": 0.25,
                "value_investor": 0.30,
                "innovation_growth": 0.20,
                "risk_adjusted_momentum": 0.10,
                "serenity_bottleneck": 0.15,
            }
        )

    def test_missing_component_key_rejected(self):
        # Omitting a non-REQUIRED key must fail loudly here, not crash later
        # with a KeyError when build_components indexes weights[k].
        with pytest.raises(ValueError):
            validate_weights(
                {
                    "platform_fit": 0.25,
                    "value_investor": 0.30,
                    "risk_adjusted_momentum": 0.10,
                    "serenity_bottleneck": 0.15,
                }
            )

    def test_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            validate_weights({"platform_fit": 0.5, "value_investor": 1.5})

    def test_zero_sum_rejected(self):
        with pytest.raises(ValueError):
            validate_weights({"platform_fit": 0.0, "value_investor": 0.0})

    def test_required_weight_zero_rejected(self):
        with pytest.raises(ValueError):
            validate_weights({"platform_fit": 0.0, "value_investor": 0.3})


# Shared component values used across composite tests.
_VALUES = {"platform_fit": 90.0, "value_investor": 40.0, "innovation_growth": 80.0, "risk_adjusted_momentum": 60.0}
# 4-comp expected: (.25*90+.30*40+.20*80+.10*60)/.85 = 56.5/.85
_FOURCOMP_EXPECTED = 56.5 / 0.85


class TestComposite:
    def test_4comp_blended(self):
        comps = build_components(_VALUES, formula_version=FORMULA_4COMP)
        assert "serenity_bottleneck" not in comps  # excluded by design in Phase 5
        result = composite(comps, pool_serenity_median=None, formula_version=FORMULA_4COMP)
        assert result == pytest.approx(_FOURCOMP_EXPECTED)

    def test_required_missing_returns_none(self):
        vals = {**_VALUES, "value_investor": None}
        comps = build_components(vals, formula_version=FORMULA_4COMP)
        # data_unavailable → excluded from ranking, NOT scored 0.
        assert composite(comps, pool_serenity_median=None, formula_version=FORMULA_4COMP) is None

    def test_5comp_serenity_missing_zero_graded_drops_uniformly(self):
        # No graded entries in pool (median None) → serenity dropped for everyone;
        # result equals the 4-comp result over the same present components.
        vals = {**_VALUES, "serenity_bottleneck": None}
        comps = build_components(vals, formula_version=FORMULA_5COMP)
        result = composite(comps, pool_serenity_median=None, formula_version=FORMULA_5COMP)
        assert result == pytest.approx(_FOURCOMP_EXPECTED)

    def test_5comp_serenity_missing_imputes_median(self):
        vals = {**_VALUES, "serenity_bottleneck": None}
        comps = build_components(vals, formula_version=FORMULA_5COMP)
        # median 70 imputed: (56.5 + .15*70)/1.0 = 67.0
        result = composite(comps, pool_serenity_median=70.0, formula_version=FORMULA_5COMP)
        assert result == pytest.approx(67.0)

    def test_5comp_serenity_present_used(self):
        vals = {**_VALUES, "serenity_bottleneck": 30.0}
        comps = build_components(vals, formula_version=FORMULA_5COMP)
        # (56.5 + .15*30)/1.0 = 61.0
        result = composite(comps, pool_serenity_median=None, formula_version=FORMULA_5COMP)
        assert result == pytest.approx(61.0)

    def test_weak_evidence_beats_absent_evidence(self):
        # The anti-gaming invariant: once a pool has graded entries, weak < neutral.
        vals_weak = {**_VALUES, "serenity_bottleneck": 20.0}
        vals_absent = {**_VALUES, "serenity_bottleneck": None}
        weak = composite(build_components(vals_weak, formula_version=FORMULA_5COMP), pool_serenity_median=50.0, formula_version=FORMULA_5COMP)
        absent = composite(build_components(vals_absent, formula_version=FORMULA_5COMP), pool_serenity_median=50.0, formula_version=FORMULA_5COMP)
        assert weak < absent  # absent imputes to neutral median (50) > weak (20)

    def test_divide_by_zero_guard(self):
        # Present REQUIRED but total present weight 0 → None, not ZeroDivisionError.
        comps = {"platform_fit": (0.0, 90.0), "value_investor": (0.0, 40.0)}
        assert composite(comps, pool_serenity_median=None, formula_version=FORMULA_4COMP) is None


class TestBaseFormulaVersion:
    def test_rh1_maps_to_base(self):
        assert base_formula_version(FORMULA_4COMP_RH1) == FORMULA_4COMP
        assert base_formula_version(FORMULA_5COMP_RH1) == FORMULA_5COMP

    def test_non_rh1_versions_map_to_themselves(self):
        assert base_formula_version(FORMULA_4COMP) == FORMULA_4COMP
        assert base_formula_version(FORMULA_5COMP) == FORMULA_5COMP

    def test_unknown_version_maps_to_itself(self):
        # Pin current (pre-rh1) fallback behavior — do not silently widen acceptance.
        assert base_formula_version("garbage") == "garbage"


class TestRh1BuildComponents:
    def test_4comp_rh1_treated_exactly_as_4comp(self):
        comps_rh1 = build_components(_VALUES, formula_version=FORMULA_4COMP_RH1)
        comps_base = build_components(_VALUES, formula_version=FORMULA_4COMP)
        assert set(comps_rh1) == set(comps_base)
        assert "serenity_bottleneck" not in comps_rh1

    def test_5comp_rh1_treated_exactly_as_5comp(self):
        vals = {**_VALUES, "serenity_bottleneck": 40.0}
        comps_rh1 = build_components(vals, formula_version=FORMULA_5COMP_RH1)
        comps_base = build_components(vals, formula_version=FORMULA_5COMP)
        assert set(comps_rh1) == set(comps_base) == set(COMPONENT_WEIGHTS)


class TestRh1Composite:
    def test_4comp_rh1_composite_matches_4comp(self):
        comps_rh1 = build_components(_VALUES, formula_version=FORMULA_4COMP_RH1)
        result_rh1 = composite(comps_rh1, pool_serenity_median=None, formula_version=FORMULA_4COMP_RH1)
        assert result_rh1 == pytest.approx(_FOURCOMP_EXPECTED)

    def test_5comp_rh1_serenity_bootstrap_fires_zero_graded(self):
        # Same F2 bootstrap as plain 5comp: zero graded (median None) -> serenity
        # dropped uniformly -> identical to the 4-comp result.
        vals = {**_VALUES, "serenity_bottleneck": None}
        comps = build_components(vals, formula_version=FORMULA_5COMP_RH1)
        result = composite(comps, pool_serenity_median=None, formula_version=FORMULA_5COMP_RH1)
        assert result == pytest.approx(_FOURCOMP_EXPECTED)

    def test_5comp_rh1_serenity_bootstrap_fires_imputes_median(self):
        vals = {**_VALUES, "serenity_bottleneck": None}
        comps = build_components(vals, formula_version=FORMULA_5COMP_RH1)
        result = composite(comps, pool_serenity_median=70.0, formula_version=FORMULA_5COMP_RH1)
        assert result == pytest.approx(67.0)  # same as test_5comp_serenity_missing_imputes_median


class TestUnknownFormulaVersionUnchanged:
    def test_unknown_version_still_gets_fivecomp_keys(self):
        # Pin the current fallback: anything != FORMULA_4COMP (base-mapped) gets
        # the full 5-key set — an unknown version must not silently narrow it.
        comps = build_components(_VALUES, formula_version="garbage")
        assert set(comps) == set(COMPONENT_WEIGHTS)

    def test_unknown_version_does_not_trigger_serenity_bootstrap(self):
        vals = {**_VALUES, "serenity_bottleneck": None}
        comps = build_components(vals, formula_version="garbage")
        # Only FORMULA_5COMP(-rh1) fires the bootstrap; an unknown version leaves
        # serenity_bottleneck present-but-None, excluding it from `present`.
        result = composite(comps, pool_serenity_median=70.0, formula_version="garbage")
        four = composite(build_components(_VALUES, formula_version=FORMULA_4COMP), pool_serenity_median=None, formula_version=FORMULA_4COMP)
        assert result == pytest.approx(four)
