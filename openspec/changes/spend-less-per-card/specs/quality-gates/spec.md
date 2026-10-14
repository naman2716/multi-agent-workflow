# Delta: quality-gates

## ADDED Requirements

### Requirement: The worker's running size matches the gate
The in-session size check SHALL count tracked edits and new files against the card's base,
excluding `diff_exclude` and locked tests, without touching the worktree's index.
