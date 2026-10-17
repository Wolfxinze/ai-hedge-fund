"""Bit-pool holdings CSV + trades ledger (research-only paper simulation).

The holdings file is the owner's real positions, so a malformed row must fail LOUD
(ValueError naming the row/ticker) rather than be silently skipped — a dropped row
would understate the pool and every percentage derived from it. The ledger math is
pinned against hand-computed numbers so a change to the weighted-average-cost or
realized-P&L rule turns these RED.
"""

from datetime import date
from pathlib import Path

import pytest

from src.bit_pool.holdings import (
    DEFAULT_HOLDINGS_PATH,
    HOLDINGS_COLUMNS,
    HOLDINGS_PATH_ENV,
    TRADES_COLUMNS,
    Holding,
    Trade,
    apply_trades,
    load_holdings,
    load_trades,
    resolve_holdings_path,
)

_HEADER = "ticker,shares,baseline_price,baseline_date,cost_basis\n"
_TRADES_HEADER = "date,ticker,side,shares,price,note\n"
_REPO = Path(__file__).resolve().parents[2]


def _write(tmp_path: Path, body: str, name: str = "pool.csv", header: str = _HEADER) -> Path:
    p = tmp_path / name
    p.write_text(header + body, encoding="utf-8")
    return p


def _h(ticker="AAA", shares=10.0, baseline=100.0, cost=None) -> Holding:
    return Holding(ticker=ticker, shares=shares, baseline_price=baseline, baseline_date=date(2026, 1, 2), cost_basis=cost)


def _t(d, ticker, side, shares, price) -> Trade:
    return Trade(date=d, ticker=ticker, side=side, shares=shares, price=price, note="")


# ── contract constants (README in a parallel task documents these verbatim) ──
def test_header_and_default_path_contract():
    assert HOLDINGS_COLUMNS == ("ticker", "shares", "baseline_price", "baseline_date", "cost_basis")
    assert TRADES_COLUMNS == ("date", "ticker", "side", "shares", "price", "note")
    # Anchored to the repo root, not the CWD: a cron job started elsewhere must not
    # silently miss the holdings file or (worse) the trades ledger and drop fills.
    assert DEFAULT_HOLDINGS_PATH == _REPO / "data/pools/bit_pool.csv"
    assert DEFAULT_HOLDINGS_PATH.is_absolute()
    assert HOLDINGS_PATH_ENV == "BIT_POOL_PATH"


def test_default_paths_do_not_depend_on_cwd(tmp_path, monkeypatch):
    from src.bit_pool.holdings import DEFAULT_TRADES_PATH, resolve_trades_path

    assert DEFAULT_TRADES_PATH == _REPO / "data/pools/bit_pool_trades.csv"
    monkeypatch.chdir(tmp_path)
    assert resolve_holdings_path(None, {}) == _REPO / "data/pools/bit_pool.csv"
    assert resolve_trades_path(None, {}) == (_REPO / "data/pools/bit_pool_trades.csv", False)
    # env overrides still win over the anchored defaults
    assert resolve_holdings_path(None, {"BIT_POOL_PATH": "env.csv"}) == Path("env.csv")
    assert resolve_trades_path(None, {"BIT_POOL_TRADES_PATH": "envt.csv"}) == (Path("envt.csv"), True)


def test_resolve_holdings_path_precedence():
    assert resolve_holdings_path("x.csv", {"BIT_POOL_PATH": "env.csv"}) == Path("x.csv")
    assert resolve_holdings_path(None, {"BIT_POOL_PATH": "env.csv"}) == Path("env.csv")
    assert resolve_holdings_path(None, {}) == DEFAULT_HOLDINGS_PATH


# ── loader: happy path ──
def test_loads_rows_and_blank_cost_basis_is_none(tmp_path):
    p = _write(tmp_path, "aapl,10,150.5,2026-01-02,120\nMSFT,2.5,400,2026-01-02,\n")
    rows = load_holdings(p)
    assert rows == (
        Holding("AAPL", 10.0, 150.5, date(2026, 1, 2), 120.0),
        Holding("MSFT", 2.5, 400.0, date(2026, 1, 2), None),
    )


def test_example_csv_loads_and_is_fake(tmp_path):
    rows = load_holdings(_REPO / "data/pools/bit_pool.example.csv")
    assert {r.ticker for r in rows} == {"AAPL", "MSFT", "NVDA"}
    assert all(r.shares > 0 for r in rows)


# ── loader: every rejection is loud and names the culprit ──
def test_missing_column_rejected(tmp_path):
    p = _write(tmp_path, "AAA,1,10,2026-01-02\n", header="ticker,shares,baseline_price,baseline_date\n")
    with pytest.raises(ValueError, match="cost_basis"):
        load_holdings(p)


@pytest.mark.parametrize(
    "row,needle",
    [
        ("1BAD,1,10,2026-01-02,\n", "1BAD"),  # bad ticker format
        ("AAA,0,10,2026-01-02,\n", "AAA"),  # shares == 0
        ("AAA,-1,10,2026-01-02,\n", "AAA"),  # shares < 0
        ("AAA,1,0,2026-01-02,\n", "AAA"),  # baseline_price == 0
        ("AAA,1,-5,2026-01-02,\n", "AAA"),  # baseline_price < 0
        ("AAA,1,10,02/01/2026,\n", "AAA"),  # unparseable date
        ("AAA,abc,10,2026-01-02,\n", "AAA"),  # unparseable shares
        ("AAA,1,10,2026-01-02,-3\n", "AAA"),  # negative cost basis
        (",1,10,2026-01-02,\n", "line 2"),  # empty ticker
    ],
)
def test_bad_row_rejected_naming_row_or_ticker(tmp_path, row, needle):
    p = _write(tmp_path, row)
    with pytest.raises(ValueError, match=needle):
        load_holdings(p)


def test_duplicate_ticker_rejected(tmp_path):
    p = _write(tmp_path, "AAA,1,10,2026-01-02,\naaa,2,11,2026-01-02,\n")
    with pytest.raises(ValueError, match="duplicate.*AAA|AAA.*duplicate"):
        load_holdings(p)


def test_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_holdings(tmp_path / "nope.csv")


# ── trades loader ──
def test_missing_trades_file_means_no_trades(tmp_path):
    assert load_trades(tmp_path / "nope.csv") == ()


def test_trades_loader_parses_and_validates(tmp_path):
    p = _write(tmp_path, "2026-01-05,aaa,BUY,2,110,top-up\n", name="t.csv", header=_TRADES_HEADER)
    assert load_trades(p) == (Trade(date(2026, 1, 5), "AAA", "buy", 2.0, 110.0, "top-up"),)


@pytest.mark.parametrize(
    "row",
    ["2026-01-05,AAA,hold,2,110,\n", "2026-01-05,AAA,buy,0,110,\n", "2026-01-05,AAA,buy,2,0,\n", "bad,AAA,buy,2,110,\n"],
)
def test_bad_trade_row_rejected(tmp_path, row):
    p = _write(tmp_path, row, name="t.csv", header=_TRADES_HEADER)
    with pytest.raises(ValueError, match="line 2"):
        load_trades(p)


def test_trades_missing_column_rejected(tmp_path):
    p = _write(tmp_path, "2026-01-05,AAA,buy,2,110\n", name="t.csv", header="date,ticker,side,shares,price\n")
    with pytest.raises(ValueError, match="note"):
        load_trades(p)


# ── ledger math ──
def test_buy_increases_shares_and_updates_weighted_average_cost():
    # 10 @ cost 100 + 10 @ 120 -> 20 @ 110; cash out 1200.
    res = apply_trades((_h(cost=100.0),), (_t(date(2026, 1, 5), "AAA", "buy", 10, 120.0),), date(2026, 1, 9))
    (pos,) = res.holdings
    assert pos.shares == 20.0
    assert pos.cost_basis == pytest.approx(110.0)
    assert pos.baseline_price == 100.0  # baseline is the original reference, not moved by buys
    assert res.net_cash_flow == pytest.approx(-1200.0)
    assert res.realized_pnl == 0.0


def test_buy_into_unknown_cost_position_keeps_cost_unknown():
    # Blending baseline_price in would FABRICATE a cost (110 here) and with it an
    # unrealized P&L the owner never had; unknown must stay unknown.
    res = apply_trades((_h(cost=None, baseline=100.0),), (_t(date(2026, 1, 5), "AAA", "buy", 10, 120.0),), date(2026, 1, 9))
    assert res.holdings[0].shares == 20.0
    assert res.holdings[0].cost_basis is None
    assert res.realized_pnl_estimated is False


def test_buy_new_ticker_creates_position_at_trade_price():
    res = apply_trades((_h(),), (_t(date(2026, 1, 5), "BBB", "buy", 3, 50.0),), date(2026, 1, 9))
    new = {h.ticker: h for h in res.holdings}["BBB"]
    assert (new.shares, new.baseline_price, new.cost_basis, new.baseline_date) == (3.0, 50.0, 50.0, date(2026, 1, 5))


def test_sell_reduces_shares_and_realizes_pnl_against_cost_basis():
    # sell 4 @ 130 against cost 100 -> realized (130-100)*4 = 120; proceeds +520.
    res = apply_trades((_h(cost=100.0, baseline=90.0),), (_t(date(2026, 1, 5), "AAA", "sell", 4, 130.0),), date(2026, 1, 9))
    assert res.holdings[0].shares == 6.0
    assert res.realized_pnl == pytest.approx(120.0)
    assert res.net_cash_flow == pytest.approx(520.0)


def test_sell_without_cost_basis_realizes_against_baseline_and_flags_estimate():
    res = apply_trades((_h(cost=None, baseline=90.0),), (_t(date(2026, 1, 5), "AAA", "sell", 4, 130.0),), date(2026, 1, 9))
    assert res.realized_pnl == pytest.approx(160.0)
    assert res.realized_pnl_estimated is True  # vs baseline, not a real cost — must be labelled


def test_known_cost_sell_is_not_an_estimate():
    res = apply_trades((_h(cost=100.0, baseline=90.0),), (_t(date(2026, 1, 5), "AAA", "sell", 4, 130.0),), date(2026, 1, 9))
    assert res.realized_pnl_estimated is False


def test_trades_applied_in_date_order_and_after_as_of_ignored():
    trades = (
        _t(date(2026, 1, 7), "AAA", "sell", 15, 130.0),  # only valid AFTER the buy below
        _t(date(2026, 1, 5), "AAA", "buy", 10, 120.0),
        _t(date(2026, 1, 20), "AAA", "sell", 5, 999.0),  # after as_of -> ignored
    )
    res = apply_trades((_h(cost=100.0),), trades, date(2026, 1, 9))
    # buy -> 20 @ 110; sell 15 @ 130 -> realized 300, 5 left.
    assert res.holdings[0].shares == pytest.approx(5.0)
    assert res.realized_pnl == pytest.approx(300.0)
    assert res.net_cash_flow == pytest.approx(-1200.0 + 1950.0)


def test_trade_on_as_of_is_applied():
    res = apply_trades((_h(),), (_t(date(2026, 1, 9), "AAA", "sell", 1, 100.0),), date(2026, 1, 9))
    assert res.holdings[0].shares == 9.0


def test_oversell_raises_naming_ticker():
    with pytest.raises(ValueError, match="AAA"):
        apply_trades((_h(shares=10),), (_t(date(2026, 1, 5), "AAA", "sell", 11, 100.0),), date(2026, 1, 9))


def test_sell_of_unheld_ticker_raises_naming_ticker():
    with pytest.raises(ValueError, match="ZZZ"):
        apply_trades((_h(),), (_t(date(2026, 1, 5), "ZZZ", "sell", 1, 100.0),), date(2026, 1, 9))


def test_sell_to_zero_removes_position():
    res = apply_trades((_h(shares=10), _h(ticker="BBB")), (_t(date(2026, 1, 5), "AAA", "sell", 10, 100.0),), date(2026, 1, 9))
    assert [h.ticker for h in res.holdings] == ["BBB"]


def test_apply_trades_does_not_mutate_inputs():
    original = (_h(cost=100.0),)
    apply_trades(original, (_t(date(2026, 1, 5), "AAA", "buy", 10, 120.0),), date(2026, 1, 9))
    assert original == (_h(cost=100.0),)


def test_tiny_sell_of_unheld_ticker_raises_oversell_not_attribute_error():
    # Below the zero-shares slack the old code fell through to cost lookup on None.
    with pytest.raises(ValueError, match="oversell.*ZZZ"):
        apply_trades((_h(),), (_t(date(2026, 1, 5), "ZZZ", "sell", 1e-10, 100.0),), date(2026, 1, 9))


# ── baseline_date: the baseline is end-of-day on baseline_date ──
@pytest.mark.parametrize("trade_day", [date(2026, 1, 2), date(2026, 1, 1)])
def test_trade_on_or_before_baseline_date_is_rejected_naming_ticker_and_date(trade_day):
    # The baseline already reflects that fill; applying it again double-counts shares.
    with pytest.raises(ValueError, match=f"AAA.*{trade_day.isoformat()}.*already included in baseline"):
        apply_trades((_h(),), (_t(trade_day, "AAA", "buy", 1, 100.0),), date(2026, 1, 9))


def test_as_of_before_a_baseline_date_is_rejected():
    with pytest.raises(ValueError, match="AAA.*2026-01-02"):
        apply_trades((_h(),), (), date(2026, 1, 1))


def test_buy_of_new_ticker_before_others_baseline_date_still_works():
    res = apply_trades((_h(),), (_t(date(2026, 1, 1), "BBB", "buy", 3, 50.0),), date(2026, 1, 9))
    assert {h.ticker for h in res.holdings} == {"AAA", "BBB"}


# ── CSV hardening: a malformed row must never be half-read ──
def test_extra_field_rejected_with_line_and_ticker(tmp_path):
    p = _write(tmp_path, "AAA,1,10,2026-01-02,,stray\n")
    with pytest.raises(ValueError, match="line 2.*AAA.*extra"):
        load_holdings(p)


def test_missing_field_rejected_with_line_and_ticker(tmp_path):
    # Only the trailing optional cost_basis may be omitted; a row short of a REQUIRED field raises.
    p = _write(tmp_path, "AAA,1,10,2026-01-02,\nBBB,1,10\n")
    with pytest.raises(ValueError, match="line 3.*BBB.*missing"):
        load_holdings(p)


def test_trailing_optional_column_may_be_omitted_entirely(tmp_path):
    # Hand-edited CSVs often drop the trailing comma of a blank last field; that is
    # unambiguous (the optional column is the last one), so accept it as blank.
    p = _write(tmp_path, "AAA,1,10,2026-01-02\n")
    assert load_holdings(p) == (Holding("AAA", 1.0, 10.0, date(2026, 1, 2), None),)
    t = _write(tmp_path, "2026-01-05,AAA,buy,2,110\n", name="t.csv", header=_TRADES_HEADER)
    assert load_trades(t) == (Trade(date(2026, 1, 5), "AAA", "buy", 2.0, 110.0, ""),)


def test_trades_missing_and_extra_fields_rejected(tmp_path):
    short = _write(tmp_path, "2026-01-05,AAA,buy,2\n", name="t.csv", header=_TRADES_HEADER)
    with pytest.raises(ValueError, match="line 2.*AAA.*missing"):
        load_trades(short)
    long = _write(tmp_path, "2026-01-05,AAA,buy,2,110,n,x\n", name="t2.csv", header=_TRADES_HEADER)
    with pytest.raises(ValueError, match="line 2.*AAA.*extra"):
        load_trades(long)


def test_line_number_counts_physical_lines_across_quoted_newlines(tmp_path):
    body = '2026-01-05,AAA,buy,2,110,"multi\nline note"\n2026-01-06,AAA,hold,1,110,\n'
    p = _write(tmp_path, body, name="t.csv", header=_TRADES_HEADER)
    with pytest.raises(ValueError, match="line 4"):
        load_trades(p)


def test_bom_prefixed_csv_loads(tmp_path):
    p = tmp_path / "bom.csv"
    p.write_text(_HEADER + "AAA,1,10,2026-01-02,\n", encoding="utf-8-sig")
    assert load_holdings(p)[0].ticker == "AAA"
    t = tmp_path / "bomt.csv"
    t.write_text(_TRADES_HEADER + "2026-01-05,AAA,buy,2,110,\n", encoding="utf-8-sig")
    assert load_trades(t)[0].ticker == "AAA"


def test_non_utf8_file_error_names_the_path(tmp_path):
    p = tmp_path / "latin.csv"
    p.write_bytes(_HEADER.encode() + "AAA,1,10,2026-01-02,caf\xe9\n".encode("latin-1"))
    with pytest.raises(ValueError, match="latin.csv"):
        load_holdings(p)


@pytest.mark.parametrize(
    "row",
    ["AAA,inf,10,2026-01-02,\n", "AAA,1,Infinity,2026-01-02,\n", "AAA,1,10,2026-01-02,1e999\n"],
)
def test_non_finite_numbers_rejected(tmp_path, row):
    with pytest.raises(ValueError, match="AAA"):
        load_holdings(_write(tmp_path, row))


def test_non_finite_trade_price_rejected(tmp_path):
    p = _write(tmp_path, "2026-01-05,AAA,buy,2,inf,\n", name="t.csv", header=_TRADES_HEADER)
    with pytest.raises(ValueError, match="line 2"):
        load_trades(p)


def test_header_only_holdings_rejected(tmp_path):
    with pytest.raises(ValueError, match="holdings file has no positions"):
        load_holdings(_write(tmp_path, ""))


def test_header_only_trades_file_is_no_trades(tmp_path):
    assert load_trades(_write(tmp_path, "", name="t.csv", header=_TRADES_HEADER)) == ()


# ── trades path resolution: an explicit path must exist ──
def test_resolve_trades_path_precedence_and_explicitness():
    from src.bit_pool.holdings import DEFAULT_TRADES_PATH, TRADES_PATH_ENV, resolve_trades_path

    assert resolve_trades_path("x.csv", {TRADES_PATH_ENV: "env.csv"}) == (Path("x.csv"), True)
    assert resolve_trades_path(None, {TRADES_PATH_ENV: "env.csv"}) == (Path("env.csv"), True)
    assert resolve_trades_path(None, {}) == (DEFAULT_TRADES_PATH, False)
    assert TRADES_PATH_ENV == "BIT_POOL_TRADES_PATH"


# ── unquoted thousands separators: a comma inside a number must never shift fields ──
def test_numeric_note_is_rejected_as_a_comma_spill_naming_the_line(tmp_path):
    # `234.50` was meant as the price `1,234.50`; unquoted, the CSV reads price=1 and
    # note='234.50' — a 99.9% wrong fill that used to load silently and exit 0.
    p = _write(tmp_path, "2026-01-05,AAA,buy,1,110,\n2026-10-10,AAPL,buy,10,1,234.50\n", name="t.csv", header=_TRADES_HEADER)
    with pytest.raises(ValueError, match="note looks like a number on line 3 — quote numbers containing commas"):
        load_trades(p)


@pytest.mark.parametrize("note", ["1,000", "42", "0.5"])
def test_any_purely_numeric_note_is_rejected(tmp_path, note):
    p = _write(tmp_path, f'2026-01-05,AAA,buy,2,110,"{note}"\n', name="t.csv", header=_TRADES_HEADER)
    with pytest.raises(ValueError, match="note looks like a number on line 2"):
        load_trades(p)


def test_quoted_thousands_price_and_textual_note_load(tmp_path):
    p = _write(tmp_path, '2026-10-10,AAPL,buy,10,"1234.50",bought 10 @ 1.2k\n', name="t.csv", header=_TRADES_HEADER)
    assert load_trades(p)[0].price == 1234.5 and load_trades(p)[0].note == "bought 10 @ 1.2k"


def test_holdings_cost_basis_comma_spill_raises(tmp_path):
    # Already-correct behaviour pinned: the spill lands as an extra field.
    p = _write(tmp_path, "AAPL,10,100,2026-01-02,1,234.50\n")
    with pytest.raises(ValueError, match="line 2.*AAPL.*extra"):
        load_holdings(p)
