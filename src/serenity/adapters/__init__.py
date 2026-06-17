"""Serenity external evidence adapters (Phase 7).

Each adapter turns a (ticker, theme/keywords) request into reference dicts
``{source_url, claim_summary}`` for ``research.build_record(..., fetch_missing=True)``.
Adapters are PURE reference-builders: they open no socket and re-implement no SSRF
check — every outbound call flows through ``src.serenity.fetch.fetch_excerpt`` so the
host allowlist, IP-pin, and per-redirect re-gating apply unchanged.
"""
