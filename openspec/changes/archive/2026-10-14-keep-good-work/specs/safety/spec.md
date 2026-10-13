# Delta: safety

## ADDED Requirements

### Requirement: Reviewer shell can't change the change
Reviewer and validator shell commands SHALL be sandboxed and guarded like a worker's, with no
file-editing tools, and anything they leave in the worktree SHALL be discarded.

### Requirement: Implementers can't edit locked tests
The worker guard SHALL deny edits to a card's locked acceptance test files.
