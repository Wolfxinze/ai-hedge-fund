"""CLI for Serenity-lite research (PRD v4 §14, research-only).

    python -m src.serenity research --theme "AI accelerator packaging" --ticker NVDA \
        --platform ai --url https://www.sec.gov/x --claim "CoWoS constrains supply" \
        --excerpt "CoWoS advanced packaging capacity constrains NVIDIA H100 supply" \
        --scorecard 4,3,4,2,3
    python -m src.serenity apply --platform ai
"""

import argparse
import logging
import sys

from src.serenity.adapters.gather import gather_references
from src.serenity.grading import SCORECARD_DIMENSIONS
from src.serenity.integrate import apply_serenity_to_pool
from src.serenity.research import build_record
from src.storage import engine, session_scope
from src.storage.models import Base

logger = logging.getLogger(__name__)


def _parse_scorecard(raw: str) -> dict:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) != len(SCORECARD_DIMENSIONS):
        raise SystemExit(f"--scorecard needs {len(SCORECARD_DIMENSIONS)} values for {SCORECARD_DIMENSIONS}")
    return {dim: int(v) for dim, v in zip(SCORECARD_DIMENSIONS, parts)}


def _cmd_research(args: argparse.Namespace) -> int:
    Base.metadata.create_all(bind=engine)
    references = [{"source_url": u, "claim_summary": args.claim, "excerpt": args.excerpt} for u in args.url]
    with session_scope() as s:
        record = build_record(
            s,
            theme=args.theme,
            ticker=args.ticker,
            platform_key=args.platform,
            chain_layer=args.chain_layer,
            bottleneck_hypothesis=args.hypothesis,
            scorecard=_parse_scorecard(args.scorecard),
            references=references,
        )
        print(f"record id={record.id} ticker={record.ticker} grade={record.evidence_grade} " f"score={record.serenity_score} action={record.recommended_action}")
    return 0


def _cmd_discover(args: argparse.Namespace) -> int:
    """Auto-populate evidence by fanning the source adapters for a ticker, then build one
    research record per source (each with that source's correct fetch headers). The actual
    document bodies are fetched through the SSRF-guarded fetcher (fetch_missing=True)."""
    Base.metadata.create_all(bind=engine)
    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    sources = tuple(s.strip() for s in args.sources.split(",") if s.strip())
    scorecard = _parse_scorecard(args.scorecard)
    result = gather_references(args.ticker, keywords=keywords, sources=sources, max_per_source=args.max_per_source)

    # Surface per-source failures: a degrade-to-[] inside gather must not look like "no evidence".
    if result.errors:
        detail = ", ".join(f"{k} ({v})" for k, v in result.errors.items())
        print(f"discover: WARNING {len(result.errors)} source(s) errored: {detail}", file=sys.stderr)
    if not result.references:
        if result.errors and not any(result.counts.values()):
            # Every attempted source errored — this is a FAILURE, not an empty result.
            print(f"discover: all sources errored for {args.ticker}; no evidence could be gathered", file=sys.stderr)
            return 2
        print(f"discover: no allowlisted evidence found for {args.ticker} across {','.join(sources)}")
        return 0

    # One record per source group, each in its own transaction so a later group failing neither
    # rolls back earlier records nor prints a record line that was never persisted.
    built_lines = []
    failed = 0
    for fetch_headers, refs in result.groups:
        if not refs:
            continue
        try:
            with session_scope() as s:
                record = build_record(
                    s,
                    theme=args.theme,
                    ticker=args.ticker,
                    platform_key=args.platform,
                    chain_layer=args.chain_layer,
                    bottleneck_hypothesis=args.hypothesis,
                    scorecard=scorecard,
                    references=refs,
                    fetch_missing=True,
                    fetch_headers=fetch_headers,
                )
                line = f"record id={record.id} ticker={record.ticker} grade={record.evidence_grade} " f"score={record.serenity_score} refs={len(refs)}"
            built_lines.append(line)  # only after the transaction commits
        except Exception:
            failed += 1
            logger.exception("discover: build_record failed for a source group of %s", args.ticker)
            print(f"discover: WARNING a source group failed to build for {args.ticker}", file=sys.stderr)
    for line in built_lines:
        print(line)
    print(f"discover: built {len(built_lines)} record(s) for {args.ticker} from {len(result.references)} reference(s)")
    return 1 if failed and not built_lines else 0


def _cmd_apply(args: argparse.Namespace) -> int:
    Base.metadata.create_all(bind=engine)
    with session_scope() as s:
        summary = apply_serenity_to_pool(s, args.platform)
        print(f"applied serenity to '{args.platform}': graded={summary['graded']} " f"median={summary['median']} reranked={summary['reranked']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="serenity", description="Serenity bottleneck research (research-only).")
    sub = parser.add_subparsers(dest="command", required=True)

    r = sub.add_parser("research", help="build a bottleneck research record from allowlisted evidence")
    r.add_argument("--theme", required=True)
    r.add_argument("--ticker")
    r.add_argument("--platform")
    r.add_argument("--chain-layer", dest="chain_layer")
    r.add_argument("--hypothesis")
    r.add_argument("--url", action="append", default=[], help="evidence URL (repeatable)")
    r.add_argument("--claim", help="claim summary the evidence must substantiate")
    r.add_argument("--excerpt", help="fetched text excerpt (Phase 0: user-provided)")
    r.add_argument("--scorecard", required=True, help="5 ints 0-4: " + ",".join(SCORECARD_DIMENSIONS))
    r.set_defaults(func=_cmd_research)

    d = sub.add_parser("discover", help="auto-populate evidence by fanning source adapters for a ticker")
    d.add_argument("--theme", required=True)
    d.add_argument("--ticker", required=True)
    d.add_argument("--keywords", required=True, help="comma-separated claim keywords (the text the evidence must substantiate)")
    d.add_argument("--platform")
    d.add_argument("--chain-layer", dest="chain_layer")
    d.add_argument("--hypothesis")
    d.add_argument("--sources", default="edgar,federal_register", help="comma-separated subset of sources")
    d.add_argument("--max-per-source", type=int, default=3, dest="max_per_source")
    d.add_argument("--scorecard", required=True, help="5 ints 0-4: " + ",".join(SCORECARD_DIMENSIONS))
    d.set_defaults(func=_cmd_discover)

    a = sub.add_parser("apply", help="fold serenity scores into a pool and re-rank (v3-5comp)")
    a.add_argument("--platform", required=True)
    a.set_defaults(func=_cmd_apply)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
