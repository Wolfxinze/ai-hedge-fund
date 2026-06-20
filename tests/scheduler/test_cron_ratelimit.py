"""Issue #18: minimum monitor schedule interval (cron rate-limit). Pure + offline, no wall-clock.

The Phase-9 write-facing monitor endpoints let an API client pick a schedule; this guard rejects a
schedule whose minimum fire interval is below a floor (default 3600s) so a heavy multi-minute LLM
monitor/refresh job can't be scheduled to fire faster than it completes. The floor lives in
``cron_map`` (the single seam the API, CLI, and scheduler all cross), NOT only at the endpoint.

Tests encode WHY: the minimum interval must catch the SHORT leg of an uneven cron (``0 0,8 * * *``
has an 8h and a 16h gap → min is 8h), the floor is inclusive so the blessed ``hourly`` keyword (exactly
3600s) passes, and the tolerant ``resolve_trigger`` is left untouched (scheduler reload still
skip-and-warns a stored sub-floor row rather than rejecting it).
"""

import pytest
from apscheduler.triggers.cron import CronTrigger

from src.scheduler.cron_map import (
    min_fire_interval_seconds,
    resolve_trigger,
    resolve_trigger_checked,
    ScheduleTooFrequentError,
)


@pytest.mark.parametrize(
    "cron,expected_seconds",
    [
        ("* * * * *", 60),
        ("*/5 * * * *", 300),
        ("0 * * * *", 3600),
        ("0 8 * * *", 86400),
        ("0 8 * * 1", 604800),
        ("0 0,8 * * *", 28800),  # uneven cron: 8h + 16h gaps → minimum is the 8h leg, NOT 16h
    ],
)
def test_min_fire_interval_seconds(cron, expected_seconds):
    assert min_fire_interval_seconds(CronTrigger.from_crontab(cron, timezone="UTC")) == expected_seconds


@pytest.mark.parametrize("schedule", ["hourly", "daily", "weekly", "monthly", "0 * * * *"])
def test_blessed_schedules_pass_default_floor(schedule):
    """Every documented keyword + the exactly-3600s 'hourly' cron clear the inclusive 3600 floor."""
    assert isinstance(resolve_trigger_checked(schedule), CronTrigger)


@pytest.mark.parametrize("schedule", ["* * * * *", "*/5 * * * *", "*/30 * * * *"])
def test_subfloor_schedules_rejected(schedule):
    with pytest.raises(ScheduleTooFrequentError) as exc:
        resolve_trigger_checked(schedule)
    assert exc.value.min_interval_seconds < exc.value.floor_seconds == 3600


def test_too_frequent_error_message_is_actionable():
    with pytest.raises(ScheduleTooFrequentError) as exc:
        resolve_trigger_checked("*/5 * * * *")
    msg = str(exc.value)
    assert "300" in msg and "3600" in msg and "MONITOR_MIN_INTERVAL_SECONDS" in msg


def test_floor_is_overridable_via_env(monkeypatch):
    monkeypatch.setenv("MONITOR_MIN_INTERVAL_SECONDS", "600")
    assert isinstance(resolve_trigger_checked("*/30 * * * *"), CronTrigger)  # 1800s clears the 600s floor
    with pytest.raises(ScheduleTooFrequentError):
        resolve_trigger_checked("*/5 * * * *")  # 300s < 600s floor → rejected
    # explicit floor arg wins over env
    with pytest.raises(ScheduleTooFrequentError):
        resolve_trigger_checked("0 * * * *", floor_seconds=86400)  # hourly rejected under a daily floor


@pytest.mark.parametrize("bad_env", ["0", "-5", "notanint"])
def test_invalid_env_floor_falls_back_to_default(monkeypatch, bad_env):
    monkeypatch.setenv("MONITOR_MIN_INTERVAL_SECONDS", bad_env)
    assert isinstance(resolve_trigger_checked("hourly"), CronTrigger)  # default 3600 admits hourly
    with pytest.raises(ScheduleTooFrequentError):
        resolve_trigger_checked("*/5 * * * *")


def test_unknown_schedule_raises_plain_valueerror_not_too_frequent():
    """A malformed/unknown schedule keeps raising the existing ValueError contract (callers map it
    distinctly from the frequency breach)."""
    with pytest.raises(ValueError) as exc:
        resolve_trigger_checked("not-a-schedule")
    assert not isinstance(exc.value, ScheduleTooFrequentError)


def test_resolve_trigger_unchanged_does_not_enforce_floor():
    """The tolerant resolve_trigger (used by the scheduler's skip-and-warn reload) must NOT reject a
    sub-floor cron — otherwise a stored too-frequent monitor would crash the build path differently."""
    assert isinstance(resolve_trigger("* * * * *"), CronTrigger)


def test_unsatisfiable_cron_returns_inf_and_is_admitted():
    """A cron firing at most once in the window (here an impossible date — Feb 31) yields inf and is
    ADMITTED: firing rarely is never a rate-limit concern. Documents the intended #18 semantics so a
    regression in the <2-fires branch would be caught."""
    trigger = CronTrigger.from_crontab("0 0 31 2 *", timezone="UTC")
    assert min_fire_interval_seconds(trigger) == float("inf")
    assert isinstance(resolve_trigger_checked("0 0 31 2 *"), CronTrigger)  # inf >= floor → passes
