"""Phase 7b: the `serenity discover` CLI wiring. OFFLINE — gather + build_record + the DB
session are stubbed. Verifies the command builds ONE record per non-empty source group, each
with that group's scoped fetch_headers and fetch_missing=True, and degrades cleanly when no
evidence is found.
"""

import contextlib
import types

import pytest

from src.serenity import cli
from src.serenity.adapters.gather import GatherResult


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    monkeypatch.setattr(cli.Base.metadata, "create_all", lambda **k: None)
    monkeypatch.setattr(cli, "session_scope", lambda: contextlib.nullcontext(object()))


def _argv(**over):
    base = {"theme": "chip supply", "ticker": "NVDA", "keywords": "cowos,packaging", "scorecard": "4,3,4,2,3"}
    base.update(over)
    argv = ["discover"]
    for k, v in base.items():
        argv += [f"--{k.replace('_', '-')}", v]
    return argv


def test_discover_builds_one_record_per_group_with_scoped_headers(monkeypatch):
    calls = []

    def fake_build_record(session, **kw):
        calls.append(kw)
        return types.SimpleNamespace(id=len(calls), ticker=kw.get("ticker"), evidence_grade="B", serenity_score=70)

    edgar_group = ({"User-Agent": "edgar-ua"}, [{"source_url": "https://www.sec.gov/x", "claim_summary": "c"}])
    fr_group = ({"User-Agent": "fr-ua"}, [{"source_url": "https://www.federalregister.gov/y", "claim_summary": "c"}])
    monkeypatch.setattr(cli, "build_record", fake_build_record)
    monkeypatch.setattr(
        cli, "gather_references",
        lambda ticker, **k: GatherResult(
            references=edgar_group[1] + fr_group[1],
            headers_by_source={"edgar": edgar_group[0], "federal_register": fr_group[0]},
            groups=[edgar_group, fr_group],
        ),
    )
    rc = cli.main(_argv())
    assert rc == 0
    assert len(calls) == 2
    assert all(c["fetch_missing"] is True for c in calls)
    assert calls[0]["fetch_headers"] == {"User-Agent": "edgar-ua"}
    assert calls[1]["fetch_headers"] == {"User-Agent": "fr-ua"}
    assert calls[0]["ticker"] == "NVDA"


def test_discover_skips_empty_groups(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "build_record", lambda session, **kw: calls.append(kw) or types.SimpleNamespace(id=1, ticker="NVDA", evidence_grade="C", serenity_score=0))
    populated = ({"User-Agent": "fr-ua"}, [{"source_url": "https://www.federalregister.gov/y", "claim_summary": "c"}])
    monkeypatch.setattr(
        cli, "gather_references",
        lambda ticker, **k: GatherResult(
            references=populated[1],
            headers_by_source={"edgar": {}, "federal_register": populated[0]},
            groups=[({"User-Agent": "edgar-ua"}, []), populated],  # edgar group empty
        ),
    )
    assert cli.main(_argv()) == 0
    assert len(calls) == 1  # only the non-empty fedreg group built a record


def test_discover_no_evidence_returns_zero_without_building(monkeypatch):
    def must_not_build(*a, **k):
        raise AssertionError("build_record must not be called when there is no evidence")

    monkeypatch.setattr(cli, "build_record", must_not_build)
    monkeypatch.setattr(cli, "gather_references", lambda ticker, **k: GatherResult(references=[], headers_by_source={}, groups=[]))
    assert cli.main(_argv()) == 0


def test_discover_all_sources_errored_exits_nonzero(monkeypatch):
    """'every source errored' must NOT look like 'no evidence' — exit non-zero so a pipeline
    can tell broken from empty."""
    def must_not_build(*a, **k):
        raise AssertionError("build_record must not be called when all sources errored")

    monkeypatch.setattr(cli, "build_record", must_not_build)
    monkeypatch.setattr(
        cli, "gather_references",
        lambda ticker, **k: GatherResult(
            references=[], headers_by_source={"edgar": {}, "federal_register": {}}, groups=[],
            errors={"edgar": "RuntimeError", "federal_register": "ValueError"},
            counts={"edgar": 0, "federal_register": 0},
        ),
    )
    assert cli.main(_argv()) == 2


def test_discover_one_group_failing_preserves_others(monkeypatch):
    """A later group's build_record raising must not roll back / hide earlier records."""
    calls = []

    def flaky_build_record(session, **kw):
        calls.append(kw)
        if len(calls) == 2:
            raise RuntimeError("second group blew up")
        return types.SimpleNamespace(id=len(calls), ticker="NVDA", evidence_grade="B", serenity_score=70)

    g1 = ({"User-Agent": "edgar-ua"}, [{"source_url": "https://www.sec.gov/x", "claim_summary": "c"}])
    g2 = ({"User-Agent": "fr-ua"}, [{"source_url": "https://www.federalregister.gov/y", "claim_summary": "c"}])
    monkeypatch.setattr(cli, "build_record", flaky_build_record)
    monkeypatch.setattr(
        cli, "gather_references",
        lambda ticker, **k: GatherResult(references=g1[1] + g2[1], headers_by_source={}, groups=[g1, g2]),
    )
    rc = cli.main(_argv())
    assert rc == 0  # first group persisted, so not a total failure
    assert len(calls) == 2  # both attempted; the second failed but didn't abort the first
