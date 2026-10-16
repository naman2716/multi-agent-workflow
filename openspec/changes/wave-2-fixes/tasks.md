# Tasks: wave-2-fixes

## Phase 0: environment checks (done 2026-10-16)
- [x] 0.1 The live install is editable. `python3` on PATH is `/usr/bin/python3` 3.9.6 and can't
      import orchestra. The `orchestra` script (`~/.local/bin/orchestra`) runs
      `~/tools/.venv/bin/python3.12`, and `pip show orchestra` there gives 0.4.0,
      "Editable project location: /Users/namanraghuvanshi/tools/orchestra". The finder maps
      `orchestra` → `~/tools/orchestra/orchestra`.
- [x] 0.2 The worktree `~/tools/orchestra-wave-2-fixes` on `wave-2-fixes` is at `db0f598`, which
      is `origin/main` (checked with `git ls-remote origin main`). Created before this session.
- [x] 0.3 From the worktree root, both `python3` and `~/tools/.venv/bin/python3.12` import
      `…/orchestra-wave-2-fixes/orchestra/__init__.py`. The cwd comes before the editable finder.
      Tests use `~/tools/.venv/bin/python3.12` (3.12.15), since 3.9 is below 3.11.
- [x] 0.4 Baseline: `python3.12 -m unittest discover -s tests -t .` ran 162 tests with 2 failures,
      `test_guard` and `test_interactive_profile`. These are the macOS `/private/var` failures
      already known from spend-less-per-card.
- [x] 0.5 Unmerged branches: `plan-from-file` (`9b94dd4`) and `guard-symlink-paths` (`fc3e0ca`)
      are both based on `db0f598`. Expected conflicts: `orchestra/cli.py` and `README.md` with
      plan-from-file. None with guard-symlink-paths, which changes only `guards.py`.

## Phase 1: proposal (this folder)
- [x] 1.1 Evidence checked against the export (event ids, sessions.json, transcripts, handoff
      patches), with corrections to the brief listed in `proposal.md`
- [x] 1.2 `proposal.md`, `design.md`, spec deltas
- [x] 1.3 Approval (2026-10-16): "build fixes 1–9b in the proposed order, one commit per fix.
      Hold 6b and 10. Fix 5: stop on diverged, no fetch."

## Phase 2: one commit per fix, each with tests (approved 2026-10-16)
Tests: `tests/test_wave2_fixes.py` (49 tests), plus two deliberate assertion changes in
`tests/test_spend_less.py` (test writers and reviewers now get the size check and Bash cap).
Full suite after every commit: 209 tests, only the 2 known macOS failures.
- [x] 2.1 `36dc96e` Groom sizing: `sizing_rules` and an est. column in the groom skill;
      missing-estimate warning in `build_tasks`; `[planning] require_estimates`;
      `harness.stale_files` note at `approve`/`run`; README (harness refresh)
- [x] 2.2 `4045e6b` Scope from decisions: `scope_add` in `WORK_SCHEMA` and the worker prompt;
      path cleaning in `_ask`; shown in terminal, ntfy and `orchestra decisions`;
      `Store.answer(pick=, scope_add=)` infers the pick from the answer text (design updated);
      `answer --scope-add`; `orchestra scope`; scope helpers in `orchestra/cards.py`
- [x] 2.3 `a20e510` `orchestra retry` resets `triaged` (`--keep-triage` keeps it)
- [x] 2.4 `a4579e4` `cards.edit_card`, `cards.split_card` (triage's split moved there; a human
      split may reach outside scope with a note, and its children can be triaged);
      `orchestra edit`, `orchestra split`
- [x] 2.5 `f6efbe2` `Git.sync_integration`: create / no-op / fast-forward (CAS) / diverged
      (squash-merged hint); `run`, `plan`, `approve`, `add`; `--allow-diverged`; report-only
      under a live lease or when the branch is checked out; no fetch. `approve`/`add` don't
      create a missing integration branch (`run` and `plan` do)
- [x] 2.6 `da16b62` Test writers: split children get the parent's test code (data files by
      name and size only); size hook against `[quality] test_max_lines`
- [x] 2.7 `a731e8a` `DATA_FILE_HINT` in worker, test-writer and triage prompts; sizing rules
      flag copied data; README Tuning
- [x] 2.8 `9a1271d` `orchestra run --detach` (`start_new_session`, `.orchestra/run.log`, same
      code via `PYTHONPATH`); dispatch skill uses it; `ship_pending` re-ships after a kill
- [x] 2.9 `6c02f70` `orchestra usage`: `decision` / `output_enforce` resumes; capped results
      and interactive sessions out of the cap what-if; idle cancelled cards folded;
      `tasks-full.json` + `events.log`. On a `/tmp` copy of the PCME export: 2 decision resumes,
      19 agent results over the cap (1 Bash, 18 Read), 53 already capped, 4 interactive,
      T01–T14 folded, as the proposal predicted
- [x] 2.9b `5dcfe2f` Reviewers and validators get the Bash output cap
- [ ] 2.10 6b (Sonnet test writers): **held** by Naman
- [ ] 2.11 10 (`checked_beyond_tests`): **held** by Naman
- [x] 2.12 README: Planning, Interactive side, decisions, card surgery, Test-first cards,
      Self-healing, Tuning, Shipping, usage
- [x] 2.13 Full suite: 209 tests, 2 failures (`test_guard`, `test_interactive_profile`, the known
      macOS `/private/var` ones; `guard-symlink-paths` likely fixes them)
- [x] 2.14 Smoke test (2026-10-16), `python3.12 -m orchestra.cli` from the worktree against
      `/tmp/wave2-smoke/app`, no agents and $0:
      - `approve` warned for the 2 unestimated cards; `require_estimates` refused (exit 1);
      - `edit --depends-on` worked and a cycle was refused; `scope --add` worked and `../evil.py`
        was refused; `split` rewired the dependent T02 to both children;
      - `add` fast-forwarded integration after a commit on main, and a diverged branch stopped
        with commit counts (exit 1);
      - `run --detach` returned at once, and the child (the worktree's code) logged the
        stale-harness note, re-shipped after a simulated kill ("no git remote" in a throwaway
        repo) and cleared `ship_pending`.
      Not exercised live: real agents (scope_add from a real worker, the test writer's size
      check and parent tests in a real session). No push.
- Merge check (`git merge-tree`, read-only): `guard-symlink-paths` merges cleanly. `plan-from-file`
  conflicts in one hunk each of `orchestra/cli.py` (`cmd_plan`'s goal message vs the new
  `_sync_integration` call) and `README.md` ("Long goals" vs "The integration branch follows the
  base branch"). Keep both sides in each.

## Phase 3: housekeeping (fix 11, done 2026-10-16)
- [x] 3.1 `0fb6b57` `orchestra usage` on a `/tmp` copy of the export (checked identical with
      `diff -rq`) → `spend-less-per-card/after.md`, compared with `baseline.md`:
      - cost per merged card $2.11 → $1.74;
      - cancelled/blocked spend $6.08 (26%) → $4.22 (24%);
      - cold-start writes 16k per session in both; 1-hour writes 1.66M → 0; mid-session
        re-writes 141k (≈ $0.55);
      - agent peak context 145k → 133k;
      - first-round review pass rate 82% → 90%.
- [x] 3.2 `0fb6b57` spend-less-per-card deltas merged into `openspec/specs/`, archived as
      `archive/2026-10-16-spend-less-per-card`. Its task 2.9b (README "Keeping your own session
      cheap") is still open, awaiting a decision.
- [x] 3.3 commit-identity deltas merged, archived as `archive/2026-10-16-commit-identity`
- [ ] 3.4 After Phase 2 ships: merge these deltas, then archive wave-2-fixes
