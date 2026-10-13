# Delta: cli

## ADDED Requirements

### Requirement: Manual commands respect live work
`orchestra retry` SHALL refuse merged, running and review cards, and `orchestra cancel` SHALL
refuse running or review cards while a live run holds the lease, unless `--force` is given.

### Requirement: Status shows lifetime and per-run spend
`orchestra status` SHALL show lifetime spend, the last run's spend and the per-run cap.
