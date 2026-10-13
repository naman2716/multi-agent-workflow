# Design: keep-good-work

## Flow of one attempt (orchestrator._execute)

```
worktree (+ slot env, clone_paths, setup_cmd unless skipped)
[test_first and no locked tests]  test-writer -> commit -> tests must fail -> lock
loop:
  worker (fresh / resume note / revise feedback / conflict note)
  gates (fix rounds in-session, changed_cmd first in fix rounds; locked tests restored first)
  squash card commits onto base
  review (+ validators on blocking findings)
    rejected & fixable & rounds left & session  -> revise feedback, loop
    rejected otherwise                          -> fresh attempt (as today)
  integrate under merge lock: rebase
    clean            -> re-gate if base moved -> CAS fast-forward -> merged
    conflict         -> rebase --abort; git merge --no-commit integration
                        clean merge  -> commit, squash, re-gate, fast-forward
                        markers      -> release lock; rounds left & session -> conflict note, loop
                                        (base := integration head, reviewer told it's a resolution)
                                        otherwise -> merge --abort, fresh attempt (as today)
```
Per attempt at most `1 + fix_rounds + conflict_rounds` reviews (default 3).

## 5. Revise in place
- `REVIEW_SCHEMA` gains required `fixable: bool` ("true if the problems can be fixed by editing
  this change; false if the approach is wrong"). Missing → treated as true.
- A contradictory approval (Set 1 override) counts as fixable.
- Revise message = `_review_feedback(...)` under "The reviewer found problems; fix them in this
  same change". The note is also added to feedback (so a later fresh attempt sees it).
- The next reviewer gets "This is a revision; the previous review found: …" in its prompt.
- Requires a worker session id; without one (e.g. it was lost) the old fresh-attempt path runs.

## 6. Squash and conflicts (git-integration)
- `Git.squash_onto(wt, base, message)`: `git reset --soft base && git commit`. Called after
  gates pass, so every card is one commit on top of its base; rebase can only conflict on the
  card's net change.
- `Git.start_merge(wt, ref)`: `git merge --no-ff --no-commit ref`; returns conflicted files
  (`git diff --name-only --diff-filter=U`), `[]` for a clean merge, `None` on other errors.
- During resolution the worker only edits files (guard unchanged); `commit_all` then records a
  merge commit, gates run against the new base (three-dot diff = the card's net change), and
  `squash_onto(new base)` linearizes it.
- New deterministic gate `conflict_markers`: added lines starting with `<<<<<<< `,
  `>>>>>>> ` or equal to `=======` fail the gate.
- Holding the merge lock only for git operations, never while an agent works.

## 7. Reviewer
- Tools: Read, Glob, Grep, plus Bash when `[review].reviewer_shell` (default true), sandboxed with
  the worker's sandbox settings. New guard profile `reviewer`: no Write/Edit tools, Bash checked
  like a worker (git state, outside writes, secret reads). After every review/validation the
  orchestrator runs `discard_uncommitted`.
- `pick_reviewer_model(cfg, worker_model)`: with `reviewer_rule = "opposite"`, if the profile
  reviewer is the same family as the worker (opus/sonnet/haiku by name), use the other of
  `opus`/`sonnet`. Balanced profile: Sonnet worker → Opus reviewer, Opus worker → Sonnet.
- Validators (`runner.validate`), `VALIDATE_SCHEMA = {confirmed, evidence}`: one per blocking
  finding (max `[review].max_validations`, default 5; extra findings stay), run with
  `asyncio.gather`. A finding is dropped only when the validator returns `confirmed: false`;
  errors or uncertainty keep it. Refuted criteria become `met: true` with the validator's evidence.
  If no blocking finding remains, the verdict becomes approve. Outcomes go to the handoff file
  and the event log (`validate`). A usage limit during validation pauses at review stage.
- Model profiles gain `validator` (economy sonnet, balanced opus, quality opus).

## 8. Test-first
- `Task.test_first` (bool) and `Task.locked_tests` (list of paths); plan schema field
  `test_first`; planner prompt: flag cards that change money (pricing, fees, payouts), stored
  data (migrations, deletes) or auth/permissions. `orchestra test-first <ID> [--off]`.
- Test-writer (`runner.write_tests`, model role `test_writer`: economy sonnet, balanced/quality
  opus), worker tools and guard, `TESTS_SCHEMA = {status: done|blocked, test_files, covered:
  [{criterion, test}], assumptions, blocker}`. It must not ask questions: ambiguous criteria are
  written for the clearest reading and listed under assumptions.
- Orchestrator checks every changed file matches `[quality].test_globs` (defaults cover
  `test_*.py`, `*_test.py`, `tests/`, `test/`, `__tests__/`, `*.test.*`, `*.spec.*`, `*_test.go`,
  `*_test.dart`); otherwise one in-session retry, then the attempt fails.
- Commit as `<ID>: acceptance tests`; pin with ref `refs/orchestra/tests/<ID>` (so squashes don't
  lose it); save the patch to `.orchestra/handoffs/<ID>.tests.patch` for later attempts (re-applied
  with `git apply`; if it no longer applies, the test-writer runs again).
- Run the gates once: if every gate passes, log a warning ("acceptance tests already pass; they may
  not test the new behaviour") and tell the reviewer. Lint failures in the new tests are fine here.
- Implementer card: `## Acceptance tests (written first; locked)` with files and coverage.
  Guard blocks Write/Edit/NotebookEdit on locked paths and shell redirects into them. Before every
  gate run the orchestrator restores locked files from the pinned ref and, if anything changed,
  notes it in feedback. The ref is deleted when the card merges or is cancelled.

## 9. Environment
- Slots: lowest free number 1..concurrency per running card (in-memory; freed in `finally`).
- `slot_env(cfg, task, slot)`: `ORCHESTRA_TASK`, `ORCHESTRA_SLOT`, `ORCHESTRA_PORT_BASE =
  port_base + slot * port_span` (defaults 41000, 100), `ORCHESTRA_DB_SUFFIX = _orch<slot>`, plus
  `[env.vars]` values with `{slot}`, `{port_base}`, `{task}` substituted. Passed to setup, gates,
  Stop-hook gates, and every agent's `env`.
- `clone_paths`: for each path present in your checkout and absent in the worktree, macOS
  `cp -c -R`, Linux `cp -R --reflink=auto`; failures are logged and ignored. With
  `skip_setup_if_cloned` (default true), `setup_cmd` is skipped when every clone path was cloned
  and every root lockfile (`diff_exclude` names) is byte-identical in checkout and worktree.
- `Gate.changed_cmd` (optional) with `{files}` = shell-quoted changed files: in fix rounds,
  run it first; if it fails, report that failure; if it passes, run the full `cmd`.

## 10. Planning
- `Git.detached_worktree(name, ref)`: create or move `<worktree_root>/_plan` to the integration
  head. `orchestra plan` explores there (the graph worktree is still used when the graph is on).
- Critic (`runner.critique_plan`, planner model, read-only + read guard), `CRITIC_SCHEMA = {verdict:
  ok|revise, issues: [{task_key, problem, fix}], plan}`. If `revise` and the revised plan passes
  `build_tasks`, it replaces the draft; issues are printed under "Critic". Failures fall back to the
  draft. `[planning].critic = true`.
- `build_tasks(..., root=)`: warn for literal scope paths whose folder doesn't exist under `root`.
- `Store.claim_ready` orders ready cards by number of open cards that depend on them
  (transitively), then by sequence.

## Harness
- `/groom` plan format and rules include `test_first`. The interactive reviewer agent gets the
  updated prompt (it already has Bash for read-only git).

## Testing
- FakeRunner gains `write_tests`, `validate`, `critique_plan`, and session ids.
- End-to-end tests: revise-in-place merges without a fresh attempt; non-fixable reject restarts;
  conflict resolved in-session and merged as one commit; conflict with no rounds restarts; validator
  refutes a finding → approve, confirms → reject; test-first: tests committed, locked file edits
  restored, tests reused on retry; slot env visible to gates; clone + setup skip; changed_cmd
  short-circuit; critic replaces plan; critical-path order; reviewer model rule; conflict-marker gate.
- Full suite with graphify; real-SDK option construction check for the new roles.
