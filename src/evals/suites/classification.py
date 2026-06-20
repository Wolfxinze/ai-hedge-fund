"""Classification-accuracy suite (PRD v4 §9.5). Deterministic-first classifier:
labels + confidence are a pure function of text + curated seed labels (no LLM
today). Balances should-classify with should-NOT-classify (the 'ai' in 'retail'
substring trap) and the loud-on-unknown-label contract.
"""

from __future__ import annotations

from src.evals.core import CodeGrader, EvalCase, Recorder
from src.evals.registry import suite
from src.observing_pools.classify import classify_candidate, SEED_LABEL_CONFIDENCE

_SUITE = "classification"


def _seed_label_accuracy(rec: Recorder) -> bool:
    """A curated seed label classifies at the seed confidence with the seed rationale."""
    result = classify_candidate(name="Acme Corp", sector="Tech", industry="Software", explicit_platforms=["ai"])
    rec.record("classify_candidate", explicit=["ai"], keys=sorted(result))
    res = result.get("ai")
    return res is not None and res.confidence == SEED_LABEL_CONFIDENCE and "curated seed" in res.rationale


def _substring_false_positives_blocked(rec: Recorder) -> bool:
    """Single-token seeds must require whole-word equality: they must NOT fire as a
    bare substring of an unrelated word. Phrase/hyphen seeds still match."""
    # should-NOT: 'ai' inside 'retail', 'ev' inside 'revenue', 'cell' inside 'excellent'.
    traps = [
        ("Big Box Retail", "Consumer", "Internet Retail", "ai"),
        ("Revenue Systems", "Finance", "Revenue Software", "ev"),
        ("Excellent Foods", "Consumer", "Excellent Packaged Goods", "cell"),
    ]
    for name, sector, industry, forbidden_key in traps:
        result = classify_candidate(name=name, sector=sector, industry=industry)
        rec.record("classify_candidate", text=f"{name}|{industry}", got=sorted(result), forbidden=forbidden_key)
        if forbidden_key in result:
            return False  # bare substring false-positive leaked
    return True


def _unknown_label_raises_loud(rec: Recorder) -> bool:
    """An explicit label outside the taxonomy must raise (ingestion error, not silent drop)."""
    try:
        classify_candidate(name="X", sector=None, industry=None, explicit_platforms=["not_a_real_platform"])
    except ValueError as exc:
        rec.record("classify_candidate", explicit=["not_a_real_platform"], raised=str(exc))
        return "not_a_real_platform" in str(exc)
    return False


@suite(_SUITE)
def build() -> list[EvalCase]:
    return [
        EvalCase("seed_label_accuracy", _SUITE, CodeGrader("classification.seed_label_accuracy", _seed_label_accuracy)),
        EvalCase("substring_false_positives_blocked", _SUITE, CodeGrader("classification.substring_false_positives_blocked", _substring_false_positives_blocked)),
        EvalCase("unknown_label_raises_loud", _SUITE, CodeGrader("classification.unknown_label_raises_loud", _unknown_label_raises_loud)),
    ]
