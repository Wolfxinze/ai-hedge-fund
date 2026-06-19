"""Phase 7: SEC EDGAR evidence adapter. Fully OFFLINE — fetch_excerpt is stubbed, no
real network. Tests encode WHY: the adapter must (1) resolve ticker→CIK and build only
*.sec.gov filing URLs, (2) NEVER let an attacker-supplied ticker smuggle an off-sec.gov
URL, (3) send the required EDGAR User-Agent, (4) bound its output so it can't burst the
rate limit, (5) never raise into build_record, and (6) never itself assert substantiation
— evidence.is_substantiated stays the independent content gate on the fetched filing text.
"""

import json
import logging

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.storage.models as m
from src.serenity import research
from src.serenity.adapters import edgar
from src.serenity.adapters.edgar import (
    _filing_document_url,
    _is_sec_host,
    _normalize_cik,
    build_edgar_references,
    discover_filings,
    edgar_fetch_headers,
    resolve_cik,
)
from src.serenity.fetch import FetchResult
from src.storage.models import SourceType

# ── EDGAR JSON fixtures ───────────────────────────────────────────────────────

_TICKERS_JSON = json.dumps(
    {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
    }
)

_SUBMISSIONS_JSON = json.dumps(
    {
        "cik": "320193",
        "name": "Apple Inc.",
        "filings": {
            "recent": {
                "accessionNumber": ["0000320193-23-000106", "0000320193-23-000077", "0000320193-23-000064"],
                "form": ["10-K", "8-K", "10-Q"],
                "filingDate": ["2023-11-03", "2023-08-04", "2023-05-05"],
                "primaryDocument": ["aapl-20230930.htm", "ef20231101.htm", "aapl-20230701.htm"],
            }
        },
    }
)


def _ok(excerpt):
    return FetchResult(True, excerpt, "https://x.sec.gov", 200, "application/json", "ok", len(excerpt))


def _fail(reason="connect_error"):
    return FetchResult(False, None, None, None, None, reason)


class _Fetcher:
    """Records every fetch_excerpt call and routes by URL substring."""

    def __init__(self, routes):
        self.routes = routes  # list[(substr, FetchResult)]
        self.calls = []

    def __call__(self, url, *, allowlist=None, max_bytes=None, headers=None, **kw):
        self.calls.append({"url": url, "allowlist": allowlist, "max_bytes": max_bytes, "headers": headers})
        for sub, res in self.routes:
            if sub in url:
                return res
        return _fail("off_allowlist")


def _wire(monkeypatch, **routes):
    """Stub edgar.fetch_excerpt; default routes return the canonical fixtures."""
    routed = [
        ("company_tickers.json", routes.get("tickers", _ok(_TICKERS_JSON))),
        ("submissions/CIK", routes.get("submissions", _ok(_SUBMISSIONS_JSON))),
    ]
    f = _Fetcher(routed)
    monkeypatch.setattr(edgar, "fetch_excerpt", f)
    return f


# ── resolve_cik ───────────────────────────────────────────────────────────────

def test_resolve_cik_zero_pads_and_is_case_insensitive(monkeypatch):
    _wire(monkeypatch)
    assert resolve_cik("aapl") == "0000320193"
    assert resolve_cik("MSFT") == "0000789019"


def test_resolve_cik_unknown_ticker_is_none(monkeypatch):
    _wire(monkeypatch)
    assert resolve_cik("ZZZZ") is None


def test_resolve_cik_blocked_fetch_is_none(monkeypatch):
    _wire(monkeypatch, tickers=_fail("blocked_private_ip"))
    assert resolve_cik("AAPL") is None


def test_resolve_cik_malformed_json_is_none(monkeypatch):
    _wire(monkeypatch, tickers=_ok("not json {{{"))
    assert resolve_cik("AAPL") is None


@pytest.mark.parametrize(
    "bad",
    ["../etc", "sec.gov@evil.com", "AAPL/../x", "http://x", "TOOLONGTICKERXX", "a b", "café", "", "  "],
)
def test_resolve_cik_rejects_malicious_ticker_pre_fetch(monkeypatch, bad):
    """An invalid ticker is rejected BEFORE any network call (no injection into URLs)."""
    f = _wire(monkeypatch)
    assert resolve_cik(bad) is None
    assert f.calls == []  # never reached the fetcher


# ── _filing_document_url (pure) ────────────────────────────────────────────────

def test_filing_document_url_strips_dashes_and_unpads_cik():
    url = _filing_document_url("0000320193", "0000320193-23-000106", "aapl-20230930.htm")
    assert url == "https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/aapl-20230930.htm"
    assert _is_sec_host(url)


@pytest.mark.parametrize(
    "acc,doc",
    [
        ("0000320193-23-000106", "../../evil.htm"),
        ("bad/accession", "x.htm"),
        ("0000320193-23-000106", "a b.htm"),
        ("0000320193-23-000106", ".."),  # dot-dot with no slash still rejected
    ],
)
def test_filing_document_url_rejects_malformed(acc, doc):
    with pytest.raises(ValueError):
        _filing_document_url("320193", acc, doc)


# ── discover_filings ───────────────────────────────────────────────────────────

def test_discover_filings_filters_forms_caps_and_orders(monkeypatch):
    _wire(monkeypatch)
    refs = discover_filings("0000320193", forms=("10-K", "10-Q"), max_filings=3)
    assert [r.form for r in refs] == ["10-K", "10-Q"]  # 8-K filtered out, most-recent-first
    assert all(_is_sec_host(r.document_url) for r in refs)
    assert refs[0].document_url.endswith("/000032019323000106/aapl-20230930.htm")


def test_discover_filings_respects_max(monkeypatch):
    _wire(monkeypatch)
    refs = discover_filings("0000320193", forms=("10-K", "10-Q", "8-K"), max_filings=1)
    assert len(refs) == 1


def test_discover_filings_invalid_cik_is_empty(monkeypatch):
    f = _wire(monkeypatch)
    assert discover_filings("../evil", forms=("10-K",), max_filings=3) == []
    assert f.calls == []  # rejected pre-fetch


def test_discover_filings_blocked_is_empty(monkeypatch):
    _wire(monkeypatch, submissions=_fail("timeout"))
    assert discover_filings("0000320193", forms=("10-K",), max_filings=3) == []


# ── build_edgar_references (entry point) ───────────────────────────────────────

def test_build_edgar_references_end_to_end(monkeypatch):
    f = _wire(monkeypatch)
    refs = build_edgar_references("AAPL", keywords=["supply", "chain", "bottleneck"], forms=("10-K", "10-Q"))
    assert len(refs) == 2
    for r in refs:
        assert _is_sec_host(r["source_url"])  # only *.sec.gov URLs emitted
        assert "excerpt" not in r  # missing excerpt forces the downstream substantiation fetch
        assert r["claim_summary"] == "supply chain bottleneck"  # keyword-only (metadata would dilute overlap)
    # the two resolution GETs each carried the EDGAR User-Agent
    assert all(c["headers"] and "User-Agent" in c["headers"] for c in f.calls)


def test_build_edgar_references_invalid_ticker_is_empty(monkeypatch):
    f = _wire(monkeypatch)
    assert build_edgar_references("sec.gov@evil", keywords=["x"]) == []
    assert f.calls == []


def test_build_edgar_references_caps_max_filings(monkeypatch):
    _wire(monkeypatch)
    refs = build_edgar_references("AAPL", keywords=["k"], forms=("10-K", "10-Q", "8-K"), max_filings=99)
    assert len(refs) <= 5  # hard ceiling so a single record can't burst EDGAR's rate limit


def test_build_edgar_references_never_raises_on_fetch_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(edgar, "fetch_excerpt", boom)
    assert build_edgar_references("AAPL", keywords=["x"]) == []  # degrades to [], does not raise


def test_user_agent_precedence(monkeypatch):
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "env-ua name@example.com")
    assert edgar_fetch_headers() == {"User-Agent": "env-ua name@example.com"}
    assert edgar_fetch_headers("explicit-ua x@example.com") == {"User-Agent": "explicit-ua x@example.com"}


def test_user_agent_falls_back_when_unset(monkeypatch):
    monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
    ua = edgar_fetch_headers()["User-Agent"]
    assert ua  # never empty — a missing UA would silently 403 every EDGAR fetch


# ── end-to-end composition with build_record ───────────────────────────────────

@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    m.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


_FILING_TEXT = "Apple discloses a severe supply chain bottleneck constraining advanced chip supply this year."
_BOILERPLATE = "Generic forward looking statements about markets competition and general economic conditions apply."


def test_edgar_refs_substantiate_only_when_filing_text_overlaps(session, monkeypatch):
    """The adapter never asserts substantiation: a filing whose fetched text overlaps the
    bottleneck claim substantiates; on-host boilerplate that doesn't overlap does NOT."""
    _wire(monkeypatch)
    refs = build_edgar_references("AAPL", keywords=["supply", "chain", "bottleneck"], forms=("10-K",), max_filings=1)
    assert len(refs) == 1

    # Filing body that actually discusses the bottleneck → substantiated.
    monkeypatch.setattr(
        research, "fetch_excerpt",
        lambda url, **k: FetchResult(True, _FILING_TEXT, url, 200, "text/html", "ok", len(_FILING_TEXT)),
    )
    rec = research.build_record(
        session, theme="chip supply", references=refs, scorecard={"supplier_concentration": 4},
        fetch_missing=True, fetch_headers=edgar_fetch_headers(),
    )
    session.commit()
    ev = session.query(m.EvidenceReference).filter_by(record_id=rec.id).one()
    assert ev.source_type == SourceType.FILING.value  # *.sec.gov → FILING
    assert ev.substantiated is True

    # On-host boilerplate that does NOT mention the claim → NOT substantiated.
    monkeypatch.setattr(
        research, "fetch_excerpt",
        lambda url, **k: FetchResult(True, _BOILERPLATE, url, 200, "text/html", "ok", len(_BOILERPLATE)),
    )
    rec2 = research.build_record(
        session, theme="chip supply", references=refs, scorecard={"supplier_concentration": 4},
        fetch_missing=True, fetch_headers=edgar_fetch_headers(),
    )
    session.commit()
    ev2 = session.query(m.EvidenceReference).filter_by(record_id=rec2.id).one()
    assert ev2.substantiated is False


# ── review hardening: misalignment, observability, caps, edge cases ────────────

_MISALIGNED_JSON = json.dumps(
    {
        "filings": {
            "recent": {
                "accessionNumber": ["0000320193-23-000106", "0000320193-23-000077"],
                "form": ["10-K"],  # shorter than the others → columnar misalignment
                "filingDate": ["2023-11-03", "2023-08-04"],
                "primaryDocument": ["a.htm", "b.htm"],
            }
        }
    }
)


def test_discover_filings_drops_misaligned_arrays(monkeypatch):
    """Unequal parallel arrays would let zip() pair the wrong document with an accession;
    the whole block must be dropped rather than emit a host-valid but WRONG filing URL."""
    _wire(monkeypatch, submissions=_ok(_MISALIGNED_JSON))
    assert discover_filings("0000320193", forms=("10-K", "10-Q"), max_filings=3) == []


def test_403_is_logged_at_warning(monkeypatch, caplog):
    """A 403 (the failure the User-Agent exists to prevent) must be louder than a benign miss."""
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "test test@example.com")  # silence the unset-UA WARN
    blocked = FetchResult(False, None, "https://www.sec.gov/x", 403, "text/html", "http_error")
    _wire(monkeypatch, tickers=blocked)
    with caplog.at_level(logging.INFO, logger="src.serenity.adapters.edgar"):
        assert resolve_cik("AAPL") is None
    assert any(r.levelno == logging.WARNING and "403" in r.getMessage() for r in caplog.records)


def test_benign_miss_stays_info(monkeypatch, caplog):
    """A non-403 miss (e.g. timeout) stays at INFO so 403s remain distinguishable."""
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "test test@example.com")  # silence the unset-UA WARN
    _wire(monkeypatch, tickers=_fail("timeout"))
    with caplog.at_level(logging.INFO, logger="src.serenity.adapters.edgar"):
        assert resolve_cik("AAPL") is None
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def test_resolution_gets_request_large_byte_cap(monkeypatch):
    """The index JSONs must be fetched with the larger cap, else a big company_tickers.json
    truncates and CIK resolution silently fails. Pins the constant's whole purpose."""
    f = _wire(monkeypatch)
    resolve_cik("AAPL")
    assert f.calls and all(c["max_bytes"] == edgar._JSON_MAX_BYTES for c in f.calls)


def test_resolution_gets_carry_exact_user_agent(monkeypatch):
    f = _wire(monkeypatch)
    build_edgar_references("AAPL", keywords=["k"], forms=("10-K",), user_agent="acme test@acme.com")
    assert f.calls and all(c["headers"] == {"User-Agent": "acme test@acme.com"} for c in f.calls)


def test_bad_max_filings_degrades_not_raises(monkeypatch):
    _wire(monkeypatch)
    refs = build_edgar_references("AAPL", keywords=["k"], forms=("10-K", "10-Q"), max_filings="bad")
    assert isinstance(refs, list)  # totality: a non-int cap degrades to the default, never raises


def test_empty_keywords_yields_unsubstantiatable_refs(monkeypatch):
    """Empty keywords → empty claim → can never substantiate (pins intent vs a future default)."""
    _wire(monkeypatch)
    refs = build_edgar_references("AAPL", keywords=[], forms=("10-K",), max_filings=1)
    assert len(refs) == 1 and refs[0]["claim_summary"] == ""


def test_discover_filings_no_matching_forms_is_empty(monkeypatch):
    _wire(monkeypatch)
    assert discover_filings("0000320193", forms=("S-1",), max_filings=3) == []


def test_normalize_cik_guards():
    assert _normalize_cik(True) is None  # bool is an int subclass — must be rejected
    assert _normalize_cik(-1) is None
    assert _normalize_cik("../evil") is None
    assert _normalize_cik("320193") == "0000320193"
    assert _normalize_cik(320193) == "0000320193"


# ── deferred #13/#15 hardening: UA validation, default UA, userinfo parity, reason leveling ──


def test_user_agent_rejects_crlf_falls_back_to_default(monkeypatch, caplog):
    """A UA with CRLF must be rejected (header injection) → default used + WARNING, never raises."""
    monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
    bad_ua = "injected\r\nX-Evil: pwned"
    with caplog.at_level(logging.WARNING, logger="src.serenity.adapters.edgar"):
        result = edgar_fetch_headers(bad_ua)
    assert result["User-Agent"] != bad_ua  # the injected value never passes through
    assert result["User-Agent"]  # falls back to a safe non-empty UA
    assert any("CRLF" in r.getMessage() or "non-printable" in r.getMessage() for r in caplog.records)


def test_user_agent_accepts_valid_printable_ua(monkeypatch):
    """A printable-ASCII UA passes through unchanged (a future over-eager regex must not reject it)."""
    monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
    good_ua = "acme-research contact@acme.com"
    assert edgar_fetch_headers(good_ua) == {"User-Agent": good_ua}


def test_default_user_agent_contains_no_placeholder_email(monkeypatch):
    """The fallback UA must never send a fake address to SEC EDGAR (risks rate-limit/ban)."""
    monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
    assert "example.com" not in edgar_fetch_headers()["User-Agent"]


def test_user_agent_unset_emits_warning(monkeypatch, caplog):
    """The unset path must still WARN so the misconfiguration stays observable."""
    monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
    with caplog.at_level(logging.WARNING, logger="src.serenity.adapters.edgar"):
        edgar_fetch_headers()
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_is_sec_host_rejects_userinfo():
    """A userinfo-bearing URL with a *.sec.gov post-@ host must return False (parity with the
    fetcher's '@'-reject and _is_federal_register_host) so discover_filings never emits it."""
    assert _is_sec_host("https://evil@sec.gov/x") is False
    assert _is_sec_host("https://evil@data.sec.gov/submissions/x.json") is False
    assert _is_sec_host("https://www.sec.gov/Archives/edgar/data/x.htm") is True  # clean URL still passes


def test_bad_content_type_logged_at_warning(monkeypatch, caplog):
    """A 200 non-JSON body (block page / API-shape change) is actionable → WARNING, not INFO —
    parity with federal_register._fetch_json's actionable-reason leveling."""
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "test test@example.com")
    bad_ct = FetchResult(False, None, "https://www.sec.gov/x", 200, "text/html", "bad_content_type")
    _wire(monkeypatch, tickers=bad_ct)
    with caplog.at_level(logging.INFO, logger="src.serenity.adapters.edgar"):
        assert resolve_cik("AAPL") is None
    assert any(r.levelno == logging.WARNING and "bad_content_type" in r.getMessage() for r in caplog.records)


def test_connect_error_stays_info(monkeypatch, caplog):
    """A benign miss (connect_error) stays INFO; only actionable reasons escalate to WARNING."""
    monkeypatch.setenv("SEC_EDGAR_USER_AGENT", "test test@example.com")
    _wire(monkeypatch, tickers=_fail("connect_error"))
    with caplog.at_level(logging.INFO, logger="src.serenity.adapters.edgar"):
        assert resolve_cik("AAPL") is None
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)
