"""Daily paper-trading snapshot of the bit pool (research-only; never places orders).

``simulate_day`` applies the trades ledger to the baseline holdings, marks every
position to its last two daily closes on or before ``as_of`` (fetched over the
window ``as_of - 14 days .. as_of``), and computes values, weights, percentage moves
and descriptive alerts. Percent fields are percentages (``-6.84`` means -6.84%).

Fail-loud degradation: a position whose fetch raises, or that has fewer than two
usable closes, is marked degraded with a reason, excluded from every total and
weight, and listed in ``PoolSnapshot.degraded`` — never silently valued at zero.

The default ``fetch_prices`` wraps ``src.tools.api.get_prices`` and imports it
lazily, so importing this module stays offline.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, timedelta

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
    total_value: float
    prev_total_value: float
    daily_pct: float | None
    baseline_value: float
    vs_baseline_pct: float | None
    realized_pnl: float
    net_cash_flow: float
    degraded: tuple[str, ...]
    alerts: tuple[tuple[str, str], ...]  # (ticker, alert code)


def _pct(now: float, then: float) -> float | None:
    return (now / then - 1.0) * 100.0 if then > 0 else None


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


def _mark(h: Holding, as_of: date, fetch_prices: FetchPrices) -> PositionSnapshot:
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
    usable = [r for r in dated if r[0] <= as_of]
    if len(usable) < 2:
        return _degraded(h, f"fewer than 2 closes on or before {as_of.isoformat()} (got {len(usable)})")
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


def simulate_day(
    holdings: tuple[Holding, ...],
    trades: tuple[Trade, ...],
    as_of: date,
    *,
    fetch_prices: FetchPrices = fetch_prices_via_provider,
) -> PoolSnapshot:
    """Build the pool snapshot for ``as_of`` (ledger applied first; see module docstring)."""
    ledger = apply_trades(holdings, trades, as_of)
    marked = [_mark(h, as_of, fetch_prices) for h in ledger.holdings]
    live = [p for p in marked if p.degraded_reason is None]

    total = sum((p.market_value for p in live), 0.0)
    prev_total = sum((p.shares * p.prev_close for p in live), 0.0)
    baseline_total = sum((p.shares * p.baseline_price for p in live), 0.0)
    positions = tuple(replace(p, weight=p.market_value / total) if p.degraded_reason is None and total > 0 else p for p in marked)
    return PoolSnapshot(
        as_of=as_of,
        positions=positions,
        total_value=total,
        prev_total_value=prev_total,
        daily_pct=_pct(total, prev_total),
        baseline_value=baseline_total,
        vs_baseline_pct=_pct(total, baseline_total),
        realized_pnl=ledger.realized_pnl,
        net_cash_flow=ledger.net_cash_flow,
        degraded=tuple(p.ticker for p in marked if p.degraded_reason is not None),
        alerts=tuple((p.ticker, a) for p in positions for a in p.alerts),
    )
