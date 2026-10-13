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
