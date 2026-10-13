# Delta: orchestration

## ADDED Requirements

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
