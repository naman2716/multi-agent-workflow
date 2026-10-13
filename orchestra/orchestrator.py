"""The orchestration loop.

For each ready task (dependencies merged), in parallel up to `concurrency`:

  fresh worktree from integration head
    -> worker (structured report)
    -> commit -> deterministic gates  --fail--> feedback, worker fixes (same attempt)
    -> independent reviewer           --reject-> feedback, new attempt from clean state
    -> [merge lock] rebase on integration -> re-gate if base moved -> CAS fast-forward
    -> merged; handoff written; worktree removed

Attempts exhausted or worker reports "blocked" -> task goes to `blocked` for a human.
Worker asks a question ("needs_decision") -> task goes to `waiting` with its worktree and
session kept; after `orchestra answer`, the same session resumes with the answer.
Usage limit hit -> dispatch pauses until the reset; cut-off agents resume their sessions.
A per-run budget stops new dispatches once this run has spent it.
Only one run works a repository's queue at a time (a heartbeat lease in the store); after a
crash, cards with their worktree and session intact resume where they stopped.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .agents import (GATE_FIX_PREFIX, Runner, blocking_findings, check_verdict, drop_refuted,
                     pick_reviewer_model, pick_worker_model)
from .config import Config
from .gates import run_gates, summarize_failures
from .asking import LineSource, parse_answer, render_question
from .gitops import Git, clone_into, matches_any, run
from .graph import Graph, GraphData
from .limits import UsageLimits, notify
from .store import Store, Task

FIX_ROUNDS = 2  # gate-failure fix loops inside one attempt, before starting fresh
REVIEW_STAGE = "__resume_at_review__"  # resume marker: worker done, only review was cut off
LIMIT_NOTE = ("You were paused by a usage limit. Continue the task from exactly where you "
              "stopped; your earlier edits are still in the worktree.")
INTERRUPTED_NOTE = ("The orchestrator was interrupted while you worked. Your edits so far are "
                    "still in the worktree; check where you were and finish the task.")
HEARTBEAT_S = 15.0
REVISE_NOTE = ("An independent reviewer found problems with your change. Fix them in this same "
               "change (keep what is right), run the checks, and report done.")
CONFLICT_NOTE = ("Your approved change conflicts with work other cards merged meanwhile. The "
                 "orchestrator merged the latest code into your worktree; these files now contain "
                 "conflict markers (<<<<<<< / ======= / >>>>>>>):\n{files}\n"
                 "Resolve every conflict so both changes' behaviour is kept, remove all markers, run "
                 "the checks, and report done. Don't run git commands.")


def slot_env(cfg: Config, task_id: str, slot: int) -> dict[str, str]:
    """Per-card environment so parallel cards don't share ports or test databases."""
    port_base = cfg.env.port_base + slot * cfg.env.port_span
    env = {"ORCHESTRA_TASK": task_id, "ORCHESTRA_SLOT": str(slot),
           "ORCHESTRA_PORT_BASE": str(port_base), "ORCHESTRA_DB_SUFFIX": f"_orch{slot}"}
    for k, v in cfg.env.vars.items():
        env[k] = str(v).replace("{slot}", str(slot)).replace("{port_base}", str(port_base)) \
            .replace("{task}", task_id)
    return env


def _lockfiles_match(repo: Path, wt: Path, patterns: list[str]) -> bool:
    """Every root-level lockfile is byte-identical in your checkout and the worktree."""
    names = {p.name for d in (repo, wt) for p in d.iterdir() if p.is_file() and matches_any(p.name, patterns)}
    if not names:
        return False  # nothing to compare: don't trust the clone
    return all((repo / n).exists() and (wt / n).exists()
               and (repo / n).read_bytes() == (wt / n).read_bytes() for n in names)


class Orchestrator:
    def __init__(self, cfg: Config, store: Store, runner: Runner,
                 log: Callable[[str], None] = print, asker: LineSource | None = None,
                 poll_s: float = 3.0):
        self.cfg, self.store, self.runner, self._print = cfg, store, runner, log
        # asker set = interactive: questions are asked live in the terminal.
        self.asker, self.poll_s = asker, poll_s
        self.prompts: dict[asyncio.Task, str] = {}
        self._prompt_lock = asyncio.Lock()
        self._prompting = False
        self._held: list[str] = []
        self._slots: dict[str, int] = {}  # running card -> environment slot
        self._asked: set[str] = set()
        self.git = Git(cfg.repo, cfg.worktree_root, cfg.base_branch, cfg.integration_branch)
        self.handoff_dir = cfg.state_path / "handoffs"
        self.stop_reason = ""
        self.graph = Graph(cfg.repo, cfg.worktree_root, cfg.integration_branch, cfg.state_path,
                           cfg.graph, log=lambda m: self.say(None, m))
        self.limits = UsageLimits(cfg.limits, log=lambda m: self.say(None, m, "limit"))
        if getattr(runner, "on_limit", "missing") is None:
            runner.on_limit = self.limits.observe

    def notify(self, title: str, message: str) -> None:
        notify(self.cfg.notify_cmd, title, message)

    def say(self, task_id: str | None, msg: str, kind: str = "info") -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"{stamp} {('[' + task_id + ']') if task_id else '[orchestra]'} {msg}"
        if self._prompting:
            self._held.append(line)  # keep the question on screen readable
        else:
            self._print(line)
        self.store.log(task_id, kind, msg)

    # ------------------------------------------------------------ live questions
    def _schedule_prompts(self) -> None:
        if not self.asker:
            return
        for t in self.store.waiting():
            if t.id not in self._asked and t.id not in self.prompts.values():
                self._asked.add(t.id)
                self.prompts[asyncio.create_task(self._prompt(t.id))] = t.id

    def _cancel_stale_prompts(self) -> None:
        """A question answered elsewhere (another terminal, Claude Code) is withdrawn here."""
        for fut, tid in list(self.prompts.items()):
            if not fut.done() and self.store.get(tid).status != "waiting":
                fut.cancel()

    async def _prompt(self, task_id: str) -> None:
        async with self._prompt_lock:
            t = self.store.get(task_id)
            if t.status != "waiting":
                return
            self._prompting = True
            try:
                others = sum(1 for _ in self._running) if hasattr(self, "_running") else 0
                self.asker.drain()
                self._print(render_question(t.id, t.title, t.decision or {}, others))
                while True:
                    self._print("  answer › ")
                    line = await self.asker.readline()
                    if line is None:  # input closed: leave it for later
                        return
                    kind, val = parse_answer(line, t.decision or {})
                    if kind == "later":
                        self._print(f"  Left for later. Answer any time: orchestra answer {t.id} \"...\"\n")
                        return
                    if kind == "invalid":
                        self._print(f"  {val}")
                        continue
                    if self.store.get(task_id).status != "waiting":
                        self._print("  (already answered elsewhere)\n")
                        return
                    self.store.answer(task_id, val)
                    self._print(f"  ✓ {t.id} resumes with: {val}\n")
                    return
            except asyncio.CancelledError:
                self._print(f"\n  ({task_id} was answered elsewhere)\n")
                raise
            finally:
                self._prompting = False
                held, self._held = self._held, []
                for line in held:
                    self._print(line)

    # ------------------------------------------------------------ main loop
    async def run(self) -> dict[str, int]:
        """Work the queue. Raises store.RunLocked if another live run holds this repo."""
        token = self.store.acquire_lease()
        beat = asyncio.create_task(self._heartbeat(token))
        try:
            return await self._run()
        finally:
            beat.cancel()
            self.store.release_lease(token)

    async def _heartbeat(self, token: str) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            try:
                if not self.store.heartbeat(token):
                    self.say(None, "run lease was taken over by another process", "warn")
            except Exception as e:  # never let bookkeeping kill the run
                self.say(None, f"lease heartbeat failed: {e!r}", "warn")

    def run_spent(self) -> float:
        return self.store.total_cost() - self._start_cost

    async def _recover(self) -> None:
        """Cards a previous run left running/in review: resume them where possible."""
        for t in self.store.interrupted():
            intact = bool(t.worktree and Path(t.worktree).exists() and t.session_id and t.base_commit)
            if intact:
                at_review = t.status == "review" and self._report_path(t).exists()
                self.store.update(t.id, status="pending", attempts=max(t.attempts - 1, 0),
                                  resume_note=REVIEW_STAGE if at_review else _interrupted_note(t))
                self.say(t.id, "recovered from interrupted run → will resume "
                         + ("at review" if at_review else "its session"))
            else:
                self.store.update(t.id, status="pending", attempts=max(t.attempts - 1, 0),
                                  session_id=None, resume_note=None, worktree=None)
                await self.git.remove_worktree(t.id, f"orchestra/task/{t.id}")
                self.say(t.id, "recovered from interrupted run → pending")
            self.store.log(t.id, "recovered", "resumable" if intact else "restarted")

    async def _run(self) -> dict[str, int]:
        self._start_cost = self.store.total_cost()
        started = time.time()
        await self.git.ensure_integration_branch()
        await self._recover()
        if self.cfg.graph.enabled:
            await self.graph.refresh()

        running: dict[asyncio.Task, str] = {}
        self._running = running
        idle_since: float | None = None
        wait_answers_s = self.cfg.decision_wait_minutes * 60
        while True:
            self._schedule_prompts()
            self._cancel_stale_prompts()
            if self.run_spent() >= self.cfg.total_budget_usd and not self.stop_reason:
                self.stop_reason = f"run budget ${self.cfg.total_budget_usd:.2f} reached"
                self.say(None, f"{self.stop_reason}; finishing in-flight tasks, no new dispatch")

            if self.limits.gave_up() and not self.stop_reason:
                self.stop_reason = f"usage limit did not reset within {self.cfg.limits.max_wait_hours}h"
                self.say(None, self.stop_reason)

            # near the limit (warning above stop_new_at): run one agent at a time
            cap = self.cfg.concurrency if self.limits.can_dispatch() else (0 if self.limits.paused else 1)
            free = cap - len(running)
            if free > 0 and not self.stop_reason:
                for t in self.store.claim_ready(free):
                    running[asyncio.create_task(self._execute(t))] = t.id

            if not running and not self.prompts:
                if (self.limits.paused and self.cfg.limits.wait_for_reset and not self.stop_reason
                        and self.store.ready_ids()):
                    until = time.strftime("%H:%M", time.localtime(self.limits.paused_until))
                    self.notify("Orchestra paused", f"Usage limit reached; resuming ~{until}")
                    await self.limits.wait()
                    continue
                if self.limits.paused and not self.cfg.limits.wait_for_reset:
                    self.stop_reason = "usage limit reached (wait_for_reset = false); re-run later"
                # Without a terminal, stay alive for a while so a decision answered elsewhere
                # (Claude Code's /decide, another terminal) resumes in this same run.
                if (not self.asker and not self.stop_reason and self.store.waiting()
                        and wait_answers_s > 0):
                    if idle_since is None:
                        idle_since = time.time()
                        self.say(None, f"waiting up to {self.cfg.decision_wait_minutes:g} min for "
                                 "answers (orchestra decisions / orchestra answer, or /decide)")
                    if time.time() - idle_since < wait_answers_s:
                        await asyncio.sleep(self.poll_s)
                        continue
                break
            idle_since = None
            pending = set(running) | set(self.prompts)
            done, _ = await asyncio.wait(pending, timeout=self.poll_s, return_when=asyncio.FIRST_COMPLETED)
            for fut in done:
                if fut in self.prompts:
                    self.prompts.pop(fut)
                    continue
                tid = running.pop(fut)
                if exc := fut.exception():
                    self.say(tid, f"orchestrator error: {exc!r}", "error")
                    await self._fail_attempt(self.store.get(tid), f"Internal error: {exc!r}")

        for t in self.store.unrunnable():
            self.say(t.id, "cannot run: a dependency is blocked", "warn")
        waiting = self.store.waiting()
        for t in waiting:
            self.say(t.id, f"waiting for your decision: {t.decision.get('question', '')[:200]}", "decision")
        counts = self.store.counts()
        spent = self.run_spent()
        self.store.set_meta("last_run", {"started_at": started, "finished_at": time.time(),
                                         "spent_usd": round(spent, 4), "counts": counts,
                                         "stop_reason": self.stop_reason})
        self.say(None, f"run finished: {counts} · this run ${spent:.2f} "
                 f"(lifetime ${self.store.total_cost():.2f})"
                 + (f" · stopped: {self.stop_reason}" if self.stop_reason else ""))
        for fut in self.prompts:
            fut.cancel()
        msg = f"{counts['merged']} merged, {counts['blocked']} blocked, {len(waiting)} need your decision"
        self.notify("Orchestra run finished", msg + (". Run: orchestra decisions" if waiting else ""))
        return counts

    # ------------------------------------------------------------ one attempt
    async def _execute(self, task: Task) -> None:
        used = set(self._slots.values())
        self._slots[task.id] = next(n for n in range(1, len(used) + 2) if n not in used)
        try:
            await self._attempt(task)
        finally:
            self._slots.pop(task.id, None)

    def _env(self, task: Task) -> dict[str, str]:
        return slot_env(self.cfg, task.id, self._slots.get(task.id, 0))

    async def _attempt(self, task: Task) -> None:
        """One attempt: worker -> gates -> review (-> revise in place) -> merge (-> resolve
        conflicts in place). A fresh attempt only when the approach is wrong or rounds run out."""
        cfg = self.cfg
        env = self._env(task)
        note = task.resume_note
        resuming = bool(note and task.worktree and Path(task.worktree).exists()
                        and task.session_id and task.base_commit)
        if resuming:
            wt, base = Path(task.worktree), task.base_commit
            graph = self.graph.snap_dir / f"{task.id}.json"
            graph = graph if (self.graph.enabled and graph.exists()) else None
        else:
            if task.worktree:  # stale worktree from a pause we can't resume: start clean
                await self.git.remove_worktree(task.id, task.branch)
            wt, branch = await self.git.create_worktree(task.id)
            # the commit the worktree was actually cut from: integration may move right after
            # (another card merging), and squashing onto a newer base would revert that work
            base = await self.git.head_of(wt)
            self.store.update(task.id, worktree=str(wt), branch=branch, base_commit=base, session_id=None)
            self.store.set_meta(_rounds_key(task.id), None)  # a fresh attempt starts its rounds over
            graph = await self.graph.snapshot(task.id)
        self.store.update(task.id, resume_note=None, used_graph=1 if graph else 0)
        verb = "resuming" if resuming else f"attempt {task.attempts}/{cfg.max_attempts}"
        self.say(task.id, f"{verb} on {pick_worker_model(cfg, task)}: {task.title}"
                 + (" (with code graph)" if graph else "") + f" · slot {self._slots.get(task.id)}")

        if not resuming:
            if not await self._prepare_worktree(task, wt, env):
                return
            if task.test_first and not await self._acceptance_tests(task, wt, base, graph, env):
                return

        saved = self._load_report(task) if (resuming and note == REVIEW_STAGE) else None
        # REVIEW_STAGE is an internal marker: without a saved report, resume the worker with a
        # plain explanation instead of sending the marker as its message.
        message = (_interrupted_note(task) if note == REVIEW_STAGE else note) if (resuming and not saved) else None
        # rounds survive pauses and crashes (persisted per attempt), so limits stay limits
        rounds = self.store.get_meta(_rounds_key(task.id)) or {"fixes": 0, "conflicts": 0,
                                                               "review_note": "", "summaries": []}
        while True:
            task = self.store.get(task.id)
            if saved:
                report, gate_log = saved
                saved = None
            else:
                outcome = await self._work_until_green(task, wt, base, graph, resume=message, env=env)
                if outcome is None:
                    return  # already handled: failed, blocked, waiting, or paused
                report, gate_log = outcome
                rounds["summaries"] = rounds["summaries"] + [report.get("summary", "")]
                report = {**report, "summary": _combined_summary(rounds["summaries"])}
                await self.git.squash_onto(wt, base, f"{task.id}: {task.title}\n\n{report['summary']}")
                self._save_report(task, report, gate_log, gated_base=base)
                self.store.set_meta(_rounds_key(task.id), rounds)

            reviewed = await self._review(task, wt, base, report, gate_log, graph, rounds["review_note"], env)
            if reviewed is None:
                return  # paused or failed
            approved, verdict, reasons = reviewed
            task = self.store.get(task.id)
            if not approved:
                overridden = verdict.get("verdict") == "approve"
                feedback = _review_feedback(verdict, reasons if overridden else None)
                fixable = overridden or verdict.get("fixable", True) is not False
                if fixable and rounds["fixes"] < cfg.review.fix_rounds and task.session_id:
                    rounds["fixes"] += 1
                    self.say(task.id, "review found fixable problems → revising in the same session "
                             f"({rounds['fixes']}/{cfg.review.fix_rounds})", "revise")
                    self.store.add_feedback(task.id, feedback)
                    message = f"{REVISE_NOTE}\n\n{feedback}"
                    rounds["review_note"] = (f"This is a revision. The previous review found:\n{feedback}\n"
                                             "Check these are fixed, then review the whole change again.")
                    self.store.set_meta(_rounds_key(task.id), rounds)
                    if await self._park_if_stopping(task, message):
                        return
                    self.store.update(task.id, status="running")
                    continue
                return await self._fail_attempt(task, feedback)

            self.say(task.id, "review approved", "review")
            can_resolve = rounds["conflicts"] < cfg.review.conflict_rounds and bool(task.session_id)
            conflict = await self._integrate(task, wt, report, env, can_resolve)
            if conflict is None:
                return  # merged, or failed and handled
            files, base = conflict
            rounds["conflicts"] += 1
            self.store.update(task.id, base_commit=base, status="running")
            if task.locked_tests:  # locked tests now carry the merged-in content
                self._lock_snapshot(task, wt)
            listed = ", ".join(files)
            self.say(task.id, f"conflicts with newly merged work in {listed} → worker resolves them "
                     f"({rounds['conflicts']}/{cfg.review.conflict_rounds})", "conflict")
            message = CONFLICT_NOTE.format(files="\n".join(f"- {f}" for f in files))
            rounds["review_note"] = (f"This change was approved earlier, then updated to resolve merge "
                                     f"conflicts with work other cards merged meanwhile (in {listed}). Check "
                                     "that the resolution keeps both changes' behaviour, then review as usual.")
            self.store.set_meta(_rounds_key(task.id), rounds)
            if await self._park_if_stopping(task, message):
                return

    async def _prepare_worktree(self, task: Task, wt: Path, env: dict) -> bool:
        """Clone dependency folders from your checkout, then run setup_cmd unless the clone
        already makes it redundant. False if the attempt failed (already handled)."""
        cfg = self.cfg
        cloned = []
        for p in cfg.clone_paths:
            rc, _ = await run(["git", "check-ignore", "-q", p], cfg.repo, check=False)
            if rc != 0:
                self.say(task.id, f"not cloning {p}: it isn't gitignored, so it would be committed", "warn")
                continue
            if await clone_into(cfg.repo / p, wt / p):
                cloned.append(p)
        if cloned:
            self.say(task.id, f"cloned {', '.join(cloned)} from your checkout")
        if not cfg.setup_cmd:
            return True
        if cfg.clone_paths and len(cloned) == len(cfg.clone_paths) and cfg.skip_setup_if_cloned \
                and _lockfiles_match(cfg.repo, wt, cfg.diff_exclude):
            self.say(task.id, "setup_cmd skipped: dependencies cloned and lockfiles match")
            return True
        rc, out = await run(cfg.setup_cmd, wt, timeout=1200, check=False, env=env)
        if rc != 0:
            await self._fail_attempt(task, f"setup_cmd failed:\n{out[-2000:]}")
            return False
        return True

    async def _acceptance_tests(self, task: Task, wt: Path, base: str, graph: Path | None,
                                env: dict) -> bool:
        """Test-first: get failing acceptance tests written (or re-applied from an earlier
        attempt), commit and lock them. False if the attempt ended (already handled)."""
        cfg = self.cfg
        patch = self.handoff_dir / f"{task.id}.tests.patch"
        if task.locked_tests and patch.exists():
            rc, _ = await run(["git", "apply", "--index", str(patch)], wt, check=False)
            if rc == 0:
                await self.git.commit_all(wt, f"{task.id}: acceptance tests")
                self._lock_snapshot(task, wt)
                self.say(task.id, f"re-applied locked acceptance tests: {', '.join(task.locked_tests)}", "tests")
                return True
            self.say(task.id, "saved acceptance tests no longer apply; writing them again", "tests")

        res = await self.runner.write_tests(cfg, replace(task, session_id=None), wt, graph=graph, env=env)
        self.store.add_cost(task.id, res.cost_usd, res.tokens)
        if not res.ok:
            if res.limited:
                await self._pause_for_limit(task, res)
            else:
                await self._fail_attempt(task, f"Test writer run failed: {res.error}")
            return False
        if res.data.get("status") == "blocked":
            await self._block(task, f"Test writer reported blocked: {res.data.get('blocker')}")
            return False
        await self.git.commit_all(wt, f"{task.id}: acceptance tests")
        changed = await self.git.changed_files(wt, base)
        deleted = await self.git.changed_files(wt, base, deleted=True)
        bad = [f for f in changed if not matches_any(f, cfg.test_globs)] + deleted
        if bad:  # only added/edited test files get locked; anything else is put back as it was
            await self.git.revert_paths(wt, base, bad)
            await self.git.commit_all(wt, f"{task.id}: drop the test writer's non-test changes")
            self.say(task.id, f"reverted the test writer's non-test changes: {', '.join(bad)}", "tests")
        tests = [f for f in changed if f not in bad]
        if not tests:
            await self._fail_attempt(task, "Test writer reported done but wrote no test files.")
            return False

        await self.git.squash_onto(wt, base, f"{task.id}: acceptance tests")
        head = await self.git.head_of(wt)
        self.handoff_dir.mkdir(parents=True, exist_ok=True)
        patch.write_text((await run(["git", "diff", "--binary", base, head], wt))[1])
        stats = await self.git.diff_stats(wt, base)
        results = await run_gates(cfg, wt, stats, await self.git.diff_text(wt, base, limit_chars=None),
                                  [], env=env)
        await self.git.discard_uncommitted(wt)
        already_pass = all(r.passed for r in results)
        info = {"tests": tests, "covered": res.data.get("covered", []),
                "assumptions": res.data.get("assumptions", []), "already_pass": already_pass}
        (self.handoff_dir / f"{task.id}.tests.json").write_text(json.dumps(info, indent=2))
        self.store.update(task.id, locked_tests=tests)
        self._lock_snapshot(self.store.get(task.id), wt)
        self.say(task.id, f"acceptance tests written and locked: {', '.join(tests)}"
                 + (" — WARNING: they already pass, so they may not test the new behaviour"
                    if already_pass else ""), "tests")
        return True

    async def _work_until_green(self, task: Task, wt: Path, base: str, graph: Path | None,
                                resume: str | None, env: dict | None = None) -> tuple[dict, str] | None:
        """Worker + gates, with gate failures fed back into the *same* session."""
        cfg = self.cfg
        message = resume
        fell_back = False
        round_no = 0
        while True:
            task = self.store.get(task.id)
            res = await self.runner.work(cfg, task, wt, graph=graph, resume=message, env=env)
            self.store.add_cost(task.id, res.cost_usd, res.tokens)
            if res.session_id:
                self.store.update(task.id, session_id=res.session_id)
            if not res.ok:
                if res.limited:
                    await self._pause_for_limit(task, res)
                    return None
                if message and not fell_back:  # session couldn't be resumed: retry cold once
                    self.say(task.id, "could not resume session; continuing from the task card")
                    message, fell_back = None, True
                    self.store.update(task.id, session_id=None)
                    continue
                await self._fail_attempt(task, f"Worker run failed: {res.error}")
                return None
            report = res.data
            if report.get("status") == "needs_decision":
                await self._ask(task, report)
                return None
            if report.get("status") == "blocked":
                await self._block(task, f"Worker reported blocked: {report.get('blocker')}")
                return None

            await self.git.commit_all(wt, f"{task.id}: {task.title}\n\n{report.get('summary', '')}")
            if task.locked_tests:  # gates always check the locked versions
                restored = self._restore_locked(task, wt)
                if restored:
                    await self.git.commit_all(wt, f"{task.id}: restore locked acceptance tests")
                    self.say(task.id, f"restored locked acceptance tests the worker changed: {restored}", "tests")
                    self.store.add_feedback(task.id, f"The locked acceptance tests {restored} were edited; "
                                            "the orchestrator restored them. Make the code pass them as written.")
            stats = await self.git.diff_stats(wt, base)
            if not stats.files:
                await self._fail_attempt(task, "Worker reported done but changed no files.")
                return None
            diff = await self.git.diff_text(wt, base, limit_chars=None)  # whole diff for the secret scan
            results = await run_gates(cfg, wt, stats, diff, task.files_scope, env=env, quick=round_no > 0,
                                      exempt=task.locked_tests)
            await self.git.discard_uncommitted(wt)
            gate_log = "\n".join(f"- {r.name}: {'pass' if r.passed else 'FAIL'}"
                                 + (f" ({r.detail})" if r.passed and r.detail else "") for r in results)
            if all(r.passed for r in results):
                size = next((r.detail for r in results if r.name == "diff_size"), f"{stats.lines} lines")
                self.say(task.id, f"gates passed ({size}, {len(stats.files)} files)")
                return report, gate_log
            failures = summarize_failures(results)
            self.say(task.id, f"gates failed (round {round_no + 1}): "
                     f"{', '.join(r.name for r in results if not r.passed)}", "gate")
            if round_no >= FIX_ROUNDS:
                # same prefix as the fix notes, so the card keeps only this latest gate log
                await self._fail_attempt(task, f"{GATE_FIX_PREFIX} Still failing after {FIX_ROUNDS} "
                                         f"fix rounds, so the attempt was restarted:\n{failures}")
                return None
            round_no += 1
            fix = f"{GATE_FIX_PREFIX} Fix these, keep the diff focused:\n{failures}"
            self.store.add_feedback(task.id, fix)
            # with a live session, send just the failures (agent keeps its context: fewer tokens)
            message = fix if self.store.get(task.id).session_id else None

    async def _review(self, task: Task, wt: Path, base: str, report: dict, gate_log: str,
                      graph: Path | None, note: str, env: dict) -> tuple[bool, dict, list[str]] | None:
        """Independent review in a fresh context, then validation of blocking findings.
        None if the attempt was paused or failed (already handled)."""
        cfg = self.cfg
        self.store.update(task.id, status="review")
        task = self.store.get(task.id)
        diff = await self.git.diff_text(wt, base)
        impact = ""
        if graph and (gd := GraphData.load(graph)):
            impact = "\n".join(gd.impact((await self.git.diff_stats(wt, base)).files))
        if task.locked_tests:
            note = (note + "\n" if note else "") + self._tests_note(task)
        model = pick_reviewer_model(cfg, pick_worker_model(cfg, task))
        rev = await self.runner.review(cfg, task, wt, diff, report, gate_log, impact=impact, graph=graph,
                                       note=note, model=model, env=env)
        await self.git.discard_uncommitted(wt)  # whatever the reviewer's commands left behind
        self.store.add_cost(task.id, rev.cost_usd, rev.tokens)
        if not rev.ok:
            if rev.limited:
                await self._pause_for_limit(task, rev, stage=REVIEW_STAGE)
            else:
                await self._fail_attempt(task, f"Reviewer run failed: {rev.error}")
            return None
        verdict = rev.data
        approved, reasons = check_verdict(verdict)
        if not approved and cfg.review.validate and blocking_findings(verdict):
            verdict = await self._validate(task, wt, diff, verdict, env)
            if verdict is None:
                return None
            approved, reasons = check_verdict(verdict)
        self._write_handoff(task, report, verdict, base)
        if not approved and verdict.get("verdict") == "approve":
            self.say(task.id, "reviewer approved but reported " + "; ".join(reasons)
                     + " → treated as a rejection", "review-override")
        elif not approved:
            self.say(task.id, f"review rejected ({model})", "review")
        return approved, verdict, reasons

    async def _validate(self, task: Task, wt: Path, diff: str, verdict: dict, env: dict) -> dict | None:
        """Each blocking finding is checked by a validator; refuted ones are dropped."""
        findings = blocking_findings(verdict)
        checked = findings[:max(self.cfg.review.max_validations, 0)]
        # validators that can run tests share the card's env slot (ports, test DB): one at a time
        gate = asyncio.Semaphore(1 if self.cfg.review.reviewer_shell else 2)

        async def one(f: dict):
            async with gate:
                return await self.runner.validate(self.cfg, task, wt, diff, f, env=env)
        results = await asyncio.gather(*(one(f) for f in checked))
        await self.git.discard_uncommitted(wt)
        refuted, limited = [], None
        for f, r in zip(checked, results):
            self.store.add_cost(task.id, r.cost_usd, r.tokens)
            if r.limited:
                limited = r
            elif r.ok and r.data.get("confirmed") is False:
                refuted.append({**f, "evidence": r.data.get("evidence", "")})
                self.store.log(task.id, "validate", f"refuted: {f['text'][:200]} — {r.data.get('evidence', '')[:300]}")
        if limited:
            await self._pause_for_limit(task, limited, stage=REVIEW_STAGE)
            return None
        self.say(task.id, f"validated {len(checked)} blocking finding(s): {len(refuted)} refuted", "validate")
        return drop_refuted(verdict, refuted)

    def _tests_note(self, task: Task) -> str:
        try:
            info = json.loads((self.handoff_dir / f"{task.id}.tests.json").read_text())
        except (OSError, ValueError):
            info = {}
        note = (f"Acceptance tests {', '.join(task.locked_tests)} were written before implementation by a "
                "separate agent and locked; the worker could not edit them. Check the change doesn't "
                "stop them from running (skip markers, conftest or test-config changes, test filters).")
        if info.get("assumptions"):
            note += " Test writer's assumptions: " + "; ".join(info["assumptions"])
        if info.get("already_pass"):
            note += " WARNING: these tests passed before implementation, so check they test the new behaviour."
        return note

    async def _integrate(self, task: Task, wt: Path, report: dict, env: dict,
                         can_resolve: bool) -> tuple[list[str], str] | None:
        """Merge under the lock. Returns (conflicted files, new base) when the worker should
        resolve a conflict (the worktree is left mid-merge, the lock released); else None
        (merged, or the attempt failed and was handled)."""
        message = f"{task.id}: {task.title}\n\n{report.get('summary', '')}"
        async with self.git.merge_lock:
            old = await self.git.head(self.cfg.integration_branch)
            base_before = await self.git.merge_base(wt)
            ok, out = await self.git.rebase_onto_integration(wt)
            if not ok:
                files = await self.git.start_merge(wt, self.cfg.integration_branch) if can_resolve else None
                if files and set(files) & set(task.locked_tests):
                    await self.git.abort_merge(wt)  # locked tests can't be edited to resolve it
                    files = None
                    out = "conflict inside the locked acceptance tests\n" + out
                if files:
                    return files, old  # worker resolves outside the lock
                if files == []:  # merges cleanly as a whole: linearize and re-check below
                    await self.git.commit_merge(wt, message)
                    await self.git.squash_onto(wt, old, message)
                    base_before = None
                else:
                    return await self._fail_attempt(
                        task, "Your change conflicted with work merged meanwhile by another task. "
                        f"Redo it on top of the latest code.\n{out[-1500:]}")
            self.store.update(task.id, base_commit=old)  # a resumed review must diff against this
            if base_before != old or self._gated_base(task) != old:
                # base moved (or gates last passed on another base): re-verify the combined code
                stats = await self.git.diff_stats(wt, old)
                diff = await self.git.diff_text(wt, old, limit_chars=None)
                results = await run_gates(self.cfg, wt, stats, diff, task.files_scope, env=env,
                                          exempt=task.locked_tests)
                await self.git.discard_uncommitted(wt)
                if not all(r.passed for r in results):
                    return await self._fail_attempt(
                        task, "After rebasing on newly merged work, checks fail:\n"
                        + summarize_failures(results))
            await self.git.fast_forward_integration(wt, old)
        self.store.update(task.id, status="merged", summary=report.get("summary", ""))
        self.say(task.id, f"merged into {self.cfg.integration_branch}", "merged")
        await self.git.remove_worktree(task.id, task.branch)
        self.store.set_meta(_rounds_key(task.id), None)
        if self.cfg.graph.enabled:
            await self.graph.refresh()  # keep the map in step with the code
        return None

    # ------------------------------------------------------------ outcomes
    async def _fail_attempt(self, task: Task, reason: str) -> None:
        task = self.store.get(task.id)
        self.store.add_feedback(task.id, reason)
        await self.git.remove_worktree(task.id, task.branch)
        if task.attempts >= self.cfg.max_attempts:
            self.store.update(task.id, status="blocked", worktree=None, session_id=None)
            self.notify(f"Orchestra: {task.id} blocked", reason[:200])
            self.say(task.id, f"blocked after {task.attempts} attempts — needs a human. "
                     f"Last reason: {reason[:300]}", "blocked")
        else:
            self.store.update(task.id, status="pending", worktree=None, session_id=None)
            self.say(task.id, f"retrying: {reason.splitlines()[0][:200]}", "retry")

    async def _block(self, task: Task, reason: str) -> None:
        self.store.add_feedback(task.id, reason)
        await self.git.remove_worktree(task.id, task.branch)
        self.store.update(task.id, status="blocked", worktree=None, session_id=None)
        self.notify(f"Orchestra: {task.id} blocked", reason[:200])
        self.say(task.id, f"blocked — needs a human: {reason[:300]}", "blocked")

    async def _ask(self, task: Task, report: dict) -> None:
        d = report.get("decision") or {}
        decision = {"question": d.get("question") or report.get("summary", "(no question given)"),
                    "options": d.get("options", []), "recommendation": d.get("recommendation", ""),
                    "why_it_matters": d.get("why_it_matters", ""), "asked_at": time.time(),
                    "work_so_far": report.get("summary", "")}
        self.store.update(task.id, status="waiting", decision=decision)
        self.say(task.id, f"needs your decision: {decision['question'][:200]}", "decision")
        self.notify(f"Orchestra: {task.id} needs a decision", decision["question"][:200])

    async def _pause_for_limit(self, task: Task, res, stage: str | None = None) -> None:
        task = self.store.get(task.id)
        self.limits.pause(res.resets_at)
        can_resume = bool(stage or self.store.get(task.id).session_id)
        if not can_resume:
            await self.git.remove_worktree(task.id, task.branch)
        self.store.update(task.id, status="pending", attempts=max(task.attempts - 1, 0),
                          resume_note=(stage or LIMIT_NOTE) if can_resume else None,
                          worktree=task.worktree if can_resume else None)
        self.say(task.id, "paused by usage limit; will resume " +
                 ("its session" if can_resume else "from scratch") + " after the reset", "limit")

    def _report_path(self, task: Task) -> Path:
        return self.handoff_dir / f"{task.id}.pending_report.json"

    def _save_report(self, task: Task, report: dict, gate_log: str, gated_base: str = "") -> None:
        self.handoff_dir.mkdir(parents=True, exist_ok=True)
        self._report_path(task).write_text(json.dumps({"report": report, "gate_log": gate_log,
                                                       "gated_base": gated_base}))

    def _gated_base(self, task: Task) -> str:
        """The base commit the card's gates last passed on (from its saved report)."""
        try:
            return json.loads(self._report_path(task).read_text()).get("gated_base", "")
        except (OSError, ValueError):
            return ""

    def _lock_dir(self, task: Task) -> Path:
        return self.handoff_dir / f"{task.id}.locked"

    def _lock_snapshot(self, task: Task, wt: Path) -> None:
        """Remember the locked tests' exact content (what every gate run must use)."""
        d = self._lock_dir(task)
        shutil.rmtree(d, ignore_errors=True)
        for p in task.locked_tests:
            if (wt / p).is_file():
                (d / p).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(wt / p, d / p)

    def _restore_locked(self, task: Task, wt: Path) -> list[str]:
        """Put back any locked test the worker changed or removed; returns those paths."""
        d, changed = self._lock_dir(task), []
        for p in task.locked_tests:
            src, dst = d / p, wt / p
            if src.is_file() and (not dst.is_file() or dst.read_bytes() != src.read_bytes()):
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                changed.append(p)
        return changed

    async def _park_if_stopping(self, task: Task, message: str) -> bool:
        """Budget reached: don't start another round now. Keep the worktree and session and
        resume this round on the next run, without using an attempt."""
        if not self.stop_reason and self.run_spent() >= self.cfg.total_budget_usd:
            self.stop_reason = f"run budget ${self.cfg.total_budget_usd:.2f} reached"
            self.say(None, f"{self.stop_reason}; finishing in-flight tasks, no new dispatch")
        if not self.stop_reason:
            return False
        self.store.update(task.id, status="pending", resume_note=message,
                          attempts=max(self.store.get(task.id).attempts - 1, 0))
        self.say(task.id, f"{self.stop_reason}: will continue this card next run", "limit")
        return True

    def _load_report(self, task: Task) -> tuple[dict, str] | None:
        try:
            d = json.loads(self._report_path(task).read_text())
            return d["report"], d["gate_log"]
        except (OSError, ValueError, KeyError):
            return None

    def _write_handoff(self, task: Task, report: dict, verdict: dict, base: str) -> None:
        self.handoff_dir.mkdir(parents=True, exist_ok=True)
        (self.handoff_dir / f"{task.id}.attempt{task.attempts}.json").write_text(json.dumps({
            "task": task.id, "title": task.title, "attempt": task.attempts, "base": base,
            "work_report": report, "review": verdict}, indent=2))


def _rounds_key(task_id: str) -> str:
    return f"rounds:{task_id}"


def _combined_summary(summaries: list[str]) -> str:
    """The first report says what the card does; later rounds say what changed since."""
    first, rest = summaries[0], [s for s in summaries[1:] if s]
    return first + ("\n\nLater rounds:\n" + "\n".join(f"- {s}" for s in rest) if rest else "")


def _interrupted_note(t: Task) -> str:
    """Resume message after an interruption. Restates answered decisions, in case the
    session was cut off before it received the latest answer."""
    if not t.decisions:
        return INTERRUPTED_NOTE
    return INTERRUPTED_NOTE + "\nDecisions already made (binding):\n" + "\n".join(
        f"- Q: {d.get('question', '')} A: {d.get('answer', '')}" for d in t.decisions)


def _review_feedback(v: dict, override_reasons: list[str] | None = None) -> str:
    parts = ["Reviewer rejected the previous attempt."]
    if override_reasons:
        parts = ["The reviewer marked the previous attempt as approved, but its own findings "
                 f"({'; '.join(override_reasons)}) mean it can't be merged. Address them."]
    unmet = [c for c in v.get("criteria", []) if not c.get("met")]
    if unmet:
        parts.append("Unmet criteria:\n" + "\n".join(f"- {c['criterion']}: {c['evidence']}" for c in unmet))
    if v.get("scope_creep"):
        parts.append("Out of scope (remove):\n" + "\n".join(f"- {s}" for s in v["scope_creep"]))
    serious = [i for i in v.get("issues", []) if i.get("severity") in ("blocker", "major")]
    if serious:
        parts.append("Issues:\n" + "\n".join(f"- [{i['severity']}] {i['file']}: {i['issue']}" for i in serious))
    if v.get("feedback"):
        parts.append(f"Reviewer guidance: {v['feedback']}")
    return "\n\n".join(parts)
