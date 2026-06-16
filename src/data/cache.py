import os
import time

_DEFAULT_TTL_SECONDS = 86400.0  # 1 day; <= 0 disables expiry


def _default_ttl() -> float:
    raw = os.environ.get("CACHE_TTL_SECONDS")
    if raw is None:
        return _DEFAULT_TTL_SECONDS
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_TTL_SECONDS


class Cache:
    """In-memory cache for API responses, with whole-key TTL expiry.

    Each entry stores ``(data, fetched_at)``. A read past the TTL is a miss and
    evicts the entry, so a stale success is never served indefinitely — the caller
    re-fetches and the loud-fail provider (src/data/providers) surfaces a *current*
    error instead of masking it with old data. TTL comes from ``CACHE_TTL_SECONDS``
    (default 1 day; <= 0 disables expiry). ``clock`` is injectable for tests.
    """

    def __init__(self, ttl_seconds: float | None = None, clock=time.time):
        self._ttl = _default_ttl() if ttl_seconds is None else float(ttl_seconds)
        self._clock = clock
        self._prices_cache: dict[str, tuple[list[dict], float]] = {}
        self._financial_metrics_cache: dict[str, tuple[list[dict], float]] = {}
        self._line_items_cache: dict[str, tuple[list[dict], float]] = {}
        self._insider_trades_cache: dict[str, tuple[list[dict], float]] = {}
        self._company_news_cache: dict[str, tuple[list[dict], float]] = {}

    def _merge_data(self, existing: list[dict] | None, new_data: list[dict], key_field: str) -> list[dict]:
        """Merge existing and new data, avoiding duplicates based on a key field."""
        if not existing:
            return new_data

        # Create a set of existing keys for O(1) lookup
        existing_keys = {item[key_field] for item in existing}

        # Only add items that don't exist yet
        merged = existing.copy()
        merged.extend([item for item in new_data if item[key_field] not in existing_keys])
        return merged

    def _get(self, store: dict[str, tuple[list[dict], float]], key: str) -> list[dict] | None:
        """Return cached data for ``key``, or None on miss/expiry (evicting stale)."""
        entry = store.get(key)
        if entry is None:
            return None
        data, fetched_at = entry
        if self._ttl > 0 and (self._clock() - fetched_at) > self._ttl:
            del store[key]  # whole-key eviction on TTL
            return None
        return data

    def _set(self, store: dict[str, tuple[list[dict], float]], key: str, data: list[dict], key_field: str) -> None:
        """Merge ``data`` into ``key`` and (re)stamp its freshness window."""
        existing = store.get(key)
        existing_data = existing[0] if existing else None
        store[key] = (self._merge_data(existing_data, data, key_field), self._clock())

    def get_prices(self, ticker: str) -> list[dict[str, any]] | None:
        """Get cached price data if available and fresh."""
        return self._get(self._prices_cache, ticker)

    def set_prices(self, ticker: str, data: list[dict[str, any]]):
        """Append new price data to cache."""
        self._set(self._prices_cache, ticker, data, key_field="time")

    def get_financial_metrics(self, ticker: str) -> list[dict[str, any]] | None:
        """Get cached financial metrics if available and fresh."""
        return self._get(self._financial_metrics_cache, ticker)

    def set_financial_metrics(self, ticker: str, data: list[dict[str, any]]):
        """Append new financial metrics to cache."""
        self._set(self._financial_metrics_cache, ticker, data, key_field="report_period")

    def get_line_items(self, ticker: str) -> list[dict[str, any]] | None:
        """Get cached line items if available and fresh."""
        return self._get(self._line_items_cache, ticker)

    def set_line_items(self, ticker: str, data: list[dict[str, any]]):
        """Append new line items to cache."""
        self._set(self._line_items_cache, ticker, data, key_field="report_period")

    def get_insider_trades(self, ticker: str) -> list[dict[str, any]] | None:
        """Get cached insider trades if available and fresh."""
        return self._get(self._insider_trades_cache, ticker)

    def set_insider_trades(self, ticker: str, data: list[dict[str, any]]):
        """Append new insider trades to cache."""
        self._set(self._insider_trades_cache, ticker, data, key_field="filing_date")

    def get_company_news(self, ticker: str) -> list[dict[str, any]] | None:
        """Get cached company news if available and fresh."""
        return self._get(self._company_news_cache, ticker)

    def set_company_news(self, ticker: str, data: list[dict[str, any]]):
        """Append new company news to cache."""
        self._set(self._company_news_cache, ticker, data, key_field="date")


# Global cache instance
_cache = Cache()


def get_cache() -> Cache:
    """Get the global cache instance."""
    return _cache
