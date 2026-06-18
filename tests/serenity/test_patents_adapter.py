"""Phase 7c: Google Patents evidence adapter. Fully OFFLINE — the adapter itself makes NO
network call (it is a pure number→URL reference builder, with no resolve step), so the body
fetch + substantiation are exercised through ``research.build_record(fetch_missing=True)`` with
``research.fetch_excerpt`` stubbed. Tests encode WHY:
  (1) an attacker-supplied patent number can NEVER smuggle a host/path/scheme into the URL
      (the number is a URL PATH segment; the regex is the primary pre-fetch defense) — a bad
      number is dropped and never produces a downstream fetch;
  (2) only on-host patents.google.com URLs are emitted;
  (3) the adapter never raises (degrades to []) and caps its output;
  (4) the adapter never asserts substantiation — evidence.is_substantiated stays the content
      gate on the FETCHED patent body (an overlapping body substantiates; off-topic boilerplate,
      even on a real patent page, does not).
"""

import logging

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.storage.models as m
from src.serenity import research
from src.serenity.adapters import patents
from src.serenity.adapters.patents import (
    _is_patents_host,
    _normalize_number,
    _patent_url,
    build_patent_references,
    patents_fetch_headers,
)
from src.serenity.fetch import FetchResult
from src.storage.models import SourceType

# ── pure builder: valid numbers → on-host path URLs ─────────────────────────────


@pytest.mark.parametrize(
    "number",
    ["US6285999B1", "US8504504B2", "US20210123456A1", "EP1234567B1", "WO2021123456A1", "CN112345678A"],
)
def test_valid_numbers_emit_on_host_path_urls(number):
    refs = build_patent_references([number], keywords=["node", "ranking"])
    assert len(refs) == 1
    url = refs[0]["source_url"]
    assert _is_patents_host(url)
    assert url == f"https://patents.google.com/patent/{number}/en"
    assert "excerpt" not in refs[0]  # missing excerpt forces the downstream substantiation fetch
    assert refs[0]["claim_summary"] == "node ranking"  # keyword-only (metadata would dilute overlap)


def test_lowercase_number_is_normalized_not_rejected():
    """Mirrors edgar ticker handling: strip().upper() so a lowercased number is valid."""
    refs = build_patent_references(["us6285999b1"], keywords=["k"])
    assert refs and refs[0]["source_url"] == "https://patents.google.com/patent/US6285999B1/en"


def test_number_is_path_segment_not_query():
    """The number must land in the PATH, never as a query value that could push '/en' off-path."""
    from urllib.parse import urlsplit

    refs = build_patent_references(["US9876543B2"], keywords=["k"])
    parts = urlsplit(refs[0]["source_url"])
    assert parts.path == "/patent/US9876543B2/en"
    assert parts.query == ""


# ── injection: a malicious number is dropped pre-URL-build ───────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        "US1234/../evil",        # path traversal
        "/US1234",               # absolute-path / leading slash
        "//evil.com/US1234",     # protocol-relative host smuggle
        "US1234@evil.com",       # userinfo / host spoof
        "US1234%2e%2e",          # percent-encoded ..
        "US1234%2f",             # percent-encoded /
        "US1234?q=evil",         # query injection
        "US1234#frag",           # fragment injection
        "US1234:evil",           # scheme/port injection
        r"US1234\evil",          # backslash separator
        "USУ234",           # cyrillic 'У' homograph — [A-Z0-9] is ASCII-only
        "XX1234567",             # unknown office prefix
        "US..1234",              # dot-dot (also caught by explicit guard)
        "US" + "A" * 21,         # overlong suffix (>20)
        "",                      # empty
        "   ",                   # whitespace-only
    ],
)
def test_injection_numbers_dropped(bad):
    assert build_patent_references([bad], keywords=["k"]) == []


def test_mixed_valid_and_invalid_keeps_only_valid():
    refs = build_patent_references(["US6285999B1", "US1234/../evil", "EP1234567B1"], keywords=["k"])
    assert [r["source_url"] for r in refs] == [
        "https://patents.google.com/patent/US6285999B1/en",
        "https://patents.google.com/patent/EP1234567B1/en",
    ]


# ── host invariant + helpers ─────────────────────────────────────────────────────


def test_host_invariant_on_emitted_source_urls():
    from urllib.parse import urlsplit

    refs = build_patent_references(["US6285999B1", "EP1234567B1"], keywords=["k"])
    for r in refs:
        assert urlsplit(r["source_url"]).hostname == "patents.google.com"
        assert not urlsplit(r["source_url"]).username


def test_is_patents_host_rejects_offhost_and_userinfo():
    assert _is_patents_host("https://patents.google.com/patent/US1/en")
    assert not _is_patents_host("https://patents.google.com.evil.com/x")  # suffix-without-dot trap
    assert not _is_patents_host("https://evil.com/patent/US1/en")
    assert not _is_patents_host("https://user@patents.google.com/x")  # userinfo rejected


def test_patent_url_raises_only_via_assertion_on_bad_template(monkeypatch):
    """_patent_url is pure; its host assertion is the machine-checkable invariant. A future
    template edit that produced an off-host URL must raise (caught upstream, not emitted)."""
    monkeypatch.setattr(patents, "_PATENT_URL_TEMPLATE", "https://evil.com/{number}")
    with pytest.raises(ValueError):
        _patent_url("US6285999B1")


# ── caps / degrade / totality ────────────────────────────────────────────────────


def test_max_patents_capped():
    numbers = [f"US{i}000000B2" for i in range(10)]
    refs = build_patent_references(numbers, keywords=["k"], max_patents=99)
    assert len(refs) <= patents._MAX_PATENTS_CAP


def test_bad_max_patents_degrades():
    refs = build_patent_references(["US6285999B1"], keywords=["k"], max_patents="bad")
    assert isinstance(refs, list) and refs  # non-int cap degrades to default, never raises


@pytest.mark.parametrize("not_a_sequence", [None, 42])
def test_non_iterable_numbers_degrade(not_a_sequence):
    """A non-iterable ``numbers`` (caller error) degrades to [] via totality — never raises."""
    assert build_patent_references(not_a_sequence, keywords=["k"]) == []


def test_single_element_list_is_valid_not_a_degrade():
    """['US123'] is a one-element list of a regex-valid number — it must NOT degrade to []."""
    assert build_patent_references(["US123"], keywords=["k"]) == [
        {"source_url": "https://patents.google.com/patent/US123/en", "claim_summary": "k"}
    ]


def test_never_raises_on_internal_error(monkeypatch, caplog):
    """Totality: an unexpected error inside the build loop degrades to [] AND is logged loudly
    (the fail-loud guarantee is part of the contract, not incidental) — never propagates."""

    def boom(_number):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(patents, "_patent_url", boom)
    with caplog.at_level(logging.ERROR, logger="src.serenity.adapters.patents"):
        assert build_patent_references(["US6285999B1"], keywords=["k"]) == []
    assert "patents unexpected error" in caplog.text


def test_max_patents_zero_floors_to_one():
    """The max(1, ...) floor: a 0/negative cap must still emit one ref, not silently empty."""
    assert len(build_patent_references(["US123"], keywords=["k"], max_patents=0)) == 1


def test_non_string_element_dropped_siblings_survive():
    """A poisoned non-string element in the list is dropped while valid siblings survive."""
    refs = build_patent_references([None, 42, "US6285999B1"], keywords=["k"])
    assert [r["source_url"] for r in refs] == ["https://patents.google.com/patent/US6285999B1/en"]


def test_empty_keywords_yields_empty_claim_summary():
    refs = build_patent_references(["US6285999B1"], keywords=[])
    assert refs and refs[0]["claim_summary"] == ""


def test_claim_summary_is_keywords_only_not_number():
    refs = build_patent_references(["US6285999B1"], keywords=["node", "ranking"])
    assert refs[0]["claim_summary"] == "node ranking"
    assert "US6285999B1" not in refs[0]["claim_summary"]  # the number never dilutes the overlap


def test_patents_fetch_headers_is_empty():
    """Google Patents needs no User-Agent (unlike SEC EDGAR) — the helper returns {}."""
    assert patents_fetch_headers() == {}


def test_normalize_number_guards():
    assert _normalize_number("US6285999B1") == "US6285999B1"
    assert _normalize_number(" us6285999b1 ") == "US6285999B1"
    assert _normalize_number(None) is None
    assert _normalize_number(123) is None
    assert _normalize_number("US12@34") is None
    assert _normalize_number("US..34") is None
    # fullmatch (not match) pin: a valid prefix followed by an internal separator must be rejected
    # whole — a .match() regression would accept the "US123" prefix and emit a path-injected URL.
    assert _normalize_number("US123 B2") is None
    assert _normalize_number("US123/B2") is None


# ── end-to-end composition with build_record (adapter never asserts substantiation) ──


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    m.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


_PATENT_BODY = (
    "A method assigns importance node ranking to documents in a linked database using an "
    "iterative citation analysis across the document network."
)
_BOILERPLATE = (
    "Cookie preferences help privacy terms about advanced search download scholar similar export."
)


def test_patent_ref_substantiates_only_when_body_overlaps(session, monkeypatch):
    """The adapter emits a reference; whether it substantiates is decided by build_record on the
    FETCHED body. Overlapping patent text → substantiated PATENT; off-topic boilerplate → not."""
    refs = build_patent_references(["US6285999B1"], keywords=["node", "ranking", "linked", "database"])
    assert len(refs) == 1

    monkeypatch.setattr(
        research, "fetch_excerpt",
        lambda url, **k: FetchResult(True, _PATENT_BODY, url, 200, "text/html", "ok", len(_PATENT_BODY)),
    )
    rec = research.build_record(session, theme="node ranking", references=refs, scorecard={"supplier_concentration": 4}, fetch_missing=True, fetch_headers=patents_fetch_headers())
    session.commit()
    ev = session.query(m.EvidenceReference).filter_by(record_id=rec.id).one()
    assert ev.source_type == SourceType.PATENT.value  # patents.google.com → PATENT
    assert ev.substantiated is True

    monkeypatch.setattr(
        research, "fetch_excerpt",
        lambda url, **k: FetchResult(True, _BOILERPLATE, url, 200, "text/html", "ok", len(_BOILERPLATE)),
    )
    rec2 = research.build_record(session, theme="node ranking", references=refs, scorecard={"supplier_concentration": 4}, fetch_missing=True, fetch_headers=patents_fetch_headers())
    session.commit()
    ev2 = session.query(m.EvidenceReference).filter_by(record_id=rec2.id).one()
    assert ev2.substantiated is False  # real patent page that doesn't discuss the claim ≠ substantiated


def test_invalid_number_produces_no_downstream_fetch(session, monkeypatch):
    """'NO fetch on a bad number': a rejected number yields no reference, so build_record never
    fetches anything derived from it. Only the valid number's URL is fetched."""
    calls = []

    def recording(url, **k):
        calls.append(url)
        return FetchResult(True, _PATENT_BODY, url, 200, "text/html", "ok", len(_PATENT_BODY))

    monkeypatch.setattr(research, "fetch_excerpt", recording)
    refs = build_patent_references(["US6285999B1", "US1234/../evil"], keywords=["node", "ranking"])
    assert len(refs) == 1  # the malicious number was dropped pre-URL-build
    research.build_record(session, theme="t", references=refs, scorecard={}, fetch_missing=True, fetch_headers=patents_fetch_headers())
    session.commit()
    assert calls == ["https://patents.google.com/patent/US6285999B1/en"]
