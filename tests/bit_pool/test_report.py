"""Bit-pool report rendering: JSON + bilingual markdown, both disclaimer-stamped.

The research disclaimer is the compliance invariant of every report this repo emits;
a report without it must be impossible (blank disclaimer -> raise), not just unlikely.
"""

import json
from datetime import date

import pytest

from src.bit_pool import report
from src.bit_pool.report import CommitteeSignal, render_markdown, to_json
from src.bit_pool.simulate import PoolSnapshot, PositionSnapshot
from src.compliance import research_disclaimer

DISCLAIMER, VERSION = research_disclaimer()


def _pos(ticker="AAA", alerts=("DAILY_MOVE",), degraded_reason=None) -> PositionSnapshot:
    if degraded_reason:
        return PositionSnapshot(ticker, 3.0, None, None, None, None, None, None, 100.0, None, None, None, (), degraded_reason)
    return PositionSnapshot(ticker, 10.0, 105.0, 100.0, date(2026, 1, 9), 5.0, 1050.0, 1.0, 100.0, 5.0, 90.0, 150.0, alerts, None)


def _snap(*positions, **over) -> PoolSnapshot:
    positions = positions or (_pos(), _pos("BBB", degraded_reason="price fetch failed: RuntimeError: down"))
    fields = dict(
        as_of=date(2026, 1, 9),
        positions=positions,
        total_value=1050.0,
        prev_total_value=1000.0,
        daily_pct=5.0,
        baseline_value=1000.0,
        vs_baseline_pct=5.0,
        realized_pnl=12.5,
        realized_pnl_estimated=False,
        net_cash_flow=-40.0,
        prices_as_of=date(2026, 1, 9),
        degraded=tuple(p.ticker for p in positions if p.degraded_reason),
        alerts=tuple((p.ticker, a) for p in positions for a in p.alerts),
    )
    return PoolSnapshot(**{**fields, **over})


def _summary_lines(md: str) -> list[str]:
    start = md.index("## 持仓总览 Pool summary")
    return md[start : md.index("## 异动提醒 Alerts")].splitlines()


_COMMITTEE = (
    CommitteeSignal("AAA", "thesis-supportive", 66.7, 70.0, {"technical_analyst": "bullish", "growth_analyst": "neutral"}, None),
    CommitteeSignal("BBB", None, None, None, {}, "committee failed: RuntimeError: boom"),
)


def test_to_json_round_trips_and_carries_disclaimer():
    out = to_json(_snap(), committee=_COMMITTEE)
    text = json.dumps(out, allow_nan=False)
    back = json.loads(text)
    assert back["disclaimer"] == DISCLAIMER and back["disclaimer_version"] == VERSION
    assert back["as_of"] == "2026-01-09"
    assert back["positions"][0]["close_date"] == "2026-01-09"
    assert back["positions"][0]["alerts"] == ["DAILY_MOVE"]
    assert back["degraded"] == ["BBB"]
    assert back["committee"][1]["degraded_reason"].startswith("committee failed")
    assert back["research_only"] is True


def test_to_json_without_committee_is_null():
    assert to_json(_snap())["committee"] is None


def test_markdown_has_bilingual_sections_tables_and_disclaimer_last():
    md = render_markdown(_snap(), committee=_COMMITTEE)
    for heading in ("## 持仓总览 Pool summary", "## 异动提醒 Alerts", "## 委员会信号 Committee signals", "## 数据降级 Degraded"):
        assert heading in md
    assert "| AAA |" in md and "+5.00%" in md and "DAILY_MOVE" in md
    assert "thesis-supportive" in md and "technical_analyst: bullish" in md
    assert "BBB" in md and "price fetch failed" in md and "committee failed" in md
    # Disclaimer closes the report: after every content section, and nothing but it follows.
    assert md.index(DISCLAIMER) > md.index("## 数据降级 Degraded")
    assert "## " not in md[md.index(DISCLAIMER) :]


def test_markdown_without_committee_says_not_run():
    md = render_markdown(_snap())
    assert "## 委员会信号 Committee signals" in md
    assert DISCLAIMER in md


def test_blank_disclaimer_raises(monkeypatch):
    monkeypatch.setattr(report, "research_disclaimer", lambda: ("   ", "v"))
    with pytest.raises(ValueError, match="disclaimer"):
        to_json(_snap())
    with pytest.raises(ValueError, match="disclaimer"):
        render_markdown(_snap())


# ── book label + committee provenance (two books: owner's fills vs Muse's paper orders) ──
def test_to_json_defaults_to_real_book_with_no_committee_provenance():
    out = to_json(_snap())
    assert (out["book"], out["committee_source"], out["committee_signals_as_of"]) == ("real", None, None)


def test_to_json_carries_label_source_and_signals_as_of():
    out = json.loads(json.dumps(to_json(_snap(), _COMMITTEE, label="muse", committee_source="muse", signals_as_of=date(2026, 1, 8))))
    assert (out["book"], out["committee_source"], out["committee_signals_as_of"]) == ("muse", "muse", "2026-01-08")
    assert out["disclaimer"] == DISCLAIMER


def test_markdown_title_names_the_book_and_muse_line_only_for_muse_source():
    md = render_markdown(_snap(), _COMMITTEE, label="muse", committee_source="muse", signals_as_of=date(2026, 1, 8))
    assert md.splitlines()[0] == "# Bit Pool 每日模拟 Daily simulation — 2026-01-09 — book: muse"
    assert "Committee: Muse agent signals as of 2026-01-08" in md
    assert md.index(DISCLAIMER) > md.index("Committee: Muse agent")
    core = render_markdown(_snap(), _COMMITTEE, committee_source="core")
    assert core.splitlines()[0].endswith("— book: real")
    assert "Muse agent" not in core


# ── partial pools: a total that excludes positions must say so ──
def test_partial_pool_is_flagged_in_json_and_first_summary_line():
    snap = _snap(_pos(), _pos("BBB", degraded_reason="down"), _pos("CCC", degraded_reason="down"))
    out = to_json(snap)
    assert out["partial"] is True and out["excluded_tickers"] == ["BBB", "CCC"]
    first = [ln for ln in _summary_lines(render_markdown(snap)) if ln.strip()][1]
    assert "PARTIAL — excludes BBB, CCC" in first


def test_complete_pool_is_not_partial():
    snap = _snap(_pos())
    assert (to_json(snap)["partial"], to_json(snap)["excluded_tickers"]) == (False, [])
    assert "PARTIAL" not in render_markdown(snap)


def test_all_degraded_pool_renders_na_not_zero():
    snap = _snap(_pos("BBB", degraded_reason="down"), total_value=None, prev_total_value=None, baseline_value=None, daily_pct=None, vs_baseline_pct=None, prices_as_of=None)
    out = json.loads(json.dumps(to_json(snap), allow_nan=False))
    assert (out["total_value"], out["prev_total_value"], out["baseline_value"], out["daily_pct"], out["vs_baseline_pct"]) == (None,) * 5
    summary = "\n".join(_summary_lines(render_markdown(snap)))
    for label in ("Total value", "Previous value", "Daily change", "Baseline value", "vs baseline", "Prices as of"):
        line = next(ln for ln in summary.splitlines() if label in ln)
        assert line.rstrip().endswith("n/a"), line


# ── freshness ──
def test_markdown_shows_close_date_column_and_prices_as_of():
    md = render_markdown(_snap(_pos()))
    assert "| Close date 收盘日 |" in md
    row = next(ln for ln in md.splitlines() if ln.startswith("| AAA |"))
    assert "| 2026-01-09 |" in row
    assert "- 价格日期 Prices as of: 2026-01-09" in md
    assert "latest available close" not in md


def test_prices_older_than_as_of_are_called_out():
    md = render_markdown(_snap(_pos(), prices_as_of=date(2026, 1, 8)))
    assert "- 价格日期 Prices as of: 2026-01-08 (latest available close; as-of date has no close yet)" in md


def test_json_carries_prices_as_of():
    assert to_json(_snap(_pos(), prices_as_of=date(2026, 1, 8)))["prices_as_of"] == "2026-01-08"


# ── realized P&L against baseline (cost unknown) is labelled an estimate ──
def test_estimated_realized_pnl_is_labelled():
    snap = _snap(_pos(), realized_pnl_estimated=True)
    assert to_json(snap)["realized_pnl_estimated"] is True
    line = next(ln for ln in render_markdown(snap).splitlines() if "Realized P&L" in ln)
    assert line.endswith("12.50 (vs baseline price — cost unknown)")
    assert "cost unknown" not in render_markdown(_snap(_pos()))


# ── trades provenance ──
def test_trades_applied_and_source_in_json_and_markdown():
    out = to_json(_snap(_pos()), trades_source="data/t.csv", trades_applied=3)
    assert (out["trades_source"], out["trades_applied"]) == ("data/t.csv", 3)
    assert "Trades applied: 3 (data/t.csv)" in render_markdown(_snap(_pos()), trades_source="data/t.csv", trades_applied=3)
    none = to_json(_snap(_pos()))
    assert (none["trades_source"], none["trades_applied"]) == (None, 0)
    assert "Trades applied: 0 (no ledger)" in render_markdown(_snap(_pos()))


# ── committee coverage + visibly degraded analysts ──
def _partial_committee():
    return (CommitteeSignal("AAA", "thesis-supportive", 90.0, 80.0, {"technical_analyst": "bullish", "growth_analyst": "neutral"}, None, coverage="1/2", degraded_analysts=("growth_analyst",)),)


def test_degraded_analyst_is_marked_in_json_and_markdown():
    out = json.loads(json.dumps(to_json(_snap(_pos()), _partial_committee())))
    row = out["committee"][0]
    assert row["analysts"] == {"technical_analyst": "bullish", "growth_analyst": {"signal": "neutral", "degraded": True}}
    assert row["coverage"] == "1/2"
    md = render_markdown(_snap(_pos()), _partial_committee())
    assert "growth_analyst: neutral (DEGRADED)" in md
    assert "technical_analyst: bullish;" in md
    assert "| 1/2 |" in md and "| Coverage |" in md


def test_cell_escapes_pipes_and_newlines():
    assert report._cell("a|b\nc\r\nd") == "a\\|b\\nc\\nd"
    bad = (CommitteeSignal("AAA", None, None, None, {}, "boom\nsecond line | x", coverage="0/5"),)
    md = render_markdown(_snap(_pos()), bad)
    row = next(ln for ln in md.splitlines() if ln.startswith("| AAA |") and "DEGRADED" in ln)
    assert "second line" in row  # still one table row
