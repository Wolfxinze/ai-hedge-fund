"""Monitor execution (PRD v4 §9.7, Phase 8 manual-run subset).

Runs a monitor's watchlist through the analyzing flow (TradingAgents adapter by
default; injectable for tests), stamping every output with the disclaimer and
persisting it. No scheduler yet (manual run); APScheduler is an expansion phase.
Emits research reports, never orders.
"""

import logging
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session

from src.compliance import research_disclaimer
from src.integrations.tradingagents_adapter import AnalyzingFlowResult, run_analyzing_flow
from src.monitoring.serialize import serialize_report
from src.storage.models import Granularity, MonitorConfig, OpportunityReport, ReportLabel

logger = logging.getLogger(__name__)


class AnalyzingFlow(Protocol):
    def __call__(self, ticker: str, trade_date: str) -> AnalyzingFlowResult:
        ...


@dataclass(frozen=True)
class MonitorRunResult:
    monitor_name: str
    reports: list[dict]  # serialized (disclaimer-checked) reports
    degraded_count: int = 0  # run-level aggregate; mirrors PoolRefreshRun.PARTIAL

    @property
    def any_degraded(self) -> bool:
        """Cheap run-level degraded signal (a caller need not scan each report)."""
        return self.degraded_count > 0


def create_monitor(
    session: Session,
    *,
    name: str,
    tickers: list[str],
    granularity: str = Granularity.WEEKLY.value,
    platform_keys: list[str] | None = None,
    selected_analysts: list[str] | None = None,
) -> MonitorConfig:
    """Create (or update) a monitor config by unique name."""
    monitor = session.query(MonitorConfig).filter_by(name=name).one_or_none()
    if monitor is None:
        monitor = MonitorConfig(name=name)
        session.add(monitor)
    monitor.tickers = tickers
    monitor.granularity = granularity
    monitor.platform_keys = platform_keys
    monitor.selected_analysts = selected_analysts
    monitor.enabled = True
    session.flush()
    return monitor


def run_monitor(
    session: Session,
    monitor: MonitorConfig,
    *,
    trade_date: str,
    analyzing_flow: AnalyzingFlow = run_analyzing_flow,
) -> MonitorRunResult:
    """Run the monitor once; persist one disclaimer-carrying report per ticker."""
    disclaimer, disclaimer_version = research_disclaimer()
    serialized: list[dict] = []
    degraded_count = 0

    for ticker in monitor.tickers or []:
        try:
            result = analyzing_flow(ticker, trade_date)
        except Exception as exc:  # one ticker's failure must not abort the whole monitor run
            logger.warning("monitor %s: analyzing flow raised for %s: %s", monitor.name, ticker, exc)
            result = AnalyzingFlowResult(
                ticker=ticker,
                label=ReportLabel.INSUFFICIENT_EVIDENCE,
                confidence=0.0,
                degraded=True,
                summary="Analysis unavailable.",
                error=str(exc),
            )
        report = OpportunityReport(
            monitor_id=monitor.id,
            ticker=ticker,
            label=result.label.value,
            confidence=result.confidence,
            degraded=result.degraded,
            time_horizon=monitor.granularity,
            summary=result.summary,
            agent_signals=result.agent_reports or None,
            risks=None,
            next_checks=None,
            disclaimer=disclaimer,
            disclaimer_version=disclaimer_version,
        )
        if result.degraded:
            degraded_count += 1
        session.add(report)
        session.flush()
        # Every emitted report flows through the chokepoint (raises if disclaimer missing).
        serialized.append(serialize_report(report))

    return MonitorRunResult(monitor_name=monitor.name, reports=serialized, degraded_count=degraded_count)
