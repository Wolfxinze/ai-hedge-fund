"""Phase 1a-ii: the data cache must expire entries (whole-key TTL).

Without TTL a successful fetch is served for the life of the process, so stale
data masks a *current* provider outage (the loud-fail provider can only surface
an error if the cache actually misses and a re-fetch is attempted). A controllable
clock makes the expiry deterministic.
"""

from src.data.cache import Cache


def test_returns_data_within_ttl():
    now = [1000.0]
    c = Cache(ttl_seconds=100, clock=lambda: now[0])
    c.set_prices("k", [{"time": "t1", "close": 1}])
    now[0] = 1050.0  # 50s < 100s TTL
    assert c.get_prices("k") == [{"time": "t1", "close": 1}]


def test_expires_past_ttl_and_evicts():
    now = [1000.0]
    c = Cache(ttl_seconds=100, clock=lambda: now[0])
    c.set_prices("k", [{"time": "t1", "close": 1}])
    now[0] = 1101.0  # 101s > 100s TTL → miss
    assert c.get_prices("k") is None
    # evicted: a later read at the same (now-stale) time is still a miss
    assert c.get_prices("k") is None


def test_ttl_zero_disables_expiry():
    now = [0.0]
    c = Cache(ttl_seconds=0, clock=lambda: now[0])
    c.set_prices("k", [{"time": "t1"}])
    now[0] = 10**9  # far future
    assert c.get_prices("k") == [{"time": "t1"}]


def test_merge_preserved_within_fresh_window():
    now = [1000.0]
    c = Cache(ttl_seconds=1000, clock=lambda: now[0])
    c.set_prices("k", [{"time": "t1"}])
    now[0] = 1100.0
    c.set_prices("k", [{"time": "t2"}])  # merge, refreshes fetched_at
    assert c.get_prices("k") == [{"time": "t1"}, {"time": "t2"}]
    # dedup by key_field still works
    c.set_prices("k", [{"time": "t2"}])
    assert c.get_prices("k") == [{"time": "t1"}, {"time": "t2"}]


def test_set_refreshes_ttl_window():
    now = [0.0]
    c = Cache(ttl_seconds=100, clock=lambda: now[0])
    c.set_prices("k", [{"time": "t1"}])
    now[0] = 80.0
    c.set_prices("k", [{"time": "t2"}])  # refreshes fetched_at to 80
    now[0] = 150.0  # 70s since last set < 100s → still fresh
    assert c.get_prices("k") is not None


def test_all_endpoints_have_ttl():
    now = [0.0]
    c = Cache(ttl_seconds=10, clock=lambda: now[0])
    c.set_financial_metrics("k", [{"report_period": "2024"}])
    c.set_line_items("k", [{"report_period": "2024"}])
    c.set_insider_trades("k", [{"filing_date": "2024-01-01"}])
    c.set_company_news("k", [{"date": "2024-01-01"}])
    now[0] = 20.0  # past TTL
    assert c.get_financial_metrics("k") is None
    assert c.get_line_items("k") is None
    assert c.get_insider_trades("k") is None
    assert c.get_company_news("k") is None
