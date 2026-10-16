# Delta: git-integration

## ADDED Requirements

### Requirement: Integration follows the base branch
At `orchestra run`, `plan`, `approve` and `add`, Orchestra SHALL compare the local base branch
(`[orchestra] base_branch`) with the integration branch:
- integration missing: create it from base;
- base is an ancestor of integration, or they are equal: leave it;
- integration is an ancestor of base: fast-forward it with a compare-and-swap and log it;
- diverged: stop with a message giving the commit counts each way, unless `--allow-diverged`
  is given.

While a live run holds the lease, `approve` and `add` SHALL only report the state.

#### Scenario: Addendum committed to main after integration was cut
- **WHEN** `orchestra/integration` is at 50422e7 and `main` is at its descendant d1aa457
- **THEN** `orchestra run` fast-forwards integration to d1aa457 before dispatching any card

#### Scenario: Squash-merged PR
- **WHEN** integration has commits that are not on base, but base already contains their changes
- **THEN** the run stops, and the message says the PR was probably squash-merged and gives the
  reset command
