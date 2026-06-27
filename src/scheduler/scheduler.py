"""BackgroundScheduler construction + lifecycle (Phase 8 + 9 / Issue #21).

Builds an in-process APScheduler that drives the weekly pool refresh (OBSERVING_POOL_REFRESH_CRON,
default Monday 08:00) and one job per enabled monitor on its cadence. Every job is registered with
``max_instances=1`` + ``coalesce=True`` so a long LLM run can never pile up overlapping fires; the
per-platform PoolLock is the second, independent serialisation layer. Factories are injectable so
tests build the scheduler with in-memory sessions and stubs (no real DB, no LLM, no real timer).

Phase 9 / Issue #21 adds ``reschedule_monitor`` for hot-reload: a monitor created or edited via the
API registers/rescheduled/removes its job on the LIVE running scheduler without an app restart.
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


def monitor_job_id(monitor_id: int) -> str:
    """Canonical job id for a monitor. Defined ONCE so build, hot-reload, and remove paths never
    diverge — callers must not construct the id string directly."""
    return f"monitor_{monitor_id}"


def _add_monitor_job(
    scheduler: BackgroundScheduler,
    *,
    monitor_id: int,
    name: str,
    schedule: str,
    analyzing_flow: AnalyzingFlow | None,
    session_factory: SessionFactory,
) -> None:
    """Register (or replace) a single monitor job on the scheduler.

    Calls ``resolve_trigger(schedule)`` — the TOLERANT variant (not resolve_trigger_checked). The
    write endpoints already validated the #18 min-interval floor via resolve_trigger_checked BEFORE
    commit, so a second strict check here would be redundant and would block the hot path needlessly.

    Raises ``ValueError`` on an invalid schedule so callers can decide whether to skip-and-warn
    (build_scheduler) or log-and-remove (reschedule_monitor)."""
    trigger = resolve_trigger(schedule)
    scheduler.add_job(
        run_monitor_job,
        kwargs={"monitor_id": monitor_id, "analyzing_flow": analyzing_flow, "session_factory": session_factory},
        trigger=trigger,
        id=monitor_job_id(monitor_id),
        max_instances=1,
        coalesce=True,
        misfire_grace_time=_MONITOR_MISFIRE_SECONDS,
        replace_existing=True,
    )


def _remove_monitor_job(scheduler: BackgroundScheduler, monitor_id: int) -> None:
    """Remove the monitor's job from the scheduler iff it exists. Idempotent — safe to call when
    the job is already absent (e.g. disabled monitor that was never registered)."""
    job_id = monitor_job_id(monitor_id)
    if scheduler.get_job(job_id) is not None:
        scheduler.remove_job(job_id)
        if scheduler.get_job(job_id) is not None:
            logger.warning("scheduler: job %r persisted after remove_job (in-memory disarm failed; DB enabled-guard still authoritative)", job_id)
        else:
            logger.info("scheduler: disarmed job %r", job_id)


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

    # Snapshot enabled monitors at build time. Phase 9 / Issue #21: hot-reload via reschedule_monitor
    # keeps the live scheduler in sync after this snapshot (create/edit no longer needs a restart).
    with session_factory() as s:
        enabled = [(m.id, m.name, monitor_schedule(m)) for m in s.query(MonitorConfig).filter_by(enabled=True).all()]
    for mid, name, sched in enabled:
        try:
            _add_monitor_job(
                scheduler,
                monitor_id=mid,
                name=name,
                schedule=sched,
                analyzing_flow=analyzing_flow,
                session_factory=session_factory,
            )
        except ValueError:
            logger.warning("scheduler: monitor %r (id=%s) has invalid schedule %r; not registered", name, mid, sched)
            continue
    logger.info("scheduler built: %d job(s)", len(scheduler.get_jobs()))
    return scheduler


def reschedule_monitor(
    scheduler: BackgroundScheduler,
    monitor: MonitorConfig,
    *,
    analyzing_flow: AnalyzingFlow | None = None,
    session_factory: SessionFactory = session_scope,
) -> None:
    """Hot-reload: register, replace, or remove a monitor's scheduler job on a LIVE scheduler
    (Phase 9 / Issue #21). Called by the API routes after a successful DB write.

    Behaviour:
    - ``monitor.enabled is False`` → remove the job (idempotent if already absent).
    - ``monitor.enabled is True`` → resolve the schedule and add/replace the job.
      If the schedule is now invalid (e.g. a manually-stored bad cron was just re-enabled),
      log a WARNING and remove any stale job so no out-of-date job fires.

    Uses ``resolve_trigger`` (tolerant), NOT ``resolve_trigger_checked``: the write endpoints
    already validated the #18 min-interval floor before commit; a second strict check here is
    redundant and would block the hot path for valid schedules that happen to sit right at the floor.

    This function is IDEMPOTENT: calling it twice for the same monitor state leaves exactly one job
    (enabled) or no job (disabled/invalid). The route calls it best-effort via ``_safe_reschedule``
    — a failure here must NOT roll back or fail a successful DB write.

    The best-effort job removal above is an OPTIMIZATION, not the safety mechanism. The AUTHORITATIVE
    disarm is the DB-checked ``not monitor.enabled`` guard inside ``run_monitor_job`` (src/scheduler/
    jobs.py): it re-reads ``enabled`` from the DB before doing any work, so a disabled monitor whose
    job-removal failed (or whose stale job lingers) still no-ops on its next fire — the DB is the
    source of truth, not the in-memory job table."""
    if not monitor.enabled:
        _remove_monitor_job(scheduler, monitor.id)
        return

    sched = monitor_schedule(monitor)
    try:
        _add_monitor_job(
            scheduler,
            monitor_id=monitor.id,
            name=monitor.name,
            schedule=sched,
            analyzing_flow=analyzing_flow,
            session_factory=session_factory,
        )
    except ValueError:
        logger.warning(
            "hot-reload: monitor %r (id=%s) has invalid schedule %r; job not registered (row persisted, will arm on next restart if fixed)",
            monitor.name,
            monitor.id,
            sched,
        )
        _remove_monitor_job(scheduler, monitor.id)


def start_scheduler(scheduler: BackgroundScheduler) -> None:
    scheduler.start()
    logger.info("APScheduler started with %d job(s)", len(scheduler.get_jobs()))


def stop_scheduler(scheduler: BackgroundScheduler) -> None:
    # wait=False: do not block ASGI shutdown on an in-flight (possibly 90-min LLM) refresh. The
    # PoolLock finally-release frees the lock on normal exit; a hard kill is covered by the lock TTL.
    scheduler.shutdown(wait=False)
    logger.info("APScheduler stopped")
