# Tasks: commit-identity

## Phase 1: investigate and propose
- [x] 1.1 Every git call with an identity or `--no-verify`, and its callers (design.md)
- [x] 1.2 How `orchestra ship` authenticates; verify commands (design.md)
- [x] 1.3 `proposal.md`, `design.md`
- [x] 1.4 Approval

## Phase 2: implement (one commit, after approval)
- [x] 2.1 `[git]` config (author, co_author, run_hooks) with validation; init template
- [x] 2.2 Identity resolution; author = committer via GIT_AUTHOR_* / GIT_COMMITTER_*; run-start check
- [x] 2.3 Co-authored-by trailer on every Orchestra commit
- [x] 2.4 run_hooks on agent-work commits; hook failure as gate feedback; test-writer failure path
- [x] 2.5 README: commit identity vs push credentials, hooks, re-authoring an unpushed branch
- [x] 2.6 Spec deltas (git-integration, orchestration, cli)
- [x] 2.7 Tests (design.md list); full suite

## Phase 3: close
- [x] 3.1 PR #6 merged (`db0f598`); deltas merged into `openspec/specs/`; archived 2026-10-16
