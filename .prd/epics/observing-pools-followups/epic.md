---
prd: true
id: EPIC-20260704-observing-pools-followups
status: IN_PROGRESS
mode: loop
effort_level: Standard
created: 2026-07-04
updated: 2026-07-08
iteration: 3
maxIterations: 32
loopStatus: failed
last_phase: VERIFY
failing_criteria: []
verification_summary: "7/7 ISC pass (iteration 2, VERIFY-first): TEST-1 1 passed / TEST-2 1 passed / TEST-3 8 passed / DOC-4 grep pass / DOC-5 25 passed / REGRESSION-6 full suite 811 passed / ANTI-7 all changed .py under tests/. Work shipped in commit 3b3bb75 on epic/observing-pools-followups."
parent: .prd/PRD-20260704-observing-pools-followups.md
children: []
name: observing-pools-followups
progress: 100%
github: null
lanes: [A, B, C]
---

# Epic: observing-pools-followups

## Overview

Close the four deterministic, non-blocking follow-ups deferred at the PR #68 merge: a
DELETE-preserves-referencing-reports test, a §19 README bind-host doc fix + doc-guard,
a GET no-commit assertion, and a signoff no-op side-effect assertion. All four are
test/doc hardening with zero production behavior change — deliberately loop-safe so a
detached Algorithm loop can self-grade each one deterministically.

## Architecture Decisions

- **mode: loop** — every criterion is deterministically verifiable (pytest exit code /
  grep), design is settled, no new public API, no UX. Rejected `mode: interactive`: there
  are no taste calls here.
- **§19 hardening = docs + doc-guard only** (Woo, 2026-07-04) — a socket-level startup
  check touches bind logic and belongs in human-gated review, not a detached loop. Rejected
  the socket-level variant for this epic.
- **No production code changes** — enforced as an anti-criterion (ISC-ANTI-7). The loop may
  only add tests and edit READMEs; any `.py` change outside `tests/` fails the epic.

## Technical Approach

- Items 1 + 3 edit `tests/observing_pools/test_api_monitors.py` (shared file → one lane,
  sequential). Item 1 seeds a monitor, drives a refresh so an `opportunity_reports` row is
  persisted (reuse the existing `env`/analyzing-flow fixtures + report-persistence path used
  by the surrounding tests), soft-deletes the monitor, then re-reads the report and asserts
  it is still returned. Item 3 strengthens `test_get_single_monitor_is_read_only` to assert
  no commit (e.g. spy/patch `Session.commit`, or assert `updated_at`/row state is unchanged
  across the GET).
- Item 4 edits `app/backend/README.md:64` and `app/README.md:143` so the uvicorn examples
  set `SERVER_BIND_HOST=127.0.0.1` (and note that non-loopback deploys must set it), then
  adds a doc-guard test under `tests/docs/` (sibling of `test_prd_reconcile.py`) asserting
  both READMEs mention `SERVER_BIND_HOST`.
- Item 3-of-signoff edits `tests/observing_pools/test_signoff_gate.py` to assert the
  loopback/unset path has no side effects (no file created at the signoff path, env
  untouched), not merely that it does not raise.
- Runner is `.venv/bin/python -m pytest` (bare `python` not on PATH). Follow TDD: write the
  asserting test first, prove it meaningfully RED where possible, then satisfy it.

## Implementation Strategy

Three conflict-free lanes, all Wave 1 (no cross-lane dependency). Lane A serializes its two
tasks (same file). Simplest execution is a single loop session over `epic.md`; parallel lane
sessions are available for speed. Risk order is irrelevant (independent, tiny). Each task is
self-verifying via its ISC command.

## IDEAL STATE CRITERIA

- [ ] ISC-TEST-1: a test proves a referencing opportunity_report is still readable after its monitor is soft-deleted | Verify: Test: .venv/bin/python -m pytest tests/observing_pools/test_api_monitors.py -k "delete and report" -q
- [ ] ISC-TEST-2: the GET /monitors/{id} read-only test asserts no DB commit occurs | Verify: Test: .venv/bin/python -m pytest tests/observing_pools/test_api_monitors.py -k "read_only" -q
- [ ] ISC-TEST-3: the loopback/unset signoff no-op test asserts no side effects, not just non-raise | Verify: Test: .venv/bin/python -m pytest tests/observing_pools/test_signoff_gate.py -k "never_raise or no_op" -q
- [ ] ISC-DOC-4: both READMEs set SERVER_BIND_HOST in their uvicorn examples | Verify: Grep: grep -q SERVER_BIND_HOST app/backend/README.md && grep -q SERVER_BIND_HOST app/README.md
- [ ] ISC-DOC-5: a doc-guard test pins the README bind-host note against regression | Verify: Test: .venv/bin/python -m pytest tests/docs -q
- [ ] ISC-REGRESSION-6: the full test suite stays green | Verify: Test: .venv/bin/python -m pytest -q
- [x] ISC-ANTI-7: no production .py changed — every changed .py lives under tests/ | Verify: Bash: ! git diff --name-only main | grep -E '[.]py$' | grep -qvE '^tests/'

## Execution Plan

Lanes are conflict-free task groups (from `conflicts_with` + shared files). All three lanes
are Wave 1 (no cross-lane `depends_on`). One session per lane, or one loop over the epic.

### Lane A: monitors-api-tests

- Wave: 1
- Tasks: 001, 002
- File scope: `tests/observing_pools/test_api_monitors.py`
- depends_on lanes: (none)
- Worktree: `../epic-observing-pools-followups-lane-A`
- Branch: `epic/observing-pools-followups/lane-A`
- Session command:
  ```bash
  nohup bun ~/.claude/skills/PAI/Tools/algorithm.ts -m loop \
    -p "$(pwd)/.prd/epics/observing-pools-followups/lane-A.md" -n 32 --budget-minutes 180 \
    >/tmp/loop-epic-observing-pools-followups-lane-A.log 2>&1 &
  ```

### Lane B: signoff-gate-test

- Wave: 1
- Tasks: 003
- File scope: `tests/observing_pools/test_signoff_gate.py`
- depends_on lanes: (none)
- Worktree: `../epic-observing-pools-followups-lane-B`
- Branch: `epic/observing-pools-followups/lane-B`
- Session command:
  ```bash
  nohup bun ~/.claude/skills/PAI/Tools/algorithm.ts -m loop \
    -p "$(pwd)/.prd/epics/observing-pools-followups/lane-B.md" -n 32 --budget-minutes 180 \
    >/tmp/loop-epic-observing-pools-followups-lane-B.log 2>&1 &
  ```

### Lane C: readme-bind-host-doc

- Wave: 1
- Tasks: 004
- File scope: `app/backend/README.md`, `app/README.md`, `tests/docs/`
- depends_on lanes: (none)
- Worktree: `../epic-observing-pools-followups-lane-C`
- Branch: `epic/observing-pools-followups/lane-C`
- Session command:
  ```bash
  nohup bun ~/.claude/skills/PAI/Tools/algorithm.ts -m loop \
    -p "$(pwd)/.prd/epics/observing-pools-followups/lane-C.md" -n 32 --budget-minutes 180 \
    >/tmp/loop-epic-observing-pools-followups-lane-C.log 2>&1 &
  ```

### Merge Plan

Single integration step after all lanes complete:

```bash
git checkout -b epic/observing-pools-followups main
git merge epic/observing-pools-followups/lane-A
git merge epic/observing-pools-followups/lane-B
git merge epic/observing-pools-followups/lane-C
.venv/bin/python -m pytest -q      # full suite green before the PR
```

Conflicts pause-and-report — never `--force`. Merge to `main` stays a human gate (open a PR, do not auto-merge).

## Loop Execution

```bash
bun ~/.claude/skills/PAI/Tools/algorithm.ts lint -p .prd/epics/observing-pools-followups/epic.md
nohup bun ~/.claude/skills/PAI/Tools/algorithm.ts -m loop \
  -p "$(pwd)/.prd/epics/observing-pools-followups/epic.md" -n 32 --budget-minutes 180 \
  >/tmp/loop-epic-observing-pools-followups.log 2>&1 &
bun ~/.claude/skills/PAI/Tools/algorithm.ts status -p .prd/epics/observing-pools-followups/epic.md
```

## Task Breakdown Preview

- 001 — DELETE preserves referencing opportunity_reports (lane A, parallel: false)
- 002 — GET /monitors/{id} asserts no DB commit (lane A, parallel: false)
- 003 — signoff loopback/unset asserts no side effects (lane B, parallel: true)
- 004 — README SERVER_BIND_HOST hardening + doc-guard (lane C, parallel: true)

## Tasks Created
- [x] 001.md — DELETE preserves referencing opportunity_reports (lane A, parallel: false)
- [x] 002.md — GET /monitors/{id} asserts no DB commit (lane A, parallel: false)
- [x] 003.md — signoff loopback/unset asserts no side effects (lane B, parallel: true)
- [x] 004.md — README SERVER_BIND_HOST hardening + doc-guard (lane C, parallel: true)

Total tasks: 4
Parallel tasks: 2 (003, 004)
Sequential tasks: 2 (001→002, same file)
Lanes: 3   Waves: 1
Estimated total effort: ~2 hours

## Dependencies

None external. Depends only on PR #68 (already merged to `main` as `7c0c6d5`).

## Estimated Effort

- Total tasks: 4
- Waves: 1
- Estimated total effort: ~2 hours

## STATUS

| Metric | Value |
|--------|-------|
| Criteria passing | 7/7 |
| Tasks complete | 4/4 (001–004) |
| Full suite | 811 passed |
| Iteration | 2 of 32 |
| Status | COMPLETE — merge to main stays human-gated (open a PR, do not auto-merge) |

## LOG

### Iteration 2 — 2026-07-08 — phase reached: VERIFY — 7/7 criteria pass — status → COMPLETE

- OBSERVE found the branch `epic/observing-pools-followups` already carried commit
  `3b3bb75` ("close the 4 deferred PR #68 follow-ups (tests+docs only)") touching exactly
  the 5 expected files (`tests/observing_pools/test_api_monitors.py`,
  `tests/observing_pools/test_signoff_gate.py`, `tests/docs/test_readme_bind_host.py`,
  `app/backend/README.md`, `app/README.md`). Applied the VERIFY-first lesson: no
  re-implementation; ran every ISC verify command verbatim instead.
- Results (all collected >0 tests — no zero-collection false-greens):
  ISC-TEST-1 `-k "delete and report"` → 1 passed; ISC-TEST-2 `-k "read_only"` → 1 passed;
  ISC-TEST-3 `-k "never_raise or no_op"` → 8 passed; ISC-DOC-4 both greps pass;
  ISC-DOC-5 `tests/docs` → 25 passed; ISC-REGRESSION-6 full suite → 811 passed
  (808 at the #68 merge + the new hardening tests); ISC-ANTI-7 every changed `.py` vs
  main lives under `tests/` → pass.
- No Custom criteria exist, so nothing was skipped. Epic frontmatter set to
  status: COMPLETE, progress: 100%, loopStatus: complete.
- Context for next iteration / human: nothing left for the loop. Remaining step is the
  human-gated merge: open a PR from `epic/observing-pools-followups` to `main`
  (per Merge Plan — no auto-merge). Note iteration 1 evidently executed all three lanes'
  work in a single commit on this branch, so the per-lane worktree/merge choreography in
  the Execution Plan is moot.
