# Design: wave-2-fixes

Event numbers (`#N`) and files refer to the PCME export described in `proposal.md`. Code
references are to `main` at `db0f598`. "Unconfirmed" marks a claim that needs a real run to check.

## 1. Groomed plans skip card sizing

**Cause.**
- `/groom` writes `plan.json` itself and never goes through the planner/critic schema, which is
  the only place `estimated_lines` is required (`agents.py:56`).
- The groom skill's rules say "aim under ~300 changed lines" (`harness.py:125`) but don't include
  `sizing_rules(cfg)`. The planner subagent file does include them (`harness.py:86`).
- `build_tasks` only warns when `est > split_at` (`planning.py:139-144`). `_estimate` turns a
  missing value into 0 (`planning.py:159-163`), so a missing estimate passes silently.
- In PCME it was worse. The committed skill (`fa9e43b`, 2026-10-14 17:23) predates PR #5
  (`7a3fe75`, merged 2026-10-15 01:44), so its JSON shape has no `estimated_lines` at all
  (`.claude/skills/groom/SKILL.md` in the PCME worktree). `orchestra init` never touches skills:
  it writes only `orchestra.toml` and `.gitignore` (`cli.py:117-135`). Skills come from
  `orchestra harness install`, which rewrites every file it manages (`harness.py:288-295`).
  PCME's were committed once and never refreshed.

**Evidence.**
- `plan.json` (14 cards) and `plan-a1-1b.json` (1 card) have no `estimated_lines` key.
- `tasks-full.json` stores 0 for T15–T29. T30–T33, made by triage, have 300, 360, 250 and 300;
  their gates measured 186, 245, 182 and 241 (#156, #163, #170, #177).
- T15 reported 621 lines (#70), T20 862 (#151) and T17 1,762 (#139).

**Change.**
- **Groom skill.** Step 3 gets `sizing_rules(cfg)` in place of "aim under ~300 changed lines".
  The same text then reaches planner, critic and groom: `estimated_lines` on every card, split
  above `split_at`, committed data counts unless `diff_exclude` covers it, copied JSON and
  fixtures are a sizing risk (fix 7 extends the data wording). Step 4's table gains an
  "est. lines" column, so the user sees the sizes while iterating.
- **`build_tasks`.** With `split_at` set, a card with no positive estimate gets one warning:
  `T05: no estimated_lines; size it before approving (limit 600, split above 420)`. Plain 0 counts
  as missing.
- **`[planning] require_estimates`.** New key, default `false`. When true, `approve` and `add`
  refuse a plan with any unestimated card. The refusal lists the cards and queues nothing.
  `plan` keeps its normal flow because the planner schema already requires the field. Triage
  splits stay warn-only, since refusing would block the run mid-flight.
- **Stale harness.** `harness.stale_files(cfg)` compares each file Orchestra manages (`.claude/agents/orchestra-*.md`,
  `.claude/skills/*/SKILL.md`) with what `build_files` would write now, for both graph
  settings. `orchestra approve` and `orchestra run` print one line when any differ:
  "harness files are older than this Orchestra (groom, dispatch); run `orchestra harness install`
  and commit the result". This needs no version stamp. A user-edited skill is reported too, and the
  wording covers that case.
- **Docs.** The README's "Interactive side" section explains that harness files are generated,
  usually committed, and refreshed with `orchestra harness install` after upgrading.

**Tests.**
- A plan with 3 cards (one missing, one 0, one estimated) produces exactly 2 missing-estimate
  warnings from `approve` and `add`.
- With `require_estimates = true`, both commands exit non-zero and queue nothing.
- The groom `SKILL.md` from `build_files` contains the limit, `estimated_lines`, `diff_exclude`
  and the split threshold.
- `stale_files` is empty right after `install_harness`, and names `groom` after the file is edited.

## 2. Answering a scope-widening decision doesn't widen `files_scope`

**Cause.**
- Decision options are plain strings (`WORK_SCHEMA`, `agents.py:73`).
- `Store.answer` records only the answer text (`store.py:413-428`).
- Nothing connects "widen scope" to `files_scope`.
- The scope gate (`gates.py:103`) and triage's split check (`orchestrator.py:1188-1193`) both read
  `task.files_scope`, so the answer can't take effect.

**Evidence.**
- #103 asks to edit `backend/app/models/core.py` and add an Alembic migration, with three string
  options.
- The groom transcript (line 255) runs `orchestra answer T17 --pick 1`, and #123 stores option 1's
  text.
- `files_scope` stayed at four globs: `backend/app/seed/**`, `backend/tests/seed/**`,
  `backend/tests/api/test_meta.py`, `backend/tests/contracts/reshape.py`.
- #140: "triage wanted to split, but the split reaches outside the card's files_scope (… backend/app/models/core.py…)".
- #141: blocked.

**Change.**
- **Schema.** `WORK_SCHEMA.decision` gains an optional `scope_add`: an array of
  `{option: int (1-based), paths: [str]}`. Options stay strings, so `render_question`, `ntfy` and
  `_print_decision` keep working. The worker prompt adds: "If an option needs files outside your
  files_scope, list them under scope_add for that option."
- **`_ask`.** It copies `scope_add` into the stored decision after cleaning the paths: relative
  only, no `..`, nothing under `.git/` or the state dir, at most 10 per option. Anything dropped
  is logged. Every place a question is shown adds "(adds to scope: …)" after that option, so the
  human sees exactly what a pick grants.
- **`Store.answer(task_id, answer, pick=None, scope_add=None)`.**
  - The paths are the picked option's `scope_add` plus any explicit `scope_add`.
  - They are merged into `files_scope` with no duplicates.
  - A `scope` event records "scope widened by answer: +…".
  - The resume note adds "Your files_scope now also includes: …".
  - If `files_scope` was empty (unrestricted), it stays empty.
- **Passing the pick through.** As built: `Store.answer` infers the pick from the answer text,
  so `parse_answer` keeps its signature. Every channel writes a pick as the option's text, alone
  or followed by " — note" (`asking.py:80-102`, `cli.py` `--pick`), and a recommendation equal
  to an option counts as that pick. The CLI also passes `--pick` explicitly. (The Phase 1 draft
  had `parse_answer` return the index; inferring it covers the terminal, ntfy and CLI paths
  without changing them.)
- **Ordering.** After widening, the card is serialized against open cards whose scope overlaps
  the new paths. This is the same rule triage's rewrite uses (`orchestrator.py:1151-1164`), moved
  into `cards.py` so the store-level answer path can call it.
- **Manual routes.**
  - `orchestra answer T17 --pick 1 --scope-add 'backend/app/models/core.py' 'backend/alembic/versions/**'`.
  - `orchestra scope T17 --add <glob>... [--remove <glob>...]` for a card that isn't waiting.
    It's the same operation as `orchestra edit --scope-add` (fix 4) and kept as a short alias.

**Tests.**
- A fake worker returns `needs_decision` with `scope_add` on option 1, and `--pick 1` is answered.
  - Expected: `files_scope` includes the paths and a `scope` event is logged.
  - A changed file under the new path then passes `run_gates`' scope check. It failed before.
  - A triage split whose cards use the widened path is accepted (`_triage_split` no longer raises).
- `--scope-add` with `..`, an absolute path or `.git/x` is refused.
- Picking an option with no `scope_add` leaves `files_scope` unchanged.

## 3. One triage per card gets used up on environment problems

**Cause.** `triaged` is set once (`orchestrator.py:1035`) and checked before every triage
(`orchestrator.py:1015`). `cmd_retry` resets status and attempts but not `triaged` (`cli.py:362`).

**Evidence.**
- T15's triage went on the stale-integration block (#38–41: "only on main (commit d1aa457)").
- After `orchestra retry T15` (#55, #58), its size block (#70) went straight to "needs a human"
  with no "triaging before it is blocked…" in between.
- The human session then split T15 by hand (groom transcript 217, 222).

**Change.**
- `cmd_retry` sets `triaged=False` and logs "re-queued by human (triage reset)". `--keep-triage`
  keeps the old behaviour.
- The same reset applies to children of a split. Today they are created with `triaged=True`
  (`orchestrator.py:1200`), which stops split → split loops. That stays, but a human retry now
  clears it.

**Tests.** A card is triaged and blocked, then retried with `cmd_retry`; when it blocks again,
triage runs a second time. With `--keep-triage`, it doesn't.

## 4. No CLI for manual card surgery

**Evidence.**
- Groom transcript line 222: Python `sqlite3` ran `update tasks set depends_on=?` (T29 →
  `["T15"]`, T16 → `["T15","T29"]`) and `update tasks set acceptance=?, spec=? where id='T15'`.
- Line 217 wrote `plan-a1-1b.json` with a heredoc, then ran `orchestra add`.

**Change.** A new `orchestra/cards.py` holds the operations, shared by triage, answers and the CLI.
- **`edit_card(store, id, depends_on=, scope_add=, scope_remove=, scope=, acceptance=, acceptance_add=, spec=, title=, non_goals=, force=False)`.**
  - Refuses `running` or `review` cards while a live lease exists, and `merged` cards, unless
    `force`. This matches "Manual commands respect live work".
  - `depends_on` must name existing cards and must not create a cycle (DFS as in `build_tasks`).
  - Changing scope re-serializes against open cards.
  - Changing acceptance or spec on a test-first card discards its locked tests, exactly as
    triage's rewrite does (`orchestrator.py:1144-1147`).
  - Every change logs an `edit` event and adds a feedback note, so the next worker sees it.
- **`split_card(store, id, items, why, split_at, handoff_dir)`.** This is `_triage_split`
  (`orchestrator.py:1176-1209`) moved out of `Orchestrator`.
  - Triage calls it with `source="triage"`, the CLI with `source="human"`.
  - The CLI path may reach outside the parent's scope; it warns but doesn't refuse.
  - Dependents are rewired, as for T21 → T30–T33.
  - Children inherit `test_first`, `source` and `model`, and get the parent's test patch (fix 6).
- **CLI.**
  - `orchestra edit T16 --depends-on T15,T29`, plus `--scope`, `--scope-add`, `--scope-remove`,
    `--acceptance-file`, `--acceptance-add`, `--spec-file` and `--force`.
  - `orchestra split T15 plan.json [--force]` reads the plan format (`{"tasks": [...]}` or a list).

**Tests.**
- Each edit flag changes the stored card and logs an event.
- A dependency cycle and an unknown id are refused.
- Running and merged cards are refused without `--force`.
- A manual split cancels the parent as "split into …", children depend on the parent's
  dependencies, and a dependent's `depends_on` is rewired to all children.
- Triage's existing split tests keep passing against the moved code.

## 5. Integration branch behind the base branch

**Cause.** `ensure_integration_branch` only creates the branch when it's missing
(`gitops.py:239-243`) and never compares it with base. The base branch is `[orchestra] base_branch`
(`config.py:267`, default `"main"`; the template writes the branch `init` found, `config.py:364`).

**Evidence.**
- #29: graph refreshed at `50422e72`.
- #39: "integration base (50422e7). They were added in commit d1aa457, which is on main only".
- #54: run finished, blocked 2, $0.36.
- Groom transcript 172: `git fetch . main:orchestra/integration`.
- #57: refresh at `d1aa457a`.

**Change.** `Git.sync_integration(allow_diverged=False) -> SyncResult` compares the local refs
`refs/heads/<base>` and `refs/heads/<integration>`:

| state | action |
|---|---|
| integration missing | create it from base (as today) |
| equal, or base is an ancestor of integration | nothing |
| integration is an ancestor of base | fast-forward with `update-ref <new> <old>` (compare-and-swap); log `integration fast-forwarded a1b2c3d → d4e5f6a (base main moved)` |
| diverged | stop: "orchestra/integration and main have diverged (3 commits only on integration, 2 only on main). Merge main into it, or reset it if its PR was squash-merged, then retry; or pass --allow-diverged to run on it as is." If `git merge-tree --write-tree` shows base already contains everything on integration, the message says it was probably squash-merged and gives the reset command. |

- **`run`** calls it at start, replacing `ensure_integration_branch` in `_run`
  (`orchestrator.py:316`).
- **`plan`** calls it through `cfg_integration` (`cli.py:62`).
- **`approve` and `add`** call it too. While a live run holds the lease, they only report the
  state and never move the ref.
- **Flags.** `--allow-diverged` on `run`, `plan` and `approve`.
- **Mid-run moves.** A fast-forward mid-run is safe: card merges already use compare-and-swap on
  the integration ref (`gitops.py:362-365`), and every card rebases onto the integration head
  before merging.

**Open question for Naman.**
- Should a diverged branch stop the run (the brief's ask, and what this design does) or be merged
  automatically when the merge is clean?
- Should the sync `git fetch` base from `origin` first? It doesn't today: no network, and PCME's
  case was a local `main`.

**Tests** (temp repos): up to date → no change; integration behind → fast-forwarded and logged;
diverged → `SyncError`, and with `allow_diverged` the run proceeds; squash-merged → the error
message names the reset; a live lease during `approve` → reported, ref untouched.

## 6. Test writers are the largest cost

**Evidence.**

| card | session | turns | tool calls | cost | test lines locked |
|---|---|---|---|---|---|
| T18 | 2ca520ac | 11 | 11 | $0.44 | 240 |
| T20 | 7a7124ba | 28 | 30 | $1.52 | 2,268 (JSON fixture 1,528) |
| T30 | 603ff5e7 | 10 | 11 | $0.46 | 277 |
| T31 | af90216b | 19 | 21 | $0.98 | 538 |
| T32 | e61f37a6 | 22 | 25 | $0.95 | 509 |
| T33 | 4c989271 | 19 | 29 | $1.26 | 671 |

- The six sessions total $5.60, 32.2% of $17.40.
- T33's worker (`e9fda2a6`) cost $0.61.
- T20's tests were discarded when triage split the card (#151–152).
- The children's test writers each started cold. T31–T33 re-derived the fixture generator one
  link of the chain at a time (#162, #169, #176).
- Locked tests don't count toward the size gate (`gates.py:91`), and the test writer gets no size
  hook (`agents.py:634-635`). Nothing told T20's test writer that 2,268 lines was too many.

**Change.**
- **Parent tests for split children.** When `split_card` splits a test-first card whose
  `handoffs/<parent>.tests.patch` exists, it records `tests_from=<parent>` on each child (a meta
  key `tests_from:<child>`). When the child's test writer runs, the prompt gets a section
  "Tests written for <parent> before it was split":
  - test code files from the parent patch in full, up to 20,000 chars;
  - data files (`.json`, `.csv`, `.yaml`, `.yml`, or any file over 300 changed lines) listed by
    path and size only, with "don't copy these wholesale; generate expected data at test time";
  - "Keep the tests that cover your card's criteria and adapt them; drop the rest".

  The patch isn't applied: it covers criteria the child doesn't have, and those tests would fail
  forever.
- **Size check for test writers.** `_editor_options` installs `make_size_guard` for the test writer
  too, with its own limit `[quality] test_max_lines` (default `max_diff_lines`) and its own
  message: "your tests are now N lines… cover each criterion once; generate large expected data at
  test time; if the card can't be tested within this, report blocked with how to split it". A
  blocked test writer already goes to triage (`orchestrator.py:670-672`). So an oversized
  test-first card gets split before the worker spends anything; for T20 that worker cost $1.49.
- **Proposal only (6b): Sonnet as the default test-writer model.** The `balanced`
  (default) and `quality` profiles use Opus for `test_writer`, and `economy` already uses Sonnet
  (`config.py:61-66`). PCME's sessions (Opus test writers, Sonnet reviewers) match `balanced`;
  its `orchestra.toml` wasn't checked. Re-pricing this run's six sessions at
  Sonnet rates (`pricing.py:10-11`; cache reads cost the same) gives **$5.60 → $3.29**, assuming
  the same turns. That assumption is unconfirmed, and so is the quality. If approved: change the
  `balanced` profile only, keep `quality` on Opus, and compare the first-round review pass rate on
  test-first cards in the next wave's `after.md`.

**Tests.**
- After a fake triage split of a test-first card with a saved patch, each child's test-writer
  prompt contains the parent's test code and lists the JSON fixture by name only.
- A child of a card with no patch gets no such section.
- The test writer's options include the size hook, set to `test_max_lines`.

## 7. Copied data files blow the diff limit

**Evidence.**
- #139: T17 "Attempt 1 produced 1,762 changed lines", about 1,500 of them from copying
  `handout_extra_sections.json` (one scalar per line) into `backend/app/seed/data/`, because the
  Dockerfile only copies `backend/app`.
- Triage proposed "a compact but identical form, with one table row per line… roughly 150–250
  lines" plus a test that it parses equal to the source.
- T20's test patch has a 1,528-line JSON fixture.

**Change.**
- One shared hint, `DATA_FILE_HINT`: "If a data file (JSON, CSV, fixtures, snapshots, seed data)
  would dominate the diff, don't commit it as is. Commit a compact form (one record per line)
  with a test that it round-trips to the source, or build it at test time, or, if it's truly
  generated, say so and suggest a `diff_exclude` entry."
- It is appended to `WORKER_APPEND`, `TEST_WRITER_APPEND` and `TRIAGE_SYSTEM`, and its planning
  form to `sizing_rules`.
- README Tuning: put generated data folders in `[quality].diff_exclude`, and keep committed
  examples hand-trimmed.

**Tests.** The prompt strings contain the hint, and the groom skill contains the planning form.

## 8. A run started from a Claude session dies with that session

**Evidence.**
- `run-output/bo9ktvxa6.output` ends "09:59:45 [orchestra] draft PR updated: …/pull/2; waiting
  up to 30 min for CI" followed by `[killed]`.
- #182 is that run's last event. There is no "run finished" or CI result.
- No card was left running (`tasks-full.json`: 15 cancelled, 10 merged, 7 pending, 1 blocked).

**Cause.**
- The dispatch skill says to start `orchestra run -j <N>` with `run_in_background`
  (`harness.py:153`). That ties the run to the Claude Code process.
- After a kill, recovery handles cards but not the ship step: `merged_before` is every merged card
  at start (`orchestrator.py:320`), so `_ship_and_fix` returns before shipping
  (`orchestrator.py:425-426`).
- With no pending cards, `cmd_run` exits before the run starts (`cli.py:278-282`).
- Nothing is lost, but the CI result, and any CI-fix card, never arrive.

**Change.**
- **`orchestra run --detach`.** It re-executes itself with the same arguments (minus `--detach`)
  through `subprocess.Popen(..., start_new_session=True, stdin=DEVNULL, stdout=<state>/run.log
  (append), stderr=STDOUT)`, then prints the pid and log path and exits 0.
  - This is portable to macOS, where there is no `setsid` binary. `nohup` alone would leave the
    run in Claude's process group.
  - The run lease already prevents a second run (`cli.py:272-275`).
  - A detached run has no TTY, so questions go through `orchestra decisions` / `/decide`, as with
    `/dispatch` today.
- **Dispatch skill.** Step 3 becomes `orchestra run --detach -j <N>`. Then read `.orchestra/run.log`
  (`tail -n 40`) and `orchestra status` to follow it. It says the run keeps going if this session
  ends, and gives `orchestra status` / `tail -f .orchestra/run.log` to reattach.
- **Resume the ship step.**
  - `_ship_and_fix` sets meta `ship_pending = {"head": <integration sha>, "at": …}` before
    `shipper.ship` and clears it after.
  - At start, a run that finds `ship_pending` re-runs the ship step once (push is a no-op, the PR
    is updated, CI is awaited). `cmd_run` no longer exits "Nothing pending" when `ship_pending` is
    set and `[ship].auto` is on.
  - If the integration head has moved since, the normal ship after this run's merges covers it.

**Tests.**
- `--detach` calls `Popen` with `start_new_session=True` and the log path (patched `Popen`), and
  doesn't pass `--detach` on.
- With `ship_pending` set and nothing pending, `orchestra run` calls a fake shipper once and clears
  the key.
- Without the key, nothing ships (as today).

## 9. `orchestra usage` gaps (and 9b, the reviewer cap)

**Evidence** (`usage.build` from this worktree on a `/tmp` copy of `logs-export`):
- `other` resumes: 11, of which 2 are decision answers (prefix "The lead answered your
  question.", `store.py:421`), 1 is `[structured-output-enforce]`, and 8 are human prompts in
  the groom session.
- Over 8,000 chars: 76 results, 56 Bash and 20 Read.
  - 53 Bash results (34 worker, 19 test writer; 8,071–8,158 chars) carry the cap's marker "[truncated N chars: the middle of this output was cut…" (`guards.py:265`).
  - The marker sits mid-output, after the head.
  - Not capped: 2 human-session Bash results, 1 reviewer Bash (T19 `f5ab8e59`, 10,845), and the
    Read results (18 agent, 2 human).
- The card join is missing with this export layout. `_state_rows` only looks for `tasks.json`,
  `events.json` or `state.db` (`usage.py:245-251`), and the export has `tasks-full.json` +
  `events.log` + `sessions.json`. With the queue joined, T01–T14 (cancelled after a double
  approve, groom transcript 118) show as $0 rows.

**Change.**
- `RESUME_KINDS` gains `("decision", "The lead answered your question.")`,
  `("decision", LEGACY_DECISION_PREFIX)` and `("output_enforce", "[structured-output-enforce]")`.
- `ToolResult` gains `capped`, true when the text contains the cap marker.
  - `cap_what_if` counts only uncapped agent results, and reports `already_capped: 53` separately.
  - The human session is left out of the what-if, because its output isn't capped by Orchestra.
  - The brief's alternative, a length test (`chars <= cap + len(note)`), is the fallback when the
    marker text isn't in the result.
- The card table folds cancelled cards with no sessions into one line: "14 cancelled cards with
  no sessions (T01–T14)".
- `_state_rows` also accepts `tasks-full.json` and `events.log`. Events are parsed from
  `[ts] #N TASK kind: detail` blocks separated by `----`.
- **9b.** `_checker_options` adds `make_output_cap` for Bash when `bash_output_max_chars` is set,
  as `_editor_options` does (`agents.py:596-598`).

**Tests.**
- A fixture transcript with a decision resume is labelled `decision`.
- A Bash result with the marker is `capped` and excluded from `results`.
- A fixture export with `tasks-full.json` + `events.log` joins card status.
- Cancelled cards with no sessions fold into one line.
- Reviewer options include the cap hook.

## 10. Proposal only: thin reviews on test-first cards

**Evidence.** Sonnet reviewers on the test-first cards made 2 tool calls each, in 6–9 seconds:
T18 `db2c2e9b`, T30 `9cb53c62`, T31 `71e57864`, T32 `69a0916a`, T33 `1ed27fe8`. T15 isn't
test-first; its reviewer `8d9e5a9b` made 4 calls in 14 seconds. All were approved first round.
Whether any of them missed a defect is unconfirmed.

**Proposal (no code until approved).**
- `REVIEW_SCHEMA` gains `checked_beyond_tests`: a short list of what the reviewer verified that
  the locked tests don't, e.g. "read X for the empty-sheet case" or "ran the CLI on sample.xlsx".
  It is required by the schema and empty-allowed.
- For test-first cards, `_tests_note` adds: "The locked tests prove the criteria as the test
  writer read them. Check at least one thing they don't: a criterion's edge case, an error path,
  or how the change fits its callers. List it under checked_beyond_tests."
- `check_verdict` treats an approve with an empty `checked_beyond_tests` on a test-first card as
  a soft finding. It is logged and counted by `orchestra usage`, but doesn't block. One wave of
  data then shows whether it should block.
- Cost: likely +$0.02–0.05 per test-first review (a few more turns on Sonnet; unconfirmed).

## 11. Housekeeping

- Copy the export to `/tmp` and run `orchestra usage <copy> --markdown` there (reading
  `tasks-full.json` needs fix 9). Write `openspec/changes/spend-less-per-card/after.md` from it.
- Compare with `baseline.md`: cost per merged card ($2.11 → $1.74), cancelled-card spend
  ($6.08/26% → $4.22/24%), cold-start cache writes, peak context, and first-round review pass rate.
- Tick 3.1, merge spend-less-per-card's deltas into `openspec/specs/` and archive it.
- Archive commit-identity (PR #6 is merged) the same way.
- This runs **before** wave-2-fixes' own deltas are merged, so its spec deltas apply on top.
