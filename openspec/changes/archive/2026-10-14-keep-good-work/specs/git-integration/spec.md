# Delta: git-integration

## ADDED Requirements

### Requirement: One commit per card
Before review, a card's commits SHALL be squashed into a single commit on top of its base, so
integration gains exactly one commit per merged card.

### Requirement: Squashing never reverts other cards' work
A card SHALL be squashed onto the commit its worktree was created from, never onto a later
integration head, so work merged by other cards in the meantime is never reverted.

#### Scenario: Another card merges right after a worktree is created
- **WHEN** integration moves between creating a card's worktree and that card's squash
- **THEN** after the card merges, the other card's files are still on integration
