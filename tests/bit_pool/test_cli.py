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
_CORE = ["technical_analyst", "fundamentals_analyst", "growth_analyst", "sentiment_analyst", "valuation_analyst"]


def _p(close, time):
    return Price(open=close, close=close, high=close, low=close, volume=1, time=time)


@pytest.fixture
def prices(monkeypatch):
    """Stub the provider. Tickers listed in ``down`` raise; others get two closes."""
    import src.tools.api as api

    state = {"down": set()}

    def fake_get_prices(ticker, start_date, end_date, api_key=None):
        if ticker in state["down"]:
            raise RuntimeError(f"{ticker} provider down")
        return [_p(100.0, "2026-01-08"), _p(101.0, "2026-01-09")]

    monkeypatch.setattr(api, "get_prices", fake_get_prices)
    return state


def _holdings(tmp_path, body="AAA,10,100,2026-01-02,\nBBB,5,50,2026-01-02,90\n"):
    p = tmp_path / "pool.csv"
    p.write_text(_HEADER + body, encoding="utf-8")
    return p


def _run(tmp_path, *extra):
    out = tmp_path / "out"
    argv = ["run", "--date", "2026-01-09", "--holdings", str(_holdings(tmp_path)), "--trades", str(tmp_path / "none.csv"), "--out", str(out), *extra]
    return cli.main(argv), out


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
    code = cli.main(["run", "--date", "2026-01-09", "--trades", str(tmp_path / "none.csv"), "--out", str(tmp_path / "o")])
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
    assert committee[0]["mean_score"] >= 60.0  # all-bullish -> supportive band
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

    calls: list = []
    monkeypatch.setattr(scoring_graph, "run_scoring_analysts", _fake_scoring(calls))
    code, _ = _run(tmp_path, "--committee", "core")
    assert code == 0
    # Nothing forced through: the runner's own defaults apply (core analysts are LLM-free).
    assert all((c["model"], c["provider"]) == ("gpt-4.1", "OpenAI") for c in calls)


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
    argv = ["run", "--date", "2026-01-09", "--holdings", str(_holdings(tmp_path, body)), "--trades", str(tmp_path / "none.csv"), "--out", str(out), "--committee", "muse", *extra]
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


def test_muse_stale_signals_degrade_every_row_and_exit_2(tmp_path, prices):
    code, data, _ = _run_muse(tmp_path, "--muse-signals", str(_muse_file(tmp_path / "s.json", as_of="2026-01-04")))
    assert code == 2
    assert all(c["band"] is None and "stale" in c["degraded_reason"] and "2026-01-04" in c["degraded_reason"] for c in data["committee"])
    assert len(data["committee"]) == 3


def test_muse_dir_without_qualifying_file_degrades_every_row(tmp_path, prices):
    sig_dir = tmp_path / "signals"
    sig_dir.mkdir()
    _muse_file(sig_dir / "2026-01-10.json", as_of="2026-01-10")
    code, data, _ = _run_muse(tmp_path, "--muse-signals", str(sig_dir))
    assert code == 2
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
