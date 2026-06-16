"""Phase 1a-i acceptance: the backtest must not swallow a provider fetch error
(PRD v4 §8.2, v4-review must-fix X2).

Before the fix, ``BacktestEngine`` wrapped the per-day price fetch in a broad
``except Exception: continue``, so a transient outage was silently converted into
"skip this day" — producing a clean-looking but survivorship-biased equity curve.
A ``ProviderFetchError`` must now propagate loudly instead.
"""

import pytest

from src.backtesting import engine as engine_mod
from src.backtesting.engine import BacktestEngine
from src.data.providers.exceptions import ProviderFetchError


def _make_engine(monkeypatch, price_data_fn):
    # Neutralize prefetch so the test never hits the network; make the per-day
    # price fetch behave as the test wants.
    monkeypatch.setattr(engine_mod, "get_prices", lambda *a, **k: [])
    monkeypatch.setattr(engine_mod, "get_financial_metrics", lambda *a, **k: [])
    monkeypatch.setattr(engine_mod, "get_insider_trades", lambda *a, **k: [])
    monkeypatch.setattr(engine_mod, "get_company_news", lambda *a, **k: [])
    monkeypatch.setattr(engine_mod, "get_price_data", price_data_fn)
    return BacktestEngine(
        agent=lambda **kwargs: {"decisions": {}, "analyst_signals": {}},
        tickers=["AAPL"],
        start_date="2024-01-02",
        end_date="2024-01-10",
        initial_capital=100_000.0,
        model_name="stub",
        model_provider="stub",
        selected_analysts=None,
        initial_margin_requirement=0.0,
    )


def test_backtest_propagates_provider_fetch_error(monkeypatch):
    def boom(*args, **kwargs):
        raise ProviderFetchError("simulated outage mid-backtest")

    engine = _make_engine(monkeypatch, boom)
    with pytest.raises(ProviderFetchError):
        engine.run_backtest()
