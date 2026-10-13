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
import time
from pathlib import Path
from typing import Callable

from .agents import GATE_FIX_PREFIX, Runner, check_verdict, pick_worker_model
from .config import Config
from .gates import run_gates, summarize_failures
from .asking import LineSource, parse_answer, render_question
from .gitops import Git, run
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
        cfg = self.cfg
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
            base = await self.git.head(cfg.integration_branch)
            self.store.update(task.id, worktree=str(wt), branch=branch, base_commit=base, session_id=None)
            graph = await self.graph.snapshot(task.id)
        self.store.update(task.id, resume_note=None, used_graph=1 if graph else 0)
        verb = "resuming" if resuming else f"attempt {task.attempts}/{cfg.max_attempts}"
        self.say(task.id, f"{verb} on {pick_worker_model(cfg, task)}: {task.title}"
                 + (" (with code graph)" if graph else ""))

        if cfg.setup_cmd and not resuming:
            rc, out = await run(cfg.setup_cmd, wt, timeout=1200, check=False)
            if rc != 0:
                return await self._fail_attempt(task, f"setup_cmd failed:\n{out[-2000:]}")

        if resuming and note == REVIEW_STAGE and (saved := self._load_report(task)):
            report, gate_log = saved
        else:
            # REVIEW_STAGE is an internal marker: without a saved report, resume the worker
            # with a plain explanation instead of sending the marker as its message.
            message = (_interrupted_note(task) if note == REVIEW_STAGE else note) if resuming else None
            outcome = await self._work_until_green(task, wt, base, graph, resume=message)
            if outcome is None:
                return  # already handled: failed, blocked, waiting, or paused
            report, gate_log = outcome
            self._save_report(task, report, gate_log)

        # independent review in a fresh context
        self.store.update(task.id, status="review")
        task = self.store.get(task.id)
        diff = await self.git.diff_text(wt, base)
        impact = ""
        if graph and (gd := GraphData.load(graph)):
            impact = "\n".join(gd.impact((await self.git.diff_stats(wt, base)).files))
        rev = await self.runner.review(cfg, task, wt, diff, report, gate_log, impact=impact, graph=graph)
        self.store.add_cost(task.id, rev.cost_usd, rev.tokens)
        if not rev.ok:
            if rev.limited:
                return await self._pause_for_limit(task, rev, stage=REVIEW_STAGE)
            return await self._fail_attempt(task, f"Reviewer run failed: {rev.error}")
        verdict = rev.data
        self._write_handoff(task, report, verdict, base)
        approved, reasons = check_verdict(verdict)
        if not approved:
            overridden = verdict.get("verdict") == "approve"
            if overridden:
                self.say(task.id, "reviewer approved but reported "
                         + "; ".join(reasons) + " → treated as a rejection", "review-override")
            else:
                self.say(task.id, "review rejected", "review")
            return await self._fail_attempt(task, _review_feedback(verdict, reasons if overridden else None))
        self.say(task.id, "review approved", "review")
        await self._integrate(task, wt, report)

    async def _work_until_green(self, task: Task, wt: Path, base: str, graph: Path | None,
                                resume: str | None) -> tuple[dict, str] | None:
        """Worker + gates, with gate failures fed back into the *same* session."""
        cfg = self.cfg
        message = resume
        fell_back = False
        round_no = 0
        while True:
            task = self.store.get(task.id)
            res = await self.runner.work(cfg, task, wt, graph=graph, resume=message)
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
            stats = await self.git.diff_stats(wt, base)
            if not stats.files:
                await self._fail_attempt(task, "Worker reported done but changed no files.")
                return None
            diff = await self.git.diff_text(wt, base, limit_chars=None)  # whole diff for the secret scan
            results = await run_gates(cfg, wt, stats, diff, task.files_scope)
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

    async def _integrate(self, task: Task, wt: Path, report: dict) -> None:
        async with self.git.merge_lock:
            old = await self.git.head(self.cfg.integration_branch)
            base_before = await self.git.merge_base(wt)
            ok, out = await self.git.rebase_onto_integration(wt)
            if not ok:
                return await self._fail_attempt(
                    task, "Your change conflicted with work merged meanwhile by another task. "
                    f"Redo it on top of the latest code.\n{out[-1500:]}")
            if base_before != old:  # base moved: re-verify against the combined code
                stats = await self.git.diff_stats(wt, old)
                diff = await self.git.diff_text(wt, old, limit_chars=None)
                results = await run_gates(self.cfg, wt, stats, diff, task.files_scope)
                await self.git.discard_uncommitted(wt)
                if not all(r.passed for r in results):
                    return await self._fail_attempt(
                        task, "After rebasing on newly merged work, checks fail:\n"
                        + summarize_failures(results))
            await self.git.fast_forward_integration(wt, old)
        self.store.update(task.id, status="merged", summary=report.get("summary", ""))
        self.say(task.id, f"merged into {self.cfg.integration_branch}", "merged")
        await self.git.remove_worktree(task.id, task.branch)
        if self.cfg.graph.enabled:
            await self.graph.refresh()  # keep the map in step with the code

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

    def _save_report(self, task: Task, report: dict, gate_log: str) -> None:
        self.handoff_dir.mkdir(parents=True, exist_ok=True)
        self._report_path(task).write_text(json.dumps({"report": report, "gate_log": gate_log}))

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
