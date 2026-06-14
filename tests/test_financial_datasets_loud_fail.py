"""Phase 1a-i: extend the loud-fail contract to the financial_datasets provider
(PRD v4 §8.2). A non-200 / parse failure must RAISE ``ProviderFetchError``; a
200 with a genuinely empty payload stays falsy NoData.
"""

import pytest

from src.data.providers.exceptions import ProviderFetchError
from src.data.providers.financial_datasets import FinancialDatasetsProvider

_PATCH = "src.data.providers.financial_datasets._make_api_request"


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_get_prices_raises_on_non_200(monkeypatch):
    provider = FinancialDatasetsProvider()
    monkeypatch.setattr(_PATCH, lambda *a, **k: _Resp(500))
    with pytest.raises(ProviderFetchError):
        provider.get_prices("FD_RAISE", "2024-01-01", "2024-02-01")


def test_get_prices_returns_falsy_nodata_on_empty_200(monkeypatch):
    provider = FinancialDatasetsProvider()
    monkeypatch.setattr(_PATCH, lambda *a, **k: _Resp(200, {"ticker": "FD_EMPTY", "prices": []}))
    result = provider.get_prices("FD_EMPTY", "2024-01-01", "2024-02-01")
    assert result == []
    assert not result


def test_get_financial_metrics_raises_on_non_200(monkeypatch):
    provider = FinancialDatasetsProvider()
    monkeypatch.setattr(_PATCH, lambda *a, **k: _Resp(429))
    with pytest.raises(ProviderFetchError):
        provider.get_financial_metrics("FD_METRICS", "2024-02-01")


def test_get_prices_raises_on_unparseable_200(monkeypatch):
    provider = FinancialDatasetsProvider()
    # 200 but a body that fails schema validation → fetch/parse error, not NoData.
    monkeypatch.setattr(_PATCH, lambda *a, **k: _Resp(200, {"unexpected": "shape"}))
    with pytest.raises(ProviderFetchError):
        provider.get_prices("FD_BADBODY", "2024-01-01", "2024-02-01")
