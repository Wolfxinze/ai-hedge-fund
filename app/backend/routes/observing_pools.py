"""Read-only API for observing pools, reports, and platforms (PRD v4 §14).

Loopback-bound (research-only). Every report is projected through
``serialize_report`` so the disclaimer invariant holds on the API surface too.
"""

import re
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.backend.database.connection import get_db
from src.monitoring.serialize import serialize_report
from src.observing_pools.pipeline import refresh_pool, RefreshConfig, RunAnalysts
from src.observing_pools.platforms import PLATFORM_KEYS
from src.observing_pools.pool_lock import (
    PoolLockContendedError,
    PoolLockDatabaseLockedError,
    refresh_pool_locked,
)
from src.storage import session_scope
from src.storage.models import (
    InnovationPlatform,
    ObservationPoolEntry,
    OpportunityReport,
    PoolRefreshRun,
    SerenityResearchRecord,
)

router = APIRouter()

# Default candidate universe for an API-triggered refresh — mirrors the CLI + scheduler defaults
# (src/observing_pools/cli.py, src/scheduler/jobs.py). Not client-settable in v1 (loopback tool).
DEFAULT_UNIVERSE = "data/universes/ai_seed.csv"

SessionFactory = Callable[[], AbstractContextManager[Session]]


class RefreshRequest(BaseModel):
    """Body for POST /observing-pools/refresh. Minimal + bounded; no secrets, no model/provider
    overrides (env-driven like the CLI/scheduler), no arbitrary universe (loopback research tool)."""

    platform_key: str
    top_n: int = Field(20, ge=1, le=200)
    dry_run: bool = False
    end_date: str | None = None
    provider_name: str = "yfinance"  # the DATA-provider label recorded on the run, not the LLM provider


def get_session_factory() -> SessionFactory:
    """The transactional session factory the refresh uses for its OWN short claim/release txns and
    the long refresh (independent of the request's get_db session — refresh_pool_locked runs the
    body outside the lock). Production = session_scope; tests override it to bind the test DB."""
    return session_scope


def get_refresh_runner() -> RunAnalysts:
    """The production scoring committee. Built lazily (the analyst/LLM stack is heavy) so importing
    this module stays offline; constructing the partial makes no network/LLM call. Tests override
    this with a deterministic stub so no run ever reaches a real model."""
    from src.scheduler.scheduler import default_run_analysts_factory

    return default_run_analysts_factory()

# Loose validation for untrusted ticker path params. Parameterized queries already
# prevent SQLi; this rejects clearly-malformed input with a 422 rather than querying.
_TICKER_RE = re.compile(r"^[A-Za-z0-9.\-]{1,16}$")


def _entry_to_dict(e: ObservationPoolEntry) -> dict:
    return {
        "ticker": e.ticker,
        "platform_key": e.platform_key,
        "status": e.status,
        "rank": e.rank,
        "composite_score": e.composite_score,
        "composite_formula_version": e.composite_formula_version,
        "components": {
            "platform_fit": e.platform_fit_score,
            "value_investor": e.value_investor_score,
            "innovation_growth": e.innovation_growth_score,
            "risk_adjusted_momentum": e.risk_adjusted_momentum_score,
            "serenity_bottleneck": e.serenity_bottleneck_score,
        },
        "score_breakdown": e.score_breakdown,
        "rationale": e.rationale,
    }


@router.get("/innovation-platforms")
def list_platforms(db: Session = Depends(get_db)) -> list[dict]:
    platforms = db.query(InnovationPlatform).order_by(InnovationPlatform.key).all()
    return [{"key": p.key, "name": p.name, "description": p.description, "enabled": p.enabled} for p in platforms]


def _run_to_dict(run: PoolRefreshRun) -> dict:
    """Full PoolRefreshRun provenance projection (PRD §10). Nullable timestamps serialise as null."""
    return {
        "id": run.id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "status": run.status,
        "provider_name": run.provider_name,
        "universe_source": run.universe_source,
        "universe_version": run.universe_version,
        "composite_formula_version": run.composite_formula_version,
        "platform_keys": run.platform_keys,
        "candidate_count": run.candidate_count,
        "fetch_errors": run.fetch_errors,
        "rejected": run.rejected,
        "token_cost": run.token_cost,
        "summary": run.summary,
        "error": run.error,
    }


# NOTE: declared BEFORE get_pool so the literal path "/observing-pools/refresh-runs" is matched
# before the parameterised "/observing-pools/{platform_key}" (else it 404s as an unknown platform).
@router.post("/observing-pools/refresh")
def refresh_observing_pool(
    body: RefreshRequest,
    run_analysts: RunAnalysts = Depends(get_refresh_runner),
    session_factory: SessionFactory = Depends(get_session_factory),
) -> dict:
    """Trigger one PoolLock-guarded refresh for a platform (research-only: reaches refresh_pool, the
    scoring-only graph — never a trade/order path). Synchronous: the loopback caller wants the result,
    and the PoolLock claim (hence 409/503) is raised synchronously before the long refresh body.
    ``dry_run`` computes + returns a summary but persists nothing and takes NO lock (matches the CLI)."""
    if body.platform_key not in PLATFORM_KEYS:
        raise HTTPException(status_code=404, detail=f"unknown platform '{body.platform_key}'")
    end_date = body.end_date or date.today().isoformat()
    config = RefreshConfig(platform_key=body.platform_key, universe_csv=DEFAULT_UNIVERSE, top_n=body.top_n, dry_run=body.dry_run)

    if body.dry_run:
        # Dry-run mutates nothing (no pool_locks row) — run UNLOCKED, exactly like the CLI.
        with session_factory() as s:
            run = refresh_pool(s, config, run_analysts, end_date=end_date, provider_name=body.provider_name)
            return {"id": None, "status": run.status, "platform_key": body.platform_key, "dry_run": True, "summary": run.summary, "error": run.error}

    try:
        outcome = refresh_pool_locked(
            config,
            run_analysts,
            end_date=end_date,
            run_id=f"api-{body.platform_key}-{uuid.uuid4().hex[:12]}",
            session_factory=session_factory,
            provider_name=body.provider_name,
        )
    except PoolLockContendedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except PoolLockDatabaseLockedError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"id": outcome.db_run_id, "status": outcome.status, "platform_key": body.platform_key, "dry_run": False, "summary": outcome.summary, "error": outcome.error}


@router.get("/observing-pools/refresh-runs")
def list_refresh_runs(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    platform_key: str | None = Query(None),
    status: str | None = Query(None),
) -> list[dict]:
    """Refresh-run provenance, newest first. ``platform_key`` filters on the JSON list column in
    Python (SQLite JSON predicates are fragile and the row count is tiny)."""
    if platform_key is not None and platform_key not in PLATFORM_KEYS:
        raise HTTPException(status_code=404, detail=f"unknown platform '{platform_key}'")
    runs = db.query(PoolRefreshRun).order_by(PoolRefreshRun.id.desc()).limit(limit).all()
    if status is not None:
        runs = [r for r in runs if r.status == status]
    if platform_key is not None:
        runs = [r for r in runs if platform_key in (r.platform_keys or [])]
    return [_run_to_dict(r) for r in runs]


@router.get("/observing-pools/{platform_key}")
def get_pool(platform_key: str, db: Session = Depends(get_db)) -> dict:
    if platform_key not in PLATFORM_KEYS:
        raise HTTPException(status_code=404, detail=f"unknown platform '{platform_key}'")
    ranked = db.query(ObservationPoolEntry).filter_by(platform_key=platform_key).filter(ObservationPoolEntry.rank.isnot(None)).order_by(ObservationPoolEntry.rank).all()
    return {"platform_key": platform_key, "count": len(ranked), "entries": [_entry_to_dict(e) for e in ranked]}


@router.get("/serenity/research/{ticker}")
def get_serenity(ticker: str, db: Session = Depends(get_db), limit: int = Query(50, ge=1, le=200)) -> list[dict]:
    if not _TICKER_RE.match(ticker):
        raise HTTPException(status_code=422, detail=f"invalid ticker '{ticker}'")
    records = db.query(SerenityResearchRecord).filter_by(ticker=ticker.upper()).order_by(SerenityResearchRecord.id.desc()).limit(limit).all()
    return [
        {
            "id": r.id,
            "ticker": r.ticker,
            "platform_key": r.platform_key,
            "theme": r.theme,
            "chain_layer": r.chain_layer,
            "bottleneck_hypothesis": r.bottleneck_hypothesis,
            "evidence_grade": r.evidence_grade,
            "serenity_score": r.serenity_score,
            "recommended_action": r.recommended_action,
            "disclaimer": r.disclaimer,
            "disclaimer_version": r.disclaimer_version,
        }
        for r in records
    ]


@router.get("/opportunity-reports")
def list_reports(db: Session = Depends(get_db), limit: int = Query(50, ge=1, le=200)) -> list[dict]:
    reports = db.query(OpportunityReport).order_by(OpportunityReport.id.desc()).limit(limit).all()
    return [serialize_report(r) for r in reports]  # disclaimer invariant enforced here


@router.get("/opportunity-reports/{report_id}")
def get_report(report_id: int, db: Session = Depends(get_db)) -> dict:
    report = db.get(OpportunityReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"report {report_id} not found")
    return serialize_report(report)  # disclaimer invariant enforced here too (§9.9 every GET route)
