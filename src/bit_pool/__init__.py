"""Bit pool: research-only paper-trading simulation of the owner's real holdings.

Loads a holdings CSV (+ an optional manual trades ledger), marks it to the latest
daily closes, raises descriptive alerts, optionally attaches committee signals, and
writes a disclaimer-stamped JSON + markdown report. It NEVER places orders and must
not import any trade-path module (``src.main``, portfolio/risk managers) — enforced
by the no_trade AST scan.

Importing this package is offline and light: the price provider and the analyst
committee are imported lazily, only when a run actually needs them.
"""
