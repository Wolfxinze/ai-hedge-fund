"""Net-new, Python-native eval framework for the Observing-Pools / Serenity
research workflow (PRD v4 §11 — Evals & Productization).

Public surface: the grader taxonomy (code/model/human), the case/result/transcript
types, pass@k / pass^k metrics, the runner, the suite registry, and reporting.
All offline by construction — suites inject stubbed seams; nothing touches the
network, an LLM, a timer, or any trade path.
"""

from src.evals.core import (
    CAPABILITY_THRESHOLD,
    CodeGrader,
    EvalCase,
    EvalResult,
    Grader,
    GraderKind,
    HumanGrader,
    ModelGrader,
    Recorder,
    REGRESSION_THRESHOLD,
    TARGET_CAPABILITY,
    TARGET_REGRESSION,
    Transcript,
)
from src.evals.metrics import pass_at_k, pass_hat_k, pass_rate
from src.evals.registry import build_all, build_suite, registered_suites, suite
from src.evals.runner import run_case, run_suite, SuiteReport

__all__ = [
    "CAPABILITY_THRESHOLD",
    "REGRESSION_THRESHOLD",
    "TARGET_CAPABILITY",
    "TARGET_REGRESSION",
    "CodeGrader",
    "EvalCase",
    "EvalResult",
    "Grader",
    "GraderKind",
    "HumanGrader",
    "ModelGrader",
    "Recorder",
    "SuiteReport",
    "Transcript",
    "build_all",
    "build_suite",
    "pass_at_k",
    "pass_hat_k",
    "pass_rate",
    "registered_suites",
    "run_case",
    "run_suite",
    "suite",
]
