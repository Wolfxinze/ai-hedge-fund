"""Unit tests for deterministic platform classification (classify.py).

Focus: token-aware keyword matching (no bare-substring false positives),
keyword confidence math, curated-label max-merge, and the loud unknown-label
guard.
"""

import pytest

from src.observing_pools.classify import (
    SEED_LABEL_CONFIDENCE,
    _KW_BASE,
    _KW_CAP,
    _KW_PER_HIT,
    classify_candidate,
)


def test_substring_false_positive_is_fixed():
    """'ai' must not match inside 'retail' (token-aware matching)."""
    results = classify_candidate(
        name="Internet Retail",
        sector="Technology",
        industry="Retail",
    )
    assert "ai" not in results


def test_genuine_keyword_only_hit():
    """A real whole-word 'ai' token yields the keyword confidence, no curated label."""
    results = classify_candidate(
        name="Quantum AI Compute",
        sector=None,
        industry=None,
    )
    assert "ai" in results
    hits = 1  # only the 'ai' token hits the ai-platform keyword set
    expected = min(_KW_CAP, _KW_BASE + _KW_PER_HIT * hits)
    assert results["ai"].confidence == expected
    assert results["ai"].rationale == "keyword match: ai"


def test_curated_label_wins_over_weak_keyword():
    """Curated seed label (0.9) beats a single weak keyword hit via max-merge."""
    results = classify_candidate(
        name="Quantum AI Compute",
        sector=None,
        industry=None,
        explicit_platforms=["ai"],
    )
    assert results["ai"].confidence == SEED_LABEL_CONFIDENCE
    assert results["ai"].rationale == "curated seed label"


def test_no_hits_no_labels_returns_empty():
    results = classify_candidate(
        name="Generic Holdings",
        sector="Financials",
        industry="Diversified",
    )
    assert results == {}


def test_unknown_explicit_label_raises():
    with pytest.raises(ValueError) as exc:
        classify_candidate(
            name="Whatever",
            sector=None,
            industry=None,
            explicit_platforms=["not_a_platform"],
        )
    assert "not_a_platform" in str(exc.value)


def test_hyphenated_seed_matches_as_phrase():
    """A hyphenated seed ('self-driving') matches as a substring phrase; the only
    robotics signal here is the hyphenated seed, so whole-word-only would miss it."""
    results = classify_candidate(
        name="Acme Self-Driving Cars",
        sector="Consumer Cyclical",
        industry="Auto Manufacturers",
    )
    assert "robotics" in results
    assert "self-driving" in results["robotics"].rationale
