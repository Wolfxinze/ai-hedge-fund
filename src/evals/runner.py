"""Suite execution + transcript capture (PRD v4 §11).

Pure orchestration: runs each (already-stubbed, offline) grader ``trials`` times,
captures a ``Transcript``, and aggregates into a ``SuiteReport``. No network/LLM/
subprocess — suites inject stubbed seams. A grader that raises drops that trial to
False with the exception captured as the reason (fail-loud, never silently green).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.evals.core import EvalCase, EvalResult, GraderKind, Recorder, Transcript
from src.evals.metrics import pass_rate

logger = logging.getLogger(__name__)


def run_case(case: EvalCase) -> tuple[EvalResult, Transcript]:
    """Run one case ``trials`` times; return its result + transcript."""
    if case.kind is GraderKind.HUMAN:
        raise RuntimeError(f"case {case.case_id!r} uses a human grader — recorded, not run (use reporting.record_signoff)")

    transcript_recorder = Recorder()
    trials: list[bool] = []
    reasons: list[str] = []
    for i in range(max(1, case.trials)):
        recorder = transcript_recorder if i == 0 else Recorder()
        try:
            ok = case.grader(recorder)
        except Exception as exc:  # a raising grader is a FAIL, never a silent pass (Rule 12)
            ok = False
            reasons.append(f"trial {i} raised {type(exc).__name__}: {exc}")
        if not ok and not reasons:
            reasons.append(f"trial {i} returned False")
        trials.append(bool(ok))

    reason = "; ".join(reasons)
    result = EvalResult(case_id=case.case_id, suite=case.suite, kind=case.kind, target=case.target, trials=trials, reason=reason)
    transcript = Transcript(
        case_id=case.case_id,
        suite=case.suite,
        kind=case.kind,
        target=case.target,
        inputs=case.inputs,
        tool_calls=transcript_recorder.calls,
        trials=trials,
        passed=result.passed,
        reason=reason,
    )
    if not result.passed:
        logger.warning("eval FAIL %s/%s: %s", case.suite, case.case_id, reason or "(no reason)")
    return result, transcript


@dataclass(frozen=True)
class SuiteReport:
    """Aggregate over a run of many cases."""

    results: list[EvalResult]
    transcripts: list[Transcript]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def failures(self) -> list[EvalResult]:
        return [r for r in self.results if not r.passed]

    @property
    def passed_count(self) -> int:
        return self.total - len(self.failures)

    @property
    def all_passed(self) -> bool:
        return not self.failures

    def pass_rate_for(self, target: str) -> float:
        return pass_rate([r.passed for r in self.results if r.target == target])

    def summary(self) -> dict:
        """Bare-dict summary (matches the observing_pools route style, not the envelope)."""
        return {
            "total": self.total,
            "passed": self.passed_count,
            "failed": len(self.failures),
            "regression_pass_rate": self.pass_rate_for("regression"),
            "capability_pass_rate": self.pass_rate_for("capability"),
            "failures": [{"suite": r.suite, "case_id": r.case_id, "kind": r.kind.value, "reason": r.reason} for r in self.failures],
        }


def run_suite(cases: list[EvalCase]) -> SuiteReport:
    """Run every case; aggregate into a ``SuiteReport``."""
    results: list[EvalResult] = []
    transcripts: list[Transcript] = []
    for case in cases:
        result, transcript = run_case(case)
        results.append(result)
        transcripts.append(transcript)
    logger.info("ran %d eval case(s): %d passed, %d failed", len(results), len(results) - len([r for r in results if not r.passed]), len([r for r in results if not r.passed]))
    return SuiteReport(results=results, transcripts=transcripts)
