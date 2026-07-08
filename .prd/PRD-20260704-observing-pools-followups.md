---
prd: true
id: PRD-20260704-observing-pools-followups
status: DRAFT
mode: interactive
effort_level: Standard
created: 2026-07-04
updated: 2026-07-04
iteration: 0
maxIterations: 128
loopStatus: null
last_phase: null
failing_criteria: []
verification_summary: "0/0"
parent: null
children: []
---

# Observing Pools Followups

> Ship the four deterministic, non-blocking follow-ups deferred at the PR #68 merge
> (Observing Pools PRD deltas). All four are mechanical test/doc hardening — no
> production behavior change — chosen specifically because they can ship autonomously
> via the Algorithm loop with deterministic self-grading.

## STATUS

| What | State |
|------|-------|
| Progress | 0/7 criteria passing |
| Phase | DRAFT |
| Next action | Parse into epic → decompose → run loop in a new session |
| Blocked by | nothing (PR #68 merged; all four items verified still-missing) |

## CONTEXT

### Problem Space

PR #68 (merged to `main` as `7c0c6d5`) shipped the 10 Observing Pools PRD deltas. Two
review passes (7 agents) surfaced four **non-blocking** follow-ups that were deferred
rather than expand that PR's scope. Each is a coverage or documentation gap, not a bug:

1. **DELETE-preserves-referencing-reports test** — §14 soft-delete keeps the monitor row
   so referencing `opportunity_reports` survive. The row-survives mechanism is tested, but
   no test proves a *referencing report* is still readable after the monitor is deleted —
   the actual WHY of the soft delete.
2. **§19 README bind-host hardening** — the startup gate keys on `SERVER_BIND_HOST`, not
   uvicorn's actual `--host`. The READMEs document bare `uvicorn main:app --reload` with no
   `SERVER_BIND_HOST`, so a copy-paste non-loopback deploy could bypass the gate. Fix the
   docs + pin them with a doc-guard test. (Woo decision 2026-07-04: docs + doc-guard only —
   no socket-level startup check.)
3. **GET /monitors/{id} no-commit assertion** — the read-only test asserts the scheduler job
   is untouched, and the docstring claims "no commit", but nothing asserts the request issues
   no DB write.
4. **signoff byte-for-byte no-op assertion** — the loopback/unset gate test asserts non-raise
   and a comment claims "byte-for-byte unchanged", but nothing asserts the no-op path has no
   side effects.

### Key Files

- `tests/observing_pools/test_api_monitors.py` — items 1 (add test) + 3 (strengthen `test_get_single_monitor_is_read_only`).
- `tests/observing_pools/test_signoff_gate.py` — item 4 (strengthen `test_loopback_and_unset_never_raise`).
- `app/backend/README.md:64`, `app/README.md:143` — item 2 (uvicorn examples).
- `tests/docs/` — item 2 (new doc-guard test, alongside existing `test_prd_reconcile.py` / `test_encryption_runbook.py`).
- Runner: `.venv/bin/python -m pytest` (bare `python` is NOT on PATH).

### Constraints

- **No production behavior change.** Every change is a test addition or a doc/README edit.
  No `.py` under `src/` or `app/backend/{routes,services,scripts,alembic}` may change.
- Hard invariants unchanged: I1 (no trade/order/`risk_manager` in the scoring path), I6 (no
  secret read-back).
- Attribution disabled — no co-author/"Generated with" trailers. Never stage `.codex-run/`,
  `*.db.bak-*`, `uv.lock`, `.prd/` junk.
- Each new/strengthened test must be mutation-meaningful (fail if the asserted property regresses),
  not tautological.

### Decisions Made

- §19 hardening scope = **docs + doc-guard test only** (loop-safe; no bind-logic change). Woo, 2026-07-04.
- Epic scope = **only these 4 review follow-ups.** #21 residuals and the §11.2 risk-haircut are
  explicitly OUT (the haircut needs a quant decision, not a loop). Woo, 2026-07-04.

## PLAN

Four independent tasks. Items 1 & 3 share `test_api_monitors.py` (must serialize). Item 4 spans
the two READMEs + a new `tests/docs/` guard. Item 3-style TDD: write the failing assertion first,
prove it RED against current code where meaningful, then make it pass. No cross-task dependency —
all can be Wave 1. Ship as one loop session over the epic, or as parallel lane sessions.

## IDEAL STATE CRITERIA (Verification Criteria)

- [ ] ISC-TEST-1: a test proves a referencing opportunity_report is still readable after its monitor is soft-deleted | Verify: Test: .venv/bin/python -m pytest tests/observing_pools/test_api_monitors.py -k "delete and report" -q
- [ ] ISC-TEST-2: the GET /monitors/{id} read-only test asserts no DB commit occurs | Verify: Test: .venv/bin/python -m pytest tests/observing_pools/test_api_monitors.py -k "read_only" -q
- [ ] ISC-TEST-3: the loopback/unset signoff no-op test asserts no side effects (not just non-raise) | Verify: Test: .venv/bin/python -m pytest tests/observing_pools/test_signoff_gate.py -k "never_raise or no_op" -q
- [ ] ISC-DOC-4: both READMEs set SERVER_BIND_HOST in their uvicorn examples | Verify: Grep: grep -q SERVER_BIND_HOST app/backend/README.md && grep -q SERVER_BIND_HOST app/README.md
- [ ] ISC-DOC-5: a doc-guard test pins the README bind-host note against regression | Verify: Test: .venv/bin/python -m pytest tests/docs -q
- [ ] ISC-REGRESSION-6: the full test suite stays green | Verify: Test: .venv/bin/python -m pytest -q
- [ ] ISC-ANTI-7: no production .py changed — every changed .py lives under tests/ | Verify: Bash: ! git diff --name-only main | grep -E '[.]py$' | grep -qvE '^tests/'

_Loop-runnable check:_ `algorithm lint -p .prd/epics/observing-pools-followups/epic.md`

## DECISIONS

_Non-obvious technical decisions logged here during BUILD/EXECUTE._

## LOG

_Session entries appended during LEARN phase._
