"""Per-platform refresh lock — the PRD §10 / X1 claim-row protocol (Phase 8).

SQLite cannot take a per-platform write lock (``BEGIN IMMEDIATE`` locks the whole DB), so the
sole serialisation mechanism is the ``pool_locks`` claim-row: a refresh ATOMICALLY claims its
platform's row in a short transaction, runs the long LLM refresh OUTSIDE the lock, then releases
the row. Different platforms never contend (independent rows); the same platform serialises (the
second claimant sees a live row and is told it's contended — never silently dropped).

The lost-update guard (must-fix #5) is a ``fence`` generation token: the claim atomically bumps
it (``fence = fence + 1``) and the claimant captures the new value; release only deletes the row
if the fence still matches. So a slow-but-alive holder whose lock was stolen after its TTL
expired releases as a NO-OP (fence mismatch) instead of clobbering the new holder's lock.

These helpers do NOT commit — the caller's ``session_scope`` owns the short transaction, so the
claim row is committed (and visible to other connections) exactly when that scope exits, keeping
the long refresh strictly outside any lock-holding transaction.
"""

import logging
import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from src.observing_pools.pipeline import refresh_pool, RefreshConfig, RunAnalysts
from src.storage import session_scope
from src.storage.models import PoolLock

logger = logging.getLogger(__name__)


def _ttl_default() -> int:
    """POOL_LOCK_TTL_SECONDS (default 3600). A safety net for a crashed holder, not the normal
    release path — a refresh that completes/raises releases immediately via the fenced delete."""
    raw = os.environ.get("POOL_LOCK_TTL_SECONDS")
    if raw is None:
        return 3600
    try:
        v = int(raw)
        if v > 0:
            return v
    except (TypeError, ValueError):
        pass
    logger.warning("invalid POOL_LOCK_TTL_SECONDS=%r; using default 3600", raw)
    return 3600


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PoolLockContendedError(RuntimeError):
    """A live (non-expired) lock row is held by another runner for this platform."""

    def __init__(self, platform_key: str) -> None:
        super().__init__(f"pool lock for '{platform_key}' is held by another runner")
        self.platform_key = platform_key


class PoolLockDatabaseLockedError(RuntimeError):
    """SQLite signalled 'database is locked' while claiming — surfaced, never swallowed."""

    def __init__(self, platform_key: str) -> None:
        super().__init__(f"SQLite 'database is locked' acquiring pool lock for '{platform_key}'")
        self.platform_key = platform_key


@dataclass(frozen=True)
class RefreshOutcome:
    """A detached snapshot of the refresh result (the ORM run is gone once its session closes)."""

    status: str
    error: str | None
    summary: dict | None
    fence: int
    db_run_id: int | None


def acquire_pool_lock(
    session: Session,
    platform_key: str,
    run_id: str,
    *,
    clock: Callable[[], datetime] = _utc_now,
    ttl_seconds: int | None = None,
) -> int:
    """Atomically claim ``platform_key``'s lock; return the captured ``fence``. Raises
    ``PoolLockContendedError`` if a live lock exists, ``PoolLockDatabaseLockedError`` on a SQLite
    busy-timeout. Does NOT commit — the caller's transaction makes the claim durable/visible."""
    ttl = ttl_seconds if ttl_seconds is not None else _ttl_default()
    now = clock()
    new_expires = now + timedelta(seconds=ttl)
    try:
        # Steal-if-expired: a single atomic UPDATE gated on expiry — no SELECT-then-UPDATE TOCTOU.
        # rowcount==1 means we won the (only) expired row; the fence is bumped atomically.
        stolen = session.execute(
            update(PoolLock)
            .where(PoolLock.platform_key == platform_key, PoolLock.expires_at < now)
            .values(locked_by=run_id, locked_at=now, expires_at=new_expires, fence=PoolLock.fence + 1)
        )
        if stolen.rowcount == 1:
            return session.execute(select(PoolLock.fence).where(PoolLock.platform_key == platform_key)).scalar_one()
        # No expired row to steal → either no row (fresh claim) or a live row (contended).
        session.add(PoolLock(platform_key=platform_key, locked_at=now, locked_by=run_id, expires_at=new_expires, fence=1))
        session.flush()  # forces the INSERT now so a PK clash surfaces as IntegrityError here
        return 1
    except IntegrityError:
        session.rollback()
        raise PoolLockContendedError(platform_key)
    except OperationalError as exc:
        session.rollback()
        if "database is locked" in str(exc).lower():
            raise PoolLockDatabaseLockedError(platform_key)
        raise


def release_pool_lock(session: Session, platform_key: str, fence: int, run_id: str) -> bool:
    """Fenced release: delete the row only if we still own this ``fence``. Returns True on a real
    release; False (with a WARNING) if the lock was stolen mid-run — a stale holder must never
    clobber the new holder. Does NOT commit (caller's transaction)."""
    result = session.execute(
        delete(PoolLock).where(
            PoolLock.platform_key == platform_key, PoolLock.locked_by == run_id, PoolLock.fence == fence
        )
    )
    if result.rowcount == 0:
        logger.warning(
            "pool_lock release no-op: platform=%s run=%s fence=%d (lock was stolen or already released)",
            platform_key, run_id, fence,
        )
        return False
    return True


def refresh_pool_locked(
    config: RefreshConfig,
    run_analysts: RunAnalysts,
    *,
    end_date: str,
    run_id: str,
    session_factory: Callable[[], AbstractContextManager[Session]] = session_scope,
    clock: Callable[[], datetime] = _utc_now,
    ttl_seconds: int | None = None,
    provider_name: str = "yfinance",
) -> RefreshOutcome:
    """Canonical PoolLock-guarded refresh used by the CLI, the scheduler job, and (future) the
    API. Claim → run ``refresh_pool`` OUTSIDE the lock transaction → fenced release in a finally
    (so a crash/exception still frees the lock). Same platform serialises (raises
    ``PoolLockContendedError``); different platforms proceed concurrently."""
    with session_factory() as lock_s:
        fence = acquire_pool_lock(lock_s, config.platform_key, run_id, clock=clock, ttl_seconds=ttl_seconds)
    try:
        with session_factory() as s:
            run = refresh_pool(s, config, run_analysts, end_date=end_date, provider_name=provider_name)
            outcome = RefreshOutcome(status=run.status, error=run.error, summary=run.summary, fence=fence, db_run_id=run.id)
        return outcome
    finally:
        with session_factory() as lock_s:
            release_pool_lock(lock_s, config.platform_key, fence, run_id)


@contextmanager
def pool_lock(
    platform_key: str,
    run_id: str,
    *,
    session_factory: Callable[[], AbstractContextManager[Session]] = session_scope,
    clock: Callable[[], datetime] = _utc_now,
    ttl_seconds: int | None = None,
) -> Iterator[int]:
    """Standalone guard for callers that aren't a pool refresh (e.g. a future bulk op): claims the
    lock, yields the fence, and releases it in a finally. Raises on contention/db-locked."""
    with session_factory() as lock_s:
        fence = acquire_pool_lock(lock_s, platform_key, run_id, clock=clock, ttl_seconds=ttl_seconds)
    try:
        yield fence
    finally:
        with session_factory() as lock_s:
            release_pool_lock(lock_s, platform_key, fence, run_id)
