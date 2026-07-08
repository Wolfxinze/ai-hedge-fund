---
prd: true
id: EPIC-20260704-observing-pools-followups-lane-C
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
name: observing-pools-followups-lane-C
progress: 0%
github: null
lanes: []
---

# Lane C: readme-bind-host-doc

## Scope

- Tasks: 004
- Files this lane owns: `app/backend/README.md`, `app/README.md`, `tests/docs/` (new guard test)

Only touch files in this lane's scope. Lanes A and B edit other files concurrently. Do NOT edit
`src/compliance.py` or `app/backend/main.py` — this lane is docs + a doc-guard test only.

## IDEAL STATE CRITERIA

- [ ] ISC-C1: both READMEs set SERVER_BIND_HOST in their uvicorn examples | Verify: Grep: grep -q SERVER_BIND_HOST app/backend/README.md && grep -q SERVER_BIND_HOST app/README.md
- [ ] ISC-C2: a doc-guard test pins the README bind-host note against regression | Verify: Test: .venv/bin/python -m pytest tests/docs -q
- [ ] ISC-C-ANTI: no production .py changed — only tests/ and READMEs | Verify: Bash: ! git diff --name-only main | grep -E '[.]py$' | grep -qvE '^tests/'

## Loop Execution

```bash
bun ~/.claude/skills/PAI/Tools/algorithm.ts lint -p .prd/epics/observing-pools-followups/lane-C.md
nohup bun ~/.claude/skills/PAI/Tools/algorithm.ts -m loop \
  -p "$(pwd)/.prd/epics/observing-pools-followups/lane-C.md" -n 32 --budget-minutes 180 \
  >/tmp/loop-epic-observing-pools-followups-lane-C.log 2>&1 &
bun ~/.claude/skills/PAI/Tools/algorithm.ts status -p .prd/epics/observing-pools-followups/lane-C.md
```

## Coordination

- Worktree: `../epic-observing-pools-followups-lane-C`
- Branch: `epic/observing-pools-followups/lane-C` (from up-to-date main)
- Commit format: `Epic observing-pools-followups/004: <desc>` (no GitHub sync).
- Do NOT touch files outside this lane's scope — lanes A/B edit them concurrently.
- Never `--force`. On conflict, pause and report; the parent owns the merge.
