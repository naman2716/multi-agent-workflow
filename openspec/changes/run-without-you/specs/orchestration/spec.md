# Delta: orchestration

## ADDED Requirements

### Requirement: Blocked cards are triaged once before they reach you
When a card is about to become blocked, a triage agent SHALL choose one of rewrite, escalate,
split or hand-to-human; allowed actions are applied automatically, at most once per card, and you
are notified.

#### Scenario: Card too big for one diff
- **WHEN** a card fails the diff-size gate on every attempt
- **THEN** triage splits it into smaller cards within its scope, dependents wait for all of them,
  and the original is cancelled as "split into …"

### Requirement: Runs end in a draft PR and fix their own CI
After a run that merged at least one card, Orchestra SHALL push the integration branch, open or
update one draft PR into the base branch, wait for CI, and turn failing checks into a fix card,
up to `ci_fix_rounds` times.

### Requirement: Decisions can be answered from a phone
With ntfy configured, every question SHALL be pushed with answer buttons, and a reply on the reply
topic SHALL resume the waiting card exactly like a terminal answer.
