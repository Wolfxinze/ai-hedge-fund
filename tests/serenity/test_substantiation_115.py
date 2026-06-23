"""PRD §11.5 substantiation hardening: numeric-aware + keyword-stuffing signal.

`is_substantiated` was token-overlap-only. §11.5 requires it to be:
- **numeric-aware** — if the claim states a quantity (40%, $2.4B, 3nm), that
  figure must appear in the backing excerpt (a source that omits / contradicts
  the number does not substantiate the number);
- **stuffing-aware** — an excerpt that packs the claim's keywords with NO
  function words (a keyword salad) is fabricated density, not backing text.

Both gates sit BEHIND the existing relevance (overlap) gate: an irrelevant
excerpt is still "no_overlap", never "keyword_stuffing". Model identifiers like
``H100`` are NOT figures (a digit run glued to a letter is an id, not a quantity).
"""

from src.serenity.evidence import (
    classify_reference,
    is_substantiated,
    substantiation_reason,
)
from src.storage.models import SourceType

_SEC = "https://www.sec.gov/Archives/edgar/x.htm"


# ── numeric-aware: claim figures must appear ────────────────────────────────────

def test_claim_figure_present_substantiates():
    claim = "gallium nitride wafer supply fell 40% amid a capacity bottleneck"
    excerpt = "the filing notes gallium nitride wafer supply fell 40% on a capacity bottleneck this year"
    assert is_substantiated(claim, excerpt) is True


def test_wrong_figure_does_not_substantiate():
    claim = "gallium nitride wafer supply fell 40% amid a capacity bottleneck"
    # On-topic (high overlap) but states a DIFFERENT number — must not substantiate.
    excerpt = "the filing notes gallium nitride wafer supply fell 25% on a capacity bottleneck this year"
    assert is_substantiated(claim, excerpt) is False
    assert substantiation_reason(claim, excerpt, SourceType.FILING) == "figure_missing"


def test_missing_figure_does_not_substantiate():
    claim = "gallium nitride wafer supply fell 40% amid a capacity bottleneck"
    # On-topic but omits the claimed figure entirely.
    excerpt = "the filing notes gallium nitride wafer supply fell sharply on a capacity bottleneck this year"
    assert is_substantiated(claim, excerpt) is False
    assert substantiation_reason(claim, excerpt, SourceType.FILING) == "figure_missing"


def test_all_multi_figures_required():
    claim = "the company guided capex of $2.4B for the 3nm node ramp next year"
    both = "the company guided capex of $2.4B for the 3nm node ramp it disclosed today"
    only_one = "the company guided capex of $2.4B for the node ramp it disclosed today"
    assert is_substantiated(claim, both) is True
    assert is_substantiated(claim, only_one) is False  # 3nm absent


def test_currency_scale_normalizes():
    claim = "the company guided revenue of $2.4B for the gallium nitride segment this year"
    # "$2.4B" vs "2.4 billion" must normalize to the same figure.
    excerpt = "the company guided revenue of 2.4 billion dollars for the gallium nitride segment this year"
    assert is_substantiated(claim, excerpt) is True


def test_model_identifier_is_not_a_figure():
    # H100 / CoWoS are identifiers, not quantities — numeric rule must be a no-op,
    # so this genuine, relevant excerpt still substantiates.
    claim = "NVIDIA H100 GPU supply is constrained by TSMC CoWoS packaging capacity"
    excerpt = "TSMC CoWoS advanced packaging capacity limits NVIDIA H100 GPU supply this quarter"
    assert is_substantiated(claim, excerpt) is True


# ── stuffing: keyword salad (zero function words) is rejected ────────────────────

_SALAD_CLAIM = "gallium nitride supplier bottleneck epitaxy"
_SALAD = "gallium nitride supplier concentration bottleneck epitaxy capacity expansion validation cycle certification"
_PROSE = "the filing discloses gallium nitride supplier concentration as a bottleneck for epitaxy capacity expansion"


def test_keyword_salad_does_not_substantiate():
    # Perfect overlap, but a bag of nouns with no function words = fabricated density.
    assert is_substantiated(_SALAD_CLAIM, _SALAD) is False
    assert substantiation_reason(_SALAD_CLAIM, _SALAD, SourceType.FILING) == "keyword_stuffing"


def test_genuine_dense_prose_still_substantiates():
    # Same terms, but with connective/function words → real backing text.
    assert is_substantiated(_SALAD_CLAIM, _PROSE) is True
    assert substantiation_reason(_SALAD_CLAIM, _PROSE, SourceType.FILING) == "ok"


def test_irrelevant_wordlist_is_no_overlap_not_stuffing():
    # A no-function-word list that DOESN'T overlap the claim must read as
    # "no_overlap" (relevance gate fires first), never "keyword_stuffing".
    unrelated = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    assert is_substantiated("supplier concentration bottleneck", unrelated) is False
    assert substantiation_reason("supplier concentration bottleneck", unrelated, SourceType.FILING) == "no_overlap"


# ── numeric gate is a QUANTITY gate, not a bare-digit gate (Stage-4 HIGH fixes) ──

def test_percent_word_and_symbol_normalize():
    # A claim written "40%" must be backed by an excerpt that spells "40 percent"
    # (and vice-versa); surface form must not gate a genuinely-backed figure.
    claim = "gallium nitride wafer supply fell 40% amid a capacity bottleneck"
    excerpt = "the filing notes gallium nitride wafer supply fell 40 percent on a capacity bottleneck"
    assert is_substantiated(claim, excerpt) is True


def test_bare_year_is_not_a_required_figure():
    # A bare integer (year / version / count / form number) is incidental to prose,
    # not a quantity the source must echo — its absence must NOT reject genuine text.
    claim = "in 2024 gallium nitride wafer supply tightened into a capacity bottleneck"
    excerpt = "the filing discloses gallium nitride wafer supply tightened into a capacity bottleneck"
    assert is_substantiated(claim, excerpt) is True
    assert substantiation_reason(claim, excerpt, SourceType.FILING) == "ok"


def test_bare_number_not_corrupted_by_excerpt_scale_word():
    # The claim states a bare "25" (no unit); the excerpt mentions "25 million"
    # elsewhere. The number gate must not corrupt the excerpt's 25 -> 25m and then
    # report the claim's 25 "missing": a bare number is not a required figure.
    claim = "gallium nitride wafer prices rose 25 over the quarter amid a supply bottleneck"
    excerpt = "gallium nitride wafer demand drove 25 million dollars of new orders amid a supply bottleneck"
    assert is_substantiated(claim, excerpt) is True


def test_currency_scale_mismatch_rejected():
    # Discriminating negative: SAME number, DIFFERENT scale must not substantiate —
    # proves _SCALE actually gates (not merely that overlap carries the claim).
    claim = "the company guided revenue of $2.4B for the gallium nitride segment this year"
    excerpt = "the company guided revenue of 2.4 trillion dollars for the gallium nitride segment this year"
    assert is_substantiated(claim, excerpt) is False
    assert substantiation_reason(claim, excerpt, SourceType.FILING) == "figure_missing"


def test_classify_reference_threads_new_gates():
    # End-to-end through the production chokepoint: allowlisted host, but a salad
    # excerpt is not substantiated and carries the auditable reason.
    out = classify_reference(source_url=_SEC, claim_summary=_SALAD_CLAIM, excerpt=_SALAD)
    assert out["source_type"] == SourceType.FILING
    assert out["substantiated"] is False
    assert out["reason"] == "keyword_stuffing"
