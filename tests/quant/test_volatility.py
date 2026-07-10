"""Pin the B1 banded-subtractive risk-haircut math (parent PRD "The math (exact
spec)"): band values, continuity at edges, monotonicity (risk never improves a
rank), clamping, short-history degradation, and parity with the risk_manager
volatility math it mirrors (without importing it from the scoring path — tests
MAY import risk_manager; src/quant/ may not, see test_no_agents_import below).
"""

from __future__ import annotations

import math

import pytest

from src.quant.volatility import (
    POLICY,
    annualized_volatility_from_closes,
    apply_risk_haircut,
    haircut_points,
)


class TestAnnualizedVolatilityFromCloses:
    def test_short_history_is_none(self):
        assert annualized_volatility_from_closes([]) is None
        assert annualized_volatility_from_closes([100.0]) is None
        assert annualized_volatility_from_closes([100.0, 101.0]) is None  # 1 return < 2

    def test_three_closes_two_returns_is_computed(self):
        # Exactly the boundary where it stops being None.
        vol = annualized_volatility_from_closes([100.0, 101.0, 99.0])
        assert vol is not None and vol > 0

    def test_zero_close_raises(self):
        with pytest.raises(ValueError):
            annualized_volatility_from_closes([100.0, 0.0, 99.0])

    def test_negative_close_raises(self):
        with pytest.raises(ValueError):
            annualized_volatility_from_closes([100.0, -5.0, 99.0])

    def test_uses_only_last_lookback_days_returns(self):
        # A volatile head followed by a flat tail: with lookback=2 only the flat
        # tail's 2 returns are used -> vol collapses to ~0, proving the truncation.
        closes = [50.0, 150.0, 10.0, 100.0, 100.0, 100.0]
        vol = annualized_volatility_from_closes(closes, lookback_days=2)
        assert vol == pytest.approx(0.0, abs=1e-9)

    def test_constant_closes_zero_volatility(self):
        vol = annualized_volatility_from_closes([100.0] * 10)
        assert vol == pytest.approx(0.0, abs=1e-9)


class TestHaircutPoints:
    def test_negative_raises(self):
        with pytest.raises(ValueError):
            haircut_points(-0.01)

    def test_below_low_band_is_zero(self):
        assert haircut_points(0.0) == 0.0
        assert haircut_points(0.10) == 0.0

    def test_band_edges_pinned(self):
        assert haircut_points(0.15) == pytest.approx(0.0)
        assert haircut_points(0.30) == pytest.approx(10.0)
        assert haircut_points(0.50) == pytest.approx(20.0)

    def test_interior_values_pinned(self):
        # midpoint of 0.15-0.30 band -> 5 pts
        assert haircut_points(0.225) == pytest.approx(5.0)
        # midpoint of 0.30-0.50 band -> 15 pts
        assert haircut_points(0.40) == pytest.approx(15.0)

    def test_capped_above_high_band(self):
        assert haircut_points(0.75) == 20.0
        assert haircut_points(5.0) == 20.0

    def test_continuous_at_edges(self):
        eps = 1e-9
        assert haircut_points(0.15 - eps) == pytest.approx(haircut_points(0.15 + eps), abs=1e-6)
        assert haircut_points(0.30 - eps) == pytest.approx(haircut_points(0.30 + eps), abs=1e-6)
        assert haircut_points(0.50 - eps) == pytest.approx(haircut_points(0.50 + eps), abs=1e-6)

    def test_monotone_non_decreasing_over_grid(self):
        grid = [i / 1000 for i in range(0, 1001, 5)]
        values = [haircut_points(v) for v in grid]
        assert all(b >= a for a, b in zip(values, values[1:]))


class TestApplyRiskHaircut:
    def test_momentum_none_untouched(self):
        adjusted, audit = apply_risk_haircut(None, 0.40)
        assert adjusted is None
        assert audit["raw_momentum"] is None
        assert audit["policy"] == POLICY

    def test_vol_none_is_degraded_zero_haircut(self):
        adjusted, audit = apply_risk_haircut(60.0, None)
        assert adjusted == 60.0  # unchanged
        assert audit["degraded"] is True
        assert audit["haircut_points"] == 0.0
        assert audit["annualized_volatility"] is None
        assert audit["raw_momentum"] == 60.0
        assert audit["policy"] == POLICY

    def test_normal_path_not_degraded(self):
        adjusted, audit = apply_risk_haircut(60.0, 0.30)
        assert adjusted == pytest.approx(50.0)  # 60 - 10
        assert audit["degraded"] is False
        assert audit["haircut_points"] == pytest.approx(10.0)
        assert audit["annualized_volatility"] == pytest.approx(0.30)

    def test_clamp_floor_at_zero(self):
        adjusted, _ = apply_risk_haircut(5.0, 0.60)  # 5 - 20 = -15 -> clamp 0
        assert adjusted == 0.0

    def test_clamp_ceiling_at_hundred(self):
        # momentum already saturated; haircut only ever subtracts, so this pins
        # the clamp's upper bound is a no-op ceiling, not a bug if h were negative.
        adjusted, _ = apply_risk_haircut(100.0, 0.0)
        assert adjusted == 100.0

    @pytest.mark.parametrize("momentum", [80.0, 50.0, 20.0])  # bullish, neutral, bearish
    def test_monotone_non_increasing_in_sigma(self, momentum):
        grid = [i / 1000 for i in range(0, 1001, 10)]
        adjusted_values = [apply_risk_haircut(momentum, v)[0] for v in grid]
        assert all(b <= a for a, b in zip(adjusted_values, adjusted_values[1:])), "adjusted must never increase as sigma increases"

    @pytest.mark.parametrize("momentum,vol", [(0.0, 0.0), (50.0, 0.40), (100.0, 0.80), (30.0, 1.5)])
    def test_bounds_always_in_0_100(self, momentum, vol):
        adjusted, _ = apply_risk_haircut(momentum, vol)
        assert 0.0 <= adjusted <= 100.0


class TestParityWithRiskManager:
    def test_matches_calculate_volatility_metrics(self):
        # tests MAY import risk_manager (characterization only) — the scoring
        # path (src/quant/, src/observing_pools/) must never do so (I1).
        import pandas as pd

        from src.agents.risk_manager import calculate_volatility_metrics

        closes = [100.0 + (i * 0.37) - ((i % 7) * 0.9) for i in range(80)]
        assert all(c > 0 for c in closes)

        pure_vol = annualized_volatility_from_closes(closes, lookback_days=60)
        ref = calculate_volatility_metrics(pd.DataFrame({"close": closes}), lookback_days=60)

        assert pure_vol == pytest.approx(ref["annualized_volatility"], abs=1e-9)

    def test_short_history_diverges_deliberately(self):
        # risk_manager fabricates a 5%-daily fallback; the pure module returns
        # None instead (decision 2 — never fabricate a worst-case sigma).
        import pandas as pd

        from src.agents.risk_manager import calculate_volatility_metrics

        closes = [100.0, 101.0]
        pure_vol = annualized_volatility_from_closes(closes)
        ref = calculate_volatility_metrics(pd.DataFrame({"close": closes}))

        assert pure_vol is None
        assert ref["annualized_volatility"] == pytest.approx(0.05 * math.sqrt(252))
