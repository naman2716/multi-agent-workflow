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

## Phase 2: one commit per fix, each with tests (after approval)
- [ ] 2.1 Groom sizing: `sizing_rules` in the groom skill, est. column; missing-estimate warning
      in `build_tasks`; `[planning] require_estimates`; `harness.stale_files` warning at
      `approve`/`run`; README (harness refresh)
- [ ] 2.2 Scope from decisions: `scope_add` in `WORK_SCHEMA` and the worker prompt; path
      cleaning in `_ask`; shown in terminal, ntfy and `orchestra decisions`; `Store.answer(pick=,
      scope_add=)`; `parse_answer` returns the pick; `answer --scope-add`; `orchestra scope`
- [ ] 2.3 `orchestra retry` resets `triaged` (`--keep-triage` to keep it)
- [ ] 2.4 `orchestra/cards.py`: `edit_card`, `split_card` (moved from `_triage_split`),
      `serialize_against_open` (moved); `orchestra edit`, `orchestra split`; triage uses them
- [ ] 2.5 `Git.sync_integration`: create / no-op / fast-forward / diverged (squash-merged hint);
      called by `run`, `plan`, `approve`, `add`; `--allow-diverged`; report-only under a live lease
- [ ] 2.6 Test writers: parent test patch for split children (code in full, data files by size);
      size hook with `[quality] test_max_lines`
- [ ] 2.7 `DATA_FILE_HINT` in worker, test writer, triage prompts and `sizing_rules`; README Tuning
- [ ] 2.8 `orchestra run --detach` (`start_new_session`, `.orchestra/run.log`); dispatch skill
      uses it; `ship_pending` meta resumes the ship step after a kill
- [ ] 2.9 `orchestra usage`: `decision` / `output_enforce` resume kinds; marked results are
      capped; human session out of the what-if; fold cancelled cards with no sessions; read
      `tasks-full.json` + `events.log`. 9b: reviewers and validators get the Bash cap
- [ ] 2.10 (if approved) 6b: `balanced` profile's test writer → Sonnet
- [ ] 2.11 (if approved) 10: `checked_beyond_tests` in `REVIEW_SCHEMA`, test-first reviewer note,
      soft finding counted by `orchestra usage`
- [ ] 2.12 README: Planning, Interactive side, Test-first cards, Self-healing, Tuning, Shipping
- [ ] 2.13 Full suite passes (apart from the 2 known macOS failures, if still present)
- [ ] 2.14 Smoke test in a `/tmp` throwaway repo with `python3.12 -m orchestra.cli`: `approve`
      warns on a missing estimate, `edit`, `split`, `scope`, integration fast-forward,
      `run --detach` writes `run.log`. Never against pcme_dashboard. No push.

## Phase 3: housekeeping (fix 11)
- [ ] 3.1 `orchestra usage` on a `/tmp` copy of the export → `spend-less-per-card/after.md`;
      compare with `baseline.md` (cost per merged card, cancelled-card spend, cold-start writes,
      peak context, first-round review pass rate)
- [ ] 3.2 Merge spend-less-per-card's deltas into `openspec/specs/`, then archive it
- [ ] 3.3 Merge commit-identity's deltas, then archive it (PR #6 merged)
- [ ] 3.4 After Phase 2 ships: merge these deltas, then archive wave-2-fixes
