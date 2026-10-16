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
- [x] 2.11 Live smoke test (2026-10-15, throwaway repo, economy profile, $0.04): every cache write
      was 5-minute (0 one-hour); a 600k-char `python3 noise.py` reached the worker as 8,148 chars with
      head, tail and the truncation note, inside the sandbox; the size check reached the worker at
      62/40 lines and it reported blocked without trimming; sessions were recorded with roles and end
      reasons, and `orchestra usage` used them. Not exercised live: resumed-session cost deltas,
      the single validator, the oversize-to-triage path, the reviewer note order.

## Phase 2: measure and close
- [x] 3.1 `after.md` from the PCME waves 1.5 + 2 export (2026-10-16): cost per merged card
      $2.11 → $1.74; cancelled/blocked spend $6.08 (26%) → $4.22 (24%); cold-start writes per
      session unchanged (16k), 0 one-hour writes; agent peak context 145k → 133k; first-round
      review pass rate 82% → 90%. What's left went to `wave-2-fixes`
- [x] 3.2 Deltas merged into `openspec/specs/`; archived 2026-10-16
