"""Pin the seed universe's platform coverage (the PR's headline fix).

The refresh pipeline only surfaces a candidate for a platform when
``classify_candidate`` returns that platform at or above
``classify_min_confidence`` (pipeline default 0.3). The PR added 18
platform-labeled rows to the REAL ``data/universes/ai_seed.csv`` so the non-AI
pools (robotics / energy_storage / blockchain / multiomic_sequencing) refresh
with real candidates instead of coming up empty — but nothing pinned it, so
reverting those rows shipped green.

This loads the real CSV via the project loader and drives the exact classify
step the pipeline drives (see ``pipeline.refresh_pool``), then asserts every
platform key clears a minimum candidate floor and that ``ai`` is exactly 25 (the
no-dilution guard mirroring ``test_pipeline.py``'s ``candidate_count == 25``).
"""

from collections import Counter

from src.observing_pools.classify import classify_candidate
from src.observing_pools.pipeline import DEFAULT_UNIVERSE, RefreshConfig
from src.observing_pools.platforms import PLATFORM_KEYS
from src.observing_pools.universe import load_seed_csv

# The pipeline's default classification threshold (drift-proof: read the field
# default rather than hardcode, so a pipeline change surfaces here too).
_MIN_CONFIDENCE: float = RefreshConfig.__dataclass_fields__["classify_min_confidence"].default

# PLATFORM_KEYS is the canonical backend taxonomy — the same five keys the
# frontend mirrors as FALLBACK_PLATFORMS in pools-panel.tsx.
_MIN_PER_PLATFORM = 3
_AI_EXPECTED = 25


def _classify_seed_counts() -> Counter:
    """Load the REAL seed CSV and count, per platform, candidates that classify
    at or above the pipeline's default min-confidence — mirroring refresh_pool."""
    rows, _rejected = load_seed_csv(DEFAULT_UNIVERSE)
    counts: Counter = Counter()
    for row in rows:
        results = classify_candidate(
            name=row.name,
            sector=row.sector,
            industry=row.industry,
            explicit_platforms=row.platforms,
        )
        for key, result in results.items():
            if result.confidence >= _MIN_CONFIDENCE:
                counts[key] += 1
    return counts


def test_default_universe_is_the_real_csv():
    # No fixture copy — the loader default and pipeline default must be the same file.
    assert DEFAULT_UNIVERSE == "data/universes/ai_seed.csv"


def test_every_platform_has_at_least_three_seed_candidates():
    """Each of the five platforms must classify >= 3 seed candidates.

    Reverting the PR's 18 platform-labeled rows drops robotics / energy_storage
    below 3 and blockchain / multiomic_sequencing to 0, turning this RED.
    """
    counts = _classify_seed_counts()
    for key in PLATFORM_KEYS:
        assert counts[key] >= _MIN_PER_PLATFORM, (
            f"platform {key!r} classifies only {counts[key]} seed candidate(s) at "
            f"min-confidence {_MIN_CONFIDENCE}; the seed CSV must supply >= {_MIN_PER_PLATFORM} "
            "so the pool can refresh (did the PR's platform-labeled rows get reverted?)"
        )


def test_ai_candidate_count_is_exactly_25_no_dilution():
    """AI must stay at exactly 25 — mirrors test_pipeline.py's candidate_count == 25.

    Guards against diluting the AI pool while adding non-AI coverage.
    """
    counts = _classify_seed_counts()
    assert counts["ai"] == _AI_EXPECTED, (
        f"ai classifies {counts['ai']} candidates, expected exactly {_AI_EXPECTED} "
        "(no-dilution guard shared with test_pipeline.py)"
    )
