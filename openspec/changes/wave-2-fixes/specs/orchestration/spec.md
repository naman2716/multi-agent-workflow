# Delta: orchestration

## MODIFIED Requirements

### Requirement: Blocked cards are triaged once before they reach you
When a card is about to become blocked, a triage agent SHALL choose one of rewrite, escalate,
split or hand-to-human; allowed actions are applied automatically, at most once per card until a
human retries it, and you are notified. A split SHALL be accepted when every child's
`files_scope` is inside the card's current `files_scope`, including paths an answer added.

#### Scenario: Card too big for one diff
- **WHEN** a card fails the diff-size gate on every attempt
- **THEN** triage splits it into smaller cards within its scope, dependents wait for all of them,
  and the original is cancelled as "split into …"

#### Scenario: Split after the scope was widened
- **WHEN** an answer added `backend/app/models/core.py` to a card's scope and triage later splits
  the card with a child that edits that file
- **THEN** the split is accepted

### Requirement: Runs end in a draft PR and fix their own CI
After a run that merged at least one card, Orchestra SHALL push the integration branch, open or
update one draft PR into the base branch, wait for CI, and turn failing checks into a fix card,
up to `ci_fix_rounds` times. A run interrupted while shipping SHALL be shipped again, CI wait
included, by the next `orchestra run`, even when no card is pending.

#### Scenario: Killed during the CI wait
- **WHEN** a run is killed while waiting for CI after its last merge
- **THEN** the next `orchestra run` updates the PR, waits for CI, and queues a fix card if CI fails

## ADDED Requirements

### Requirement: Picking a scope option widens the card
When a worker's decision lists `scope_add` paths for an option and the human picks that option
(by number, from any channel), those paths SHALL be added to the card's `files_scope`. A `scope`
event SHALL be logged, and the resume note SHALL name the added paths. The question SHALL show
each option's paths.

#### Scenario: Widen scope for a model change
- **WHEN** T17 asks to edit `backend/app/models/core.py` and add a migration, and the human answers
  `--pick 1`
- **THEN** both paths are in T17's `files_scope` and the scope gate accepts changes to them

### Requirement: Split test-first cards pass their tests on
When a test-first card with locked tests is split, each child's test writer SHALL receive the
parent's test code as a starting point, with data files listed by path and size only, and an
instruction to keep what covers its own criteria.

### Requirement: Test writers hear about size
The test writer SHALL get a size check like workers do, against `[quality] test_max_lines`
(default `max_diff_lines`), telling it to cover each criterion once, to generate large expected
data at test time, and to report blocked with a split when the card can't be tested within it.
