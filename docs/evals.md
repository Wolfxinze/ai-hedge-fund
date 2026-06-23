# Observing Pools / Serenity — Eval Framework (Phase 11)

A net-new, **Python-native** eval framework (`src/evals/`) that turns every
invariant the prior phases built — research-only/no-trade, scoring stability,
evidence integrity, SSRF defense, and the disclaimer invariant — into a
**regression gate**. It adapts Anthropic's *"Demystifying Evals for AI Agents"*
three-grader taxonomy; it does **not** depend on the bun/TS PAI `Evals` skill.

Everything is **fully offline** — no network, no LLM, no timer, no subprocess, and
no trade path is reachable from any grader or fixture.

## Setup & running

```bash
poetry install                       # deps already present (pytest, sqlalchemy, requests)

# Run the whole suite (writes JSONL transcripts, exits non-zero on any failure):
poetry run python -m src.evals
poetry run python -m src.evals --suite ssrf --out evals_runs/ssrf.jsonl

# As part of the normal test gate (the suites are also asserted green by pytest):
poetry run pytest tests/evals -q
```

`evals_runs/` (git-ignored) holds the append-only JSONL transcript of each run —
one line per case: inputs, the boundary calls the grader observed (`tool_calls`),
the per-trial pass/fail, and the reason. A non-zero exit means a regression gate
tripped; the failure summary lists `{suite, case_id, reason}`.

## Configuration

The suites pin the env knobs they depend on so a run is deterministic:

| Env var | Used by | Default |
|---|---|---|
| `DISCLAIMER_VERSION` | disclaimer suite (canonical version) | `2026-06` |
| `SERENITY_HOST_ALLOWLIST` | SSRF/evidence allowlist (extends the default) | unset → built-in allowlist |

The evals assert against the **real shipped constants**, e.g. the composite
formula versions are `v3-4comp` / `v3-5comp` (`src/observing_pools/scoring.py`),
where `v3` is the composite formula's own version axis and `-4comp` / `-5comp`
mark the 4-component (pre-Serenity) and 5-component (Serenity-folded) variants.
These strings are the source of truth — the evals assert them verbatim, never a
reformatted label.

## Grader taxonomy (`src/evals/core.py`)

| Type | Class | Used for | Seams wrapped |
|---|---|---|---|
| **code** | `CodeGrader` | deterministic checks (the default — every Phase-11 target is deterministic) | `serialize_report`, `grade_evidence`, `is_substantiated`, `classify_reference`, `composite`, `classify_candidate`, `fetch_excerpt`/`_validate_ip`, the scoring-graph node set |
| **model** | `ModelGrader` | genuine free-text nuance only | a stub judge offline; **never** grades evidence / sets `source_type` / touches scoring (structurally impossible — `CodeGrader` has no judge slot) |
| **human** | `HumanGrader` | counsel sign-off, calibration | recorded, **never run** (`grade()` raises); recorded via `reporting.record_signoff` |

## Metrics

- **pass@k** — ≥1 success in k trials (capability signal).
- **pass^k** — *all* k trials succeed (consistency / regression signal).

Because the graders are deterministic and offline, "k trials" is repeated
in-process invocation with no flakiness source, so **pass^k is a strict
regression assertion**. Regression suites target 100%; the capability tier
(~70%) exists in the framework but no current case uses it (the domain is
deterministic).

## Coverage (suites)

| Suite | Asserts |
|---|---|
| `classification` | exact labels + confidence; the `'ai'`-in-`'retail'` substring trap is blocked; unknown labels raise |
| `scoring` | `composite` determinism (pass^k) + independent reference; a **degraded** analyst is excluded (never a masking 50 that outranks a bearish read); REQUIRED-missing excludes (not 0); the F2 bootstrap imputes absent serenity at the median (never favorably); degenerate weights rejected |
| `evidence` | known-fake/off-allowlist → UNVERIFIED → F; a 200-but-irrelevant corpus (200 pairs) never substantiates; per-host cap bounds flooding; zero-substantiated → withheld (None, not 0); a genuine allowlisted source *does* count |
| `injection` | an injection payload in claim/excerpt/scorecard is **inert** — it cannot flip host-derived `source_type`, the deterministic `substantiated`, or the grade (no LLM in the grading path) |
| `ssrf` | the `_validate_ip` matrix (internal/metadata/IPv6/IPv4-mapped/encoded blocked, public allowed); off-allowlist + suffix-spoof hosts UNVERIFIED; `fetch_excerpt` blocks non-https / userinfo / off-allowlist / raw-internal-IP / DNS-rebinding with the exact reason |
| `disclaimer` | `serialize_report` refuses a blank/whitespace disclaimer; the **DB CHECK** rejects one at the DB layer (both tables); the disclaimer survives a sqlite3 logical dump/restore; canonical text is non-directional (model-grader, stub judge) |
| `no_trade` | the scoring graph wires no risk/portfolio node; the scoring/classification modules import no trade path directly |

## Research-only / loopback posture

This is research-and-education software. **No grader or fixture can reach a trade
path.** The scoring graph (`scoring_graph._build_scoring_workflow`) drops
`risk_management_agent` and `portfolio_manager`; the `no_trade` suite asserts both
the graph node set and the module imports. Evals exercise only `refresh_pool` /
`run_monitor → serialize_report` — never `run_hedge_fund` or any order path.

## Educational-disclaimer invariant

Every product output path carries the disclaimer, enforced at **two layers**:

1. **Serialization** — `serialize_report` raises `DisclaimerError` on a blank
   disclaimer/version (the single projection chokepoint; the `monitoring export`
   CLI routes through it).
2. **Database** — `opportunity_reports` and `serenity_research_records` carry a
   `CHECK(length(trim(disclaimer)) > 0 AND length(trim(disclaimer_version)) > 0)`
   (migration `c7e2f1a4b9d6`) in addition to NOT NULL, so a blank disclaimer is
   impossible even for a direct DB write that bypasses serialization.

The disclaimer suite verifies both layers plus a sqlite3 `.dump` round-trip (via
stdlib `iterdump`, no subprocess) and the export CLI. *Known gap (follow-up):* the
serenity GET projection (`GET /serenity/research/{ticker}`) still emits the
disclaimer directly rather than through a `serialize_serenity` chokepoint; the DB
CHECK already blocks a blank one at the DB layer.

## Counsel sign-off (release precondition)

Per PRD §13/§19, **counsel sign-off must be recorded before any shared (non-loopback)
exposure**. It is a *recorded* human verdict, not an automated test:

```python
from src.evals.reporting import record_signoff, signoff_recorded
record_signoff("evals_runs/signoff.jsonl", reviewer="counsel", notes="...", approved=True)
assert signoff_recorded("evals_runs/signoff.jsonl")
```

Its absence is reported as a release-blocker; it never makes a suite "pass".
