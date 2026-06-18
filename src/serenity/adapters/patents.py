"""Google Patents evidence adapter (Serenity Phase 7c).

Turns explicit patent ``number(s)`` into ``patents.google.com`` reference dicts for
``research.build_record(..., fetch_missing=True, fetch_headers=patents_fetch_headers())``.
Like the EDGAR/Federal-Register adapters it is a PURE reference-builder layered on the
SSRF-guarded ``src.serenity.fetch.fetch_excerpt``: it owns no socket and re-implements no
SSRF check. Unlike them it has NO resolve step (a patent number maps to its URL by a fixed
template), so the adapter itself makes ZERO HTTP calls — the only outbound request is the
downstream patent-body GET, performed by ``build_record(fetch_missing=True)`` through the
fetcher, where ``patents.google.com → PATENT`` is already on ``DEFAULT_HOST_ALLOWLIST`` (no
new SSRF surface).

The patent number is a URL PATH segment (like EDGAR's filing document, unlike Federal
Register's urlencoded query term), so a STRICT pre-fetch number regex is the primary defense
against an attacker-supplied number smuggling a host/path/scheme: ``[A-Z0-9]`` is ASCII-only
and excludes ``/ . @ % ? # : \\`` and whitespace, so no separator can escape the path. The
adapter emits ONLY URLs whose host it asserts is ``patents.google.com`` and rejects userinfo
(``@``). It NEVER raises into ``build_record`` (degrades to []), caps its output, and NEVER
asserts substantiation — ``evidence.is_substantiated`` remains the independent content gate on
the *fetched* patent body (a real patent page that merely name-drops a keyword in boilerplate
but does not discuss the claim does not substantiate). Google Patents serves full text without
a User-Agent, so ``patents_fetch_headers()`` returns ``{}``.
"""

import logging
import re
from collections.abc import Sequence
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_PATENTS_HOST = "patents.google.com"
# Fixed template — the validated number is the ONLY interpolated value and lands in the path.
# The '/en' suffix pins the English rendering (else a non-US patent may 302 to a locale page).
_PATENT_URL_TEMPLATE = "https://patents.google.com/patent/{number}/en"

# Primary pre-fetch defense. Office prefix + an ASCII-alphanumeric suffix (kind codes A1/B2 etc.).
# [A-Z0-9] excludes every path/host/scheme separator, so no value that matches can escape the
# path segment. fullmatch (not match) is used so a trailing newline can't satisfy a '$' anchor.
_NUMBER_RE = re.compile(r"(US|EP|WO|CN)[A-Z0-9]{1,20}")
_MAX_PATENTS_CAP = 5  # hard ceiling so one record can't fan into an unbounded burst of fetches
_DEFAULT_MAX_PATENTS = 3


def patents_fetch_headers() -> dict[str, str]:
    """Headers for the downstream patent-body fetch. Google Patents requires NO User-Agent
    (unlike SEC EDGAR), so this is ``{}``; the helper exists for call-site symmetry with
    ``edgar_fetch_headers`` / ``federal_register_fetch_headers``."""
    return {}


def _is_patents_host(url: str) -> bool:
    """True iff ``url``'s host is patents.google.com (or a subdomain) and carries no userinfo.
    Defense-in-depth: the adapter must never emit an off-host source_url; the fetcher re-checks."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.username or parts.password:  # userinfo — the fetcher rejects '@'; stay consistent
        return False
    host = (parts.hostname or "").lower()
    return host == _PATENTS_HOST or host.endswith("." + _PATENTS_HOST)


def _normalize_number(raw: object) -> str | None:
    """Uppercased validated patent number, or None for anything not matching the strict regex.
    Belt-and-suspenders '..'/'@' checks back up the regex (which already excludes both)."""
    if not isinstance(raw, str):
        return None
    t = raw.strip().upper()
    if ".." in t or "@" in t:
        return None
    return t if _NUMBER_RE.fullmatch(t) else None


def _patent_url(number: str) -> str:
    """Pure: compose the canonical patents.google.com URL for a pre-validated ``number``. Asserts
    the result is on-host (ValueError otherwise) so a future template edit that opened a
    host-injection path fails loudly rather than emitting an off-host URL."""
    url = _PATENT_URL_TEMPLATE.format(number=number)
    if not _is_patents_host(url):
        raise ValueError(f"patent URL host assertion failed: {url!r}")
    return url


def _claim_summary(keywords: Sequence[str]) -> str:
    """The claim text matched (downstream) against the fetched patent body. Keyword-only: the
    patent number/jurisdiction would only dilute the claim↔excerpt overlap and is already carried
    by the source_url for provenance."""
    return " ".join(str(k).strip() for k in keywords if str(k).strip())


def build_patent_references(
    numbers: Sequence[str],
    *,
    keywords: Sequence[str],
    max_patents: int = _DEFAULT_MAX_PATENTS,
) -> list[dict]:
    """Validate ``numbers`` and return reference dicts ``{source_url (a patents.google.com page),
    claim_summary}`` for
    ``build_record(references=..., fetch_missing=True, fetch_headers=patents_fetch_headers())``.

    NEVER raises and NEVER fetches anything itself — the patent body is fetched downstream by
    build_record. An invalid number is dropped (logged INFO); a non-iterable ``numbers`` or any
    unexpected error degrades to []. Output is capped at ``min(max_patents, _MAX_PATENTS_CAP)``.
    """
    try:  # totality: a non-int max_patents must degrade, not raise into the caller
        cap = max(1, min(int(max_patents), _MAX_PATENTS_CAP))
    except (TypeError, ValueError):
        cap = _DEFAULT_MAX_PATENTS
    claim = _claim_summary(keywords)
    try:
        urls: list[str] = []
        for raw in numbers:
            num = _normalize_number(raw)
            if num is None:
                logger.info("patents rejecting invalid number: %r", raw)
                continue
            try:
                url = _patent_url(num)
            except (ValueError, TypeError):
                logger.warning("patents url host assertion failed for %r; dropping", num)
                continue
            if not _is_patents_host(url):  # belt-and-suspenders; cannot happen with the fixed template
                continue
            urls.append(url)
        if len(urls) > cap:
            logger.warning("patents: %d valid number(s) exceed cap %d; using the first %d", len(urls), cap, cap)
            urls = urls[:cap]
        return [{"source_url": u, "claim_summary": claim} for u in urls]
    except Exception:  # totality — a bug (e.g. a non-iterable numbers) must not raise into build_record
        logger.exception("patents unexpected error building references")
        return []
