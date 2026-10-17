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


def _snap(*positions) -> PoolSnapshot:
    positions = positions or (_pos(), _pos("BBB", degraded_reason="price fetch failed: RuntimeError: down"))
    return PoolSnapshot(
        as_of=date(2026, 1, 9),
        positions=positions,
        total_value=1050.0,
        prev_total_value=1000.0,
        daily_pct=5.0,
        baseline_value=1000.0,
        vs_baseline_pct=5.0,
        realized_pnl=12.5,
        net_cash_flow=-40.0,
        degraded=tuple(p.ticker for p in positions if p.degraded_reason),
        alerts=tuple((p.ticker, a) for p in positions for a in p.alerts),
    )


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
