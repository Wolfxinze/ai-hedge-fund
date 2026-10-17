"""Bit-pool report rendering: JSON dict + bilingual (zh/en) markdown.

Both forms carry the research disclaimer from ``src.compliance.research_disclaimer``
(the single source of its text/version); a blank disclaimer raises rather than
emitting an unstamped report. Reports contain tickers and numbers only — no account
numbers or personal identifiers.

Fail-loud rendering: a pool that excludes degraded positions is marked PARTIAL (JSON
``partial`` / ``excluded_tickers``), unknown totals render ``n/a`` (never 0.00), the
close date behind every number is shown, realized P&L measured against baseline
price is labelled an estimate, and degraded committee analysts are marked as such.
Equity (positions + cash) and its return vs the original holdings' capital are shown
next to the positions-only total; on a PARTIAL pool they are labelled PARTIAL too and
the return is n/a (initial capital covers positions the equity cannot price). Each
position shows its weight of positions and of equity.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

from src.bit_pool.simulate import PoolSnapshot, PositionSnapshot
from src.compliance import research_disclaimer

_NONE = "无 None"
_NA = "n/a"


@dataclass(frozen=True)
class CommitteeSignal:
    """One ticker's committee outcome (descriptive band, never a trade instruction)."""

    ticker: str
    band: str | None  # ReportLabel value, e.g. "thesis-supportive"; None when degraded
    mean_score: float | None  # mean 0-100 score over non-degraded analysts
    confidence: float | None
    analysts: dict[str, str]  # analyst key -> signal
    degraded_reason: str | None
    coverage: str | None = None  # "valid analysts/committee size", e.g. "4/5"
    degraded_analysts: tuple[str, ...] = ()  # analysts the flow marked degraded (signal forced neutral)


def _disclaimer() -> tuple[str, str]:
    text, version = research_disclaimer()
    if not (text or "").strip():
        raise ValueError("research disclaimer is blank — refusing to emit an unstamped bit-pool report")
    return text, version


def _jsonable(value):
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def _committee_json(c: CommitteeSignal) -> dict:
    """A degraded analyst becomes ``{"signal": ..., "degraded": true}`` — never a bare 'neutral'."""
    row = _jsonable(asdict(c))
    row["analysts"] = {k: ({"signal": v, "degraded": True} if k in c.degraded_analysts else v) for k, v in c.analysts.items()}
    return row


def to_json(
    snapshot: PoolSnapshot,
    committee: tuple[CommitteeSignal, ...] | None = None,
    *,
    label: str = "real",
    committee_source: str | None = None,
    signals_as_of: date | None = None,
    trades_source: str | None = None,
    trades_applied: int = 0,
) -> dict:
    """JSON-serializable report dict (dates as ISO strings), disclaimer-stamped.

    ``label`` names the book (e.g. ``real`` = owner's fills, ``muse`` = Muse's paper
    orders); ``committee_source`` is ``core|full|muse`` or None; ``signals_as_of`` is
    the Muse signals date when the source is ``muse``. ``trades_source`` is the ledger
    path read (None when no ledger) and ``trades_applied`` the rows dated <= as_of.
    """
    text, version = _disclaimer()
    body = _jsonable(asdict(snapshot))
    body["cash"] = snapshot.cash
    body["equity"] = snapshot.equity
    body["equity_vs_initial_pct"] = snapshot.equity_vs_initial_pct
    body["book"] = label
    body["partial"] = bool(snapshot.degraded)
    body["excluded_tickers"] = list(snapshot.degraded)
    body["trades_source"] = trades_source
    body["trades_applied"] = trades_applied
    body["committee_source"] = committee_source
    body["committee_signals_as_of"] = _jsonable(signals_as_of)
    body["alerts"] = [{"ticker": t, "alert": a} for t, a in snapshot.alerts]
    body["committee"] = None if committee is None else [_committee_json(c) for c in committee]
    body["research_only"] = True
    body["disclaimer"] = text
    body["disclaimer_version"] = version
    return body


def _num(value: float | None, fmt: str = ",.2f", none: str = "—") -> str:
    return none if value is None else format(value, fmt)


def _cash(value: float) -> str:
    """Cash with float residue (|x| < half a cent) shown as 0.00, never ``-0.00``."""
    return _num(0.0 if abs(value) < 0.005 else value)


def _weight(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:,.2f}%"


def _pct(value: float | None, none: str = "—") -> str:
    return none if value is None else f"{value:+.2f}%"


def _shares(value: float) -> str:
    return f"{value:,.6f}".rstrip("0").rstrip(".")


def _cell(text: str) -> str:
    """Escape ``|`` and newlines so free-text (e.g. an error message) cannot break a table row."""
    return text.replace("|", "\\|").replace("\r\n", "\\n").replace("\r", "\\n").replace("\n", "\\n")


def _position_row(p: PositionSnapshot) -> str:
    close_date = p.close_date.isoformat() if p.close_date else "—"
    cells = [p.ticker, _shares(p.shares), _num(p.close), close_date, _pct(p.daily_pct), _num(p.market_value), _weight(p.weight), _weight(p.weight_of_equity), _pct(p.vs_baseline_pct), ", ".join(p.alerts) or ("DEGRADED" if p.degraded_reason else "")]
    return "| " + " | ".join(cells) + " |"


def _prices_as_of_line(s: PoolSnapshot) -> str:
    if s.prices_as_of is None:
        return f"- 价格日期 Prices as of: {_NA}"
    note = " (latest available close; as-of date has no close yet)" if s.prices_as_of < s.as_of else ""
    return f"- 价格日期 Prices as of: {s.prices_as_of.isoformat()}{note}"


def _summary(s: PoolSnapshot, trades_source: str | None, trades_applied: int) -> list[str]:
    partial = [f"- **PARTIAL — excludes {', '.join(s.degraded)}** (totals cover priced positions only)"] if s.degraded else []
    estimated = " (vs baseline price — cost unknown)" if s.realized_pnl_estimated else ""
    # Equity covers priced positions only when some are degraded; n/a needs no label.
    # Its return is n/a then (capital covers every position), but still marked PARTIAL.
    partial_eq = " (PARTIAL)" if s.degraded and s.equity is not None else ""
    return [
        "## 持仓总览 Pool summary",
        "",
        *partial,
        _prices_as_of_line(s),
        f"- 总市值 Total value: {_num(s.total_value, none=_NA)}",
        f"- 账户权益 Equity (positions + cash): {_num(s.equity, none=_NA)}{partial_eq}",
        f"- 现金 Cash: {_cash(s.cash)}",
        f"- 初始资本 Initial capital: {_num(s.initial_capital, none=_NA)}",
        f"- 权益收益 Equity vs initial: {_pct(s.equity_vs_initial_pct, none=_NA)}{partial_eq}",
        f"- 前一交易日市值 Previous value: {_num(s.prev_total_value, none=_NA)}",
        f"- 日涨跌 Daily change: {_pct(s.daily_pct, none=_NA)}",
        f"- 基准市值 Baseline value: {_num(s.baseline_value, none=_NA)}",
        f"- 相对基准 vs baseline: {_pct(s.vs_baseline_pct, none=_NA)}",
        f"- 已实现盈亏 Realized P&L: {_num(s.realized_pnl)}{estimated}",
        f"- 净现金流 Net cash flow: {_cash(s.net_cash_flow)}",
        f"- 已应用交易 Trades applied: {trades_applied} ({trades_source or 'no ledger'})",
        "",
        "| Ticker | Shares | Close | Close date 收盘日 | Daily % | Value | Weight (positions) | Weight (equity) 占权益 | vs Baseline % | Alerts |",
        "|---|---:|---:|---|---:|---:|---:|---:|---:|---|",
        *[_position_row(p) for p in s.positions],
        "",
    ]


def _alerts(s: PoolSnapshot) -> list[str]:
    lines = [f"- {t}: {a}" for t, a in s.alerts] or [_NONE]
    return ["## 异动提醒 Alerts", "", *lines, ""]


def _committee(committee: tuple[CommitteeSignal, ...] | None, source: str | None, signals_as_of: date | None) -> list[str]:
    head = ["## 委员会信号 Committee signals", ""]
    if source == "muse":
        head += [f"Committee: Muse agent signals as of {signals_as_of.isoformat() if signals_as_of else '—'}", ""]
    if committee is None:
        return [*head, "未运行 Not run (--committee none)", ""]
    rows = ["| Ticker | Band | Mean score | Coverage | Analyst signals |", "|---|---|---:|---:|---|"]
    for c in committee:
        band = c.band or f"DEGRADED ({c.degraded_reason})"
        signals = "; ".join(f"{k}: {v}" + (" (DEGRADED)" if k in c.degraded_analysts else "") for k, v in c.analysts.items()) or "—"
        rows.append(f"| {c.ticker} | {_cell(band)} | {_num(c.mean_score, '.1f')} | {c.coverage or '—'} | {_cell(signals)} |")
    return [*head, *rows, ""]


def _degraded(s: PoolSnapshot, committee: tuple[CommitteeSignal, ...] | None) -> list[str]:
    lines = [f"- {p.ticker} (price): {p.degraded_reason}" for p in s.positions if p.degraded_reason]
    lines += [f"- {c.ticker} (committee): {c.degraded_reason}" for c in committee or () if c.degraded_reason]
    return ["## 数据降级 Degraded", "", *(lines or [_NONE]), ""]


def render_markdown(
    snapshot: PoolSnapshot,
    committee: tuple[CommitteeSignal, ...] | None = None,
    *,
    label: str = "real",
    committee_source: str | None = None,
    signals_as_of: date | None = None,
    trades_source: str | None = None,
    trades_applied: int = 0,
) -> str:
    """Bilingual markdown report; the disclaimer is always the closing section."""
    text, version = _disclaimer()
    lines = [
        f"# Bit Pool 每日模拟 Daily simulation — {snapshot.as_of.isoformat()} — book: {label}",
        "",
        "> 研究用纸面模拟，不下单 Research-only paper simulation; no orders are placed.",
        "",
        *_summary(snapshot, trades_source, trades_applied),
        *_alerts(snapshot),
        *_committee(committee, committee_source, signals_as_of),
        *_degraded(snapshot, committee),
        "---",
        "",
        f"免责声明 Disclaimer ({version}): {text}",
        "",
    ]
    return "\n".join(lines)
