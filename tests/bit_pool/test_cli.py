"""Bit-pool CLI: exit-code contract, committee wiring, offline import, no-trade scan.

Exit codes are the automation contract (0 ok / 2 degraded / 1 error) — a cron job
that alerts on 2 would go blind if degradation exited 0. Committee wiring is proven
by monkeypatching ``scoring_graph.run_scoring_analysts`` and asserting the exact
keys + model/provider it receives; price fetch is stubbed at ``src.tools.api.get_prices``.
Nothing here touches the network.
"""

import json
import os
import pathlib
import subprocess
import sys

import pytest

import src.evals.suites.no_trade as nt
from src.bit_pool import cli
from src.data.models import Price
from src.evals.registry import build_suite
from src.evals.runner import run_case

_REPO = pathlib.Path(__file__).resolve().parents[2]
_HEADER = "ticker,shares,baseline_price,baseline_date,cost_basis\n"
_TRADES_HEADER = "date,ticker,side,shares,price,note\n"
_CORE = ["technical_analyst", "fundamentals_analyst", "growth_analyst", "sentiment_analyst", "valuation_analyst"]


def _p(close, time):
    return Price(open=close, close=close, high=close, low=close, volume=1, time=time)


@pytest.fixture
def prices(monkeypatch):
    """Stub the provider. Tickers listed in ``down`` raise; others get two closes."""
    import src.tools.api as api

    state = {"down": set(), "rows": {}}

    def fake_get_prices(ticker, start_date, end_date, api_key=None):
        if ticker in state["down"]:
            raise RuntimeError(f"{ticker} provider down")
        return state["rows"].get(ticker, [_p(100.0, "2026-01-08"), _p(101.0, "2026-01-09")])

    monkeypatch.setattr(api, "get_prices", fake_get_prices)
    return state


def _holdings(tmp_path, body="AAA,10,100,2026-01-02,\nBBB,5,50,2026-01-02,90\n"):
    p = tmp_path / "pool.csv"
    p.write_text(_HEADER + body, encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _no_ambient_trades_env(monkeypatch, tmp_path):
    # A developer's shell export must never leak a real ledger into these runs, and the
    # repo-anchored defaults must never resolve to the owner's REAL holdings/ledger.
    from src.bit_pool import holdings

    monkeypatch.delenv("BIT_POOL_TRADES_PATH", raising=False)
    monkeypatch.delenv("BIT_POOL_PATH", raising=False)
    monkeypatch.setattr(holdings, "DEFAULT_HOLDINGS_PATH", tmp_path / "anchored" / "bit_pool.csv")
    monkeypatch.setattr(holdings, "DEFAULT_TRADES_PATH", tmp_path / "anchored" / "bit_pool_trades.csv")


def _ledger(tmp_path, body="", name="trades.csv"):
    p = tmp_path / name
    p.write_text(_TRADES_HEADER + body, encoding="utf-8")
    return p


def _run(tmp_path, *extra, body=None):
    out = tmp_path / "out"
    holdings = _holdings(tmp_path) if body is None else _holdings(tmp_path, body)
    argv = ["run", "--date", "2026-01-09", "--holdings", str(holdings), "--trades", str(_ledger(tmp_path)), "--out", str(out), *extra]
    return cli.main(argv), out


def _json(out):
    return json.loads((out / "2026-01-09.json").read_text(encoding="utf-8"))


def test_ok_run_exits_0_and_writes_json_and_markdown(tmp_path, prices, capsys):
    code, out = _run(tmp_path)
    assert code == 0
    md_path = out / "2026-01-09.md"
    assert capsys.readouterr().out.strip() == str(md_path)
    data = json.loads((out / "2026-01-09.json").read_text(encoding="utf-8"))
    assert data["degraded"] == [] and data["committee"] is None
    assert "## 持仓总览 Pool summary" in md_path.read_text(encoding="utf-8")


def test_degraded_position_exits_2(tmp_path, prices):
    prices["down"].add("BBB")
    code, out = _run(tmp_path)
    assert code == 2
    assert json.loads((out / "2026-01-09.json").read_text(encoding="utf-8"))["degraded"] == ["BBB"]


def test_bad_csv_exits_1_with_stderr(tmp_path, prices, capsys):
    bad = tmp_path / "bad.csv"
    bad.write_text(_HEADER + "AAA,-1,100,2026-01-02,\n", encoding="utf-8")
    code = cli.main(["run", "--date", "2026-01-09", "--holdings", str(bad), "--out", str(tmp_path / "o")])
    assert code == 1
    assert "AAA" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()


def test_oversell_exits_1(tmp_path, prices, capsys):
    trades = tmp_path / "t.csv"
    trades.write_text("date,ticker,side,shares,price,note\n2026-01-05,AAA,sell,999,100,\n", encoding="utf-8")
    code = cli.main(["run", "--date", "2026-01-09", "--holdings", str(_holdings(tmp_path)), "--trades", str(trades), "--out", str(tmp_path / "o")])
    assert code == 1
    assert "AAA" in capsys.readouterr().err


def test_missing_holdings_file_exits_1(tmp_path, capsys):
    assert cli.main(["run", "--holdings", str(tmp_path / "nope.csv"), "--out", str(tmp_path / "o")]) == 1
    assert "nope.csv" in capsys.readouterr().err


def test_bad_date_exits_1_not_2(tmp_path, capsys):
    # 2 is reserved for "degraded"; a usage error must never masquerade as it.
    assert cli.main(["run", "--date", "09/01/2026", "--holdings", str(_holdings(tmp_path))]) == 1


def test_holdings_path_from_env(tmp_path, prices, monkeypatch):
    monkeypatch.setenv("BIT_POOL_PATH", str(_holdings(tmp_path)))
    code = cli.main(["run", "--date", "2026-01-09", "--trades", str(_ledger(tmp_path)), "--out", str(tmp_path / "o")])
    assert code == 0


def test_parser_defaults_contract():
    # No LLM provider/model default: the persona layer is the external Muse agent, and a
    # silent local-model default would make `--committee full` run something unasked.
    args = cli.build_parser().parse_args(["run"])
    assert (args.committee, args.provider, args.model, args.out, args.label, args.muse_signals) == ("none", None, None, "reports/bit_pool", "real", None)
    assert cli.COMMITTEE_CHOICES == ("none", "core", "full", "muse")


def test_no_ollama_default_left_in_bit_pool_sources():
    for path in (_REPO / "src" / "bit_pool").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "Ollama" not in text and "gemma4" not in text, path.name


def test_label_lands_in_json_book_and_markdown_title(tmp_path, prices):
    code, out = _run(tmp_path, "--label", "muse")
    assert code == 0
    data = json.loads((out / "2026-01-09.json").read_text(encoding="utf-8"))
    assert data["book"] == "muse"
    assert (data["committee_source"], data["committee_signals_as_of"]) == (None, None)
    assert "book: muse" in (out / "2026-01-09.md").read_text(encoding="utf-8").splitlines()[0]


@pytest.mark.parametrize("label", ["Muse", "", "a" * 33, "real book", "x/y"])
def test_invalid_label_exits_1(tmp_path, prices, capsys, label):
    code, out = _run(tmp_path, "--label", label)
    assert code == 1
    assert "label" in capsys.readouterr().err
    assert not out.exists()


# ── committee wiring ──
def _fake_scoring(calls, fail_for=()):
    def fake(tickers, selected_analysts, end_date, *, start_date=None, model_name="gpt-4.1", model_provider="OpenAI"):
        calls.append({"tickers": tickers, "keys": list(selected_analysts), "end": end_date, "model": model_name, "provider": model_provider})
        (ticker,) = tickers
        if ticker in fail_for:
            raise RuntimeError(f"{ticker} committee boom")
        return {f"{k}_agent": {ticker: {"signal": "bullish", "confidence": 80}} for k in selected_analysts}, {}

    return fake


def test_committee_core_uses_five_llm_free_keys_and_passes_model_provider(tmp_path, prices, monkeypatch):
    from src.observing_pools import scoring_graph

    calls: list = []
    monkeypatch.setattr(scoring_graph, "run_scoring_analysts", _fake_scoring(calls))
    code, out = _run(tmp_path, "--committee", "core", "--provider", "OpenAI", "--model", "gpt-x")
    assert code == 0
    assert cli.CORE_COMMITTEE == tuple(_CORE)
    assert [c["tickers"] for c in calls] == [["AAA"], ["BBB"]]
    assert all(c["keys"] == _CORE and c["model"] == "gpt-x" and c["provider"] == "OpenAI" and c["end"] == "2026-01-09" for c in calls)
    committee = json.loads((out / "2026-01-09.json").read_text(encoding="utf-8"))["committee"]
    assert committee[0]["band"] == "thesis-supportive"
    assert committee[0]["mean_score"] == 90.0  # bullish @80 -> (80+100)/2 = 90 for all five
    assert committee[0]["coverage"] == "5/5"
    assert committee[0]["analysts"]["technical_analyst"] == "bullish"


def test_committee_full_uses_committee_analyst_keys(tmp_path, prices, monkeypatch):
    from src.observing_pools import scoring_graph
    from src.observing_pools.agents_bridge import committee_analyst_keys

    calls: list = []
    monkeypatch.setattr(scoring_graph, "run_scoring_analysts", _fake_scoring(calls))
    code, out = _run(tmp_path, "--committee", "full", "--provider", "OpenAI", "--model", "gpt-x")
    assert code == 0
    assert calls[0]["keys"] == committee_analyst_keys()
    assert (calls[0]["model"], calls[0]["provider"]) == ("gpt-x", "OpenAI")
    assert json.loads((out / "2026-01-09.json").read_text(encoding="utf-8"))["committee_source"] == "full"


@pytest.mark.parametrize("extra", [(), ("--provider", "OpenAI"), ("--model", "gpt-x")])
def test_committee_full_without_provider_and_model_exits_1(tmp_path, prices, monkeypatch, capsys, extra):
    from src.observing_pools import scoring_graph

    calls: list = []
    monkeypatch.setattr(scoring_graph, "run_scoring_analysts", _fake_scoring(calls))
    code, out = _run(tmp_path, "--committee", "full", *extra)
    assert code == 1
    assert "full committee needs --provider and --model" in capsys.readouterr().err
    assert calls == [] and not out.exists()


def test_committee_core_needs_no_provider_or_model(tmp_path, prices, monkeypatch):
    from src.observing_pools import scoring_graph

    kwargs: list[dict] = []

    def fake(tickers, selected_analysts, end_date, **kw):
        kwargs.append(kw)
        (ticker,) = tickers
        return {f"{k}_agent": {ticker: {"signal": "bullish", "confidence": 80}} for k in selected_analysts}, {}

    monkeypatch.setattr(scoring_graph, "run_scoring_analysts", fake)
    code, _ = _run(tmp_path, "--committee", "core")
    assert code == 0 and len(kwargs) == 2
    # Nothing forced through: the runner's own defaults apply (core analysts are LLM-free).
    # Checked on the kwargs actually passed — a fake's own defaults would mask a forced value.
    assert all("model_name" not in kw and "model_provider" not in kw for kw in kwargs)


def test_committee_exception_degrades_one_ticker_without_aborting(tmp_path, prices, monkeypatch):
    from src.observing_pools import scoring_graph

    calls: list = []
    monkeypatch.setattr(scoring_graph, "run_scoring_analysts", _fake_scoring(calls, fail_for={"AAA"}))
    code, out = _run(tmp_path, "--committee", "core")
    assert code == 2
    assert len(calls) == 2  # BBB still ran after AAA blew up
    committee = {c["ticker"]: c for c in json.loads((out / "2026-01-09.json").read_text(encoding="utf-8"))["committee"]}
    assert "AAA committee boom" in committee["AAA"]["degraded_reason"]
    assert committee["BBB"]["degraded_reason"] is None


# ── committee=muse (external Muse agent's signals file) ──
def _muse_file(path, as_of="2026-01-09", signals=None):
    from src.observing_pools.agents_bridge import committee_analyst_keys

    if signals is None:
        keys = committee_analyst_keys()[:5]
        signals = {k: {"AAA": {"signal": "bullish", "confidence": 90}, "BBB": {"signal": "bearish", "confidence": 85}} for k in keys}
    path.write_text(json.dumps({"as_of": as_of, "signals": signals}), encoding="utf-8")
    return path


def _run_muse(tmp_path, *extra, body="AAA,10,100,2026-01-02,\nBBB,5,50,2026-01-02,90\nCCC,1,10,2026-01-02,\n"):
    out = tmp_path / "out"
    argv = ["run", "--date", "2026-01-09", "--holdings", str(_holdings(tmp_path, body)), "--trades", str(_ledger(tmp_path)), "--out", str(out), "--committee", "muse", *extra]
    code = cli.main(argv)
    data = json.loads((out / "2026-01-09.json").read_text(encoding="utf-8")) if (out / "2026-01-09.json").exists() else None
    return code, data, out


def test_muse_committee_end_to_end_bands_and_absent_ticker(tmp_path, prices):
    code, data, out = _run_muse(tmp_path, "--muse-signals", str(_muse_file(tmp_path / "sig.json")), "--label", "muse")
    rows = {c["ticker"]: c for c in data["committee"]}
    assert rows["AAA"]["band"] == "thesis-supportive" and rows["AAA"]["degraded_reason"] is None
    assert rows["BBB"]["band"] == "thesis-challenging"
    # Muse said nothing about CCC: absent, not scored zero -> insufficient evidence, degraded.
    assert rows["CCC"]["band"] is None and "insufficient evidence" in rows["CCC"]["degraded_reason"]
    assert (rows["AAA"]["coverage"], rows["CCC"]["coverage"]) == ("5/5", "0/5")
    assert code == 2
    assert (data["book"], data["committee_source"], data["committee_signals_as_of"]) == ("muse", "muse", "2026-01-09")
    assert "Committee: Muse agent signals as of 2026-01-09" in (out / "2026-01-09.md").read_text(encoding="utf-8")


def test_muse_committee_all_present_exits_0_and_reads_dir_from_env(tmp_path, prices, monkeypatch):
    sig_dir = tmp_path / "signals"
    sig_dir.mkdir()
    _muse_file(sig_dir / "2026-01-05.json", as_of="2026-01-05")  # exactly 4 days old: still fresh
    _muse_file(sig_dir / "2026-01-10.json", as_of="2026-01-10", signals={})  # after the run date: ignored
    monkeypatch.setenv("MUSE_SIGNALS_PATH", str(sig_dir))
    code, data, _ = _run_muse(tmp_path, body="AAA,10,100,2026-01-02,\nBBB,5,50,2026-01-02,90\n")
    assert code == 0
    assert data["committee_signals_as_of"] == "2026-01-05"
    assert [c["band"] for c in data["committee"]] == ["thesis-supportive", "thesis-challenging"]


def test_muse_stale_signals_degrade_every_row_and_exit_1(tmp_path, prices):
    # Every committee row degraded is systemic (Muse did not deliver), not a per-ticker blip.
    code, data, _ = _run_muse(tmp_path, "--muse-signals", str(_muse_file(tmp_path / "s.json", as_of="2026-01-04")))
    assert code == 1
    assert all(c["band"] is None and "stale" in c["degraded_reason"] and "2026-01-04" in c["degraded_reason"] for c in data["committee"])
    assert len(data["committee"]) == 3


def test_muse_dir_without_qualifying_file_degrades_every_row(tmp_path, prices):
    sig_dir = tmp_path / "signals"
    sig_dir.mkdir()
    _muse_file(sig_dir / "2026-01-10.json", as_of="2026-01-10")
    code, data, _ = _run_muse(tmp_path, "--muse-signals", str(sig_dir))
    assert code == 1
    assert all(c["degraded_reason"] == "no Muse signals on or before 2026-01-09" for c in data["committee"])
    assert data["committee_signals_as_of"] is None


def test_muse_future_dated_file_exits_1(tmp_path, prices, capsys):
    code, data, _ = _run_muse(tmp_path, "--muse-signals", str(_muse_file(tmp_path / "s.json", as_of="2026-01-10")))
    assert code == 1 and data is None
    assert "2026-01-10" in capsys.readouterr().err


def test_muse_bad_file_content_exits_1(tmp_path, prices, capsys):
    bad = _muse_file(tmp_path / "s.json", signals={"warren_buffett": {"AAA": {"signal": "buy", "confidence": 50}}})
    code, _, _ = _run_muse(tmp_path, "--muse-signals", str(bad))
    assert code == 1
    assert "AAA" in capsys.readouterr().err


def test_muse_dir_file_with_internal_as_of_after_run_date_exits_1(tmp_path, prices, capsys):
    # The filename picks the file, but the content's own as_of is authoritative: a file
    # named for the run date that claims a later as_of is inconsistent -> error, not data.
    sig_dir = tmp_path / "signals"
    sig_dir.mkdir()
    _muse_file(sig_dir / "2026-01-09.json", as_of="2026-01-12")
    code, data, _ = _run_muse(tmp_path, "--muse-signals", str(sig_dir))
    assert code == 1 and data is None
    assert "2026-01-12" in capsys.readouterr().err


def test_muse_dir_file_with_much_older_internal_as_of_is_stale(tmp_path, prices):
    # A fresh filename must not launder old content: staleness follows the internal as_of.
    sig_dir = tmp_path / "signals"
    sig_dir.mkdir()
    _muse_file(sig_dir / "2026-01-09.json", as_of="2025-12-20")
    code, data, _ = _run_muse(tmp_path, "--muse-signals", str(sig_dir))
    assert code == 1
    assert all(c["band"] is None and "stale" in c["degraded_reason"] and "2025-12-20" in c["degraded_reason"] for c in data["committee"])
    assert data["committee_signals_as_of"] == "2025-12-20"


def test_muse_without_signals_path_exits_1(tmp_path, prices, monkeypatch, capsys):
    monkeypatch.delenv("MUSE_SIGNALS_PATH", raising=False)
    code, data, _ = _run_muse(tmp_path)
    assert code == 1 and data is None
    assert "--muse-signals" in capsys.readouterr().err


# ── offline import + --help ──
def _subprocess(code_or_args):
    env = {**os.environ, "PYTHONPATH": str(_REPO)}
    return subprocess.run([sys.executable, *code_or_args], cwd=_REPO, env=env, capture_output=True, text=True, timeout=120)


def test_import_is_light_and_offline():
    probe = "import sys, src.bit_pool, src.bit_pool.cli; heavy=[m for m in ('src.tools.api','src.observing_pools.scoring_graph','langgraph','src.monitoring.committee_flow') if m in sys.modules]; print(heavy)"
    res = _subprocess(["-c", probe])
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "[]"


def test_module_help_works():
    res = _subprocess(["-m", "src.bit_pool", "--help"])
    assert res.returncode == 0, res.stderr
    assert "run" in res.stdout
    res = _subprocess(["-m", "src.bit_pool", "run", "--help"])
    assert res.returncode == 0 and "--committee" in res.stdout


# ── no-trade AST scan covers src/bit_pool ──
def _no_trade_case():
    return next(c for c in build_suite("no_trade") if c.case_id == "modules_have_no_direct_trade_imports")


def test_no_trade_scan_lists_every_bit_pool_module():
    pkg = _REPO / "src" / "bit_pool"
    modules = {p.stem for p in pkg.glob("*.py")} - {"__init__", "__main__"}
    assert set(nt._SCANNED_BIT_POOL_MODULES) == modules
    assert nt._forbidden_imports_in(pkg, nt._SCANNED_BIT_POOL_MODULES) is None


def test_no_trade_scan_bites_on_bit_pool_imports(monkeypatch):
    # `src.bit_pool` occurs only in bit_pool's own imports (cli -> src.bit_pool.*), so
    # treating it as forbidden must flip the suite to FAIL via the bit_pool scan.
    monkeypatch.setattr(nt, "_FORBIDDEN_IMPORT_SUBSTRINGS", ("src.bit_pool",))
    result, _ = run_case(_no_trade_case())
    assert result.passed is False


def test_planted_trade_import_would_be_flagged(tmp_path):
    (tmp_path / "planted.py").write_text("from src.agents.portfolio_manager import portfolio_management_agent\n", encoding="utf-8")
    hit = nt._forbidden_imports_in(tmp_path, ("planted",))
    assert hit is not None and hit[0] == "planted"


# ── A: trades ledger path ──
def test_explicit_missing_trades_file_exits_1_and_writes_nothing(tmp_path, prices, capsys):
    missing = tmp_path / "typo.csv"
    code = cli.main(["run", "--date", "2026-01-09", "--holdings", str(_holdings(tmp_path)), "--trades", str(missing), "--out", str(tmp_path / "o")])
    assert code == 1
    assert f"trades ledger not found: {missing}" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()


def test_implicit_default_trades_may_be_absent(tmp_path, prices, monkeypatch):
    monkeypatch.chdir(tmp_path)  # anchored default (autouse fixture) does not exist
    code = cli.main(["run", "--date", "2026-01-09", "--holdings", str(_holdings(tmp_path)), "--out", str(tmp_path / "o")])
    assert code == 0
    data = _json(tmp_path / "o")
    assert (data["trades_source"], data["trades_applied"]) == (None, 0)
    assert "Trades applied: 0 (no ledger)" in (tmp_path / "o" / "2026-01-09.md").read_text(encoding="utf-8")


def test_implicit_default_trades_used_when_present(tmp_path, prices, monkeypatch):
    from src.bit_pool import holdings

    (tmp_path / "anchored").mkdir()
    anchored = _ledger(tmp_path / "anchored", "2026-01-05,AAA,buy,1,100,\n", name="bit_pool_trades.csv")
    assert holdings.DEFAULT_TRADES_PATH == anchored
    assert cli.main(["run", "--date", "2026-01-09", "--holdings", str(_holdings(tmp_path)), "--out", str(tmp_path / "o")]) == 0
    data = _json(tmp_path / "o")
    assert (data["trades_source"], data["trades_applied"]) == (str(anchored), 1)


def test_implicit_defaults_ignore_the_cwd(tmp_path, prices, monkeypatch):
    # cron starts in $HOME: a CWD-relative default would read the wrong (or no) ledger
    # and silently drop fills. A decoy CWD-relative ledger must be ignored.
    from src.bit_pool import holdings

    cwd = tmp_path / "cron_home"
    (cwd / "data" / "pools").mkdir(parents=True)
    _ledger(cwd / "data" / "pools", "2026-01-05,AAA,sell,999,100,\n", name="bit_pool_trades.csv")  # would oversell -> exit 1
    _holdings(cwd / "data" / "pools", "ZZZ,1,10,2026-01-02,\n").rename(cwd / "data" / "pools" / "bit_pool.csv")
    monkeypatch.chdir(cwd)
    (tmp_path / "anchored").mkdir()
    _holdings(tmp_path / "anchored").rename(holdings.DEFAULT_HOLDINGS_PATH)
    _ledger(tmp_path / "anchored", "2026-01-05,AAA,buy,2,100,\n", name="bit_pool_trades.csv")
    out = tmp_path / "o"
    assert cli.main(["run", "--date", "2026-01-09", "--out", str(out)]) == 0
    data = _json(out)
    assert {p["ticker"]: p["shares"] for p in data["positions"]} == {"AAA": 12.0, "BBB": 5.0}
    assert (data["trades_source"], data["trades_applied"]) == (str(holdings.DEFAULT_TRADES_PATH), 1)


def test_trades_path_from_env_is_honoured_and_counts_only_rows_through_as_of(tmp_path, prices, monkeypatch):
    ledger = _ledger(tmp_path, "2026-01-05,AAA,buy,2,100,\n2026-01-20,AAA,sell,1,100,\n", name="env_trades.csv")
    monkeypatch.setenv("BIT_POOL_TRADES_PATH", str(ledger))
    out = tmp_path / "o"
    assert cli.main(["run", "--date", "2026-01-09", "--holdings", str(_holdings(tmp_path)), "--out", str(out)]) == 0
    data = _json(out)
    assert (data["trades_source"], data["trades_applied"]) == (str(ledger), 1)
    assert {p["ticker"]: p["shares"] for p in data["positions"]}["AAA"] == 12.0
    assert f"Trades applied: 1 ({ledger})" in (out / "2026-01-09.md").read_text(encoding="utf-8")


def test_env_trades_path_that_does_not_exist_exits_1(tmp_path, prices, monkeypatch, capsys):
    monkeypatch.setenv("BIT_POOL_TRADES_PATH", str(tmp_path / "moved.csv"))
    assert cli.main(["run", "--date", "2026-01-09", "--holdings", str(_holdings(tmp_path)), "--out", str(tmp_path / "o")]) == 1
    assert "trades ledger not found" in capsys.readouterr().err


# ── E: freshness ──
def test_stale_close_exits_2(tmp_path, prices):
    prices["rows"]["AAA"] = [_p(100.0, "2025-12-30"), _p(100.0, "2025-12-31")]
    code, out = _run(tmp_path, body="AAA,10,100,2026-01-02,\n")
    assert code == 2
    assert {"ticker": "AAA", "alert": "STALE"} in _json(out)["alerts"]
    assert _json(out)["degraded"] == []


def test_future_date_exits_1(tmp_path, prices, capsys):
    code = cli.main(["run", "--date", "2999-01-01", "--holdings", str(_holdings(tmp_path)), "--trades", str(_ledger(tmp_path)), "--out", str(tmp_path / "o")])
    assert code == 1
    assert "future" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()


def test_markdown_shows_prices_as_of_and_close_date(tmp_path, prices):
    code, out = _run(tmp_path)
    md = (out / "2026-01-09.md").read_text(encoding="utf-8")
    assert code == 0 and "Prices as of: 2026-01-09" in md and "Close date 收盘日" in md
    assert _json(out)["prices_as_of"] == "2026-01-09"


# ── F: partial / all-degraded pools ──
def test_partial_pool_flagged_in_json(tmp_path, prices):
    prices["down"].add("BBB")
    code, out = _run(tmp_path)
    assert code == 2
    assert (_json(out)["partial"], _json(out)["excluded_tickers"]) == (True, ["BBB"])
    assert "PARTIAL — excludes BBB" in (out / "2026-01-09.md").read_text(encoding="utf-8")


def test_every_position_degraded_is_systemic_exit_1_with_null_totals(tmp_path, prices, capsys):
    prices["down"].update({"AAA", "BBB"})
    code, out = _run(tmp_path)
    assert code == 1
    assert "every position degraded" in capsys.readouterr().err
    data = _json(out)
    assert (data["total_value"], data["prev_total_value"], data["baseline_value"], data["daily_pct"], data["vs_baseline_pct"]) == (None,) * 5
    assert "Total value: n/a" in (out / "2026-01-09.md").read_text(encoding="utf-8")


# ── G: committee coverage + degraded analysts ──
def _fake_scoring_partial(bad_keys):
    def fake(tickers, selected_analysts, end_date, **_kw):
        (ticker,) = tickers
        return {f"{k}_agent": {ticker: {"signal": "garbage" if k in bad_keys else "bullish", "confidence": 80}} for k in selected_analysts}, {}

    return fake


def test_degraded_analysts_excluded_from_mean_and_visibly_marked(tmp_path, prices, monkeypatch):
    from src.observing_pools import scoring_graph

    monkeypatch.setattr(scoring_graph, "run_scoring_analysts", _fake_scoring_partial({"growth_analyst", "sentiment_analyst"}))
    code, out = _run(tmp_path, "--committee", "core")
    assert code == 0  # partial analyst degradation keeps the band
    row = _json(out)["committee"][0]
    # 3 valid bullish @80 -> 90 each; counting the 2 degraded (scored 50) would give 74.
    assert row["mean_score"] == 90.0
    assert row["coverage"] == "3/5"
    assert row["analysts"]["growth_analyst"] == {"signal": "neutral", "degraded": True}
    assert row["analysts"]["technical_analyst"] == "bullish"
    md = (out / "2026-01-09.md").read_text(encoding="utf-8")
    assert "growth_analyst: neutral (DEGRADED)" in md and "| 3/5 |" in md


def test_every_ticker_committee_failing_exits_1(tmp_path, prices, monkeypatch, capsys):
    from src.observing_pools import scoring_graph

    monkeypatch.setattr(scoring_graph, "run_scoring_analysts", _fake_scoring([], fail_for={"AAA", "BBB"}))
    code, out = _run(tmp_path, "--committee", "core")
    assert code == 1
    assert "every committee ticker degraded" in capsys.readouterr().err
    assert all(c["coverage"] == "0/5" for c in _json(out)["committee"])


def test_muse_file_with_empty_signals_degrades_every_row_with_specific_reason(tmp_path, prices):
    code, data, _ = _run_muse(tmp_path, "--muse-signals", str(_muse_file(tmp_path / "s.json", signals={})))
    assert code == 1
    assert [c["degraded_reason"] for c in data["committee"]] == ["Muse signals file has no signals"] * 3


# ── I: atomic writes + book guard ──
def test_reports_render_fully_before_any_write(tmp_path, prices, monkeypatch):
    def boom(*_a, **_kw):
        raise ValueError("render failed")

    monkeypatch.setattr(cli, "render_markdown", boom)
    code, out = _run(tmp_path)
    assert code == 1
    assert not (out / "2026-01-09.json").exists()  # no half-written report pair


def test_reports_are_written_via_tmp_and_os_replace(tmp_path, prices, monkeypatch):
    calls = []
    real = os.replace

    def spy(src, dst):
        calls.append((pathlib.Path(src).name, pathlib.Path(dst).name))
        return real(src, dst)

    monkeypatch.setattr(cli.os, "replace", spy)
    code, out = _run(tmp_path)
    assert code == 0
    assert calls == [("2026-01-09.json.tmp", "2026-01-09.json"), ("2026-01-09.md.tmp", "2026-01-09.md")]
    assert sorted(p.name for p in out.iterdir()) == ["2026-01-09.json", "2026-01-09.md"]


def test_refuses_to_overwrite_a_different_book_and_writes_nothing(tmp_path, prices, capsys):
    out = tmp_path / "out"
    out.mkdir()
    existing = out / "2026-01-09.json"
    existing.write_text(json.dumps({"book": "muse"}), encoding="utf-8")
    code, _ = _run(tmp_path, "--label", "real")
    assert code == 1
    assert "refusing to overwrite book muse with book real" in capsys.readouterr().err
    assert json.loads(existing.read_text(encoding="utf-8")) == {"book": "muse"}
    assert not (out / "2026-01-09.md").exists()


def test_same_book_rerun_overwrites(tmp_path, prices):
    assert _run(tmp_path, "--label", "muse")[0] == 0
    code, out = _run(tmp_path, "--label", "muse")
    assert code == 0 and _json(out)["book"] == "muse"


# ── J: a sell of an unheld ticker is a clean error ──
def test_tiny_sell_of_unheld_ticker_exits_1(tmp_path, prices, capsys):
    trades = _ledger(tmp_path, "2026-01-05,ZZZ,sell,0.0000000001,100,\n", name="t.csv")
    code = cli.main(["run", "--date", "2026-01-09", "--holdings", str(_holdings(tmp_path)), "--trades", str(trades), "--out", str(tmp_path / "o")])
    assert code == 1
    err = capsys.readouterr().err
    assert "oversell" in err and "ZZZ" in err


# ── equity policy alerts: --max-weight / OVER_WEIGHT and NEGATIVE_CASH (never exit codes) ──
def test_max_weight_defaults_to_off(tmp_path, prices):
    assert cli.build_parser().parse_args(["run"]).max_weight is None
    code, out = _run(tmp_path)
    assert code == 0
    assert not any(a["alert"] == "OVER_WEIGHT" for a in _json(out)["alerts"])


def test_max_weight_flags_over_weight_without_changing_exit_code(tmp_path, prices):
    # AAA 10*101=1010, BBB 5*101=505 → equity 1515; AAA 66.7%, BBB 33.3%.
    code, out = _run(tmp_path, "--max-weight", "50")
    assert code == 0  # policy alert only
    data = _json(out)
    assert [a["ticker"] for a in data["alerts"] if a["alert"] == "OVER_WEIGHT"] == ["AAA"]
    assert data["equity"] == pytest.approx(1515.0) and data["cash"] == 0.0
    assert data["initial_capital"] == pytest.approx(10 * 100.0 + 5 * 50.0)
    assert "AAA: OVER_WEIGHT" in (out / "2026-01-09.md").read_text(encoding="utf-8")


def test_unfunded_buy_raises_negative_cash_without_changing_exit_code(tmp_path, prices):
    out = tmp_path / "out"
    ledger = _ledger(tmp_path, "2026-01-05,AAA,buy,1,100,\n")
    code = cli.main(["run", "--date", "2026-01-09", "--holdings", str(_holdings(tmp_path)), "--trades", str(ledger), "--out", str(out)])
    assert code == 0
    data = _json(out)
    assert {"ticker": "(pool)", "alert": "NEGATIVE_CASH"} in data["alerts"]
    assert data["cash"] == pytest.approx(-100.0)
    assert "- (pool): NEGATIVE_CASH" in (out / "2026-01-09.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("bad", ["0", "-5", "101", "abc", "nan"])
def test_invalid_max_weight_exits_1(tmp_path, prices, capsys, bad):
    code, out = _run(tmp_path, "--max-weight", bad)
    assert code == 1
    err = capsys.readouterr().err
    assert "max-weight" in err and "unrecognized" not in err  # rejected as a bad value, not an unknown flag
    assert not out.exists()
