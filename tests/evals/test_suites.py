"""The domain eval suites must (a) all register, (b) all PASS on the current code
(they are regression gates), and (c) genuinely FAIL when their seam regresses
(Rule 9: a grader that cannot fail is worthless). Fully offline.
"""

from src.evals.registry import build_all, build_suite, registered_suites
from src.evals.runner import run_case, run_suite

_EXPECTED_SUITES = {"classification", "disclaimer", "evidence", "injection", "no_trade", "scoring", "ssrf"}


def _case(suite_name, case_id):
    return next(c for c in build_suite(suite_name) if c.case_id == case_id)


def test_all_suites_registered():
    assert set(registered_suites()) == _EXPECTED_SUITES


def test_full_suite_is_green():
    """Every Phase-11 regression eval passes on the current (hardened) code."""
    report = run_suite(build_all())
    assert report.total >= 25  # 7 suites worth of cases
    assert report.all_passed, report.summary()["failures"]
    assert report.pass_rate_for("regression") == 1.0


# ── non-vacuity: each marquee grader must catch a planted regression ──────────
def test_disclaimer_grader_catches_removed_chokepoint(monkeypatch):
    import src.evals.suites.disclaimer as d

    # Regression: serialize_report no longer refuses a blank disclaimer.
    monkeypatch.setattr(d, "serialize_report", lambda report: {"disclaimer": ""})
    result, _ = run_case(_case("disclaimer", "serialize_refuses_blank"))
    assert result.passed is False


def test_ssrf_grader_catches_broken_validator(monkeypatch):
    import src.evals.suites.ssrf as s

    # Regression: _validate_ip accepts everything (SSRF wide open).
    monkeypatch.setattr(s, "_validate_ip", lambda ip: True)
    result, _ = run_case(_case("ssrf", "validate_ip_matrix"))
    assert result.passed is False


def test_scoring_grader_catches_degraded_masking(monkeypatch):
    import src.evals.suites.scoring as sc
    from src.observing_pools import scoring as real_scoring

    # Regression: composite returns a number even when a REQUIRED component is missing
    # (i.e. a degraded/missing value no longer excludes -> it could outrank a bearish).
    monkeypatch.setattr(sc, "composite", lambda *a, **k: 99.0)
    result, _ = run_case(_case("scoring", "degraded_never_outranks_bearish"))
    assert result.passed is False
    # sanity: the real composite is untouched outside the patch
    assert real_scoring.composite is not None
