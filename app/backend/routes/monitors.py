"""Monitor CRUD + manual-run API (PRD v4 §14 / §9.7, Phase 9 / Issue #21).

Loopback-bound research-only surface: a run reaches only ``run_monitor`` → ``serialize_report``
(every report carries the disclaimer), NEVER ``run_hedge_fund`` or any order/trade path. Bare
dict/list responses + ``HTTPException`` errors, matching the sibling ``observing_pools.py`` (NOT the
older ``flows.py`` Pydantic-envelope style — a deliberate convention choice, see the Phase-9 PR).

A schedule is validated against the Issue-#18 minimum-interval floor (``resolve_trigger_checked``) so
an API client cannot register a monitor that fires faster than its multi-minute job completes.

HOT-RELOAD (Phase 9 / Issue #21): monitors now hot-reload on create/edit — the live scheduler job is
registered/updated/removed immediately after each successful DB write via ``_safe_reschedule``. Manual
``POST /monitors/{id}/run`` is unaffected (always synchronous).
"""

import logging
import re
import threading
from contextlib import contextmanager
from datetime import date, datetime

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.backend.database.connection import get_db
from src.monitoring.runner import AnalyzingFlow, create_monitor, run_monitor
from src.observing_pools.platforms import PLATFORM_KEYS
from src.scheduler.cron_map import resolve_trigger_checked, ScheduleTooFrequentError
from src.scheduler.scheduler import reschedule_monitor
from src.storage.models import Granularity, MonitorConfig

logger = logging.getLogger(__name__)

router = APIRouter()


def get_scheduler(request: Request) -> BackgroundScheduler | None:
    """FastAPI dependency: return the live BackgroundScheduler from app.state, or None if the
    scheduler failed to start. Routes use this for hot-reload and must tolerate None gracefully."""
    return getattr(request.app.state, "scheduler", None)


def _safe_reschedule(scheduler: BackgroundScheduler | None, monitor: MonitorConfig) -> None:
    """Best-effort hot-reload: call reschedule_monitor but never let a scheduling failure propagate.
    Rationale: the DB write is the source of truth. A scheduling side-effect failure must NOT 500 a
    successful create/edit (the next app restart re-snapshots the live monitors). The failure is
    logged LOUDLY via logger.exception so ops sees it — never silently swallowed."""
    if scheduler is None:
        # Degraded mode: scheduler failed to build/start. The row IS persisted (source of truth) but
        # won't be armed until a restart re-snapshots the live monitors — leave a per-write breadcrumb
        # so an operator isn't left guessing why a just-saved monitor never fires.
        logger.warning(
            "hot-reload skipped: scheduler not running (degraded mode); monitor id=%s persisted but NOT armed until restart",
            monitor.id,
        )
        return
    try:
        reschedule_monitor(scheduler, monitor)
    except Exception:
        logger.exception(
            "hot-reload: failed to (re)schedule monitor id=%s; row persisted, will arm on next restart",
            monitor.id,
        )


# Mirrors observing_pools._TICKER_RE: reject clearly-malformed tickers with 422 rather than storing them.
_TICKER_RE = re.compile(r"^[A-Za-z0-9.\-]{1,16}$")
_GRANULARITIES = {g.value for g in Granularity}
# Upper-bound a watchlist so POST /monitors/{id}/run can't be coerced into a synchronous
# thousands-of-tickers job (each ticker is a multi-minute analyzing-flow call).
_MAX_TICKERS = 100
# Cap concurrent manual runs: each run is a synchronous multi-minute analyzing-flow job, so the
# loopback surface must not be coerced into N parallel storms. cap=2 is lenient — never 429s the
# legitimate single-user case. A BoundedSemaphore raises if released more than acquired (a leak alarm).
_MAX_CONCURRENT_RUNS = 2
_run_semaphore = threading.BoundedSemaphore(_MAX_CONCURRENT_RUNS)


class MonitorCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    tickers: list[str] = Field(min_length=1, max_length=_MAX_TICKERS)
    granularity: str = Granularity.WEEKLY.value
    platform_keys: list[str] | None = None
    selected_analysts: list[str] | None = None
    schedule: str | None = None  # cron/keyword; validated against the #18 floor on the effective schedule


class MonitorPatchRequest(BaseModel):
    """All optional — a true partial update. ``model_dump(exclude_unset=True)`` distinguishes an
    omitted field (leave as-is) from an explicit ``null`` (clear a nullable column)."""

    tickers: list[str] | None = Field(default=None, max_length=_MAX_TICKERS)
    granularity: str | None = None
    schedule: str | None = None
    enabled: bool | None = None
    platform_keys: list[str] | None = None
    selected_analysts: list[str] | None = None


class MonitorRunRequest(BaseModel):
    trade_date: str | None = None  # YYYY-MM-DD; default today (aligns with the scheduler's _today())


def get_analyzing_flow() -> AnalyzingFlow | None:
    """Default flow sentinel: ``None`` → run_monitor builds the ai-hedge-fund committee
    from the monitor's selected_analysts (#51). Overridden in tests with a stub flow so a
    run never spawns the real LLM committee."""
    return None


def _monitor_to_dict(monitor: MonitorConfig) -> dict:
    return {
        "id": monitor.id,
        "name": monitor.name,
        "tickers": monitor.tickers,
        "platform_keys": monitor.platform_keys,
        "granularity": monitor.granularity,
        "schedule": monitor.schedule,
        "selected_analysts": monitor.selected_analysts,
        "lookback_window": monitor.lookback_window,
        "enabled": monitor.enabled,
        "created_at": monitor.created_at.isoformat() if monitor.created_at else None,
    }


def _validate_tickers(tickers: list[str]) -> list[str]:
    if not tickers:
        raise HTTPException(status_code=422, detail="tickers must be a non-empty list")
    if len(tickers) > _MAX_TICKERS:
        raise HTTPException(status_code=422, detail=f"too many tickers ({len(tickers)}); max is {_MAX_TICKERS}")
    for ticker in tickers:
        if not isinstance(ticker, str) or not _TICKER_RE.match(ticker):
            raise HTTPException(status_code=422, detail=f"invalid ticker '{ticker}'")
    return [ticker.upper() for ticker in tickers]


def _validate_granularity(granularity: str) -> None:
    if granularity not in _GRANULARITIES:
        raise HTTPException(status_code=422, detail=f"invalid granularity '{granularity}' (expected one of {sorted(_GRANULARITIES)})")


def _validate_platform_keys(platform_keys: list[str] | None) -> None:
    for key in platform_keys or []:
        if key not in PLATFORM_KEYS:
            raise HTTPException(status_code=422, detail=f"unknown platform '{key}'")


def _validate_selected_analysts(selected_analysts: list[str] | None) -> None:
    """Reject unknown analyst ids (422). Lazy-import keeps the routes module offline-importable."""
    if not selected_analysts:
        return  # None/empty/omitted → no constraint
    from src.utils.analysts import ANALYST_CONFIG  # lazy import INSIDE the fn (keep module offline-importable)

    unknown = [a for a in selected_analysts if a not in ANALYST_CONFIG]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown analyst(s): {sorted(set(unknown))}")


def _validate_schedule(effective_schedule: str) -> None:
    """Reject a schedule below the #18 minimum-interval floor (422), or a malformed/unknown one (422).
    ``effective_schedule`` is the explicit cron if set, else the granularity keyword (matches
    ``jobs.monitor_schedule`` — what the scheduler will later resolve)."""
    try:
        resolve_trigger_checked(effective_schedule)
    except ScheduleTooFrequentError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid schedule: {exc}")


def _validate_trade_date(value: str) -> None:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail=f"invalid trade_date '{value}' (expected YYYY-MM-DD)")


@contextmanager
def _db_locked_to_503(db: Session):
    """Wrap a write section, mapping a SQLite 'database is locked' OperationalError — raised on a
    ``flush`` OR a ``commit`` — to a 503 (parity with the refresh route's PoolLockDatabaseLockedError
    → 503 in observing_pools.py), instead of an opaque 500. ``run_monitor``/``create_monitor`` flush
    BEFORE the route's commit, so the whole write must be guarded, not just the commit.
    This is ERROR-MAPPING only: it does not make the write concurrency-safe (the atomicity guarantees
    live in PoolLock); it classifies a contended-DB failure loudly so a client can retry. IntegrityError
    (duplicate name) and other OperationalErrors are NOT caught here — they propagate to the caller's
    own handler unchanged."""
    try:
        yield
    except OperationalError as exc:
        if "database is locked" in str(exc).lower():  # mirrors pool_lock.py's detection
            db.rollback()
            raise HTTPException(status_code=503, detail="database is locked; retry shortly")
        raise


@router.get("/monitors")
def list_monitors(db: Session = Depends(get_db), limit: int = Query(50, ge=1, le=500)) -> list[dict]:
    monitors = db.query(MonitorConfig).order_by(MonitorConfig.id.desc()).limit(limit).all()
    return [_monitor_to_dict(monitor) for monitor in monitors]


@router.post("/monitors")
def create_monitor_endpoint(
    body: MonitorCreateRequest,
    db: Session = Depends(get_db),
    scheduler=Depends(get_scheduler),
) -> dict:
    """Create a monitor. 409 (not a silent upsert) if the name is taken — use PATCH to update.
    Hot-registers the job on the live scheduler after a successful commit (Phase 9 / Issue #21)."""
    tickers = _validate_tickers(body.tickers)
    _validate_granularity(body.granularity)
    _validate_platform_keys(body.platform_keys)
    _validate_selected_analysts(body.selected_analysts)
    _validate_schedule(body.schedule or body.granularity)  # #18 floor on the effective schedule
    if db.query(MonitorConfig).filter_by(name=body.name).first() is not None:
        raise HTTPException(status_code=409, detail=f"monitor '{body.name}' already exists; use PATCH to update")
    try:
        # create_monitor flush()es before the commit; guard the whole write so a locked DB on the
        # flush maps to 503 too (not just the commit). IntegrityError is NOT caught by the CM —
        # it propagates to the 409 handler below.
        with _db_locked_to_503(db):
            monitor = create_monitor(db, name=body.name, tickers=tickers, granularity=body.granularity, platform_keys=body.platform_keys, selected_analysts=body.selected_analysts)
            if body.schedule is not None:
                monitor.schedule = body.schedule  # create_monitor does not set schedule
            db.commit()  # get_db does not commit
    except IntegrityError:
        # The pre-check above handles the common case; this closes the concurrent-create race on the
        # unique name (constraint backstops corruption) — surface the clean 409, not a raw 500.
        db.rollback()
        raise HTTPException(status_code=409, detail=f"monitor '{body.name}' already exists; use PATCH to update")
    db.refresh(monitor)
    _safe_reschedule(scheduler, monitor)
    return _monitor_to_dict(monitor)


@router.patch("/monitors/{monitor_id}")
def patch_monitor_endpoint(
    monitor_id: int,
    body: MonitorPatchRequest,
    db: Session = Depends(get_db),
    scheduler=Depends(get_scheduler),
) -> dict:
    """Partial update. Name is immutable here (avoids the unique-collision path). If schedule or
    granularity changes, the resulting effective schedule is re-validated against the #18 floor.
    Hot-updates the live scheduler job after a successful commit (Phase 9 / Issue #21)."""
    monitor = db.get(MonitorConfig, monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail=f"monitor {monitor_id} not found")
    fields = body.model_dump(exclude_unset=True)  # omitted → untouched; explicit null → clear
    # Explicit null on a NOT-NULL column survives exclude_unset and would otherwise surface as an
    # opaque IntegrityError 500 — reject it as a 422 at the boundary (tickers/granularity/enabled are
    # nullable=False; platform_keys/schedule/selected_analysts ARE nullable, so null legitimately clears them).
    for non_nullable in ("tickers", "granularity", "enabled"):
        if non_nullable in fields and fields[non_nullable] is None:
            raise HTTPException(status_code=422, detail=f"{non_nullable} cannot be null")
    if "tickers" in fields:
        fields["tickers"] = _validate_tickers(fields["tickers"])
    if "granularity" in fields:
        _validate_granularity(fields["granularity"])
    if "platform_keys" in fields:
        _validate_platform_keys(fields["platform_keys"])
    if "selected_analysts" in fields:
        _validate_selected_analysts(fields["selected_analysts"])
    # Re-validate the effective schedule against the #18 floor when it could change OR when the monitor
    # is being (re-)enabled — so a pre-floor sub-floor row (created via direct DB/CLI) can't be re-armed
    # through the API and then picked up by the scheduler.
    if "schedule" in fields or "granularity" in fields or fields.get("enabled") is True:
        effective = fields.get("schedule", monitor.schedule) or fields.get("granularity", monitor.granularity)
        _validate_schedule(effective)
    for key, value in fields.items():
        setattr(monitor, key, value)
    with _db_locked_to_503(db):
        db.commit()
    db.refresh(monitor)
    _safe_reschedule(scheduler, monitor)
    return _monitor_to_dict(monitor)


@router.post("/monitors/{monitor_id}/run")
def run_monitor_endpoint(
    monitor_id: int,
    body: MonitorRunRequest | None = None,
    db: Session = Depends(get_db),
    analyzing_flow: AnalyzingFlow | None = Depends(get_analyzing_flow),
) -> dict:
    """Run a monitor once NOW (synchronous). Reaches only run_monitor → serialize_report, so every
    persisted report carries the disclaimer; a single ticker's failure degrades, never aborts."""
    # Acquire a run permit BEFORE the try whose finally releases — a BoundedSemaphore raises if you
    # release a permit you never took. At the cap, fail fast with a retryable 429 (no queueing).
    if not _run_semaphore.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="too many concurrent monitor runs; retry shortly")
    try:
        monitor = db.get(MonitorConfig, monitor_id)
        if monitor is None:
            raise HTTPException(status_code=404, detail=f"monitor {monitor_id} not found")
        trade_date = (body.trade_date if body and body.trade_date else None) or date.today().isoformat()
        _validate_trade_date(trade_date)
        # run_monitor flush()es per report (BEFORE the commit), so guard the whole run: a locked DB on a
        # mid-run flush maps to 503, not an opaque 500.
        with _db_locked_to_503(db):
            result = run_monitor(db, monitor, trade_date=trade_date, analyzing_flow=analyzing_flow)
            db.commit()  # run_monitor only flush()es per report; the route owns the commit
        return {"monitor_name": result.monitor_name, "reports": result.reports, "degraded_count": result.degraded_count, "any_degraded": result.any_degraded}
    finally:
        _run_semaphore.release()  # released on EVERY exit path (success, 404/422/503, or uncaught error)
