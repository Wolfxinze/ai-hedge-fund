"""Bit-pool daily snapshot math + alerts (research-only; never places orders).

Every number here is hand-computable from the fixture, so a formula change (e.g.
weights over degraded positions, pool daily % from closes instead of prev closes)
turns a test RED. Alert boundaries are pinned with values on each side of every
threshold (e.g. a 5.00% move fires, 4.999% does not). Price fetch and the clock are
always injected — no network.
"""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

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
ET = ZoneInfo("America/New_York")


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
@pytest.mark.parametrize("close,fires", [(105.0, True), (95.0, True), (104.99, False), (95.01, False), (104.999, False), (95.001, False)])
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


def test_all_degraded_pool_has_no_fabricated_totals_or_percentages():
    # A pool worth "0.00" when the provider is down is a plausible-but-wrong number.
    snap = simulate_day((_h("AAA", 1, 100.0), _h("BBB", 1, 50.0)), (), AS_OF, fetch_prices=_fetch_from({"AAA": RuntimeError("x"), "BBB": RuntimeError("y")}))
    assert (snap.total_value, snap.prev_total_value, snap.baseline_value) == (None, None, None)
    assert snap.daily_pct is None and snap.vs_baseline_pct is None
    assert snap.prices_as_of is None


def test_partially_degraded_pool_keeps_real_totals():
    table = {"AAA": [_p(100.0, "2026-01-08"), _p(102.0, "2026-01-09")], "BBB": RuntimeError("down")}
    snap = simulate_day((_h("AAA", 1, 100.0), _h("BBB", 1, 50.0)), (), AS_OF, fetch_prices=_fetch_from(table))
    assert snap.total_value == pytest.approx(102.0) and snap.degraded == ("BBB",)


# ── duplicate same-day bars: prev_close must be the prior SESSION ──
def test_duplicate_date_rows_keep_last_and_prev_is_prior_session():
    rows = [_p(100.0, "2026-01-08"), _p(110.0, "2026-01-09T00:00:00-05:00"), _p(110.5, "2026-01-09T15:59:00-05:00")]
    snap = simulate_day((_h("AAA", 1, 110.5),), (), AS_OF, fetch_prices=_fetch_from({"AAA": rows}))
    (p,) = snap.positions
    assert (p.prev_close, p.close, p.close_date) == (100.0, 110.5, date(2026, 1, 9))
    assert "DAILY_MOVE" in p.alerts


def test_two_rows_on_one_date_are_one_close_not_two():
    rows = [_p(110.0, "2026-01-09T00:00:00-05:00"), _p(110.5, "2026-01-09T15:59:00-05:00")]
    snap = simulate_day((_h("AAA", 1, 100.0),), (), AS_OF, fetch_prices=_fetch_from({"AAA": rows}))
    assert snap.degraded == ("AAA",)


# ── freshness: which close the numbers are from must be visible ──
def test_prices_as_of_is_most_common_close_date_and_odd_one_out_is_mixed_date():
    table = {
        "AAA": [_p(100.0, "2026-01-07"), _p(100.0, "2026-01-08")],
        "BBB": [_p(50.0, "2026-01-07"), _p(50.0, "2026-01-08")],
        "CCC": [_p(10.0, "2026-01-08"), _p(10.0, "2026-01-09")],
    }
    holdings = (_h("AAA", 1, 100.0), _h("BBB", 1, 50.0), _h("CCC", 1, 10.0))
    snap = simulate_day(holdings, (), AS_OF, fetch_prices=_fetch_from(table))
    assert snap.prices_as_of == date(2026, 1, 8)
    by = {p.ticker: p for p in snap.positions}
    assert "MIXED_DATE" in by["CCC"].alerts
    assert "MIXED_DATE" not in by["AAA"].alerts and "MIXED_DATE" not in by["BBB"].alerts
    assert ("CCC", "MIXED_DATE") in snap.alerts


def test_uniform_close_dates_raise_no_mixed_date():
    snap = _one(100.0, 101.0)
    assert snap.prices_as_of == date(2026, 1, 9)
    assert "MIXED_DATE" not in snap.positions[0].alerts


# ── intraday guard: a same-day bar before the close settles is not a close ──
_INTRADAY_ROWS = [_p(99.0, "2026-01-07"), _p(100.0, "2026-01-08"), _p(110.0, "2026-01-09")]


@pytest.mark.parametrize(
    "now,close,close_day",
    [
        (datetime(2026, 1, 9, 10, 0, tzinfo=ET), 100.0, date(2026, 1, 8)),  # market open: drop today's bar
        (datetime(2026, 1, 9, 16, 14, tzinfo=ET), 100.0, date(2026, 1, 8)),  # just before settle
        (datetime(2026, 1, 9, 16, 15, tzinfo=ET), 110.0, date(2026, 1, 9)),  # settled
        (datetime(2026, 1, 9, 21, 30, tzinfo=timezone.utc), 110.0, date(2026, 1, 9)),  # 16:30 ET given in UTC
        (datetime(2026, 1, 9, 20, 0, tzinfo=timezone.utc), 100.0, date(2026, 1, 8)),  # 15:00 ET given in UTC
        (datetime(2026, 1, 12, 9, 0, tzinfo=ET), 110.0, date(2026, 1, 9)),  # as_of in the past: untouched
    ],
)
def test_intraday_guard_uses_prior_close_until_1615_et(now, close, close_day):
    snap = simulate_day((_h("AAA", 1, 100.0),), (), AS_OF, fetch_prices=_fetch_from({"AAA": list(_INTRADAY_ROWS)}), now=now)
    (p,) = snap.positions
    assert (p.close, p.close_date) == (close, close_day)
    assert snap.prices_as_of == close_day


# ── unknown cost basis flows through as unknown ──
def test_unknown_cost_estimate_flag_reaches_snapshot():
    holdings = (_h("AAA", 10, 100.0),)  # cost unknown
    fetch = _fetch_from({"AAA": [_p(100.0, "2026-01-08"), _p(110.0, "2026-01-09")]})
    buy = simulate_day(holdings, (Trade(date(2026, 1, 5), "AAA", "buy", 10, 120.0, ""),), AS_OF, fetch_prices=fetch)
    assert buy.positions[0].cost_basis is None and buy.positions[0].unrealized_pnl is None
    assert buy.realized_pnl_estimated is False
    sell = simulate_day(holdings, (Trade(date(2026, 1, 5), "AAA", "sell", 4, 130.0, ""),), AS_OF, fetch_prices=fetch)
    assert sell.realized_pnl == pytest.approx(120.0) and sell.realized_pnl_estimated is True


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
