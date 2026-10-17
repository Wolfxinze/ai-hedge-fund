"""Bit-pool daily snapshot math + alerts (research-only; never places orders).

Every number here is hand-computable from the fixture, so a formula change (e.g.
weights over degraded positions, pool daily % from closes instead of prev closes)
turns a test RED. Alert boundaries are pinned on BOTH sides: a threshold that drifts
by a hair, or flips ``>=`` to ``>``, fails here. Price fetch is always injected —
no network.
"""

from datetime import date

import pytest

from src.bit_pool.holdings import Holding, Trade
from src.bit_pool.simulate import (
    BASELINE_HEAVY_PCT,
    BASELINE_REVIEW_PCT,
    BASELINE_WARN_PCT,
    DAILY_MOVE_PCT,
    STALE_DAYS,
    PoolSnapshot,
    simulate_day,
)
from src.data.models import Price

AS_OF = date(2026, 1, 9)  # a Friday


def _p(close: float, time: str) -> Price:
    return Price(open=close, close=close, high=close, low=close, volume=1, time=time)


def _h(ticker, shares, baseline, cost=None) -> Holding:
    return Holding(ticker=ticker, shares=shares, baseline_price=baseline, baseline_date=date(2026, 1, 2), cost_basis=cost)


def _fetch_from(table: dict[str, list[Price]], calls: list | None = None):
    def fetch(ticker, start_iso, end_iso):
        if calls is not None:
            calls.append((ticker, start_iso, end_iso))
        value = table[ticker]
        if isinstance(value, Exception):
            raise value
        return value

    return fetch


def _one(prev_close: float, close: float, *, baseline: float = 100.0, close_day: str = "2026-01-09") -> PoolSnapshot:
    fetch = _fetch_from({"AAA": [_p(prev_close, "2026-01-02"), _p(close, close_day)]})
    return simulate_day((_h("AAA", 1, baseline),), (), AS_OF, fetch_prices=fetch)


def test_alert_constants_contract():
    assert (DAILY_MOVE_PCT, BASELINE_WARN_PCT, BASELINE_HEAVY_PCT, BASELINE_REVIEW_PCT, STALE_DAYS) == (5.0, -10.0, -20.0, 20.0, 4)


def test_snapshot_math_exact_on_fixture():
    holdings = (_h("AAA", 10, 100.0, cost=90.0), _h("BBB", 5, 200.0))
    table = {
        "AAA": [_p(99.0, "2026-01-07"), _p(100.0, "2026-01-08"), _p(102.0, "2026-01-09"), _p(500.0, "2026-01-12")],  # last row is AFTER as_of
        "BBB": [_p(210.0, "2026-01-08"), _p(189.0, "2026-01-09")],
    }
    calls: list = []
    snap = simulate_day(holdings, (), AS_OF, fetch_prices=_fetch_from(table, calls))

    # window = as_of-14d .. as_of
    assert calls[0] == ("AAA", "2025-12-26", "2026-01-09")

    a, b = snap.positions
    assert (a.close, a.prev_close, a.close_date) == (102.0, 100.0, date(2026, 1, 9))
    assert a.market_value == pytest.approx(1020.0)
    assert a.daily_pct == pytest.approx(2.0)
    assert a.vs_baseline_pct == pytest.approx(2.0)
    assert a.unrealized_pnl == pytest.approx((102.0 - 90.0) * 10)
    assert b.market_value == pytest.approx(945.0)
    assert b.daily_pct == pytest.approx((189.0 / 210.0 - 1) * 100)
    assert b.vs_baseline_pct == pytest.approx(-5.5)
    assert b.unrealized_pnl is None  # no cost basis -> no fabricated P&L

    assert snap.total_value == pytest.approx(1965.0)
    assert snap.prev_total_value == pytest.approx(10 * 100.0 + 5 * 210.0)
    assert snap.daily_pct == pytest.approx((1965.0 / 2050.0 - 1) * 100)
    assert snap.baseline_value == pytest.approx(2000.0)
    assert snap.vs_baseline_pct == pytest.approx((1965.0 / 2000.0 - 1) * 100)
    assert a.weight == pytest.approx(1020.0 / 1965.0)
    assert abs(a.weight + b.weight - 1.0) <= 1e-9
    assert snap.degraded == ()
    assert snap.realized_pnl == 0.0 and snap.net_cash_flow == 0.0


def test_unsorted_provider_rows_are_sorted_by_time():
    fetch = _fetch_from({"AAA": [_p(110.0, "2026-01-09"), _p(100.0, "2026-01-08")]})
    snap = simulate_day((_h("AAA", 1, 100.0),), (), AS_OF, fetch_prices=fetch)
    assert (snap.positions[0].close, snap.positions[0].prev_close) == (110.0, 100.0)


def test_datetime_style_time_strings_are_accepted():
    fetch = _fetch_from({"AAA": [_p(100.0, "2026-01-08T00:00:00Z"), _p(110.0, "2026-01-09T00:00:00Z")]})
    snap = simulate_day((_h("AAA", 1, 100.0),), (), AS_OF, fetch_prices=fetch)
    assert snap.positions[0].close_date == date(2026, 1, 9)


# ── alert boundaries ──
@pytest.mark.parametrize("close,fires", [(105.0, True), (95.0, True), (104.99, False), (95.01, False)])
def test_daily_move_boundary(close, fires):
    snap = _one(100.0, close, baseline=close)  # baseline == close keeps baseline alerts quiet
    assert ("DAILY_MOVE" in snap.positions[0].alerts) is fires


@pytest.mark.parametrize(
    "close,expected",
    [
        (90.0, {"BASELINE_WARN"}),  # exactly -10
        (90.01, set()),  # -9.99
        (80.0, {"BASELINE_HEAVY"}),  # exactly -20 -> HEAVY, not WARN
        (80.01, {"BASELINE_WARN"}),  # -19.99
        (120.0, {"BASELINE_REVIEW"}),  # exactly +20
        (119.99, set()),  # +19.99
    ],
)
def test_baseline_boundaries(close, expected):
    snap = _one(close, close)  # prev == close keeps DAILY_MOVE quiet
    assert set(snap.positions[0].alerts) == expected


@pytest.mark.parametrize("close_day,fires", [("2026-01-04", True), ("2026-01-05", False)])
def test_stale_fires_at_five_days_not_four(close_day, fires):
    fetch = _fetch_from({"AAA": [_p(100.0, "2026-01-01"), _p(100.0, close_day)]})
    snap = simulate_day((_h("AAA", 1, 100.0),), (), AS_OF, fetch_prices=fetch)
    assert ("STALE" in snap.positions[0].alerts) is fires


def test_pool_alerts_aggregate_position_alerts():
    snap = _one(100.0, 105.0, baseline=105.0)
    assert ("AAA", "DAILY_MOVE") in snap.alerts


# ── degradation: visible, excluded, never silently zero ──
def test_fetch_exception_degrades_and_is_excluded_from_totals():
    holdings = (_h("AAA", 10, 100.0), _h("BBB", 5, 200.0), _h("CCC", 1, 50.0))
    table = {
        "AAA": [_p(100.0, "2026-01-08"), _p(102.0, "2026-01-09")],
        "BBB": RuntimeError("provider down"),
        "CCC": [_p(51.0, "2026-01-09")],  # only one close
    }
    snap = simulate_day(holdings, (), AS_OF, fetch_prices=_fetch_from(table))
    by = {p.ticker: p for p in snap.positions}
    assert snap.degraded == ("BBB", "CCC")
    assert "provider down" in by["BBB"].degraded_reason
    assert "close" in by["CCC"].degraded_reason
    for t in ("BBB", "CCC"):
        assert by[t].market_value is None and by[t].weight is None  # not a fake 0
    assert snap.total_value == pytest.approx(1020.0)
    assert snap.baseline_value == pytest.approx(1000.0)
    assert by["AAA"].weight == pytest.approx(1.0)


def test_closes_after_as_of_do_not_count_toward_two():
    fetch = _fetch_from({"AAA": [_p(100.0, "2026-01-09"), _p(101.0, "2026-01-12")]})
    snap = simulate_day((_h("AAA", 1, 100.0),), (), AS_OF, fetch_prices=fetch)
    assert snap.degraded == ("AAA",)


def test_all_degraded_pool_has_no_fabricated_percentages():
    snap = simulate_day((_h("AAA", 1, 100.0),), (), AS_OF, fetch_prices=_fetch_from({"AAA": RuntimeError("x")}))
    assert snap.total_value == 0.0
    assert snap.daily_pct is None and snap.vs_baseline_pct is None


# ── ledger flows into the snapshot ──
def test_trades_flow_into_snapshot():
    holdings = (_h("AAA", 10, 100.0, cost=100.0),)
    trades = (Trade(date(2026, 1, 5), "AAA", "sell", 4, 130.0, ""),)
    fetch = _fetch_from({"AAA": [_p(100.0, "2026-01-08"), _p(110.0, "2026-01-09")]})
    snap = simulate_day(holdings, trades, AS_OF, fetch_prices=fetch)
    assert snap.positions[0].shares == 6.0
    assert snap.total_value == pytest.approx(660.0)
    assert snap.realized_pnl == pytest.approx(120.0)
    assert snap.net_cash_flow == pytest.approx(520.0)
