"""Production ``fetch_closes`` bridge (price_bridge) — FetchCloses Protocol impl.

``fetch_closes_via_provider`` adapts ``src.tools.api.get_prices`` to the
``FetchCloses`` seam consumed by ``pipeline._apply_haircut`` under an rh1 formula
version. Offline: ``get_prices`` is stubbed via monkeypatch so no network/keys are
touched. We pin the contract (shape, oldest→newest sort, wide-enough lookback
window, fail-loud on provider error) and the I1 trade-path boundary (must NOT
import ``src.agents.risk_manager`` et al — the same invariant the no_trade AST
suite enforces on the scoring modules).
"""

import ast
import pathlib
from datetime import date

import pytest

from src.data.models import Price
from src.observing_pools import price_bridge
from src.observing_pools.price_bridge import fetch_closes_via_provider

# Mirrors src/evals/suites/no_trade.py::_FORBIDDEN_IMPORT_SUBSTRINGS — a direct
# import of any of these reopens a path to the trade graph (I1 constraint).
_FORBIDDEN_IMPORT_SUBSTRINGS = ("run_hedge_fund", "portfolio_manager", "risk_manager", "risk_management", "src.main")


def _price(close: float, time: str) -> Price:
    """A Price with a given close/time; other required OHLCV fields are dummies."""
    return Price(open=close, close=close, high=close, low=close, volume=1000, time=time)


def test_returns_list_of_floats_matching_protocol_shape(monkeypatch):
    monkeypatch.setattr(price_bridge, "get_prices", lambda t, s, e, **k: [_price(10.0, "2025-06-01"), _price(11.0, "2025-06-02")])
    out = fetch_closes_via_provider("NVDA", "2025-06-30")
    assert isinstance(out, list)
    assert out == [10.0, 11.0]
    assert all(isinstance(c, float) for c in out)


def test_sorts_closes_oldest_to_newest_by_time(monkeypatch):
    # Provider returns rows OUT of chronological order — bridge must sort by .time asc.
    unsorted = [_price(30.0, "2025-06-03"), _price(10.0, "2025-06-01"), _price(20.0, "2025-06-02")]
    monkeypatch.setattr(price_bridge, "get_prices", lambda t, s, e, **k: unsorted)
    out = fetch_closes_via_provider("NVDA", "2025-06-30")
    assert out == [10.0, 20.0, 30.0]  # oldest → newest, not provider order


def test_start_date_is_at_least_84_days_before_end_date(monkeypatch):
    captured: dict[str, str] = {}

    def stub(ticker, start_date, end_date, **kwargs):
        captured["ticker"] = ticker
        captured["start_date"] = start_date
        captured["end_date"] = end_date
        return []

    monkeypatch.setattr(price_bridge, "get_prices", stub)
    end = "2025-06-30"
    fetch_closes_via_provider("NVDA", end)
    assert captured["ticker"] == "NVDA"
    assert captured["end_date"] == end
    span = (date.fromisoformat(end) - date.fromisoformat(captured["start_date"])).days
    assert span >= 84, f"start_date only {span} calendar days back; need >= 84 for a 60 trading-day lookback"


def test_provider_exception_propagates_unchanged(monkeypatch):
    class ProviderDown(RuntimeError):
        pass

    def boom(ticker, start_date, end_date, **kwargs):
        raise ProviderDown("provider unreachable")

    monkeypatch.setattr(price_bridge, "get_prices", boom)
    # Must NOT swallow — pipeline._apply_haircut relies on the exception surfacing to degrade.
    with pytest.raises(ProviderDown, match="provider unreachable"):
        fetch_closes_via_provider("NVDA", "2025-06-30")


def test_does_not_import_risk_manager_or_trade_path(_source_imports=None):
    """I1 boundary: AST-scan price_bridge.py source for any forbidden trade import."""
    src = pathlib.Path(price_bridge.__file__)
    tree = ast.parse(src.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
    bad = [name for name in imported for sub in _FORBIDDEN_IMPORT_SUBSTRINGS if sub in name]
    assert bad == [], f"price_bridge.py imports forbidden trade-path modules: {bad}"
