"""Committee analyzing-flow tests (#51).

Pins the mean-score-band aggregation, the None/[]→full-committee default, the
selected_analysts→committee wiring (load-bearing), run-time fail-loud on an unknown
analyst id, and the single-degradation-path contract (the flow does not swallow —
run_monitor's guard degrades). All offline: a stub run_analysts replaces the real
scoring graph, so no LLM / network. Research-only: a label + confidence, never a trade.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.storage.models as m
from src.integrations.tradingagents_adapter import AnalyzingFlowResult
from src.monitoring.committee_flow import (
    _aggregate,
    _band,
    make_committee_analyzing_flow,
)
from src.monitoring.runner import create_monitor, run_monitor
from src.observing_pools.agents_bridge import committee_analyst_keys
from src.storage.models import ReportLabel


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    m.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _signals(ticker: str, per_key: dict[str, dict]) -> dict[str, dict[str, dict]]:
    """Build the run_scoring_analysts shape: {f'{key}_agent': {ticker: {...}}}."""
    return {f"{key}_agent": {ticker: raw} for key, raw in per_key.items()}


def _stub_run(returns: dict[str, dict[str, dict]]):
    """A run_analysts stub that records its (tickers, committee, end_date) calls."""
    calls: list[tuple[list[str], list[str], str]] = []

    def _run(tickers, selected_analysts, end_date):
        calls.append((list(tickers), list(selected_analysts), end_date))
        return returns, {}

    _run.calls = calls  # type: ignore[attr-defined]
    return _run


# ── aggregation: mean-score bands ───────────────────────────────────────────


def test_aggregate_all_bullish_is_supportive_with_mean_confidence():
    committee = ["warren_buffett", "cathie_wood"]
    sig = _signals(
        "NVDA",
        {
            "warren_buffett": {"signal": "bullish", "confidence": 80},
            "cathie_wood": {"signal": "bullish", "confidence": 80},
        },
    )
    res = _aggregate("NVDA", committee, sig)
    assert res.label == ReportLabel.THESIS_SUPPORTIVE
    assert res.degraded is False
    assert res.confidence == 80.0  # mean contributing confidence, not a band score
    assert res.raw_decision == ReportLabel.THESIS_SUPPORTIVE.value
    assert set(res.agent_reports) == {"warren_buffett", "cathie_wood"}
    assert all(r["degraded"] is False for r in res.agent_reports.values())


def test_aggregate_all_bearish_is_challenging():
    committee = ["warren_buffett", "michael_burry"]
    sig = _signals(
        "NVDA",
        {
            "warren_buffett": {"signal": "bearish", "confidence": 80},
            "michael_burry": {"signal": "bearish", "confidence": 80},
        },
    )
    res = _aggregate("NVDA", committee, sig)
    assert res.label == ReportLabel.THESIS_CHALLENGING
    assert res.degraded is False


def test_aggregate_balanced_is_mixed():
    committee = ["warren_buffett", "michael_burry"]
    sig = _signals(
        "NVDA",
        {
            "warren_buffett": {"signal": "bullish", "confidence": 80},  # score 90
            "michael_burry": {"signal": "bearish", "confidence": 80},  # score 10
        },
    )
    res = _aggregate("NVDA", committee, sig)  # mean 50 → MIXED
    assert res.label == ReportLabel.MIXED


@pytest.mark.parametrize(
    "signal,confidence,expected",
    [
        ("bullish", 20, ReportLabel.THESIS_SUPPORTIVE),  # score 60 == _SUPPORTIVE_AT (inclusive)
        ("bearish", 20, ReportLabel.THESIS_CHALLENGING),  # score 40 == _CHALLENGING_AT (inclusive)
        ("neutral", 50, ReportLabel.MIXED),  # score 50 strictly between bands
    ],
)
def test_aggregate_band_boundaries_are_inclusive(signal, confidence, expected):
    sig = _signals("NVDA", {"warren_buffett": {"signal": signal, "confidence": confidence}})
    res = _aggregate("NVDA", ["warren_buffett"], sig)
    assert res.label == expected


def test_band_thresholds_directly():
    assert _band(60.0) == ReportLabel.THESIS_SUPPORTIVE
    assert _band(59.99) == ReportLabel.MIXED
    assert _band(40.0) == ReportLabel.THESIS_CHALLENGING
    assert _band(40.01) == ReportLabel.MIXED
    assert _band(100.0) == ReportLabel.THESIS_SUPPORTIVE
    assert _band(0.0) == ReportLabel.THESIS_CHALLENGING


def test_aggregate_no_signals_is_insufficient_evidence_and_degraded():
    res = _aggregate("NVDA", ["warren_buffett", "cathie_wood"], {})
    assert res.label == ReportLabel.INSUFFICIENT_EVIDENCE
    assert res.degraded is True
    assert res.confidence == 0.0
    assert res.agent_reports == {}


def test_aggregate_all_signals_degraded_preserves_agent_reports():
    # Distinct from the no-signals case: signals ARE present but every one is unusable.
    # No valid score → INSUFFICIENT_EVIDENCE/degraded, but the per-analyst audit trail
    # (why each was dropped) must survive in agent_reports — operators need it.
    committee = ["warren_buffett", "cathie_wood"]
    sig = _signals(
        "NVDA",
        {
            "warren_buffett": {"signal": "garbage", "confidence": 80},
            "cathie_wood": {"signal": None, "confidence": 70},
        },
    )
    res = _aggregate("NVDA", committee, sig)
    assert res.label == ReportLabel.INSUFFICIENT_EVIDENCE
    assert res.degraded is True
    assert res.confidence == 0.0
    assert set(res.agent_reports) == {"warren_buffett", "cathie_wood"}  # populated, not dropped
    assert all(r["degraded"] is True for r in res.agent_reports.values())


def test_aggregate_partial_degradation_keeps_band_and_excludes_bad_analyst():
    committee = ["warren_buffett", "cathie_wood"]
    sig = _signals(
        "NVDA",
        {
            "warren_buffett": {"signal": "bullish", "confidence": 80},  # valid → score 90
            "cathie_wood": {"signal": "garbage", "confidence": 70},  # degraded → excluded
        },
    )
    res = _aggregate("NVDA", committee, sig)
    assert res.label == ReportLabel.THESIS_SUPPORTIVE  # mean over warren only (90)
    assert res.degraded is False  # partial degradation keeps the band
    assert res.confidence == 80.0  # only warren's confidence contributes
    assert res.agent_reports["cathie_wood"]["degraded"] is True
    assert res.agent_reports["cathie_wood"]["degraded_reason"] == "unknown_signal"
    assert res.agent_reports["cathie_wood"]["signal"] == "neutral"  # never a fabricated directional signal


def test_aggregate_clamps_out_of_range_confidence_into_report():
    # A valid signal with an out-of-range confidence passes _safe_agent_score (signal_to_score
    # raises only on a bad signal, not on a bad confidence). The reported confidence must still
    # be a real percentage — clamped to [0,100], matching the score path — never >100 or <0.
    committee = ["warren_buffett", "cathie_wood"]
    sig = _signals(
        "NVDA",
        {
            "warren_buffett": {"signal": "bullish", "confidence": 150},  # over-range
            "cathie_wood": {"signal": "bullish", "confidence": -20},  # under-range
        },
    )
    res = _aggregate("NVDA", committee, sig)
    assert 0.0 <= res.confidence <= 100.0  # would be 65.0 unclamped (mean of 150 & -20)
    assert res.confidence == 50.0  # clamped: mean of 100 and 0


def test_aggregate_omits_analysts_that_did_not_run():
    # committee has 3; only one produced a signal → mean over that one, others absent.
    committee = ["warren_buffett", "cathie_wood", "michael_burry"]
    sig = _signals("NVDA", {"warren_buffett": {"signal": "bullish", "confidence": 100}})
    res = _aggregate("NVDA", committee, sig)
    assert set(res.agent_reports) == {"warren_buffett"}  # not-run analysts omitted, not zeroed
    assert res.label == ReportLabel.THESIS_SUPPORTIVE


# ── factory: default committee + load-bearing selected_analysts ─────────────


def test_factory_none_runs_full_committee():
    committee = committee_analyst_keys()
    stub = _stub_run(_signals("NVDA", {k: {"signal": "bullish", "confidence": 80} for k in committee}))
    flow = make_committee_analyzing_flow(None, run_analysts=stub)
    res = flow("NVDA", "2026-06-12")
    assert res.label == ReportLabel.THESIS_SUPPORTIVE
    assert stub.calls[0][1] == committee  # the full blended committee was run
    assert len(stub.calls[0][1]) == 16


def test_factory_empty_list_runs_full_committee():
    committee = committee_analyst_keys()
    stub = _stub_run(_signals("NVDA", {k: {"signal": "bullish", "confidence": 80} for k in committee}))
    flow = make_committee_analyzing_flow([], run_analysts=stub)
    flow("NVDA", "2026-06-12")
    assert stub.calls[0][1] == committee  # [] mirrors None → full committee (pipeline.py parity)
    assert len(stub.calls[0][1]) == 16  # literal anchor: not just self-referential to committee_analyst_keys()


def test_factory_custom_list_is_load_bearing():
    stub = _stub_run(_signals("NVDA", {"warren_buffett": {"signal": "bullish", "confidence": 90}}))
    flow = make_committee_analyzing_flow(["warren_buffett"], run_analysts=stub)
    res = flow("NVDA", "2026-06-12")
    assert stub.calls[0][1] == ["warren_buffett"]  # exactly the selected committee, nothing else
    assert res.label == ReportLabel.THESIS_SUPPORTIVE


def test_factory_unknown_analyst_fails_loud_at_construction():
    # Run-time enforcement (#51) behind the route's write-boundary 422.
    with pytest.raises(ValueError, match="bogus_analyst"):
        make_committee_analyzing_flow(["warren_buffett", "bogus_analyst"])


def test_flow_does_not_swallow_runner_errors():
    # Single degradation path: the flow propagates; run_monitor's guard degrades.
    def _boom(tickers, selected_analysts, end_date):
        raise RuntimeError("LLM down")

    flow = make_committee_analyzing_flow(None, run_analysts=_boom)
    with pytest.raises(RuntimeError, match="LLM down"):
        flow("NVDA", "2026-06-12")


def test_default_committee_is_valid_against_registry():
    # Justifies skipping validation on the default path (it is valid by construction).
    from src.utils.analysts import ANALYST_CONFIG

    assert set(committee_analyst_keys()) <= set(ANALYST_CONFIG)


# ── wiring: run_monitor default → committee from monitor.selected_analysts ──


def test_run_monitor_defaults_to_committee_built_from_selected_analysts(session, monkeypatch):
    captured: dict = {}

    def fake_make(selected_analysts):
        captured["selected"] = selected_analysts
        return lambda t, d: AnalyzingFlowResult(t, ReportLabel.MIXED, 50.0, False, "committee stub")

    monkeypatch.setattr("src.monitoring.committee_flow.make_committee_analyzing_flow", fake_make)
    monitor = create_monitor(session, name="AI weekly", tickers=["NVDA"], selected_analysts=["warren_buffett"])
    result = run_monitor(session, monitor, trade_date="2026-06-12")  # no analyzing_flow injected

    assert captured["selected"] == ["warren_buffett"]  # monitor's selection drove the committee
    assert len(result.reports) == 1
    assert result.reports[0]["label"] == ReportLabel.MIXED.value


def test_route_default_flow_sentinel_is_none():
    # The route DI returns None so run_monitor builds the committee (not TradingAgents).
    from app.backend.routes.monitors import get_analyzing_flow

    assert get_analyzing_flow() is None
