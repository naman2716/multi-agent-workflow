# Proposal: run-without-you (review recommendations 11–14)

Builds on `main` (sets 1 and 2 merged).

## Why

A run still needs you at four points: a blocked card waits for you even when a rewrite, a
stronger model or a split would fix it; finished work stops at a local branch; questions can
only be answered from the terminal or Claude Code; and a Linear ticket has to be retyped as a
prompt. This change removes those stops without taking the start of work away from you.

## What changes

11. **Self-healing blocked cards.** When a card is about to become blocked (attempts used up, or
    the worker/test writer reports blocked), a triage agent reads its history and picks one action:
    `rewrite` the card (clearer spec/criteria, scope may change), `escalate` it to a stronger model,
    `split` it into smaller cards inside its original scope (dependents are rewired), or hand it to
    you with a short note. Each card is triaged at most once; you're notified of what it did. Off
    when the run budget is reached.
12. **Draft PR + CI fix cards.** At the end of a run that merged anything, Orchestra pushes
    `orchestra/integration`, opens or updates one draft PR into your base branch with a generated
    description (cards, decisions, test-first notes, cost, Linear links), and waits for CI. A red
    run becomes a fix card (failing checks and log tails in its spec) that the same run picks up,
    then the PR is updated again; at most `ci_fix_rounds` (default 2). Uses `git` and the `gh` CLI
    on your Mac, so it works in any repo you've authenticated. Skipped with a note when there is no
    remote or `gh`.
13. **Answer from your phone (ntfy).** Questions are pushed to an ntfy topic with buttons for the
    recommended and one other option, plus "Other answer…" that opens the reply page. Replies
    (`T03 2`, `T03 r`, `T03: your words`) go to a private reply topic the run polls; the agent
    resumes as if you had typed it in the terminal. Blocked cards, triage actions, PR links, pauses
    and run results are pushed too. `orchestra ntfy setup` creates random topic names.
14. **Manual intake from a Linear ticket.** No automatic intake: you start every plan. Besides a
    prompt, `orchestra plan --linear ENG-123 ["extra notes"]` builds the goal from the ticket
    (title, description, comments) and records it on the cards. Optionally it comments on the ticket
    when the plan is queued and when the PR is opened. `orchestra linear show ENG-123` prints a
    ticket (for `/groom`).

## Out of scope

Automatic intake of any kind (by your choice); answering through Slack or Linear comments;
recommendations 15–17 (tracing, golden eval set, best-of-N).

## Impact

- Modules: orchestration, agents, planning, cli, harness; new modules `ship` (git push + gh),
  `ntfy` (publish/poll), `linear` (GraphQL client), seeded as specs.
- New config: `[autonomy]` (`triage`, `triage_actions`), `[ship]` (`auto`, `remote`, `draft`,
  `gh`, `ci_wait_minutes`, `ci_fix_rounds`, `linear_magic_word`), `[ntfy]` (`server`, `topic`,
  `reply_topic`, `token_env`), `[linear]` (`api_key_env`, `comment`). Secrets only via env vars
  (`LINEAR_API_KEY`, optional `NTFY_TOKEN`).
- SQLite: `triaged`, `source` columns. New card status flow: a split card ends `cancelled`
  with "split into …".
- Cost: one triage call per card that would otherwise block; fix cards only when CI is red.
