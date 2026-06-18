"""Phase 7b: multi-source gatherer. OFFLINE — the per-source reference builders are stubbed.
Tests encode WHY: (1) sources are fanned and merged with per-source header groups, (2) dedup
collapses repeats by normalized URL, (3) a buggy adapter cannot leak an off-allowlist URL,
(4) one source failing never sinks the others (totality), (5) headers stay scoped per source.
"""

from src.serenity.adapters import edgar, federal_register
from src.serenity.adapters.gather import gather_references, GatherResult


def _edgar_ref(path="x"):
    return {"source_url": f"https://www.sec.gov/Archives/edgar/data/320193/000/{path}.htm", "claim_summary": "c"}


def _fr_ref(path="y"):
    return {"source_url": f"https://www.federalregister.gov/documents/2026/{path}", "claim_summary": "c"}


def _wire(monkeypatch, *, edgar_refs, fr_refs):
    monkeypatch.setattr(edgar, "build_edgar_references", lambda ticker, **k: list(edgar_refs))
    monkeypatch.setattr(federal_register, "build_federal_register_references", lambda term, **k: list(fr_refs))


def test_fans_both_sources_with_scoped_headers(monkeypatch):
    _wire(monkeypatch, edgar_refs=[_edgar_ref()], fr_refs=[_fr_ref()])
    res = gather_references("NVDA", keywords=["cowos", "packaging"])
    assert isinstance(res, GatherResult)
    assert len(res.references) == 2
    # groups preserve per-source header scoping: EDGAR carries its UA, FedReg carries its own
    edgar_headers, edgar_group = res.groups[0]
    fr_headers, fr_group = res.groups[1]
    assert "User-Agent" in edgar_headers and edgar_group == [_edgar_ref()]
    assert "User-Agent" in fr_headers and fr_group == [_fr_ref()]
    assert res.headers_by_source["edgar"] == edgar_headers


def test_dedup_collapses_repeats_across_and_within_sources(monkeypatch):
    dup = _edgar_ref("same")
    # same URL surfaced twice within edgar, plus a trailing-slash variant
    slash_variant = {**dup, "source_url": dup["source_url"] + "/"}
    _wire(monkeypatch, edgar_refs=[dup, dup, slash_variant], fr_refs=[_fr_ref()])
    res = gather_references("NVDA", keywords=["k"])
    edgar_urls = [r["source_url"] for r in res.groups[0][1]]
    assert edgar_urls == [dup["source_url"]]  # only first-seen kept
    assert len(res.references) == 2  # one edgar + one fedreg


def test_off_allowlist_url_from_buggy_adapter_is_dropped(monkeypatch):
    leaky = {"source_url": "https://evil.example.com/x", "claim_summary": "c"}
    _wire(monkeypatch, edgar_refs=[leaky, _edgar_ref()], fr_refs=[])
    res = gather_references("NVDA", keywords=["k"])
    urls = [r["source_url"] for r in res.references]
    assert "https://evil.example.com/x" not in urls
    assert _edgar_ref()["source_url"] in urls


def test_one_source_failing_does_not_sink_others(monkeypatch):
    def boom(ticker, **k):
        raise RuntimeError("edgar exploded")

    monkeypatch.setattr(edgar, "build_edgar_references", boom)
    monkeypatch.setattr(federal_register, "build_federal_register_references", lambda term, **k: [_fr_ref()])
    res = gather_references("NVDA", keywords=["k"])  # must not raise
    assert [r["source_url"] for r in res.references] == [_fr_ref()["source_url"]]
    assert res.groups[0][1] == []  # edgar group empty, fedreg group populated
    assert res.groups[1][1] == [_fr_ref()]
    # the failure is CARRIED (not swallowed) so the producer can tell 'broken' from 'empty'
    assert res.errors == {"edgar": "RuntimeError"}
    assert res.counts == {"edgar": 0, "federal_register": 1}


def test_source_returning_none_tolerated(monkeypatch):
    monkeypatch.setattr(edgar, "build_edgar_references", lambda ticker, **k: None)
    monkeypatch.setattr(federal_register, "build_federal_register_references", lambda term, **k: [_fr_ref()])
    res = gather_references("NVDA", keywords=["k"])  # `refs or []` guards None
    assert [r["source_url"] for r in res.references] == [_fr_ref()["source_url"]]


def test_non_int_max_per_source_does_not_raise(monkeypatch):
    _wire(monkeypatch, edgar_refs=[_edgar_ref()], fr_refs=[_fr_ref()])
    res = gather_references("NVDA", keywords=["k"], max_per_source="bad")  # gather forwards; adapters coerce
    assert isinstance(res, GatherResult)


def test_userinfo_url_dropped(monkeypatch):
    """A userinfo-bearing URL from a buggy adapter is dropped (consistent with the fetcher,
    which rejects '@'), even though its host is on the allowlist."""
    sneaky = {"source_url": "https://evil@www.sec.gov/x", "claim_summary": "c"}
    _wire(monkeypatch, edgar_refs=[sneaky, _edgar_ref()], fr_refs=[])
    res = gather_references("NVDA", keywords=["k"])
    assert [r["source_url"] for r in res.references] == [_edgar_ref()["source_url"]]


def test_unknown_source_skipped(monkeypatch):
    _wire(monkeypatch, edgar_refs=[_edgar_ref()], fr_refs=[_fr_ref()])
    res = gather_references("NVDA", keywords=["k"], sources=("edgar", "bogus"))
    assert len(res.references) == 1
    assert "bogus" not in res.headers_by_source


def test_subset_of_sources_only_runs_requested(monkeypatch):
    called = {"edgar": False, "fr": False}

    def edgar_b(ticker, **k):
        called["edgar"] = True
        return [_edgar_ref()]

    def fr_b(term, **k):
        called["fr"] = True
        return [_fr_ref()]

    monkeypatch.setattr(edgar, "build_edgar_references", edgar_b)
    monkeypatch.setattr(federal_register, "build_federal_register_references", fr_b)
    gather_references("NVDA", keywords=["k"], sources=("federal_register",))
    assert called["fr"] is True and called["edgar"] is False
