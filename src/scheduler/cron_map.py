"""Schedule-string → cron / APScheduler trigger mapping (Phase 8).

Pure + import-side-effect-free. Aligns the ``schedule`` vocabulary used by ``MonitorConfig`` and
``HedgeFundFlowRun.schedule`` (hourly/daily/weekly/monthly + ``Granularity``) to canonical 5-field
cron expressions, and accepts a custom 5-field cron passthrough. ``resolve_*`` RAISE ``ValueError``
on an unknown/malformed value — callers decide the policy: the env-configured refresh cron should
fail loud at startup; a per-monitor row should be skipped-and-warned so one bad row can't take down
the whole scheduler.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

# Enum keyword → canonical 5-field cron. 'weekly' matches the OBSERVING_POOL_REFRESH_CRON default
# ('0 8 * * 1', Monday 08:00). Daily/monthly anchor at 08:00 to avoid a midnight thundering herd.
CRON_BY_KEYWORD: dict[str, str] = {
    "hourly": "0 * * * *",
    "daily": "0 8 * * *",
    "weekly": "0 8 * * 1",
    "monthly": "0 8 1 * *",
}


def resolve_cron(schedule: str) -> str:
    """Return a 5-field cron expression for a keyword or a custom 5-field cron passthrough.
    Raises ValueError on empty/unknown (does NOT range-validate a passthrough — ``resolve_trigger``
    does that via APScheduler)."""
    if not schedule or not str(schedule).strip():
        raise ValueError("empty schedule string")
    key = str(schedule).strip().lower()
    if key in CRON_BY_KEYWORD:
        return CRON_BY_KEYWORD[key]
    if len(key.split()) == 5:  # custom 5-field cron passthrough
        return key
    raise ValueError(f"unknown schedule {schedule!r} (expected one of {sorted(CRON_BY_KEYWORD)} or a 5-field cron)")


def resolve_trigger(schedule: str) -> CronTrigger:
    """Map ``schedule`` to a UTC APScheduler ``CronTrigger``. Raises ValueError on an unknown
    keyword OR a malformed custom cron (range errors surface from ``CronTrigger.from_crontab``).

    Deliberately does NOT enforce the Issue-#18 frequency floor: the scheduler's reload
    (``scheduler.py``) calls this and skip-and-warns one bad row, so a stored sub-floor monitor must
    not crash the build. The WRITE-endpoint boundary calls ``resolve_trigger_checked`` instead."""
    return CronTrigger.from_crontab(resolve_cron(schedule), timezone="UTC")


# ── Issue #18: minimum monitor schedule interval (cron rate-limit) ───────────────────────────────

# A fixed UTC anchor for interval sampling — pure (no wall-clock) so the computation is deterministic
# and DST-immune (triggers are UTC). Chosen far from any DST-style edge; the exact value is irrelevant.
_SAMPLE_ANCHOR = datetime(2024, 1, 1, tzinfo=timezone.utc)
_SAMPLE_FIRES = 70  # spans an hour at minute-resolution AND catches the short leg of multi-time crons


class ScheduleTooFrequentError(ValueError):
    """A schedule's minimum fire interval is below the configured floor (Issue #18). Subclass of
    ValueError so an unguarded caller still treats it as bad input, but the write endpoint maps it to
    a distinct, actionable 422 message."""

    def __init__(self, min_interval_seconds: int, floor_seconds: int) -> None:
        super().__init__(
            f"schedule fires every {min_interval_seconds}s; minimum allowed is {floor_seconds}s "
            "(set MONITOR_MIN_INTERVAL_SECONDS to change)"
        )
        self.min_interval_seconds = min_interval_seconds
        self.floor_seconds = floor_seconds


def _min_interval_floor() -> int:
    """MONITOR_MIN_INTERVAL_SECONDS (default 3600). Mirrors pool_lock._ttl_default's defensive parse:
    an invalid value warns and falls back rather than crashing the write path. 3600 admits the most
    frequent blessed keyword ('hourly' == exactly 3600s) while rejecting */N-minute abuse."""
    raw = os.environ.get("MONITOR_MIN_INTERVAL_SECONDS")
    if raw is None:
        return 3600
    try:
        v = int(raw)
        if v > 0:
            return v
    except (TypeError, ValueError):
        pass
    logger.warning("invalid MONITOR_MIN_INTERVAL_SECONDS=%r; using default 3600", raw)
    return 3600


def min_fire_interval_seconds(trigger: CronTrigger, *, samples: int = _SAMPLE_FIRES) -> float:
    """The smallest gap (seconds) between consecutive fires of ``trigger``, by sampling a window of
    consecutive fire times from a fixed UTC anchor. Sampling (not a single next-after-next delta) is
    required so an uneven cron (e.g. ``0 0,8 * * *`` = 8h then 16h) reports its SHORT leg. Returns
    ``inf`` for a trigger that fires at most once (never rate-limited)."""
    fires: list[datetime] = []
    prev: datetime | None = None
    cursor = _SAMPLE_ANCHOR
    for _ in range(samples):
        nxt = trigger.get_next_fire_time(prev, cursor)
        if nxt is None:
            break
        fires.append(nxt)
        prev = nxt
        cursor = nxt + timedelta(seconds=1)  # step strictly forward so the loop always advances
    if len(fires) < 2:
        return float("inf")
    return min((b - a).total_seconds() for a, b in zip(fires, fires[1:]))


def resolve_trigger_checked(schedule: str, *, floor_seconds: int | None = None) -> CronTrigger:
    """``resolve_trigger`` + the Issue-#18 frequency floor. Raises the existing ValueError on an
    unknown/malformed schedule, or ``ScheduleTooFrequentError`` when the minimum fire interval is
    BELOW the floor (inclusive: a schedule firing exactly at the floor passes). The WRITE endpoints
    (POST/PATCH /monitors) call this; the scheduler reload keeps using the tolerant ``resolve_trigger``."""
    trigger = resolve_trigger(schedule)
    floor = floor_seconds if floor_seconds is not None else _min_interval_floor()
    interval = min_fire_interval_seconds(trigger)
    if interval < floor:
        raise ScheduleTooFrequentError(int(interval), floor)
    return trigger
