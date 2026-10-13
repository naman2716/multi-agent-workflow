# Delta: quality-gates

## ADDED Requirements

### Requirement: No conflict markers merge
A change whose added lines contain conflict markers SHALL fail the `conflict_markers` gate.

### Requirement: Quick checks first in fix rounds
When a gate defines `changed_cmd`, fix rounds SHALL run it on the changed files first and only run
the full command if it passes.

### Requirement: Locked tests are what gets checked
For a card with locked acceptance tests, every gate run SHALL use the locked versions of those files.
