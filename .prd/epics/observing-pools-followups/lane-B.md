---
prd: true
id: EPIC-20260704-observing-pools-followups-lane-B
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
name: observing-pools-followups-lane-B
progress: 0%
github: null
lanes: []
---

# Lane B: signoff-gate-test

## Scope

- Tasks: 003
- Files this lane owns: `tests/observing_pools/test_signoff_gate.py`

Only touch files in this lane's scope. Lanes A and C edit other files concurrently.

## IDEAL STATE CRITERIA

- [ ] ISC-B1: the loopback/unset signoff no-op test asserts no side effects, not just non-raise | Verify: Test: .venv/bin/python -m pytest tests/observing_pools/test_signoff_gate.py -k "never_raise or no_op" -q
- [ ] ISC-B2: the whole signoff-gate test module stays green | Verify: Test: .venv/bin/python -m pytest tests/observing_pools/test_signoff_gate.py -q
- [ ] ISC-B-ANTI: this lane changes only its test file — no production .py | Verify: Bash: ! git diff --name-only main | grep -E '[.]py$' | grep -qvE '^tests/'

## Loop Execution

```bash
bun ~/.claude/skills/PAI/Tools/algorithm.ts lint -p .prd/epics/observing-pools-followups/lane-B.md
nohup bun ~/.claude/skills/PAI/Tools/algorithm.ts -m loop \
  -p "$(pwd)/.prd/epics/observing-pools-followups/lane-B.md" -n 32 --budget-minutes 180 \
  >/tmp/loop-epic-observing-pools-followups-lane-B.log 2>&1 &
bun ~/.claude/skills/PAI/Tools/algorithm.ts status -p .prd/epics/observing-pools-followups/lane-B.md
```

## Coordination

- Worktree: `../epic-observing-pools-followups-lane-B`
- Branch: `epic/observing-pools-followups/lane-B` (from up-to-date main)
- Commit format: `Epic observing-pools-followups/003: <desc>` (no GitHub sync).
- Do NOT touch files outside this lane's scope — lanes A/C edit them concurrently.
- Never `--force`. On conflict, pause and report; the parent owns the merge.
