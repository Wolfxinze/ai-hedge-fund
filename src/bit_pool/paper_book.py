"""Cash-seeded virtual paper book (research-only; never places orders).

The daily bit-pool snapshot marks a holdings file and treats cash as sell proceeds
minus buy cost, starting at zero. It cannot represent a budget of uninvested cash,
and it does not turn a decision into a fill. This module is that missing book:

* USD cash, long-only, gross exposure at most 1 (no leverage, no shorts)
* integer shares, so a buy that does not fit in cash is cut or skipped
* explicit commission and slippage in basis points, charged to cash
* one decision source per book, stored and never re-applied for a finished session
* ``equal_weight`` deploys on the first session's close (deterministic plumbing)
* ``technical`` reads the repo's LLM-free technical analyst and fills on the *next*
  session close (bullish names share equity; anything else goes to cash)

Neither source calls an LLM or a broker. A historical replay is not a forward
paper-trading track record and is not a performance claim. Synthetic or supplied
price files must be labelled and cannot establish investing performance.

State is one JSON file, replaced atomically after each completed session. A second
run of the same window adds no fills. Extending ``--end`` continues from the last
completed session only when the rest of the config matches.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from src.compliance import research_disclaimer


def _disclaimer() -> tuple[str, str]:
    text, version = research_disclaimer()
    if not (text or "").strip():
        raise ValueError("research disclaimer is blank — refusing to emit an unstamped paper book")
    return text, version

DECISION_EQUAL = "equal_weight"
DECISION_TECHNICAL = "technical"
DECISIONS = (DECISION_EQUAL, DECISION_TECHNICAL)
REBALANCE_ONCE = "once"
REBALANCE_WEEKLY = "weekly"
EXECUTION_SAME = "same_close"
EXECUTION_NEXT = "next_close"
DATA_YFINANCE = "yfinance_historical"
DATA_SUPPLIED = "supplied_prices"
DATA_SYNTHETIC = "synthetic_fixture"
DATA_LABELS = (DATA_YFINANCE, DATA_SUPPLIED, DATA_SYNTHETIC)
CURRENCY = "USD"
DEFAULT_CASH = 50_000.0
DEFAULT_TICKERS = ("AAPL", "MSFT", "NVDA")
DEFAULT_BENCHMARK = "SPY"
DEFAULT_COMMISSION_BPS = 5.0
DEFAULT_SLIPPAGE_BPS = 5.0
MAX_BPS = 100.0
MAX_TICKERS = 8
MAX_WINDOW_DAYS = 370
CASH_FLOOR = -0.005
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_LABEL_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_SOURCE_EQUAL = "deterministic_equal_weight"
_SOURCE_TECHNICAL = "llm_free_technical_analyst_mechanical_map"

DecisionProvider = Callable[[date], dict]


@dataclass(frozen=True)
class PaperConfig:
    label: str
    initial_cash: float
    currency: str
    tickers: tuple[str, ...]
    benchmark: str
    start: date
    end: date
    decision: str
    rebalance: str
    commission_bps: float
    slippage_bps: float
    execution: str
    data_mode: str
    long_only: bool = True
    max_gross_exposure: float = 1.0

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "initial_cash": self.initial_cash,
            "currency": self.currency,
            "tickers": list(self.tickers),
            "benchmark": self.benchmark,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "decision": self.decision,
            "rebalance": self.rebalance,
            "commission_bps": self.commission_bps,
            "slippage_bps": self.slippage_bps,
            "execution": self.execution,
            "data_mode": self.data_mode,
            "long_only": self.long_only,
            "max_gross_exposure": self.max_gross_exposure,
        }


def execution_for(decision: str) -> str:
    if decision == DECISION_EQUAL:
        return EXECUTION_SAME
    if decision == DECISION_TECHNICAL:
        return EXECUTION_NEXT
    raise ValueError(f"unknown decision {decision!r}")


def default_rebalance(decision: str) -> str:
    return REBALANCE_ONCE if decision == DECISION_EQUAL else REBALANCE_WEEKLY


def fingerprint(config: PaperConfig) -> str:
    """Identity of a book. ``end`` is excluded so a later bounded run can continue."""
    payload = config.to_dict()
    payload.pop("end")
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _finite(value: float, name: str) -> float:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite (got {value!r})")
    return value


def validate_config(config: PaperConfig, *, today: date | None = None) -> None:
    today = today or date.today()
    if not _LABEL_RE.match(config.label):
        raise ValueError(f"label {config.label!r} must match {_LABEL_RE.pattern}")
    if config.currency != CURRENCY:
        raise ValueError(f"currency must be {CURRENCY} (got {config.currency!r})")
    cash = _finite(config.initial_cash, "initial_cash")
    if cash <= 0:
        raise ValueError(f"initial_cash must be > 0 (got {cash})")
    if not config.tickers:
        raise ValueError("tickers is empty")
    if len(config.tickers) > MAX_TICKERS:
        raise ValueError(f"at most {MAX_TICKERS} tickers (got {len(config.tickers)})")
    if len(set(config.tickers)) != len(config.tickers):
        raise ValueError(f"duplicate tickers: {config.tickers}")
    for ticker in config.tickers:
        if not _TICKER_RE.match(ticker):
            raise ValueError(f"invalid ticker {ticker!r}")
    if not _TICKER_RE.match(config.benchmark):
        raise ValueError(f"invalid benchmark {config.benchmark!r}")
    if config.decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    if config.rebalance not in (REBALANCE_ONCE, REBALANCE_WEEKLY):
        raise ValueError(f"rebalance must be {REBALANCE_ONCE!r} or {REBALANCE_WEEKLY!r}")
    if config.execution != execution_for(config.decision):
        raise ValueError(f"{config.decision} uses execution {execution_for(config.decision)}, not {config.execution}")
    if config.data_mode not in DATA_LABELS:
        raise ValueError(f"data_mode must be one of {DATA_LABELS}")
    for name, bps in (("commission_bps", config.commission_bps), ("slippage_bps", config.slippage_bps)):
        bps = _finite(bps, name)
        if not 0 <= bps <= MAX_BPS:
            raise ValueError(f"{name} must be in [0, {MAX_BPS:g}] (got {bps})")
    if not config.long_only:
        raise ValueError("long_only must stay true (shorts are refused)")
    gross = _finite(config.max_gross_exposure, "max_gross_exposure")
    if not 0 < gross <= 1.0:
        raise ValueError(f"max_gross_exposure must be in (0, 1] (no leverage; got {gross})")
    if config.end < config.start:
        raise ValueError(f"end {config.end} is before start {config.start}")
    if config.end > today:
        raise ValueError(f"end {config.end} is in the future (today is {today}); this command does not invent closes")
    if (config.end - config.start).days > MAX_WINDOW_DAYS:
        raise ValueError(f"window is longer than {MAX_WINDOW_DAYS} days; keep the run bounded")


def _floor_shares(value: float) -> int:
    if value < 0:
        raise ValueError(f"share count is negative ({value})")
    return int(math.floor(round(value, 8)))


def _rate(bps: float) -> float:
    return bps / 10_000.0


def buy_fill(close: float, slippage_bps: float) -> float:
    if not close > 0:
        raise ValueError(f"close must be > 0 (got {close})")
    return close * (1.0 + _rate(slippage_bps))


def sell_fill(close: float, slippage_bps: float) -> float:
    if not close > 0:
        raise ValueError(f"close must be > 0 (got {close})")
    price = close * (1.0 - _rate(slippage_bps))
    if price <= 0:
        raise ValueError(f"sell fill is non-positive (close={close}, slippage_bps={slippage_bps})")
    return price


def map_technical_signals(signals: Mapping[str, Mapping[str, object] | None], tickers: tuple[str, ...]) -> tuple[dict[str, float], list[str]]:
    """Bullish names share 100% of equity. Neutral, bearish, and missing names stay in cash.

    Confidence is ignored for sizing. No name gets a negative weight, and the weights
    sum to 1 only when at least one name is bullish (otherwise they sum to 0).
    """
    missing: list[str] = []
    bullish: list[str] = []
    for ticker in tickers:
        raw = signals.get(ticker)
        signal = raw.get("signal") if isinstance(raw, Mapping) else None
        if signal not in {"bullish", "bearish", "neutral"}:
            missing.append(ticker)
            continue
        if signal == "bullish":
            bullish.append(ticker)
    if not bullish:
        return {ticker: 0.0 for ticker in tickers}, missing
    weight = 1.0 / len(bullish)
    return {ticker: (weight if ticker in bullish else 0.0) for ticker in tickers}, missing


def validate_targets(targets: Mapping[str, float], tickers: tuple[str, ...], max_gross: float) -> dict[str, float]:
    if set(targets) != set(tickers):
        raise ValueError(f"targets must cover {list(tickers)} exactly (got {sorted(targets)})")
    clean: dict[str, float] = {}
    for ticker in tickers:
        weight = _finite(float(targets[ticker]), f"weight {ticker}")
        if weight < 0:
            raise ValueError(f"negative weight for {ticker} refused (no shorts)")
        clean[ticker] = weight
    gross = sum(clean.values())
    if gross > max_gross + 1e-9:
        raise ValueError(f"target weights sum to {gross:.6f}, above max gross {max_gross} (no leverage)")
    return clean


def decision_dates(sessions: list[date], rebalance: str) -> set[date]:
    if not sessions:
        return set()
    if rebalance == REBALANCE_ONCE:
        return {sessions[0]}
    seen: set[tuple[int, int]] = set()
    chosen: set[date] = set()
    for day in sessions:
        key = (day.isocalendar().year, day.isocalendar().week)
        if key not in seen:
            seen.add(key)
            chosen.add(day)
    return chosen


def common_sessions(prices: Mapping[str, Mapping[date, float]], tickers: tuple[str, ...], benchmark: str, start: date, end: date) -> list[date]:
    needed = list(tickers) + [benchmark]
    missing = [name for name in needed if name not in prices]
    if missing:
        raise ValueError(f"price table is missing {missing}")
    sets: list[set[date]] = []
    for name in needed:
        days = {day for day, close in prices[name].items() if start <= day <= end and isinstance(close, (int, float)) and math.isfinite(close) and close > 0}
        sets.append(days)
    return sorted(set.intersection(*sets)) if sets else []


def new_state(config: PaperConfig) -> dict:
    text, version = _disclaimer()
    return {
        "version": 1,
        "fingerprint": fingerprint(config),
        "config": config.to_dict(),
        "disclaimer": text,
        "disclaimer_version": version,
        "cash": config.initial_cash,
        "positions": {},
        "fills": [],
        "pending": None,
        "decisions": [],
        "completed_sessions": [],
        "marks": {},
        "equity_curve": [
            {
                "date": None,
                "session": "opening",
                "cash": config.initial_cash,
                "positions_value": 0.0,
                "equity": config.initial_cash,
                "peak_equity": config.initial_cash,
                "drawdown_pct": 0.0,
                "benchmark_close": None,
                "benchmark_return_pct": None,
            }
        ],
        "benchmark_base": None,
        "commission_paid": 0.0,
        "slippage_drag": 0.0,
        "realized_pnl": 0.0,
        "notes": [],
        "errors": [],
    }


def _shares(state: dict, ticker: str) -> int:
    row = state["positions"].get(ticker)
    return 0 if row is None else int(row["shares"])


def _positions_value(state: dict, closes: Mapping[str, float]) -> float:
    total = 0.0
    for ticker, row in state["positions"].items():
        shares = int(row["shares"])
        if shares < 0:
            raise RuntimeError(f"short position in state: {ticker} {shares}")
        if shares == 0:
            continue
        if ticker not in closes:
            raise ValueError(f"no close for held ticker {ticker}")
        total += shares * float(closes[ticker])
    return total


def _note(state: dict, text: str) -> None:
    state["notes"].append(text)


def _apply_buy(state: dict, day: date, ticker: str, want: int, close: float, config: PaperConfig, source: str, reason: str) -> None:
    if want <= 0:
        return
    if not config.long_only:
        raise RuntimeError("long_only is off")
    price = buy_fill(close, config.slippage_bps)
    unit = price * (1.0 + _rate(config.commission_bps))
    affordable = _floor_shares(state["cash"] / unit) if unit > 0 else 0
    while affordable > 0 and affordable * unit > state["cash"] + 1e-8:
        affordable -= 1
    qty = min(want, affordable)
    if qty <= 0:
        _note(state, f"{day.isoformat()} skip buy {ticker}: want {want} share(s) at {price:.4f} but cash is {state['cash']:.4f}")
        return
    notional = qty * price
    commission = notional * _rate(config.commission_bps)
    cost = notional + commission
    if cost > state["cash"] + 1e-8:
        raise RuntimeError(f"overspend guard tripped for {ticker}: cost {cost} cash {state['cash']}")
    held = _shares(state, ticker)
    previous = state["positions"].get(ticker)
    previous_cost = 0.0 if previous is None else float(previous["avg_cost"]) * held
    state["cash"] -= cost
    state["positions"][ticker] = {"shares": held + qty, "avg_cost": (previous_cost + notional) / (held + qty)}
    drag = qty * (price - close)
    state["commission_paid"] += commission
    state["slippage_drag"] += drag
    state["fills"].append(
        {
            "date": day.isoformat(),
            "ticker": ticker,
            "side": "buy",
            "shares": qty,
            "close": close,
            "fill_price": price,
            "notional": notional,
            "commission": commission,
            "slippage_drag": drag,
            "cash_after": state["cash"],
            "decision_source": source,
            "note": reason,
        }
    )


def _apply_sell(state: dict, day: date, ticker: str, qty: int, close: float, config: PaperConfig, source: str, reason: str) -> None:
    held = _shares(state, ticker)
    if qty <= 0:
        return
    if qty > held:
        raise RuntimeError(f"short guard tripped: sell {qty} {ticker} but only {held} held")
    price = sell_fill(close, config.slippage_bps)
    notional = qty * price
    commission = notional * _rate(config.commission_bps)
    proceeds = notional - commission
    if proceeds < -1e-8:
        raise RuntimeError(f"sell proceeds negative for {ticker}")
    avg = float(state["positions"][ticker]["avg_cost"])
    state["cash"] += proceeds
    state["realized_pnl"] += (price - avg) * qty - commission
    left = held - qty
    if left == 0:
        del state["positions"][ticker]
    else:
        state["positions"][ticker] = {"shares": left, "avg_cost": avg}
    drag = qty * (close - price)
    state["commission_paid"] += commission
    state["slippage_drag"] += drag
    state["fills"].append(
        {
            "date": day.isoformat(),
            "ticker": ticker,
            "side": "sell",
            "shares": qty,
            "close": close,
            "fill_price": price,
            "notional": notional,
            "commission": commission,
            "slippage_drag": drag,
            "cash_after": state["cash"],
            "decision_source": source,
            "note": reason,
        }
    )
    if state["cash"] < CASH_FLOOR:
        raise RuntimeError(f"cash went negative after selling {ticker}: {state['cash']}")


def execute_targets(state: dict, day: date, closes: Mapping[str, float], targets: Mapping[str, float], config: PaperConfig, source: str, reason: str) -> None:
    """Rebalance to integer share targets. Sells happen before buys. Buys never spend more than cash."""
    clean = validate_targets(targets, config.tickers, config.max_gross_exposure)
    equity = state["cash"] + _positions_value(state, closes)
    if equity <= 0:
        raise RuntimeError(f"non-positive equity on {day.isoformat()} ({equity}); refusing to trade")
    desired: dict[str, int] = {}
    for ticker in config.tickers:
        weight = clean[ticker]
        if weight == 0:
            desired[ticker] = 0
            continue
        desired[ticker] = _floor_shares(weight * equity / buy_fill(float(closes[ticker]), config.slippage_bps))
    for ticker in config.tickers:
        held = _shares(state, ticker)
        if held > desired[ticker]:
            _apply_sell(state, day, ticker, held - desired[ticker], float(closes[ticker]), config, source, reason)
    for ticker in config.tickers:
        held = _shares(state, ticker)
        if desired[ticker] > held:
            _apply_buy(state, day, ticker, desired[ticker] - held, float(closes[ticker]), config, source, reason)


def replay_ledger(initial_cash: float, fills: list[dict], ending_marks: Mapping[str, float]) -> dict:
    """Second accounting pass from the fill list alone. Used to check the running cash balance."""
    cash = initial_cash
    shares: dict[str, int] = {}
    for fill in fills:
        qty = int(fill["shares"])
        if qty <= 0:
            raise ValueError(f"non-positive fill shares: {fill}")
        ticker = fill["ticker"]
        if fill["side"] == "buy":
            cost = qty * float(fill["fill_price"]) + float(fill["commission"])
            if cost > cash + 1e-6:
                raise ValueError(f"replay overspend on {fill['date']} {ticker}: cost {cost} cash {cash}")
            cash -= cost
            shares[ticker] = shares.get(ticker, 0) + qty
        elif fill["side"] == "sell":
            held = shares.get(ticker, 0)
            if qty > held:
                raise ValueError(f"replay oversell on {fill['date']} {ticker}: sell {qty} held {held}")
            cash += qty * float(fill["fill_price"]) - float(fill["commission"])
            left = held - qty
            if left == 0:
                shares.pop(ticker, None)
            else:
                shares[ticker] = left
        else:
            raise ValueError(f"replay unknown side {fill['side']!r}")
        if cash < CASH_FLOOR:
            raise ValueError(f"replay negative cash after {fill['date']} {ticker}: {cash}")
    market = 0.0
    for ticker, qty in shares.items():
        if qty < 0:
            raise ValueError(f"replay short {ticker}")
        if ticker not in ending_marks:
            raise ValueError(f"replay missing mark for {ticker}")
        market += qty * float(ending_marks[ticker])
    return {"cash": cash, "positions_value": market, "equity": cash + market, "shares": {ticker: qty for ticker, qty in shares.items() if qty}}


def _mark(state: dict, day: date, closes: Mapping[str, float], benchmark: str) -> None:
    market = _positions_value(state, closes)
    equity = state["cash"] + market
    if state["cash"] < CASH_FLOOR:
        raise RuntimeError(f"negative cash on {day.isoformat()}: {state['cash']}")
    for ticker, row in state["positions"].items():
        if int(row["shares"]) < 0:
            raise RuntimeError(f"short {ticker}")
    if state["benchmark_base"] is None:
        state["benchmark_base"] = float(closes[benchmark])
    base = float(state["benchmark_base"])
    bench_ret = (float(closes[benchmark]) / base - 1.0) * 100.0 if base > 0 else None
    peak = max(float(point["peak_equity"]) for point in state["equity_curve"])
    peak = max(peak, equity)
    drawdown = (equity / peak - 1.0) * 100.0 if peak > 0 else None
    state["marks"][day.isoformat()] = {name: float(value) for name, value in closes.items()}
    state["equity_curve"].append(
        {
            "date": day.isoformat(),
            "session": "close",
            "cash": state["cash"],
            "positions_value": market,
            "equity": equity,
            "peak_equity": peak,
            "drawdown_pct": drawdown,
            "benchmark_close": float(closes[benchmark]),
            "benchmark_return_pct": bench_ret,
        }
    )
    state["completed_sessions"].append(day.isoformat())


def step_session(state: dict, day: date, sessions: list[date], prices: Mapping[str, Mapping[date, float]], config: PaperConfig, provider: DecisionProvider) -> None:
    """Advance one session. Completed sessions are left untouched (resume / idempotency)."""
    if day.isoformat() in state["completed_sessions"]:
        return
    if day not in sessions:
        raise ValueError(f"{day.isoformat()} is not a session in this window")
    closes = {ticker: float(prices[ticker][day]) for ticker in config.tickers}
    closes[config.benchmark] = float(prices[config.benchmark][day])
    nxt = sessions[sessions.index(day) + 1] if sessions.index(day) + 1 < len(sessions) else None
    # A next-close decision fills on the first later session that actually runs.
    # Ending a bounded run on the decision day leaves it pending; extending --end
    # fills it once, on that later session, and does not fill it twice.
    pending = state["pending"]
    if pending and date.fromisoformat(pending["decided_on"]) < day:
        execute_targets(state, day, closes, pending["targets"], config, pending["source"], f"next_close fill of decision {pending['decided_on']}")
        for record in state["decisions"]:
            if record["date"] == pending["decided_on"]:
                record["fill_on"] = day.isoformat()
        state["pending"] = None
    if day in decision_dates(sessions, config.rebalance) and not any(row["date"] == day.isoformat() for row in state["decisions"]):
        produced = provider(day)
        targets = validate_targets(produced["targets"], config.tickers, config.max_gross_exposure)
        source = str(produced.get("source") or config.decision)
        record = {
            "date": day.isoformat(),
            "targets": targets,
            "signals": produced.get("signals"),
            "missing_signals": list(produced.get("missing_signals") or []),
            "source": source,
            "execution": config.execution,
            "fill_on": day.isoformat() if config.execution == EXECUTION_SAME else (None if nxt is None else nxt.isoformat()),
        }
        state["decisions"].append(record)
        if record["missing_signals"]:
            state["errors"].append(f"{day.isoformat()} missing technical signal for {', '.join(record['missing_signals'])}; those names were left in cash")
        if config.execution == EXECUTION_SAME:
            execute_targets(state, day, closes, targets, config, source, "same_close deployment")
        else:
            state["pending"] = {"decided_on": day.isoformat(), "fill_on": record["fill_on"], "targets": targets, "source": source}
            if nxt is None:
                _note(state, f"{day.isoformat()} decision was not filled: no later session in the window")
    _mark(state, day, closes, config.benchmark)
    _check_replay(state, closes)


def _check_replay(state: dict, closes: Mapping[str, float]) -> None:
    replayed = replay_ledger(float(state["config"]["initial_cash"]), state["fills"], closes)
    if abs(replayed["cash"] - state["cash"]) > 1e-4:
        raise RuntimeError(f"ledger cash {state['cash']} != replay {replayed['cash']}")
    market = _positions_value(state, closes)
    if abs((state["cash"] + market) - (replayed["cash"] + replayed["positions_value"])) > 1e-4:
        raise RuntimeError("cash + holdings diverged from the replayed ledger")
    held = {ticker: int(row["shares"]) for ticker, row in state["positions"].items() if int(row["shares"])}
    if held != replayed["shares"]:
        raise RuntimeError(f"positions {held} != replay {replayed['shares']}")


def run_book(state: dict, sessions: list[date], prices: Mapping[str, Mapping[date, float]], config: PaperConfig, provider: DecisionProvider, persist: Callable[[dict], None] | None = None) -> dict:
    if not sessions:
        raise ValueError(f"no common sessions with positive closes for {list(config.tickers)} and {config.benchmark} between {config.start} and {config.end}")
    for day in sessions:
        if day.isoformat() in state["completed_sessions"]:
            continue
        step_session(state, day, sessions, prices, config, provider)
        if persist is not None:
            persist(state)
    return state


def max_drawdown(state: dict) -> tuple[float | None, str | None]:
    worst: float | None = None
    when: str | None = None
    for point in state["equity_curve"]:
        value = point.get("drawdown_pct")
        if value is None:
            continue
        if worst is None or value < worst:
            worst = float(value)
            when = point.get("date")
    return worst, when


def build_summary(state: dict, *, price_provenance: Mapping[str, dict] | None = None, sessions: list[date] | None = None) -> dict:
    text, version = _disclaimer()
    curve = state["equity_curve"]
    last = curve[-1]
    initial = float(state["config"]["initial_cash"])
    equity = float(last["equity"])
    drawdown, drawdown_date = max_drawdown(state)
    config = state["config"]
    pending = state["pending"]
    unfilled = pending is not None and pending.get("fill_on") is None
    limitations = [
        "Virtual USD paper book only. No brokerage connection, no order routing, no live trading, no leverage, and no short sales.",
        "equal_weight is a deterministic plumbing rule: on the first session it buys integer shares toward equal weights and then holds. It is not an AI investment decision and not the portfolio manager.",
        "technical mode calls the repository's LLM-free technical_analyst_agent (price indicators only) and maps bullish to an equal long weight and every other label to cash. Confidence does not change size. No LLM and no portfolio-manager agent is invoked.",
        "technical fills use the next session's close after the signal close, plus a flat slippage assumption. equal_weight deploys at the first session close plus the same slippage assumption. Neither is a broker fill or a next-open fill.",
        "This command replays a bounded historical window. It is not a forward paper-trading track record. A later run with a later --end on the same state file continues that book; this process does not schedule itself.",
        "Commission and slippage are flat basis-point assumptions charged to cash, not a broker fee schedule. Average cost excludes commission; commission still reduces cash and equity.",
        "Integer shares leave residual cash. A buy that does not fit is skipped rather than partially funded with borrowed money.",
        "The SPY (or configured benchmark) figure is the price return between the first and last session closes in the window. It is not charged costs and it is not a portfolio.",
        "Synthetic and supplied price files check mechanics only. They do not establish investing performance. A yfinance replay describes that window under the stated rule; it is not a forecast and not a guarantee.",
        "The existing bit-pool snapshot starts cash at zero and values a holdings file. It is a different accounting identity and was not used to mark this cash book.",
    ]
    if unfilled:
        limitations.append("The last technical decision had no later session inside the window, so it remains pending and unfilled.")
    establishes = False
    return {
        "disclaimer": text,
        "disclaimer_version": version,
        "book": config["label"],
        "fingerprint": state["fingerprint"],
        "currency": config["currency"],
        "initial_cash": initial,
        "final_cash": last["cash"],
        "final_positions_value": last["positions_value"],
        "final_equity": equity,
        "cash_plus_positions": last["cash"] + last["positions_value"],
        "total_return_pct": (equity / initial - 1.0) * 100.0 if initial else None,
        "max_drawdown_pct": drawdown,
        "max_drawdown_date": drawdown_date,
        "benchmark": config["benchmark"],
        "benchmark_return_pct": last.get("benchmark_return_pct"),
        "benchmark_note": "Price return from the first session close to the last session close. No costs and no slippage.",
        "decision_mode": config["decision"],
        "decision_source": _SOURCE_EQUAL if config["decision"] == DECISION_EQUAL else _SOURCE_TECHNICAL,
        "llm_invoked": False,
        "ai_portfolio_manager": False,
        "run_kind": "bounded_historical_replay",
        "forward_paper_trading": False,
        "establishes_investing_performance": establishes,
        "execution": config["execution"],
        "rebalance": config["rebalance"],
        "data_mode": config["data_mode"],
        "costs": {
            "commission_bps": config["commission_bps"],
            "slippage_bps": config["slippage_bps"],
            "commission_paid": state["commission_paid"],
            "slippage_drag": state["slippage_drag"],
            "cost_basis": "average fill price, commission excluded from cost basis and deducted from cash",
        },
        "constraints": {"long_only": True, "shorts": False, "leverage": False, "max_gross_exposure": config["max_gross_exposure"], "integer_shares": True},
        "window": {
            "requested_start": config["start"],
            "requested_end": config["end"],
            "first_session": None if not sessions else sessions[0].isoformat(),
            "last_session": None if not sessions else sessions[-1].isoformat(),
            "sessions": 0 if sessions is None else len(sessions),
            "completed_sessions": len(state["completed_sessions"]),
        },
        "fills": len(state["fills"]),
        "decisions": len(state["decisions"]),
        "pending": state["pending"],
        "positions": _position_rows(state),
        "realized_pnl": state["realized_pnl"],
        "invariants": invariant_report(state),
        "notes": list(state["notes"]),
        "errors": list(state["errors"]),
        "price_provenance": dict(price_provenance or {}),
        "limitations": limitations,
        "runtime": {"python": sys.version.split()[0]},
    }


def _position_rows(state: dict) -> list[dict]:
    marks = state["marks"]
    last_day = state["completed_sessions"][-1] if state["completed_sessions"] else None
    last_marks = marks.get(last_day, {}) if last_day else {}
    equity = float(state["equity_curve"][-1]["equity"])
    rows = []
    for ticker, row in sorted(state["positions"].items()):
        shares = int(row["shares"])
        close = last_marks.get(ticker)
        market = None if close is None else shares * float(close)
        rows.append(
            {
                "ticker": ticker,
                "shares": shares,
                "avg_cost": row["avg_cost"],
                "close": close,
                "market_value": market,
                "weight_of_equity": None if market is None or equity <= 0 else market / equity,
            }
        )
    return rows


def invariant_report(state: dict) -> dict:
    """Structural checks over the stored book. A failed check is reported; callers also raise on the running pass."""
    problems: list[str] = []
    initial = float(state["config"]["initial_cash"])
    for point in state["equity_curve"]:
        cash = float(point["cash"])
        market = float(point["positions_value"])
        equity = float(point["equity"])
        if cash < CASH_FLOOR:
            problems.append(f"negative cash on {point.get('date')}: {cash}")
        if abs((cash + market) - equity) > 1e-4:
            problems.append(f"cash + holdings != equity on {point.get('date')}")
    for ticker, row in state["positions"].items():
        if int(row["shares"]) < 0:
            problems.append(f"short {ticker}")
    if state["completed_sessions"]:
        last = state["completed_sessions"][-1]
        try:
            replay_ledger(initial, state["fills"], state["marks"][last])
        except (ValueError, KeyError) as exc:
            problems.append(f"replay failed: {exc}")
    return {"ok": not problems, "problems": problems}


def equal_weight_provider(config: PaperConfig) -> DecisionProvider:
    weight = 1.0 / len(config.tickers)
    targets = {ticker: weight for ticker in config.tickers}

    def provide(day: date) -> dict:
        del day
        return {"targets": targets, "signals": None, "missing_signals": [], "source": _SOURCE_EQUAL}

    return provide


def _signal_row(raw: object) -> dict | None:
    if not isinstance(raw, Mapping):
        return None
    signal = raw.get("signal")
    if signal not in {"bullish", "bearish", "neutral"}:
        return None
    confidence = raw.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(float(confidence)):
        confidence_out = None
    else:
        confidence_out = float(confidence)
    return {"signal": signal, "confidence": confidence_out}


def technical_provider(config: PaperConfig, signal_fn: Callable[[date, tuple[str, ...]], Mapping[str, object]]) -> DecisionProvider:
    def provide(day: date) -> dict:
        raw = signal_fn(day, config.tickers)
        stored = {ticker: _signal_row(raw.get(ticker)) for ticker in config.tickers}
        targets, missing = map_technical_signals(stored, config.tickers)
        return {"targets": targets, "signals": stored, "missing_signals": missing, "source": _SOURCE_TECHNICAL}

    return provide


def live_technical_signals(day: date, tickers: tuple[str, ...]) -> dict[str, dict | None]:
    """Call the in-repo technical analyst. Lazy import: no LLM client and no network at import time.

    The analyst computes indicators from prices it fetches itself through the configured
    provider (yfinance by default). It does not call a model.
    """
    from src.agents.technicals import technical_analyst_agent

    start = (day - timedelta(days=400)).isoformat()
    payload = {
        "messages": [],
        "data": {
            "tickers": list(tickers),
            "start_date": start,
            "end_date": day.isoformat(),
            "portfolio": {"cash": 0.0, "positions": {}},
            "analyst_signals": {},
        },
        "metadata": {"show_reasoning": False, "model_name": "none", "model_provider": "none"},
    }
    try:
        out = technical_analyst_agent(payload)
    except Exception as exc:  # noqa: BLE001 — surface the failure; do not invent a signal
        raise RuntimeError(f"technical analyst failed on {day.isoformat()}: {type(exc).__name__}: {exc}") from exc
    analysis = (out.get("data") or {}).get("analyst_signals", {}).get("technical_analyst_agent", {})
    signals: dict[str, dict | None] = {}
    for ticker in tickers:
        row = analysis.get(ticker)
        if not isinstance(row, dict) or row.get("signal") not in {"bullish", "bearish", "neutral"}:
            signals[ticker] = None
        else:
            signals[ticker] = {"signal": row.get("signal"), "confidence": row.get("confidence")}
    return signals


def load_price_csv(path: Path) -> dict[str, dict[date, float]]:
    """CSV columns: date,ticker,close. The last row for a date wins."""
    table: dict[str, dict[date, float]] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        header = [name.strip() for name in (reader.fieldnames or [])]
        required = {"date", "ticker", "close"}
        if not required.issubset(set(header)):
            raise ValueError(f"price CSV {path} needs columns date,ticker,close")
        for raw in reader:
            ticker = (raw.get("ticker") or "").strip().upper()
            if not _TICKER_RE.match(ticker):
                raise ValueError(f"price CSV invalid ticker {ticker!r}")
            try:
                day = date.fromisoformat((raw.get("date") or "").strip())
                close = float((raw.get("close") or "").strip())
            except ValueError as exc:
                raise ValueError(f"price CSV bad row {raw!r}: {exc}") from None
            if not (math.isfinite(close) and close > 0):
                raise ValueError(f"price CSV non-positive close for {ticker} on {day}")
            table.setdefault(ticker, {})[day] = close
    if not table:
        raise ValueError(f"price CSV {path} has no rows")
    return table


def fetch_price_table(tickers: tuple[str, ...], benchmark: str, start: date, end: date) -> tuple[dict[str, dict[date, float]], dict[str, dict]]:
    from src.tools.api import get_prices

    table: dict[str, dict[date, float]] = {}
    provenance: dict[str, dict] = {"provider": "yfinance_via_src.tools.api.get_prices", "symbols": {}}
    for ticker in list(tickers) + [benchmark]:
        try:
            rows = get_prices(ticker, start.isoformat(), end.isoformat())
        except Exception as exc:  # noqa: BLE001 — one symbol's outage fails the book loudly
            raise RuntimeError(f"price fetch failed for {ticker}: {type(exc).__name__}: {exc}") from exc
        closes: dict[date, float] = {}
        for row in rows:
            try:
                day = date.fromisoformat(str(row.time)[:10])
                close = float(row.close)
            except (TypeError, ValueError):
                continue
            if start <= day <= end and math.isfinite(close) and close > 0:
                closes[day] = close
        table[ticker] = closes
        provenance["symbols"][ticker] = {
            "rows": len(closes),
            "first": None if not closes else min(closes).isoformat(),
            "last": None if not closes else max(closes).isoformat(),
        }
    return table, provenance


def price_provenance_from_table(table: Mapping[str, Mapping[date, float]], names: list[str], source: str) -> dict:
    symbols = {}
    for name in names:
        closes = table.get(name, {})
        symbols[name] = {"rows": len(closes), "first": None if not closes else min(closes).isoformat(), "last": None if not closes else max(closes).isoformat()}
    return {"provider": source, "symbols": symbols}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def save_state(path: Path, state: dict) -> None:
    _atomic_write(path, json.dumps(state, indent=2, sort_keys=True, allow_nan=False) + "\n")


def load_state(path: Path, config: PaperConfig) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"cannot read paper state {path}: {exc}") from None
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ValueError(f"unsupported paper state version in {path}")
    expected = fingerprint(config)
    if state.get("fingerprint") != expected:
        raise ValueError(f"state {path} belongs to book {state.get('fingerprint')} ({(state.get('config') or {}).get('label')}), not {expected} ({config.label}). Use a separate --state file, or --reset to discard this book.")
    state["config"]["end"] = config.end.isoformat()
    return state


def _write_fills(path: Path, fills: list[dict]) -> None:
    fields = ["date", "ticker", "side", "shares", "close", "fill_price", "notional", "commission", "slippage_drag", "cash_after", "decision_source", "note"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for fill in fills:
            writer.writerow({key: fill.get(key) for key in fields})


def _write_equity(path: Path, curve: list[dict]) -> None:
    fields = ["date", "session", "cash", "positions_value", "equity", "peak_equity", "drawdown_pct", "benchmark_close", "benchmark_return_pct"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        for point in curve:
            row = {key: point.get(key) for key in fields}
            if row.get("date") is None:
                row["date"] = ""
            for key, value in list(row.items()):
                if value is None:
                    row[key] = ""
            writer.writerow(row)


def _write_prices(path: Path, marks: Mapping[str, Mapping[str, float]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date", "ticker", "close"], lineterminator="\n")
        writer.writeheader()
        for day in sorted(marks):
            for ticker in sorted(marks[day]):
                writer.writerow({"date": day, "ticker": ticker, "close": marks[day][ticker]})


def write_outputs(out: Path, state: dict, summary: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    _atomic_write(out / "summary.json", json.dumps(summary, indent=2, allow_nan=False) + "\n")
    _atomic_write(out / "decisions.json", json.dumps(state["decisions"], indent=2, allow_nan=False) + "\n")
    _write_fills(out / "fills.csv", state["fills"])
    _write_equity(out / "equity.csv", state["equity_curve"])
    _write_prices(out / "prices_used.csv", state["marks"])


def _parse_date(raw: str, name: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"{name} {raw!r} is not YYYY-MM-DD") from None


def _tickers(raw: str) -> tuple[str, ...]:
    parts = tuple(piece.strip().upper() for piece in raw.split(",") if piece.strip())
    if not parts:
        raise ValueError("tickers is empty")
    return parts


def config_from_mapping(data: Mapping[str, object], *, today: date | None = None) -> PaperConfig:
    decision = str(data.get("decision") or DECISION_EQUAL)
    rebalance = str(data.get("rebalance") or default_rebalance(decision))
    tickers_raw = data.get("tickers") or list(DEFAULT_TICKERS)
    if isinstance(tickers_raw, str):
        tickers = _tickers(tickers_raw)
    else:
        tickers = tuple(str(item).strip().upper() for item in tickers_raw)
    config = PaperConfig(
        label=str(data.get("label") or "verify"),
        initial_cash=float(data.get("initial_cash", DEFAULT_CASH)),
        currency=str(data.get("currency") or CURRENCY),
        tickers=tickers,
        benchmark=str(data.get("benchmark") or DEFAULT_BENCHMARK).strip().upper(),
        start=_parse_date(str(data["start"]), "start"),
        end=_parse_date(str(data["end"]), "end"),
        decision=decision,
        rebalance=rebalance,
        commission_bps=float(data.get("commission_bps", DEFAULT_COMMISSION_BPS)),
        slippage_bps=float(data.get("slippage_bps", DEFAULT_SLIPPAGE_BPS)),
        execution=execution_for(decision),
        data_mode=str(data.get("data_mode") or DATA_YFINANCE),
        long_only=bool(data.get("long_only", True)),
        max_gross_exposure=float(data.get("max_gross_exposure", 1.0)),
    )
    validate_config(config, today=today)
    return config


def _overlay(base: dict, args: object) -> dict:
    mapping = {
        "label": "label",
        "cash": "initial_cash",
        "tickers": "tickers",
        "benchmark": "benchmark",
        "start": "start",
        "end": "end",
        "decision": "decision",
        "rebalance": "rebalance",
        "commission_bps": "commission_bps",
        "slippage_bps": "slippage_bps",
    }
    merged = dict(base)
    for arg_name, key in mapping.items():
        value = getattr(args, arg_name, None)
        if value is not None:
            merged[key] = value
    return merged


def technical_systemic(state: dict) -> bool:
    if state["config"]["decision"] != DECISION_TECHNICAL or not state["decisions"]:
        return False

    def usable(record: dict) -> bool:
        signals = record.get("signals") or {}
        return any(isinstance(row, dict) and row.get("signal") in {"bullish", "bearish", "neutral"} for row in signals.values())

    return not any(usable(record) for record in state["decisions"])


def run_paper_cli(args: object, *, today: date | None = None) -> int:
    """CLI entry. Returns 0 when invariants hold, 1 on error or a fully missing technical signal."""
    try:
        base: dict = {}
        config_path = getattr(args, "config", None)
        if config_path:
            base = json.loads(Path(config_path).read_text(encoding="utf-8"))
            if not isinstance(base, dict):
                raise ValueError("--config must be a JSON object")
        merged = _overlay(base, args)
        if "start" not in merged or "end" not in merged:
            raise ValueError("paper run needs --start and --end (or a --config that sets them)")
        prices_csv = getattr(args, "prices_csv", None)
        data_label = getattr(args, "data_label", None)
        if prices_csv:
            if data_label is None:
                raise ValueError("--prices-csv requires --data-label synthetic_fixture or supplied_prices")
            if data_label == DATA_YFINANCE:
                raise ValueError("a price CSV cannot be labelled yfinance_historical")
            merged["data_mode"] = data_label
        else:
            if data_label not in (None, DATA_YFINANCE):
                raise ValueError("--data-label synthetic_fixture and supplied_prices require --prices-csv")
            merged["data_mode"] = DATA_YFINANCE
        config = config_from_mapping(merged, today=today)
        out = Path(args.out)
        state_path = Path(args.state) if getattr(args, "state", None) else out / "state.json"
        if getattr(args, "reset", False) and state_path.exists():
            state_path.unlink()
        if state_path.exists():
            state = load_state(state_path, config)
        else:
            state = new_state(config)
        if prices_csv:
            table = load_price_csv(Path(prices_csv))
            provenance = price_provenance_from_table(table, list(config.tickers) + [config.benchmark], f"csv:{prices_csv}")
        else:
            table, provenance = fetch_price_table(config.tickers, config.benchmark, config.start, config.end)
            provenance["provider"] = "yfinance_via_src.tools.api.get_prices"
        sessions = common_sessions(table, config.tickers, config.benchmark, config.start, config.end)
        if config.decision == DECISION_EQUAL:
            provider = equal_weight_provider(config)
        else:
            provider = technical_provider(config, live_technical_signals)
        before = len(state["completed_sessions"])
        run_book(state, sessions, table, config, provider, persist=lambda current: save_state(state_path, current))
        summary = build_summary(state, price_provenance=provenance, sessions=sessions)
        summary["state_path"] = str(state_path)
        summary["new_sessions_this_run"] = len(state["completed_sessions"]) - before
        write_outputs(out, state, summary)
        save_state(state_path, state)
        print(out / "summary.json")
        _print_result(summary)
        if not summary["invariants"]["ok"]:
            print(f"bit_pool paper: error: invariant failure {summary['invariants']['problems']}", file=sys.stderr)
            return 1
        if technical_systemic(state):
            print("bit_pool paper: error: every technical decision lacked a usable signal", file=sys.stderr)
            return 1
        return 0
    except (ValueError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        if "state" in locals() and "out" in locals() and state.get("completed_sessions"):
            try:
                partial = build_summary(state, price_provenance=locals().get("provenance"), sessions=locals().get("sessions"))
                partial["errors"] = list(partial["errors"]) + [f"{type(exc).__name__}: {exc}"]
                write_outputs(out, state, partial)
                if "state_path" in locals():
                    save_state(state_path, state)
            except (ValueError, OSError, RuntimeError, json.JSONDecodeError):
                pass
        print(f"bit_pool paper: error: {exc}", file=sys.stderr)
        return 1


def _print_result(summary: dict) -> None:
    print(
        f"book={summary['book']} equity={summary['final_equity']:.2f} cash={summary['final_cash']:.2f} "
        f"positions={summary['final_positions_value']:.2f} return_pct={summary['total_return_pct']:.4f} "
        f"max_drawdown_pct={summary['max_drawdown_pct']} sessions={summary['window']['completed_sessions']} "
        f"new={summary['new_sessions_this_run']} data={summary['data_mode']} decision={summary['decision_source']} llm=false"
    )
