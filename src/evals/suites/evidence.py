"""Evidence-grading suite (PRD v4 §9.6/§11.5). Deterministic grade math:
- a known-fake citation on an off-allowlist host (incl. userinfo spoof) is
  UNVERIFIED -> never substantiates -> grade F, even with perfect text overlap;
- a 200-but-irrelevant page does not substantiate (overlap gate, not HTTP status);
- URL-flooding is bounded by the per-host cap; a keyword-salad excerpt is rejected
  (§11.5 stuffing signal) while the same terms in genuine prose still substantiate;
- zero substantiated -> F -> serenity_score withheld (None, not 0);
- a GENUINE allowlisted, substantiated source DOES count (balanced should-do control).
Pure functions + hardcoded fixtures; no DB, no network, no LLM.
"""

from __future__ import annotations

from src.evals.core import CodeGrader, EvalCase, Recorder
from src.evals.registry import suite
from src.serenity.evidence import classify_reference, is_substantiated
from src.serenity.grading import grade_evidence, serenity_score
from src.storage.models import EvidenceGrade, SourceType

_SUITE = "evidence"
_FULL_SCORECARD = {"supplier_concentration": 4, "validation_cycle": 4, "capacity_expansion": 4, "certification_strictness": 4, "purity_precision": 4}


def _irrelevant_corpus(n: int) -> list[tuple[str, str]]:
    """Deterministic corpus of (claim, irrelevant-but-long excerpt) pairs. Each
    excerpt is >= the min word count (so failure is OVERLAP, not length) and shares
    no tokens with the tech-bottleneck claim."""
    boilerplate = [
        "please enable javascript and accept all cookies to continue browsing this website today",
        "sign in to your account or create a new profile to access member resources",
        "rate limit exceeded please wait a few moments and then retry your earlier request",
        "the page you requested could not be found error four zero four return home",
        "subscribe to our newsletter for weekly updates delivered straight to your email inbox",
    ]
    pairs = []
    for i in range(n):
        claim = f"gallium nitride supplier concentration bottleneck validation cycle thesis {i}"
        pairs.append((claim, boilerplate[i % len(boilerplate)]))
    return pairs


def _known_fake_off_allowlist(rec: Recorder) -> bool:
    claim = "supplier concentration in gallium nitride epitaxy is a bottleneck"
    excerpt = "supplier concentration in gallium nitride epitaxy is a severe bottleneck for capacity expansion"
    fake = classify_reference(source_url="https://totally-fake-blog.example/post", claim_summary=claim, excerpt=excerpt)
    if fake["source_type"] != SourceType.UNVERIFIED or fake["substantiated"]:
        return False
    spoof = classify_reference(source_url="https://sec.gov@evil.example/x", claim_summary=claim, excerpt=excerpt)
    if spoof["source_type"] != SourceType.UNVERIFIED or spoof["substantiated"]:
        return False
    grade = grade_evidence([fake, spoof])
    rec.record("grade_evidence", grade=str(grade), refs="2 fake/off-allowlist")
    return grade == EvidenceGrade.F


def _irrelevant_200_not_substantiated(rec: Recorder) -> bool:
    pairs = _irrelevant_corpus(200)
    substantiated = sum(1 for claim, excerpt in pairs if is_substantiated(claim, excerpt))
    rec.record("is_substantiated", corpus=len(pairs), substantiated=substantiated)
    return substantiated == 0


def _flooding_and_stuffing_capped(rec: Recorder) -> bool:
    flooded = [{"source_host": "reuters.com", "source_type": SourceType.NEWS, "substantiated": True} for _ in range(5)]
    if grade_evidence(flooded) != EvidenceGrade.C:  # per-host cap=2 -> weight 2 -> C, never A
        return False
    distinct = [
        {"source_host": "sec.gov", "source_type": SourceType.FILING, "substantiated": True},
        {"source_host": "patents.google.com", "source_type": SourceType.PATENT, "substantiated": True},
    ]
    if grade_evidence(distinct) != EvidenceGrade.B:  # 3+2 = 5 -> B
        return False
    # §11.5 stuffing signal (now ENFORCED): a keyword salad — claim terms packed
    # with NO function words — is rejected outright, so fabricated density cannot
    # manufacture substantiation. The same terms in a genuine sentence (with
    # connective words) still count. Per-host cap + allowlist remain the bounds above.
    claim = "gallium nitride supplier bottleneck epitaxy"
    salad = "gallium nitride supplier concentration bottleneck epitaxy capacity expansion validation cycle certification"
    prose = "the filing discloses gallium nitride supplier concentration as a bottleneck for epitaxy capacity expansion"
    stuffed_rejected = is_substantiated(claim, salad) is False
    genuine_counts = is_substantiated(claim, prose) is True
    rec.record("grade_evidence", flooded="C", distinct="B", stuffed_rejected=stuffed_rejected, genuine_counts=genuine_counts)
    return stuffed_rejected and genuine_counts  # volume/density cannot manufacture a grade


def _zero_substantiated_withholds(rec: Recorder) -> bool:
    if grade_evidence([]) != EvidenceGrade.F:
        return False
    unsub = [{"source_host": "sec.gov", "source_type": SourceType.FILING, "substantiated": False}]
    if grade_evidence(unsub) != EvidenceGrade.F:
        return False
    score = serenity_score(_FULL_SCORECARD, EvidenceGrade.F)
    rec.record("serenity_score", grade="F", score=score)
    return score is None  # withheld (-> bootstrap), never 0


def _genuine_evidence_substantiates(rec: Recorder) -> bool:
    claim = "gallium nitride supplier concentration is a bottleneck"
    excerpt = "the filing discloses gallium nitride supplier concentration as a bottleneck risk to capacity"
    ref = classify_reference(source_url="https://www.sec.gov/Archives/edgar/x.htm", claim_summary=claim, excerpt=excerpt)
    rec.record("classify_reference", source_type=str(ref["source_type"]), substantiated=ref["substantiated"])
    return ref["source_type"] == SourceType.FILING and ref["substantiated"] is True


@suite(_SUITE)
def build() -> list[EvalCase]:
    return [
        EvalCase("known_fake_off_allowlist", _SUITE, CodeGrader("evidence.known_fake_off_allowlist", _known_fake_off_allowlist)),
        EvalCase("irrelevant_200_not_substantiated", _SUITE, CodeGrader("evidence.irrelevant_200_not_substantiated", _irrelevant_200_not_substantiated), inputs={"corpus": 200}),
        EvalCase("flooding_and_stuffing_capped", _SUITE, CodeGrader("evidence.flooding_and_stuffing_capped", _flooding_and_stuffing_capped)),
        EvalCase("zero_substantiated_withholds", _SUITE, CodeGrader("evidence.zero_substantiated_withholds", _zero_substantiated_withholds)),
        EvalCase("genuine_evidence_substantiates", _SUITE, CodeGrader("evidence.genuine_evidence_substantiates", _genuine_evidence_substantiates)),
    ]
