"""pass@k / pass^k metrics (PRD v4 §11, Anthropic "Demystifying Evals").

pass@k  = probability of >=1 success in k trials  (capability signal).
pass^k  = probability ALL k trials succeed         (consistency / regression signal).

The underlying Phase-11 graders are deterministic and offline, so "k trials" is
repeated in-process invocation with no flakiness source — pass^k is therefore a
strict regression assertion, not a flaky-test smoother.
"""

from __future__ import annotations

from collections.abc import Sequence


def pass_at_k(trials: Sequence[bool], k: int | None = None) -> bool:
    """True if any of the first ``k`` (default all) trials passed."""
    window = trials if k is None else trials[:k]
    return any(window)


def pass_hat_k(trials: Sequence[bool], k: int | None = None) -> bool:
    """True only if every one of the first ``k`` (default all) trials passed."""
    window = trials if k is None else trials[:k]
    return bool(window) and all(window)


def pass_rate(flags: Sequence[bool]) -> float:
    """Fraction of True over a sequence (suite-level pass@k / pass^k rate)."""
    if not flags:
        return 0.0
    return sum(1 for f in flags if f) / len(flags)
