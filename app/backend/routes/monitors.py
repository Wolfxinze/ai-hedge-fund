"""Monitor CRUD + manual-run API (PRD v4 §14 / §9.7, Phase 9).

Loopback-bound research-only surface: a run reaches only ``run_monitor`` → ``serialize_report``
(every report carries the disclaimer), NEVER ``run_hedge_fund`` or any order/trade path. Bare
dict/list responses + ``HTTPException`` errors, matching the sibling ``observing_pools.py`` (NOT the
older ``flows.py`` Pydantic-envelope style — a deliberate convention choice, see the Phase-9 PR).

A schedule is validated against the Issue-#18 minimum-interval floor (``resolve_trigger_checked``) so
an API client cannot register a monitor that fires faster than its multi-minute job completes.

HOT-RELOAD CAVEAT: the in-process scheduler snapshots enabled monitors at build time only
(``src/scheduler/scheduler.py``). A monitor created/edited here begins firing (or picks up a changed
cadence) on the NEXT app restart — not immediately. Manual ``POST /monitors/{id}/run`` is unaffected.
"""

import logging
import re
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.backend.database.connection import get_db
from src.integrations.tradingagents_adapter import run_analyzing_flow
from src.monitoring.runner import AnalyzingFlow, create_monitor, run_monitor
from src.observing_pools.platforms import PLATFORM_KEYS
from src.scheduler.cron_map import resolve_trigger_checked, ScheduleTooFrequentError
from src.storage.models import Granularity, MonitorConfig

logger = logging.getLogger(__name__)

router = APIRouter()

# Mirrors observing_pools._TICKER_RE: reject clearly-malformed tickers with 422 rather than storing them.
_TICKER_RE = re.compile(r"^[A-Za-z0-9.\-]{1,16}$")
_GRANULARITIES = {g.value for g in Granularity}
# Upper-bound a watchlist so POST /monitors/{id}/run can't be coerced into a synchronous
# thousands-of-tickers job (each ticker is a multi-minute analyzing-flow call).
_MAX_TICKERS = 100


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


def get_analyzing_flow() -> AnalyzingFlow:
    """The production analyzing flow (TradingAgents adapter). Overridden in tests with a stub so a
    run never spawns the real uv subprocess / LLM."""
    return run_analyzing_flow


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


def _commit_or_503(db: Session) -> None:
    """Commit, mapping a SQLite 'database is locked' OperationalError to a 503 — parity with the
    refresh route's PoolLockDatabaseLockedError → 503 (observing_pools.py), instead of an opaque 500.
    This is ERROR-MAPPING only: it does not make the write concurrency-safe (the atomicity guarantees
    live in PoolLock); it classifies a contended-DB failure loudly so a client can retry. Other errors
    (e.g. IntegrityError on a duplicate name) propagate unchanged to the caller's own handler."""
    try:
        db.commit()
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
def create_monitor_endpoint(body: MonitorCreateRequest, db: Session = Depends(get_db)) -> dict:
    """Create a monitor. 409 (not a silent upsert) if the name is taken — use PATCH to update.
    Does NOT auto-register with the running scheduler (see the module HOT-RELOAD caveat)."""
    tickers = _validate_tickers(body.tickers)
    _validate_granularity(body.granularity)
    _validate_platform_keys(body.platform_keys)
    _validate_schedule(body.schedule or body.granularity)  # #18 floor on the effective schedule
    if db.query(MonitorConfig).filter_by(name=body.name).first() is not None:
        raise HTTPException(status_code=409, detail=f"monitor '{body.name}' already exists; use PATCH to update")
    monitor = create_monitor(db, name=body.name, tickers=tickers, granularity=body.granularity, platform_keys=body.platform_keys, selected_analysts=body.selected_analysts)
    if body.schedule is not None:
        monitor.schedule = body.schedule  # create_monitor does not set schedule
    try:
        _commit_or_503(db)  # get_db does not commit; create_monitor only flush()es. locked-DB → 503.
    except IntegrityError:
        # The pre-check above handles the common case; this closes the concurrent-create race on the
        # unique name (constraint backstops corruption) — surface the clean 409, not a raw 500.
        db.rollback()
        raise HTTPException(status_code=409, detail=f"monitor '{body.name}' already exists; use PATCH to update")
    db.refresh(monitor)
    return _monitor_to_dict(monitor)


@router.patch("/monitors/{monitor_id}")
def patch_monitor_endpoint(monitor_id: int, body: MonitorPatchRequest, db: Session = Depends(get_db)) -> dict:
    """Partial update. Name is immutable here (avoids the unique-collision path). If schedule or
    granularity changes, the resulting effective schedule is re-validated against the #18 floor."""
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
    # Re-validate the effective schedule against the #18 floor when it could change OR when the monitor
    # is being (re-)enabled — so a pre-floor sub-floor row (created via direct DB/CLI) can't be re-armed
    # through the API and then picked up by the scheduler.
    if "schedule" in fields or "granularity" in fields or fields.get("enabled") is True:
        effective = fields.get("schedule", monitor.schedule) or fields.get("granularity", monitor.granularity)
        _validate_schedule(effective)
    for key, value in fields.items():
        setattr(monitor, key, value)
    _commit_or_503(db)
    db.refresh(monitor)
    return _monitor_to_dict(monitor)


@router.post("/monitors/{monitor_id}/run")
def run_monitor_endpoint(
    monitor_id: int,
    body: MonitorRunRequest | None = None,
    db: Session = Depends(get_db),
    analyzing_flow: AnalyzingFlow = Depends(get_analyzing_flow),
) -> dict:
    """Run a monitor once NOW (synchronous). Reaches only run_monitor → serialize_report, so every
    persisted report carries the disclaimer; a single ticker's failure degrades, never aborts."""
    monitor = db.get(MonitorConfig, monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail=f"monitor {monitor_id} not found")
    trade_date = (body.trade_date if body and body.trade_date else None) or date.today().isoformat()
    _validate_trade_date(trade_date)
    result = run_monitor(db, monitor, trade_date=trade_date, analyzing_flow=analyzing_flow)
    _commit_or_503(db)  # run_monitor only flush()es per report; the route owns the commit. locked-DB → 503.
    return {"monitor_name": result.monitor_name, "reports": result.reports, "degraded_count": result.degraded_count, "any_degraded": result.any_degraded}
