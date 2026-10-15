# Delta: harness

## ADDED Requirements

### Requirement: Groom sizes every card
The `/groom` skill SHALL carry the same sizing rules as the planner: the project's diff limit and
split threshold, `estimated_lines` on every card, committed data counted unless `diff_exclude`
covers it, and copied data files treated as a sizing risk.

### Requirement: Stale harness files are reported
`orchestra approve` and `orchestra run` SHALL print one line naming any managed harness file that
differs from what this Orchestra version would write, and point to `orchestra harness install`.

#### Scenario: Harness committed before an upgrade
- **WHEN** the repo's groom skill was written by an older Orchestra
- **THEN** `orchestra approve` reports it and suggests `orchestra harness install`

### Requirement: Dispatch survives its session
The `/dispatch` skill SHALL start the run with `orchestra run --detach` and follow its log, so
ending or restarting the Claude Code session doesn't stop the run.
