"""Typed provider outcomes (PRD v4 §8.2, Phase 1a-i).

Loud-fail contract: a *fetch failure* (network/transport/parse error, or a
throttle that cannot be distinguished from genuine emptiness) RAISES a typed
exception — it is never returned as an empty list/None. Genuine *no data* (the
source legitimately has nothing for the window) stays an empty result (falsy),
so existing ``if not data:`` callers keep working unchanged.

This split is what stops a transient provider outage from being silently scored
as "no data" and corrupting a ranking or a backtest — the central reliability
finding across the PRD review rounds.
"""


class ProviderError(Exception):
    """Base class for financial-data-provider errors."""


class ProviderFetchError(ProviderError):
    """A fetch failed (network/transport/parse).

    Distinct from genuine no-data, which is returned as an empty result. Raising
    — never swallowing — is what keeps a data outage from masquerading as an
    empty/bearish signal downstream.
    """


class ProviderAmbiguousError(ProviderFetchError):
    """An empty result that cannot be distinguished from a throttle.

    Some providers return an empty body on a 429 rather than raising. Treated
    conservatively as a fetch error. Subclasses ``ProviderFetchError`` so a
    single ``except ProviderFetchError`` catches both.
    """
