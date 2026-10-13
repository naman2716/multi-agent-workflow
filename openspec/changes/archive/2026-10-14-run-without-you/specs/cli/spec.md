# Delta: cli

## ADDED Requirements

### Requirement: Plans start only when you ask
Orchestra SHALL NOT create plans on its own; `orchestra plan` takes a prompt or `--linear <ID>`.

### Requirement: Manual ship and ntfy setup
`orchestra ship` SHALL run the push/PR/CI step on demand (refusing while a run is active unless
`--force`); `orchestra ntfy setup` SHALL generate random topic names; `orchestra linear show <ID>`
SHALL print a ticket as plan input.

### Requirement: Shipping is opt-in per project
A configuration without a `[ship]` section SHALL NOT push or open PRs; `orchestra init` SHALL
write `[ship].auto = true`.
