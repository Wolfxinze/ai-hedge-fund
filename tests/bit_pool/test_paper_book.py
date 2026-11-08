"""Cash-seeded paper book. Fixture prices check the ledger, not investment performance.

Every dollar figure below is hand-computed. Network and the technical analyst are
not used; the technical mapping is fed signals directly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from src.bit_pool.paper_book import (
    DATA_SYNTHETIC,
    DECISION_EQUAL,
    DECISION_TECHNICAL,
    EXECUTION_NEXT,
    EXECUTION_SAME,
    REBALANCE_ONCE,
    REBALANCE_WEEKLY,
    PaperConfig,
    build_summary,
    buy_fill,
    common_sessions,
    decision_dates,
    equal_weight_provider,
    execute_targets,
    load_state,
    map_technical_signals,
    new_state,
    replay_ledger,
    run_book,
    save_state,
    technical_provider,
    validate_config,
    validate_targets,
)

_REPO = Path(__file__).resolve().parents[2]
TODAY = date(2026, 11, 8)
D1 = date(2024, 1, 2)
D2 = date(2024, 1, 3)
D3 = date(2024, 1, 4)


def _config(**overrides) -> PaperConfig:
    data = dict(
        label="verify",
        initial_cash=10_000.0,
        currency="USD",
        tickers=("AAA", "BBB"),
        benchmark="SPY",
        start=D1,
        end=D3,
        decision=DECISION_EQUAL,
        rebalance=REBALANCE_ONCE,
        commission_bps=0.0,
        slippage_bps=0.0,
        execution=EXECUTION_SAME,
        data_mode=DATA_SYNTHETIC,
    )
    data.update(overrides)
    config = PaperConfig(**data)
    validate_config(config, today=TODAY)
    return config


def _prices():
    return {
        "AAA": {D1: 100.0, D2: 110.0, D3: 110.0},
        "BBB": {D1: 50.0, D2: 40.0, D3: 40.0},
        "SPY": {D1: 500.0, D2: 505.0, D3: 505.0},
    }


def _flat(price_a=10.0, price_b=10.0, spy=100.0):
    return {
        "AAA": {D1: price_a, D2: price_a, D3: price_a},
        "BBB": {D1: price_b, D2: price_b, D3: price_b},
        "SPY": {D1: spy, D2: spy, D3: spy},
    }


def test_equal_weight_zero_cost_math_and_drawdown():
    config = _config()
    state = new_state(config)
    run_book(state, [D1, D2], _prices(), config, equal_weight_provider(config))
    assert [(f["ticker"], f["side"], f["shares"], f["fill_price"]) for f in state["fills"]] == [
        ("AAA", "buy", 50, 100.0),
        ("BBB", "buy", 100, 50.0),
    ]
    assert state["cash"] == pytest.approx(0.0)
    assert state["positions"]["AAA"]["shares"] == 50
    assert state["positions"]["BBB"]["shares"] == 100
    day1, day2 = state["equity_curve"][1], state["equity_curve"][2]
    assert day1["equity"] == pytest.approx(10_000.0)
    assert day1["cash"] + day1["positions_value"] == pytest.approx(day1["equity"])
    # 50*110 + 100*40 = 9500. This is a fixture path, not a market result.
    assert day2["positions_value"] == pytest.approx(9_500.0)
    assert day2["equity"] == pytest.approx(9_500.0)
    assert day2["benchmark_return_pct"] == pytest.approx(1.0)
    summary = build_summary(state, sessions=[D1, D2])
    assert summary["total_return_pct"] == pytest.approx(-5.0)
    assert summary["max_drawdown_pct"] == pytest.approx(-5.0)
    assert summary["max_drawdown_date"] == D2.isoformat()
    assert summary["establishes_investing_performance"] is False
    assert summary["llm_invoked"] is False
    assert summary["forward_paper_trading"] is False
    assert summary["data_mode"] == DATA_SYNTHETIC
    assert "Research and educational use only" in summary["disclaimer"]
    assert summary["invariants"]["ok"] is True
    replay = replay_ledger(10_000.0, state["fills"], {"AAA": 110.0, "BBB": 40.0})
    assert replay["equity"] == pytest.approx(9_500.0)
    assert replay["cash"] == pytest.approx(0.0)


def test_costs_reduce_cash_and_never_overspend():
    config = _config(tickers=("AAA",), commission_bps=100.0, slippage_bps=100.0, initial_cash=1_000.0)
    prices = {"AAA": {D1: 100.0, D2: 100.0}, "SPY": {D1: 500.0, D2: 500.0}}
    state = new_state(config)
    run_book(state, [D1, D2], prices, config, equal_weight_provider(config))
    fill = state["fills"][0]
    assert buy_fill(100.0, 100.0) == pytest.approx(101.0)
    assert fill["shares"] == 9
    assert fill["fill_price"] == pytest.approx(101.0)
    assert fill["notional"] == pytest.approx(909.0)
    assert fill["commission"] == pytest.approx(9.09)
    assert state["cash"] == pytest.approx(1_000.0 - 918.09)
    assert state["cash"] > 0
    marked = state["equity_curve"][1]
    assert marked["positions_value"] == pytest.approx(900.0)
    assert marked["equity"] == pytest.approx(state["cash"] + 900.0)
    assert marked["equity"] < 1_000.0
    assert state["commission_paid"] == pytest.approx(9.09)
    assert state["slippage_drag"] == pytest.approx(9.0)
    # A second share would have cost 102.01 and does not fit in the residual cash.
    assert state["cash"] < 102.01


def test_buy_is_cut_to_cash_instead_of_borrowing():
    config = _config(tickers=("AAA",), initial_cash=150.0)
    prices = {"AAA": {D1: 100.0}, "SPY": {D1: 50.0}}
    state = new_state(config)
    run_book(state, [D1], prices, config, equal_weight_provider(config))
    assert state["fills"][0]["shares"] == 1
    assert state["cash"] == pytest.approx(50.0)
    assert state["positions"]["AAA"]["shares"] == 1


def test_negative_weight_and_leverage_are_refused():
    config = _config(tickers=("AAA",))
    with pytest.raises(ValueError, match="no shorts"):
        validate_targets({"AAA": -0.1}, ("AAA",), 1.0)
    with pytest.raises(ValueError, match="no leverage"):
        validate_targets({"AAA": 0.6, "BBB": 0.6}, ("AAA", "BBB"), 1.0)
    state = new_state(config)

    def short(_day):
        return {"targets": {"AAA": -1.0}, "signals": None, "missing_signals": [], "source": "bad"}

    with pytest.raises(ValueError, match="no shorts"):
        run_book(state, [D1], {"AAA": {D1: 10.0}, "SPY": {D1: 10.0}}, config, short)
    assert state["fills"] == []
    assert state["cash"] == pytest.approx(10_000.0)


def test_sell_does_not_exceed_the_long():
    config = _config(tickers=("AAA",), rebalance=REBALANCE_WEEKLY, end=date(2024, 1, 8))
    sessions = [D1, date(2024, 1, 8)]
    prices = {"AAA": {sessions[0]: 100.0, sessions[1]: 110.0}, "SPY": {sessions[0]: 100.0, sessions[1]: 100.0}}

    def provider(day):
        weight = 1.0 if day == D1 else 0.0
        return {"targets": {"AAA": weight}, "signals": None, "missing_signals": [], "source": "fixture"}

    state = new_state(config)
    run_book(state, sessions, prices, config, provider)
    assert [f["side"] for f in state["fills"]] == ["buy", "sell"]
    assert state["fills"][0]["shares"] == 100
    assert state["fills"][1]["shares"] == 100
    assert state["positions"] == {}
    assert state["cash"] == pytest.approx(11_000.0)
    assert state["realized_pnl"] == pytest.approx(1_000.0)
    with pytest.raises(RuntimeError, match="short guard"):
        held = new_state(config)
        held["positions"]["AAA"] = {"shares": 1, "avg_cost": 10.0}
        execute_targets(held, D1, {"AAA": 10.0}, {"AAA": 0.0}, config, "fixture", "flatten")
        # execute of a flat target sells the 1 share; force the guard through a second sell
        from src.bit_pool.paper_book import _apply_sell

        _apply_sell(held, D1, "AAA", 1, 10.0, config, "fixture", "again")


def test_next_close_fills_the_following_session_and_resume_matches():
    config = _config(decision=DECISION_TECHNICAL, rebalance=REBALANCE_WEEKLY, execution=EXECUTION_NEXT, tickers=("AAA",), initial_cash=1_000.0)
    # Friday decision, Monday fill + new decision, Tuesday fill of the Monday decision.
    friday, monday, tuesday = date(2024, 1, 5), date(2024, 1, 8), date(2024, 1, 9)
    sessions = [friday, monday, tuesday]
    prices = {"AAA": {day: 10.0 for day in sessions}, "SPY": {day: 100.0 for day in sessions}}
    calls = []

    def signals(day, tickers):
        del tickers
        calls.append(day)
        signal = "bullish" if day == friday else "bearish"
        return {"AAA": {"signal": signal, "confidence": 80}}

    provider = technical_provider(config, signals)
    full = new_state(config)
    run_book(full, sessions, prices, config, provider)
    assert full["fills"][0]["date"] == monday.isoformat()
    assert full["fills"][0]["side"] == "buy"
    assert full["fills"][0]["shares"] == 100
    assert full["fills"][1]["date"] == tuesday.isoformat()
    assert full["fills"][1]["side"] == "sell"
    assert full["positions"] == {}
    assert full["cash"] == pytest.approx(1_000.0)
    assert calls == [friday, monday]

    resumed = new_state(config)
    resume_calls: list[date] = []

    def resume_signals(day, tickers):
        resume_calls.append(day)
        return signals(day, tickers)

    run_book(resumed, [friday], prices, config, technical_provider(config, resume_signals))
    assert resumed["fills"] == []
    assert resumed["pending"]["decided_on"] == friday.isoformat()
    assert resumed["pending"]["fill_on"] is None  # no later session was known yet
    run_book(resumed, sessions, prices, config, technical_provider(config, resume_signals))
    assert resumed["cash"] == pytest.approx(full["cash"])
    assert [(row["date"], row["side"], row["shares"]) for row in resumed["fills"]] == [(row["date"], row["side"], row["shares"]) for row in full["fills"]]
    assert friday not in resume_calls[1:]  # the stored Friday decision is not recomputed


def test_second_run_is_idempotent_across_state_reload(tmp_path):
    config = _config()
    state = new_state(config)
    sessions = [D1, D2, D3]
    run_book(state, sessions, _prices(), config, equal_weight_provider(config))
    path = tmp_path / "state.json"
    save_state(path, state)
    loaded = load_state(path, config)
    calls = []

    def counting(day):
        calls.append(day)
        return equal_weight_provider(config)(day)

    run_book(loaded, sessions, _prices(), config, counting)
    assert calls == []
    assert loaded["fills"] == state["fills"]
    assert loaded["cash"] == pytest.approx(state["cash"])
    summary = build_summary(loaded, sessions=sessions)
    assert summary["invariants"]["ok"] is True
    for point in loaded["equity_curve"]:
        assert point["cash"] + point["positions_value"] == pytest.approx(point["equity"])
        assert point["cash"] >= -0.005


def test_config_mismatch_and_empty_prices_and_window_guards(tmp_path):
    config = _config()
    path = tmp_path / "state.json"
    save_state(path, new_state(config))
    other = _config(initial_cash=20_000.0)
    with pytest.raises(ValueError, match="belongs to book"):
        load_state(path, other)
    with pytest.raises(ValueError, match="no common sessions"):
        run_book(new_state(config), [], _prices(), config, equal_weight_provider(config))
    with pytest.raises(ValueError, match="future"):
        validate_config(_config(end=date(2026, 12, 1)), today=TODAY)
    with pytest.raises(ValueError, match="370"):
        validate_config(_config(start=date(2024, 1, 1), end=date(2025, 6, 1)), today=TODAY)
    with pytest.raises(ValueError, match="no leverage"):
        validate_config(_config(max_gross_exposure=1.5), today=TODAY)


def test_weekly_decision_dates_are_not_daily():
    sessions = []
    day = date(2024, 1, 2)
    while len(sessions) < 20:
        if day.weekday() < 5:
            sessions.append(day)
        day = date.fromordinal(day.toordinal() + 1)
    chosen = decision_dates(sessions, REBALANCE_WEEKLY)
    assert len(chosen) < 8
    assert len(chosen) >= 4
    assert decision_dates(sessions, REBALANCE_ONCE) == {sessions[0]}


def test_technical_map_is_long_only_equal_weight_among_bullish_names():
    targets, missing = map_technical_signals(
        {"AAA": {"signal": "bullish", "confidence": 10}, "BBB": {"signal": "neutral", "confidence": 99}, "CCC": None},
        ("AAA", "BBB", "CCC"),
    )
    assert targets == {"AAA": 1.0, "BBB": 0.0, "CCC": 0.0}
    assert missing == ["CCC"]
    cash, missing_all = map_technical_signals({"AAA": {"signal": "bearish", "confidence": 100}}, ("AAA",))
    assert cash == {"AAA": 0.0}
    assert missing_all == []
    # Confidence must not scale size: a low-confidence bullish name still takes the full sleeve.
    assert targets["AAA"] == 1.0


def test_common_sessions_drop_days_missing_a_name():
    prices = {"AAA": {D1: 1.0, D2: 1.0}, "BBB": {D1: 1.0}, "SPY": {D1: 1.0, D2: 1.0}}
    assert common_sessions(prices, ("AAA", "BBB"), "SPY", D1, D2) == [D1]


def _write_csv(path: Path, rows: list[tuple[str, str, float]]) -> None:
    path.write_text("date,ticker,close\n" + "\n".join(f"{day},{ticker},{close}" for day, ticker, close in rows) + "\n", encoding="utf-8")


def test_cli_synthetic_book_is_labelled_and_idempotent(tmp_path):
    csv_path = tmp_path / "prices.csv"
    _write_csv(
        csv_path,
        [
            ("2024-01-02", "AAA", 100),
            ("2024-01-02", "BBB", 50),
            ("2024-01-02", "SPY", 500),
            ("2024-01-03", "AAA", 110),
            ("2024-01-03", "BBB", 40),
            ("2024-01-03", "SPY", 505),
        ],
    )
    out = tmp_path / "out"
    args = [
        sys.executable,
        "-m",
        "src.bit_pool",
        "paper",
        "--cash",
        "10000",
        "--tickers",
        "AAA,BBB",
        "--start",
        "2024-01-02",
        "--end",
        "2024-01-03",
        "--decision",
        "equal_weight",
        "--prices-csv",
        str(csv_path),
        "--data-label",
        "synthetic_fixture",
        "--commission-bps",
        "0",
        "--slippage-bps",
        "0",
        "--out",
        str(out),
    ]
    env = {**os.environ, "PYTHONPATH": str(_REPO)}
    first = subprocess.run(args, cwd=_REPO, env=env, capture_output=True, text=True, timeout=60)
    assert first.returncode == 0, first.stderr + first.stdout
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["data_mode"] == "synthetic_fixture"
    assert summary["establishes_investing_performance"] is False
    assert summary["final_equity"] == pytest.approx(9_500.0)
    assert summary["final_cash"] == pytest.approx(0.0)
    assert summary["cash_plus_positions"] == pytest.approx(summary["final_equity"])
    assert summary["llm_invoked"] is False
    fills = (out / "fills.csv").read_text(encoding="utf-8")
    assert "AAA" in fills
    second = subprocess.run(args, cwd=_REPO, env=env, capture_output=True, text=True, timeout=60)
    assert second.returncode == 0, second.stderr
    assert "new=0" in second.stdout
    again = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert again["fills"] == summary["fills"]
    assert again["final_equity"] == pytest.approx(summary["final_equity"])
    rejected = subprocess.run(
        [sys.executable, "-m", "src.bit_pool", "paper", "--start", "2024-01-02", "--end", "2024-01-03", "--prices-csv", str(csv_path), "--data-label", "yfinance_historical", "--out", str(tmp_path / "nope")],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert rejected.returncode == 1
    assert "cannot be labelled yfinance_historical" in rejected.stderr


def test_cli_help_mentions_paper_and_rejects_a_missing_window():
    env = {**os.environ, "PYTHONPATH": str(_REPO)}
    help_run = subprocess.run([sys.executable, "-m", "src.bit_pool", "paper", "--help"], cwd=_REPO, env=env, capture_output=True, text=True, timeout=60)
    assert help_run.returncode == 0, help_run.stderr
    assert "never places orders" in help_run.stdout
    assert "--cash" in help_run.stdout
    missing = subprocess.run([sys.executable, "-m", "src.bit_pool", "paper", "--out", "unused"], cwd=_REPO, env=env, capture_output=True, text=True, timeout=60)
    assert missing.returncode == 1
    assert "--start" in missing.stderr
