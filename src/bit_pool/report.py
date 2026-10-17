"""Bit-pool report rendering: JSON dict + bilingual (zh/en) markdown.

Both forms carry the research disclaimer from ``src.compliance.research_disclaimer``
(the single source of its text/version); a blank disclaimer raises rather than
emitting an unstamped report. Reports contain tickers and numbers only — no account
numbers or personal identifiers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

from src.bit_pool.simulate import PoolSnapshot, PositionSnapshot
from src.compliance import research_disclaimer

_NONE = "无 None"


@dataclass(frozen=True)
class CommitteeSignal:
    """One ticker's committee outcome (descriptive band, never a trade instruction)."""

    ticker: str
    band: str | None  # ReportLabel value, e.g. "thesis-supportive"; None when degraded
    mean_score: float | None  # mean 0-100 score over non-degraded analysts
    confidence: float | None
    analysts: dict[str, str]  # analyst key -> signal
    degraded_reason: str | None


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


def to_json(
    snapshot: PoolSnapshot,
    committee: tuple[CommitteeSignal, ...] | None = None,
    *,
    label: str = "real",
    committee_source: str | None = None,
    signals_as_of: date | None = None,
) -> dict:
    """JSON-serializable report dict (dates as ISO strings), disclaimer-stamped.

    ``label`` names the book (e.g. ``real`` = owner's fills, ``muse`` = Muse's paper
    orders); ``committee_source`` is ``core|full|muse`` or None; ``signals_as_of`` is
    the Muse signals date when the source is ``muse``.
    """
    text, version = _disclaimer()
    body = _jsonable(asdict(snapshot))
    body["book"] = label
    body["committee_source"] = committee_source
    body["committee_signals_as_of"] = _jsonable(signals_as_of)
    body["alerts"] = [{"ticker": t, "alert": a} for t, a in snapshot.alerts]
    body["committee"] = None if committee is None else [_jsonable(asdict(c)) for c in committee]
    body["research_only"] = True
    body["disclaimer"] = text
    body["disclaimer_version"] = version
    return body


def _num(value: float | None, fmt: str = ",.2f") -> str:
    return "—" if value is None else format(value, fmt)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}%"


def _shares(value: float) -> str:
    return f"{value:,.6f}".rstrip("0").rstrip(".")


def _cell(text: str) -> str:
    """Escape ``|`` so free-text (e.g. an error message) cannot break a table row."""
    return text.replace("|", "\\|")


def _position_row(p: PositionSnapshot) -> str:
    weight = None if p.weight is None else p.weight * 100
    cells = [p.ticker, _shares(p.shares), _num(p.close), _pct(p.daily_pct), _num(p.market_value), _num(weight) + ("%" if weight is not None else ""), _pct(p.vs_baseline_pct), ", ".join(p.alerts) or ("DEGRADED" if p.degraded_reason else "")]
    return "| " + " | ".join(cells) + " |"


def _summary(s: PoolSnapshot) -> list[str]:
    return [
        "## 持仓总览 Pool summary",
        "",
        f"- 总市值 Total value: {_num(s.total_value)}",
        f"- 前一交易日市值 Previous value: {_num(s.prev_total_value)}",
        f"- 日涨跌 Daily change: {_pct(s.daily_pct)}",
        f"- 基准市值 Baseline value: {_num(s.baseline_value)}",
        f"- 相对基准 vs baseline: {_pct(s.vs_baseline_pct)}",
        f"- 已实现盈亏 Realized P&L: {_num(s.realized_pnl)}",
        f"- 净现金流 Net cash flow: {_num(s.net_cash_flow)}",
        "",
        "| Ticker | Shares | Close | Daily % | Value | Weight | vs Baseline % | Alerts |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
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
    rows = ["| Ticker | Band | Mean score | Analyst signals |", "|---|---|---:|---|"]
    for c in committee:
        band = c.band or f"DEGRADED ({c.degraded_reason})"
        signals = "; ".join(f"{k}: {v}" for k, v in c.analysts.items()) or "—"
        rows.append(f"| {c.ticker} | {_cell(band)} | {_num(c.mean_score, '.1f')} | {_cell(signals)} |")
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
) -> str:
    """Bilingual markdown report; the disclaimer is always the closing section."""
    text, version = _disclaimer()
    lines = [
        f"# Bit Pool 每日模拟 Daily simulation — {snapshot.as_of.isoformat()} — book: {label}",
        "",
        "> 研究用纸面模拟，不下单 Research-only paper simulation; no orders are placed.",
        "",
        *_summary(snapshot),
        *_alerts(snapshot),
        *_committee(committee, committee_source, signals_as_of),
        *_degraded(snapshot, committee),
        "---",
        "",
        f"免责声明 Disclaimer ({version}): {text}",
        "",
    ]
    return "\n".join(lines)
