# Design: commit-identity

## Investigation: where Orchestra runs git with an identity or `--no-verify`

Line numbers are on `main` at `352e378`.

### The git calls that set an identity or skip hooks (`orchestra/gitops.py`)

| Line | Method | Command | Identity | `--no-verify` | Hooks it would run without it |
|---|---|---|---|---|---|
| 140-141 | `commit_all` | `git -c user.name=Orchestra -c user.email=orchestra@local commit -q --no-verify -m` | yes | yes | pre-commit, commit-msg |
| 184-185 | `rebase_onto_integration` | `git -c … rebase <integration>` | yes (committer of rewritten commits) | n/a | rebase runs pre-rebase/post-rewrite only, never pre-commit |
| 197-198 | `squash_onto` | `git reset --soft <base>` then `git -c … commit -q --no-verify --allow-empty -m` | yes | yes | pre-commit, commit-msg |
| 203-204 | `start_merge` | `git -c … merge --no-ff --no-commit <integration>` | yes (unused: `--no-commit`) | n/a | none (no commit made) |
| 216-217 | `commit_merge` | `git add -A` then `git -c … commit -q --no-verify -m` | yes | yes | pre-commit, commit-msg |

### Who calls them (`orchestra/orchestrator.py`)

| Line | Path | Call | Content |
|---|---|---|---|
| 567 | worker round done | `squash_onto(wt, base, "Tnn: title\n\nsummary")` | mechanical: re-commits the card as one commit |
| 649-651 | test-first, re-apply saved tests | `git apply --index` + `commit_all("Tnn: acceptance tests")` | mechanical: tests written earlier |
| 668 | test writer done | `commit_all("Tnn: acceptance tests")` | **new agent work** |
| 673-674 | test writer touched non-test files | `revert_paths` + `commit_all("… drop the test writer's non-test changes")` | mechanical |
| 681 | test-first | `squash_onto(wt, base, "Tnn: acceptance tests")` | mechanical |
| 732 | worker report, every gate round; also commits a worker-resolved merge (MERGE_HEAD) | `commit_all("Tnn: title\n\nsummary")` | **new agent work** |
| 736 | worker edited locked tests | `commit_all("Tnn: restore locked acceptance tests")` | mechanical |
| 857 | integrate | `rebase_onto_integration` | mechanical (rewrites the card's commit) |
| 859 | conflict with newly merged work | `start_merge` | no commit; the worker resolves, then line 732 commits it |
| 867-868 | whole-change merge applied cleanly | `commit_merge` + `squash_onto` | mechanical, no agent involved |
| 885 | merge | `fast_forward_integration` → `git update-ref` (`gitops.py:249`) | no commit (moves the branch) |

- **Fix CI path:** `_queue_ci_fix` (`orchestrator.py:458-481`) only queues a normal card
  (`source = "ci"`). Its commits go through the same lines above; it has no git calls of its own.
- **Conflict resolution:** `start_merge` (859), then the worker resolves in its session, then
  `commit_all` (732) records the merge, `squash_onto` (567) linearizes it, and `rebase` (857) runs.
- **Ship** (`orchestra/ship.py`): no commits. `git fetch -q <remote> refs/heads/<base>:…` (90),
  `git push <remote> <integration>:<integration>` (131, no `--no-verify`, so a `pre-push` hook
  runs), and `gh pr list/create/edit/view/checks`, `gh run view` (`_gh`, 73-76).
- **Hooks that already run:** `git worktree add` (`gitops.py:120`, `243`) and
  `git checkout` (`gitops.py:229`, `238`) run `post-checkout`; every commit runs `post-commit`;
  ship's push runs `pre-push`. `run()` sets `GRAPHIFY_SKIP_HOOK=1` (`gitops.py:29`) so graphify's
  post-commit/post-checkout hooks exit early inside worktrees.

## Investigation: how `orchestra ship` authenticates

Orchestra holds no GitHub credentials. It shells out to `git` and `gh` as you, in your checkout,
with `QUIET_ENV` (`ship.py:28-29`: `GIT_TERMINAL_PROMPT=0`, `GH_PROMPT_DISABLED=1`, so nothing can
prompt; a missing credential fails the step and the run reports "not shipped").

- **git push** uses the remote URL as configured: on this Mac pcme's `origin` is
  `https://github.com/naman2716/pcme_dashboard.git` (HTTPS). Git asks its credential helper:
  `credential.helper=osxkeychain`, set in Xcode's system gitconfig
  (`/Applications/Xcode.app/Contents/Developer/usr/share/git-core/gitconfig`). The token or
  password lives in your macOS login keychain as an internet password for `github.com`. There is no
  repo- or user-level helper, and `gh auth setup-git` has not been run. With an SSH remote
  (`git@github.com:…`) it would use your SSH agent and keys instead.
- **gh** uses its own login: `gh auth status` shows account `naman2716`, token stored in the
  system keyring, git protocol https, scopes `gist, read:org, repo, workflow`. No `GH_TOKEN` /
  `GITHUB_TOKEN` is set in the environment (either would override the keyring).
- So push and PR calls use two different credentials: the keychain entry for git, and gh's own
  token.
- `orchestra ship` passes `--repo owner/name` to gh, parsed from the remote URL (`ship.py:78-84`),
  so gh never guesses the repo.
- **Nothing stored by Orchestra:** the code has no GitHub token handling. The only secrets it reads
  are `NTFY_TOKEN` and `LINEAR_API_KEY`, from environment variables by name (`config.py:126`,
  `linear.py:48`). `.orchestra/` holds the queue, handoffs and ntfy topic names only.

Commands to verify on your Mac:

```bash
git -C ~/Downloads/pcme_dashboard remote get-url origin                     # URL form: https:// or git@
git -C ~/Downloads/pcme_dashboard config --show-origin --get-all credential.helper   # which helper, set where
security find-internet-password -s github.com                               # the keychain entry (metadata only; no -w)
gh auth status                                                               # gh's account, token store, scopes
env | grep -E '^(GH_|GITHUB_)' | cut -d= -f1                                 # token env vars that would override gh
grep -rnE 'GH_TOKEN|GITHUB_TOKEN|credential|password' ~/tools/orchestra/orchestra   # Orchestra's code
```

## Design

### Identity

`config.GitSettings(author="user", co_author="", run_hooks=False)` under `Config.git`.
`author` and `co_author` are validated at load: `"user"`, `"orchestra"` or `Name <email>`
(non-empty name, an `@` in the email, no newlines).

`gitops.Identity(name, email)` resolved once per process:
- `"orchestra"` → `Orchestra <orchestra@local>`
- literal → parsed
- `"user"` → `git config user.name` / `git config user.email` run in the repo (so local, global,
  system and `includeIf` all apply). Missing either → `IdentityError("… git config user.email is not
  set in <repo> … run git config --global user.email you@example.com, or set [git] author =
  \"Name <email>\" or \"orchestra\" in orchestra.toml")`.

`cmd_run` resolves it before taking the lease and exits 1 with that message. `Orchestrator._run`
resolves it again (library use) before any work.

Every committing call drops `-c user.name=… -c user.email=…` and instead runs with env
`GIT_AUTHOR_NAME/EMAIL` and `GIT_COMMITTER_NAME/EMAIL` set to the identity. `Git` gets an
`identity` and a helper `_commit_env()`. `rebase` and `merge` get the same env.

### Co-author

`commit` gets `--trailer "Co-authored-by: <co_author>"` (git ≥ 2.32; this Mac has 2.54) when
`co_author` is set and isn't the author. git skips a trailer identical to an existing one
(`trailer.ifExists` default `addIfDifferentNeighbor`). It's applied on every Orchestra commit,
including the squash, so the commit that lands carries it.

### Hooks

`Git.commit_all(wt, message, hooks=False, env=None)` returns `CommitResult(committed, ok, output)`.
- `hooks=True` with `run_hooks` → no `--no-verify`, `check=False`, timeout 600 s, the card's slot
  env, output captured.
- Everything else keeps `--no-verify`.

Call sites with `hooks=True`: worker commit (732) and test writer commit (668). All mechanical
commits in the table above keep `--no-verify`.

Worker path (`_work_until_green`), when the hook fails:
- The worktree is **not** reset: formatter hooks' fixes stay, the worker sees them, and the next
  `git add -A` takes them.
- The failure becomes `GateResult(False, "git hooks", tail of output)` and goes through the same
  `summarize_failures` / `GATE_FIX_PREFIX` / `round_no` / `FIX_ROUNDS` logic as a gate failure.
  The worker is resumed in its session with it; after `FIX_ROUNDS` the attempt restarts.
- The gates themselves aren't run on that round (nothing was committed).

Test writer path: a failed hook → `_fail_attempt(task, "Test writer's commit was rejected by the
repository's git hooks:\n<tail>")`.

A missing hooks folder or no installed hooks → git commits normally; nothing to detect.

### init and README

The `CONFIG_TEMPLATE` gets the `[git]` block above.

README "Commit identity and push credentials":
- **Commit identity** is the name and email written into each commit (author/committer); it's
  what GitHub uses to attribute commits. Set by `[git] author`.
- **Push credentials** decide whether you're allowed to push and open PRs. Orchestra uses your
  git credential helper and `gh` login and stores none (verify commands above).
- What `run_hooks` does and doesn't run.
- How to re-author an unpushed integration branch, only when `orchestra status` shows nothing
  running, in review or waiting. Rewriting changes SHAs that in-flight cards' worktrees and base
  commits point to.

  ```bash
  git -C <repo> log --format='%h %an <%ae>' main..orchestra/integration   # check first
  git -C <repo> rebase -r --exec 'git commit --amend --no-edit --no-verify --reset-author' \
      main orchestra/integration
  ```

  `--reset-author` takes the identity from your current git config. Then `git checkout -` back to
  where you were.

## Tests (Phase 2)

- Each author mode:
  - `user` → commits show your config's name and email as author and committer
  - `orchestra` → `Orchestra <orchestra@local>`
  - literal → as given; an invalid literal raises at config load
  - stray `GIT_COMMITTER_EMAIL` in the env doesn't split author and committer
- Missing `user.email` (isolated `HOME`/`GIT_CONFIG_GLOBAL`, no repo config): `cmd_run` exits with
  the message and takes no lease, and `Orchestrator.run` raises before any card is claimed.
- `co_author` adds exactly one trailer to the squashed commit on the integration branch; an empty
  value adds none.
- `run_hooks = false`: a failing `pre-commit` hook installed in the test repo doesn't stop the card
  (today's behaviour).
- `run_hooks = true`: the same hook runs. A failing hook becomes `[gate:git hooks]` feedback in the
  same session, the worker's fix passes and the card merges. A hook that keeps failing uses up the
  rounds and restarts the attempt. Mechanical commits (squash, restore locked tests) don't run it.
- `orchestra init` writes the `[git]` section, and it loads.
