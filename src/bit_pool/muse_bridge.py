"""Muse bridge: consume the external Muse agent's signals file as the committee.

Muse (an external manager/trader agent, not part of this repo) writes one JSON file
per day into a private reports repo::

    {"as_of": "YYYY-MM-DD",
     "signals": {"<analyst_key>": {"<TICKER>": {"signal": "bullish|bearish|neutral",
                                                "confidence": 0-100,
                                                "reasoning": "<optional>"}}}}

``analyst_key`` is a bare key from ``committee_analyst_keys()`` (no ``_agent``
suffix). Every malformed entry fails LOUD with a ``ValueError`` naming the analyst
and ticker — a silently dropped or coerced signal would move a committee band
unnoticed. ``make_muse_runner`` adapts the file to the ``RunScoringAnalysts``
contract so the existing committee aggregation (omit-not-zero, insufficient-evidence
→ degraded) applies unchanged. Research-only: reads signals, never places orders.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType

from src.bit_pool.holdings import _TICKER_RE

MUSE_SIGNALS_ENV = "MUSE_SIGNALS_PATH"
# Signals older than this many calendar days before the run date are stale.
MUSE_STALE_DAYS = 4
SIGNALS = ("bullish", "bearish", "neutral")
_DATED_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")


@dataclass(frozen=True)
class MuseSignals:
    """One validated Muse signals file: analyst_key -> ticker -> {signal, confidence, reasoning}."""

    as_of: date
    signals: Mapping[str, Mapping[str, Mapping[str, object]]]
    source_path: str


def _parse_entry(analyst: str, ticker: str, raw: object) -> Mapping[str, object]:
    where = f"analyst '{analyst}' ticker '{ticker}'"
    if not _TICKER_RE.match(ticker):
        raise ValueError(f"Muse signals: invalid ticker '{ticker}' under analyst '{analyst}'")
    if not isinstance(raw, dict):
        raise ValueError(f"Muse signals: {where}: entry must be an object")
    if raw.get("signal") not in SIGNALS:
        raise ValueError(f"Muse signals: {where}: signal {raw.get('signal')!r} not in {SIGNALS}")
    confidence = raw.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 100:
        raise ValueError(f"Muse signals: {where}: confidence {confidence!r} must be a number in 0-100")
    reasoning = raw.get("reasoning", "")
    if reasoning is None:
        reasoning = ""
    if not isinstance(reasoning, str):
        raise ValueError(f"Muse signals: {where}: reasoning must be a string")
    return MappingProxyType({"signal": raw["signal"], "confidence": confidence, "reasoning": reasoning})


def _parse_signals(raw: object) -> Mapping[str, Mapping[str, Mapping[str, object]]]:
    # Light import (scoring math only) — kept local so importing this module stays cheap.
    from src.observing_pools.agents_bridge import committee_analyst_keys

    if not isinstance(raw, dict):
        raise ValueError("Muse signals: 'signals' must be an object of analyst_key -> ticker -> entry")
    known = set(committee_analyst_keys())
    out: dict[str, Mapping[str, Mapping[str, object]]] = {}
    for analyst, by_ticker in raw.items():
        if analyst not in known:
            raise ValueError(f"Muse signals: unknown analyst '{analyst}' (use bare committee keys, e.g. 'warren_buffett')")
        if not isinstance(by_ticker, dict):
            raise ValueError(f"Muse signals: analyst '{analyst}' must map tickers to entries")
        out[analyst] = MappingProxyType({ticker: _parse_entry(analyst, ticker, entry) for ticker, entry in by_ticker.items()})
    return MappingProxyType(out)


def load_muse_signals(path: str | Path) -> MuseSignals:
    """Read and validate a Muse signals file; any malformed content raises ``ValueError``."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Muse signals {path}: invalid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"Muse signals {path}: top level must be an object")
    try:
        as_of = date.fromisoformat(doc["as_of"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Muse signals {path}: missing or unparseable as_of {doc.get('as_of')!r} (want YYYY-MM-DD)") from exc
    return MuseSignals(as_of=as_of, signals=_parse_signals(doc.get("signals")), source_path=str(path))


def _file_date(name: str) -> date | None:
    match = _DATED_FILE_RE.match(name)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def resolve_muse_signals(path_arg: str | Path, as_of: date) -> Path | None:
    """A file is returned as-is; a directory yields its newest ``YYYY-MM-DD.json`` dated <= ``as_of`` (None if none)."""
    path = Path(path_arg)
    if path.is_file():
        return path
    if not path.is_dir():
        raise FileNotFoundError(f"Muse signals path not found: {path}")
    dated = [(d, p) for p in path.iterdir() if p.is_file() and (d := _file_date(p.name)) is not None and d <= as_of]
    return max(dated)[1] if dated else None


def is_stale(muse: MuseSignals, as_of: date) -> bool:
    """True when the signals are more than ``MUSE_STALE_DAYS`` before ``as_of``; future-dated raises."""
    if muse.as_of > as_of:
        raise ValueError(f"Muse signals {muse.source_path} are dated {muse.as_of.isoformat()}, after the run date {as_of.isoformat()}")
    return (as_of - muse.as_of).days > MUSE_STALE_DAYS


def make_muse_runner(muse: MuseSignals) -> Callable[[list[str], list[str], str], tuple[dict[str, dict[str, dict]], dict]]:
    """Adapt Muse signals to the committee's ``RunScoringAnalysts`` contract (no LLM cost).

    Only requested analysts/tickers present in the file are returned; absent ones are
    omitted so the committee treats them as absent (not zero).
    """

    def run(tickers: list[str], selected_analysts: list[str], end_date: str) -> tuple[dict[str, dict[str, dict]], dict]:
        out: dict[str, dict[str, dict]] = {}
        for key in selected_analysts:
            by_ticker = muse.signals.get(key, {})
            hits = {t: dict(by_ticker[t]) for t in tickers if t in by_ticker}
            if hits:
                out[f"{key}_agent"] = hits
        return out, {}

    return run
