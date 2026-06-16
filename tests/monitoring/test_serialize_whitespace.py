"""serialize_report must reject a WHITESPACE-ONLY disclaimer, not just an empty
string — the .strip() guard is a compliance invariant a refactor could silently
drop, letting a disclaimer-less report reach the API/export surface. Review Test-Gap-4.
"""

import pytest

from src.monitoring.serialize import DisclaimerError, serialize_report
from src.storage.models import OpportunityReport, ReportLabel


def test_rejects_whitespace_only_disclaimer():
    report = OpportunityReport(
        ticker="X",
        label=ReportLabel.MIXED.value,
        degraded=False,
        disclaimer="   ",
        disclaimer_version="  ",
    )
    with pytest.raises(DisclaimerError):
        serialize_report(report)


def test_accepts_valid_disclaimer():
    report = OpportunityReport(
        ticker="X",
        label=ReportLabel.MIXED.value,
        degraded=False,
        disclaimer="Research only — not investment advice.",
        disclaimer_version="2026-06",
    )
    out = serialize_report(report)
    assert out["disclaimer"] == "Research only — not investment advice."
