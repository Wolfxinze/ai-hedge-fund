"""Regression tests: a profit->loss earnings series must not crash the CAGR site.

When oldest net_income is positive but latest is negative, the fractional-power
CAGR expression ``(latest / oldest) ** (1 / years)`` returns a complex number;
the downstream ``min(historical_growth, 0.15)`` then raises
``TypeError: '<' not supported between instances of 'float' and 'complex'``.
These tests pin that a profit->loss series routes to the -5% cap floor instead.
"""

from types import SimpleNamespace

from src.agents.warren_buffett import calculate_intrinsic_value


def _item(net_income):
    # Only fields the valuation path touches: net_income drives historical
    # growth; depreciation/capex/shares keep owner_earnings positive & valid.
    return SimpleNamespace(
        net_income=net_income,
        depreciation_and_amortization=100.0,
        capital_expenditure=-50.0,
        outstanding_shares=1000.0,
    )


def test_profit_to_loss_earnings_do_not_crash():
    """oldest profit, latest loss -> returns a dict, intrinsic_value never complex."""
    # newest-first: latest is a loss (-500), oldest is a profit (+800)
    items = [_item(v) for v in (-500.0, 200.0, 400.0, 600.0, 800.0)]

    result = calculate_intrinsic_value(items)

    assert isinstance(result, dict)
    iv = result["intrinsic_value"]
    assert not isinstance(iv, complex)
    assert iv is None or isinstance(iv, (int, float))


def test_profit_to_loss_maps_to_cap_floor():
    """A measurable decline maps to the -5% cap floor -> Stage 1 growth of -3.5%."""
    items = [_item(v) for v in (-500.0, 200.0, 400.0, 600.0, 800.0)]

    result = calculate_intrinsic_value(items)

    details = " ".join(result["details"])
    # conservative_growth = -0.05 * 0.7 = -0.035 -> stage1_growth = -3.5%
    assert "Stage 1 (-3.5%" in details


def test_positive_earnings_path_unchanged():
    """All-positive growing earnings still produce a positive Stage 1 growth."""
    # newest-first descending: latest 800 (profit), oldest 100 (profit)
    items = [_item(v) for v in (800.0, 600.0, 400.0, 200.0, 100.0)]

    result = calculate_intrinsic_value(items)

    iv = result["intrinsic_value"]
    assert isinstance(iv, float) and iv > 0
    details = " ".join(result["details"])
    # growth caps at 15% -> haircut 10.5% -> stage1_growth capped at 8.0%
    assert "Stage 1 (8.0%" in details
