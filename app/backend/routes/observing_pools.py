"""Read-only API for observing pools, reports, and platforms (PRD v4 §14).

Loopback-bound (research-only). Every report is projected through
``serialize_report`` so the disclaimer invariant holds on the API surface too.
"""

import logging
import re
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.backend.database.connection import get_db
from src.monitoring.serialize import serialize_report, serialize_serenity
from src.observing_pools.pipeline import (
    DEFAULT_UNIVERSE,
    refresh_pool,
    RefreshConfig,
    RunAnalysts,
)
from src.observing_pools.platforms import PLATFORM_KEYS
from src.observing_pools.pool_lock import (
    PoolLockContendedError,
    PoolLockDatabaseLockedError,
    refresh_pool_locked,
)
from src.serenity.grading import SCORECARD_DIMENSIONS
from src.serenity.research import build_record
from src.storage import session_scope
from src.storage.models import (
    InnovationPlatform,
    ObservationPoolEntry,
    OpportunityReport,
    PoolRefreshRun,
    RefreshRunStatus,
    SerenityResearchRecord,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_REFRESH_RUN_STATUSES = {s.value for s in RefreshRunStatus}

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
        try:
            with session_factory() as s:
                run = refresh_pool(s, config, run_analysts, end_date=end_date, provider_name=body.provider_name)
                return {"id": None, "status": run.status, "platform_key": body.platform_key, "dry_run": True, "summary": run.summary, "error": run.error}
        except Exception:
            # Fail loud WITH context (no forensic trail otherwise — dry-run persists no run row), and
            # surface a generic 500 rather than leaking a raw exception/SQL string to the client.
            logger.exception("dry-run refresh failed platform=%s", body.platform_key)
            raise HTTPException(status_code=500, detail=f"refresh failed for '{body.platform_key}'")

    try:
        outcome = refresh_pool_locked(
            config,
            run_analysts,
            end_date=end_date,
            run_id=f"api-{body.platform_key}-{uuid.uuid4().hex}",
            session_factory=session_factory,
            provider_name=body.provider_name,
        )
    except PoolLockContendedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except PoolLockDatabaseLockedError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception:
        logger.exception("refresh failed platform=%s", body.platform_key)
        raise HTTPException(status_code=500, detail=f"refresh failed for '{body.platform_key}'")
    return {"id": outcome.db_run_id, "status": outcome.status, "platform_key": body.platform_key, "dry_run": False, "summary": outcome.summary, "error": outcome.error}


@router.get("/observing-pools/refresh-runs")
def list_refresh_runs(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    platform_key: str | None = Query(None),
    status: str | None = Query(None),
) -> list[dict]:
    """Refresh-run provenance, newest first. Filters are applied BEFORE the limit so ``limit`` bounds
    the MATCHING set (not a pre-truncated window that newer non-matching rows could exhaust). The
    ``platform_key`` JSON-list filter runs in Python (SQLite JSON predicates are fragile); the run
    table is low-cardinality (weekly refresh × platforms), so the unbounded read is cheap and the
    response stays bounded by ``limit``."""
    if platform_key is not None and platform_key not in PLATFORM_KEYS:
        raise HTTPException(status_code=404, detail=f"unknown platform '{platform_key}'")
    if status is not None and status not in _REFRESH_RUN_STATUSES:
        # Validate like platform_key (a typo must not masquerade as "no runs in that state").
        raise HTTPException(status_code=422, detail=f"unknown status '{status}' (expected one of {sorted(_REFRESH_RUN_STATUSES)})")
    query = db.query(PoolRefreshRun).order_by(PoolRefreshRun.id.desc())
    if status is not None:
        query = query.filter(PoolRefreshRun.status == status)
    runs = query.all() if platform_key is not None else query.limit(limit).all()
    if platform_key is not None:
        runs = [r for r in runs if platform_key in (r.platform_keys or [])][:limit]
    return [_run_to_dict(r) for r in runs]


@router.get("/observing-pools/{platform_key}")
def get_pool(platform_key: str, db: Session = Depends(get_db)) -> dict:
    if platform_key not in PLATFORM_KEYS:
        raise HTTPException(status_code=404, detail=f"unknown platform '{platform_key}'")
    ranked = db.query(ObservationPoolEntry).filter_by(platform_key=platform_key).filter(ObservationPoolEntry.rank.isnot(None)).order_by(ObservationPoolEntry.rank).all()
    return {"platform_key": platform_key, "count": len(ranked), "entries": [_entry_to_dict(e) for e in ranked]}


# ROUTE-SHADOW (Issue #21): a future write-result lookup-by-id should be registered as
# `/serenity/research/by-id/{id}`. The literal `by-id` segment makes it a DISTINCT two-segment path that
# the single-segment `{ticker}` route cannot match, so it resolves correctly regardless of declaration
# order. Do NOT instead add a bare `/serenity/research/{id}`: that collides with `{ticker}` on the SAME
# segment (every `/serenity/research/<x>` matches both), forcing a fragile declaration-order dependency
# — exactly the trap the `by-id` prefix avoids. (That same-segment, order-dependent case does exist in
# this module: `/observing-pools/refresh`[-runs] is declared before `/observing-pools/{platform_key}`.)
@router.get("/serenity/research/{ticker}")
def get_serenity(ticker: str, db: Session = Depends(get_db), limit: int = Query(50, ge=1, le=200)) -> list[dict]:
    if not _TICKER_RE.match(ticker):
        raise HTTPException(status_code=422, detail=f"invalid ticker '{ticker}'")
    records = db.query(SerenityResearchRecord).filter_by(ticker=ticker.upper()).order_by(SerenityResearchRecord.id.desc()).limit(limit).all()
    return [serialize_serenity(r) for r in records]  # disclaimer invariant enforced here (§9.9 every GET route)


# The sources the discover flow may fan (mirrors gather._REGISTRY; "patents" is intentionally
# absent there — number-driven, not ticker-driven). Validated here so a typo fails loud with a
# 422 instead of gather's silent skip-with-warning.
_DISCOVER_SOURCES = ("edgar", "federal_register")


class DiscoverRequest(BaseModel):
    """Body for POST /serenity/discover. Bounded + explicit: the scorecard is the caller's
    human judgment (never defaulted server-side — a fabricated mid score would masquerade as
    analysis), and sources are restricted to the allowlisted ticker-driven adapters."""

    ticker: str
    theme: str
    keywords: list[str] = Field(..., max_length=20)
    platform_key: str | None = None
    chain_layer: str | None = None
    hypothesis: str | None = None
    sources: list[str] = Field(default_factory=lambda: list(_DISCOVER_SOURCES), max_length=len(_DISCOVER_SOURCES))
    max_per_source: int = Field(3, ge=1, le=10)
    scorecard: dict[str, int]


def get_gatherer():
    """The production multi-source evidence gatherer (EDGAR + Federal Register). Imported lazily
    so this module stays import-offline; tests override this with a stub so no request ever
    reaches the real adapters/network."""
    from src.serenity.adapters.gather import gather_references

    return gather_references


@router.post("/serenity/discover")
def discover_serenity(body: DiscoverRequest, db: Session = Depends(get_db), gatherer=Depends(get_gatherer)) -> dict:
    """UI-triggered evidence discovery: fan the source adapters for a ticker, then build one
    research record per non-empty source group (each with that source's fetch headers) — the API
    twin of ``python -m src.serenity discover``. Research-only: produces SerenityResearchRecords
    (disclaimer invariant via serialize_serenity), never a trade/order path. Outbound calls go
    only to allowlisted hosts through the SSRF-guarded fetcher (build_record → fetch_excerpt)."""
    ticker = body.ticker.strip().upper()
    if not _TICKER_RE.match(ticker):
        raise HTTPException(status_code=422, detail=f"invalid ticker '{body.ticker}'")
    theme = body.theme.strip()
    if not theme:
        raise HTTPException(status_code=422, detail="theme must not be blank")
    keywords = [k.strip() for k in body.keywords if k.strip()]
    if not keywords:
        raise HTTPException(status_code=422, detail="keywords must contain at least one non-blank entry")
    if body.platform_key is not None and body.platform_key not in PLATFORM_KEYS:
        raise HTTPException(status_code=404, detail=f"unknown platform '{body.platform_key}'")
    unknown_sources = [s for s in body.sources if s not in _DISCOVER_SOURCES]
    if unknown_sources:
        raise HTTPException(status_code=422, detail=f"unknown source(s) {unknown_sources} (expected subset of {list(_DISCOVER_SOURCES)})")
    # Order-preserving dedup: a repeated source name would otherwise re-invoke the builder and let
    # gather's per-source counts[name] overwrite the first pass — plus it amplifies outbound requests.
    sources = tuple(dict.fromkeys(body.sources))
    if set(body.scorecard) != set(SCORECARD_DIMENSIONS) or any(not 0 <= v <= 4 for v in body.scorecard.values()):
        raise HTTPException(status_code=422, detail=f"scorecard must map exactly {list(SCORECARD_DIMENSIONS)} to ints 0-4")

    try:
        result = gatherer(ticker, keywords=keywords, sources=sources, max_per_source=body.max_per_source)
    except Exception:
        # Fail loud in the log WITH the traceback, but never leak the raw exception to the client.
        logger.exception("serenity discover: gather failed ticker=%s", ticker)
        raise HTTPException(status_code=502, detail=f"evidence gathering failed for '{ticker}'")
    if result.errors and all(s in result.errors for s in sources):
        # EVERY requested source errored — an upstream FAILURE, not an empty result (CLI parity).
        # (A source that legitimately returns zero refs while another errors falls through to a 200
        # with records=[] + source_errors populated: "no evidence found + one degraded source".)
        raise HTTPException(status_code=502, detail=f"all evidence sources errored for '{ticker}': {result.errors}")

    # One record per source group, each committed independently so a later group failing neither
    # rolls back earlier records nor reports a record that was never persisted (CLI parity).
    records = []
    failed_groups = 0
    for fetch_headers, refs in result.groups:
        if not refs:
            continue
        try:
            record = build_record(
                db,
                theme=theme,
                ticker=ticker,
                platform_key=body.platform_key,
                chain_layer=body.chain_layer,
                bottleneck_hypothesis=body.hypothesis,
                scorecard=body.scorecard,
                references=refs,
                fetch_missing=True,
                fetch_headers=fetch_headers,
            )
            db.commit()  # get_db does not commit
            records.append(record)
        except Exception:
            db.rollback()
            failed_groups += 1
            logger.exception("serenity discover: build_record failed for a source group ticker=%s", ticker)
    if failed_groups and not records:
        raise HTTPException(status_code=500, detail=f"discover failed to build any record for '{ticker}'")
    return {
        "ticker": ticker,
        "records": [serialize_serenity(r) for r in records],  # disclaimer invariant on the write surface too
        "reference_count": len(result.references),
        "source_errors": result.errors,
        "failed_groups": failed_groups,
    }


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
