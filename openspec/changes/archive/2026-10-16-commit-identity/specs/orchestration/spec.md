# Delta: orchestration

## ADDED Requirements

### Requirement: A hook rejection is a failed gate
When the repository's commit hooks reject a worker's commit, the worker SHALL get the hook output
as gate feedback (`[gate:git hooks]`) in the same session, within the existing fix rounds, and the
worktree SHALL keep what the hooks changed. When they reject the test writer's commit, the attempt
SHALL fail with the hook output as feedback.
