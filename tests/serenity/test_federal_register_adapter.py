"""Phase 7b: Federal Register adapter. Fully OFFLINE — fetch_excerpt is stubbed. Tests encode
WHY: (1) only on-host federalregister.gov html_urls are emitted (never off-host pdf/raw_text
links), (2) a search term can never change the request host (urlencoded query value), (3) the
adapter never raises and degrades to [], and (4) it never asserts substantiation — the fetched
body + is_substantiated remain the content gate.
"""

import json
import logging
from urllib.parse import parse_qs, urlsplit

import pytest

from src.serenity.adapters import federal_register as fr
from src.serenity.adapters.federal_register import (
    _is_federal_register_host,
    build_federal_register_references,
    federal_register_fetch_headers,
    search_documents,
)
from src.serenity.fetch import FetchResult

_RESULTS_JSON = json.dumps(
    {
        "count": 3,
        "results": [
            {
                "document_number": "2026-03065",
                "title": "Prohibition on Certain Semiconductor Products",
                "type": "Rule",
                "publication_date": "2026-02-17",
                "html_url": "https://www.federalregister.gov/documents/2026/02/17/2026-03065/semiconductor",
                "pdf_url": "https://www.govinfo.gov/content/pkg/FR-2026-02-17/pdf/2026-03065.pdf",
            },
            {
                "document_number": "2026-02000",
                "title": "Export Controls Notice",
                "type": "Notice",
                "publication_date": "2026-01-10",
                "html_url": "https://www.federalregister.gov/documents/2026/01/10/2026-02000/export",
            },
            {  # off-host html_url — must be DROPPED (defense-in-depth)
                "document_number": "evil",
                "title": "poisoned",
                "type": "Notice",
                "publication_date": "2026-01-01",
                "html_url": "https://evil.example.com/x",
            },
        ],
    }
)


def _ok(excerpt):
    return FetchResult(True, excerpt, "https://www.federalregister.gov/x", 200, "application/json", "ok", len(excerpt))


def _fail(reason="connect_error", status=None):
    return FetchResult(False, None, None, status, None, reason)


class _Fetcher:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, url, *, allowlist=None, max_bytes=None, headers=None, **kw):
        self.calls.append({"url": url, "allowlist": allowlist, "max_bytes": max_bytes, "headers": headers})
        return self.result


def _wire(monkeypatch, result=None):
    f = _Fetcher(result if result is not None else _ok(_RESULTS_JSON))
    monkeypatch.setattr(fr, "fetch_excerpt", f)
    return f


# ── happy path + host invariant ────────────────────────────────────────────────

def test_build_references_emits_only_on_host_html_urls(monkeypatch):
    f = _wire(monkeypatch)
    refs = build_federal_register_references("semiconductor", keywords=["semiconductor", "supply"], max_documents=5)
    assert len(refs) == 2  # the off-host third hit is dropped
    for r in refs:
        assert _is_federal_register_host(r["source_url"])
        assert "excerpt" not in r
        assert r["claim_summary"] == "semiconductor supply"
    # never emits the off-host pdf_url
    assert all("govinfo" not in r["source_url"] and "evil" not in r["source_url"] for r in refs)
    # one search GET, carrying the courtesy UA + the larger byte cap
    assert len(f.calls) == 1
    assert "User-Agent" in f.calls[0]["headers"]
    assert f.calls[0]["max_bytes"] == fr._JSON_MAX_BYTES


def test_term_cannot_change_request_host(monkeypatch):
    """A term with query-significant chars is urlencoded into the VALUE; host stays fixed."""
    f = _wire(monkeypatch, _ok(json.dumps({"results": []})))
    search_documents("supply & demand (chips)", max_documents=3)
    requested = f.calls[0]["url"]
    assert urlsplit(requested).hostname == "www.federalregister.gov"
    # the term survives intact as the conditions[term] query value
    assert parse_qs(urlsplit(requested).query)["conditions[term]"] == ["supply & demand (chips)"]


@pytest.mark.parametrize("bad_term", ["x=evil", "a://b", "<script>", "@host", "", "  ", "z" * 257])
def test_invalid_term_rejected_pre_fetch(monkeypatch, bad_term):
    f = _wire(monkeypatch)
    assert search_documents(bad_term, max_documents=3) == []
    assert f.calls == []  # rejected before any network call


# ── caps / degrade / totality ──────────────────────────────────────────────────

def test_max_documents_capped(monkeypatch):
    _wire(monkeypatch)
    refs = build_federal_register_references("x", keywords=["k"], max_documents=99)
    assert len(refs) <= 5


def test_bad_max_documents_degrades(monkeypatch):
    _wire(monkeypatch)
    assert isinstance(build_federal_register_references("x", keywords=["k"], max_documents="bad"), list)


def test_blocked_fetch_is_empty(monkeypatch):
    _wire(monkeypatch, _fail("blocked_private_ip"))
    assert build_federal_register_references("x", keywords=["k"]) == []


def test_malformed_json_is_empty(monkeypatch):
    _wire(monkeypatch, _ok("not json {{"))
    assert build_federal_register_references("x", keywords=["k"]) == []


def test_results_not_a_list_is_empty(monkeypatch):
    _wire(monkeypatch, _ok(json.dumps({"results": {"unexpected": "dict"}})))
    assert build_federal_register_references("x", keywords=["k"]) == []


def test_never_raises_on_fetch_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(fr, "fetch_excerpt", boom)
    assert build_federal_register_references("x", keywords=["k"]) == []


def test_403_logged_at_warning(monkeypatch, caplog):
    _wire(monkeypatch, _fail("http_error", status=403))
    with caplog.at_level(logging.INFO, logger="src.serenity.adapters.federal_register"):
        assert search_documents("x", max_documents=3) == []
    assert any(r.levelno == logging.WARNING and "403" in r.getMessage() for r in caplog.records)


# ── query construction details ─────────────────────────────────────────────────

def test_bad_date_dropped_good_date_kept(monkeypatch):
    f = _wire(monkeypatch, _ok(json.dumps({"results": []})))
    search_documents("x", published_after="2026-01-01", published_before="not-a-date", max_documents=3)
    q = parse_qs(urlsplit(f.calls[0]["url"]).query)
    assert q.get("conditions[publication_date][gte]") == ["2026-01-01"]
    assert "conditions[publication_date][lte]" not in q  # invalid date dropped, not raised


def test_invalid_doc_type_filtered(monkeypatch):
    f = _wire(monkeypatch, _ok(json.dumps({"results": []})))
    search_documents("x", doc_types=["RULE", "BOGUS"], max_documents=3)
    q = parse_qs(urlsplit(f.calls[0]["url"]).query)
    assert q.get("conditions[type][]") == ["RULE"]


def test_fetch_headers_returns_user_agent():
    assert "User-Agent" in federal_register_fetch_headers()
    assert federal_register_fetch_headers("custom ua")["User-Agent"] == "custom ua"
