"""Phase 7c: the `serenity research --patent` CLI wiring. OFFLINE — build_record + the DB
session are stubbed; the real (network-free) patents adapter runs so the test exercises the
true integration. Verifies a --patent number becomes a patents.google.com reference with
fetch_missing=True + empty headers, an invalid number is dropped, and a --url-only call stays
offline (fetch_missing=False), preserving existing behavior.
"""

import contextlib
import types

import pytest

from src.serenity import cli


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    monkeypatch.setattr(cli.Base.metadata, "create_all", lambda **k: None)
    monkeypatch.setattr(cli, "session_scope", lambda: contextlib.nullcontext(object()))


def _capture_build_record(monkeypatch):
    calls = []

    def fake_build_record(session, **kw):
        calls.append(kw)
        return types.SimpleNamespace(id=1, ticker=kw.get("ticker"), evidence_grade="C", serenity_score=0, recommended_action="hold")

    monkeypatch.setattr(cli, "build_record", fake_build_record)
    return calls


def test_research_patent_flag_builds_patent_reference(monkeypatch):
    calls = _capture_build_record(monkeypatch)
    rc = cli.main(["research", "--theme", "t", "--patent", "US6285999B1", "--claim", "node ranking", "--scorecard", "4,3,4,2,3"])
    assert rc == 0
    assert len(calls) == 1
    kw = calls[0]
    assert kw["fetch_missing"] is True  # patent body is not user-supplied → must be fetched
    assert kw["fetch_headers"] == {}  # Google Patents needs no User-Agent
    patent_refs = [r for r in kw["references"] if "patents.google.com" in r["source_url"]]
    assert patent_refs == [{"source_url": "https://patents.google.com/patent/US6285999B1/en", "claim_summary": "node ranking"}]


def test_research_url_only_stays_offline(monkeypatch):
    """No --patent → existing behavior preserved: offline (fetch_missing False, no headers)."""
    calls = _capture_build_record(monkeypatch)
    rc = cli.main([
        "research", "--theme", "t", "--url", "https://www.sec.gov/x",
        "--claim", "c", "--excerpt", "e", "--scorecard", "4,3,4,2,3",
    ])
    assert rc == 0
    kw = calls[0]
    assert kw["fetch_missing"] is False
    assert kw["fetch_headers"] is None
    assert kw["references"] == [{"source_url": "https://www.sec.gov/x", "claim_summary": "c", "excerpt": "e"}]


def test_research_invalid_patent_is_dropped(monkeypatch):
    """A malicious/invalid patent number is rejected by the adapter and never reaches references."""
    calls = _capture_build_record(monkeypatch)
    rc = cli.main(["research", "--theme", "t", "--patent", "US1234/../evil", "--claim", "c", "--scorecard", "4,3,4,2,3"])
    assert rc == 0
    assert all("patents.google.com" not in r["source_url"] for r in calls[0]["references"])


def test_research_multiple_patents(monkeypatch):
    calls = _capture_build_record(monkeypatch)
    cli.main([
        "research", "--theme", "t",
        "--patent", "US6285999B1", "--patent", "EP1234567B1",
        "--claim", "k", "--scorecard", "4,3,4,2,3",
    ])
    urls = [r["source_url"] for r in calls[0]["references"] if "patents.google.com" in r["source_url"]]
    assert urls == [
        "https://patents.google.com/patent/US6285999B1/en",
        "https://patents.google.com/patent/EP1234567B1/en",
    ]
