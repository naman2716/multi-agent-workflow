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
