"""Evidence host-allowlisting + substantiation (PRD v4 §9.6, §11.5, R2).

``source_type`` is derived from an allowlisted host — never accepted from the LLM
or user. Substantiation is a deterministic claim↔text overlap check: a fetched
page that does not mention the claim's terms (e.g. a rate-limit/login page) does
not count, even with HTTP 200. Phase 0 operates on user-provided excerpts; live
fetching + SSRF hardening is an expansion phase (PRD Phase 7/8).
"""

import re
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
    if parsed.username or parsed.password:  # userinfo — the fetcher rejects '@'; stay consistent
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


def _tokens(text: str | None) -> set[str]:
    return {w for w in _WORD_RE.findall((text or "").lower()) if len(w) >= 3}


def is_substantiated(claim: str | None, excerpt: str | None, min_overlap: float = _MIN_OVERLAP) -> bool:
    """True iff the excerpt materially mentions the claim's terms.

    Guards against URL-flooding with on-host-but-irrelevant pages: an excerpt that
    does not overlap the claim fails regardless of HTTP status.
    """
    claim_tokens = _tokens(claim)
    excerpt_tokens = _tokens(excerpt)
    if not claim_tokens or len(excerpt_tokens) < _MIN_EXCERPT_WORDS:
        return False
    overlap = len(claim_tokens & excerpt_tokens) / len(claim_tokens)
    return overlap >= min_overlap


def substantiation_reason(claim: str | None, excerpt: str | None, stype: SourceType, min_overlap: float = _MIN_OVERLAP) -> str:
    """Coarse, deterministic reason a reference did/didn't substantiate — for
    observability, so a withheld grade is auditable (unverified host vs missing/
    short excerpt vs no overlap). Does NOT change the grade math."""
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
    return "ok" if overlap >= min_overlap else "no_overlap"


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
