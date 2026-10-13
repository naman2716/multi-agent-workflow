# Delta: ntfy

## ADDED Requirements

### Requirement: Replies only answer waiting cards
A reply SHALL change a card only when that card is waiting for a decision; anything else is
ignored and acknowledged.

### Requirement: Secrets stay out of config
Access tokens SHALL come from an environment variable, never from `orchestra.toml`.
