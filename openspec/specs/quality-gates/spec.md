# quality-gates Specification

## Purpose

Deterministic checks and how their commands run (`gates.py`, `gitops.run`).

## Requirements

### Requirement: Generated files don't count toward diff size
Files matching `diff_exclude` (default: common lockfiles and `go.sum`) SHALL be excluded from
the line count checked against `max_diff_lines`.

#### Scenario: New dependency
- **WHEN** a card adds a dependency and the lockfile changes by 700 lines
- **THEN** the diff-size gate counts only the other files

### Requirement: Secret scan covers the whole diff
The secret scan SHALL read every added line of the change, not a truncated excerpt.

### Requirement: Timed-out commands leave nothing running
When a gate or setup command times out, its whole process group SHALL be killed.

### Requirement: No conflict markers merge
A change whose added lines contain conflict markers SHALL fail the `conflict_markers` gate.

### Requirement: Quick checks first in fix rounds
When a gate defines `changed_cmd`, fix rounds SHALL run it on the changed files first and only run
the full command if it passes.

### Requirement: Locked tests are what gets checked
For a card with locked acceptance tests, every gate run SHALL use the locked versions of those files.
