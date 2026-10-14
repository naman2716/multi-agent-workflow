# Tasks: run-without-you

## 1. Foundations
- [x] 1.1 Config sections `[autonomy]`, `[ship]`, `[ntfy]`, `[linear]`; template
- [x] 1.2 Store columns `triaged`, `source`; plan source in meta

## 2. Triage (rec 11)
- [x] 2.1 `TRIAGE_SCHEMA`, prompt, `runner.triage`
- [x] 2.2 `_triage` in `_fail_attempt`/`_block`; rewrite / escalate / split / human; notifications

## 3. Shipping (rec 12)
- [x] 3.1 `ship.py`: push, PR upsert, checks polling, failed logs, PR body
- [x] 3.2 Run loop: drain → ship → CI fix card → drain, capped; `orchestra ship`

## 4. ntfy (rec 13)
- [x] 4.1 `ntfy.py`: publish, poll, question message with actions, reply parsing
- [x] 4.2 Wire into `_ask`, notifications, run-loop polling; `orchestra ntfy setup|test`

## 5. Linear (rec 14)
- [x] 5.1 `linear.py`: issue, comment
- [x] 5.2 `orchestra plan --linear`, `orchestra linear show`, card source, comments on queue/PR

## 6. Harness, tests, docs
- [x] 6.1 Harness skill text (`/groom`, `/dispatch`)
- [x] 6.2 Tests (design.md list); full suite green
- [x] 6.3 README; archive; commit
