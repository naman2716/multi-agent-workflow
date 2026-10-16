# Delta: git-integration

## ADDED Requirements

### Requirement: Commits use the configured identity
Every commit, rebase and merge Orchestra makes SHALL use the `[git] author` identity as both
author and committer: the repository's `git config user.name` / `user.email` ("user", default),
`Orchestra <orchestra@local>` ("orchestra"), or a literal `Name <email>`. Environment variables
SHALL NOT be able to make the committer differ from the author.

#### Scenario: Default
- **WHEN** `author = "user"` and git config says `naman2716 <singh.naman013@gmail.com>`
- **THEN** the commit landed on the integration branch has that author and committer

### Requirement: No silent fallback identity
With `author = "user"`, a missing `user.name` or `user.email` SHALL stop `orchestra run` before it
takes the run lease, with a message naming what is missing and how to set it.

### Requirement: Co-author trailer
With `co_author` set to `Name <email>` (and not the author), every Orchestra commit SHALL carry a
`Co-authored-by: Name <email>` trailer.

### Requirement: Commit hooks on agent work
With `run_hooks = true`, commits of agent work (worker changes and conflict resolutions, test
writer's tests) SHALL run the repository's installed commit hooks; Orchestra's re-commits of work
that already passed (squash, locked-test handling, clean merges) SHALL skip them. With
`run_hooks = false` (default) all Orchestra commits skip them.
