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
