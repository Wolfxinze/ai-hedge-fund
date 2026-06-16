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
