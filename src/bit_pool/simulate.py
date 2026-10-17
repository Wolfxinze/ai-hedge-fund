"""Daily paper-trading snapshot of the bit pool (research-only; never places orders).

``simulate_day`` applies the trades ledger to the baseline holdings, marks every
position to its last two daily closes (sessions) on or before ``as_of`` (fetched over
the window ``as_of - 14 days .. as_of``), and computes values, weights, percentage
moves and descriptive alerts. Percent fields are percentages (``-6.84`` means -6.84%).

Price rows are reduced to one per calendar date (the last row for that date), so
``prev_close`` is always the prior session. Intraday guard: before 16:15 ET on the
current ET date, that date's bar is still forming and is ignored (the prior close is
used). ``PoolSnapshot.prices_as_of`` is the most common close date across priced
positions (ties → the later date); a position on a different date gets
``MIXED_DATE``.

Fail-loud degradation: a position whose fetch raises, or that has fewer than two
usable closes, is marked degraded with a reason, excluded from every total and
weight, and listed in ``PoolSnapshot.degraded`` — never silently valued at zero. When
every position is degraded the pool totals are ``None``, not 0.0.

The default ``fetch_prices`` wraps ``src.tools.api.get_prices`` and imports it
lazily, so importing this module stays offline.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from src.bit_pool.holdings import Holding, Trade, apply_trades
from src.data.models import Price

# Descriptive alert thresholds (percent / calendar days). Research flags, not signals.
DAILY_MOVE_PCT = 5.0
BASELINE_WARN_PCT = -10.0
BASELINE_HEAVY_PCT = -20.0
BASELINE_REVIEW_PCT = 20.0
STALE_DAYS = 4

# Calendar-day lookback: covers a long weekend plus a holiday with room to spare.
_WINDOW_DAYS = 14
# Float slack so a move of exactly 5.00% (e.g. 95/100) is not lost to rounding.
_EPS = 1e-9
# US equities close at 16:00 ET; give the provider 15 minutes to publish the final bar.
MARKET_TZ = ZoneInfo("America/New_York")
CLOSE_SETTLED_AT = time(16, 15)

FetchPrices = Callable[[str, str, str], list[Price]]


def fetch_prices_via_provider(ticker: str, start_iso: str, end_iso: str) -> list[Price]:
    """Default fetcher: ``src.tools.api.get_prices``, imported at call time (offline import)."""
    from src.tools import api

    return api.get_prices(ticker, start_iso, end_iso)


@dataclass(frozen=True)
class PositionSnapshot:
    ticker: str
    shares: float
    close: float | None
    prev_close: float | None
    close_date: date | None
    daily_pct: float | None
    market_value: float | None
    weight: float | None
    baseline_price: float
    vs_baseline_pct: float | None
    cost_basis: float | None
    unrealized_pnl: float | None  # None when cost_basis is unknown (never fabricated)
    alerts: tuple[str, ...]
    degraded_reason: str | None


@dataclass(frozen=True)
class PoolSnapshot:
    as_of: date
    positions: tuple[PositionSnapshot, ...]
    total_value: float | None  # None when every position is degraded (never a fake 0.0)
    prev_total_value: float | None
    daily_pct: float | None
    baseline_value: float | None
    vs_baseline_pct: float | None
    realized_pnl: float
    realized_pnl_estimated: bool  # True if a sell realized against baseline_price (cost unknown)
    net_cash_flow: float
    prices_as_of: date | None  # most common close date among priced positions
    degraded: tuple[str, ...]
    alerts: tuple[tuple[str, str], ...]  # (ticker, alert code)


def _pct(now: float | None, then: float | None) -> float | None:
    return (now / then - 1.0) * 100.0 if now is not None and then is not None and then > 0 else None


def _alerts(daily_pct: float, vs_baseline_pct: float, close_date: date, as_of: date) -> tuple[str, ...]:
    out: list[str] = []
    if abs(daily_pct) >= DAILY_MOVE_PCT - _EPS:
        out.append("DAILY_MOVE")
    if vs_baseline_pct <= BASELINE_HEAVY_PCT + _EPS:
        out.append("BASELINE_HEAVY")
    elif vs_baseline_pct <= BASELINE_WARN_PCT + _EPS:
        out.append("BASELINE_WARN")
    elif vs_baseline_pct >= BASELINE_REVIEW_PCT - _EPS:
        out.append("BASELINE_REVIEW")
    if (as_of - close_date).days > STALE_DAYS:
        out.append("STALE")
    return tuple(out)


def _degraded(h: Holding, reason: str) -> PositionSnapshot:
    return PositionSnapshot(h.ticker, h.shares, None, None, None, None, None, None, h.baseline_price, None, h.cost_basis, None, (), reason)


def _last_complete_day(as_of: date, now: datetime) -> date:
    """Latest date whose bar is a settled close: before 16:15 ET, today's (ET) bar is not."""
    now_et = now.astimezone(MARKET_TZ) if now.tzinfo is not None else now.replace(tzinfo=MARKET_TZ)
    today_et = now_et.date()
    if as_of >= today_et and now_et.time() < CLOSE_SETTLED_AT:
        return today_et - timedelta(days=1)
    return as_of


def _mark(h: Holding, as_of: date, cutoff: date, fetch_prices: FetchPrices) -> PositionSnapshot:
    """Mark one holding to market; any failure becomes a degraded snapshot (with reason)."""
    start = (as_of - timedelta(days=_WINDOW_DAYS)).isoformat()
    try:
        prices = fetch_prices(h.ticker, start, as_of.isoformat())
    except Exception as exc:  # noqa: BLE001 — degrade the position visibly, never crash the run
        return _degraded(h, f"price fetch failed: {type(exc).__name__}: {exc}")
    try:
        dated = sorted(((date.fromisoformat(p.time[:10]), p.close) for p in prices), key=lambda r: r[0])
    except (ValueError, TypeError, AttributeError) as exc:
        return _degraded(h, f"unparseable price rows: {type(exc).__name__}: {exc}")
    # One close per session: the LAST row for a date wins (stable sort keeps provider order).
    by_day: dict[date, float] = {}
    for day, close_value in dated:
        if day <= cutoff:
            by_day[day] = close_value
    usable = list(by_day.items())
    if len(usable) < 2:
        return _degraded(h, f"fewer than 2 closes on or before {cutoff.isoformat()} (got {len(usable)})")
    (_, prev_close), (close_date, close) = usable[-2], usable[-1]
    if not (close > 0 and prev_close > 0):
        return _degraded(h, f"non-positive close (close={close}, prev_close={prev_close})")
    daily_pct = _pct(close, prev_close)
    vs_baseline_pct = _pct(close, h.baseline_price)
    unrealized = (close - h.cost_basis) * h.shares if h.cost_basis is not None else None
    return PositionSnapshot(
        ticker=h.ticker,
        shares=h.shares,
        close=close,
        prev_close=prev_close,
        close_date=close_date,
        daily_pct=daily_pct,
        market_value=h.shares * close,
        weight=None,  # filled once the pool total is known
        baseline_price=h.baseline_price,
        vs_baseline_pct=vs_baseline_pct,
        cost_basis=h.cost_basis,
        unrealized_pnl=unrealized,
        alerts=_alerts(daily_pct, vs_baseline_pct, close_date, as_of),
        degraded_reason=None,
    )


def _prices_as_of(live: list[PositionSnapshot]) -> date | None:
    """Most common close date among priced positions; ties go to the later date."""
    counts = Counter(p.close_date for p in live)
    return max(counts, key=lambda d: (counts[d], d)) if counts else None


def _finish(p: PositionSnapshot, total: float | None, prices_as_of: date | None) -> PositionSnapshot:
    """Fill weight and the MIXED_DATE alert once pool-level facts are known."""
    if p.degraded_reason is not None:
        return p
    alerts = p.alerts + (("MIXED_DATE",) if p.close_date != prices_as_of else ())
    return replace(p, weight=p.market_value / total if total else None, alerts=alerts)


def simulate_day(
    holdings: tuple[Holding, ...],
    trades: tuple[Trade, ...],
    as_of: date,
    *,
    fetch_prices: FetchPrices = fetch_prices_via_provider,
    now: datetime | None = None,
) -> PoolSnapshot:
    """Build the pool snapshot for ``as_of`` (ledger applied first; see module docstring).

    ``now`` (default: the current time in ET) drives the intraday guard; inject it in tests.
    """
    ledger = apply_trades(holdings, trades, as_of)
    cutoff = _last_complete_day(as_of, now if now is not None else datetime.now(MARKET_TZ))
    marked = [_mark(h, as_of, cutoff, fetch_prices) for h in ledger.holdings]
    live = [p for p in marked if p.degraded_reason is None]

    # Every position degraded → no pool value is known; None, never a fabricated 0.0.
    all_degraded = bool(marked) and not live
    total = None if all_degraded else sum((p.market_value for p in live), 0.0)
    prev_total = None if all_degraded else sum((p.shares * p.prev_close for p in live), 0.0)
    baseline_total = None if all_degraded else sum((p.shares * p.baseline_price for p in live), 0.0)
    prices_as_of = _prices_as_of(live)
    positions = tuple(_finish(p, total, prices_as_of) for p in marked)
    return PoolSnapshot(
        as_of=as_of,
        positions=positions,
        total_value=total,
        prev_total_value=prev_total,
        daily_pct=_pct(total, prev_total),
        baseline_value=baseline_total,
        vs_baseline_pct=_pct(total, baseline_total),
        realized_pnl=ledger.realized_pnl,
        realized_pnl_estimated=ledger.realized_pnl_estimated,
        net_cash_flow=ledger.net_cash_flow,
        prices_as_of=prices_as_of,
        degraded=tuple(p.ticker for p in marked if p.degraded_reason is not None),
        alerts=tuple((p.ticker, a) for p in positions for a in p.alerts),
    )
