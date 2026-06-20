"""Suite registry — maps a suite name to a builder that returns its ``EvalCase``s.

Suites self-register at import via the ``@suite`` decorator (the analyst-node
discovery idiom used elsewhere in the repo). ``build_all`` imports the suites
package so every module registers, then flattens. Builders MUST be import-safe:
no network/DNS/file I/O at import or build time (offline guarantee).
"""

from __future__ import annotations

import importlib
from collections.abc import Callable

from src.evals.core import EvalCase

_REGISTRY: dict[str, Callable[[], list[EvalCase]]] = {}


def suite(name: str) -> Callable[[Callable[[], list[EvalCase]]], Callable[[], list[EvalCase]]]:
    """Decorator registering a suite builder under ``name``."""

    def _register(builder: Callable[[], list[EvalCase]]) -> Callable[[], list[EvalCase]]:
        if name in _REGISTRY:
            raise ValueError(f"duplicate eval suite name: {name!r}")
        _REGISTRY[name] = builder
        return builder

    return _register


def registered_suites() -> list[str]:
    _import_suites()  # ensure every suite module has registered before listing
    return sorted(_REGISTRY)


def build_suite(name: str) -> list[EvalCase]:
    _import_suites()
    if name not in _REGISTRY:
        raise KeyError(f"unknown eval suite {name!r}; registered: {registered_suites()}")
    return _REGISTRY[name]()


def build_all() -> list[EvalCase]:
    """Import every suite module and return all registered cases, flattened."""
    _import_suites()
    cases: list[EvalCase] = []
    for name in sorted(_REGISTRY):
        cases.extend(_REGISTRY[name]())
    return cases


def _import_suites() -> None:
    """Import the suites package so each module's @suite decorator runs."""
    importlib.import_module("src.evals.suites")
