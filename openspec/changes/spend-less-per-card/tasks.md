# Tasks: spend-less-per-card

## Phase 1: baseline and proposal
- [x] 1.1 `orchestra/pricing.py` (one price table, cache writes by TTL) and `orchestra usage <export-dir>`
      (`orchestra/usage.py`): de-duplicated usage, roles, per-card join with queue state, resume
      rounds, cold starts, tool-output what-if; table, `--markdown`, `--json`, `--csv`
- [x] 1.2 `tests/test_usage.py`
- [x] 1.3 `baseline.md` from the wave-1 zip; recorded total matches $30.41
- [x] 1.4 `proposal.md`, `design.md`
- [x] 1.5 Approval: fixes 0, 1, 8, 2, 3, 6, 4, 5 in that order; fix 7 no change

## Phase 2: one commit per fix, each with tests (after approval)
- [x] 2.0 Count spend once: per-session cost deltas; budget and `run_spent()` use them
- [x] 2.1 Size cards before dispatch: `estimated_lines`, limit-aware planner/critic prompts, plan check
- [x] 2.8 Five-minute cache writes: `[agents] cache_ttl`, `FORCE_PROMPT_CACHING_5M` in `agent_env()`
- [x] 2.2 Worker diff-size hook (80% / 100%); oversize diff_size failure goes straight to triage
- [x] 2.3 One validator session per review; card → diff → findings
- [x] 2.6 `sessions` table with role and end_reason; `orchestra usage` reads it
- [x] 2.4 Bash output cap per project. The CLI's own limit keeps only the head (checked in its code), so a
      PreToolUse hook rewrites worker commands to return head + tail instead; tested in bash and zsh
- [x] 2.5 Reviewer note after the diff
- [x] 2.7 Context budget: no change (approved); revisit if after.md shows long sessions on right-sized cards
- [x] 2.9a README: sizing, validators, budget, [agents] settings, `orchestra usage`
- [ ] 2.9b README: "Keeping your own session cheap" (not in the approved list; awaiting a decision)
- [x] 2.10 Spec deltas under `specs/` (orchestration, agents, planning, quality-gates, cli)

## Phase 2: measure and close
- [ ] 3.1 Next comparable wave → `after.md` with `orchestra usage`; compare cost per merged card,
      cancelled-card spend, cold-start writes, peak context, first-round review pass rate
- [ ] 3.2 Merge deltas into `openspec/specs/`, archive the change
