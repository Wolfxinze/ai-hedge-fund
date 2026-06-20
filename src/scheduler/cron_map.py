"""Schedule-string → cron / APScheduler trigger mapping (Phase 8).

Pure + import-side-effect-free. Aligns the ``schedule`` vocabulary used by ``MonitorConfig`` and
``HedgeFundFlowRun.schedule`` (hourly/daily/weekly/monthly + ``Granularity``) to canonical 5-field
cron expressions, and accepts a custom 5-field cron passthrough. ``resolve_*`` RAISE ``ValueError``
on an unknown/malformed value — callers decide the policy: the env-configured refresh cron should
fail loud at startup; a per-monitor row should be skipped-and-warned so one bad row can't take down
the whole scheduler.
"""

import logging

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
    keyword OR a malformed custom cron (range errors surface from ``CronTrigger.from_crontab``)."""
    return CronTrigger.from_crontab(resolve_cron(schedule), timezone="UTC")
