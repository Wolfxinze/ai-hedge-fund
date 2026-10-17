"""Holdings CSV + trades ledger for the bit pool (research-only).

The holdings file holds the owner's real positions, so unlike the observing-pools
seed loader (which rejects-and-continues) every malformed row here fails LOUD with a
``ValueError`` naming the line/ticker: a silently skipped position would understate
the pool and corrupt every percentage derived from it.

Ledger semantics (applied after the baseline, in date order, trades dated <= as_of).
The baseline is end-of-day on each holding's ``baseline_date``, so a trade on a
baseline ticker dated on/before that day is already in it and raises, as does an
``as_of`` before any baseline_date. A buy adds shares and moves the weighted-average
per-share cost — unless the cost is unknown (``None``), which stays unknown rather
than being blended with baseline_price (a buy of a new ticker opens a position with
baseline_price = cost = trade price). A sell reduces shares and realizes
``(price - per-share cost) * shares``; with an unknown cost it realizes against
``baseline_price`` and flags ``realized_pnl_estimated``. Selling more than is held
(including any sell of an unheld ticker) raises; a position sold to zero drops out.
Everything returns new frozen objects — inputs never mutate.
"""

from __future__ import annotations

import csv
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

HOLDINGS_COLUMNS = ("ticker", "shares", "baseline_price", "baseline_date", "cost_basis")
TRADES_COLUMNS = ("date", "ticker", "side", "shares", "price", "note")
# Anchored to the repo root (not the CWD) so a cron job started elsewhere can't miss
# the ledger — the implicit trades default may be absent, so a CWD miss would
# silently drop every fill.
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HOLDINGS_PATH = _REPO_ROOT / "data/pools/bit_pool.csv"
DEFAULT_TRADES_PATH = _REPO_ROOT / "data/pools/bit_pool_trades.csv"
HOLDINGS_PATH_ENV = "BIT_POOL_PATH"
TRADES_PATH_ENV = "BIT_POOL_TRADES_PATH"
SIDES = ("buy", "sell")

# Same conservative whitelist as observing_pools.universe: leading letter, then
# letters/digits/.-, up to 10 chars.
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
# A note that is only digits/dots/commas is almost surely the tail of an unquoted
# thousands separator (``1,234.50`` -> price=1, note='234.50'); the lookahead keeps a
# digit-free note such as '...' legal.
_NUMERIC_NOTE_RE = re.compile(r"^(?=.*\d)[\d.,]+$")
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
    realized_pnl_estimated: bool  # True if any sell realized against baseline_price (cost unknown)


def resolve_holdings_path(cli_value: str | None, env: Mapping[str, str]) -> Path:
    """``--holdings`` beats ``$BIT_POOL_PATH`` beats the default path."""
    if cli_value:
        return Path(cli_value)
    if env.get(HOLDINGS_PATH_ENV):
        return Path(env[HOLDINGS_PATH_ENV])
    return DEFAULT_HOLDINGS_PATH


def resolve_trades_path(cli_value: str | None, env: Mapping[str, str]) -> tuple[Path, bool]:
    """``--trades`` beats ``$BIT_POOL_TRADES_PATH`` beats the default → (path, explicit).

    Only the implicit default may be absent (= no trades); an explicitly given path
    (flag or env) that does not exist is an error, so a typo can't silently drop fills.
    """
    if cli_value:
        return Path(cli_value), True
    if env.get(TRADES_PATH_ENV):
        return Path(env[TRADES_PATH_ENV]), True
    return DEFAULT_TRADES_PATH, False


def _read_rows(path: Path, expected: tuple[str, ...], kind: str) -> Iterable[tuple[int, dict[str, str]]]:
    """Rows as (physical line number of the row's last line, fields); malformed shapes raise.

    The last expected column is optional; when it is also the header's last column a
    row may omit it entirely (no trailing comma) and it reads as blank. Any other
    short row, and any extra field, raises naming the line.
    """
    optional = expected[-1]
    rows: list[tuple[int, dict[str, str]]] = []
    try:
        with path.open(newline="", encoding="utf-8-sig") as fh:  # -sig: tolerate an Excel BOM
            reader = csv.DictReader(fh)
            header = [c.strip() for c in (reader.fieldnames or [])]
            missing = [c for c in expected if c not in header]
            if missing:
                raise ValueError(f"{kind} CSV {path} missing columns: {missing} (expected header: {','.join(expected)})")
            reader.fieldnames = header
            for raw in reader:
                where = f"{kind} line {reader.line_num} ({(raw.get('ticker') or '').strip().upper()})"
                if None in raw:
                    raise ValueError(f"{where}: extra fields {raw[None]!r} beyond the {len(header)}-column header")
                absent = [k for k, v in raw.items() if v is None]
                if absent == [optional] and header[-1] == optional:
                    raw = {**raw, optional: ""}
                elif absent:
                    raise ValueError(f"{where}: missing fields {absent}")
                rows.append((reader.line_num, raw))
    except UnicodeDecodeError as exc:
        raise ValueError(f"{kind} CSV {path} is not valid UTF-8: {exc}") from None
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
    if not (value > 0 and math.isfinite(value)):  # also rejects NaN and inf
        raise ValueError(f"{where}: {field_name} must be a finite number > 0 (got {raw!r})")
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
    if not out:
        raise ValueError(f"holdings file has no positions: {path}")
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
        note = (raw.get("note") or "").strip()
        if _NUMERIC_NOTE_RE.match(note):
            raise ValueError(f"{where} ({ticker}): note looks like a number on line {lineno} — quote numbers containing commas (note={note!r})")
        out.append(
            Trade(
                date=_iso_date(raw.get("date"), "date", where),
                ticker=ticker,
                side=side,
                shares=_positive(raw.get("shares"), "shares", where),
                price=_positive(raw.get("price"), "price", where),
                note=note,
            )
        )
    return tuple(out)


def _check_baseline_dates(holdings: tuple[Holding, ...], trades: list[Trade], as_of: date) -> None:
    """The baseline is end-of-day on baseline_date: reject double-counted fills and pre-baseline runs."""
    baseline_dates = {h.ticker: h.baseline_date for h in holdings}
    for ticker, bd in baseline_dates.items():
        if as_of < bd:
            raise ValueError(f"as_of {as_of.isoformat()} is before {ticker}'s baseline_date {bd.isoformat()}")
    for t in trades:
        bd = baseline_dates.get(t.ticker)
        if bd is not None and t.date <= bd:
            raise ValueError(f"trade {t.side} {t.ticker} on {t.date.isoformat()} is already included in baseline (baseline_date {bd.isoformat()})")


def apply_trades(holdings: tuple[Holding, ...], trades: tuple[Trade, ...], as_of: date) -> LedgerResult:
    """Apply trades dated <= ``as_of`` (stable date order) to the baseline holdings."""
    applied = sorted((t for t in trades if t.date <= as_of), key=lambda t: t.date)
    _check_baseline_dates(holdings, applied, as_of)
    book: dict[str, Holding] = {h.ticker: h for h in holdings}
    realized = 0.0
    cash = 0.0
    estimated = False
    for t in applied:
        current = book.get(t.ticker)
        if t.side == "buy":
            cash -= t.shares * t.price
            if current is None:
                book[t.ticker] = Holding(t.ticker, t.shares, t.price, t.date, t.price)
                continue
            total = current.shares + t.shares
            # Unknown cost stays unknown: blending in baseline_price would fabricate one.
            avg_cost = None if current.cost_basis is None else (current.shares * current.cost_basis + t.shares * t.price) / total
            book[t.ticker] = replace(current, shares=total, cost_basis=avg_cost)
            continue
        held = current.shares if current is not None else 0.0
        if current is None or t.shares > held + _ZERO_SHARES:
            raise ValueError(f"oversell: trade on {t.date.isoformat()} sells {t.shares:g} {t.ticker} but only {held:g} held")
        cash += t.shares * t.price
        if current.cost_basis is None:
            estimated = True
        realized += (t.price - (current.cost_basis if current.cost_basis is not None else current.baseline_price)) * t.shares
        remaining = held - t.shares
        if remaining <= _ZERO_SHARES:
            del book[t.ticker]
        else:
            book[t.ticker] = replace(current, shares=remaining)
    return LedgerResult(holdings=tuple(book.values()), realized_pnl=realized, net_cash_flow=cash, realized_pnl_estimated=estimated)
