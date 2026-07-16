"""EDGAR full-text-search *seek* adapter (Serenity): find UNKNOWN tickers by keyword.

Where ``edgar.build_edgar_references`` starts from a KNOWN ticker, this adapter runs the
inverse: given bottleneck keywords, query SEC EDGAR full-text search and surface the
companies (CIK + tickers) whose recent filings match — candidates the caller did not
already know to look at. It is a PURE query/aggregate layer over the SSRF-guarded
``fetch.fetch_excerpt``: it owns no socket, re-implements no SSRF check, and only ever
composes ``https://efts.sec.gov`` URLs (a ``.sec.gov`` host → allowlisted as FILING).

IMPORTANT — User-Agent: efts.sec.gov requires the SEC contact format ``Name email@example.com``
in the User-Agent. A UA WITHOUT a contact (including this repo's default project-URL UA)
gets served an HTML 'Undeclared Automated Tool' block page instead of JSON. This adapter
does NOT crash on that: the HTML body fails ``json.loads`` and degrades to an entry in
``SeekResult.errors`` (per keyword), so a misconfigured UA is observable, not fatal. Set
``SEC_EDGAR_USER_AGENT`` (or pass ``user_agent=``) to a ``Name email@example.com`` string
for live use. UA resolution + the CRLF-injection guard are reused verbatim from ``edgar``.

Totality mirrors ``build_edgar_references``: ``seek_candidates`` NEVER raises on
network/HTTP/parse problems — each per-keyword failure appends a short message to
``errors`` and the remaining keywords still proceed. Keywords are hardened BEFORE URL
composition (stripped, 1-80 chars, no control/CRLF) so an attacker-supplied keyword can
never smuggle a header or an off-efts host into the request.
"""

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

from src.serenity.adapters.edgar import _JSON_MAX_BYTES, _is_sec_host, _normalize_cik, _user_agent
from src.serenity.evidence import DEFAULT_HOST_ALLOWLIST
from src.serenity.fetch import fetch_excerpt

logger = logging.getLogger(__name__)

# The only endpoint contacted. Host is efts.sec.gov → *.sec.gov → allowlisted FILING.
_EFTS_HOST = "efts.sec.gov"
_EFTS_URL = "https://efts.sec.gov/LATEST/search-index"

_MAX_KEYWORD_LEN = 80
_MAX_CANDIDATES_CAP = 25  # SEC-politeness ceiling; also the spec's upper bound
# Control chars / CR / LF — a keyword carrying any is rejected pre-fetch (header-injection
# defense; the keyword is urlencoded into ?q= but this fails loud rather than silently mangle).
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# A parenthesized group inside a display_name, e.g. "(TSM, TSMWF)" or "(CIK 0001046179)".
_PAREN_RE = re.compile(r"\(([^)]*)\)")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# A ticker symbol as EDGAR renders them in display_names (uppercase alnum + . -).
_TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")


@dataclass(frozen=True)
class SeekCandidate:
    """One company surfaced by the keyword search. ``cik`` is the stable identity; ``tickers``
    may be empty for a filer with no listed symbol (still a valid candidate)."""

    cik: str  # 10-digit zero-padded
    company: str
    tickers: tuple[str, ...]
    hits: int  # matching filings for this CIK across ALL keywords
    latest_filing_date: str | None  # YYYY-MM-DD, or None if no valid date was seen


@dataclass(frozen=True)
class SeekResult:
    """Total result of a seek: ranked candidates plus a per-keyword error trail. ``errors``
    is non-empty when a keyword was rejected pre-fetch or its query failed/blocked — the
    remaining keywords still contribute candidates (partial results)."""

    candidates: list[SeekCandidate] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _clean_keyword(keyword: object) -> str | None:
    """Return the stripped keyword if valid (str, 1-80 chars, no control/CRLF), else None."""
    if not isinstance(keyword, str):
        return None
    kw = keyword.strip()
    if not (1 <= len(kw) <= _MAX_KEYWORD_LEN):
        return None
    if _CONTROL_RE.search(kw):
        return None
    return kw


def _parse_display_name(display: str) -> tuple[str, tuple[str, ...]]:
    """Split ``display_names[0]`` into (company, tickers).

    company = text before the first '('. tickers = the comma-separated symbols in the FIRST
    parenthesized group that is NOT the '(CIK …)' group; an entry with no such group yields
    (). A malformed name with no parens returns the whole (stripped) string as company, ().
    """
    company = display.split("(", 1)[0].strip()
    tickers: tuple[str, ...] = ()
    for group in _PAREN_RE.findall(display):
        inner = group.strip()
        if not inner or inner.upper().startswith("CIK"):
            continue  # the CIK group is not a ticker group; skip empties too
        syms = tuple(s for s in (part.strip().upper() for part in inner.split(",")) if _TICKER_RE.match(s))
        tickers = syms
        break  # only the FIRST non-CIK group carries tickers
    return company, tickers


def _fetch_hits(keyword: str, *, user_agent: str) -> tuple[list[dict], str | None]:
    """Query efts FTS for one quoted keyword. Returns (hits, error): never raises.

    An error string (never both a populated ``hits`` and an error) is returned on a
    blocked/failed fetch, an off-efts host (defense-in-depth), or a non-JSON body — the
    last is the HTML 'Undeclared Automated Tool' block page served to a non-contact UA.
    """
    phrase = quote(f'"{keyword}"', safe="")  # quoted-phrase, fully urlencoded (no host escape)
    url = f"{_EFTS_URL}?q={phrase}"
    # Defense-in-depth: the keyword is urlencoded into the query, but assert the host anyway.
    if not _is_sec_host(url) or (urlsplit(url).hostname or "").lower() != _EFTS_HOST:
        return [], f"{keyword!r}: refused non-efts host"
    try:
        result = fetch_excerpt(
            url,
            allowlist=DEFAULT_HOST_ALLOWLIST,
            max_bytes=_JSON_MAX_BYTES,
            headers={"User-Agent": user_agent},
        )
    except Exception:  # totality — a fetch bug must not raise into the caller
        logger.exception("edgar_fts fetch crashed for %r", keyword)
        return [], f"{keyword!r}: fetch error"
    if not result.ok or not result.excerpt:
        return [], f"{keyword!r}: fetch failed ({result.reason})"
    try:
        data = json.loads(result.excerpt)
    except (json.JSONDecodeError, ValueError, TypeError):
        # Non-JSON body → almost always the HTML UA block page. Record, do not crash.
        return [], f"{keyword!r}: non-JSON response (likely UA block page — set a 'Name email' User-Agent)"
    hits = (((data or {}).get("hits") or {}).get("hits")) if isinstance(data, dict) else None
    if not isinstance(hits, list):
        return [], f"{keyword!r}: unexpected response shape"
    return hits, None


def _aggregate(hits: list[dict], acc: dict[str, dict]) -> None:
    """Fold one keyword's hits into ``acc`` (keyed by 10-digit CIK), mutating in place."""
    for hit in hits:
        source = hit.get("_source") if isinstance(hit, dict) else None
        if not isinstance(source, dict):
            continue
        ciks = source.get("ciks")
        cik = _normalize_cik(ciks[0]) if isinstance(ciks, list) and ciks else None
        if cik is None:
            continue  # a hit we can't group by identity is dropped, not fabricated
        display_list = source.get("display_names")
        display = display_list[0] if isinstance(display_list, list) and display_list else ""
        company, tickers = _parse_display_name(str(display))
        raw_date = source.get("file_date")
        file_date = str(raw_date) if isinstance(raw_date, str) and _DATE_RE.fullmatch(raw_date) else None

        entry = acc.get(cik)
        if entry is None:
            acc[cik] = {"company": company, "tickers": tickers, "hits": 1, "latest": file_date}
            continue
        entry["hits"] += 1
        if not entry["company"] and company:  # keep the first non-empty company/tickers seen
            entry["company"] = company
        if not entry["tickers"] and tickers:
            entry["tickers"] = tickers
        if file_date and (entry["latest"] is None or file_date > entry["latest"]):
            entry["latest"] = file_date


def seek_candidates(
    keywords: Sequence[str],
    *,
    max_candidates: int = 10,
    user_agent: str | None = None,
) -> SeekResult:
    """Seek UNKNOWN tickers whose recent filings match ``keywords`` via EDGAR full-text search.

    One quoted-phrase FTS GET per keyword; hits are grouped by CIK across ALL keywords,
    ranked by (hits desc, latest_filing_date desc), and capped at ``max_candidates``
    (clamped to 1..25). NEVER raises: a per-keyword failure (blocked fetch, HTML UA block
    page, malformed JSON) appends to ``SeekResult.errors`` and other keywords still proceed.
    An empty keyword list returns an empty result with one error.
    """
    try:
        cap = max(1, min(int(max_candidates), _MAX_CANDIDATES_CAP))
    except (TypeError, ValueError):
        cap = 10
    ua = _user_agent(user_agent)

    if not keywords:
        return SeekResult(candidates=[], errors=["no keywords provided"])

    acc: dict[str, dict] = {}
    errors: list[str] = []
    for raw in keywords:
        kw = _clean_keyword(raw)
        if kw is None:
            errors.append(f"invalid keyword rejected pre-fetch: {raw!r}")
            continue
        hits, err = _fetch_hits(kw, user_agent=ua)
        if err is not None:
            errors.append(err)
            continue
        _aggregate(hits, acc)

    candidates = [
        SeekCandidate(
            cik=cik,
            company=e["company"],
            tickers=e["tickers"],
            hits=e["hits"],
            latest_filing_date=e["latest"],
        )
        for cik, e in acc.items()
    ]
    # Rank by hits desc, then recency desc (None date sorts last via "" in a reverse sort).
    candidates.sort(key=lambda c: (c.hits, c.latest_filing_date or ""), reverse=True)
    return SeekResult(candidates=candidates[:cap], errors=errors)
