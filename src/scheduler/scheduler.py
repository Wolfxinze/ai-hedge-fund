"""BackgroundScheduler construction + lifecycle (Phase 8).

Builds an in-process APScheduler that drives the weekly pool refresh (OBSERVING_POOL_REFRESH_CRON,
default Monday 08:00) and one job per enabled monitor on its cadence. Every job is registered with
``max_instances=1`` + ``coalesce=True`` so a long LLM run can never pile up overlapping fires; the
per-platform PoolLock is the second, independent serialisation layer. Factories are injectable so
tests build the scheduler with in-memory sessions and stubs (no real DB, no LLM, no real timer).
"""

import logging
import os
from collections.abc import Callable
from contextlib import AbstractContextManager

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session

from src.monitoring.runner import AnalyzingFlow
from src.observing_pools.pipeline import RunAnalysts
from src.scheduler.cron_map import resolve_trigger
from src.scheduler.jobs import (
    monitor_schedule,
    refresh_all_platforms_job,
    run_monitor_job,
)
from src.storage import session_scope
from src.storage.models import MonitorConfig

logger = logging.getLogger(__name__)

REFRESH_JOB_ID = "pool_refresh"
_REFRESH_MISFIRE_SECONDS = 3600  # if the scheduler was down <1h, still fire the weekly refresh once
_MONITOR_MISFIRE_SECONDS = 600

SessionFactory = Callable[[], AbstractContextManager[Session]]


def default_run_analysts_factory() -> RunAnalysts:
    """Production scoring runner, imported LAZILY: the analyst/LLM stack is heavy, so it is pulled
    only when the refresh job actually fires — never at app import/startup. Public so the Phase-9
    refresh API and the scheduler share ONE definition of 'the production runner' (no triplication)."""
    from functools import partial

    from src.observing_pools.scoring_graph import run_scoring_analysts

    return partial(
        run_scoring_analysts,
        model_name=os.environ.get("OBSERVING_POOL_MODEL", "gpt-4.1"),
        model_provider=os.environ.get("OBSERVING_POOL_PROVIDER", "OpenAI"),
    )


def build_scheduler(
    *,
    session_factory: SessionFactory = session_scope,
    run_analysts_factory: Callable[[], RunAnalysts] | None = None,
    analyzing_flow: AnalyzingFlow | None = None,
) -> BackgroundScheduler:
    """Construct (but do NOT start) the scheduler with the refresh job + one job per enabled
    monitor. A monitor with an invalid schedule is skipped-and-warned (it never takes down the
    whole scheduler); an invalid OBSERVING_POOL_REFRESH_CRON raises ValueError (the caller —
    main.py startup — catches it, logs, and the app still starts without the scheduler)."""
    scheduler = BackgroundScheduler(executors={"default": ThreadPoolExecutor(max_workers=2)}, timezone="UTC")
    raf = run_analysts_factory or default_run_analysts_factory

    refresh_cron = os.environ.get("OBSERVING_POOL_REFRESH_CRON", "0 8 * * 1")
    scheduler.add_job(
        refresh_all_platforms_job,
        kwargs={"run_analysts_factory": raf, "session_factory": session_factory},
        trigger=resolve_trigger(refresh_cron),
        id=REFRESH_JOB_ID,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=_REFRESH_MISFIRE_SECONDS,
        replace_existing=True,
    )

    # Snapshot enabled monitors at build time (new/edited monitors register on next restart — a
    # documented Phase-8 gap; hot-reload is Phase 9).
    with session_factory() as s:
        enabled = [(m.id, m.name, monitor_schedule(m)) for m in s.query(MonitorConfig).filter_by(enabled=True).all()]
    for mid, name, sched in enabled:
        try:
            trigger = resolve_trigger(sched)
        except ValueError:
            logger.warning("scheduler: monitor %r (id=%s) has invalid schedule %r; not registered", name, mid, sched)
            continue
        scheduler.add_job(
            run_monitor_job,
            kwargs={"monitor_id": mid, "analyzing_flow": analyzing_flow, "session_factory": session_factory},
            trigger=trigger,
            id=f"monitor_{mid}",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=_MONITOR_MISFIRE_SECONDS,
            replace_existing=True,
        )
    logger.info("scheduler built: %d job(s)", len(scheduler.get_jobs()))
    return scheduler


def start_scheduler(scheduler: BackgroundScheduler) -> None:
    scheduler.start()
    logger.info("APScheduler started with %d job(s)", len(scheduler.get_jobs()))


def stop_scheduler(scheduler: BackgroundScheduler) -> None:
    # wait=False: do not block ASGI shutdown on an in-flight (possibly 90-min LLM) refresh. The
    # PoolLock finally-release frees the lock on normal exit; a hard kill is covered by the lock TTL.
    scheduler.shutdown(wait=False)
    logger.info("APScheduler stopped")
