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
    _figures,
    _is_keyword_salad,
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


# ── §11.5 polish (#43): surface-form folding + contract pinning ──────────────────

def test_fullwidth_digits_normalize():
    # A fabricated excerpt that writes the figure in full-width digits (４０％) must
    # not evade the ASCII numeric gate: NFKC folds it to "40%", so a genuine match
    # still substantiates while a wrong full-width number still rejects.
    claim = "gallium nitride wafer supply fell 40% amid a capacity bottleneck"
    fw_right = "the filing notes gallium nitride wafer supply fell ４０％ on a capacity bottleneck"
    fw_wrong = "the filing notes gallium nitride wafer supply fell ２５％ on a capacity bottleneck"
    assert is_substantiated(claim, fw_right) is True
    assert is_substantiated(claim, fw_wrong) is False
    assert substantiation_reason(claim, fw_wrong, SourceType.FILING) == "figure_missing"


def test_superscript_digits_do_not_mint_a_figure():
    # NFKC must NOT decompose a superscript digit onto an adjacent number and mint a
    # figure never printed — neither by APPENDING ("10²%" must not read as "102%",
    # "²5%" must not read as "25%") nor by JOINING the digits a superscript SEPARATES
    # ("4²0%" must not read as "40%"). A human reads ² as a footnote mark, not a digit.
    assert _figures("growth of 10²%") == {"10%"}      # the ² is dropped, not glued to 10
    assert _figures("a ²5% move") == {"5%"}           # not 25%
    assert "40%" not in _figures("a 4²0% jump")       # a separator ² must not glue 4 + 0 -> 40
    claim = "gallium nitride capacity rose 102% over the prior bottleneck cycle this year"
    excerpt = "the filing notes gallium nitride capacity rose 10²% over the prior bottleneck cycle this year"
    assert is_substantiated(claim, excerpt) is False
    assert substantiation_reason(claim, excerpt, SourceType.FILING) == "figure_missing"


def test_times_is_not_promoted_to_a_multiplier_figure():
    # "N times" is ambiguous — in filings it almost always means OCCASIONS, not an
    # "Nx" multiplier. Promoting it to a required figure would suppress genuine
    # backing phrased differently ("on three occasions"). Per §11.5 (an ambiguous
    # number is not gated), "times" is not a unit, so the claim still substantiates.
    claim = "the company restated gallium nitride capacity guidance 3 times this year amid the bottleneck"
    excerpt = "the filing shows the company restated gallium nitride capacity guidance on three occasions amid the bottleneck"
    assert is_substantiated(claim, excerpt) is True
    assert _figures("met with suppliers 5 times") == set()  # occasions, not 5x


def test_wrong_and_missing_both_map_to_figure_missing():
    # G2: the gate intentionally COARSE-merges "states a different number" and
    # "omits the number" into one auditable reason — pin that they are not split.
    claim = "gallium nitride wafer supply fell 40% amid a capacity bottleneck"
    wrong = "the filing notes gallium nitride wafer supply fell 25% on a capacity bottleneck this year"
    missing = "the filing notes gallium nitride wafer supply fell sharply on a capacity bottleneck this year"
    r_wrong = substantiation_reason(claim, wrong, SourceType.FILING)
    r_missing = substantiation_reason(claim, missing, SourceType.FILING)
    assert r_wrong == r_missing == "figure_missing"


def test_identifier_digits_do_not_satisfy_a_figure():
    # G3: a digit run glued to a letter (h100m) is an identifier, not a figure — it
    # must NOT satisfy a claim stating the same UNIT-BEARING figure (100m). Pins the
    # lookbehind: without it, "h100m" would mint the figure 100m and falsely match.
    claim = "the gallium nitride line added 100m of capacity amid the bottleneck this year"
    excerpt = "the h100m gallium nitride line added capacity amid the bottleneck this year"
    assert is_substantiated(claim, excerpt) is False
    assert substantiation_reason(claim, excerpt, SourceType.FILING) == "figure_missing"


def test_keyword_salad_word_count_boundary():
    # G4: the stuffing gate fires only at >= _MIN_EXCERPT_WORDS words. Seven
    # function-word-free words are below the floor (not salad); eight are at it.
    seven = "alpha beta gamma delta epsilon zeta eta"
    eight = "alpha beta gamma delta epsilon zeta eta theta"
    assert _is_keyword_salad(seven) is False
    assert _is_keyword_salad(eight) is True


def test_keyword_salad_gate_sees_through_fullwidth():
    # The salad gate runs on NFKC-folded text (_norm), so a full-width-spelled bag of
    # eight content words is still detected as stuffing — it cannot evade the ASCII
    # word matcher by using look-alike codepoints.
    fullwidth_eight = "ａｌｐｈａ ｂｅｔａ ｇａｍｍａ ｄｅｌｔａ ｅｐｓｉｌｏｎ ｚｅｔａ ｅｔａ ｔｈｅｔａ"
    assert _is_keyword_salad(fullwidth_eight) is True


def test_micrometre_unit_normalizes_both_codepoints():
    # The micrometre figure must match whether written with the micro sign (µ, U+00B5)
    # or a greek mu (μ, U+03BC); NFKC folds the former to the latter.
    assert _figures("a 3µm node") == _figures("a 3μm node") == {"3μm"}


# ── §11.5 (#43): numeric surface-forms are wontfix-by-design, decisions pinned ────
# All three #43 surface-form items resolve to "keep the gate as-is" — each candidate
# fix would weaken a deterministic guard. These tests pin those decisions so a future
# refactor cannot silently regress them.

def test_bare_numeric_data_row_is_treated_as_salad():
    # #43 (salad table-header) — WONTFIX. A function-word-free bare numeric data row is
    # deliberately still a keyword salad. Exempting it (by a digit-stripped count OR a
    # digit-majority test) necessarily re-opens a stuffing evasion: the gate cannot
    # distinguish real numbers backing a claim from arbitrary numbers padding a keyword
    # bag. Over-strictness here only WITHHOLDS a grade (a bare row is rare — real backing
    # carries prose); it never mints a false substantiation. Pinned vs reintroduction.
    claim = "Revenue and operating income both rose this quarter"
    row = "Revenue 1234 1198 1056 operating 412 389 350 income 298 276"
    assert _is_keyword_salad(row) is True
    assert is_substantiated(claim, row) is False
    assert substantiation_reason(claim, row, SourceType.FILING) == "keyword_stuffing"


def test_keyword_stuffing_not_evaded_by_padding_digits():
    # The evasion any numeric exemption would open: claim keywords padded with arbitrary
    # (here digit-MAJORITY) numbers. The strict gate counts digits like any token, so it
    # stays a salad and is NOT substantiated. Both a digit-stripped count AND a
    # digit-dominance exemption would flip this to a false "ok" — this reds if either
    # regression lands.
    claim = "gallium nitride supplier bottleneck epitaxy"
    padded = "gallium nitride supplier bottleneck 100 200 300 400 500"
    assert _is_keyword_salad(padded) is True
    assert is_substantiated(claim, padded) is False
    assert substantiation_reason(claim, padded, SourceType.FILING) == "keyword_stuffing"


def test_dollar_figure_not_satisfied_by_bare_number():
    # #43 surface-form decision (wontfix-by-design): "$" is part of the figure key, so
    # a "$1,200" claim is NOT backed by a bare "1200" in the excerpt — an unmarked
    # integer is ambiguous (a count / year / id) and §11.5 deliberately does not gate
    # on it. Pinned so the asymmetry cannot silently regress into a false match.
    claim = "The acquisition closed at a price of $1,200 per share agreement"
    excerpt = "The acquisition agreement disclosed a closing price of 1200 for the deal terms"
    assert is_substantiated(claim, excerpt) is False
    assert substantiation_reason(claim, excerpt, SourceType.FILING) == "figure_missing"
