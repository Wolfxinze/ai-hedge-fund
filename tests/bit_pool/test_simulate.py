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
    POOL_ALERT_TICKER,
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


# ── STALE is measured from the last session the intraday guard allows, not as_of ──
def test_holiday_monday_with_guard_active_is_not_stale():
    # as_of Wed 2026-01-21, but it is Tue 03:00 ET: the guard caps closes at Mon 01-19,
    # which is a market holiday (MLK), so Fri 01-16 IS the latest possible close —
    # 3 days before the cutoff, not 5 days before as_of. Flagging it STALE (exit 2)
    # would cry wolf on every post-holiday morning run.
    rows = [_p(100.0, "2026-01-15"), _p(101.0, "2026-01-16")]
    now = datetime(2026, 1, 20, 3, 0, tzinfo=ET)
    snap = simulate_day((_h("AAA", 1, 101.0),), (), date(2026, 1, 21), fetch_prices=_fetch_from({"AAA": rows}), now=now)
    (p,) = snap.positions
    assert p.close_date == date(2026, 1, 16)
    assert "STALE" not in p.alerts


def test_stale_still_fires_relative_to_the_guard_cutoff():
    # Same guard (cutoff Mon 01-19); a close 5 days before the cutoff is genuinely stale.
    rows = [_p(100.0, "2026-01-13"), _p(100.0, "2026-01-14")]
    now = datetime(2026, 1, 20, 3, 0, tzinfo=ET)
    snap = simulate_day((_h("AAA", 1, 100.0),), (), date(2026, 1, 21), fetch_prices=_fetch_from({"AAA": rows}), now=now)
    assert "STALE" in snap.positions[0].alerts


# ── pinned guards: a broken price never leaks into another position's numbers ──
@pytest.mark.parametrize(("prev_close", "close"), [(100.0, 0.0), (0.0, 100.0), (100.0, float("nan")), (100.0, -1.0)], ids=["zero-close", "zero-prev", "nan-close", "negative-close"])
def test_non_positive_or_nan_close_degrades_only_that_position(prev_close, close):
    # Already-correct behaviour pinned. Without the guard a 0/-1 close would be summed
    # into the pool (and given a weight), a NaN would poison every total, and a 0 prev
    # close would crash the alert math.
    table = {"AAA": [_p(prev_close, "2026-01-08"), _p(close, "2026-01-09")], "BBB": [_p(50.0, "2026-01-08"), _p(55.0, "2026-01-09")]}
    snap = simulate_day((_h("AAA", 10, 100.0), _h("BBB", 2, 50.0)), (), AS_OF, fetch_prices=_fetch_from(table))
    by = {p.ticker: p for p in snap.positions}
    assert snap.degraded == ("AAA",)
    assert "non-positive close" in by["AAA"].degraded_reason
    assert by["AAA"].market_value is None and by["AAA"].weight is None
    assert by["BBB"].market_value == pytest.approx(110.0) and by["BBB"].weight == pytest.approx(1.0)
    assert (snap.total_value, snap.prev_total_value, snap.baseline_value) == (pytest.approx(110.0), pytest.approx(100.0), pytest.approx(100.0))


def test_unparseable_price_row_degrades_with_reason():
    # Already-correct behaviour pinned: an empty time string must not be guessed at.
    table = {"AAA": [_p(100.0, "2026-01-08"), _p(101.0, "")], "BBB": [_p(50.0, "2026-01-08"), _p(55.0, "2026-01-09")]}
    snap = simulate_day((_h("AAA", 1, 100.0), _h("BBB", 1, 50.0)), (), AS_OF, fetch_prices=_fetch_from(table))
    by = {p.ticker: p for p in snap.positions}
    assert snap.degraded == ("AAA",)
    assert "unparseable price rows" in by["AAA"].degraded_reason
    assert snap.total_value == pytest.approx(55.0)


# ── equity = positions + cash, measured against the ORIGINAL holdings' capital ──
# The Muse book is self-funded from the seeded holdings: a sell turns shares into cash,
# so a positions-only total would read a sell as a loss. Equity must not move when a
# position is sold at the very close it is marked to.
def _flat(close: float = 110.0) -> list[Price]:
    return [_p(100.0, "2026-01-08"), _p(close, "2026-01-09")]


def test_selling_at_the_close_moves_value_to_cash_and_leaves_equity_unchanged():
    holdings = (_h("AAA", 10, 100.0, cost=100.0),)
    fetch = _fetch_from({"AAA": _flat(110.0)})
    before = simulate_day(holdings, (), AS_OF, fetch_prices=fetch)
    after = simulate_day(holdings, (Trade(AS_OF, "AAA", "sell", 5, 110.0, ""),), AS_OF, fetch_prices=fetch)

    assert (before.total_value, before.cash, before.equity) == (pytest.approx(1100.0), 0.0, pytest.approx(1100.0))
    assert after.total_value == pytest.approx(550.0)  # positions-only total drops...
    assert after.cash == pytest.approx(550.0)  # ...by exactly the proceeds now held as cash
    assert after.equity == pytest.approx(before.equity)  # selling at the close changes nothing
    assert after.vs_baseline_pct == before.vs_baseline_pct  # existing semantics untouched
    assert before.initial_capital == after.initial_capital == pytest.approx(1000.0)
    assert after.equity_vs_initial_pct == pytest.approx(10.0)


def test_buy_funded_by_an_earlier_sell_keeps_equity_and_cash_consistent():
    holdings = (_h("AAA", 10, 100.0), _h("BBB", 5, 200.0))
    trades = (Trade(date(2026, 1, 5), "AAA", "sell", 4, 110.0, ""), Trade(date(2026, 1, 6), "BBB", "buy", 2, 200.0, ""))
    fetch = _fetch_from({"AAA": _flat(110.0), "BBB": _flat(210.0)})
    snap = simulate_day(holdings, trades, AS_OF, fetch_prices=fetch)

    assert snap.total_value == pytest.approx(6 * 110.0 + 7 * 210.0)
    assert snap.cash == pytest.approx(440.0 - 400.0) == snap.net_cash_flow
    assert snap.equity == pytest.approx(snap.total_value + snap.cash) == pytest.approx(2170.0)
    assert snap.equity_vs_initial_pct == pytest.approx((2170.0 / 2000.0 - 1) * 100)
    assert not any(a == "NEGATIVE_CASH" for _, a in snap.alerts)


def test_initial_capital_is_the_original_holdings_and_constant_across_trades():
    holdings = (_h("AAA", 10, 100.0), _h("BBB", 5, 200.0))
    fetch = _fetch_from({"AAA": _flat(), "BBB": _flat(), "NEW": _flat()})
    untouched = simulate_day(holdings, (), AS_OF, fetch_prices=fetch)
    # Sell AAA out entirely, then open a new ticker with the proceeds: neither changes capital.
    trades = (Trade(date(2026, 1, 5), "AAA", "sell", 10, 120.0, ""), Trade(date(2026, 1, 6), "NEW", "buy", 5, 100.0, ""))
    traded = simulate_day(holdings, trades, AS_OF, fetch_prices=fetch)
    assert untouched.initial_capital == traded.initial_capital == pytest.approx(10 * 100.0 + 5 * 200.0)
    assert [p.ticker for p in traded.positions] == ["BBB", "NEW"]


def test_buy_without_funds_raises_negative_cash_pool_alert():
    holdings = (_h("AAA", 10, 100.0),)
    fetch = _fetch_from({"AAA": _flat()})
    snap = simulate_day(holdings, (Trade(date(2026, 1, 5), "AAA", "buy", 1, 100.0, ""),), AS_OF, fetch_prices=fetch)
    assert snap.cash == pytest.approx(-100.0)
    assert (POOL_ALERT_TICKER, "NEGATIVE_CASH") in snap.alerts
    assert POOL_ALERT_TICKER == "(pool)"  # cannot collide with a real ticker such as POOL


def test_negative_cash_ignores_sub_half_cent_rounding():
    holdings = (_h("AAA", 10, 100.0),)
    fetch = _fetch_from({"AAA": _flat()})
    snap = simulate_day(holdings, (Trade(date(2026, 1, 5), "AAA", "buy", 1, 0.004, ""),), AS_OF, fetch_prices=fetch)
    assert snap.cash == pytest.approx(-0.004)
    assert not any(a == "NEGATIVE_CASH" for _, a in snap.alerts)


def test_negative_cash_fires_even_when_every_position_is_degraded():
    holdings = (_h("AAA", 10, 100.0),)
    fetch = _fetch_from({"AAA": RuntimeError("down")})
    snap = simulate_day(holdings, (Trade(date(2026, 1, 5), "AAA", "buy", 1, 100.0, ""),), AS_OF, fetch_prices=fetch)
    assert (snap.equity, snap.equity_vs_initial_pct) == (None, None)
    assert snap.cash == pytest.approx(-100.0) and snap.initial_capital == pytest.approx(1000.0)
    assert (POOL_ALERT_TICKER, "NEGATIVE_CASH") in snap.alerts


def test_all_degraded_pool_has_no_equity():
    snap = simulate_day((_h("AAA", 1, 100.0),), (), AS_OF, fetch_prices=_fetch_from({"AAA": RuntimeError("down")}))
    assert (snap.total_value, snap.equity, snap.equity_vs_initial_pct) == (None, None, None)
    assert snap.cash == 0.0


def _weights_pool(max_weight_pct):
    # AAA 100 / BBB 300 → weights of equity 25% / 75%.
    holdings = (_h("AAA", 1, 100.0), _h("BBB", 3, 100.0))
    fetch = _fetch_from({"AAA": _flat(100.0), "BBB": _flat(100.0)})
    return simulate_day(holdings, (), AS_OF, fetch_prices=fetch, max_weight_pct=max_weight_pct)


def _over(snap):
    return [t for t, a in snap.alerts if a == "OVER_WEIGHT"]


def test_over_weight_fires_above_the_limit_not_at_it():
    assert _over(_weights_pool(75.0)) == []  # BBB is exactly 75% → no alert
    assert _over(_weights_pool(74.9)) == ["BBB"]
    assert _over(_weights_pool(24.9)) == ["AAA", "BBB"]
    assert "OVER_WEIGHT" in next(p for p in _weights_pool(74.9).positions if p.ticker == "BBB").alerts


def test_over_weight_is_off_without_a_limit():
    assert _over(_weights_pool(None)) == []


def test_over_weight_is_measured_against_equity_not_positions_only():
    # Sell 2 BBB at the close: positions AAA 100 / BBB 100 (50% each of positions), cash 200,
    # equity 400 → 25% each of equity. A 30% limit must not fire.
    holdings = (_h("AAA", 1, 100.0), _h("BBB", 3, 100.0))
    fetch = _fetch_from({"AAA": _flat(100.0), "BBB": _flat(100.0)})
    snap = simulate_day(holdings, (Trade(AS_OF, "BBB", "sell", 2, 100.0, ""),), AS_OF, fetch_prices=fetch, max_weight_pct=30.0)
    assert snap.equity == pytest.approx(400.0)
    assert _over(snap) == []


@pytest.mark.parametrize("bad", [0.0, -5.0, 101.0, float("nan"), float("inf")])
def test_invalid_max_weight_pct_raises(bad):
    with pytest.raises(ValueError, match="max_weight_pct"):
        _weights_pool(bad)


def test_partial_pool_has_equity_but_no_equity_return():
    # initial_capital covers AAA AND BBB (2000) while equity covers only priced AAA (1100):
    # the ratio would report a fake -45% loss, so the return is unknown (None) on a PARTIAL day.
    holdings = (_h("AAA", 10, 100.0), _h("BBB", 5, 200.0))
    snap = simulate_day(holdings, (), AS_OF, fetch_prices=_fetch_from({"AAA": _flat(110.0), "BBB": RuntimeError("down")}))
    assert snap.degraded == ("BBB",)
    assert snap.equity == pytest.approx(1100.0) and snap.initial_capital == pytest.approx(2000.0)
    assert snap.equity_vs_initial_pct is None


def test_weight_of_equity_differs_from_positions_weight_once_cash_exists():
    holdings = (_h("AAA", 1, 100.0), _h("BBB", 3, 100.0))
    fetch = _fetch_from({"AAA": _flat(100.0), "BBB": _flat(100.0)})
    snap = simulate_day(holdings, (Trade(AS_OF, "BBB", "sell", 2, 100.0, ""),), AS_OF, fetch_prices=fetch)
    by = {p.ticker: p for p in snap.positions}
    # positions AAA 100 / BBB 100 (50% each); cash 200 → equity 400 (25% each).
    assert (by["AAA"].weight, by["BBB"].weight) == (pytest.approx(0.5), pytest.approx(0.5))
    assert (by["AAA"].weight_of_equity, by["BBB"].weight_of_equity) == (pytest.approx(0.25), pytest.approx(0.25))


def test_over_weight_agrees_with_weight_of_equity():
    snap = _weights_pool(74.9)
    for p in snap.positions:
        assert ("OVER_WEIGHT" in p.alerts) == (p.weight_of_equity * 100 > 74.9)
    assert {p.ticker: p.weight_of_equity for p in snap.positions} == {"AAA": pytest.approx(0.25), "BBB": pytest.approx(0.75)}


def test_weight_of_equity_is_none_for_degraded_or_non_positive_equity():
    partial = simulate_day((_h("AAA", 1, 100.0), _h("BBB", 1, 100.0)), (), AS_OF, fetch_prices=_fetch_from({"AAA": _flat(100.0), "BBB": RuntimeError("down")}))
    assert {p.ticker: p.weight_of_equity for p in partial.positions} == {"AAA": pytest.approx(1.0), "BBB": None}
    # Buy 2 AAA at 1000 with no cash: positions 300, cash -2000 → equity -1700 (no meaningful share).
    broke = simulate_day((_h("AAA", 1, 100.0),), (Trade(date(2026, 1, 5), "AAA", "buy", 2, 1000.0, ""),), AS_OF, fetch_prices=_fetch_from({"AAA": _flat(100.0)}), max_weight_pct=50.0)
    assert broke.equity == pytest.approx(-1700.0)
    assert broke.positions[0].weight_of_equity is None
    assert "OVER_WEIGHT" in broke.positions[0].alerts  # deficit book still flags every position
