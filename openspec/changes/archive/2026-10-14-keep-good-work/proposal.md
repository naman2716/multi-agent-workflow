# Proposal: keep-good-work (review recommendations 5–10)

Builds on `stop-the-leaks` (branch `keep-good-work` is cut from it).

## Why

Today a reviewer rejection or a rebase conflict deletes the worktree and starts the card over,
the reviewer can't run anything and is often the same model as the worker, the worker writes
the tests that prove its own work, parallel gates share ports and databases, and plans are
drafted once against your checkout rather than the code workers start from.

## What changes

5. **Revise in place.** The reviewer says whether its findings are `fixable` in the current
   change. Fixable rejections go back into the worker's own session (default 1 round), then gates
   and a fresh review run again. A fresh attempt only when the approach is wrong or rounds run out.
6. **Agent-resolved conflicts.** Each card's commits are squashed into one after its gates pass.
   If the rebase onto integration conflicts, the orchestrator merges integration into the
   worktree, the worker resolves the conflict markers in its session (default 1 round), gates
   re-run (a new gate rejects leftover conflict markers), a reviewer checks the resolution, and
   the card merges as a single commit. A clean merge needs no agent.
7. **Stronger reviewer.**
   - Can run tests and small scripts: sandboxed Bash, no file edits, anything it changes is discarded.
   - Model is the opposite of the worker's: Sonnet work is reviewed by Opus, Opus work by Sonnet
     (`[review].reviewer_rule = "opposite"`, or `"profile"` for today's behaviour).
   - Before a rejection counts, each blocking finding (unmet criterion, blocker/major issue,
     scope creep; up to 5) is checked by a validator agent in parallel. Findings it refutes with
     evidence are dropped; if none remain, the card is approved. Validators run only on rejections.
8. **Test-first cards.** Cards flagged `test_first` (the planner flags money, data and auth work;
   you can flag or unflag any card) first get a test-writer agent that writes failing acceptance
   tests for each criterion. Those files are locked: the implementer's guard blocks edits to them,
   and the orchestrator restores them before every gate run. The tests are reused on later attempts.
9. **Per-worktree environment.**
   - Every running card gets a slot: `ORCHESTRA_SLOT`, `ORCHESTRA_PORT_BASE`, `ORCHESTRA_TASK`,
     `ORCHESTRA_DB_SUFFIX`, plus your own templated variables (e.g. a per-slot `DATABASE_URL`).
     They reach `setup_cmd`, gates and all agents of that card.
   - `clone_paths` (e.g. `node_modules`) are copied from your checkout with copy-on-write
     (APFS clone on macOS), and `setup_cmd` is skipped when the lockfiles match.
   - A gate can define `changed_cmd` (e.g. `pytest {files}`) that runs first in fix rounds; the
     full command still has to pass.
10. **Planner upgrades.** Plans are drafted in a read-only worktree at the integration head; a
   critic agent reviews each plan and returns a corrected one with its notes; scope paths whose
   folder doesn't exist are flagged; ready cards are dispatched longest-dependency-chain first.

## Out of scope

Recommendations 11–14 (self-healing blocked cards, PR automation, two-way decisions, intake)
and 15–17 (tracing, golden eval set, best-of-N).

## Impact

- Modules: orchestration, agents, quality-gates, safety, planning, cli, harness.
- New config: `[review]` (`fix_rounds`, `conflict_rounds`, `validate`, `reviewer_rule`,
  `reviewer_shell`), `[env]` (`port_base`, `port_span`, `[env.vars]`), `[quality].clone_paths`,
  `skip_setup_if_cloned`, `test_globs`, per-gate `changed_cmd`, `[planning].critic`; new model
  roles `validator` and `test_writer` in the profiles.
- SQLite: `test_first`, `locked_tests` columns (auto-migrated).
- CLI: `orchestra test-first <ID> [--off]`.
- Cost: validators run only on rejections; test-writer only on flagged cards; the critic once
  per plan. Revise-in-place and conflict resolution replace full reruns, so typical cost per
  merged card should fall.
- History on `orchestra/integration` becomes one commit per card.
