# Design: spend-less-per-card

Numbers are wave 1 (`baseline.md`). "Unconfirmed" means a claim that needs a real run to check.

## 0. Count spend once

**Cause.** The CLI keeps a running cost per session and restores it on `--resume`; the SDK's
`ResultMessage.total_cost_usd` and `model_usage` report that running total. `ClaudeRunner._run`
returns it as the call's cost (`agents.py:511-512`), and `Store.add_cost` adds it to the card
(`orchestrator.py:710`, also 658/785/821/978). A worker resumed for a gate fix, a revise, a
conflict, an answer or after a pause therefore re-adds everything before it. Evidence: for every
card, `tasks.cost_usd` equals the sum of the CLI's `cost-state.totalCostUSD` at each run end
(T02: $0.293 + $0.432 + … = $3.962 recorded vs $3.237 actual).

**Fix.** Remember the last cumulative cost and tokens per session id, and add only the delta:
`delta = max(total - last_seen[session_id], 0)`. With fix 6 this is a column on the `sessions`
row; without it, a `meta` key `session_cost:<id>`. The interrupted-run gap (T09, $0.27 never
recorded) closes the same way: on recovery, a resumed session reports a total that includes the
lost run, so the delta picks it up. A session that can't be resumed stays lost; that's acceptable.

**Tests.** A fake runner that returns cumulative totals across three resumes → card cost equals the
last total, not the sum; `run_spent()` and the budget stop use the delta; a fresh session id starts
from zero.

## 1. Size cards before dispatch

- `PLAN_SCHEMA` task items gain `estimated_lines` (integer, required) and `size_notes` (string:
  what dominates the size, such as generated payloads, fixtures or migrations).
- `PLANNER_SYSTEM` and `CRITIC_SYSTEM` get the real limit instead of the hard-coded "~300": "the
  diff gate rejects more than {max_diff_lines} changed lines (lockfiles and `diff_exclude` don't
  count); split any card estimated above {0.7 × limit}. Generated or example data (JSON payloads,
  fixtures, snapshots) counts unless it is excluded; prefer building it at test time over
  committing it" (the T05 triage reason, events.log line 132).
- `build_tasks` / plan validation flags any card with `estimated_lines > 0.7 × max_diff_lines`
  that the critic left unsplit, and `orchestra plan` prints the estimates so you see them at
  approval.
- Config: `[quality] split_above = 0.7` (fraction of `max_diff_lines`).

**Target:** no card cancelled for size. **Risk:** estimates are guesses; fix 2 is the backstop.
**Tests:** schema accepts and requires the field; the prompt contains the configured limit; a
plan with an oversized card is flagged.

## 2. Catch oversize early in the worker

- **Running size check.** A `PostToolUse` hook on `Write|Edit|Bash` in worker sessions runs
  `git diff --numstat <base>` in the worktree (excluding `diff_exclude`), the same count the gate
  uses (`gates.py:94`). Over the limit, it returns `additionalContext`: "Your diff is now N changed
  lines; the limit is L. Stop, don't trim: report status blocked with how you would split this card."
  It warns once at 80% and again over 100%. The cost is one `git diff` per edit (milliseconds).
- **Skip the trim round when it's hopeless.** In `_work_until_green`, when the only failure is
  `diff_size` and `counted > 1.5 × max_diff_lines`, skip the in-session fix round and go straight
  to `_fail_attempt` with triage (attempts are not retried for size; triage splits).
  T01 (1.42×) and T02 (1.20×, which then passed at 594 lines) still get their trim round; T04 (2.9×)
  and T05 (18×) would have skipped theirs ($0.28 and $0.34).
- Config: `[quality] oversize_triage_factor = 1.5`.

**Tests:** the hook warns at 80% and 100% (a fake worktree with numstat output); a 2× diff_size
failure triages without a fix round; a 1.2× failure still gets one.

## 3. Validators: one session per review (recommended) vs. cache-friendly order

| | A. One validator session for all blocking findings | B. One session per finding, card and diff first |
|---|---|---|
| Wave-1 cost | ~$0.79 less: 3 sessions instead of 8, avoiding 99k tokens of 1-hour cold writes | ~$0.45 less (unconfirmed): sessions 2…N would read the shared card + diff from cache, but each still pays its own tools, system prompt and attachments, and runs its own turns |
| Independence | Findings are checked in one context, so a weak first refutation could colour the rest. Independence from the *reviewer* (what validation exists for) is unchanged | Each finding judged alone, as today |
| Latency | One session, findings in sequence inside it | Today's semaphore already runs them one at a time when `reviewer_shell` is on |
| Schema | `findings: [{index, confirmed, evidence}]`; a missing index counts as confirmed (today's "can't tell" rule) | unchanged |

**Recommendation: A**, with the prompt ordered card → diff → numbered findings (so fix 5 holds
too). Wave 1 had 0 of 8 findings refuted, so cross-contamination had nothing to change. Keep
`max_validations` as the cap on findings per session. If validators are later shown to agree too
easily, B is a one-line prompt reorder away. Today validators diverge after about 4.1k
characters of a ~28k prompt because `## Finding to verify` sits before the diff (`agents.py:629-631`).

**Tests:** one runner call per review with all findings; per-finding verdicts map back to
`drop_refuted`; missing verdicts count as confirmed; the prompt puts the diff before the findings.

## 4. Cap tool output

- The bundled CLI (2.1.294) has `bashOutputMaxChars`: "How many characters of a successful Bash
  or PowerShell command's output Claude receives inline (default 30000; values clamp to
  4000-128000). Output past this is saved to a file and Claude receives a short preview plus the
  path." `BASH_MAX_OUTPUT_LENGTH` only sizes the read-back window when that is set. No hook needed.
  Orchestra already passes a settings dict for the sandbox (`agents.py:397-413`); add
  `bashOutputMaxChars` there for worker, test-writer, reviewer and validator sessions.
- The 30,000 default never triggered in wave 1 (largest Bash result 23,965 chars). At 8,000: 33
  Bash and 9 Read results in agent sessions are over, 197k chars, ~1.35M context-token re-reads
  (~$0.5). Read has its own token limit (`CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS`); leave it.
- What the big outputs were: deliberate file dumps (`cat a b c d`, `sed -n 1,400p
  docs/ORCHESTRA_HANDOVER.md`), not test output. The handover doc was dumped by most workers.
  Test runs (`uv run …`) added only 15k chars in total.
- **Failing tests must survive.** Gate output is unaffected (the orchestrator runs gates itself,
  `gates.py`). For the worker's own test runs, the CLI's "short preview" is head-first (unconfirmed),
  so a long pytest log could hide the `short test summary info` at the end. Two guards: the cap
  applies only to *successful* commands per the CLI's description (failed commands, exit ≠ 0,
  keep 30k; unconfirmed), and WORKER_APPEND says "for long test output, pipe through `tail -n 80`
  or read the saved file's end". **Phase 2 gate:** run a real worker session with a 50k-char failing
  pytest log and check the failing test names and summary reach the agent; ship fix 4 only if they do.
- Config: `[agents] bash_output_max_chars = 8000` (0 = CLI default).

**Tests:** `sandbox_settings`/options include the key with the configured value; 0 leaves it out;
the clamp range is enforced (4000–128000).

## 5. Prompt prefix audit

Render order is tools → system → messages; a cache hit needs a byte-identical prefix. Order today:

**Worker / test writer** (`_editor_options`, `agents.py:543-569`)
1. Tools: Read, Write, Edit, Glob, Grep, Bash (+ graph MCP tools when the graph is on). Stable.
2. System: the CLI's `claude_code` preset (stable), then the append: WORKER_APPEND or
   TEST_WRITER_APPEND, SANDBOX_NOTE, GRAPH_GUIDE, `## Project context`. Stable per config.
3. First user message: the task card (per card), then the CLI's attachments, which the transcript
   logs *after* the prompt: sandbox instructions (~14k chars), environment (cwd = the card's
   worktree path), model, token and USD budget, CLAUDE.md files (the user's
   `~/.claude/CLAUDE.md` and the worktree's), session context (user email; git status with branch
   `orchestra/task/Tnn` and the last five commits), and date. Whether the API sees the
   attachments before or after the card is unconfirmed; the card is per card either way.

Nothing dynamic sits in the system prompt, so workers share tools + system across cards (the
~5k read on validators' first turns is that shared part). Within a card a resume keeps the whole
history. **No change needed for workers.**

**Reviewer** (`review`, `agents.py:609-622`): system REVIEWER_SYSTEM + sandbox + guide + context
(no preset); prompt: card → `## Context for this review` (revision note: per round) → worker
report → gates → impact → diff. Round 2 differs from round 1 from the note onward, but the diff
differs too, so moving the note saves little. **Change:** put the note after the diff, so the
card + report prefix matches when only the review note changes (conflict re-reviews). Small.

**Validator** (`validate`, `agents.py:624-634`): card → `## Finding to verify` → diff. **Change**
(fix 3): card → diff → findings.

**Triage** (`_triage_context`, `orchestrator.py:1035-1062`): card → why blocked → feedback →
history (includes the inflated "Cost so far", fixed by fix 0) → events → last handoff → other open
cards. One call per card, so the prefix doesn't matter.

**Out of Orchestra's control:** the CLI loads `~/.claude/CLAUDE.md` into every agent because
worktrees sit under `$HOME`. `CLAUDE_CODE_DISABLE_CLAUDE_MDS` would also drop the repo's
CLAUDE.md, which agents need. Moving `worktree_root` outside `$HOME` avoids it; proposed as a
separate change, not here.

## 6. Session metadata

New table `sessions(session_id PK, task_id, role, model, attempt, started_at, ended_at,
end_reason, cost_usd, tokens, last_total_usd, last_total_tokens)`. `ClaudeRunner._run` already
sees the session id, model and timing; the orchestrator sets `role` (worker, test_writer, reviewer,
validator, triage, planner, critic) and `end_reason` (`done`, `gate_failed`, `review_rejected`,
`blocked`, `needs_decision`, `usage_limit`, `interrupted`, `budget`, `error`) where it already
branches. A resumed session updates its row. `orchestra usage` reads the table when present
and falls back to prompt-shape classification (`usage.classify`) for older exports.
`orchestra status Tnn` lists a card's sessions.

**Tests:** each role writes a row; resume updates rather than inserts; end_reason per branch;
the usage tool prefers the table.

## 7. Context budget per worker (proposal only)

Wave 1: 77 worker turns ran above 80k context, all on T01, T04, T05 (oversized, cancelled) and
T13/T14 (T05's children, Opus, peaks 95k/92k). Their cache reads above 80k cost **$0.36** in
total.

| | (a) CLI compaction (`CLAUDE_CODE_AUTO_COMPACT_WINDOW`, e.g. 100k) | (b) End the session, start fresh with a structured handoff |
|---|---|---|
| Cost | One summarization call when crossed; the session keeps its cache for the summary | A cold start (~15–40k written, 1-hour price today) plus re-reading files the handoff names |
| Losing decisions | Summary is model-written; decisions and the card's constraints can blur | Handoff is structured (goal, decisions, files touched, what's left) and decisions are already restated by `_interrupted_note` |
| Locked tests, `files_scope` | Unaffected (enforced by hooks and gates, not context) | Unaffected; the card is re-sent |
| Code | One env var | New end-and-handoff path on top of the resume path |

**Recommendation: neither for now.** The whole above-80k cost is smaller than one cold start per
affected session, and every case was an oversized card that fixes 1 and 2 address. If after-wave
data shows long sessions on right-sized cards, start with (a) at 120k for workers only; it's one
setting.

## 8. Five-minute cache writes

`agent_env()` gains `FORCE_PROMPT_CACHING_5M=1` (CLI: `{ttl: "5m", reason: "force_5m_env"}`).
Config `[agents] cache_ttl = "5m" | "1h" | "auto"` (default `"5m"`). Re-priced wave 1: writes cost
$4.03 less; turns that came more than 5 minutes after the previous one (155k tokens read in
the wave, mostly gate runs and a review in between) would be re-written, $0.65, so **net $3.38**.
The usage-limit pause (T03, ~4 hours) was a cache hit with the 1-hour TTL (brief finding 6;
unconfirmed how, since the gap exceeds the TTL). With 5 minutes it would be a full re-write
(~$0.30), already inside the $0.65. **Unconfirmed until a run:** that the env var takes effect
through the SDK, and that `usage.cache_creation.ephemeral_5m_input_tokens` appears.

**Tests:** `agent_env` includes the variable for "5m", leaves it out for "auto", and sets
`ENABLE_PROMPT_CACHING_1H=1` for "1h".

## README: the interactive session

New section "Keeping your own session cheap": the `/groom` + `/dispatch` + `/review-integration`
session in wave 1 ran 111 turns to a 296k context and used 22.4M tokens, about as much as all 18
worker sessions together (21.6M). Run each phase in its own session (or `/clear` between them),
do hand fixes in a fresh session, and point a fresh session at `orchestra status` / the draft PR
rather than carrying the history. Optional: `orchestra run` prints "next: /review-integration in a
fresh session" when the queue drains.

## Measuring Phase 2

`orchestra usage <export> --json` on the next comparable wave → `after.md`, compared on: cost per
merged card (wave 1: $17.13 CLI across 11 merged cards = $1.56; recorded $1.77), cancelled-card
spend ($6.08), cold-start writes (720k across agent sessions), peak context (145k),
first-round review pass rate (wave 1: 9 of 11 merged cards approved first time; T02 and T10
needed a revise).
