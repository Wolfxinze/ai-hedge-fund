"""Multi-source evidence gatherer (Serenity Phase 7b).

Fans the per-ticker evidence adapters (EDGAR filings + Federal Register documents) for one
``(ticker, keywords)`` request, dedups references by normalized ``source_url``, and returns
them GROUPED by source — each group paired with that source's fetch headers.

Why groups: ``build_record`` forwards ONE ``fetch_headers`` dict to every reference in a
call, so a single merged call would send the SEC User-Agent to Federal Register URLs too.
That is harmless today (a UA carries no credentials, and ``fetch.py`` strips hop-sensitive
headers on cross-host redirects), but keeping headers scoped per source stays correct when a
future source needs a header that MUST NOT leak. The caller does one ``build_record`` per
group. Total: one source failing never sinks the others, and a buggy adapter cannot leak an
off-allowlist URL into the merged list (final ``source_type_for_host`` filter).

This module owns NO socket and NO SSRF logic — every outbound call still flows through each
adapter's use of ``fetch_excerpt``.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from src.serenity.adapters import edgar, federal_register
from src.serenity.evidence import DEFAULT_HOST_ALLOWLIST, host_of, source_type_for_host
from src.storage.models import SourceType

logger = logging.getLogger(__name__)

_DEFAULT_SOURCES = ("edgar", "federal_register")


@dataclass(frozen=True)
class GatherResult:
    references: list[dict]  # deduped [{source_url, claim_summary}] across all sources
    headers_by_source: dict[str, dict]  # {"edgar": {"User-Agent": ...}, "federal_register": {...}}
    groups: list  # [(fetch_headers, [refs]), ...] — loop into one build_record per group


def _edgar_refs(ticker, keywords, max_per_source, allowlist, user_agent):
    return edgar.build_edgar_references(
        ticker, keywords=keywords, max_filings=max_per_source, allowlist=allowlist, user_agent=user_agent
    )


def _fedreg_refs(ticker, keywords, max_per_source, allowlist, user_agent):
    return federal_register.build_federal_register_references(
        ticker, keywords=keywords, max_documents=max_per_source, allowlist=allowlist, user_agent=user_agent
    )


# name → (reference_builder, headers_builder). "patents" is intentionally absent (number-driven,
# not ticker-driven; ticker→patent discovery is blocked on an allowlist decision — see issue).
_REGISTRY = {
    "edgar": (_edgar_refs, edgar.edgar_fetch_headers),
    "federal_register": (_fedreg_refs, federal_register.federal_register_fetch_headers),
}


def _norm_url(url: str) -> str:
    """Normalize for dedup: lowercase scheme+host, strip a trailing slash, drop fragment, keep
    query. Collisions across sources are near-impossible (disjoint hosts) — this mainly guards
    a single source emitting the same URL twice."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def gather_references(
    ticker: str,
    *,
    keywords: Sequence[str],
    sources: Sequence[str] = _DEFAULT_SOURCES,
    max_per_source: int = 3,
    allowlist: dict[str, SourceType] | None = None,
    user_agent: str | None = None,
) -> GatherResult:
    """Fan the requested ``sources`` for ``(ticker, keywords)`` and return deduped references
    grouped by source with each source's fetch headers. Total: a source raising contributes no
    references; an off-allowlist URL is dropped before it can reach the merged list."""
    allowlist = allowlist if allowlist is not None else DEFAULT_HOST_ALLOWLIST
    seen: set[str] = set()
    merged: list[dict] = []
    groups: list = []
    headers_by_source: dict[str, dict] = {}
    for name in sources:
        entry = _REGISTRY.get(name)
        if entry is None:
            logger.warning("gather: unknown source %r, skipping", name)
            continue
        builder, headers_fn = entry
        headers = headers_fn(user_agent)
        headers_by_source[name] = headers
        try:
            refs = builder(ticker, keywords, max_per_source, allowlist, user_agent)
        except Exception:  # totality — one source must never sink the others
            logger.exception("gather: source %r failed; contributing no references", name)
            refs = []
        group_refs: list[dict] = []
        for ref in refs or []:
            url = ref.get("source_url") if isinstance(ref, dict) else None
            if not isinstance(url, str):
                continue
            # Final host filter: a buggy adapter must not leak an off-allowlist URL into the list.
            if source_type_for_host(host_of(url), allowlist) is SourceType.UNVERIFIED:
                logger.warning("gather: dropping off-allowlist url from %s: %s", name, url)
                continue
            key = _norm_url(url)
            if key in seen:
                continue
            seen.add(key)
            merged.append(ref)
            group_refs.append(ref)
        groups.append((headers, group_refs))
    return GatherResult(references=merged, headers_by_source=headers_by_source, groups=groups)
