# Delta: orchestration

## ADDED Requirements

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
