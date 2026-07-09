"""Pure volatility + B1 banded-subtractive risk haircut math (parent PRD
`.prd/PRD-20260709-risk-haircut-rh1.md` — "The math (exact spec)").

Stdlib only (``statistics``, ``math``) — no pandas, no numpy, no I/O, no agent
imports. This mirrors (does NOT import) the volatility math at
``src/agents/risk_manager.py:222`` (``calculate_volatility_metrics``) so the
scoring path can use it without crossing the I1 trade-path boundary. One
deliberate divergence from ``risk_manager``: short/insufficient history returns
``None`` here (degraded, handled by the caller) instead of a fabricated 5%-daily
fallback — never invent a worst-case volatility.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence

_TRADING_DAYS_PER_YEAR = 252

# Band edges (annualized vol, decimal) and their haircut points (PRD B1 spec).
_BAND_LOW = 0.15  # below this: no haircut
_BAND_MID = 0.30  # 0.15-0.30: linear 0 -> 10 pts
_BAND_HIGH = 0.50  # 0.30-0.50: linear 10 -> 20 pts; >= this: capped at 20

POLICY = "b1-banded-subtractive-v1"


def annualized_volatility_from_closes(closes: Sequence[float], lookback_days: int = 60) -> float | None:
    """Sample-std (ddof=1) of daily close-to-close pct-change returns, annualized.

    Uses the last ``min(lookback_days, available)`` returns. Fewer than 2 returns
    (i.e. fewer than 3 closes) -> ``None`` (degraded; caller decides policy). A
    zero/negative close raises ``ValueError`` (boundary validation; never divide
    by zero silently).
    """
    if any(c <= 0 for c in closes):
        raise ValueError(f"closes must be strictly positive, got {closes!r}")
    if len(closes) < 3:
        return None
    returns = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
    recent = returns[-min(lookback_days, len(returns)) :]
    if len(recent) < 2:
        return None
    return statistics.stdev(recent) * math.sqrt(_TRADING_DAYS_PER_YEAR)


def haircut_points(annualized_vol: float) -> float:
    """B1 bands: 0 below 0.15; linear 0->10 over 0.15-0.30; linear 10->20 over
    0.30-0.50; capped at 20 for >= 0.50. Continuous at every band edge."""
    if annualized_vol < 0:
        raise ValueError(f"annualized_vol must be >= 0, got {annualized_vol!r}")
    if annualized_vol < _BAND_LOW:
        return 0.0
    if annualized_vol < _BAND_MID:
        return (annualized_vol - _BAND_LOW) / (_BAND_MID - _BAND_LOW) * 10.0
    if annualized_vol < _BAND_HIGH:
        return 10.0 + (annualized_vol - _BAND_MID) / (_BAND_HIGH - _BAND_MID) * 10.0
    return 20.0


def apply_risk_haircut(momentum: float | None, annualized_vol: float | None) -> tuple[float | None, dict]:
    """B1 banded subtractive haircut: ``clamp(momentum - h(sigma), 0, 100)``.

    ``momentum`` None -> ``(None, audit)`` untouched (nothing to haircut).
    ``annualized_vol`` None -> ``(momentum, audit)`` with zero haircut and
    ``degraded: True`` (missing/short price data; never fabricate a worst-case
    sigma). The audit dict always carries ``raw_momentum``, ``haircut_points``,
    ``annualized_volatility``, ``degraded``, and ``policy``.
    """
    if momentum is None:
        audit = {
            "raw_momentum": None,
            "haircut_points": 0.0,
            "annualized_volatility": annualized_vol,
            "degraded": False,
            "policy": POLICY,
        }
        return None, audit

    if annualized_vol is None:
        audit = {
            "raw_momentum": momentum,
            "haircut_points": 0.0,
            "annualized_volatility": None,
            "degraded": True,
            "policy": POLICY,
        }
        return momentum, audit

    h = haircut_points(annualized_vol)
    adjusted = max(0.0, min(100.0, momentum - h))
    audit = {
        "raw_momentum": momentum,
        "haircut_points": h,
        "annualized_volatility": annualized_vol,
        "degraded": False,
        "policy": POLICY,
    }
    return adjusted, audit
