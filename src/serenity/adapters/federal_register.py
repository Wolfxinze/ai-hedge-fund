"""Federal Register evidence adapter (Serenity Phase 7b).

Mirrors the EDGAR adapter: a pure reference-builder on the SSRF-guarded fetcher. Given a
search term + keywords, queries the public Federal Register `documents.json` API (no auth)
and returns ``{source_url (a federalregister.gov document page), claim_summary}`` reference
dicts for ``research.build_record(fetch_missing=True)``.

``source_url`` is taken VERBATIM from each hit's ``html_url`` and host-asserted — the adapter
never composes a federalregister.gov URL from string pieces, so (unlike EDGAR's path-built
filing URLs) there is no path-injection surface; the search term is only ever a urlencoded
query VALUE. Total: never raises into ``build_record``, caps output, and never asserts
substantiation (``evidence.is_substantiated`` stays the content gate on the fetched body).
Federal Register serves the API without a User-Agent; the UA here is courtesy only (a
descriptive UA avoids bot-blocking that would 302 to an unblock page).
"""

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

from src.serenity.evidence import DEFAULT_HOST_ALLOWLIST
from src.serenity.fetch import fetch_excerpt
from src.storage.models import SourceType

logger = logging.getLogger(__name__)

_DOCUMENTS_URL = "https://www.federalregister.gov/api/v1/documents.json"
_FR_HOST = "federalregister.gov"

# The term is only ever a urlencoded query VALUE (never a host/path segment), so it is far
# lower-risk than EDGAR's ticker; still validate to a conservative full-text-search charset.
_TERM_RE = re.compile(r"^[\w .,&\-/+()]{1,256}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DOC_TYPE_VALUES = {"RULE", "PRORULE", "NOTICE", "PRESDOCU"}

_FIELDS = ("document_number", "title", "type", "publication_date", "html_url")
_MAX_DOCS_CAP = 5  # hard ceiling so a single record can't burst the API rate limit
_PER_PAGE = 20  # small fixed page; never paginate beyond page 1 for evidence
_JSON_MAX_BYTES = 4_000_000
_DEFAULT_USER_AGENT = "ai-hedge-fund serenity-research contact@example.com"


@dataclass(frozen=True)
class FederalRegisterDoc:
    document_number: str
    title: str
    type: str
    publication_date: str
    html_url: str


def _user_agent(explicit: str | None) -> str:
    return explicit or _DEFAULT_USER_AGENT


def federal_register_fetch_headers(user_agent: str | None = None) -> dict[str, str]:
    """Courtesy User-Agent for the downstream document-body fetch. Federal Register does NOT
    require a UA (unlike SEC EDGAR), but a descriptive UA avoids bot-blocking."""
    return {"User-Agent": _user_agent(user_agent)}


def _is_federal_register_host(url: str) -> bool:
    """True iff ``url``'s host is federalregister.gov or a subdomain (defense-in-depth: a hit's
    html_url must stay on-host; pdf_url/raw_text_url often point off-host to govinfo.gov/S3)."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host == _FR_HOST or host.endswith("." + _FR_HOST)


def _normalize_term(term: str) -> str | None:
    if not isinstance(term, str):
        return None
    t = term.strip()
    return t if _TERM_RE.match(t) else None


def _claim_summary(keywords: Sequence[str]) -> str:
    return " ".join(str(k).strip() for k in keywords if str(k).strip())


def _build_query(
    term: str,
    doc_types: Sequence[str],
    published_after: str | None,
    published_before: str | None,
    per_page: int,
) -> str:
    """Compose the documents.json URL. Every variable is a urlencoded query VALUE on a
    hardcoded scheme+host+path, so nothing can escape into the netloc/path."""
    params = [("conditions[term]", term), ("per_page", str(per_page)), ("order", "relevance")]
    params.extend(("fields[]", f) for f in _FIELDS)
    params.extend(("conditions[type][]", dt) for dt in doc_types)
    if published_after:
        params.append(("conditions[publication_date][gte]", published_after))
    if published_before:
        params.append(("conditions[publication_date][lte]", published_before))
    return _DOCUMENTS_URL + "?" + urlencode(params)


def _fetch_json(url: str, *, allowlist: dict[str, SourceType], user_agent: str) -> object | None:
    try:
        result = fetch_excerpt(url, allowlist=allowlist, max_bytes=_JSON_MAX_BYTES, headers={"User-Agent": user_agent})
        if not result.ok or not result.excerpt:
            level = logging.WARNING if result.status in (403, 429) else logging.INFO
            logger.log(level, "federal_register fetch not ok (%s, http %s): %s", result.reason, result.status, url)
            return None
        return json.loads(result.excerpt)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.info("federal_register json parse failed for %s: %s", url, exc)
        return None
    except Exception:  # totality — a bug here must not raise into build_record
        logger.exception("federal_register unexpected fetch/parse error for %s", url)
        return None


def search_documents(
    term: str,
    *,
    doc_types: Sequence[str] | None = None,
    published_after: str | None = None,
    published_before: str | None = None,
    max_documents: int,
    allowlist: dict[str, SourceType] | None = None,
    user_agent: str | None = None,
) -> list[FederalRegisterDoc]:
    """One search GET (no resolve step). Returns up to ``max_documents`` docs whose html_url
    is on federalregister.gov. [] on invalid input / blocked / malformed / empty. Never raises."""
    norm = _normalize_term(term)
    if norm is None:
        logger.info("federal_register rejecting invalid term: %r", term)
        return []
    try:  # totality: a non-int cap degrades, never raises
        cap = max(1, min(int(max_documents), _MAX_DOCS_CAP))
    except (TypeError, ValueError):
        cap = 3
    types = tuple(str(dt).upper() for dt in (doc_types or ()) if str(dt).upper() in _DOC_TYPE_VALUES)
    after = published_after if (published_after and _DATE_RE.match(published_after)) else None
    before = published_before if (published_before and _DATE_RE.match(published_before)) else None
    allowlist = allowlist if allowlist is not None else DEFAULT_HOST_ALLOWLIST
    url = _build_query(norm, types, after, before, min(cap, _PER_PAGE))
    data = _fetch_json(url, allowlist=allowlist, user_agent=_user_agent(user_agent))
    if not isinstance(data, dict):
        return []
    results = data.get("results")
    if not isinstance(results, list):
        return []
    out: list[FederalRegisterDoc] = []
    for hit in results:
        if not isinstance(hit, dict):
            continue
        html_url = hit.get("html_url")
        if not isinstance(html_url, str) or not _is_federal_register_host(html_url):
            continue  # only the on-host human-readable page; never pdf_url/raw_text_url
        out.append(
            FederalRegisterDoc(
                document_number=str(hit.get("document_number", "")),
                title=str(hit.get("title", "")),
                type=str(hit.get("type", "")),
                publication_date=str(hit.get("publication_date", "")),
                html_url=html_url,
            )
        )
        if len(out) >= cap:
            break
    return out


def build_federal_register_references(
    term: str,
    *,
    keywords: Sequence[str],
    doc_types: Sequence[str] | None = None,
    published_after: str | None = None,
    published_before: str | None = None,
    max_documents: int = 3,
    allowlist: dict[str, SourceType] | None = None,
    user_agent: str | None = None,
) -> list[dict]:
    """Resolve ``term`` → recent Federal Register documents and return reference dicts
    ``{source_url (a federalregister.gov page), claim_summary}`` for
    ``build_record(references=..., fetch_missing=True, fetch_headers=federal_register_fetch_headers())``.
    NEVER raises and NEVER fetches a document body itself (only the one search GET goes out)."""
    docs = search_documents(
        term,
        doc_types=doc_types,
        published_after=published_after,
        published_before=published_before,
        max_documents=max_documents,
        allowlist=allowlist,
        user_agent=user_agent,
    )
    claim = _claim_summary(keywords)
    return [{"source_url": d.html_url, "claim_summary": claim} for d in docs]
