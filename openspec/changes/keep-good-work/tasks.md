# Tasks: keep-good-work

## 1. Git and gates foundations
- [ ] 1.1 `squash_onto`, `start_merge`, `detached_worktree`, test-ref helpers in gitops
- [ ] 1.2 `conflict_markers` gate; `changed_cmd` quick path; env passed to gates
- [ ] 1.3 Config: `[review]`, `[env]`, `[planning]`, `clone_paths`, `skip_setup_if_cloned`, `test_globs`,
      `Gate.changed_cmd`; profiles gain `validator`, `test_writer`; template

## 2. Agents
- [ ] 2.1 `fixable` in review schema; reviewer prompt (tests, revision note, conflict note)
- [ ] 2.2 Reviewer Bash + `reviewer` guard profile; `pick_reviewer_model`
- [ ] 2.3 Validator role and schema
- [ ] 2.4 Test-writer role and schema; locked-path guard for the implementer
- [ ] 2.5 Critic role and schema; planner prompt flags `test_first`

## 3. Orchestration
- [ ] 3.1 `_execute` loop: revise in place, squash, conflict resolution
- [ ] 3.2 Validation of blocking findings
- [ ] 3.3 Test-first phase, locked-test restore, patch reuse, ref cleanup
- [ ] 3.4 Slots, clone_paths, setup skip, env to agents
- [ ] 3.5 Critical-path ordering in `claim_ready`

## 4. Planning and CLI
- [ ] 4.1 Plan in `_plan` worktree at integration head; critic pass; path warnings
- [ ] 4.2 `test_first` through plan/approve/add, render, `orchestra test-first`
- [ ] 4.3 Harness: plan format and groom rules

## 5. Tests and docs
- [ ] 5.1 FakeRunner extensions; new end-to-end and unit tests (design.md list)
- [ ] 5.2 Full suite green (with graphify); real-SDK option check
- [ ] 5.3 README; archive change; commit
