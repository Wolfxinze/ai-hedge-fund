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


def test_evidence_grader_catches_substantiation_leak(monkeypatch):
    import src.evals.suites.evidence as ev

    # Regression: is_substantiated returns True for everything -> the 200-irrelevant
    # corpus would (wrongly) all substantiate.
    monkeypatch.setattr(ev, "is_substantiated", lambda claim, excerpt: True)
    result, _ = run_case(_case("evidence", "irrelevant_200_not_substantiated"))
    assert result.passed is False


def test_injection_grader_catches_payload_leak(monkeypatch):
    import src.evals.suites.injection as inj
    from src.storage.models import SourceType

    # Regression: classify_reference is tricked into marking everything FILING +
    # substantiated (as if the injected text set the fields) -> grader must catch it.
    monkeypatch.setattr(inj, "classify_reference", lambda **kw: {"source_host": "x", "source_type": SourceType.FILING, "substantiated": True, "reason": "ok"})
    result, _ = run_case(_case("injection", "payload_cannot_flip_source_type_or_substantiation"))
    assert result.passed is False


def test_classification_grader_catches_substring_leak(monkeypatch):
    import src.evals.suites.classification as cl

    # Regression: the classifier fires 'ai' on every input -> the substring trap
    # ('ai' in 'Internet Retail') is no longer blocked.
    monkeypatch.setattr(cl, "classify_candidate", lambda **kw: {"ai": None})
    result, _ = run_case(_case("classification", "substring_false_positives_blocked"))
    assert result.passed is False


def test_no_trade_grader_catches_planted_forbidden_import(monkeypatch):
    import src.evals.suites.no_trade as nt

    # Regression: a forbidden substring that DOES occur in the scanned modules'
    # real imports (scoring_graph imports src.utils.analysts) -> the AST scan must
    # flip to FAIL, proving it actually inspects imports (not a vacuous pass).
    monkeypatch.setattr(nt, "_FORBIDDEN_IMPORT_SUBSTRINGS", ("analysts",))
    result, _ = run_case(_case("no_trade", "modules_have_no_direct_trade_imports"))
    assert result.passed is False


def test_no_trade_grader_scans_quant_module(monkeypatch):
    import src.evals.suites.no_trade as nt

    # The new quant-package scan must genuinely inspect src/quant/volatility.py.
    # `statistics` is imported ONLY by the quant module (none of the observing_pools
    # scanned modules import it), so treating it as forbidden leaves the
    # observing_pools scan clean and forces the QUANT scan to bite -> proving a
    # PLANTED `import src.agents.risk_manager` in volatility.py would be caught.
    monkeypatch.setattr(nt, "_FORBIDDEN_IMPORT_SUBSTRINGS", ("statistics",))
    result, _ = run_case(_case("no_trade", "modules_have_no_direct_trade_imports"))
    assert result.passed is False
