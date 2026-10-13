# agents Specification

## Purpose

Agent roles, prompts, the task card and per-role SDK options (`agents.py`).

## Requirements

### Requirement: Decisions stay on the card
Every answered decision SHALL appear on the card for all later attempts.

#### Scenario: Many feedback notes after a decision
- **WHEN** a card has an answered decision followed by gate failures and a failed attempt
- **THEN** the next attempt's card still lists the decision under "Decisions already made"

### Requirement: The card lists the checks
The worker's card SHALL list the command of every configured gate and ask the worker to run them before reporting done.

### Requirement: Optional gate check before a worker stops
With `gates_on_stop` enabled, a worker that reports `done` SHALL be sent back once with the
failures of the fast gates if any fail; any hook error SHALL allow the stop.

### Requirement: Agents don't share auto memory
Every agent session SHALL run with Claude Code auto memory disabled.

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
