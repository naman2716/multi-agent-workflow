# Proposal: wave-2-fixes

Builds on `main` at `db0f598` (spend-less-per-card, PR #5, and commit-identity, PR #6, merged).
**Phase 1 only:** this folder proposes the fixes. No behaviour changes until Naman approves.

Evidence is the PCME waves 1.5 + 2 export,
`~/Downloads/pcme_dashboard/.claude/worktrees/groom-waves-1-5-2-4a201c/.orchestra/` (read-only):

- `#N` is the event id in `logs-export/events.log`, the same as `events.id` in `state.db`.
- "groom transcript" is `logs-export/agent-transcripts/groom-session/3430591c-….jsonl`.
- Per-session costs are from `sessions.json`, and they sum to the run's $17.40.
- `state.db` is newer than the export (T34–T48 were added on Oct 16), so every card fact here
  comes from `tasks-full.json`.

## Why

| | wave 1 | this run |
|---|---|---|
| actual spend | $23.21 | $17.40 |
| merged cards / spend per merged card | 11 / $2.11 | 10 / $1.74 |
| spend on cancelled or blocked cards | $6.08 (26%) | $4.22 (24%): T20 $3.31, T17 $0.91 |
| largest overshoot of the 600-line limit | 11,058 lines | 1,762 lines (T17, #139) |
| gate-fix trim rounds / validator sessions / 1-hour cache writes | 6 / 8 / 1.66M | 0 / 1 / 0 (agents) |

The spend-less-per-card fixes held. What's left falls into three groups:

1. **Cards that weren't sized.** These caused the $4.22.
2. **Card surgery the CLI can't do.** Naman's interactive session had to write raw SQL and run a
   git ref update by hand.
3. **Test writers.** They are now the largest single cost: $5.60, 32% of agent spend.

## Corrections to the brief

These came out of re-checking the evidence. Each is cited in `design.md`.

- **The groom skill PCME used is older than the sizing work.** `.claude/skills/groom/SKILL.md`
  is *committed* in pcme_dashboard (`fa9e43b`, 2026-10-14 17:23). PR #5 added `estimated_lines` to
  the groom plan format and merged later (01:44 on 10-15), and the harness was never re-installed.
  So PCME's `/groom` didn't even show the field. On `main`, the groom skill lists
  `estimated_lines` in its JSON shape (`harness.py:64`) but still lacks the sizing rules that the
  planner agent gets (`harness.py:86` vs `harness.py:124-131`).
- **T17's answer was a `--pick`, not free text.** The groom transcript (line 255) runs
  `orchestra answer T17 --pick 1`. The stored answer (#123) is option 1's full text: "Widen scope:
  edit backend/app/models/core.py (GRANULARITIES) and add
  backend/alembic/versions/0002_fytd_granularity.py…". The decision itself is #103.
- **The hand edits used Python's sqlite3 module,** not the sqlite CLI (groom transcript line 222).
  They ran `update tasks set depends_on=?` for T29 and T16, and `set acceptance=?, spec=?` for T15.
- **The 621, 862 and 1,762 line counts are worker self-reports** in blocked and triage events
  (#70, #151, #139). No gate measured them, because the size check stopped each worker first.
- **T31, T32 and T33 extended the same fixtures in turn rather than rewriting them.** They form a
  `depends_on` chain (T30→T31→T32→T33). Each test writer added to `make_mvp_expected.py`,
  `mvp_aug2026_expected.json` and `test_parity.py` on top of the previous card's merged version:
  +538, +509 and +671 test lines (#163, #170, #177). The waste is three cold starts re-deriving
  the same generator, not three copies.
- **T20's thrown-away test patch was 2,268 lines,** and 1,528 of those were one JSON fixture
  (`handoffs/T20.tests.patch`). Giving that patch whole to the children would undo fix 7, so
  fix 6 filters it.
- **Locked tests have no size limit at all.** They are exempt from the size and scope gates
  (`gates.py:72,91,103`), and the test writer runs without the size hook
  (`agents.py:634-635`, `stop_gate=False`).
- **Split children can never be triaged.** `_triage_split` creates them with `triaged=True`
  (`orchestrator.py:1200`). T30–T33 all have `triaged=1`.
- **T15 is not test-first** (`test_first=0`). Fix 10's list of thin reviews is therefore
  T18 and T30–T33, plus T15, which is not a test-first card.
- **A kill during the CI wait loses the CI result.** It does not lose work. The next
  `orchestra run` never re-checks CI (`orchestrator.py:320,425-426`), and with nothing pending it
  exits before reaching the ship step (`cli.py:278-282`).
- **Usage numbers:**
  - 11 resumes are labelled `other`. Two are decision answers (T16 `fbbe48e7`, T17
    `0292b5ad`), one is the SDK's `[structured-output-enforce]` nudge, and eight are prompts in the
    human session.
  - 76 results are flagged over 8,000 chars: 56 Bash and 20 Read.
  - 53 of the 56 Bash results carry the cap's truncation marker. The marker sits in the **middle**
    of the output (head + note + tail), not at the end.
  - Of the other 3 Bash results, 2 are in the human session. The third (10,845 chars) is in T19's
    **reviewer** session `f5ab8e59`. Reviewers and validators never get the Bash output cap:
    `_checker_options` installs only the read guard (`agents.py:655`).
  - The 20 Read results split as 18 agent and 2 human-session.

## What changes (proposed, in priority order)

"This run" savings are measured against $17.40 and are upper bounds unless marked otherwise.

| # | Fix | Effect on this run | Evidence |
|---|---|---|---|
| 1 | **Groomed plans get sized.** The groom skill gets `sizing_rules()`. `approve` and `add` warn once per card with no estimate. `[planning] require_estimates` (default off) turns the warning into a refusal. Stale harness files are detected and refreshed with `orchestra harness install`. | up to **$4.22** (T20 + T17). T20 would have been split before any agent ran; its children came in at 186–245 lines (#156–177). | plan.json; tasks-full T15–T29 `estimated_lines=0`; `planning.py:139-144` |
| 2 | **Picking a scope-widening option widens `files_scope`.** Decision options can carry `scope_add` paths. Picking one adds them to the card and logs it. A manual route: `orchestra answer --scope-add` and `orchestra scope T17 --add`. | Unblocks T17 without SQL. Its triage split (#140) would have been accepted. $0.91 already spent. | #103, #123, #140–141; `orchestrator.py:1188-1193` |
| 3 | **`orchestra retry` resets `triaged`.** A human re-queue gets a fresh triage. | T15's size block (#70) would have been triaged (~$0.2–0.3) instead of hand-split by the human session | #38–41, #55, #70 |
| 4 | **Card surgery from the CLI.** `orchestra edit <ID> --depends-on/--scope/--acceptance/...` and `orchestra split <ID> <plan-file>`, which reuses triage's split path and rewires dependents. Running and merged cards are refused without `--force`. | No raw SQL. Groom transcript lines 217–222 become two commands. | groom transcript 217, 222 |
| 5 | **Sync the integration branch with base** at `run`, `plan` and `approve`. Fast-forward when integration is behind. Stop with a clear message when the branches have diverged; `--allow-diverged` overrides. | **$0.36** (#54) plus one manual `git fetch . main:orchestra/integration` (groom transcript 172) | #29, #39, #54, #57 |
| 6 | **Test writers: reuse and a size check.** Children of a split test-first card get the parent's locked tests (code in full, data files listed by size only) as a starting point. Test writers get a size check like workers do. | Unconfirmed, at most part of T30–T33's **$3.65** test-writer spend. The size check would have flagged T20's tests at 600 lines, before the $1.49 worker ran. | `handoffs/T20.tests.patch`; T30–T33 sessions |
| 6b | *Proposal only:* **Sonnet as the default test-writer model.** | Re-pricing the same tokens: **$5.60 → $3.29 (−$2.31)**. Quality is unknown. | sessions.json; `pricing.py` |
| 7 | **Data-file hint** for workers, test writers and triage: when a data file dominates the diff, propose a compact form with a round-trip test, generate it at test time, or add a `diff_exclude` entry. Plus a README note. | T17's ~1,500 copied lines → ~150–250 (triage's own estimate, #139); T20's 1,528-line fixture | #139; `T20.tests.patch` |
| 8 | **Runs survive the session that started them.** `orchestra run --detach` starts the run in its own process session with output to `.orchestra/run.log`, and `/dispatch` uses it. A run killed while shipping re-checks CI on the next run. | No spend saved; a run that dies mid-ship still gets its CI result checked | `run-output/bo9ktvxa6.output` (last line before `[killed]`); #182 is the last event |
| 9 | **`orchestra usage` fixes:** label `decision` and `output_enforce` resumes; treat marked results as already capped; leave the human session out of the cap what-if; fold cancelled cards that have no sessions; read the `tasks-full.json` + `events.log` export layout. | Report accuracy: "over the cap" goes from 76 to 19 agent results (1 reviewer Bash, 18 Read). 53 count as already capped and 4 human-session results drop out. 2 resumes are relabelled. | numbers above |
| 9b | **Reviewers and validators get the Bash output cap** too. | 1 result in this run (10,845 chars) | `agents.py:655`; T19 `f5ab8e59` |
| 10 | *Proposal only:* **Reviews must check something beyond the locked tests.** A required `checked_beyond_tests` field for test-first cards (see design). No code until approved. | T18 and T30–T33 reviewers each made 2 tool calls in 6–9 s | reviewer sessions `db2c2e9b`, `9cb53c62`, `71e57864`, `69a0916a`, `1ed27fe8` |
| 11 | **Housekeeping:** `after.md` for spend-less-per-card from this export, compared with `baseline.md`, then archive spend-less-per-card and commit-identity. | — | — |

**Taken together:** fixes 1 and 7 address the whole $4.22 spent on cancelled or blocked cards.
Fix 6b would save another $2.31 if Sonnet's tests hold up. Fixes 2–5 and 8 remove every manual
step from this run except the human's own decisions:
- the SQL edits and the hand split (groom transcript 217, 222);
- the `git fetch` (172);
- the second `orchestra approve --force` after cancelling T01–T14 (118).

## Out of scope

- Datadog LLM Obs tracing, a golden eval set, best-of-N (recs 15–17).
- Unblocking T17 or editing the pcme_dashboard queue. That happens in the PCME project after
  these fixes ship.
- Fetching the base branch from `origin` before the integration sync. Fix 5 compares local refs
  only; see the open question in `design.md`.

## Impact

- **Code:** `harness.py`, `planning.py`, `cli.py`, `config.py`, `store.py`, `orchestrator.py`,
  `agents.py`, `gates.py`, `asking.py`, `ntfy.py`, `gitops.py`, `usage.py`. A new
  `orchestra/cards.py` holds the card surgery shared by triage, answers and the CLI.
- **Docs:** README sections Planning, Test-first cards, Self-healing, Tuning, Interactive side,
  and Shipping.
- **Specs:** deltas for planning, harness, cli, orchestration, agents and git-integration.
- **Conflicts with unmerged branches** (both based on `db0f598`):
  - `plan-from-file` (`9b94dd4`) changes `cli.py` imports (lines 2–35), `cmd_plan` (186–200) and
    the `plan` parser (631–637), plus README lines 103, 377 and 402. Fixes 1, 2, 4, 5 and 8 touch
    `cli.py` next to those spots: the `approve`/`add`/`answer`/`retry`/`run` parsers and new
    subcommands near line 640. So **`cli.py` and `README.md` will likely conflict** (README around
    "## Planning", line 403). Both are textual conflicts that are easy to resolve.
  - `guard-symlink-paths` (`fc3e0ca`) changes only `guards.py`, which this change doesn't touch.
    No conflict expected.
- **Pre-existing test failures:** 162 tests on the untouched worktree, 2 failing (`test_guard`,
  `test_interactive_profile`). These are the macOS `/var` vs `/private/var` failures recorded in
  spend-less-per-card, and `guard-symlink-paths` probably fixes them. Phase 2 will keep them as
  they are and report them.
