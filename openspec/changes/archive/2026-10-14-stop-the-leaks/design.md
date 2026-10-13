# Design: stop-the-leaks

## 1. Per-run budget (orchestration, cli)
- `Orchestrator.run()` records `store.total_cost()` at start; the stop check compares
  `total_cost() - start` against `total_budget_usd` (or `--budget`).
- On exit it stores `meta.last_run = {started_at, finished_at, spent_usd, counts}`.
- `orchestra status` prints `lifetime $X · last run $Y · per-run cap $Z`.

## 2. Decisions on the card (agents, orchestration)
- New column `decisions` (JSON list of `{question, answer, at}`), added by the existing migration list.
- `Store.answer()` appends to `decisions` instead of `feedback`.
- `task_card()` renders every decision under `## Decisions already made (binding)`.
- Feedback rendering keeps only the most recent gate-failure note (prefix match on the
  orchestrator's own fix message), then the last 3 notes, each capped at 2,000 chars.
  (As built: the "still failing after 2 rounds" attempt note uses the same prefix, so it
  replaces the fix notes instead of repeating the same log.)

## 3. Verdict cross-check (orchestration)
- `agents.check_verdict(v) -> (approved, reasons)`: approved only if `verdict == "approve"`,
  every criterion `met`, no `blocker`/`major` issue, and `scope_creep` empty.
- An overridden approval is logged as event `review-override` and its reasons go into the feedback.

## 4. Diff size and secret scan (quality-gates)
- `DiffStats` gains `per_file: dict[path, lines]`; `counted(exclude)` sums files not matching
  `[quality].diff_exclude` (default: common lockfiles plus `go.sum`).
- Gates get the full `git diff` (no char limit) for the secret scan; the reviewer still gets the
  60k excerpt.
- (As built) the reviewer's gate log and the "gates passed" event include passing details, e.g.
  `12 lines (+700 in lockfiles/generated files, not counted)`.

## 5. Plans (planning, cli)
- `plan_fingerprint(plan)` = sha256 of the canonical JSON of its tasks. Approved fingerprints
  are kept in `meta.approved_plans`; `plan --approve`, `approve` and `add` refuse a repeat
  unless `--force`.
- `build_tasks(..., existing=open_cards)`: a new card whose scope overlaps (or, with the graph,
  is coupled to) an open card gets `depends_on` that card. Existing cards never gain edges,
  so no cycles.
- Overlap: split each scope entry at its first glob segment; entries overlap when their literal
  bases are equal or nested, or a literal path matches the other's pattern (`in_scope`).
  Conservative by design: false positives serialize, false negatives no longer happen.

## 6. Subprocesses (quality-gates)
- `gitops.run()` starts commands with `start_new_session=True`; on timeout or cancellation it
  sends SIGKILL to the process group.

## 7. Run lease (orchestration)
- `meta.run_lease = {token, pid, host, heartbeat}` acquired in a `BEGIN IMMEDIATE` transaction.
  A lease is live if its heartbeat is under 60 s old and, on the same host, its pid exists.
- `run()` acquires before recovery (else raises `RunLocked`; CLI exits 3 with the holder's
  pid/host), refreshes every 15 s from a background task (also while waiting on usage limits),
  and releases in `finally`.
- `cancel` refuses a running/review card while a live lease exists; `retry` refuses merged,
  running and review cards; `--force` overrides both.
- (As built) `orchestra run` checks for a live lease before anything else, so the message is
  "another run is active" rather than "nothing pending". `run` also treats running/review
  cards as resumable work when no live lease exists.

## 8. Resumable recovery (orchestration)
- `Store.interrupted()` lists running/review cards. For each: if worktree, session and base
  commit exist, set `pending`, give back the attempt, and set `resume_note` to `REVIEW_STAGE`
  (when it was in review and the saved report exists) or `INTERRUPTED_NOTE`. Otherwise reset
  and remove the worktree as today.
- `_execute`: `REVIEW_STAGE` with no saved report resumes the worker with `INTERRUPTED_NOTE`.
- (As built) the interrupted note restates answered decisions, in case the session was cut
  off before it received the latest answer.

## 9. Sandbox and isolation (safety, agents, cli)
- New `[sandbox]` config: `enabled` (true), `allowed_domains`, `allow_write`, `deny_read`,
  `excluded_commands`. Defaults:
  - domains: npm/yarn registries, PyPI, pub.dev + its storage host, Go proxy/sumdb, GitHub hosts;
  - allow_write: npm/pnpm/yarn/pip/pub/go caches (macOS and Linux paths);
  - deny_read: `~/.ssh`, `~/.aws`, `~/.config/gcloud`, `~/.netrc`, `~/.docker/config.json`.
- `agents.sandbox_settings(cfg)` builds `{"enabled", "autoAllowBashIfSandboxed": true,
  "allowUnsandboxedCommands": false, "failIfUnavailable": true, "excludedCommands",
  "network": {"allowedDomains"}, "filesystem": {"allowWrite", "denyRead"}}`. The SDK merges
  `sandbox` into `--settings` JSON verbatim, so keys missing from its TypedDict
  (`failIfUnavailable`, `filesystem`) still reach the CLI.
- Every role gets `env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}`; planner and reviewer also
  get a read guard hook.
- `setup_cmd` and gates run in the orchestrator process, outside the sandbox, as today.
- (As built) `allow_local_binding` (default true) maps to `network.allowLocalBinding`: on macOS,
  sandboxed commands otherwise can't listen on ports or reach localhost services, which breaks
  most backend test suites. Trade-off: anything on localhost is reachable while it's on.
- Note from the sandbox docs: in a linked worktree the sandbox still lets git write refs in the
  shared `.git` (only `hooks/` and `config` are protected), so the guard's `branch -f` /
  `update-ref` blocks remain necessary.
- Guard (second layer): git subcommands detected after global options (`git -C x`, `git -c k=v`);
  adds `branch -f/-m/-M`, `config`, `stash`, `symbolic-ref`; blocks redirects (`>`, `>>`, `tee`)
  to absolute or `..` paths outside the worktree except `/dev/null`, `/tmp`, `$TMPDIR`; blocks
  Read/Grep/Glob on `.env*` (except `.example`/`.sample`/`.template`) and credential paths.

## 10. Checks in the card and Stop hook (agents, quality-gates)
- `Gate.fast: bool` (default false; `init` marks lint/typecheck/analyze/vet as fast).
- `task_card(t, checks)` adds `## Checks the orchestrator will run` with each command; the worker
  is told to run them before reporting done.
- `[quality].gates_on_stop = false` (opt-in). When on, the worker gets a Stop hook that:
  finds the worker's report (`last_assistant_message`, else the last structured tool input in
  the transcript); does nothing unless it is `status: done`; runs the fast gates in the worktree;
  on failure returns `{"decision": "block", "reason": <failures>}` at most once per session.
  Any error inside the hook allows the stop.
- Risk: how a blocked Stop interacts with structured output isn't documented; hence opt-in
  until a live smoke run confirms it.

## Testing
- Each fixed bug's repro test is inverted into `tests/test_regressions.py`; `review/` is removed.
- New unit tests: lease (live, stale, release), recovery paths, verdict check, fingerprint,
  cross-plan serialization, glob overlap, process-group kill, sandbox settings shape, guard
  cases, card rendering, Stop-hook decision logic with a fake transcript.
- Full suite runs on Python 3.12. Live SDK behaviour (sandbox start, Stop hook) needs one paid
  smoke run on your Mac; steps go in the README.
