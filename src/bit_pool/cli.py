"""Bit-pool CLI: ``python -m src.bit_pool run [...]`` (research-only; never places orders).

Writes ``<out>/<date>.json`` and ``<out>/<date>.md`` and prints the markdown path.
Exit codes are an automation contract: 0 ok, 2 any position or committee ticker
degraded, 1 error (bad CSV, oversell, bad arguments) with a message on stderr.
Usage errors exit 1 (not argparse's default 2) so 2 always means "degraded".

The committee (``--committee core|full``) runs the ai-hedge-fund scoring analysts
per ticker via ``make_committee_analyzing_flow``; both it and the scoring graph are
imported lazily so ``import src.bit_pool.cli`` and ``--help`` stay offline.
``--committee muse`` feeds the external Muse agent's signals file (``--muse-signals``
or ``$MUSE_SIGNALS_PATH``) through the same flow — no LLM runs here. There is no
default LLM provider/model: ``full`` requires both ``--provider`` and ``--model``.
``--label`` names the book (``real`` = owner's fills, ``muse`` = Muse's paper orders).
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

from src.bit_pool.holdings import DEFAULT_TRADES_PATH, load_holdings, load_trades, resolve_holdings_path
from src.bit_pool.muse_bridge import MUSE_SIGNALS_ENV, is_stale, load_muse_signals, make_muse_runner, resolve_muse_signals
from src.bit_pool.report import CommitteeSignal, render_markdown, to_json
from src.bit_pool.simulate import simulate_day

# LLM-free analysts (pure computation over provider data) — cheap enough to run daily.
CORE_COMMITTEE = ("technical_analyst", "fundamentals_analyst", "growth_analyst", "sentiment_analyst", "valuation_analyst")
COMMITTEE_CHOICES = ("none", "core", "full", "muse")
DEFAULT_OUT = "reports/bit_pool"
DEFAULT_LABEL = "real"
_LABEL_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DEGRADED = 2


class _Parser(argparse.ArgumentParser):
    """Usage errors exit 1 so exit code 2 is reserved for a degraded run."""

    def error(self, message: str):  # noqa: D401 — argparse hook
        self.print_usage(sys.stderr)
        self.exit(EXIT_ERROR, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m src.bit_pool", description="Bit pool: research-only daily paper simulation of your holdings. Never places orders.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="simulate one day and write <out>/<date>.json + .md")
    run.add_argument("--date", default=None, help="as-of date YYYY-MM-DD (default: today)")
    run.add_argument("--holdings", default=None, help="holdings CSV (default: $BIT_POOL_PATH or data/pools/bit_pool.csv)")
    run.add_argument("--trades", default=str(DEFAULT_TRADES_PATH), help=f"trades ledger CSV; missing file = no trades (default: {DEFAULT_TRADES_PATH})")
    run.add_argument("--committee", choices=COMMITTEE_CHOICES, default="none", help="analyst committee: none (default), core (5 LLM-free analysts), full (needs --provider and --model), muse (external Muse agent signals)")
    run.add_argument("--provider", default=None, help="LLM provider for the committee (required for full; no default)")
    run.add_argument("--model", default=None, help="LLM model for the committee (required for full; no default)")
    run.add_argument("--muse-signals", default=None, help=f"Muse signals file, or directory of YYYY-MM-DD.json (default: ${MUSE_SIGNALS_ENV})")
    run.add_argument("--label", default=DEFAULT_LABEL, help=f"book name, ^[a-z0-9_-]{{1,32}}$ (default: {DEFAULT_LABEL}); e.g. real = owner's fills, muse = Muse's paper orders")
    run.add_argument("--out", default=DEFAULT_OUT, help=f"output directory (default: {DEFAULT_OUT})")
    return parser


def _committee_keys(mode: str) -> list[str]:
    if mode == "core":
        return list(CORE_COMMITTEE)
    from src.observing_pools.agents_bridge import committee_analyst_keys

    return committee_analyst_keys()


def _committee_signal(result) -> CommitteeSignal:
    """``AnalyzingFlowResult`` → report row; an insufficient-evidence result is degraded."""
    reports = result.agent_reports or {}
    valid = [r["score"] for r in reports.values() if not r.get("degraded")]
    analysts = {k: str(r.get("signal")) for k, r in reports.items()}
    if result.degraded:
        return CommitteeSignal(result.ticker, None, None, None, analysts, result.error or result.summary)
    return CommitteeSignal(result.ticker, str(result.label.value), sum(valid) / len(valid) if valid else None, result.confidence, analysts, None)


def run_committee(tickers: list[str], as_of: date, mode: str, *, model: str | None = None, provider: str | None = None) -> tuple[CommitteeSignal, ...]:
    """Run the core/full committee per ticker; model/provider are passed only when given."""
    from src.monitoring.committee_flow import make_committee_analyzing_flow
    from src.observing_pools import scoring_graph

    overrides = {k: v for k, v in (("model_name", model), ("model_provider", provider)) if v is not None}
    runner = functools.partial(scoring_graph.run_scoring_analysts, **overrides)
    return _run_flow(make_committee_analyzing_flow(_committee_keys(mode), run_analysts=runner), tickers, as_of)


def _degrade_all(tickers: list[str], reason: str) -> tuple[CommitteeSignal, ...]:
    return tuple(CommitteeSignal(t, None, None, None, {}, reason) for t in tickers)


def run_muse_committee(tickers: list[str], as_of: date, signals_path: str) -> tuple[tuple[CommitteeSignal, ...], date | None]:
    """Committee from Muse's signals file → (rows, signals as_of). Missing/stale signals degrade every row."""
    path = resolve_muse_signals(signals_path, as_of)
    if path is None:
        return _degrade_all(tickers, f"no Muse signals on or before {as_of.isoformat()}"), None
    muse = load_muse_signals(path)
    if is_stale(muse, as_of):
        return _degrade_all(tickers, f"Muse signals stale (as_of {muse.as_of.isoformat()})"), muse.as_of
    from src.monitoring.committee_flow import make_committee_analyzing_flow

    flow = make_committee_analyzing_flow(list(muse.signals), run_analysts=make_muse_runner(muse))
    return _run_flow(flow, tickers, as_of), muse.as_of


def _run_flow(flow, tickers: list[str], as_of: date) -> tuple[CommitteeSignal, ...]:
    """Run ``flow`` per ticker; a per-ticker exception degrades only that ticker."""
    out: list[CommitteeSignal] = []
    for ticker in tickers:
        try:
            out.append(_committee_signal(flow(ticker, as_of.isoformat())))
        except Exception as exc:  # noqa: BLE001 — one ticker's failure must not abort the run
            out.append(CommitteeSignal(ticker, None, None, None, {}, f"committee failed: {type(exc).__name__}: {exc}"))
    return tuple(out)


def _muse_signals_path(args: argparse.Namespace) -> str | None:
    return args.muse_signals or os.environ.get(MUSE_SIGNALS_ENV) or None


def _validate_args(args: argparse.Namespace) -> None:
    """Fail fast (exit 1) on bad flag combinations before any file is read or written."""
    if not _LABEL_RE.match(args.label):
        raise ValueError(f"--label {args.label!r} must match {_LABEL_RE.pattern}")
    if args.committee == "full" and not (args.provider and args.model):
        raise ValueError("full committee needs --provider and --model")
    if args.committee == "muse" and _muse_signals_path(args) is None:
        raise ValueError(f"muse committee needs --muse-signals PATH or ${MUSE_SIGNALS_ENV}")


def _run(args: argparse.Namespace) -> int:
    _validate_args(args)
    as_of = date.fromisoformat(args.date) if args.date else date.today()
    holdings = load_holdings(resolve_holdings_path(args.holdings, os.environ))
    trades = load_trades(args.trades)
    snapshot = simulate_day(holdings, trades, as_of)
    committee, signals_as_of = None, None
    tickers = [p.ticker for p in snapshot.positions]
    if args.committee == "muse":
        committee, signals_as_of = run_muse_committee(tickers, as_of, _muse_signals_path(args))
    elif args.committee != "none":
        committee = run_committee(tickers, as_of, args.committee, model=args.model, provider=args.provider)
    meta = {"label": args.label, "committee_source": None if args.committee == "none" else args.committee, "signals_as_of": signals_as_of}

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    md_path = out / f"{as_of.isoformat()}.md"
    (out / f"{as_of.isoformat()}.json").write_text(json.dumps(to_json(snapshot, committee, **meta), indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(snapshot, committee, **meta), encoding="utf-8")
    print(md_path)

    committee_degraded = any(c.degraded_reason for c in committee or ())
    return EXIT_DEGRADED if snapshot.degraded or committee_degraded else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:  # --help exits 0; usage errors exit EXIT_ERROR via _Parser
        return int(exc.code or 0)
    try:
        return _run(args)
    except (ValueError, OSError) as exc:
        print(f"bit_pool: error: {exc}", file=sys.stderr)
        return EXIT_ERROR
