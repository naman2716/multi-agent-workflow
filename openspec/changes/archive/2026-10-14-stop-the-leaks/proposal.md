# Proposal: stop-the-leaks (review recommendations 1–4)

## Why

The Oct 2026 review found bugs that silently stop runs, lose human decisions, merge work the
reviewer flagged, or let agents act outside their worktree. Ten are reproduced in
`review/test_review_findings.py`. This change closes them before any larger feature work.

## What changes

1. **Bug fixes (rec 1):**
   - Budget applies per `orchestra run`; lifetime spend is still shown (bug 1).
   - Answered decisions live in their own field and always appear on the card; stale gate
     logs no longer crowd the card (bug 2).
   - An "approve" with unmet criteria, blocker/major issues or scope creep is treated as a
     reject (bug 3).
   - Lockfiles and other configured generated files don't count toward `max_diff_lines` (bug 6).
   - The secret scan reads the whole diff, not the 60k-char excerpt (bug 7).
   - Approving the same plan twice is refused; new cards are serialized against cards already
     queued or running (bug 8).
   - Glob scopes are compared by their literal base, so `src/**/*.py` overlaps `src/api/x.py` (bug 9).
   - Gate and setup commands run in their own process group and the whole group is killed on
     timeout (bug 11).
   - A missing review report no longer sends the internal resume marker to the worker (bug 12).
   - Added (not in rec 1, trivial): `retry` refuses merged cards, `cancel` refuses cards a live
     run is working on, both overridable with `--force` (bug 13).
2. **Run lease and resumable recovery (rec 2):** one live `orchestra run` per repo, enforced by a
   heartbeat lease in SQLite. After a crash or Ctrl-C, a card with its worktree and session intact
   resumes its session (or resumes at review) instead of restarting (bugs 4, 10).
3. **Sandboxed agents (rec 3):** worker shell commands run in the Claude Code OS sandbox
   (Seatbelt on macOS) with writes limited to the worktree, temp and package caches; network
   limited to package registries and GitHub; `failIfUnavailable` on. All agents run with auto
   memory off. The regex guard stays as a second layer, tightened against the bypasses found,
   and blocks reading `.env` and credential files.
4. **Checks in the card (rec 4):** the card lists the exact gate commands the orchestrator will
   run. An opt-in Stop hook runs the gates marked `fast` before a worker can finish (off by
   default until smoke-tested against the live SDK).

## Out of scope

Recommendations 5–14 (revise-in-place, conflict resolution, reviewer upgrade, test-first cards,
PR automation, intake, etc.). They follow in later changes.

## Impact

- Modules: orchestration, agents, quality-gates, safety, planning, cli.
- New `orchestra.toml` keys: `[sandbox]` section, `[quality].diff_exclude`,
  `[quality].gates_on_stop`, per-gate `fast`. Existing configs keep working with defaults.
- SQLite: new `decisions` column (auto-migrated); answered decisions already stored as feedback
  notes are left as they are.
- Behaviour changes users will notice: a second concurrent `orchestra run` now exits with a
  message; `retry`/`cancel` may require `--force`; workers can no longer reach arbitrary hosts.
