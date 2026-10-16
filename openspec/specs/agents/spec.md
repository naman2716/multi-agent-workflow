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

### Requirement: Triage role
A read-only triage agent SHALL return one action (rewrite, escalate, split, human) with its reason
and the fields that action needs.

### Requirement: Cards are sized against the diff gate
Planner, critic and triage prompts SHALL state the project's `max_diff_lines` and the split
threshold (`split_above` of it), and every planned card SHALL carry `estimated_lines`. Generated or
example data SHALL count toward the estimate.

### Requirement: Workers hear about size while working
Worker sessions SHALL get a check after each Write/Edit/Bash that counts the change the way the
diff-size gate does and tells the worker once at 80% of the limit and once past it, asking it to
stop and report blocked with a split rather than trim.

### Requirement: Prompt-cache lifetime is configurable
Agent sessions SHALL use the `[agents].cache_ttl` prompt-cache lifetime: "5m" (default), "1h", or
"auto" (the CLI's choice).

### Requirement: Worker Bash output is capped with head and tail
With `[agents].bash_output_max_chars` above zero, a worker's or test writer's foreground Bash
output longer than the cap SHALL reach the agent as its head and tail with a note giving the
number of characters cut. The command's exit status, working directory and output on `exit` or
timeout SHALL be kept.

### Requirement: Varying prompt content goes last
Validator prompts SHALL be card, diff, then the numbered findings; reviewer prompts SHALL put the
per-round review note after the diff.
