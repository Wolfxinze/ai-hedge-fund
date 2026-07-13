"""Production ``FetchCloses`` bridge: adapt ``src.tools.api.get_prices`` to the
oldest→newest daily-close list the rh1 risk haircut consumes.

This is the concrete ``fetch_closes`` a production call site injects into
``pipeline.refresh_pool`` when an rh1 formula version is active (the default
``v3-4comp`` path never touches prices — ship dark). It fetches a window wide
enough to cover the 60 trading-day lookback in
``annualized_volatility_from_closes`` and returns closes sorted ascending by time.

I1 boundary: this module is on the scoring path and must NOT import
``src.agents.risk_manager`` or any trade-path module (enforced by the no_trade
AST scan). It imports only ``src.tools.api.get_prices``.

Fail-loud: exceptions from ``get_prices`` are NOT caught here —
``pipeline._apply_haircut`` already wraps ``fetch_closes`` in a try/except and
degrades the ticker visibly, so swallowing errors here would defeat that design.
"""

from __future__ import annotations

from datetime import date, timedelta

from src.tools.api import get_prices

# 100 calendar days comfortably covers the 60 trading-day lookback once weekends
# and holidays (~30% non-trading) are removed: 60 / 0.69 ≈ 87 calendar days.
_LOOKBACK_CALENDAR_DAYS = 100


def fetch_closes_via_provider(ticker: str, end_date: str) -> list[float]:
    """Return ``ticker``'s daily closes up to ``end_date`` (inclusive), oldest→newest.

    ``end_date`` is an ISO date string (``YYYY-MM-DD``). Fetches from
    ``end_date - 100 days`` via ``get_prices`` and returns ``[p.close, ...]`` sorted
    ascending by ``p.time`` (defensive — provider order is not assumed). Any
    exception from ``get_prices`` propagates unchanged (see module docstring).
    """
    start_date = (date.fromisoformat(end_date) - timedelta(days=_LOOKBACK_CALENDAR_DAYS)).isoformat()
    prices = get_prices(ticker, start_date, end_date)
    return [p.close for p in sorted(prices, key=lambda p: p.time)]
