"""EDGAR full-text-search *seek* adapter — fully OFFLINE (fetch_excerpt stubbed).

Tests encode WHY: seek_candidates must (1) turn bottleneck keywords into UNKNOWN-ticker
candidates by querying efts.sec.gov FTS, parsing company + tickers out of display_names,
(2) aggregate hits per CIK across ALL keywords and rank by (hits desc, recency desc),
(3) be TOTAL like build_edgar_references — a per-keyword network/HTTP/parse failure (incl.
the HTML 'Undeclared Automated Tool' block page served to a non-contact-format UA) becomes
an entry in SeekResult.errors, never a raise, and other keywords still proceed, and
(4) harden every keyword BEFORE URL composition (CRLF/control/oversize rejected pre-fetch).
"""

import json

import pytest

from src.serenity.adapters import edgar_fts
from src.serenity.adapters.edgar_fts import (
    SeekCandidate,
    SeekResult,
    seek_candidates,
)
from src.serenity.fetch import FetchResult

# ── FTS response fixtures ──────────────────────────────────────────────────────


def _source(ciks, display, file_date, forms=("10-K",)):
    return {"_source": {"ciks": list(ciks), "display_names": list(display), "file_date": file_date, "root_forms": list(forms)}}


def _resp(*hits, total=None):
    return json.dumps({"hits": {"total": {"value": total if total is not None else len(hits)}, "hits": list(hits)}})


_TSM = _source(
    ["0001046179"],
    ["TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD  (TSM, TSMWF)  (CIK 0001046179)"],
    "2024-01-15",
)
_NOTICK = _source(
    ["0000320193"],
    ["SOME PRIVATE FILER LLC  (CIK 0000320193)"],  # only the CIK group → no ticker group
    "2024-02-01",
)
_MALFORMED = _source(
    ["0000789019"],
    ["GARBLED NAME NO PARENS AT ALL"],  # no parenthesized group at all
    "2024-03-01",
)


def _ok(excerpt):
    return FetchResult(True, excerpt, "https://efts.sec.gov/x", 200, "application/json", "ok", len(excerpt))


class _Fetcher:
    """Records every fetch_excerpt call; routes by URL substring (the urlencoded keyword)."""

    def __init__(self, routes, default=None):
        self.routes = routes  # list[(substr, FetchResult)]
        self.default = default if default is not None else _ok(_resp())
        self.calls = []

    def __call__(self, url, *, allowlist=None, max_bytes=None, headers=None, **kw):
        self.calls.append({"url": url, "allowlist": allowlist, "max_bytes": max_bytes, "headers": headers})
        for sub, res in self.routes:
            if sub in url:
                return res
        return self.default


def _wire(monkeypatch, routes=(), default=None):
    f = _Fetcher(list(routes), default=default)
    monkeypatch.setattr(edgar_fts, "fetch_excerpt", f)
    return f


# ── display_names parsing ──────────────────────────────────────────────────────


def test_parses_company_and_tickers_from_display_names(monkeypatch):
    _wire(monkeypatch, [("bottleneck", _ok(_resp(_TSM)))])
    result = seek_candidates(["bottleneck"])
    assert isinstance(result, SeekResult)
    assert result.errors == []
    (c,) = result.candidates
    assert isinstance(c, SeekCandidate)
    assert c.cik == "0001046179"
    assert c.company == "TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD"
    assert c.tickers == ("TSM", "TSMWF")
    assert c.hits == 1
    assert c.latest_filing_date == "2024-01-15"


def test_entry_without_ticker_group_is_valid_with_empty_tickers(monkeypatch):
    _wire(monkeypatch, [("bottleneck", _ok(_resp(_NOTICK)))])
    (c,) = seek_candidates(["bottleneck"]).candidates
    assert c.cik == "0000320193"
    assert c.company == "SOME PRIVATE FILER LLC"
    assert c.tickers == ()  # no non-CIK parenthesized group → empty, still a candidate


def test_malformed_display_name_still_yields_candidate(monkeypatch):
    _wire(monkeypatch, [("bottleneck", _ok(_resp(_MALFORMED)))])
    (c,) = seek_candidates(["bottleneck"]).candidates
    assert c.cik == "0000789019"
    assert c.tickers == ()
    assert c.company  # best-effort company, no crash


# ── aggregation + ranking ──────────────────────────────────────────────────────


def test_aggregates_hits_per_cik_across_keywords_and_ranks(monkeypatch):
    # TSM appears under both keywords (2 hits, latest 2024-05-01); a rival appears once.
    tsm_a = _source(["0001046179"], ["TSMC  (TSM)  (CIK 0001046179)"], "2024-01-15")
    tsm_b = _source(["0001046179"], ["TSMC  (TSM)  (CIK 0001046179)"], "2024-05-01")
    rival = _source(["0000111111"], ["RIVAL CORP  (RVL)  (CIK 0000111111)"], "2024-09-09")
    f = _wire(
        monkeypatch,
        [("chip", _ok(_resp(tsm_a, rival))), ("shortage", _ok(_resp(tsm_b)))],
    )
    result = seek_candidates(["chip", "shortage"])
    assert result.errors == []
    assert len(f.calls) == 2  # one FTS GET per keyword
    ranked = result.candidates
    assert [c.cik for c in ranked] == ["0001046179", "0000111111"]  # 2 hits ranks above 1 hit
    tsm = ranked[0]
    assert tsm.hits == 2
    assert tsm.latest_filing_date == "2024-05-01"  # max file_date seen across keywords


def test_ranking_breaks_ties_by_recency(monkeypatch):
    older = _source(["0000000001"], ["OLD CO  (OLD)  (CIK 0000000001)"], "2020-01-01")
    newer = _source(["0000000002"], ["NEW CO  (NEW)  (CIK 0000000002)"], "2024-01-01")
    _wire(monkeypatch, [("keyword", _ok(_resp(older, newer)))])
    ranked = seek_candidates(["keyword"]).candidates
    assert [c.cik for c in ranked] == ["0000000002", "0000000001"]  # equal hits → newer first


def test_respects_max_candidates_cap(monkeypatch):
    hits = [_source([f"000000000{i}"], [f"CO{i}  (T{i})  (CIK 000000000{i})"], f"2024-01-0{i}") for i in range(1, 6)]
    _wire(monkeypatch, [("keyword", _ok(_resp(*hits)))])
    result = seek_candidates(["keyword"], max_candidates=2)
    assert len(result.candidates) == 2  # capped


# ── totality: HTML block page, network failures ────────────────────────────────


def test_html_block_page_records_error_partial_candidates(monkeypatch):
    """efts.sec.gov serves an HTML 'Undeclared Automated Tool' page to a non-contact UA.
    fetch_excerpt returns it ok (text/html is textual); json.loads fails → recorded error,
    NOT a crash, and other keywords still produce candidates."""
    html = FetchResult(True, "<html><body>Undeclared Automated Tool</body></html>", "https://efts.sec.gov/x", 200, "text/html", "ok", 40)
    _wire(monkeypatch, [("blocked", html), ("bottleneck", _ok(_resp(_TSM)))])
    result = seek_candidates(["blocked", "bottleneck"])
    assert len(result.errors) == 1 and "blocked" in result.errors[0]
    assert [c.cik for c in result.candidates] == ["0001046179"]  # the good keyword still produced a candidate


def test_fetch_failure_becomes_error_not_raise(monkeypatch):
    _wire(monkeypatch, [("keyword", FetchResult(False, None, None, None, None, "timeout"))])
    result = seek_candidates(["keyword"])
    assert result.candidates == []
    assert len(result.errors) == 1 and "keyword" in result.errors[0]


def test_internal_exception_in_fetch_is_swallowed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(edgar_fts, "fetch_excerpt", boom)
    result = seek_candidates(["keyword"])
    assert result.candidates == []
    assert result.errors  # degraded to an error entry, never raised


# ── input hardening ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["inject\r\nX-Evil: y", "ctrl\x00char", "x" * 81, "", "   "])
def test_invalid_keyword_rejected_pre_fetch(monkeypatch, bad):
    f = _wire(monkeypatch)
    result = seek_candidates([bad])
    assert result.candidates == []
    assert result.errors  # rejected with a recorded error
    assert f.calls == []  # never composed a URL / hit the network


def test_empty_keyword_list_returns_error(monkeypatch):
    f = _wire(monkeypatch)
    result = seek_candidates([])
    assert result.candidates == []
    assert len(result.errors) == 1
    assert f.calls == []


def test_only_efts_host_contacted_with_quoted_phrase(monkeypatch):
    f = _wire(monkeypatch, [("bottleneck", _ok(_resp(_TSM)))])
    seek_candidates(["bottleneck"])
    (call,) = f.calls
    assert call["url"].startswith("https://efts.sec.gov/LATEST/search-index?q=")
    assert "%22bottleneck%22" in call["url"]  # quoted-phrase, urlencoded
    assert call["max_bytes"] == edgar_fts._JSON_MAX_BYTES  # bounded like edgar's JSON cap


# ── User-Agent resolution ──────────────────────────────────────────────────────


def test_user_agent_env_resolution(monkeypatch):
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "acme research contact@acme.com")
    f = _wire(monkeypatch, [("bottleneck", _ok(_resp(_TSM)))])
    seek_candidates(["bottleneck"])
    (call,) = f.calls
    assert call["headers"] == {"User-Agent": "acme research contact@acme.com"}


def test_explicit_user_agent_overrides_env(monkeypatch):
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "env ua env@example.com")
    f = _wire(monkeypatch, [("bottleneck", _ok(_resp(_TSM)))])
    seek_candidates(["bottleneck"], user_agent="explicit ua x@acme.com")
    (call,) = f.calls
    assert call["headers"] == {"User-Agent": "explicit ua x@acme.com"}


def test_crlf_user_agent_falls_back_not_injected(monkeypatch):
    monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
    f = _wire(monkeypatch, [("bottleneck", _ok(_resp(_TSM)))])
    seek_candidates(["bottleneck"], user_agent="inject\r\nX-Evil: y")
    (call,) = f.calls
    ua = call["headers"]["User-Agent"]
    assert "\r" not in ua and "\n" not in ua  # CRLF UA never reaches the header
