# OpenSpec in Orchestra

Spec-first workflow (OpenSpec convention, no CLI required).

- `openspec/specs/<module>/spec.md` — source of truth for how each module behaves today.
- `openspec/changes/<change>/` — one folder per proposed change: `proposal.md`, `design.md`,
  `tasks.md`, and `specs/<module>/spec.md` deltas (ADDED / MODIFIED / REMOVED requirements).
- When a change ships, its deltas are merged into `openspec/specs/` and the folder moves to
  `openspec/changes/archive/<YYYY-MM-DD>-<change>/`.

## Module map

| Module | Code | Covers |
| --- | --- | --- |
| orchestration | `orchestrator.py`, `store.py` | run loop, task lifecycle, budget, recovery, run lease |
| agents | `agents.py` | roles, prompts, task card, SDK options per role |
| quality-gates | `gates.py`, `gitops.run` | deterministic checks and how commands run |
| safety | `guards.py`, sandbox settings | containment of what agents can read, write and run |
| planning | `planning.py` | plan validation, ids, serialization of overlapping work |
| git-integration | `gitops.py` | worktrees, rebase, compare-and-swap merge |
| decisions | `asking.py`, decision flow | agents asking the human and resuming |
| usage-limits | `limits.py` | pause and resume on usage limits |
| graph | `graph.py` | Graphify code graph |
| harness | `harness.py` | Claude Code agents, skills and hooks |
| cli | `cli.py`, `config.py` | commands and `orchestra.toml` |
