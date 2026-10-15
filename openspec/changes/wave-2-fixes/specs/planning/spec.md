# Delta: planning

## ADDED Requirements

### Requirement: Missing estimates are flagged
Building tasks from a plan with a split threshold SHALL warn once for every card without a
positive `estimated_lines`. With `[planning] require_estimates = true`, `orchestra approve` and
`orchestra add` SHALL refuse such a plan and queue nothing. Triage splits stay warn-only.

#### Scenario: Groomed plan without estimates
- **WHEN** `orchestra approve` reads a plan whose 14 cards have no `estimated_lines`
- **THEN** it prints 14 missing-estimate warnings and queues the cards

#### Scenario: Estimates required
- **WHEN** `require_estimates` is true and one card has no estimate
- **THEN** `orchestra approve` exits non-zero, names that card, and queues nothing

### Requirement: Card surgery keeps the graph valid
Manual edits to an open card's `depends_on` SHALL name existing cards and SHALL NOT create a
cycle. A scope change SHALL make the card wait for open cards whose scope overlaps. A split
SHALL cancel the parent as "split into …" and rewire its dependents to all of the children.
