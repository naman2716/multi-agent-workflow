# orchestration Specification

## Purpose

Run loop, task lifecycle, budget, recovery and the run lease (`orchestrator.py`, `store.py`).

## Requirements

### Requirement: Budget is enforced per run
The orchestrator SHALL stop dispatching new cards once the spend of the current `orchestra run`
reaches `total_budget_usd` (or `--budget`), regardless of spend in earlier runs.

#### Scenario: Earlier runs don't block a new run
- **WHEN** lifetime spend is above `total_budget_usd` and a new run starts with a pending card
- **THEN** the card is dispatched

### Requirement: Reviewer approval is consistent with its findings
An approval SHALL be treated as a rejection when any criterion is unmet, any issue is
`blocker` or `major`, or `scope_creep` is non-empty.

#### Scenario: Contradictory approval
- **WHEN** the reviewer returns `approve` with an unmet criterion
- **THEN** the attempt fails with the reviewer's findings as feedback and nothing is merged

### Requirement: One live run per repository
Only one `orchestra run` SHALL operate on a repository's queue at a time, using a lease with a
heartbeat; a stale lease (no heartbeat for 60 s, or a dead pid on the same host) MAY be taken over.

#### Scenario: Second run while one is live
- **WHEN** a run holds a live lease and another `orchestra run` starts
- **THEN** the second exits with code 3 naming the holder, and no card changes state

### Requirement: Interrupted cards resume their work
After an interrupted run, a card whose worktree, session and base commit still exist SHALL
resume its session (or resume at review if its report was saved) without using an attempt.

#### Scenario: Ctrl-C during review
- **WHEN** a run is killed while a card is in review and its report was saved
- **THEN** the next run reviews that card again without re-running the worker

### Requirement: Fixable rejections are revised in place
When the reviewer rejects a change as `fixable`, the orchestrator SHALL send the findings to the
worker's own session and review again, up to `[review].fix_rounds` times, before starting a
fresh attempt.

#### Scenario: One fix after review
- **WHEN** the first review rejects as fixable and the revision is approved
- **THEN** the card merges on attempt 1 without a new worktree

### Requirement: Merge conflicts are resolved by the worker
When an approved card conflicts with integration, the orchestrator SHALL merge integration into
the worktree and have the worker resolve the markers in its session, then re-run gates and a
review of the resolution, up to `[review].conflict_rounds` times.

#### Scenario: Two cards edit the same line
- **WHEN** a card's rebase conflicts with work merged meanwhile
- **THEN** the worker resolves it, the card passes gates and review, and merges as one commit

### Requirement: Blocking findings are validated
Before a rejection counts, each blocking finding SHALL be checked by a validator; a finding is
dropped only when the validator refutes it with evidence; if none remain the change is approved.

### Requirement: Test-first cards lock their acceptance tests
For a `test_first` card, acceptance tests SHALL be written by a separate agent before
implementation, kept unchanged through every gate run, and reused on later attempts.

### Requirement: Each running card has its own environment slot
Each running card SHALL get a distinct slot number and derived environment variables, visible to
its setup command, gates and agents.

### Requirement: Longest chains start first
Among ready cards, those with more open cards depending on them SHALL be dispatched first.

### Requirement: Extra rounds respect the run budget and survive pauses
Revision and conflict rounds SHALL NOT start once the run budget is reached; the card SHALL be
parked with its worktree, session and round counters and continue on the next run.

### Requirement: Blocked cards are triaged once before they reach you
When a card is about to become blocked, a triage agent SHALL choose one of rewrite, escalate,
split or hand-to-human; allowed actions are applied automatically, at most once per card, and you
are notified.

#### Scenario: Card too big for one diff
- **WHEN** a card fails the diff-size gate on every attempt
- **THEN** triage splits it into smaller cards within its scope, dependents wait for all of them,
  and the original is cancelled as "split into …"

### Requirement: Runs end in a draft PR and fix their own CI
After a run that merged at least one card, Orchestra SHALL push the integration branch, open or
update one draft PR into the base branch, wait for CI, and turn failing checks into a fix card,
up to `ci_fix_rounds` times.

### Requirement: Decisions can be answered from a phone
With ntfy configured, every question SHALL be pushed with answer buttons, and a reply on the reply
topic SHALL resume the waiting card exactly like a terminal answer.

### Requirement: A usage limit postpones triage instead of using it up
When a usage limit stops a card's triage, the card SHALL be blocked without being marked
triaged, and its triage SHALL run at the start of the next run.

### Requirement: Spend is counted once per session
A call's cost and tokens SHALL be added to its card as the growth of its session's running total
since that session's last report, so resuming a session never re-counts earlier runs.

#### Scenario: Gate fix in the same session
- **WHEN** a worker session reports $0.30, is resumed for a gate fix and then reports $0.50
- **THEN** the card's cost grows by $0.50 in total, and the run budget sees $0.50

### Requirement: Every agent session is recorded
Each agent session SHALL have a record with its card, role, model, attempt, start, end of its
latest run, number of runs, cost, tokens and end reason (done, gate_failed, review_rejected,
blocked, needs_decision, usage_limit, interrupted, budget, error). A resumed session updates
its record.

### Requirement: Far-oversized changes go to triage
When the diff-size gate fails by more than `oversize_triage_factor` times `max_diff_lines`, the
attempt SHALL skip its in-session fix round and go to triage at once. If triage says a human must
decide, the card SHALL be blocked rather than retried.

#### Scenario: 18x the limit
- **WHEN** a worker's change is 11,058 lines against a 600-line limit
- **THEN** no trim round runs and triage is asked to split the card

### Requirement: One validator session per review
A review's blocking findings (up to `max_validations`) SHALL be verified together in one
validator session that returns a verdict per finding. A finding without a refuting verdict,
or any finding when the validator fails, SHALL stand.
