"""Domain eval suites. Importing this package registers every suite (each
submodule's ``@suite`` decorator runs at import). Each suite co-locates its
graders with its inline, offline fixtures (known-fake citations, irrelevant
excerpts, stuffing/injection payloads, SSRF URLs). No I/O at import.
"""

from src.evals.suites import (
    classification,
    disclaimer,
    evidence,
    injection,
    no_trade,
    scoring,
    ssrf,
)

# Listed in __all__ so the registration-only imports are not flagged unused
# (pyflakes treats __all__ names as exported/used).
__all__ = ["classification", "disclaimer", "evidence", "injection", "no_trade", "scoring", "ssrf"]
