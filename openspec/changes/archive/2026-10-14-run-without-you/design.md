# Design: run-without-you

## 11. Triage (orchestration, agents)
- Trigger: `_fail_attempt` when attempts are exhausted, and `_block` (worker or test writer
  reported blocked). Not for decisions, budget stops, or cards already `triaged`.
- Agent (`runner.triage`, planner model, read-only + read guard) runs in a detached `_triage`
  worktree at the integration head (moved under a lock). Input: the card, all feedback, recent
  events, last handoff (report + review), the block reason. `TRIAGE_SCHEMA = {action: rewrite |
  escalate | split | human, reason, card?: {title, spec, acceptance_criteria, non_goals,
  files_scope}, model?, cards?: [plan task items], note_for_human}`.
- Apply (each sets `triaged = 1`, logs `triage`, notifies):
  - `rewrite`: replace the given fields, status `pending`, attempts 0, feedback gains
    "Triage: <reason>". If the scope changed, add `depends_on` edges to open cards that overlap the
    new scope (skipping cards that depend on this one, so no cycles).
  - `escalate`: `model` = given model or `opus`; if the card already ran on that family, fall back
    to `human`. Pending, attempts 0.
  - `split`: build the new cards with `build_tasks(existing=open cards)`; every new card's scope must
    lie within the original scope (unless that was unrestricted), else fall back to `human`. New
    cards inherit `test_first`, `source`, and are marked `triaged` (no second triage). Cards that
    depended on the original now depend on all new cards; the original becomes `cancelled` with
    summary "split into T08, T09".
  - `human` (or any failure/invalid output): block as today, with the triage note added to feedback.
- `[autonomy].triage_actions` limits which actions apply automatically; others become `human`.

## 12. Shipping (new module `ship`, orchestration)
- `Shipper(cfg, store, git, run)`: `push()` = `git push <remote> <integration>` (fast-forward; a
  rejected push is reported, never forced). `upsert_pr(title, body)` = `gh pr list --head <integration>
  --base <base> --state open --json number,url`, then `gh pr edit` or `gh pr create --draft`.
  `checks(pr)` = `gh pr checks <pr> --json name,bucket,link,workflow` polled every 30 s until nothing
  is pending or `ci_wait_minutes` passes. `failed_logs(check)` = `gh run view <run-id> --log-failed`
  (run id parsed from an Actions link), last 3,000 chars; non-Actions checks get their link only.
- PR title: the latest plan's goal (first line, ≤ 72 chars) or "Orchestra: N cards". Body: one
  section per merged card on integration (id, title, summary, decisions, test-first note, attempts,
  cost), totals, and `<magic word> ENG-123` lines for Linear sources (default "Part of", so merging
  doesn't close the ticket).
- Run loop: `_run` drains the queue, then (if `[ship].auto`, this run merged ≥ 1 card, and no stop
  reason) ships. Red CI → one fix card: title "Fix CI: <checks>", spec = failing checks + log tails,
  acceptance "the failing checks pass", scope = files changed on integration vs base plus test globs,
  complexity large, `source = "ci"`. The loop drains again and ships again, up to `ci_fix_rounds`.
- Missing remote/`gh`/auth → one log line and a notification; the run still succeeds.
- `orchestra ship` runs the same step by hand (also prints the PR URL).

## 13. ntfy (new module `ntfy`, orchestration)
- `Ntfy(server, topic, reply_topic, token)`; HTTP via `urllib` in `asyncio.to_thread`.
  `publish(title, message, actions, priority, tags)` posts JSON to the server root.
  `poll(since)` = `GET <server>/<reply_topic>/json?poll=1&since=<id or unix time>`.
- Question message: card id, title, question, why it matters, numbered options, recommendation,
  and "Reply: T03 <n> | T03 r | T03: your answer". Actions (max 3): recommended option and one other
  as `http` POSTs of `T03 <n>` to the reply topic, plus `view` "Other answer…" → web reply page.
- The run polls the reply topic every 10 s while any card is waiting (since = run start, then the
  last message id). Replies are parsed with `asking.parse_answer` after the `T03` prefix; unknown
  ids or cards no longer waiting are ignored (with a confirmation saying so). A valid reply calls
  `store.answer` and publishes "✓ T03 resumes with …".
- Other notifications (blocked, triage, PR, pause, run finished) are published to `topic` alongside
  `notify_cmd`.
- `orchestra ntfy setup [--write]`: random `orchestra-<16 chars>` topic names; prints subscribe
  steps; `--write` adds them to `orchestra.toml`. `orchestra ntfy test` sends a sample question.
- Security: topic names are the secret on public ntfy.sh; optional `NTFY_TOKEN` (Bearer) for
  protected or self-hosted servers. Replies only change cards that are `waiting`.

## 14. Linear (new module `linear`, planning, cli)
- `Linear(api_key)`: `POST https://api.linear.app/graphql`, header `Authorization: <key>`.
  `issue(identifier)` → id, identifier, title, description, url, comments (body, author).
  `comment(issue_id, body)` → `commentCreate`.
- `orchestra plan --linear ENG-123 ["notes"]`: goal = "Linear ENG-123: <title>\n\n<description>\n\n
  Comments:\n…" + notes. The plan stores `source = {"linear": "ENG-123", "url": …}`; each queued
  card gets `source = "linear:ENG-123"`. With `[linear].comment`, comment on queue (cards + ids) and
  after the PR is opened (link).
- `orchestra linear show ENG-123` prints the same goal text (used by `/groom`).
- Missing `LINEAR_API_KEY` → clear error naming the env var.

## Harness
- `/groom`: "if the user gives a Linear ticket, read it with `orchestra linear show <ID>`".
- `/dispatch`: report the PR link and CI result at the end; mention ntfy for answering away.

## Testing
- FakeRunner gains `triage`; fake `gh` script and a local bare remote; fake ntfy and Linear clients.
- Tests: each triage action incl. scope/escalation fallbacks and once-only; ship creates then edits
  the PR, red CI → fix card dispatched → green; no remote/gh → skipped; ntfy question formatting
  (≤ 3 actions) and reply parsing → resume; `plan --linear` goal and source; Linear comment calls.

## As built (differences from the plan above, mostly from the independent review)
- **Shipping is opt-in per project.** `[ship].auto` defaults to false when a config has no
  `[ship]` section, so upgrading never starts pushing to a repo; `orchestra init` writes
  `auto = true`. A missing remote is a log line only; a missing `gh`, failed push or gh error is
  logged and notified.
- **CI verdicts are for the pushed commit.** Shipping waits until the PR's `headRefOid` is the
  pushed SHA, and a result counts only when two polls in a row show the same non-pending set.
  `cancel` buckets give "cancelled" (no fix card); an all-skipped set gives "none".
- **gh targets the right repo and PR**: `--repo owner/name` from the remote URL when it is on
  github.com; fork PRs (`isCrossRepository`) are ignored. The base is the fetched
  `<remote>/<base>` when available, so cards already on the remote base aren't listed again.
- **The CI fix card depends on nothing** (open cards at that point wait on you or are stuck), and
  after every ship the run works any card that became ready meanwhile (the fix card, or a card
  answered from the phone during the CI wait), then ships again. Those later drains only wait
  for answers to questions asked during them.
- **ntfy topics are secrets kept out of `orchestra.toml`**: `orchestra ntfy setup` writes them to
  `.orchestra/ntfy.json` (mode 600, gitignored); `[ntfy].topic`/`reply_topic` still work for
  uncommitted configs. Titles carry the project name. Answer buttons (and reply help) appear only
  when a separate reply topic is set; with `NTFY_TOKEN`, buttons send it too.
- **Buttons are bound to their question**: each carries `T03#<tag>` where the tag hashes the repo,
  card and `asked_at`; a stale button is refused with the current question. Typed replies
  (`T03 2`) answer the current question.
- A question is marked pushed only after delivery, so an undelivered one is re-sent next run.
- **Triage and usage limits**: when a limit stops triage (paused, or the triage call itself is
  limited), the card is blocked without using its triage and is triaged at the start of the next
  run (`meta triage_later`).
- Triage rewrite can set `regenerate_tests` to rewrite a wrong locked test without changing the
  criteria. `orchestra retry` refuses a card cancelled by a split without `--force`.
- `_integrate` records a failure under the merge lock and calls `_fail_attempt` after releasing
  it, since triage may run a whole agent.
- PR text from agents is defused (`Fixes ENG-7` → `Fixes → ENG-7`) so only the
  `linear_magic_word` lines link tickets; the title comes from the latest plan with cards in the PR.
- `orchestra plan` recomputes card ids right before queueing (a live run may have added cards);
  `orchestra ship` refuses while a run is active unless `--force`.
- Linear timeouts and malformed replies become `LinearError`; a failed PR comment never breaks
  the end of a run.
- Not done: squash-merged PRs leave their commits on the integration branch (README explains the
  reset); a reply typed without a tag answers that card's current question.
