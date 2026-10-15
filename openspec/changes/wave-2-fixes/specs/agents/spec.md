# Delta: agents

## ADDED Requirements

### Requirement: Decision options can name the files they need
A worker's decision MAY list, per option, `scope_add` paths the option needs outside its
`files_scope`. The worker prompt SHALL ask for them when an option needs such files.

### Requirement: Data files are kept small
Worker, test-writer and triage prompts SHALL say that a data file which would dominate the diff
is committed in a compact form with a round-trip test, built at test time, or proposed for
`diff_exclude`, never copied as is.

### Requirement: Every agent that runs Bash gets the output cap
With `bash_output_max_chars` set, reviewers and validators SHALL get the same Bash output cap
(head + tail) as workers and test writers.
