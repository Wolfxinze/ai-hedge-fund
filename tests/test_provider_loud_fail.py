"""Phase 1a-i acceptance: the provider loud-fail contract (PRD v4 §8.2).

Encodes the v4-review must-fix X2 at the provider boundary: a fetch failure must
RAISE ``ProviderFetchError`` (loud); genuine emptiness must stay falsy NoData so
existing ``if not data:`` callers are unchanged. The backtest half of X2 lives in
``tests/backtesting/test_loud_fail.py``.
"""

import pandas as pd
import pytest

from src.data.providers.exceptions import ProviderError, ProviderFetchError
from src.data.providers.yfinance import YFinanceProvider


class _RaisingTicker:
    """A yfinance Ticker whose .history() fails like a network/throttle error."""

    def history(self, *args, **kwargs):
        raise ConnectionError("simulated yfinance transport failure")


class _EmptyTicker:
    """A yfinance Ticker that genuinely returns no rows (NoData, not an error)."""

    def history(self, *args, **kwargs):
        return pd.DataFrame()


def test_provider_fetch_error_is_a_provider_error():
    assert issubclass(ProviderFetchError, ProviderError)


def test_get_prices_raises_on_transport_failure(monkeypatch):
    provider = YFinanceProvider()
    monkeypatch.setattr(provider, "_ticker", lambda ticker: _RaisingTicker())
    with pytest.raises(ProviderFetchError):
        provider.get_prices("RAISE_AAPL", "2024-01-01", "2024-02-01")


def test_get_prices_returns_falsy_nodata_on_genuine_emptiness(monkeypatch):
    """Genuine emptiness must remain falsy NoData — only real errors raise."""
    provider = YFinanceProvider()
    monkeypatch.setattr(provider, "_ticker", lambda ticker: _EmptyTicker())
    result = provider.get_prices("EMPTY_MSFT", "2024-01-01", "2024-02-01")
    assert result == []
    assert not result  # falsy → unchanged contract for legacy `if not prices:` branches


class _AllRaisingTicker:
    """Every attribute access fails like a transport error (Gap-1: the contract
    must hold for ALL fetch methods, not just get_prices)."""

    def __getattr__(self, name):
        raise ConnectionError(f"simulated transport failure accessing {name}")


@pytest.mark.parametrize(
    "call",
    [
        lambda p: p.get_financial_metrics("ZZYF", "2024-02-01"),
        lambda p: p.search_line_items("ZZYF", ["revenue"], "2024-02-01"),
        lambda p: p.get_insider_trades("ZZYF", "2024-02-01"),
        lambda p: p.get_company_news("ZZYF", "2024-02-01"),
        lambda p: p.get_market_cap("ZZYF", "2024-02-01"),
    ],
)
def test_all_yfinance_fetch_methods_raise_on_transport_failure(monkeypatch, call):
    provider = YFinanceProvider()
    monkeypatch.setattr(provider, "_ticker", lambda ticker: _AllRaisingTicker())
    with pytest.raises(ProviderFetchError):
        call(provider)


def _yahoo_down_errors():
    """What yfinance raises when the news endpoint answers with an error page:
    a bare RuntimeError in 0.2.x, YFDataException in 1.x."""
    from yfinance.exceptions import YFDataException

    message = "*** YAHOO! FINANCE IS CURRENTLY DOWN! ***"
    return [RuntimeError(message), YFDataException(message)]


@pytest.mark.parametrize("error", _yahoo_down_errors(), ids=lambda e: type(e).__name__)
def test_news_outage_from_yahoo_is_a_fetch_error(monkeypatch, error):
    """A dead news endpoint is a fetch failure: it must raise ProviderFetchError
    so only the analyst that needs news degrades. As a bare RuntimeError (0.2.x)
    it escaped the analyst boundary and failed every ticker's whole committee."""

    class _YahooDownNewsTicker:
        @property
        def news(self):
            raise error

    provider = YFinanceProvider()
    monkeypatch.setattr(provider, "_ticker", lambda ticker: _YahooDownNewsTicker())
    with pytest.raises(ProviderFetchError):
        provider.get_company_news("DOWN_NEWS", "2024-02-01")


def test_news_bad_ticker_is_a_fetch_error(monkeypatch):
    """Building the Ticker can itself fail (yfinance 1.x rejects an empty symbol);
    that must degrade one analyst like every other fetch method, not the run."""
    provider = YFinanceProvider()

    def _bad_ticker(ticker):
        raise ValueError("Empty ticker name")

    monkeypatch.setattr(provider, "_ticker", _bad_ticker)
    with pytest.raises(ProviderFetchError):
        provider.get_company_news("BAD_NEWS", "2024-02-01")


def test_missing_yfinance_package_still_raises_runtime_error(monkeypatch):
    """A missing package is a setup error, not an outage: it must stay loud and
    must not be downgraded to a per-analyst degradation."""
    provider = YFinanceProvider()

    def _no_package():
        raise RuntimeError("The yfinance package is required when FINANCIAL_DATA_PROVIDER=yfinance.")

    monkeypatch.setattr(provider, "_yf", _no_package)
    with pytest.raises(RuntimeError) as excinfo:
        provider.get_company_news("NOPKG_NEWS", "2024-02-01")
    assert not isinstance(excinfo.value, ProviderFetchError)
