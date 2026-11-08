"""Pool ranking from supplied bars and analyst rows. No network. Not a performance test."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from src.bit_pool.pool_select import (
    CASH_RESERVE,
    MAX_NAME_WEIGHT,
    MAX_SECTOR_WEIGHT,
    Quote,
    SeedName,
    last_completed_session,
    propose_allocation,
    quote_from_bars,
    rank_universe,
)

SESSION = date(2026, 11, 6)
ET = ZoneInfo("America/New_York")


def _name(ticker, sector="Technology", platforms=("ai",)):
    return SeedName(ticker, ticker, "NASDAQ", sector, "Software", platforms)


def _quote(close=100.0, volume=1_000_000.0, day=SESSION):
    return Quote(close, day, volume, close * volume, 20)


def _signal(signal, confidence=80):
    return {"signal": signal, "confidence": confidence, "reasoning": {"profitability_signal": {"details": f"{signal} evidence"}}}


def _analysts(tickers, signal="bullish"):
    return {ticker: {"fundamentals_analyst": _signal(signal), "valuation_analyst": _signal(signal), "technical_analyst": _signal(signal), "growth_analyst": _signal(signal)} for ticker in tickers}


def test_calendar_uses_real_sessions_and_refuses_a_later_date():
    sessions = [date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6)]  # Thursday holiday gap is just a missing date
    sunday_evening = datetime(2026, 11, 8, 18, 43, tzinfo=ET)
    assert last_completed_session(sessions, now=sunday_evening) == SESSION
    assert last_completed_session(sessions, now=sunday_evening, requested=date(2026, 11, 5)) == date(2026, 11, 5)
    with pytest.raises(ValueError, match="refusing to invent"):
        last_completed_session(sessions, now=sunday_evening, requested=date(2026, 11, 9))
    # Friday 2026-11-06 is today and the cash session has not settled.
    friday_morning = datetime(2026, 11, 6, 10, 0, tzinfo=ET)
    assert last_completed_session(sessions, now=friday_morning) == date(2026, 11, 5)
    friday_settled = datetime(2026, 11, 6, 16, 15, tzinfo=ET)
    assert last_completed_session(sessions, now=friday_settled) == SESSION
    # A bar after the clock date is not a completed session.
    with pytest.raises(ValueError, match="refusing to invent"):
        last_completed_session(sessions + [date(2026, 11, 9)], now=sunday_evening, requested=date(2026, 11, 9))


def test_missing_value_evidence_is_excluded_not_scored_zero():
    names = (_name("AAA"),)
    ranked = rank_universe(names, {"AAA": _quote()}, {"AAA": {"technical_analyst": _signal("bullish")}}, session=SESSION)
    assert ranked[0]["pool"] == "excluded"
    assert ranked[0]["composite"] is None
    assert "valuation_analyst" in ranked[0]["coverage_gaps"]


def test_stale_close_is_excluded_from_orders():
    names = (_name("AAA"),)
    stale = Quote(100.0, date(2026, 11, 5), 1_000_000.0, 100_000_000.0, 20)
    ranked = rank_universe(names, {"AAA": stale}, _analysts(["AAA"]), session=SESSION)
    assert ranked[0]["pool"] == "excluded"
    assert ranked[0]["close_date"] == "2026-11-05"
    allocation = propose_allocation(ranked, cash=50_000.0)
    assert allocation["orders"] == []
    assert allocation["cash_remaining"] == pytest.approx(50_000.0)


def test_allocation_respects_cash_reserve_name_cap_and_sector_cap():
    names = tuple(_name(ticker, sector="Technology") for ticker in ("AAA", "BBB", "CCC", "DDD"))
    quotes = {name.ticker: _quote(100.0) for name in names}
    ranked = rank_universe(names, quotes, _analysts([name.ticker for name in names]), session=SESSION)
    assert all(row["pool"] == "candidate" for row in ranked)
    allocation = propose_allocation(ranked, cash=50_000.0)
    spent = sum(order["estimated_cost"] for order in allocation["orders"])
    assert spent <= 50_000.0 * (1.0 - CASH_RESERVE) + 0.05
    assert allocation["cash_remaining"] == pytest.approx(50_000.0 - spent)
    assert allocation["cash_remaining"] >= 50_000.0 * CASH_RESERVE - 0.05
    assert spent + allocation["cash_remaining"] == pytest.approx(50_000.0)
    for order in allocation["orders"]:
        assert order["weight_of_cash"] <= MAX_NAME_WEIGHT + 1e-9
        assert order["shares"] >= 1
        assert order["status"] == "pending_next_session"
        assert order["holding"] is False
    sector_spent = sum(order["estimated_cost"] for order in allocation["orders"])
    assert sector_spent / 50_000.0 <= MAX_SECTOR_WEIGHT + 1e-9
    # 12% * 3 = 36% would break the 35% sector cap, so the fourth name cannot join at a full sleeve
    # and the booked sector weight stays inside the cap. Four names at 12% would be 48%.
    assert len(allocation["orders"]) <= 3


def test_sentiment_without_dated_observations_is_a_gap():
    names = (_name("AAA"),)
    empty_sentiment = {"signal": "neutral", "confidence": 0, "reasoning": {"insider_trading": {"metrics": {"total_trades": 0}}, "news_sentiment": {"metrics": {"bullish_articles": 0, "bearish_articles": 0, "total_articles": 5}}}}
    analysts = {"AAA": {"fundamentals_analyst": _signal("bearish", 90), "valuation_analyst": _signal("bearish", 90), "sentiment_analyst": empty_sentiment}}
    ranked = rank_universe(names, {"AAA": _quote(dollar := 100.0)}, analysts, session=SESSION)
    del dollar
    assert "sentiment_analyst" in ranked[0]["coverage_gaps"]
    assert ranked[0]["pool"] == "avoid"


def test_zero_confidence_and_extreme_gap_are_not_auto_sized():
    names = (_name("AAA"), _name("BBB"))
    huge = {"signal": "bullish", "confidence": 100, "reasoning": {"owner_earnings_analysis": {"details": "owner_earnings_analysis: Gap: 872.9%, Weight: 35%"}}}
    quiet = {"signal": "neutral", "confidence": 0, "reasoning": {}}
    analysts = {
        "AAA": {"fundamentals_analyst": _signal("bullish"), "valuation_analyst": huge, "technical_analyst": _signal("bullish")},
        "BBB": {"fundamentals_analyst": _signal("bullish"), "valuation_analyst": quiet},
    }
    ranked = rank_universe(names, {name.ticker: _quote() for name in names}, analysts, session=SESSION)
    by_ticker = {row["ticker"]: row for row in ranked}
    assert by_ticker["AAA"]["pool"] == "watch"
    assert any("872.9" in flag for flag in by_ticker["AAA"]["uncertainty"])
    assert by_ticker["BBB"]["pool"] == "candidate"
    assert "valuation_analyst" in by_ticker["BBB"]["coverage_gaps"]
    orders = propose_allocation(ranked, cash=50_000.0)["orders"]
    assert [order["ticker"] for order in orders] == ["BBB"]


def test_dust_residual_stays_cash():
    names = tuple(_name(ticker, sector="Technology" if ticker != "ZZZ" else "Energy") for ticker in ("AAA", "BBB", "ZZZ"))
    quotes = {"AAA": _quote(100.0), "BBB": _quote(100.0), "ZZZ": _quote(5.0)}
    ranked = rank_universe(names, quotes, _analysts(["AAA", "BBB", "ZZZ"]), session=SESSION)
    allocation = propose_allocation(ranked, cash=50_000.0)
    assert all(order["estimated_cost"] >= 50_000.0 * 0.01 - 1.0 for order in allocation["orders"])
    assert allocation["cash_remaining"] + sum(order["estimated_cost"] for order in allocation["orders"]) == pytest.approx(50_000.0)


def test_quote_median_uses_only_bars_on_or_before_the_session():
    bars = [
        (date(2026, 11, 5), 10.0, 100.0),
        (date(2026, 11, 6), 20.0, 100.0),
        (date(2026, 11, 9), 500.0, 100.0),
    ]
    quote = quote_from_bars(bars, SESSION)
    assert quote is not None
    assert quote.close == 20.0
    assert quote.close_date == SESSION
    assert quote.median_dollar_volume == pytest.approx(1500.0)
