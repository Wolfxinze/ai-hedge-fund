"""Core abstractions for the Observing-Pools / Serenity eval framework (PRD v4 §11).

Net-new, Python-native (not the bun/TS PAI Evals skill). Adapts Anthropic's
"Demystifying Evals for AI Agents" three-grader taxonomy:

  * ``CodeGrader``  — deterministic, fast, reproducible. The default here, because
    every Phase-11 acceptance target (disclaimer, SSRF, evidence grading,
    classification, scoring) is deterministic in the existing code.
  * ``ModelGrader`` — for genuine free-text nuance only. Takes an *injected* judge
    callable; the offline default raises, so a suite must supply a stub judge.
    Forbidden by construction from grading evidence / setting ``source_type`` /
    touching any scoring or trade path (PRD §11.5: the LLM never grades).
  * ``HumanGrader`` — recorded, never run inline (counsel sign-off, calibration).
    ``grade`` raises; ``record`` appends to the reporting log.

A ``Grader`` is the reusable mechanism; an ``EvalCase`` is a grader plus run
config (trials, regression/capability target) and the metadata a ``Transcript``
captures for inspectability. Nothing here does I/O at import or touches the
network/LLM — suites inject already-stubbed seams.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from src.evals.metrics import pass_at_k as _pass_at_k
from src.evals.metrics import pass_hat_k as _pass_hat_k

# Anthropic targets: regression suites are quality gates (~99-100%); capability
# suites are stretch goals (~70%). Stored on the case so the runner/report can
# decide gate-vs-informational without hard-coding per suite.
TARGET_REGRESSION = "regression"
TARGET_CAPABILITY = "capability"

REGRESSION_THRESHOLD = 1.0  # deterministic graders: every trial, every case must pass
CAPABILITY_THRESHOLD = 0.70


class GraderKind(StrEnum):
    CODE = "code"
    MODEL = "model"
    HUMAN = "human"


class Recorder:
    """Collects boundary-call observations for a single case's transcript.

    Graders call ``record`` around the real seam (a thin log, not a monkeypatch)
    so the transcript shows *what the seam returned* — e.g. ``fetch_excerpt`` URL
    -> reason, or ``serialize_report`` ticker -> raised/ok. Mutable by design;
    one Recorder per case, never shared.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def record(self, seam: str, **fields: Any) -> None:
        self.calls.append({"seam": seam, **fields})


@runtime_checkable
class Grader(Protocol):
    grader_id: str
    kind: GraderKind

    def __call__(self, recorder: Recorder) -> bool:
        """Run the evaluation once; return True on pass. May record boundary calls."""
        ...


@dataclass(frozen=True)
class CodeGrader:
    """Deterministic grader wrapping a pure/offline check ``fn(recorder) -> bool``."""

    grader_id: str
    fn: Callable[[Recorder], bool]
    kind: GraderKind = GraderKind.CODE

    def __call__(self, recorder: Recorder) -> bool:
        return bool(self.fn(recorder))


@dataclass(frozen=True)
class ModelGrader:
    """Model-based grader. ``fn(recorder, judge) -> bool`` calls the injected judge.

    The judge is supplied at construction. Offline suites inject a deterministic
    stub judge (so wiring is tested without an LLM); a real judge is opt-in and is
    never wired into evidence/scoring graders (those are ``CodeGrader``s with no
    judge slot at all — the "LLM never grades" invariant is structural).
    """

    grader_id: str
    fn: Callable[[Recorder, Callable[[str], bool]], bool]
    judge: Callable[[str], bool]
    kind: GraderKind = GraderKind.MODEL

    def __call__(self, recorder: Recorder) -> bool:
        return bool(self.fn(recorder, self.judge))


class HumanGrader:
    """Recorded-not-run tier (counsel sign-off, calibration spot-checks).

    ``__call__`` raises — the runner must never execute a human grader. Verdicts
    are appended out-of-band via ``record`` (one JSONL line), keeping the human
    tier fully offline and auditable.
    """

    kind: GraderKind = GraderKind.HUMAN

    def __init__(self, grader_id: str) -> None:
        self.grader_id = grader_id

    def __call__(self, recorder: Recorder) -> bool:  # noqa: ARG002 - Protocol signature
        raise RuntimeError(f"human grader {self.grader_id!r} is recorded, not run inline")


@dataclass(frozen=True)
class EvalCase:
    """One unit of evaluation: a grader plus run configuration + metadata.

    ``trials`` repeated invocations feed pass@k / pass^k. For deterministic
    (``CodeGrader``) cases trials>1 asserts *consistency* (pass^k) rather than
    smoothing flakiness — there is no randomness source offline.
    """

    case_id: str
    suite: str
    grader: Grader
    description: str = ""
    trials: int = 1
    target: str = TARGET_REGRESSION
    inputs: dict = field(default_factory=dict)

    @property
    def kind(self) -> GraderKind:
        return self.grader.kind


@dataclass(frozen=True)
class EvalResult:
    """Outcome of running one case ``trials`` times."""

    case_id: str
    suite: str
    kind: GraderKind
    target: str
    trials: list[bool]
    reason: str = ""

    @property
    def pass_at_k(self) -> bool:
        return _pass_at_k(self.trials)

    @property
    def pass_hat_k(self) -> bool:
        return _pass_hat_k(self.trials)

    @property
    def passed(self) -> bool:
        """Gate semantics: regression requires every trial (pass^k); capability
        requires at least one (pass@k)."""
        if self.target == TARGET_CAPABILITY:
            return self.pass_at_k
        return self.pass_hat_k


@dataclass(frozen=True)
class Transcript:
    """Inspectable record of one case run — the agent-trajectory analogue for
    these non-agent seams. ``tool_calls`` are the recorder's boundary observations."""

    case_id: str
    suite: str
    kind: GraderKind
    target: str
    inputs: dict
    tool_calls: list[dict]
    trials: list[bool]
    passed: bool
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "suite": self.suite,
            "kind": self.kind.value,
            "target": self.target,
            "inputs": self.inputs,
            "tool_calls": self.tool_calls,
            "trials": self.trials,
            "passed": self.passed,
            "reason": self.reason,
        }
