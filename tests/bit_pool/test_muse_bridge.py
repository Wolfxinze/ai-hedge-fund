"""Muse bridge: the external Muse agent's signals file → committee runner input.

Muse (an external manager agent) writes ``signals/YYYY-MM-DD.json``; this repo only
consumes it. Every malformed entry must fail LOUD naming the analyst/ticker — a
silently dropped or coerced signal would change a committee band without anyone
noticing. Staleness and date resolution decide whether a run is degraded, so their
boundaries (4 vs 5 days, on-or-before) are pinned exactly. Offline: tmp_path files
and the real analyst registry.
"""

import json
from datetime import date

import pytest

from src.bit_pool import muse_bridge as mb
from src.monitoring.committee_flow import make_committee_analyzing_flow
from src.observing_pools.agents_bridge import committee_analyst_keys
from src.storage.models import ReportLabel


def _write(tmp_path, doc, name="2026-01-09.json"):
    p = tmp_path / name
    p.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    return p


def _doc(signals=None, as_of="2026-01-09"):
    if signals is None:
        signals = {"warren_buffett": {"AAA": {"signal": "bullish", "confidence": 80, "reasoning": "moat"}}}
    return {"as_of": as_of, "signals": signals}


def _entry(**over):
    return {"warren_buffett": {"AAA": {"signal": "bullish", "confidence": 80, **over}}}


# ── load_muse_signals ──
def test_valid_file_loads_with_bare_analyst_keys(tmp_path):
    p = _write(tmp_path, _doc({"warren_buffett": {"AAA": {"signal": "bullish", "confidence": 80}}, "technical_analyst": {"BRK.B": {"signal": "neutral", "confidence": 0, "reasoning": "flat"}}}))
    muse = mb.load_muse_signals(p)
    assert muse.as_of == date(2026, 1, 9)
    assert muse.source_path == str(p)
    assert muse.signals["warren_buffett"]["AAA"]["signal"] == "bullish"
    assert muse.signals["warren_buffett"]["AAA"]["confidence"] == 80
    assert muse.signals["technical_analyst"]["BRK.B"]["reasoning"] == "flat"


def test_signals_are_immutable(tmp_path):
    muse = mb.load_muse_signals(_write(tmp_path, _doc()))
    with pytest.raises(TypeError):
        muse.signals["warren_buffett"] = {}


@pytest.mark.parametrize(
    ("doc", "needle"),
    [
        ("{not json", "JSON"),
        ({"signals": {}}, "as_of"),
        ({"as_of": "09/01/2026", "signals": {}}, "as_of"),
        ({"as_of": "2026-01-09", "signals": []}, "signals"),
        (_doc({"warren_buffett_agent": {"AAA": {"signal": "bullish", "confidence": 1}}}), "warren_buffett_agent"),
        (_doc({"nobody": {"AAA": {"signal": "bullish", "confidence": 1}}}), "nobody"),
        (_doc({"warren_buffett": {"aaa": {"signal": "bullish", "confidence": 1}}}), "aaa"),
        (_doc({"warren_buffett": {"TOOLONGTICKER": {"signal": "bullish", "confidence": 1}}}), "TOOLONGTICKER"),
        (_doc(_entry(signal="buy")), "AAA"),
        (_doc({"warren_buffett": {"AAA": {"signal": "bullish"}}}), "AAA"),
        (_doc(_entry(confidence="80")), "AAA"),
        (_doc(_entry(confidence=True)), "AAA"),
        (_doc(_entry(confidence=-0.1)), "AAA"),
        (_doc(_entry(confidence=100.5)), "AAA"),
        # Python's json accepts these non-standard tokens; NaN would slip past a naive
        # range check and poison the committee mean.
        ('{"as_of": "2026-01-09", "signals": {"warren_buffett": {"AAA": {"signal": "bullish", "confidence": NaN}}}}', "AAA"),
        ('{"as_of": "2026-01-09", "signals": {"warren_buffett": {"AAA": {"signal": "bullish", "confidence": Infinity}}}}', "AAA"),
        ('{"as_of": "2026-01-09", "signals": {"warren_buffett": {"AAA": {"signal": "bullish", "confidence": -Infinity}}}}', "AAA"),
    ],
    ids=["bad-json", "missing-as_of", "bad-as_of", "signals-not-dict", "agent-suffix", "unknown-analyst", "lowercase-ticker", "long-ticker", "bad-signal", "missing-confidence", "string-confidence", "bool-confidence", "negative-confidence", "over-100-confidence", "nan-confidence", "infinity-confidence", "neg-infinity-confidence"],
)
def test_invalid_files_raise_naming_the_offender(tmp_path, doc, needle):
    with pytest.raises(ValueError, match=needle):
        mb.load_muse_signals(_write(tmp_path, doc))


def test_bad_entry_error_names_analyst_and_ticker(tmp_path):
    with pytest.raises(ValueError) as exc:
        mb.load_muse_signals(_write(tmp_path, _doc(_entry(signal="buy"))))
    assert "warren_buffett" in str(exc.value) and "AAA" in str(exc.value)


def test_confidence_bounds_are_inclusive(tmp_path):
    for c in (0, 100, 55.5):
        assert mb.load_muse_signals(_write(tmp_path, _doc(_entry(confidence=c)))).signals["warren_buffett"]["AAA"]["confidence"] == c


# ── resolve_muse_signals ──
def test_resolve_returns_a_file_path_unchanged(tmp_path):
    p = _write(tmp_path, _doc(), name="anything.json")
    assert mb.resolve_muse_signals(str(p), date(2020, 1, 1)) == p


def test_resolve_picks_newest_on_or_before_and_ignores_later_and_foreign_names(tmp_path):
    for name in ("2026-01-05.json", "2026-01-08.json", "2026-01-09.json", "2026-01-10.json", "notes.json", "2026-01-09.json.bak", "2026-13-01.json"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    assert mb.resolve_muse_signals(str(tmp_path), date(2026, 1, 9)) == tmp_path / "2026-01-09.json"
    assert mb.resolve_muse_signals(str(tmp_path), date(2026, 1, 8)) == tmp_path / "2026-01-08.json"
    assert mb.resolve_muse_signals(str(tmp_path), date(2026, 1, 7)) == tmp_path / "2026-01-05.json"


def test_resolve_returns_none_when_nothing_qualifies(tmp_path):
    (tmp_path / "2026-01-10.json").write_text("{}", encoding="utf-8")
    (tmp_path / "readme.json").write_text("{}", encoding="utf-8")
    assert mb.resolve_muse_signals(str(tmp_path), date(2026, 1, 9)) is None


def test_resolve_missing_path_fails_loud(tmp_path):
    with pytest.raises(OSError):
        mb.resolve_muse_signals(str(tmp_path / "nope"), date(2026, 1, 9))


# ── staleness ──
def test_stale_boundary_is_more_than_four_days(tmp_path):
    assert mb.MUSE_STALE_DAYS == 4
    muse = mb.load_muse_signals(_write(tmp_path, _doc(as_of="2026-01-05")))
    assert mb.is_stale(muse, date(2026, 1, 9)) is False  # exactly 4 days: fresh
    assert mb.is_stale(muse, date(2026, 1, 10)) is True  # 5 days: stale
    assert mb.is_stale(muse, date(2026, 1, 5)) is False


def test_future_dated_signals_raise(tmp_path):
    muse = mb.load_muse_signals(_write(tmp_path, _doc(as_of="2026-01-10")))
    with pytest.raises(ValueError, match="2026-01-10"):
        mb.is_stale(muse, date(2026, 1, 9))


# ── runner ──
def test_runner_returns_agent_suffixed_signals_for_requested_only_and_no_cost(tmp_path):
    p = _write(tmp_path, _doc({"warren_buffett": {"AAA": {"signal": "bullish", "confidence": 80}, "BBB": {"signal": "bearish", "confidence": 70}}, "technical_analyst": {"BBB": {"signal": "neutral", "confidence": 50}}}))
    runner = mb.make_muse_runner(mb.load_muse_signals(p))
    signals, cost = runner(["AAA"], ["warren_buffett", "technical_analyst", "ben_graham"], "2026-01-09")
    assert cost == {}
    assert signals == {"warren_buffett_agent": {"AAA": {"signal": "bullish", "confidence": 80, "reasoning": ""}}}


def test_runner_through_committee_flow_bands(tmp_path):
    keys = committee_analyst_keys()[:4]
    bull = {"signal": "bullish", "confidence": 90}
    bear = {"signal": "bearish", "confidence": 90}
    muse = mb.load_muse_signals(_write(tmp_path, _doc({k: {"AAA": bull, "BBB": bear} for k in keys})))
    flow = make_committee_analyzing_flow(list(muse.signals), run_analysts=mb.make_muse_runner(muse))
    assert flow("AAA", "2026-01-09").label is ReportLabel.THESIS_SUPPORTIVE
    assert flow("BBB", "2026-01-09").label is ReportLabel.THESIS_CHALLENGING
    absent = flow("CCC", "2026-01-09")
    assert absent.label is ReportLabel.INSUFFICIENT_EVIDENCE and absent.degraded is True
