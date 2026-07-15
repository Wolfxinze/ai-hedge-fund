"""Regression tests: negative latest revenue must not crash the CAGR sites.

A negative latest revenue with 3+ data points makes the fractional-power
CAGR expression return a complex number; the downstream numeric comparison
(`cagr > 0.08`) or `min(..., 0.12)` then raises TypeError. These tests pin
that a negative latest revenue routes to the existing fallback branches.
"""

from types import SimpleNamespace

from src.agents.aswath_damodaran import (
    analyze_growth_and_reinvestment,
    calculate_intrinsic_value_dcf,
)


def _metric(revenue, roic=None):
    ns = SimpleNamespace(
        revenue=revenue,
        return_on_invested_capital=roic,
        free_cash_flow=1000.0,
    )
    ns.model_dump = lambda: {"revenue": revenue}
    return ns


def _line_item(fcf=1000.0, shares=100.0):
    return SimpleNamespace(free_cash_flow=fcf, outstanding_shares=shares)


def test_negative_latest_revenue_growth_score_no_crash():
    # metrics are NEWEST-first; oldest revenue positive, latest negative.
    # 3 points -> fractional exponent (1/2) -> negative base -> complex.
    metrics = [_metric(-50.0), _metric(120.0), _metric(100.0)]
    line_items = [_line_item(), _line_item()]

    result = analyze_growth_and_reinvestment(metrics, line_items)

    assert isinstance(result, dict)
    assert "Revenue data incomplete" in result["details"]


def test_negative_latest_revenue_dcf_no_crash():
    metrics = [_metric(-50.0), _metric(120.0), _metric(100.0)]
    line_items = [_line_item()]

    result = calculate_intrinsic_value_dcf(metrics, line_items, {})

    assert isinstance(result, dict)
    # base_growth fell back to 0.04; DCF still completes to a real value.
    assert result["intrinsic_value"] is not None
    assert isinstance(result["intrinsic_value"], float)
    assert result["assumptions"]["base_growth"] == 0.04


def test_positive_revenue_paths_unchanged():
    # Growing positive revenues: CAGR ~= 22.5% -> +2 growth points, real DCF.
    metrics = [_metric(150.0), _metric(120.0), _metric(100.0)]
    line_items = [_line_item()]

    growth = analyze_growth_and_reinvestment(metrics, line_items)
    assert growth["score"] >= 2
    assert "Revenue CAGR" in growth["details"]

    dcf = calculate_intrinsic_value_dcf(metrics, line_items, {})
    assert isinstance(dcf["intrinsic_value"], float)
    assert dcf["intrinsic_value"] > 0
