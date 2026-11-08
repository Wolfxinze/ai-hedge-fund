"""Rank the repo seed universe and propose a virtual allocation. Research only.

This is not an all-market optimizer and it does not call an LLM. It loads
``data/universes/ai_seed.csv`` (the observing-pool seed: a curated list, not
the whole US market), marks each name to the last completed US session, and
runs the repository's LLM-free analysts (technical, fundamentals, valuation,
growth, sentiment). Scores use ``signal_to_score`` and the v3-4comp composite.
A missing analyst is a coverage gap, not a bearish zero.

Proposed shares are sized off that session's close plus the paper book's
5 bps slippage and 5 bps commission. They are pending the next session. This
module does not invent that next close and does not book a fill.

No broker, no short, no leverage.
"""

from __future__ import annotations

import csv
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from src.bit_pool.paper_book import buy_fill, dump_json
from src.bit_pool.simulate import CLOSE_SETTLED_AT, MARKET_TZ
from src.observing_pools.classify import SEED_LABEL_CONFIDENCE
from src.observing_pools.scoring import FORMULA_4COMP, build_components, composite, mean_or_none, signal_to_score

CASH_RESERVE = 0.20
MAX_NAME_WEIGHT = 0.12
MAX_SECTOR_WEIGHT = 0.35
MAX_NAMES = 8
MIN_ORDER_WEIGHT = 0.01
CANDIDATE_MIN = 60.0
WATCH_MIN = 45.0
MIN_MEDIAN_DOLLAR_VOLUME = 20_000_000.0
MIN_VALUE_ANALYSTS = 1
LOOKBACK_SESSIONS = 20
COMMISSION_BPS = 5.0
SLIPPAGE_BPS = 5.0
ANALYSTS = ("technical_analyst", "fundamentals_analyst", "valuation_analyst", "growth_analyst", "sentiment_analyst")
_METHOD_GAP_RE = re.compile(r"([a-z_]+_analysis):.*?Gap:\s*(-?\d+(?:\.\d+)?)%", re.IGNORECASE)
EXTREME_GAP_PCT = 200.0
VALUE_ANALYSTS = ("fundamentals_analyst", "valuation_analyst")

AnalystRunner = Callable[[str, str, date], dict | None]


@dataclass(frozen=True)
class SeedName:
    ticker: str
    name: str
    exchange: str
    sector: str
    industry: str
    platforms: tuple[str, ...]


@dataclass(frozen=True)
class Quote:
    close: float
    close_date: date
    volume: float | None
    median_dollar_volume: float | None
    sessions: int


def load_seed(path: Path) -> tuple[SeedName, ...]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"ticker", "name", "exchange", "sector", "industry", "platforms"}
        header = set(reader.fieldnames or [])
        if not required.issubset(header):
            raise ValueError(f"seed {path} missing columns: {sorted(required - header)}")
        rows: list[SeedName] = []
        seen: set[str] = set()
        for raw in reader:
            ticker = (raw.get("ticker") or "").strip().upper()
            if not ticker or ticker in seen:
                raise ValueError(f"seed {path} has a blank or duplicate ticker {ticker!r}")
            seen.add(ticker)
            platforms = tuple(part.strip() for part in (raw.get("platforms") or "").split(";") if part.strip())
            rows.append(
                SeedName(
                    ticker=ticker,
                    name=(raw.get("name") or "").strip(),
                    exchange=(raw.get("exchange") or "").strip(),
                    sector=(raw.get("sector") or "").strip() or "unknown",
                    industry=(raw.get("industry") or "").strip(),
                    platforms=platforms,
                )
            )
    if not rows:
        raise ValueError(f"seed {path} is empty")
    return tuple(rows)


def last_completed_session(session_dates: Sequence[date], *, now: datetime, requested: date | None = None) -> date:
    """Latest date that both has a bar and is a settled US session.

    Weekends and holidays are simply dates missing from ``session_dates``
    (the SPY calendar actually fetched). A bar dated today counts only at or
    after 16:15 America/New_York. A bar after today's ET date is ignored.
    ``requested`` later than that session raises instead of inventing a close.
    """
    now_et = now.astimezone(MARKET_TZ) if now.tzinfo is not None else now.replace(tzinfo=MARKET_TZ)
    today = now_et.date()
    completed = [day for day in session_dates if day < today or (day == today and now_et.time() >= CLOSE_SETTLED_AT)]
    if not completed:
        raise ValueError("no completed US session in the supplied calendar")
    latest = max(completed)
    if requested is None:
        return latest
    if requested > latest:
        raise ValueError(f"requested as-of {requested.isoformat()} is after the last completed session {latest.isoformat()}; refusing to invent a close")
    eligible = [day for day in completed if day <= requested]
    if not eligible:
        raise ValueError(f"no completed session on or before {requested.isoformat()}")
    return max(eligible)


def _confidence(row: Mapping[str, object] | None) -> float | None:
    if not row:
        return None
    confidence = row.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(float(confidence)):
        return None
    return float(confidence)


def _score(row: Mapping[str, object] | None) -> float | None:
    """A zero-confidence label is not a view. It must not become a neutral 50."""
    if not row:
        return None
    signal = row.get("signal")
    if signal not in {"bullish", "bearish", "neutral"}:
        return None
    confidence = _confidence(row)
    if confidence is None or confidence <= 0:
        return None
    return signal_to_score(str(signal), confidence)


def valuation_order_block(evidence: str) -> tuple[bool, list[str]]:
    """Block an automatic order when the agent's DCF gap is extreme, or when DCF is absent and another method's gap is.

    Owner-earnings gaps in this agent can print thousands of percent beside a ordinary DCF.
    The DCF line, when present, is the sizing check. Every wide gap is still listed.
    """
    found = [(name, float(gap)) for name, gap in _METHOD_GAP_RE.findall(evidence or "")]
    flags = [f"{name} {gap:.1f}%" for name, gap in found if abs(gap) > EXTREME_GAP_PCT]
    dcf = [gap for name, gap in found if name.lower().startswith("dcf")]
    if dcf:
        return abs(dcf[0]) > EXTREME_GAP_PCT, flags
    return any(abs(gap) > EXTREME_GAP_PCT for _, gap in found), flags


def _evidence(row: Mapping[str, object] | None) -> str:
    if not row:
        return ""
    reasoning = row.get("reasoning")
    if not isinstance(reasoning, dict):
        return ""
    parts: list[str] = []
    for key, value in reasoning.items():
        if isinstance(value, dict) and value.get("details"):
            parts.append(f"{key}: {value['details']}")
        elif key == "final_analysis" and isinstance(value, dict):
            parts.append(f"growth weighted_score={value.get('weighted_score')}")
    text = " | ".join(parts)
    return text[:500]


def sentiment_has_observations(row: Mapping[str, object] | None) -> bool:
    """Yahoo news rows in this provider carry a null sentiment label, so a neutral
    with zero dated insider trades and zero directional articles is not evidence."""
    if not row or not isinstance(row.get("reasoning"), dict):
        return False
    reasoning = row["reasoning"]
    insider = reasoning.get("insider_trading") if isinstance(reasoning.get("insider_trading"), dict) else {}
    news = reasoning.get("news_sentiment") if isinstance(reasoning.get("news_sentiment"), dict) else {}
    insider_metrics = insider.get("metrics") if isinstance(insider.get("metrics"), dict) else {}
    news_metrics = news.get("metrics") if isinstance(news.get("metrics"), dict) else {}
    trades = insider_metrics.get("total_trades") or 0
    directional = (news_metrics.get("bullish_articles") or 0) + (news_metrics.get("bearish_articles") or 0)
    return trades > 0 or directional > 0


def rank_universe(
    names: Sequence[SeedName],
    quotes: Mapping[str, Quote | None],
    analysts: Mapping[str, Mapping[str, Mapping[str, object] | None]],
    *,
    session: date,
) -> list[dict]:
    ranked: list[dict] = []
    for seed in names:
        quote = quotes.get(seed.ticker)
        by_agent = dict(analysts.get(seed.ticker) or {})
        if "sentiment_analyst" in by_agent and not sentiment_has_observations(by_agent.get("sentiment_analyst")):
            by_agent["sentiment_analyst"] = None
        factors = []
        for agent in ANALYSTS:
            row = by_agent.get(agent)
            score = _score(row if isinstance(row, Mapping) else None)
            factors.append(
                {
                    "analyst": agent,
                    "status": "observed" if score is not None else "missing",
                    "signal": None if not isinstance(row, Mapping) else row.get("signal"),
                    "confidence": None if not isinstance(row, Mapping) else row.get("confidence"),
                    "score": score,
                    "evidence": _evidence(row if isinstance(row, Mapping) else None),
                }
            )
        value = mean_or_none(_score(by_agent.get(agent) if isinstance(by_agent.get(agent), Mapping) else None) for agent in VALUE_ANALYSTS)
        growth = _score(by_agent.get("growth_analyst") if isinstance(by_agent.get("growth_analyst"), Mapping) else None)
        momentum = mean_or_none(
            [
                _score(by_agent.get("technical_analyst") if isinstance(by_agent.get("technical_analyst"), Mapping) else None),
                _score(by_agent.get("sentiment_analyst") if isinstance(by_agent.get("sentiment_analyst"), Mapping) else None),
            ]
        )
        platform = SEED_LABEL_CONFIDENCE * 100.0
        components = build_components(
            {"platform_fit": platform, "value_investor": value, "innovation_growth": growth, "risk_adjusted_momentum": momentum},
            formula_version=FORMULA_4COMP,
        )
        composite_score = composite(components, pool_serenity_median=None, formula_version=FORMULA_4COMP)
        observed = [factor for factor in factors if factor["status"] == "observed"]
        gaps = [factor["analyst"] for factor in factors if factor["status"] == "missing"]
        liquid = quote is not None and quote.close_date == session and quote.median_dollar_volume is not None and quote.median_dollar_volume >= MIN_MEDIAN_DOLLAR_VOLUME
        stale_price = quote is None or quote.close_date != session
        if composite_score is None or len([agent for agent in VALUE_ANALYSTS if _score(by_agent.get(agent) if isinstance(by_agent.get(agent), Mapping) else None) is not None]) < MIN_VALUE_ANALYSTS:
            pool = "excluded"
            reason = "insufficient fundamental or valuation evidence; not scored as zero"
        elif stale_price:
            pool = "excluded"
            reason = "no close on the as-of session" if quote is None or quote.close_date != session else "no positive close on the as-of session"
        elif composite_score < WATCH_MIN:
            pool = "avoid"
            reason = f"composite {composite_score:.1f} is below {WATCH_MIN:.0f}"
        elif composite_score < CANDIDATE_MIN or not liquid:
            pool = "watch"
            reason = "below the candidate threshold" if composite_score < CANDIDATE_MIN else f"median dollar volume below {MIN_MEDIAN_DOLLAR_VOLUME:.0f}"
        else:
            pool = "candidate"
            reason = f"composite {composite_score:.1f} with liquidity and value evidence"
        valuation_evidence = next((factor["evidence"] for factor in factors if factor["analyst"] == "valuation_analyst"), "")
        blocked, gap_flags = valuation_order_block(valuation_evidence)
        uncertainty = gap_flags
        if pool == "candidate" and blocked:
            pool = "watch"
            reason = f"no usable DCF gap within {EXTREME_GAP_PCT:.0f}%; shown in the ranked list and not auto-sized"
        ranked.append(
            {
                "ticker": seed.ticker,
                "name": seed.name,
                "exchange": seed.exchange,
                "sector": seed.sector,
                "industry": seed.industry,
                "platforms": list(seed.platforms),
                "pool": pool,
                "reason": reason,
                "composite": composite_score,
                "platform_fit": platform,
                "value_investor": value,
                "innovation_growth": growth,
                "risk_adjusted_momentum": momentum,
                "formula": FORMULA_4COMP,
                "close": None if quote is None else quote.close,
                "close_date": None if quote is None else quote.close_date.isoformat(),
                "volume": None if quote is None else quote.volume,
                "median_dollar_volume": None if quote is None else quote.median_dollar_volume,
                "price_sessions": 0 if quote is None else quote.sessions,
                "factors": factors,
                "coverage_observed": len(observed),
                "coverage_gaps": gaps,
                "llm_invoked": False,
                "uncertainty": uncertainty,
            }
        )
    ranked.sort(key=lambda row: (-(row["composite"] if row["composite"] is not None else -1.0), row["ticker"]))
    for index, row in enumerate(ranked, start=1):
        row["rank"] = index
    return ranked


def propose_allocation(ranked: Sequence[Mapping[str, object]], *, cash: float, commission_bps: float = COMMISSION_BPS, slippage_bps: float = SLIPPAGE_BPS) -> dict:
    """Spend at most ``(1 - cash reserve)`` of cash. Caps are fractions of cash.

    Shares use the as-of close plus slippage and commission as a budget check.
    The order status stays pending: the next session's close is not known here.
    """
    if cash <= 0 or not math.isfinite(cash):
        raise ValueError("cash must be a finite number > 0")
    budget = cash * (1.0 - CASH_RESERVE)
    spent = 0.0
    sector_spent: dict[str, float] = {}
    orders: list[dict] = []
    candidates = [row for row in ranked if row["pool"] == "candidate" and isinstance(row.get("close"), (int, float)) and row["close"] > 0]
    for row in candidates:
        if len(orders) >= MAX_NAMES:
            break
        sector = str(row["sector"])
        room = min(cash * MAX_NAME_WEIGHT, cash * MAX_SECTOR_WEIGHT - sector_spent.get(sector, 0.0), budget - spent)
        if room < cash * MIN_ORDER_WEIGHT:
            continue
        close = float(row["close"])
        unit = buy_fill(close, slippage_bps) * (1.0 + commission_bps / 10_000.0)
        shares = int(math.floor(room / unit)) if unit > 0 else 0
        if shares < 1:
            continue
        notional = shares * buy_fill(close, slippage_bps)
        commission = notional * commission_bps / 10_000.0
        cost = notional + commission
        if spent + cost > budget + 1e-6:
            continue
        spent += cost
        sector_spent[sector] = sector_spent.get(sector, 0.0) + cost
        orders.append(
            {
                "ticker": row["ticker"],
                "side": "buy",
                "shares": shares,
                "sizing_close": close,
                "sizing_close_date": row["close_date"],
                "fill_price_estimate": buy_fill(close, slippage_bps),
                "estimated_cost": cost,
                "weight_of_cash": cost / cash,
                "sector": sector,
                "status": "pending_next_session",
                "holding": False,
                "rationale": row["reason"],
                "composite": row["composite"],
            }
        )
    cash_left = cash - spent
    if cash_left < cash * CASH_RESERVE - 0.05:
        raise RuntimeError("allocation spent the cash reserve")
    if spent - budget > 0.05:
        raise RuntimeError("allocation exceeded the risk budget")
    return {
        "currency": "USD",
        "initial_cash": cash,
        "cash_reserve_fraction": CASH_RESERVE,
        "max_name_weight": MAX_NAME_WEIGHT,
        "max_sector_weight": MAX_SECTOR_WEIGHT,
        "max_names": MAX_NAMES,
        "commission_bps": commission_bps,
        "slippage_bps": slippage_bps,
        "estimated_spent": spent,
        "cash_remaining": cash_left,
        "orders": orders,
        "note": "Shares are a pending proposal sized on the as-of close. They are not a holding and not a fill. The next session close is not in this file.",
    }


def render_markdown(payload: Mapping[str, object]) -> str:
    lines = [
        f"# Pool selection {payload['session_as_of']}",
        "",
        payload["disclaimer"],
        "",
        "## What this is",
        "",
        payload["methodology"],
        "",
        f"- Analysis timestamp (UTC): {payload['analysis_timestamp_utc']}",
        f"- Clock used for the session rule: {payload['clock_et']}",
        f"- Last completed US session: {payload['session_as_of']}",
        f"- Requested as-of: {payload['requested_as_of']}",
        f"- Universe: {payload['universe_path']} ({payload['universe_count']} names). Limited seed scan, not an all-market optimum.",
        f"- LLM invoked: {payload['llm_invoked']}",
        f"- Model: {payload['model']}",
        "",
        "## Allocation",
        "",
        f"- Cash: {payload['allocation']['initial_cash']:.2f} USD",
        f"- Estimated cost of pending buys: {payload['allocation']['estimated_spent']:.2f}",
        f"- Cash remaining: {payload['allocation']['cash_remaining']:.2f}",
        "",
        "| Ticker | Shares | Sizing close | Est. cost | Weight | Status |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    if not payload["allocation"]["orders"]:
        lines.append("| — | — | — | — | — | no candidate passed the rules; cash stays uninvested |")
    for order in payload["allocation"]["orders"]:
        lines.append(
            f"| {order['ticker']} | {order['shares']} | {order['sizing_close']:.4f} | {order['estimated_cost']:.2f} | {order['weight_of_cash']*100:.2f}% | {order['status']} |"
        )
    lines.extend(["", "## Ranked names", "", "| Rank | Ticker | Pool | Composite | Close date | Sector | Gaps | Reason |", "| ---: | --- | --- | ---: | --- | --- | --- | --- |"])
    for row in payload["rows"]:
        composite_text = "n/a" if row["composite"] is None else f"{row['composite']:.1f}"
        gaps = ", ".join(row["coverage_gaps"]) or "—"
        lines.append(f"| {row['rank']} | {row['ticker']} | {row['pool']} | {composite_text} | {row['close_date'] or 'n/a'} | {row['sector']} | {gaps} | {row['reason']} |")
    lines.extend(["", "## Limitations", ""])
    for item in payload["limitations"]:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def write_pool_files(out: Path, payload: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "pools.json").write_text(dump_json(payload), encoding="utf-8")
    (out / "pools.md").write_text(render_markdown(payload), encoding="utf-8")
    fields = ["rank", "ticker", "name", "pool", "composite", "close", "close_date", "sector", "platforms", "median_dollar_volume", "coverage_gaps", "reason"]
    with (out / "pools.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in payload["rows"]:
            writer.writerow(
                {
                    "rank": row["rank"],
                    "ticker": row["ticker"],
                    "name": row["name"],
                    "pool": row["pool"],
                    "composite": "" if row["composite"] is None else f"{row['composite']:.4f}",
                    "close": "" if row["close"] is None else row["close"],
                    "close_date": row["close_date"] or "",
                    "sector": row["sector"],
                    "platforms": ";".join(row["platforms"]),
                    "median_dollar_volume": "" if row["median_dollar_volume"] is None else f"{row['median_dollar_volume']:.2f}",
                    "coverage_gaps": ";".join(row["coverage_gaps"]),
                    "reason": row["reason"],
                }
            )
    with (out / "proposed_orders.csv").open("w", newline="", encoding="utf-8") as handle:
        order_fields = ["ticker", "side", "shares", "sizing_close", "sizing_close_date", "estimated_cost", "weight_of_cash", "status", "composite"]
        writer = csv.DictWriter(handle, fieldnames=order_fields, lineterminator="\n")
        writer.writeheader()
        for order in payload["allocation"]["orders"]:
            writer.writerow({key: order.get(key) for key in order_fields})


def quote_from_bars(bars: Sequence[tuple[date, float, float]], as_of: date) -> Quote | None:
    """bars are (session, close, volume) on or before as_of, any order."""
    usable = [(day, close, volume) for day, close, volume in bars if day <= as_of and close > 0 and math.isfinite(close)]
    if not usable:
        return None
    usable.sort(key=lambda item: item[0])
    window = usable[-LOOKBACK_SESSIONS:]
    last_day, last_close, last_volume = window[-1]
    dollars = [close * volume for _, close, volume in window if volume is not None and volume >= 0 and math.isfinite(volume)]
    median = None
    if dollars:
        ordered = sorted(dollars)
        mid = len(ordered) // 2
        median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0
    if last_day != as_of:
        # A name that did not trade on the as-of session is not priced for that decision.
        return Quote(last_close, last_day, last_volume, median, len(window))
    return Quote(last_close, last_day, last_volume, median, len(window))


def live_analyst(agent_key: str, ticker: str, as_of: date) -> dict | None:
    """Call one repository analyst. Exceptions become a missing factor, not a zero."""
    from datetime import timedelta as _timedelta

    starters = {
        "technical_analyst": ("src.agents.technicals", "technical_analyst_agent", "technical_analyst_agent"),
        "fundamentals_analyst": ("src.agents.fundamentals", "fundamentals_analyst_agent", "fundamentals_analyst_agent"),
        "valuation_analyst": ("src.agents.valuation", "valuation_analyst_agent", "valuation_analyst_agent"),
        "growth_analyst": ("src.agents.growth_agent", "growth_analyst_agent", "growth_analyst_agent"),
        "sentiment_analyst": ("src.agents.sentiment", "sentiment_analyst_agent", "sentiment_analyst_agent"),
    }
    module_name, func_name, agent_id = starters[agent_key]
    import importlib

    func = getattr(importlib.import_module(module_name), func_name)
    state = {
        "messages": [],
        "data": {
            "tickers": [ticker],
            "start_date": (as_of - _timedelta(days=400)).isoformat(),
            "end_date": as_of.isoformat(),
            "portfolio": {"cash": 0.0, "positions": {}},
            "analyst_signals": {},
        },
        "metadata": {"show_reasoning": False, "model_name": "none", "model_provider": "none"},
    }
    out = func(state)
    row = (out.get("data") or {}).get("analyst_signals", {}).get(agent_id, {}).get(ticker)
    if not isinstance(row, dict):
        return None
    return row


def fetch_spy_sessions(start: date, end: date) -> list[date]:
    from src.tools.api import get_prices

    rows = get_prices("SPY", start.isoformat(), end.isoformat())
    days = set()
    for row in rows:
        try:
            day = date.fromisoformat(str(row.time)[:10])
            close = float(row.close)
        except (TypeError, ValueError):
            continue
        if start <= day <= end and math.isfinite(close) and close > 0:
            days.add(day)
    return sorted(days)


def fetch_quotes(tickers: Sequence[str], start: date, end: date) -> dict[str, Quote | None]:
    from src.tools.api import get_prices

    quotes: dict[str, Quote | None] = {}
    for ticker in tickers:
        try:
            rows = get_prices(ticker, start.isoformat(), end.isoformat())
        except Exception as exc:  # noqa: BLE001 — one name's outage is a coverage gap
            print(f"pool_select: price fetch failed for {ticker}: {type(exc).__name__}: {exc}")
            quotes[ticker] = None
            continue
        bars: list[tuple[date, float, float]] = []
        for row in rows:
            try:
                day = date.fromisoformat(str(row.time)[:10])
                close = float(row.close)
                volume = float(row.volume)
            except (TypeError, ValueError):
                continue
            if start <= day <= end and math.isfinite(close) and close > 0:
                bars.append((day, close, volume))
        quotes[ticker] = quote_from_bars(bars, end)
    return quotes


def run_analysts(tickers: Sequence[str], as_of: date) -> dict[str, dict[str, dict | None]]:
    results: dict[str, dict[str, dict | None]] = {}
    for ticker in tickers:
        results[ticker] = {}
        for agent in ANALYSTS:
            try:
                results[ticker][agent] = live_analyst(agent, ticker, as_of)
            except Exception as exc:  # noqa: BLE001 — record the gap; do not invent a signal
                print(f"pool_select: {agent} failed for {ticker}: {type(exc).__name__}: {exc}")
                results[ticker][agent] = None
        observed = sum(1 for row in results[ticker].values() if row)
        print(f"pool_select: {ticker} analysts observed {observed}/{len(ANALYSTS)}")
    return results


def build_payload(*, names: Sequence[SeedName], quotes: Mapping[str, Quote | None], analysts: Mapping[str, Mapping[str, dict | None]], cash: float, session: date, requested: date | None, now: datetime, universe_path: str, spy_sessions: Sequence[date]) -> dict:
    from src.compliance import research_disclaimer

    text, version = research_disclaimer()
    ranked = rank_universe(names, quotes, analysts, session=session)
    # A price whose close_date is not the decision session cannot support an order.
    for row in ranked:
        if row["close_date"] not in (None, session.isoformat()) and row["pool"] == "candidate":
            row["pool"] = "watch"
            row["reason"] = f"close date {row['close_date']} is not the decision session {session.isoformat()}"
    allocation = propose_allocation(ranked, cash=cash)
    now_et = now.astimezone(MARKET_TZ) if now.tzinfo is not None else now.replace(tzinfo=MARKET_TZ)
    return {
        "disclaimer": text,
        "disclaimer_version": version,
        "analysis_timestamp_utc": now.astimezone(timezone_utc()).isoformat(),
        "clock_et": now_et.isoformat(),
        "requested_as_of": None if requested is None else requested.isoformat(),
        "session_as_of": session.isoformat(),
        "spy_sessions": [day.isoformat() for day in spy_sessions],
        "universe_path": universe_path,
        "universe_count": len(names),
        "universe_note": "Curated observing-pool seed. A limited scan of the names the repository already tracks. Not a search of every US listing and not an optimum.",
        "llm_invoked": False,
        "model": "none — persona LLMs and the portfolio manager were not run; no local model server, no GPU, and no API key",
        "formula": FORMULA_4COMP,
        "methodology": (
            "Each seed name is scored with the repository composite v3-4comp. "
            "platform_fit is the curated-seed confidence (0.9) and does not differentiate names inside this file. "
            "value_investor is the mean of the fundamentals and valuation analyst scores when those agents return a signal. "
            "innovation_growth is the growth analyst. risk_adjusted_momentum is the mean of the technical analyst and any sentiment analyst that had dated insider trades or directional news. "
            "signal_to_score maps bullish/neutral/bearish. A missing agent is omitted, and a name with no value_investor score is excluded rather than treated as zero. "
            f"candidate means composite >= {CANDIDATE_MIN:.0f} and median 20-session dollar volume >= {MIN_MEDIAN_DOLLAR_VOLUME:.0f}. "
            f"watch is {WATCH_MIN:.0f} to {CANDIDATE_MIN:.0f}, or a higher score that failed the liquidity screen. avoid is below {WATCH_MIN:.0f} with enough evidence to score. "
            f"Pending buys keep a {CASH_RESERVE:.0%} cash reserve, at most {MAX_NAME_WEIGHT:.0%} in one name, {MAX_SECTOR_WEIGHT:.0%} in one sector, and {MAX_NAMES} names. "
            f"A zero-confidence analyst label is not scored. A valuation gap wider than {EXTREME_GAP_PCT:.0f}% keeps the name visible and blocks an automatic order. "
            f"A leftover smaller than {MIN_ORDER_WEIGHT:.0%} of cash stays in cash."
        ),
        "rows": ranked,
        "allocation": allocation,
        "counts": {pool: sum(1 for row in ranked if row["pool"] == pool) for pool in ("candidate", "watch", "avoid", "excluded")},
        "limitations": [
            "No LLM and no portfolio-manager agent ran. Rule scores from the five LLM-free analysts are not full AI output.",
            "The universe is the observing-pool seed only. Names outside that file were not examined.",
            "Yahoo statement rows are filtered to period ends on or before the session date. This provider does not expose the SEC acceptance timestamp, so a statement fetched today could have been published after the session. Report periods are in the factor evidence; they are not a point-in-time filing database.",
            "The yfinance news adapter stores a null sentiment label, so headlines do not create bullish or bearish evidence. Sentiment counts only when dated insider transactions (or a directional label) exist on or before the session.",
            "A valuation-agent gap wider than 200% is the agent's own figure, not an audited appraisal. Those names stay in the ranked list and are not given an automatic order.",
            "Yahoo returned a temporary outage on some sentiment calls during this run. Those names record a missing sentiment factor rather than a neutral score.",
            "Daily bars do not encode an early close. A date with a SPY bar before today is treated as a completed session. Today's bar is ignored until 16:15 America/New_York.",
            "Proposed shares are not filled. The next regular session after this as-of date was not priced.",
            "Dollar volume uses unadjusted Yahoo close times volume. It is a liquidity screen, not a fundamental value.",
        ],
    }


def timezone_utc():
    from datetime import timezone

    return timezone.utc


def run_select(out: Path, *, universe: Path, cash: float, requested: date | None, now: datetime | None = None) -> dict:
    now = now or datetime.now(MARKET_TZ)
    lookback_start = (requested or now.date()) - timedelta(days=120)
    fetch_end = requested or now.date()
    # Never ask the provider for a date after the clock's ET date.
    now_et = now.astimezone(MARKET_TZ) if now.tzinfo is not None else now.replace(tzinfo=MARKET_TZ)
    if fetch_end > now_et.date():
        raise ValueError(f"as-of {fetch_end.isoformat()} is after the ET date {now_et.date().isoformat()}")
    sessions = fetch_spy_sessions(lookback_start, fetch_end)
    session = last_completed_session(sessions, now=now, requested=requested)
    names = load_seed(universe)
    price_start = session - timedelta(days=90)
    quotes = fetch_quotes([name.ticker for name in names], price_start, session)
    analysts = run_analysts([name.ticker for name in names], session)
    payload = build_payload(
        names=names,
        quotes=quotes,
        analysts=analysts,
        cash=cash,
        session=session,
        requested=requested,
        now=now,
        universe_path=str(universe),
        spy_sessions=[day for day in sessions if day <= session],
    )
    write_pool_files(out, payload)
    return payload
