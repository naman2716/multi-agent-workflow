# Delta: planning

## ADDED Requirements

### Requirement: Plans are drafted against the integration head
The planner and critic SHALL explore a read-only worktree at the integration branch head.

### Requirement: Missing folders are flagged
A literal scope path whose folder doesn't exist SHALL produce a plan warning.

### Requirement: Risky cards are flagged test-first
The planner SHALL set `test_first` on cards that change money, stored data, or auth/permissions.
