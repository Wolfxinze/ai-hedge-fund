"""Phase 8: cron_map. Pure, offline, no wall-clock. Verifies the enum keywords map to the
canonical crons, custom cron passes through, the 'weekly' default aligns with
OBSERVING_POOL_REFRESH_CRON, and unknown/malformed values RAISE (callers decide skip vs fail-loud).
"""

import pytest
from apscheduler.triggers.cron import CronTrigger

from src.scheduler.cron_map import CRON_BY_KEYWORD, resolve_cron, resolve_trigger
from src.storage.models import Granularity


@pytest.mark.parametrize(
    "keyword,expected_cron",
    [("hourly", "0 * * * *"), ("daily", "0 8 * * *"), ("weekly", "0 8 * * 1"), ("monthly", "0 8 1 * *")],
)
def test_keyword_maps_to_canonical_cron(keyword, expected_cron):
    assert resolve_cron(keyword) == expected_cron
    assert isinstance(resolve_trigger(keyword), CronTrigger)


def test_keyword_is_case_insensitive():
    assert resolve_cron("WEEKLY") == "0 8 * * 1"


def test_weekly_aligns_with_refresh_cron_default():
    """The 'weekly' keyword must equal the OBSERVING_POOL_REFRESH_CRON default so a monitor on
    'weekly' and the default pool refresh fire on the same Monday-08:00 cadence."""
    assert CRON_BY_KEYWORD["weekly"] == "0 8 * * 1"


@pytest.mark.parametrize("g", list(Granularity))
def test_all_granularities_except_custom_resolve(g):
    """Every Granularity except CUSTOM is a known keyword; CUSTOM has no fixed cron (it carries a
    per-monitor cron string), so it correctly raises here."""
    if g is Granularity.CUSTOM:
        with pytest.raises(ValueError):
            resolve_trigger(g.value)
    else:
        assert isinstance(resolve_trigger(g.value), CronTrigger)


def test_custom_cron_passthrough():
    assert resolve_cron("*/5 * * * *") == "*/5 * * * *"
    assert isinstance(resolve_trigger("0 9 * * 2"), CronTrigger)


@pytest.mark.parametrize("bad", ["", "   ", "garbage", "every day", None])
def test_unknown_schedule_raises(bad):
    with pytest.raises(ValueError):
        resolve_cron(bad)
    with pytest.raises(ValueError):
        resolve_trigger(bad)


def test_malformed_custom_cron_raises_via_trigger():
    """A 5-field-shaped but out-of-range cron passes resolve_cron's shape check but must raise when
    APScheduler validates field ranges in resolve_trigger."""
    assert resolve_cron("61 * * * *") == "61 * * * *"  # shape OK
    with pytest.raises(ValueError):
        resolve_trigger("61 * * * *")  # minute 61 invalid → from_crontab raises
