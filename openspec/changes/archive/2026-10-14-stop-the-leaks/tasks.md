# Tasks: stop-the-leaks

## 1. Bug fixes (rec 1)
- [x] 1.1 Per-run budget, `meta.last_run`, status line (bug 1)
- [x] 1.2 `decisions` column, `Store.answer` writes it, card renders it; gate-log de-dup (bug 2)
- [x] 1.3 `check_verdict` and override handling in `_execute` (bug 3)
- [x] 1.4 `DiffStats.per_file`, `diff_exclude` config, counted lines in the size gate (bug 6)
- [x] 1.5 Full diff to gates for the secret scan (bug 7)
- [x] 1.6 Plan fingerprint + `--force`; `build_tasks(existing=...)` in plan/approve/add (bug 8)
- [x] 1.7 Glob-aware `_scopes_overlap` (bug 9)
- [x] 1.8 Process-group kill in `gitops.run` (bug 11)
- [x] 1.9 No resume marker leak (bug 12)
- [x] 1.10 `retry` / `cancel` status checks with `--force` (bug 13)

## 2. Run lease and recovery (rec 2)
- [x] 2.1 Lease acquire / heartbeat / release in `Store`; `RunLocked`
- [x] 2.2 Heartbeat task in `Orchestrator.run`; CLI exit code 3 with holder info
- [x] 2.3 Resumable recovery via `Store.interrupted()` and `_recover()`

## 3. Sandbox (rec 3)
- [x] 3.1 `[sandbox]` config with defaults; template and `init`
- [x] 3.2 `sandbox_settings()`, `agent_env()`; wire into worker; env + read guard for planner/reviewer
- [x] 3.3 Guard hardening (git global options, new subcommands, redirects, secret reads)

## 4. Checks in the card (rec 4)
- [x] 4.1 `Gate.fast`; `detect_gates` marks fast gates; template
- [x] 4.2 `task_card(..., checks)` with gate commands; worker prompt line
- [x] 4.3 Opt-in Stop hook (`gates_on_stop`) with report detection and one-block cap

## 5. Tests and docs
- [x] 5.1 Invert repro tests into `tests/test_regressions.py`; remove `review/`
- [x] 5.2 New unit tests listed in design.md
- [x] 5.3 README: config keys, run lease, sandbox, live smoke-test steps
- [x] 5.4 Full suite green on Python 3.12; commit; archive this change
