# Delta: agents

## ADDED Requirements

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

## MODIFIED Requirements

### Requirement: Varying prompt content goes last
Validator prompts SHALL be card, diff, then the numbered findings; reviewer prompts SHALL put the
per-round review note after the diff.
