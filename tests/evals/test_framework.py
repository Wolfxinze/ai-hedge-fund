"""Framework mechanics: grader taxonomy, metrics, runner, transcript, reporting.

These verify the eval engine itself (no domain seams). Each test encodes WHY:
- a raising grader must FAIL, never silently pass (Rule 12);
- pass^k must actually distinguish all-pass from any-fail (a metric that can't
  fail is worthless, Rule 9);
- human graders are recorded, never run.
"""

import json

import pytest

from src.evals.core import (
    CodeGrader,
    EvalCase,
    GraderKind,
    HumanGrader,
    ModelGrader,
    Recorder,
    TARGET_CAPABILITY,
)
from src.evals.metrics import pass_at_k, pass_hat_k, pass_rate
from src.evals.reporting import record_signoff, signoff_recorded, write_transcripts
from src.evals.runner import run_case, run_suite


# ── metrics ──────────────────────────────────────────────────────────────────
def test_pass_at_k_is_any_and_pass_hat_k_is_all():
    assert pass_at_k([False, False, True]) is True
    assert pass_at_k([False, False, False]) is False
    assert pass_hat_k([True, True, True]) is True
    assert pass_hat_k([True, False, True]) is False  # one failure breaks pass^k
    assert pass_hat_k([]) is False  # vacuous all() must NOT count as a pass


def test_pass_at_k_respects_k_window():
    assert pass_at_k([False, True], k=1) is False  # only first trial considered
    assert pass_hat_k([True, False], k=1) is True


def test_pass_rate():
    assert pass_rate([True, True, False, False]) == 0.5
    assert pass_rate([]) == 0.0


# ── grader taxonomy ──────────────────────────────────────────────────────────
def test_code_grader_runs_and_records():
    def fn(rec: Recorder) -> bool:
        rec.record("widget", value=7)
        return True

    g = CodeGrader("g.code", fn)
    rec = Recorder()
    assert g.kind is GraderKind.CODE
    assert g(rec) is True
    assert rec.calls == [{"seam": "widget", "value": 7}]


def test_model_grader_uses_injected_judge():
    def fn(rec: Recorder, judge) -> bool:
        return judge("is the summary non-directional?")

    pass_judge = ModelGrader("g.model.pass", fn, judge=lambda _q: True)
    fail_judge = ModelGrader("g.model.fail", fn, judge=lambda _q: False)
    assert pass_judge.kind is GraderKind.MODEL
    assert pass_judge(Recorder()) is True
    assert fail_judge(Recorder()) is False  # the stub judge can genuinely fail


def test_human_grader_raises_when_run():
    g = HumanGrader("g.human")
    assert g.kind is GraderKind.HUMAN
    with pytest.raises(RuntimeError, match="recorded, not run"):
        g(Recorder())


# ── runner + transcript ──────────────────────────────────────────────────────
def _case(grader, *, trials=1, target="regression"):
    return EvalCase(case_id="c1", suite="s", grader=grader, trials=trials, target=target, inputs={"x": 1})


def test_run_case_passing_code_grader_builds_transcript():
    result, transcript = run_case(_case(CodeGrader("g", lambda rec: rec.record("seam", ok=True) or True)))
    assert result.passed is True
    assert transcript.passed is True
    assert transcript.inputs == {"x": 1}
    assert transcript.tool_calls == [{"seam": "seam", "ok": True}]
    assert transcript.to_dict()["case_id"] == "c1"


def test_run_case_raising_grader_fails_loudly_not_silently():
    def boom(_rec):
        raise ValueError("seam exploded")

    result, transcript = run_case(_case(CodeGrader("g", boom)))
    assert result.passed is False
    assert "ValueError" in result.reason and "seam exploded" in result.reason
    assert transcript.passed is False


def test_run_case_false_grader_records_reason():
    result, _ = run_case(_case(CodeGrader("g", lambda _rec: False)))
    assert result.passed is False
    assert "returned False" in result.reason


def test_pass_hat_k_gates_regression_consistency():
    # A grader that passes only on the first of 3 trials must FAIL a regression
    # case (pass^k), proving trials>1 asserts consistency, not best-of-k.
    state = {"n": 0}

    def flaky(_rec):
        state["n"] += 1
        return state["n"] == 1

    result, _ = run_case(_case(CodeGrader("g", flaky), trials=3, target="regression"))
    assert result.trials == [True, False, False]
    assert result.pass_at_k is True
    assert result.pass_hat_k is False
    assert result.passed is False  # regression uses pass^k


def test_capability_target_uses_pass_at_k():
    state = {"n": 0}

    def flaky(_rec):
        state["n"] += 1
        return state["n"] == 2

    result, _ = run_case(_case(CodeGrader("g", flaky), trials=3, target=TARGET_CAPABILITY))
    assert result.pass_hat_k is False
    assert result.pass_at_k is True
    assert result.passed is True  # capability uses pass@k


def test_run_case_refuses_human_grader():
    with pytest.raises(RuntimeError, match="recorded, not run"):
        run_case(_case(HumanGrader("g.human")))


def test_run_suite_aggregates_and_summarizes():
    cases = [
        EvalCase("ok", "s", CodeGrader("g1", lambda _r: True)),
        EvalCase("bad", "s", CodeGrader("g2", lambda _r: False)),
        EvalCase("cap", "s", CodeGrader("g3", lambda _r: False), target=TARGET_CAPABILITY),
    ]
    report = run_suite(cases)
    assert report.total == 3
    assert report.passed_count == 1
    assert report.all_passed is False
    summary = report.summary()
    assert summary["failed"] == 2
    assert {f["case_id"] for f in summary["failures"]} == {"bad", "cap"}
    assert summary["regression_pass_rate"] == 0.5  # ok passes, bad fails


# ── reporting: JSONL transcript + counsel sign-off ───────────────────────────
def test_write_transcripts_jsonl_roundtrip(tmp_path):
    _, transcript = run_case(_case(CodeGrader("g", lambda _r: True)))
    out = write_transcripts([transcript], tmp_path / "t.jsonl")
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["case_id"] == "c1" and row["passed"] is True


def test_write_transcripts_appends(tmp_path):
    _, t = run_case(_case(CodeGrader("g", lambda _r: True)))
    p = tmp_path / "t.jsonl"
    write_transcripts([t], p)
    write_transcripts([t], p)
    assert len(p.read_text(encoding="utf-8").splitlines()) == 2  # append, not overwrite


def test_signoff_recording(tmp_path):
    p = tmp_path / "signoff.jsonl"
    assert signoff_recorded(p) is False  # absent file
    record_signoff(p, reviewer="counsel", notes="reviewed loopback posture", approved=False)
    assert signoff_recorded(p) is False  # a not-approved line does not count
    record_signoff(p, reviewer="counsel", notes="approved for internal use", approved=True)
    assert signoff_recorded(p) is True
