# Delta: ship

## ADDED Requirements

### Requirement: Never force, never main
Shipping SHALL push only the integration branch, never force-push, and open PRs as drafts into the
base branch.

### Requirement: Degrade quietly
Without a remote, `gh`, or authentication, shipping SHALL be skipped with a note and the run SHALL
still succeed.
