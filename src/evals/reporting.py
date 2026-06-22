"""Logging, JSONL transcript capture, failure reporting + the eval CLI (PRD v4 §11).

Transcripts are append-only JSONL (one line per case) — naturally immutable, no DB
needed (eval runs are dev/CI artifacts, not a product output path). The CLI exits
non-zero on any failure so CI fails loudly, mirroring the monitoring CLI's exit-2
convention. Counsel sign-off (PRD §13/§19) is a *recorded* human line, not an
automated gate — its absence is reported as a release-blocker, never a test pass.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from src.evals.core import GraderKind, Transcript
from src.evals.registry import build_all, build_suite, registered_suites
from src.evals.runner import run_suite, SuiteReport

logger = logging.getLogger(__name__)

# Anchored to the project root (this file is src/evals/reporting.py → parents[2] is the repo
# root) so eval runs always land in <root>/evals_runs/ regardless of the process CWD.
DEFAULT_RUN_DIR = str(Path(__file__).resolve().parents[2] / "evals_runs")


def write_transcripts(transcripts: list[Transcript], path: str | Path) -> Path:
    """Append one JSONL line per transcript; create parent dirs. Returns the path."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as fh:
        for t in transcripts:
            fh.write(json.dumps(t.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
    return out


def record_signoff(path: str | Path, *, reviewer: str, notes: str, approved: bool) -> Path:
    """Append a counsel/human sign-off line (recorded, not run). Returns the path."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    line = {"kind": GraderKind.HUMAN.value, "type": "signoff", "reviewer": reviewer, "approved": bool(approved), "notes": notes}
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
    return out


def signoff_recorded(path: str | Path) -> bool:
    """True if ``path`` holds at least one approved counsel sign-off line."""
    p = Path(path)
    if not p.exists():
        return False
    for raw in p.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if row.get("type") == "signoff" and row.get("approved") is True:
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    """Run all (or one) eval suite, write transcripts, print a summary, exit non-zero on failure."""
    import argparse

    parser = argparse.ArgumentParser(prog="evals", description="Observing-Pools / Serenity eval suites (research-only, offline).")
    parser.add_argument("--suite", default=None, help=f"run one suite (default: all). Registered: {registered_suites()}")
    parser.add_argument("--out", default=f"{DEFAULT_RUN_DIR}/transcripts.jsonl", help="JSONL transcript path")
    args = parser.parse_args(argv)

    cases = build_suite(args.suite) if args.suite else build_all()
    if not cases:  # zero cases is a wiring failure, not a clean pass — fail loud (Rule 12)
        print(f"no eval cases to run (suite={args.suite!r}); registered suites: {registered_suites()}", file=sys.stderr)
        return 2

    report: SuiteReport = run_suite(cases)
    write_transcripts(report.transcripts, args.out)

    summary = report.summary()
    print(json.dumps(summary, indent=2, sort_keys=True))
    if report.failures:
        print(f"\nFAILED: {len(report.failures)}/{report.total} case(s) — see {args.out}", file=sys.stderr)
        return 2
    print(f"\nOK: {report.passed_count}/{report.total} case(s) passed")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(main())
