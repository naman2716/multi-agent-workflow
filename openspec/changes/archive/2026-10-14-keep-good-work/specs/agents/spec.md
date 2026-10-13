# Delta: agents

## ADDED Requirements

### Requirement: Reviewer differs from the worker and can run code
With `reviewer_rule = "opposite"`, the reviewer SHALL use a different model family from the
worker that wrote the change, and SHALL be able to run sandboxed shell commands without
editing files.

#### Scenario: Balanced profile
- **WHEN** a card was written by Sonnet
- **THEN** it is reviewed by Opus

### Requirement: Review states whether problems are fixable
The review output SHALL include `fixable`, saying whether the findings can be fixed in the
current change.

### Requirement: Plans get a critic pass
With `[planning].critic` on, every plan SHALL be reviewed by a critic agent whose corrected plan
replaces the draft when it is valid.
