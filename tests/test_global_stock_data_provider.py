"""Unit tests for the global-stock-data provider.

All network I/O goes through ``src.data.providers._gsd_client``; every test
monkeypatches those fetchers so the suite is hermetic. Covers: registry wiring,
HK symbol normalisation, price mapping + Sina fallback, statement-driven and
Eastmoney-fallback metrics, news date filtering, the empty insider-trades
contract, and the loud-fail contract for every fetch method.
"""

import pytest

from src.data.providers import _gsd_client as client
from src.data.providers import registry
from src.data.providers.exceptions import ProviderFetchError
from src.data.providers.global_stock_data import GlobalStockDataProvider, _to_yahoo_symbol


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    registry.reset_financial_data_provider_cache()
    yield


# --------------------------------------------------------------------------- #
# Registry wiring
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("alias", ["globalstockdata", "global_stock_data", "global-stock-data", "gsd"])
def test_aliases_resolve_to_global_stock_data(monkeypatch, alias):
    monkeypatch.setenv("FINANCIAL_DATA_PROVIDER", alias)
    registry.reset_financial_data_provider_cache()
    assert isinstance(registry.get_financial_data_provider(), GlobalStockDataProvider)


def test_default_provider_unchanged(monkeypatch):
    monkeypatch.delenv("FINANCIAL_DATA_PROVIDER", raising=False)
    monkeypatch.delenv("FINANCIAL_DATA_SOURCE", raising=False)
    registry.reset_financial_data_provider_cache()
    assert registry.get_configured_provider_name() == "yfinance"


# --------------------------------------------------------------------------- #
# Ticker normalisation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "ticker,expected",
    [("AAPL", "AAPL"), ("aapl", "AAPL"), ("0700.HK", "0700.HK"), ("00700", "0700.HK"), ("700", "0700.HK"), ("9988.HK", "9988.HK")],
)
def test_symbol_normalisation(ticker, expected):
    assert _to_yahoo_symbol(ticker) == expected


# --------------------------------------------------------------------------- #
# Prices
# --------------------------------------------------------------------------- #
def test_get_prices_maps_yahoo_rows(monkeypatch):
    rows = [{"date": "2024-01-02", "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 5000}]
    monkeypatch.setattr(client, "yahoo_chart", lambda *a, **k: rows)
    prices = GlobalStockDataProvider().get_prices("PRICE_AAPL", "2024-01-01", "2024-01-31")
    assert len(prices) == 1
    assert prices[0].close == 101.0
    assert prices[0].time == "2024-01-02T00:00:00"


def test_get_prices_falls_back_to_sina_for_us(monkeypatch):
    monkeypatch.setattr(client, "yahoo_chart", lambda *a, **k: [])
    sina_rows = [
        {"date": "2024-01-02", "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 1},
        {"date": "2024-03-01", "open": 12.0, "high": 13.0, "low": 11.0, "close": 12.5, "volume": 2},
    ]
    monkeypatch.setattr(client, "sina_us_daily", lambda *a, **k: sina_rows)
    prices = GlobalStockDataProvider().get_prices("SINA_FB", "2024-01-01", "2024-01-31")
    assert [p.time[:10] for p in prices] == ["2024-01-02"]  # window-filtered


def test_get_prices_hk_does_not_use_sina(monkeypatch):
    monkeypatch.setattr(client, "yahoo_chart", lambda *a, **k: [])

    def _boom(*a, **k):
        raise AssertionError("Sina must not be used for HK tickers")

    monkeypatch.setattr(client, "sina_us_daily", _boom)
    assert GlobalStockDataProvider().get_prices("0700.HK", "2024-01-01", "2024-01-31") == []


# --------------------------------------------------------------------------- #
# Financial metrics
# --------------------------------------------------------------------------- #
def _statements_payload():
    def node(value):
        return {"raw": value}

    return {
        "incomeStatementHistoryQuarterly": {
            "incomeStatementHistory": [
                {"endDate": {"fmt": "2024-03-31"}, "totalRevenue": node(1000), "grossProfit": node(400), "operatingIncome": node(300), "netIncome": node(100)},
                {"endDate": {"fmt": "2023-12-31"}, "totalRevenue": node(950), "grossProfit": node(380), "operatingIncome": node(280), "netIncome": node(90)},
                {"endDate": {"fmt": "2023-09-30"}, "totalRevenue": node(900), "grossProfit": node(360), "operatingIncome": node(260), "netIncome": node(85)},
                {"endDate": {"fmt": "2023-06-30"}, "totalRevenue": node(850), "grossProfit": node(340), "operatingIncome": node(240), "netIncome": node(80)},
            ]
        },
        "balanceSheetHistoryQuarterly": {
            "balanceSheetStatements": [
                {"endDate": {"fmt": "2024-03-31"}, "totalAssets": node(5000), "totalStockholderEquity": node(2000), "totalCurrentAssets": node(1500), "totalCurrentLiabilities": node(800)},
            ]
        },
        "cashflowStatementHistoryQuarterly": {"cashflowStatements": []},
    }


def _key_stats_payload():
    """Yahoo financialData / defaultKeyStatistics snapshot (precomputed ratios)."""
    def node(value):
        return {"raw": value}

    return {
        "financialData": {
            "grossMargins": node(0.45),
            "operatingMargins": node(0.30),
            "profitMargins": node(0.25),
            "returnOnEquity": node(1.2),
            "returnOnAssets": node(0.26),
            "currentRatio": node(1.1),
            "quickRatio": node(0.9),
            "debtToEquity": node(150.0),  # Yahoo reports a percent -> 1.5
            "revenueGrowth": node(0.16),
            "earningsGrowth": node(0.21),
            "freeCashflow": node(1000),
            "operatingCashflow": node(1400),
            "ebitda": node(1600),
            "grossProfits": node(2000),
            "totalDebt": node(800),
            "totalCash": node(600),
            "totalRevenue": node(4000),
            "financialCurrency": "USD",
        },
        "defaultKeyStatistics": {
            "enterpriseValue": node(50000),
            "enterpriseToEbitda": node(26.0),
            "priceToBook": node(39.0),
            "pegRatio": node(2.3),
            "sharesOutstanding": node(1000),
            "trailingEps": node(8.0),
            "bookValue": node(7.0),
            "earningsQuarterlyGrowth": node(0.19),
            "netIncomeToCommon": node(1100),
            "totalAssets": node(8000),
        },
        "summaryDetail": {"marketCap": node(40000), "trailingPE": node(34.0), "priceToSalesTrailing12Months": node(9.0)},
        "price": {"currency": "USD"},
    }


def _split_quote_summary(symbol, modules):
    """Route statement-history modules to statements, everything else to the snapshot.

    Mirrors the live reality: Yahoo's statement endpoint is gutted (only revenue +
    net income survive) while the financialData snapshot stays rich.
    """
    if any("StatementHistory" in m for m in modules):
        return _statements_payload()
    return _key_stats_payload()


def test_financial_metrics_overlay_from_key_statistics(monkeypatch):
    # The gutted statements carry junk for most fields; the precomputed snapshot must win.
    monkeypatch.setattr(client, "yahoo_quote_summary", _split_quote_summary)
    m = GlobalStockDataProvider().get_financial_metrics("OVL_AAPL", "2024-12-31", period="ttm", limit=1)[0]
    assert m.return_on_equity == pytest.approx(1.2)  # snapshot, not statement-derived 0.1775
    assert m.gross_margin == pytest.approx(0.45)  # snapshot overrides statement 0.4
    assert m.debt_to_equity == pytest.approx(1.5)  # 150% -> 1.5 (percent scaling)
    assert m.current_ratio == pytest.approx(1.1)
    assert m.debt_to_assets == pytest.approx(800 / 8000)
    assert m.free_cash_flow_yield == pytest.approx(1000 / 40000)
    assert m.return_on_invested_capital == pytest.approx(1200 / 7200)  # opInc 1200 / (debt 800 + equity 7000 - cash 600)
    assert m.price_to_earnings_ratio == pytest.approx(34.0)
    assert m.earnings_per_share == pytest.approx(8.0)


def test_search_line_items_overlay_from_key_statistics(monkeypatch):
    monkeypatch.setattr(client, "yahoo_quote_summary", _split_quote_summary)
    items = GlobalStockDataProvider().search_line_items(
        "OVL_LI",
        ["revenue", "free_cash_flow", "capital_expenditure", "total_debt", "ebitda", "outstanding_shares"],
        "2024-12-31",
        period="ttm",
        limit=1,
    )
    x = items[0]
    assert x.revenue == 3700  # TTM sum from statements (still reliable) — overlay not applied
    assert x.free_cash_flow == 1000  # filled from snapshot
    assert x.capital_expenditure == -400  # derived: FCF 1000 - OCF 1400
    assert x.total_debt == 800
    assert x.ebitda == 1080  # statement-derived (TTM operatingIncome) wins; overlay only fills gaps
    assert x.outstanding_shares == 1000


def test_financial_metrics_from_statements(monkeypatch):
    def fake_quote_summary(symbol, modules):
        if any("StatementHistory" in m for m in modules):
            return _statements_payload()
        return {"summaryDetail": {"marketCap": {"raw": 20000}, "trailingPE": {"raw": 25.0}}, "defaultKeyStatistics": {"sharesOutstanding": {"raw": 1000}, "trailingEps": {"raw": 4.0}}}

    monkeypatch.setattr(client, "yahoo_quote_summary", fake_quote_summary)
    metrics = GlobalStockDataProvider().get_financial_metrics("METRIC_AAPL", "2024-12-31", period="ttm", limit=1)
    assert len(metrics) == 1
    m = metrics[0]
    assert m.report_period == "2024-03-31"
    assert m.market_cap == 20000
    assert m.price_to_earnings_ratio == 25.0
    assert m.gross_margin == pytest.approx(0.4)  # TTM grossProfit 1480 / TTM revenue 3700
    assert m.return_on_equity == pytest.approx(0.1775)  # TTM netIncome 355 / point-in-time equity 2000


def test_financial_metrics_fall_back_to_eastmoney(monkeypatch):
    monkeypatch.setattr(client, "yahoo_quote_summary", lambda symbol, modules: {} if any("StatementHistory" in m for m in modules) else {"summaryDetail": {"marketCap": {"raw": 1}}})
    monkeypatch.setattr(client, "eastmoney_search", lambda *a, **k: [{"code": "AAPL", "name": "Apple", "mkt_num": 105}])
    monkeypatch.setattr(
        client,
        "eastmoney_key_indicators",
        lambda *a, **k: [{"REPORT_DATE": "2024-03-31 00:00:00", "ROE_AVG": 18.5, "ROA": 9.2, "NET_PROFIT_RATIO": 25.0, "BASIC_EPS": 1.5, "DEBT_ASSET_RATIO": 40.0}],
    )
    metrics = GlobalStockDataProvider().get_financial_metrics("EM_AAPL", "2024-12-31", period="quarter", limit=1)
    assert len(metrics) == 1
    assert metrics[0].return_on_equity == pytest.approx(0.185)
    assert metrics[0].debt_to_assets == pytest.approx(0.40)
    assert metrics[0].earnings_per_share == 1.5


# --------------------------------------------------------------------------- #
# Line items, news, insider trades
# --------------------------------------------------------------------------- #
def test_search_line_items(monkeypatch):
    monkeypatch.setattr(client, "yahoo_quote_summary", lambda symbol, modules: _statements_payload())
    items = GlobalStockDataProvider().search_line_items("LI_AAPL", ["revenue", "net_income"], "2024-12-31", period="quarter", limit=2)
    assert items[0].revenue == 1000
    assert items[0].net_income == 100


def test_company_news_date_filter(monkeypatch):
    # 2024-01-15 and 2024-06-15 epochs
    monkeypatch.setattr(
        client,
        "yahoo_news",
        lambda *a, **k: [
            {"title": "in window", "publisher": "Reuters", "link": "u1", "providerPublishTime": 1705276800},
            {"title": "out of window", "publisher": "Reuters", "link": "u2", "providerPublishTime": 1718409600},
        ],
    )
    news = GlobalStockDataProvider().get_company_news("NEWS_AAPL", "2024-01-31", start_date="2024-01-01")
    assert [n.title for n in news] == ["in window"]


def test_insider_trades_is_empty_nodata():
    result = GlobalStockDataProvider().get_insider_trades("AAPL", "2024-12-31")
    assert result == []
    assert not result


# --------------------------------------------------------------------------- #
# Loud-fail contract
# --------------------------------------------------------------------------- #
def _raise(*a, **k):
    raise ConnectionError("simulated transport failure")


def test_get_prices_raises_on_transport_failure(monkeypatch):
    monkeypatch.setattr(client, "yahoo_chart", _raise)
    monkeypatch.setattr(client, "sina_us_daily", _raise)
    with pytest.raises(ProviderFetchError):
        GlobalStockDataProvider().get_prices("RAISE_AAPL", "2024-01-01", "2024-02-01")


@pytest.mark.parametrize(
    "call",
    [
        lambda p: p.get_financial_metrics("ZZGSD", "2024-02-01"),
        lambda p: p.search_line_items("ZZGSD", ["revenue"], "2024-02-01"),
        lambda p: p.get_company_news("ZZGSD", "2024-02-01"),
        lambda p: p.get_market_cap("ZZGSD", "2024-02-01"),
    ],
)
def test_fetch_methods_raise_on_transport_failure(monkeypatch, call):
    monkeypatch.setattr(client, "yahoo_chart", _raise)
    monkeypatch.setattr(client, "yahoo_quote_summary", _raise)
    monkeypatch.setattr(client, "yahoo_news", _raise)
    with pytest.raises(ProviderFetchError):
        call(GlobalStockDataProvider())
