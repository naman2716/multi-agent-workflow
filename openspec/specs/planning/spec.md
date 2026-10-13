# planning Specification

## Purpose

Plan validation, ids and serialization of overlapping work (`planning.py`).

## Requirements

### Requirement: A plan is queued once
Approving a plan whose tasks match an already approved plan SHALL be refused unless `--force`.

### Requirement: New cards wait for overlapping open cards
A new card whose scope overlaps, or is coupled to, a pending, running, review or waiting card
SHALL depend on that card.

### Requirement: Glob scopes overlap by their literal base
Scope entries SHALL be treated as overlapping when their literal bases (the path before the
first glob segment) are equal or nested, or when a literal path matches the other's pattern.

#### Scenario: Recursive glob vs file
- **WHEN** one card has `src/**/*.py` and another `src/api/views.py`
- **THEN** the second depends on the first
