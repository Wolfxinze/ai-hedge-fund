"""Committee analyzing flow for monitors (#51).

Builds the ai-hedge-fund scoring committee as a monitor's analyzing flow, so a
monitor's ``selected_analysts`` becomes load-bearing at run time. ``None``/``[]``
runs the full blended committee (mirrors observing-pools ``pipeline.py``); a custom
list is validated against the analyst registry up-front and fails loud on an unknown
id — the run-time enforcement #51 asks for, behind the route's write-boundary 422.

Aggregation (mean-score bands): each committee analyst's bullish/bearish/neutral
signal maps to 0-100 via ``signal_to_score``; the mean of the *valid* (non-degraded)
scores picks the band — >=60 thesis-supportive, <=40 thesis-challenging, else mixed.
No valid scores → insufficient-evidence (degraded). Partial degradation keeps the
band and is recorded per-analyst in ``agent_reports``. Research-only: emits a report
label + confidence, never a trade.
"""

import logging
from typing import Protocol

from src.integrations.tradingagents_adapter import AnalyzingFlowResult
from src.monitoring.runner import AnalyzingFlow
from src.observing_pools.agents_bridge import _safe_agent_score, committee_analyst_keys
from src.observing_pools.scoring import AgentSignal, mean_or_none
from src.storage.models import ReportLabel

logger = logging.getLogger(__name__)

# Mean-score band thresholds on the 0-100 attractiveness scale (signal_to_score).
_SUPPORTIVE_AT = 60.0
_CHALLENGING_AT = 40.0


class RunScoringAnalysts(Protocol):
    """The scoring-graph runner contract (matches ``scoring_graph.run_scoring_analysts``)."""

    def __call__(self, tickers: list[str], selected_analysts: list[str], end_date: str) -> tuple[dict[str, dict[str, dict]], dict]:
        ...


def make_committee_analyzing_flow(
    selected_analysts: list[str] | None,
    *,
    run_analysts: RunScoringAnalysts | None = None,
) -> AnalyzingFlow:
    """Build a committee analyzing flow. ``None``/``[]`` → full blended committee.

    A *custom* analyst list is validated against the registry up-front and fails loud
    on an unknown id; the default committee is valid by construction (guarded by a
    test). ``run_analysts`` is injectable for offline tests and defaults to the real
    scoring graph, imported lazily so this module stays offline-importable.
    """
    if selected_analysts:
        _validate_committee(selected_analysts)
        committee = list(selected_analysts)
    else:
        committee = committee_analyst_keys()

    def _flow(ticker: str, trade_date: str) -> AnalyzingFlowResult:
        runner_fn = run_analysts
        if runner_fn is None:
            # Lazy: the scoring graph pulls the agent/LLM stack.
            from src.observing_pools.scoring_graph import run_scoring_analysts

            runner_fn = run_scoring_analysts
        # Let failures propagate — run_monitor's per-ticker guard turns them into a
        # single degraded, disclaimer-carrying report (one degradation path).
        signals, _cost = runner_fn([ticker], committee, trade_date)
        return _aggregate(ticker, committee, signals)

    return _flow


def _validate_committee(selected_analysts: list[str]) -> None:
    """Reject unknown analyst ids (run-time backstop behind the route's 422)."""
    # Heavy import (pulls the agent stack) — only when a custom list is validated, so
    # the aggregation/default-path unit tests stay offline-importable.
    from src.utils.analysts import ANALYST_CONFIG

    unknown = sorted({a for a in selected_analysts if a not in ANALYST_CONFIG})
    if unknown:
        raise ValueError(f"unknown analyst(s): {unknown}")


def _aggregate(ticker: str, committee: list[str], signals: dict[str, dict[str, dict]]) -> AnalyzingFlowResult:
    """Mean-score-band aggregation of one ticker's committee signals → a report result."""
    scores: list[float] = []
    confidences: list[float] = []
    agent_reports: dict[str, dict] = {}

    for key in committee:
        raw = signals.get(f"{key}_agent", {}).get(ticker)
        if raw is None:
            continue  # analyst not run / no signal for this ticker → omit (mirrors component_scores)
        score, degraded, reason = _safe_agent_score(raw)
        agent_reports[key] = {
            "signal": raw.get("signal") if not degraded else AgentSignal.NEUTRAL.value,
            "confidence": raw.get("confidence"),
            "score": round(score, 2),
            "degraded": degraded,
            "degraded_reason": reason,
        }
        if not degraded:
            scores.append(score)
            confidences.append(float(raw.get("confidence", 50)))  # coercible: _safe_agent_score passed

    mean_score = mean_or_none(scores)
    if mean_score is None:
        # All analysts absent or degraded → no evidence to stand on (degraded run).
        return AnalyzingFlowResult(
            ticker=ticker,
            label=ReportLabel.INSUFFICIENT_EVIDENCE,
            confidence=0.0,
            degraded=True,
            summary="No valid analyst signals — insufficient evidence.",
            agent_reports=agent_reports,
        )

    label = _band(mean_score)
    confidence = mean_or_none(confidences) or 0.0  # non-empty whenever scores is non-empty
    summary = f"Committee {label.value}: mean score {mean_score:.1f}/100 over {len(scores)} analyst(s)."
    return AnalyzingFlowResult(
        ticker=ticker,
        label=label,
        confidence=round(confidence, 2),
        degraded=False,  # partial degradation keeps the band; degraded analysts are recorded above
        summary=summary,
        raw_decision=label.value,
        agent_reports=agent_reports,
    )


def _band(mean_score: float) -> ReportLabel:
    """Map a 0-100 mean score to a report band."""
    if mean_score >= _SUPPORTIVE_AT:
        return ReportLabel.THESIS_SUPPORTIVE
    if mean_score <= _CHALLENGING_AT:
        return ReportLabel.THESIS_CHALLENGING
    return ReportLabel.MIXED
