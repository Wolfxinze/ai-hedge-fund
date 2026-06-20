"""In-process APScheduler layer (Observing Pools Phase 8 — Monitoring + Scheduler).

Mounts a BackgroundScheduler in the FastAPI backend that drives the weekly pool refresh and the
enabled monitors on their cadence, each refresh serialised per-platform by the PoolLock claim-row.
Research-only: jobs only produce ranked pools + disclaimer-bearing reports, never trades.
"""
