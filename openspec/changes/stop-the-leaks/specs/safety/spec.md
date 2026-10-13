# Delta: safety

## ADDED Requirements

### Requirement: Worker shell commands are OS-sandboxed
When `[sandbox].enabled` is true (default), worker Bash commands SHALL run in the Claude Code
sandbox with writes limited to the worktree, temp and configured caches, network limited to
`allowed_domains`, and the run SHALL fail rather than run unsandboxed.

### Requirement: Guard blocks known bypasses
The worker guard SHALL block state-changing git commands even after global options, moving or
configuring refs (`branch -f`, `config`, `stash`, `symbolic-ref`), shell redirects outside the
worktree (except `/dev/null` and temp), and reads of `.env` and credential files.

#### Scenario: Moving the integration ref
- **WHEN** a worker runs `git branch -f orchestra/integration HEAD`
- **THEN** the command is denied with a reason
