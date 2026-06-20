"""Scheduler job callables (Phase 8). Plain functions with injected factories so they are unit-
testable offline (no APScheduler, no real clock, no LLM). Research-only: a job only writes ranked
pools (refresh) and disclaimer-bearing reports (monitors) — never an order/trade.

Each job opens its OWN session via the injected ``session_factory`` (never a shared/request
session — APScheduler runs jobs in worker threads). Pool refreshes go through ``refresh_pool_locked``
so the same platform serialises (per-platform PoolLock) while different platforms proceed.
"""

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date

from sqlalchemy.orm import Session

from src.monitoring.runner import AnalyzingFlow, run_monitor
from src.observing_pools.pipeline import RefreshConfig, RunAnalysts
from src.observing_pools.platforms import PLATFORM_KEYS
from src.observing_pools.pool_lock import (
    PoolLockContendedError,
    PoolLockDatabaseLockedError,
    refresh_pool_locked,
)
from src.storage import session_scope
from src.storage.models import MonitorConfig

logger = logging.getLogger(__name__)

DEFAULT_UNIVERSE = "data/universes/ai_seed.csv"  # matches the observing_pools CLI default

SessionFactory = Callable[[], AbstractContextManager[Session]]


def _today() -> str:
    return date.today().isoformat()


def monitor_schedule(monitor: MonitorConfig) -> str:
    """The schedule string for a monitor: the explicit ``schedule`` (cron/keyword) if set, else the
    ``granularity`` keyword (daily/weekly/monthly)."""
    return (monitor.schedule or monitor.granularity or "").strip()


def refresh_all_platforms_job(
    *,
    run_analysts_factory: Callable[[], RunAnalysts],
    universe_csv: str = DEFAULT_UNIVERSE,
    session_factory: SessionFactory = session_scope,
    end_date_fn: Callable[[], str] = _today,
    top_n: int = 20,
    token_budget: int | None = None,
) -> None:
    """Refresh every platform, each serialised by its PoolLock. A platform that is already locked
    (an in-flight refresh) is SKIPPED with a WARNING; a platform that errors is logged and the job
    continues — one platform never aborts the others. Emits no trades."""
    run_analysts = run_analysts_factory()
    end_date = end_date_fn()
    refreshed = 0
    for platform_key in PLATFORM_KEYS:
        run_id = f"scheduler-{platform_key}-{end_date}"
        cfg = RefreshConfig(platform_key=platform_key, universe_csv=universe_csv, top_n=top_n, token_budget=token_budget)
        try:
            outcome = refresh_pool_locked(cfg, run_analysts, end_date=end_date, run_id=run_id, session_factory=session_factory)
            refreshed += 1
            logger.info("scheduled refresh platform=%s status=%s", platform_key, outcome.status)
        except PoolLockContendedError:
            logger.warning("scheduled refresh skipped platform=%s: already locked (refresh in progress)", platform_key)
        except PoolLockDatabaseLockedError:
            logger.error("scheduled refresh platform=%s: database is locked under contention — skipped", platform_key)
        except Exception:  # one platform's failure must not abort the rest of the run
            logger.exception("scheduled refresh platform=%s failed", platform_key)
    logger.info("scheduled refresh complete: %d/%d platform(s) refreshed", refreshed, len(PLATFORM_KEYS))


def run_monitor_job(
    monitor_id: int,
    *,
    analyzing_flow: AnalyzingFlow | None = None,
    session_factory: SessionFactory = session_scope,
    trade_date_fn: Callable[[], str] = _today,
) -> None:
    """Run ONE monitor by id, persisting one disclaimer-bearing report per ticker via run_monitor's
    serialize_report chokepoint. Skips a monitor that was deleted/disabled since registration.
    Never issues a trade. Opens its own session (own transaction)."""
    trade_date = trade_date_fn()
    with session_factory() as s:
        monitor = s.get(MonitorConfig, monitor_id)
        if monitor is None or not monitor.enabled:
            logger.info("scheduled monitor id=%s missing/disabled; skipping", monitor_id)
            return
        kwargs = {"trade_date": trade_date}
        if analyzing_flow is not None:  # else run_monitor uses its production default flow
            kwargs["analyzing_flow"] = analyzing_flow
        result = run_monitor(s, monitor, **kwargs)
        logger.info("scheduled monitor=%s reports=%d degraded=%d", monitor.name, len(result.reports), result.degraded_count)
