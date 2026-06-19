"""SEC EDGAR evidence adapter (Serenity Phase 7).

Turns ``(ticker, keywords)`` into ``*.sec.gov`` filing-document reference dicts for
``research.build_record(..., fetch_missing=True, fetch_headers=...)``. The adapter is a
PURE reference-builder layered on the SSRF-guarded ``src.serenity.fetch.fetch_excerpt``:
it owns no socket and re-implements no SSRF check. Every outbound call — the two
resolution JSON GETs here, and the downstream filing-body GET in ``build_record`` —
flows through ``fetch_excerpt``, so all host-allowlisting (``*.sec.gov`` → FILING),
IP-pinning, and per-redirect re-gating apply unchanged. No new SSRF surface: the
adapter emits only URLs whose host it asserts ends with ``.sec.gov``.

SEC EDGAR returns 403 to clients without a declared ``User-Agent`` (a contact string)
and asks for ≤10 req/s. So the adapter (a) sends a User-Agent on every EDGAR request via
the fetcher's ``headers`` passthrough, and (b) caps how many references one record can
emit so it can never burst the rate limit. The adapter NEVER raises into ``build_record``
and NEVER asserts substantiation — ``evidence.is_substantiated`` remains the independent
content gate on the *fetched* filing text (a filing that merely name-drops the ticker but
not the bottleneck claim does not substantiate).
"""

import json
import logging
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from src.serenity.evidence import DEFAULT_HOST_ALLOWLIST
from src.serenity.fetch import fetch_excerpt
from src.storage.models import SourceType

logger = logging.getLogger(__name__)

# EDGAR endpoints. Every host ends with .sec.gov → allowlisted as FILING by the fetcher.
_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"

# Strict allowlists, validated BEFORE the value ever influences a URL — the primary
# defense against an attacker-supplied ticker smuggling a host/path/scheme into EDGAR.
_TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")
_CIK_RE = re.compile(r"^\d{1,10}$")
_ACCESSION_RE = re.compile(r"^[0-9\-]{1,30}$")
_DOCUMENT_RE = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")

_DEFAULT_FORMS: tuple[str, ...] = ("10-K", "10-Q", "8-K")
_MAX_FILINGS_CAP = 5  # hard ceiling so a single record can never burst EDGAR's ~10 req/s
# SEC EDGAR's access policy accepts a project URL in place of a contact email; use the repo URL so the
# unset-env fallback never sends a placeholder address (which risks SEC rate-limiting / an access ban).
_DEFAULT_USER_AGENT = "ai-hedge-fund serenity-research https://github.com/Wolfxinze/ai-hedge-fund"
# Printable ASCII only — rejects CRLF/control chars so a caller- or env-supplied UA can't inject headers.
_UA_RE = re.compile(r"^[\x20-\x7E]+$")
# The EDGAR index JSONs are larger than an evidence excerpt. The default 2 MB filing cap
# would truncate them mid-object, json.loads would then fail, and CIK resolution would
# silently zero out. Give them headroom so a legitimately large index parses whole. (A
# body that still exceeds this is truncated → json.loads fails → clean degrade to None.)
_JSON_MAX_BYTES = 8_000_000
# Fetch-failure reasons that signal an API-shape change / block page (vs a benign miss) → WARNING.
# Parity with federal_register._fetch_json's actionable-reason leveling.
_ACTIONABLE_REASONS = {"bad_content_type", "too_large", "blocked_redirect"}


@dataclass(frozen=True)
class FilingRef:
    """One discovered filing. ``document_url`` is the *.sec.gov text whose body is later
    fetched + substantiation-checked downstream; the rest is provenance."""

    accession_no: str
    primary_document: str
    form: str
    filing_date: str
    document_url: str


def _user_agent(explicit: str | None) -> str:
    """Resolve the EDGAR User-Agent (arg > env > hardcoded contact). Never empty: a
    missing UA would silently 403 every fetch, so we fall back loudly (WARN) rather than
    send none. fetch.py stays source-agnostic — only this adapter reads the env."""
    ua = explicit or os.environ.get("SEC_EDGAR_USER_AGENT")
    if not ua:
        logger.warning(
            "SEC_EDGAR_USER_AGENT unset; using default contact UA. Set it to "
            "'Name email@example.com' to comply with SEC EDGAR's access policy."
        )
        return _DEFAULT_USER_AGENT
    # fullmatch (not match): '$' matches before a trailing '\n', so .match would leak a UA ending
    # in a newline straight into the header — the exact CRLF case this guard exists to block.
    if not _UA_RE.fullmatch(ua):
        logger.warning(
            "SEC_EDGAR_USER_AGENT contains non-printable or CRLF characters; falling back to "
            "the default UA to prevent header injection."
        )
        return _DEFAULT_USER_AGENT
    return ua


def _normalize_ticker(ticker: str) -> str | None:
    if not isinstance(ticker, str):
        return None
    t = ticker.strip().upper()
    return t if _TICKER_RE.match(t) else None


def _normalize_cik(cik: object) -> str | None:
    """10-digit zero-padded CIK string, or None if not a clean non-negative integer."""
    if isinstance(cik, bool):
        return None
    if isinstance(cik, int) and cik >= 0:
        return f"{cik:010d}"
    if isinstance(cik, str) and _CIK_RE.match(cik.strip()):
        return f"{int(cik.strip()):010d}"
    return None


def _is_sec_host(url: str) -> bool:
    """True iff ``url``'s host is sec.gov or a *.sec.gov subdomain AND it carries no userinfo
    (defense-in-depth: the adapter must never emit an off-sec.gov or userinfo-bearing source_url;
    fetch_excerpt rejects '@' too). Parity with _is_federal_register_host."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if "@" in (parts.netloc or ""):  # any userinfo — exact parity with the fetcher's _gate '@'-reject
        return False
    host = (parts.hostname or "").lower()
    return host == "sec.gov" or host.endswith(".sec.gov")


def _filing_document_url(cik: str, accession_no: str, primary_document: str) -> str:
    """Pure: compose the canonical *.sec.gov Archives document URL. Raises ValueError on a
    malformed accession/document so the caller drops that filing rather than emit garbage."""
    if not _ACCESSION_RE.match(accession_no) or not _DOCUMENT_RE.match(primary_document) or ".." in primary_document:
        raise ValueError("malformed accession/document")
    accession_nodash = accession_no.replace("-", "")
    return _ARCHIVES_URL.format(cik=int(cik), accession=accession_nodash, document=primary_document)


def _fetch_json(url: str, *, allowlist: dict[str, SourceType], user_agent: str) -> object | None:
    """GET a *.sec.gov JSON doc through the SSRF guard; parse it. Total: a blocked/failed
    fetch or malformed JSON yields None — never raises into the caller."""
    try:
        result = fetch_excerpt(
            url, allowlist=allowlist, max_bytes=_JSON_MAX_BYTES, headers={"User-Agent": user_agent}
        )
        if not result.ok or not result.excerpt:
            # A 403/429 is the failure the User-Agent exists to prevent (missing/garbage UA,
            # or an IP rate-limit ban) — it is operationally actionable, so it must be LOUDER
            # than a benign "this ticker has no such filing" miss.
            actionable = result.status in (403, 429) or result.reason in _ACTIONABLE_REASONS
            level = logging.WARNING if actionable else logging.INFO
            logger.log(level, "edgar fetch not ok (%s, http %s): %s", result.reason, result.status, url)
            return None
        return json.loads(result.excerpt)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.info("edgar json parse failed for %s: %s", url, exc)
        return None
    except Exception:  # totality — a bug here must not raise into build_record
        logger.exception("edgar unexpected fetch/parse error for %s", url)
        return None


def resolve_cik(
    ticker: str,
    *,
    allowlist: dict[str, SourceType] | None = None,
    user_agent: str | None = None,
) -> str | None:
    """Map a ticker to its zero-padded 10-digit CIK via company_tickers.json, or None.

    Returns None on an invalid ticker (rejected pre-fetch), a blocked/failed fetch,
    malformed JSON, or an absent ticker. Case-insensitive match. Never raises.
    """
    norm = _normalize_ticker(ticker)
    if norm is None:
        logger.info("edgar rejecting invalid ticker: %r", ticker)
        return None
    allowlist = allowlist if allowlist is not None else DEFAULT_HOST_ALLOWLIST
    data = _fetch_json(_COMPANY_TICKERS_URL, allowlist=allowlist, user_agent=_user_agent(user_agent))
    if not isinstance(data, dict):
        return None
    for row in data.values():
        if isinstance(row, dict) and str(row.get("ticker", "")).upper() == norm:
            return _normalize_cik(row.get("cik_str"))
    return None


def discover_filings(
    cik: str,
    *,
    forms: Sequence[str],
    max_filings: int,
    allowlist: dict[str, SourceType] | None = None,
    user_agent: str | None = None,
) -> list[FilingRef]:
    """Recent filings for ``cik`` (most-recent first), filtered to ``forms``, capped at
    ``max_filings``. Returns [] on an invalid CIK or any blocked/failed/malformed result."""
    cik_norm = _normalize_cik(cik)
    if cik_norm is None:
        return []
    allowlist = allowlist if allowlist is not None else DEFAULT_HOST_ALLOWLIST
    data = _fetch_json(
        _SUBMISSIONS_URL.format(cik10=cik_norm), allowlist=allowlist, user_agent=_user_agent(user_agent)
    )
    if not isinstance(data, dict):
        return []
    recent = ((data.get("filings") or {}).get("recent")) or {}
    accession = recent.get("accessionNumber") or []
    form_list = recent.get("form") or []
    filing_date = recent.get("filingDate") or []
    primary_doc = recent.get("primaryDocument") or []
    # EDGAR's recent block is columnar: index i of every array describes ONE filing.
    # Unequal lengths mean a partial/corrupt response — zip() would silently realign rows
    # and pair the wrong document with an accession, fabricating a host-valid but wrong
    # filing URL. Drop the whole block rather than emit misaligned evidence.
    cols = (accession, form_list, filing_date, primary_doc)
    if len({len(c) for c in cols}) != 1:
        logger.warning(
            "edgar submissions arrays misaligned for CIK %s (lengths %s); dropping",
            cik_norm, [len(c) for c in cols],
        )
        return []
    want = {str(f).upper() for f in forms}
    out: list[FilingRef] = []
    for acc, form, date, doc in zip(accession, form_list, filing_date, primary_doc):
        if not acc or not doc:
            continue
        if want and str(form).upper() not in want:
            continue
        try:
            url = _filing_document_url(cik_norm, str(acc), str(doc))
        except (ValueError, TypeError):
            continue
        if not _is_sec_host(url):  # belt-and-suspenders; cannot happen with the fixed template
            continue
        out.append(
            FilingRef(
                accession_no=str(acc),
                primary_document=str(doc),
                form=str(form),
                filing_date=str(date),
                document_url=url,
            )
        )
        if len(out) >= max_filings:
            break
    return out


def _claim_summary(keywords: Sequence[str]) -> str:
    """The claim text matched (downstream) against the fetched filing body. Keyword-only:
    metadata like the accession/date would only dilute the claim↔excerpt overlap, and is
    already carried by FilingRef + the source_url for provenance."""
    return " ".join(str(k).strip() for k in keywords if str(k).strip())


def build_edgar_references(
    ticker: str,
    *,
    keywords: Sequence[str],
    forms: Sequence[str] = _DEFAULT_FORMS,
    max_filings: int = 3,
    allowlist: dict[str, SourceType] | None = None,
    user_agent: str | None = None,
) -> list[dict]:
    """Resolve ``ticker`` → CIK → recent filings and return reference dicts
    ``{source_url (a *.sec.gov filing doc), claim_summary}`` for
    ``build_record(references=..., fetch_missing=True, fetch_headers=edgar_fetch_headers(...))``.

    NEVER raises and NEVER fetches a filing body itself (only the two small resolution
    GETs go out here). An invalid ticker / blocked resolution / no matching filing yields
    [] — a record then builds with zero references and simply stays ungraded.
    """
    try:  # totality: a non-int max_filings must degrade, not raise into the caller
        max_filings = max(1, min(int(max_filings), _MAX_FILINGS_CAP))
    except (TypeError, ValueError):
        max_filings = 3
    allowlist = allowlist if allowlist is not None else DEFAULT_HOST_ALLOWLIST
    ua = _user_agent(user_agent)
    cik = resolve_cik(ticker, allowlist=allowlist, user_agent=ua)
    if cik is None:
        return []
    filings = discover_filings(
        cik, forms=forms, max_filings=max_filings, allowlist=allowlist, user_agent=ua
    )
    claim = _claim_summary(keywords)
    return [{"source_url": f.document_url, "claim_summary": claim} for f in filings]


def edgar_fetch_headers(user_agent: str | None = None) -> dict[str, str]:
    """The header dict to pass as ``build_record(fetch_headers=...)`` so the downstream
    filing-body fetch also carries the EDGAR User-Agent (else SEC 403s it)."""
    return {"User-Agent": _user_agent(user_agent)}
