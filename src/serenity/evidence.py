"""Evidence host-allowlisting + substantiation (PRD v4 §9.6, §11.5, R2).

``source_type`` is derived from an allowlisted host — never accepted from the LLM
or user. Substantiation is a deterministic three-gate check (PRD §11.5): the
excerpt must (1) overlap the claim's terms — a fetched page that does not mention
them (e.g. a rate-limit/login page) does not count even with HTTP 200; (2) contain
any figure the claim states (numeric-aware); and (3) not be a keyword salad
(claim terms with no function words → fabricated density). Phase 0 operates on
user-provided excerpts; live fetching + SSRF hardening is an expansion phase
(PRD Phase 7/8).
"""

import re
import unicodedata
from urllib.parse import urlparse

from src.storage.models import SourceType

# Base domains → source_type. Subdomains match via endswith("." + base).
DEFAULT_HOST_ALLOWLIST: dict[str, SourceType] = {
    "sec.gov": SourceType.FILING,
    "uspto.gov": SourceType.PATENT,
    "patents.google.com": SourceType.PATENT,
    "federalregister.gov": SourceType.REGULATORY,
    "europa.eu": SourceType.REGULATORY,
    "reuters.com": SourceType.NEWS,
    "bloomberg.com": SourceType.NEWS,
    "wsj.com": SourceType.NEWS,
}

_WORD_RE = re.compile(r"[a-z0-9]+")
_MIN_OVERLAP = 0.20
_MIN_EXCERPT_WORDS = 8


def host_of(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if "@" in (parsed.netloc or ""):  # any userinfo — exact parity with the fetcher's _gate '@'-reject
        return None
    return parsed.netloc.lower().split(":")[0] or None


def source_type_for_host(host: str | None, allowlist: dict[str, SourceType] = DEFAULT_HOST_ALLOWLIST) -> SourceType:
    """Map a host to a source_type via the allowlist; unknown hosts → UNVERIFIED."""
    if not host:
        return SourceType.UNVERIFIED
    for base, stype in allowlist.items():
        if host == base or host.endswith("." + base):
            return stype
    return SourceType.UNVERIFIED


def _norm(text: str | None) -> str:
    """Lowercased + Unicode-NFKC-folded text. NFKC collapses full-width and other
    compatibility forms (４０％ -> "40%", µ -> μ) so a fabricated excerpt cannot evade
    the ASCII word/figure gates by spelling figures in look-alike codepoints.

    Category-``No`` characters (superscripts ², subscripts, fractions ½, circled ①)
    are replaced with a SPACE BEFORE NFKC: their compatibility decompositions are
    bare ASCII digits that would otherwise glue onto an adjacent number and *mint* a
    figure never printed — both by appending (``10²%`` -> ``102%``) and, if deleted,
    by joining the digits a superscript separated (``4²0%`` -> ``40%``). A space is a
    token/figure boundary, so neither glue occurs. Full-width digits are category
    ``Nd`` (not ``No``), so the intended fold (``４０％`` -> ``40%``) is preserved."""
    cleaned = "".join(" " if unicodedata.category(c) == "No" else c for c in (text or ""))
    return unicodedata.normalize("NFKC", cleaned).lower()


def _tokens(text: str | None) -> set[str]:
    return {w for w in _WORD_RE.findall(_norm(text)) if len(w) >= 3}


# §11.5 numeric-aware gate. A "figure" is a *quantity*: a number carrying a unit,
# scale word, %, or $ ($2.4B, 3nm, 40%, 2.4 billion). A bare integer (a year,
# count, version, or form number) is NOT a figure — it is incidental to prose, not
# a quantity the source must echo, and gating on it would falsely reject genuine
# backing text. The negative lookbehind keeps model identifiers out (the "100" in
# "h100" is glued to a letter); the trailing boundary keeps a unit from swallowing
# an adjacent word's prefix ("50bps" must not read as "50 billion"). Matched on
# NFKC-folded text (see _norm), so ４０％ ≡ 40% and the micrometre unit is the folded
# μm — the micro sign µ (U+00B5) has already become greek mu μ (U+03BC) by the time
# this matches. The word "times" is deliberately NOT a unit: "N times" almost always
# means occasions, not an "Nx" multiplier, so promoting it would mint a phantom
# figure requirement and suppress genuinely-backed claims (same ambiguity class as
# "$1,200" vs a bare "1200" — an ambiguous number is not gated, per §11.5).
# Known limitation (#43): the micrometre fallback unit "um" doubles as the English
# filler/interjection "um". A unit only binds when it directly follows a number (the
# unit group is only ever the optional tail after `(number)\s?` in _FIGURE_RE, with no
# independent anchor of its own), so the collision is NARROW — only a "<number> um"
# sequence misfires, e.g. transcribed prose "...got 5 um maybe 6..." reads "5um" as a
# spurious micrometre figure. This is an ACCEPTED trade-off: per #43 the gate stays
# strict — "um" is retained so legitimate technical claims ("40 um feature size") are
# still caught — rather than dropping the token to spare the rare informal-speech
# false positive.
_FIGURE_RE = re.compile(
    r"(?<![a-z0-9])(\$?)(\d[\d,]*(?:\.\d+)?)\s?"
    r"(%|percent|pct|nm|μm|um|ghz|mhz|kw|mw|gw|billion|trillion|million|thousand|bn|b|m|t|k|x)?"
    r"(?![a-z0-9])"
)
_SCALE = {"billion": "b", "bn": "b", "trillion": "t", "million": "m", "thousand": "k", "percent": "%", "pct": "%"}

# §11.5 stuffing gate. Real backing prose carries connective/function words; a bag
# of keywords does not. Intentionally small + common — the goal is to spot a salad,
# not to POS-tag.
_FUNCTION_WORDS = frozenset({
    "a", "an", "and", "or", "but", "the", "of", "to", "in", "on", "for", "with",
    "as", "by", "at", "from", "into", "over", "under", "about", "per", "via",
    "is", "are", "was", "were", "be", "been", "being", "has", "have", "had",
    "will", "would", "can", "could", "may", "might", "do", "does", "did",
    "this", "that", "these", "those", "it", "its", "their", "his", "her", "our",
    "your", "they", "we", "you", "he", "she", "not", "no", "than", "then", "so",
    "if", "when", "while", "which", "who", "whom", "whose", "there", "here",
    "also", "up", "down", "out", "off", "between", "amid", "across", "within",
})


def _figures(text: str | None) -> set[str]:
    """Normalized numeric quantities found in `text` (e.g. {"40%", "2.4b", "3nm"}).
    A bare integer — a year, count, version, or form number — is dropped: it is
    incidental to prose, not a quantity a backing source must echo."""
    out: set[str] = set()
    for dollar, num, unit in _FIGURE_RE.findall(_norm(text)):
        if unit:
            suffix = _SCALE.get(unit, unit)  # normalize scale words / %, else keep the unit
        elif dollar:
            suffix = "$"
        else:
            continue  # bare number — a year/count/version, not a claimed quantity
        out.add(num.replace(",", "") + suffix)
    return out


def _claim_figures_missing(claim: str | None, excerpt: str | None) -> bool:
    """True iff the claim states figures and at least one is absent from the excerpt."""
    claim_figs = _figures(claim)
    return bool(claim_figs) and not claim_figs <= _figures(excerpt)


def _is_keyword_salad(excerpt: str | None) -> bool:
    """True iff a long-enough excerpt has zero function words (keyword stuffing)."""
    words = _WORD_RE.findall(_norm(excerpt))
    if len(words) < _MIN_EXCERPT_WORDS:
        return False
    return not any(w in _FUNCTION_WORDS for w in words)


def is_substantiated(claim: str | None, excerpt: str | None, min_overlap: float = _MIN_OVERLAP) -> bool:
    """True iff the excerpt materially backs the claim (PRD §11.5).

    Three deterministic gates, in order:
    1. **relevance** — the excerpt must overlap the claim's terms (guards
       URL-flooding with on-host-but-irrelevant pages: fails regardless of HTTP
       status);
    2. **numeric-aware** — any figure the claim states (40%, $2.4B, 3nm) must
       appear in the excerpt; a source that omits/contradicts the number does
       not back it;
    3. **anti-stuffing** — a relevant excerpt that is a keyword salad (claim
       terms with no function words) is fabricated density, not backing text.
    """
    claim_tokens = _tokens(claim)
    excerpt_tokens = _tokens(excerpt)
    if not claim_tokens or len(excerpt_tokens) < _MIN_EXCERPT_WORDS:
        return False
    overlap = len(claim_tokens & excerpt_tokens) / len(claim_tokens)
    if overlap < min_overlap:
        return False
    if _claim_figures_missing(claim, excerpt):
        return False
    return not _is_keyword_salad(excerpt)


def substantiation_reason(claim: str | None, excerpt: str | None, stype: SourceType, min_overlap: float = _MIN_OVERLAP) -> str:
    """Coarse, deterministic reason a reference did/didn't substantiate — for
    observability, so a withheld grade is auditable (unverified host vs missing/
    short excerpt vs no overlap vs claimed-figure absent vs keyword-stuffing). Does
    NOT change the grade math."""
    if stype is SourceType.UNVERIFIED:
        return "unverified_host"
    excerpt_tokens = _tokens(excerpt)
    if not excerpt_tokens:
        return "no_excerpt"
    if len(excerpt_tokens) < _MIN_EXCERPT_WORDS:
        return "excerpt_too_short"
    claim_tokens = _tokens(claim)
    if not claim_tokens:
        return "no_claim"
    overlap = len(claim_tokens & excerpt_tokens) / len(claim_tokens)
    if overlap < min_overlap:
        return "no_overlap"
    if _claim_figures_missing(claim, excerpt):
        return "figure_missing"
    if _is_keyword_salad(excerpt):
        return "keyword_stuffing"
    return "ok"


def classify_reference(
    *,
    source_url: str,
    claim_summary: str | None,
    excerpt: str | None,
    allowlist: dict[str, SourceType] = DEFAULT_HOST_ALLOWLIST,
) -> dict:
    """Derive {source_host, source_type, substantiated, reason} deterministically.

    ``reason`` is observability only (not persisted) — it explains a withheld grade
    without changing the deterministic grade computation.
    """
    host = host_of(source_url)
    stype = source_type_for_host(host, allowlist)
    substantiated = stype is not SourceType.UNVERIFIED and is_substantiated(claim_summary, excerpt)
    reason = substantiation_reason(claim_summary, excerpt, stype, min_overlap=_MIN_OVERLAP)
    return {"source_host": host, "source_type": stype, "substantiated": substantiated, "reason": reason}
