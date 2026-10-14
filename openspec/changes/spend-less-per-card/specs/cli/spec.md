# Delta: cli

## ADDED Requirements

### Requirement: Usage report
`orchestra usage <export-dir>` SHALL report tokens by type (counted once per message id),
estimated USD from one price table, the CLI's own totals, cold starts, resume rounds and large
tool outputs, per card, role, model and session, joined with the card's recorded cost, status,
gate failures and reviews; as a table, Markdown, JSON or CSV. Recorded session roles SHALL be
preferred over prompt-shape guesses.

### Requirement: Card status lists sessions
`orchestra status <card>` SHALL list the card's agent sessions with role, model, runs, cost and
end reason.
