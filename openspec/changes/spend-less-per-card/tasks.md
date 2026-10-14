# Tasks: spend-less-per-card

## Phase 1: baseline and proposal
- [x] 1.1 `orchestra/pricing.py` (one price table, cache writes by TTL) and `orchestra usage <export-dir>`
      (`orchestra/usage.py`): de-duplicated usage, roles, per-card join with queue state, resume
      rounds, cold starts, tool-output what-if; table, `--markdown`, `--json`, `--csv`
- [x] 1.2 `tests/test_usage.py`
- [x] 1.3 `baseline.md` from the wave-1 zip; recorded total matches $30.41
- [x] 1.4 `proposal.md`, `design.md`
- [ ] 1.5 Approval of the fixes and their order

## Phase 2: one commit per fix, each with tests (after approval)
- [ ] 2.0 Count spend once: per-session cost deltas; budget and `run_spent()` use them
- [ ] 2.1 Size cards before dispatch: `estimated_lines`, limit-aware planner/critic prompts, plan check
- [ ] 2.8 Five-minute cache writes: `[agents] cache_ttl`, `FORCE_PROMPT_CACHING_5M` in `agent_env()`
- [ ] 2.2 Worker diff-size hook (80% / 100%); oversize diff_size failure goes straight to triage
- [ ] 2.3 One validator session per review; card → diff → findings
- [ ] 2.6 `sessions` table with role and end_reason; `orchestra usage` reads it
- [ ] 2.4 `bashOutputMaxChars` per project, after the real-CLI check that failing tests survive
- [ ] 2.5 Reviewer note after the diff
- [ ] 2.7 Context budget: no change unless after.md shows long sessions on right-sized cards
- [ ] 2.9 README: "Keeping your own session cheap"; optional end-of-run reminder
- [ ] 2.10 Spec deltas under `specs/` (agents, orchestration, cli) for the approved fixes

## Phase 2: measure and close
- [ ] 3.1 Next comparable wave → `after.md` with `orchestra usage`; compare cost per merged card,
      cancelled-card spend, cold-start writes, peak context, first-round review pass rate
- [ ] 3.2 Merge deltas into `openspec/specs/`, archive the change
