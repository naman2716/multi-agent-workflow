# ntfy Specification

## Purpose

Phone notifications and answers through ntfy (`ntfy.py`).

## Requirements

### Requirement: Replies only answer waiting cards
A reply SHALL change a card only when that card is waiting for a decision; anything else is
ignored and acknowledged.

### Requirement: A button answers only its own question
Answer buttons SHALL carry a tag for the question they were sent with, and a reply whose tag
doesn't match the card's current question SHALL be refused.

### Requirement: Secrets stay out of the committed config
Access tokens SHALL come from an environment variable, and `orchestra ntfy setup` SHALL keep the
topic names in the gitignored state directory, never in `orchestra.toml`.
