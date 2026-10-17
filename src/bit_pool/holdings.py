"""Holdings CSV + trades ledger for the bit pool (research-only).

The holdings file holds the owner's real positions, so unlike the observing-pools
seed loader (which rejects-and-continues) every malformed row here fails LOUD with a
``ValueError`` naming the line/ticker: a silently skipped position would understate
the pool and corrupt every percentage derived from it.

Ledger semantics (applied after the baseline, in date order, trades dated <= as_of):
a buy adds shares and moves the weighted-average per-share cost (a buy of a new
ticker opens a position with baseline_price = cost = trade price); a sell reduces
shares and realizes ``(price - per-share cost) * shares``, where per-share cost is
``cost_basis`` if present else ``baseline_price``. Overselling raises; a position
sold to zero drops out. Everything returns new frozen objects — inputs never mutate.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

HOLDINGS_COLUMNS = ("ticker", "shares", "baseline_price", "baseline_date", "cost_basis")
TRADES_COLUMNS = ("date", "ticker", "side", "shares", "price", "note")
DEFAULT_HOLDINGS_PATH = Path("data/pools/bit_pool.csv")
DEFAULT_TRADES_PATH = Path("data/pools/bit_pool_trades.csv")
HOLDINGS_PATH_ENV = "BIT_POOL_PATH"
SIDES = ("buy", "sell")

# Same conservative whitelist as observing_pools.universe: leading letter, then
# letters/digits/.-, up to 10 chars.
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
# Share counts are floats (fractional shares); treat a residual below this as zero.
_ZERO_SHARES = 1e-9


@dataclass(frozen=True)
class Holding:
    ticker: str
    shares: float
    baseline_price: float
    baseline_date: date
    cost_basis: float | None  # per-share cost; None when unknown


@dataclass(frozen=True)
class Trade:
    date: date
    ticker: str
    side: str  # "buy" | "sell"
    shares: float
    price: float
    note: str


@dataclass(frozen=True)
class LedgerResult:
    holdings: tuple[Holding, ...]
    realized_pnl: float
    net_cash_flow: float  # sum(sell proceeds) - sum(buy cost)


def resolve_holdings_path(cli_value: str | None, env: Mapping[str, str]) -> Path:
    """``--holdings`` beats ``$BIT_POOL_PATH`` beats the default path."""
    if cli_value:
        return Path(cli_value)
    if env.get(HOLDINGS_PATH_ENV):
        return Path(env[HOLDINGS_PATH_ENV])
    return DEFAULT_HOLDINGS_PATH


def _read_rows(path: Path, expected: tuple[str, ...], kind: str) -> Iterable[tuple[int, dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = [c.strip() for c in (reader.fieldnames or [])]
        missing = [c for c in expected if c not in header]
        if missing:
            raise ValueError(f"{kind} CSV {path} missing columns: {missing} (expected header: {','.join(expected)})")
        reader.fieldnames = header
        rows = [(lineno, raw) for lineno, raw in enumerate(reader, start=2)]
    return rows


def _ticker(raw: str | None, lineno: int, kind: str) -> str:
    ticker = (raw or "").strip().upper()
    if not ticker:
        raise ValueError(f"{kind} line {lineno}: empty ticker")
    if not _TICKER_RE.match(ticker):
        raise ValueError(f"{kind} line {lineno}: invalid ticker format {ticker!r}")
    return ticker


def _positive(raw: str | None, field_name: str, where: str) -> float:
    try:
        value = float((raw or "").strip())
    except ValueError:
        raise ValueError(f"{where}: {field_name} {raw!r} is not a number") from None
    if not value > 0:  # also rejects NaN
        raise ValueError(f"{where}: {field_name} must be > 0 (got {raw!r})")
    return value


def _iso_date(raw: str | None, field_name: str, where: str) -> date:
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        raise ValueError(f"{where}: {field_name} {raw!r} is not a YYYY-MM-DD date") from None


def load_holdings(path: str | Path) -> tuple[Holding, ...]:
    """Parse + validate the holdings CSV. Any bad row raises ``ValueError``."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"holdings CSV not found: {path} (copy data/pools/bit_pool.example.csv to start)")
    out: list[Holding] = []
    seen: set[str] = set()
    for lineno, raw in _read_rows(path, HOLDINGS_COLUMNS, "holdings"):
        ticker = _ticker(raw.get("ticker"), lineno, "holdings")
        where = f"holdings line {lineno} ({ticker})"
        if ticker in seen:
            raise ValueError(f"{where}: duplicate ticker {ticker}")
        seen.add(ticker)
        cost_raw = (raw.get("cost_basis") or "").strip()
        out.append(
            Holding(
                ticker=ticker,
                shares=_positive(raw.get("shares"), "shares", where),
                baseline_price=_positive(raw.get("baseline_price"), "baseline_price", where),
                baseline_date=_iso_date(raw.get("baseline_date"), "baseline_date", where),
                cost_basis=_positive(cost_raw, "cost_basis", where) if cost_raw else None,
            )
        )
    return tuple(out)


def load_trades(path: str | Path) -> tuple[Trade, ...]:
    """Parse + validate the trades ledger. A missing file means no trades."""
    path = Path(path)
    if not path.exists():
        return ()
    out: list[Trade] = []
    for lineno, raw in _read_rows(path, TRADES_COLUMNS, "trades"):
        where = f"trades line {lineno}"
        ticker = _ticker(raw.get("ticker"), lineno, "trades")
        side = (raw.get("side") or "").strip().lower()
        if side not in SIDES:
            raise ValueError(f"{where} ({ticker}): side must be one of {SIDES} (got {raw.get('side')!r})")
        out.append(
            Trade(
                date=_iso_date(raw.get("date"), "date", where),
                ticker=ticker,
                side=side,
                shares=_positive(raw.get("shares"), "shares", where),
                price=_positive(raw.get("price"), "price", where),
                note=(raw.get("note") or "").strip(),
            )
        )
    return tuple(out)


def _per_share_cost(h: Holding) -> float:
    return h.cost_basis if h.cost_basis is not None else h.baseline_price


def apply_trades(holdings: tuple[Holding, ...], trades: tuple[Trade, ...], as_of: date) -> LedgerResult:
    """Apply trades dated <= ``as_of`` (stable date order) to the baseline holdings."""
    book: dict[str, Holding] = {h.ticker: h for h in holdings}
    realized = 0.0
    cash = 0.0
    for t in sorted((t for t in trades if t.date <= as_of), key=lambda t: t.date):
        current = book.get(t.ticker)
        if t.side == "buy":
            cash -= t.shares * t.price
            if current is None:
                book[t.ticker] = Holding(t.ticker, t.shares, t.price, t.date, t.price)
                continue
            total = current.shares + t.shares
            avg_cost = (current.shares * _per_share_cost(current) + t.shares * t.price) / total
            book[t.ticker] = replace(current, shares=total, cost_basis=avg_cost)
            continue
        held = current.shares if current is not None else 0.0
        if t.shares > held + _ZERO_SHARES:
            raise ValueError(f"oversell: trade on {t.date.isoformat()} sells {t.shares:g} {t.ticker} but only {held:g} held")
        cash += t.shares * t.price
        realized += (t.price - _per_share_cost(current)) * t.shares
        remaining = held - t.shares
        if remaining <= _ZERO_SHARES:
            del book[t.ticker]
        else:
            book[t.ticker] = replace(current, shares=remaining)
    return LedgerResult(holdings=tuple(book.values()), realized_pnl=realized, net_cash_flow=cash)
