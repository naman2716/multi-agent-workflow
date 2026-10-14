# Proposal: commit-identity

Builds on `main` at `352e378` (run-without-you, PR #3, and fix-rename-scope, PR #4, merged).
Proposal only; no code changes until approved.

## Why

- Every commit Orchestra makes is hard-coded to `Orchestra <orchestra@local>` (`gitops.py:140,
  184, 197, 203, 216`). GitHub can't link that address to an account, so agent work shows as a
  plain "Orchestra" and doesn't count toward yours. On pcme_dashboard's integration branch, 11 of
  13 commits are `Orchestra <orchestra@local>`; the other 2 are your hand fixes as
  `naman2716 <singh.naman013@gmail.com>`.
- Every Orchestra commit passes `--no-verify` (`gitops.py:141, 198, 217`), which skips the
  project's `pre-commit` and `commit-msg` hooks, silently.

## What changes

New `[git]` section in `orchestra.toml`, written by `orchestra init` with comments:

```toml
[git]
author = "user"     # "user" (default): your git config user.name / user.email
                    # "orchestra": Orchestra <orchestra@local>
                    # or a literal "Name <email>"
co_author = ""      # optional "Name <email>": adds "Co-authored-by: Name <email>" to every Orchestra commit
run_hooks = false   # true: no --no-verify on commits of agent work, so pre-commit / commit-msg hooks run
```

1. **Author.** Each mode resolves to one name and email. They're passed to every committing git
   call (commit, rebase, merge, squash) as both author and committer, through
   `GIT_AUTHOR_*` / `GIT_COMMITTER_*`, so GitHub shows one identity and stray `GIT_*` variables
   in your shell can't split them.
2. **Fail early, never fall back.** With `author = "user"`, `orchestra run` checks
   `git config user.name` and `user.email` in the repo before it takes the run lease. If either is
   missing it exits with a message saying which one, how to set it, and the alternatives. A literal
   that isn't `Name <email>` is a config error when `orchestra.toml` loads. The same goes for
   `co_author`.
3. **Co-author trailer.** When `co_author` is set (and differs from the author), every Orchestra
   commit gets `--trailer "Co-authored-by: <value>"`. The squashed commit that lands on the
   integration branch carries it.
4. **Hooks.** With `run_hooks = true`, commits that record new agent work run without `--no-verify`:
   - the worker's change, including its conflict resolutions
   - the test writer's tests

   A failing hook on the worker's commit is a failed gate, `[gate:git hooks] FAILED` plus the
   hook's output. It counts toward the existing gate rounds (`FIX_ROUNDS`), and the worker fixes
   it in the same session. A failing hook on the test writer's commit fails that attempt with the
   output as feedback.

   Orchestra's own mechanical re-commits of content that already passed keep `--no-verify`,
   in all modes, and the README says so:
   - the squash to one commit
   - re-applied, restored or reverted locked tests
   - a merge that applied cleanly with no agent involved

   With `run_hooks = false` (default), behaviour is today's, and the README says hooks are skipped.
5. **Existing commits stay as they are.** The README explains how to re-author an unpushed
   integration branch by hand, and when it's safe: with no cards in flight.
6. **README section** "Commit identity and push credentials": who commits vs. who pushes, and how
   to check both on your Mac.

## Things to know before approving

- **`run_hooks` runs only hooks that are installed.** A card adding `.pre-commit-config.yaml`
  (pcme T09) doesn't install anything. pcme's `.git/hooks` holds only graphify's `post-checkout`
  and `post-commit`, so `run_hooks = true` there does nothing until you run `pre-commit install`
  in your checkout. Worktrees share that hooks folder.
- **Some hooks already run today.** `--no-verify` doesn't skip `post-commit`, `post-checkout`
  (on `git worktree add` / `checkout`) or `pre-push` (on `orchestra ship`'s `git push`).
  Orchestra sets `GRAPHIFY_SKIP_HOOK=1` (`gitops.py:29`), so graphify's hooks exit early in
  worktrees. That is unchanged.
- Hooks run in the orchestrator process (not the agent sandbox), with the card's slot environment,
  under a 600 s timeout. The pre-commit framework's first run builds its environments; run
  `pre-commit install --install-hooks` once so that doesn't happen mid-card.
- **GitHub linking** needs the commit email to be a verified address on your GitHub account (or
  your `ID+naman2716@users.noreply.github.com` address). `author = "user"` uses whatever your git
  config says; in pcme that is `singh.naman013@gmail.com` (global config, name `naman2716`).

## Out of scope

- Rewriting existing commits automatically.
- Signing commits (GPG/SSH). With `author = "user"`, a `commit.gpgsign = true` in your config
  would now apply to Orchestra's commits too. That's noted in the README, not handled.
- Any change to how `orchestra ship` authenticates (it uses your git and `gh` setup; see design.md).

## Impact

- Modules: git-integration (`gitops.py`: identity and hooks on commits), orchestration (hook
  failure as gate feedback, identity check at run start), cli/config (`[git]`, `init` template),
  README.
- No store or schema changes. Default behaviour changes in one way: **commits are authored by
  you** (`author = "user"`), not `Orchestra <orchestra@local>`. Set `author = "orchestra"` to keep
  today's behaviour.
