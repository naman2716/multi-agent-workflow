# Delta: cli

## MODIFIED Requirements

### Requirement: Manual commands respect live work
`orchestra retry` SHALL refuse merged, running and review cards, and `orchestra cancel` SHALL
refuse running or review cards while a live run holds the lease, unless `--force` is given.
`orchestra edit`, `orchestra scope` and `orchestra split` SHALL refuse merged cards, and running
or review cards while a live run holds the lease, unless `--force` is given.

## ADDED Requirements

### Requirement: Retry gives a fresh triage
`orchestra retry` SHALL clear the card's `triaged` flag unless `--keep-triage` is given, so the
next block is triaged again.

#### Scenario: Environment block, then a size block
- **WHEN** a card's triage was spent on a stale-branch block and the human retries it
- **THEN** its next block (e.g. over the size limit) is triaged before it reaches the human

### Requirement: Edit and split cards from the CLI
`orchestra edit <ID>` SHALL change an open card's `depends_on`, `files_scope`, acceptance
criteria, spec, title or non-goals, and log each change. `orchestra split <ID> <plan-file>` SHALL
split a card through the same path triage uses. `orchestra scope <ID> --add/--remove` SHALL change
`files_scope` only.

### Requirement: Answers can widen scope
`orchestra answer <ID> --scope-add <glob>...` SHALL add the paths to the card's `files_scope`
along with the answer. Absolute paths, `..` and paths under `.git/` or the state directory SHALL
be refused.

### Requirement: Detached runs
`orchestra run --detach` SHALL start the run in a new process session with stdin closed and output
appended to `<state_dir>/run.log`, print its pid and log path, and return at once.

### Requirement: Usage report reads the export as written
`orchestra usage` SHALL:
- label sessions resumed with a decision answer `decision`;
- count tool results that carry the Bash cap's truncation marker as already capped, not as over
  the cap;
- leave the human interactive session out of the cap what-if;
- fold cancelled cards with no sessions into one line;
- accept `tasks-full.json` and `events.log` as the queue state.
