# Proposal: spend-less-per-card

Builds on `main` at `352e378` (run-without-you, PR #3, and fix-rename-scope, PR #4, merged).
Phase 1 only: this change adds the read-only `orchestra usage` command and `baseline.md`, and
proposes the fixes below. No orchestrator behaviour changes until approved.

## Why

Wave 1 of the PCME dashboard (see `baseline.md`) cost **$23.21** in agent sessions by the CLI's own
accounting, and Orchestra *recorded* **$30.41**. Three things drive cost per card:

1. **Work thrown away.** T01, T04 and T05 each ran a full attempt, failed the 600-line `diff_size`
   gate (852, 1,756 and 11,058 lines), were trimmed in the same session, then split by triage.
   They cost **$6.08 (26%)** plus **$0.47** of triage. T05's Opus worker was the most expensive
   session ($3.06, 45 turns, peak context 145k).
2. **Cold starts and how they are billed.** Every cache write in the wave used the 1-hour TTL, billed
   at 2× the input price instead of 1.25× for 5 minutes (1.66M tokens written). Reviewers (364k),
   workers and the test writer (171k) and validators (162k) each start cold.
3. **Spend that isn't real.** `tasks.cost_usd` re-adds a session's cumulative cost on every resume,
   so the budget over-counted by 32% and the third run stopped at "$25" after spending $19.42.

## Findings beyond the brief

These came out of building the baseline; each has evidence in `baseline.md` or `design.md`.

- **Cost is double-counted on resume** (fix 0 below). `ResultMessage.total_cost_usd` and
  `model_usage` are cumulative for a resumed session. `_work_until_green` adds them after every run
  (`orchestrator.py:710`), so recorded = Σ cumulative totals at run ends, exactly, for every card.
  The triage prompt also shows the agent this inflated "Cost so far" (`orchestrator.py:1044`).
- **The CLI chose the 1-hour cache TTL** for every agent session. The bundled CLI (2.1.294) has
  `FORCE_PROMPT_CACHING_5M`, which returns `{ttl: "5m", reason: "force_5m_env"}`. Re-pricing the
  wave with 5-minute writes, and charging a full re-write wherever two turns were more than five
  minutes apart, saves **$3.38 net** (fix 8).
- **Validators changed nothing in wave 1:** 8 findings checked, 0 refuted (events.log lines 76, 82, 111),
  for $1.71. That argues for batching them (fix 3), not for dropping them on one wave of evidence.
- **The user's global `~/.claude/CLAUDE.md` reaches every agent**, labelled "Project", because
  worktrees live under `$HOME` and the CLI walks up parent directories. It tells agents to run
  `graphify query` first. Small in tokens, but it is instructions agents shouldn't get. Listed under
  fix 5, not fixed here.
- **Test environment:** `$HOME/work/venv` doesn't exist on this Mac; the suite was run with
  `~/tools/.venv/bin/python`. Two tests fail on clean `main` before any change
  (`test_guard`, `test_interactive_profile`): macOS temp dirs are `/var/...` → `/private/var/...`,
  and the write guard compares the resolved worktree with the unresolved path. Both pass with
  `TMPDIR=/private/tmp/...`. Pre-existing and macOS-only; real worktrees under `~/Downloads` aren't affected. Not fixed in this change.

## What changes (proposed, in recommended order)

Savings are for wave 1, against the $23.21 actual spend, and are upper bounds unless noted.

| # | Fix | Wave-1 saving | Evidence |
|---|---|---|---|
| 0 | **Count spend once.** Store per-session cost as the delta from the last cumulative total. | $0 of tokens; the budget is right again ($7.47 over-count removed, third run wouldn't have stopped ~$5.60 early) | baseline.md "How to read the money columns" |
| 1 | **Size cards before dispatch.** Planner/critic estimate changed lines per card against `max_diff_lines`; split anything over 70%; generated payloads count. | up to **$6.55** (cancelled cards $6.08 + their triage $0.47); less in practice, since the split cards redo some reading | T01/T04/T05 rows; triage reasons in events.log |
| 8 | **5-minute cache writes for agents** (`FORCE_PROMPT_CACHING_5M=1`, configurable). | **$3.38** net ($4.03 saved on writes, $0.65 re-written after gaps over 5 min). Unconfirmed until a run shows `ephemeral_5m` writes | `usage.cache_creation` in every transcript |
| 2 | **Catch oversize early in the worker.** A post-edit hook warns at the limit and asks for "blocked"; a `diff_size` failure over 1.5× the limit goes straight to triage. | **$0.62** measured (T04 and T05 trim rounds); the stop-at-the-limit part could have cut T05's $2.72 first round but isn't measurable from transcripts | "resumed tok" for T04/T05; gate details in events.log |
| 3 | **One validator session per review** (all blocking findings), shared card and diff first. | **~$0.79** (5 of 8 validator cold starts avoided; those 5 sessions cost $1.03) | validator rows; events.log 76/82/111 |
| 6 | **Record session metadata** (role, session_id, model, start, end, end_reason, cost delta) in a `sessions` table. | none directly; lets `orchestra usage` stop guessing roles, and makes fix 0 one table instead of a meta key | `tasks.session_id` holds one session per card |
| 4 | **Cap Bash output** at 8k chars via the CLI's `bashOutputMaxChars` setting (per-project `[quality] bash_output_max_chars`). | **~$0.5** (42 agent results over 8k, 197k chars, ~1.35M context-token re-reads) before any re-reads it causes | "Tool output over 8,000 chars" |
| 5 | **Stable prompt prefix:** finding last for validators (folds into fix 3), stable notes before per-round notes for reviewers; move user-level CLAUDE.md out of agents' reach. | small; mostly inside fix 3 | design.md, prefix audit |
| 7 | **Context budget per worker** (proposal only). | at most **$0.36** (all cache reads above 80k context, 77 turns, all on T01/T04/T05/T13/T14) | design.md |

Plus a README section for the interactive session (not Orchestra code): run `/groom`,
`/dispatch` and `/review-integration` in separate sessions or `/clear` between them, and do hand
fixes in a fresh session. The interactive session used 24.6M tokens (peak context 296k), about as much as all
44 agent sessions together (26.3M); $7.82 priced at API rates, outside Orchestra's budget.

**Taken together** the savings overlap: fix 2's measured saving sits inside fix 1's (it catches
what sizing misses), and $0.74 of fix 8's is on the cancelled cards. Fixes 1, 8 and 3 together
would have cut wave 1 from $23.21 to roughly **$13–17**, depending on how much of the cancelled
cards' reading the split cards had to repeat. Fix 0 makes the budget measure the real number.

## Out of scope

- Switching models per card type. The baseline doesn't make a clear case: all 14 reviewer
  sessions cost $4.55 together (reviewers take the opposite family to the worker), and one wave
  is too little quality data to trade against.
- Datadog LLM Obs tracing (rec 15).
- The macOS guard test failures and the `~/.claude/CLAUDE.md` leak (noted above; separate changes).

## Impact

- New: `orchestra/pricing.py` (one price table), `orchestra/usage.py`, `orchestra usage` command, `tests/test_usage.py`.
- Phase 2, per fix: agents (planner/critic schema and prompts, validator prompt, worker hook),
  orchestration (cost delta, sessions table, triage shortcut), config (`[quality]` / `[agents]` keys),
  store (`sessions` table), README.
