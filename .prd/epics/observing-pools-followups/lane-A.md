---
prd: true
id: EPIC-20260704-observing-pools-followups-lane-A
status: DRAFT
mode: loop
effort_level: Standard
created: 2026-07-04
updated: 2026-07-04
iteration: 0
maxIterations: 32
loopStatus: null
last_phase: null
failing_criteria: []
verification_summary: ""
parent: .prd/epics/observing-pools-followups/epic.md
children: []
name: observing-pools-followups-lane-A
progress: 0%
github: null
lanes: []
---

# Lane A: monitors-api-tests

## Scope

- Tasks: 001, 002
- Files this lane owns: `tests/observing_pools/test_api_monitors.py`

Only touch files in this lane's scope. Lanes B and C edit other files concurrently. Tasks 001
and 002 share this one file — do them sequentially (they conflict), 001 then 002.

## IDEAL STATE CRITERIA

- [ ] ISC-A1: a test proves a referencing opportunity_report is still readable after its monitor is soft-deleted | Verify: Test: .venv/bin/python -m pytest tests/observing_pools/test_api_monitors.py -k "delete and report" -q
- [ ] ISC-A2: the GET /monitors/{id} read-only test asserts no DB commit occurs | Verify: Test: .venv/bin/python -m pytest tests/observing_pools/test_api_monitors.py -k "read_only" -q
- [ ] ISC-A3: the whole monitors-api test module stays green | Verify: Test: .venv/bin/python -m pytest tests/observing_pools/test_api_monitors.py -q
- [ ] ISC-A-ANTI: this lane changes only its test file — no production .py | Verify: Bash: ! git diff --name-only main | grep -E '[.]py$' | grep -qvE '^tests/'

## Loop Execution

```bash
bun ~/.claude/skills/PAI/Tools/algorithm.ts lint -p .prd/epics/observing-pools-followups/lane-A.md
nohup bun ~/.claude/skills/PAI/Tools/algorithm.ts -m loop \
  -p "$(pwd)/.prd/epics/observing-pools-followups/lane-A.md" -n 32 --budget-minutes 180 \
  >/tmp/loop-epic-observing-pools-followups-lane-A.log 2>&1 &
bun ~/.claude/skills/PAI/Tools/algorithm.ts status -p .prd/epics/observing-pools-followups/lane-A.md
```

## Coordination

- Worktree: `../epic-observing-pools-followups-lane-A`
- Branch: `epic/observing-pools-followups/lane-A` (from up-to-date main)
- Commit format: `Epic observing-pools-followups/001: <desc>` (no GitHub sync).
- Do NOT touch files outside this lane's scope — lanes B/C edit them concurrently.
- Never `--force`. On conflict, pause and report; the parent owns the merge.
