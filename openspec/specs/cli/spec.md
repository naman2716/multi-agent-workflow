# cli Specification

## Purpose

Commands and `orchestra.toml` (`cli.py`, `config.py`).

## Requirements

### Requirement: Manual commands respect live work
`orchestra retry` SHALL refuse merged, running and review cards, and `orchestra cancel` SHALL
refuse running or review cards while a live run holds the lease, unless `--force` is given.

### Requirement: Status shows lifetime and per-run spend
`orchestra status` SHALL show lifetime spend, the last run's spend and the per-run cap.

### Requirement: Flag a card test-first
`orchestra test-first <ID>` SHALL set the flag on a card that hasn't started an attempt, and
`--off` SHALL clear it.

### Requirement: Plans start only when you ask
Orchestra SHALL NOT create plans on its own; `orchestra plan` takes a prompt or `--linear <ID>`.

### Requirement: Manual ship and ntfy setup
`orchestra ship` SHALL run the push/PR/CI step on demand (refusing while a run is active unless
`--force`); `orchestra ntfy setup` SHALL generate random topic names; `orchestra linear show <ID>`
SHALL print a ticket as plan input.

### Requirement: Shipping is opt-in per project
A configuration without a `[ship]` section SHALL NOT push or open PRs; `orchestra init` SHALL
write `[ship].auto = true`.
