"""Decisions (agents asking the human), usage-limit pause/resume, session reuse, model profiles."""
import asyncio
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_orchestra import FakeRunner, done, make_repo  # noqa: E402

from orchestra.agents import AgentResult  # noqa: E402
from orchestra.config import Config, Gate, Limits  # noqa: E402
from orchestra.limits import LimitSignal, UsageLimits, looks_like_limit  # noqa: E402
from orchestra.orchestrator import LIMIT_NOTE, Orchestrator  # noqa: E402
from orchestra.store import Store, Task  # noqa: E402


def with_session(step, sid):
    def wrapped(wt, n, task):
        r = step(wt, n, task)
        r.session_id = sid
        return r
    return wrapped


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = make_repo(self.tmp)
        self.cfg = Config(repo=self.repo, concurrency=2, max_attempts=3, total_budget_usd=100,
                          gates=[Gate("test", f"{sys.executable} -m unittest -q")],
                          limits=Limits(resume_buffer_s=0, fallback_wait_min=0),
                          decision_wait_minutes=0)
        self.store = Store(self.cfg.state_path / "state.db")
        self.logs: list[str] = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, tid, scope):
        self.store.add_task(Task(id=tid, title=f"task {tid}", spec="", acceptance=["x"], files_scope=scope))

    def run_orch(self, runner, asker=None, poll_s=0.05, during=None):
        async def go():
            orch = Orchestrator(self.cfg, self.store, runner, log=self.logs.append,
                                asker=asker, poll_s=poll_s)
            side = asyncio.create_task(during()) if during else None
            out = await orch.run()
            if side:
                await side
            return out
        return asyncio.run(go())


def asking_runner(extra=None):
    """T01 asks a question first, then finishes using whatever answer it was given."""
    def ask(wt, n, task):
        (wt / "fee.py").write_text("# draft\n")
        return AgentResult({"status": "needs_decision", "summary": "drafted", "files_changed": [],
                            "tests": "", "blocker": "",
                            "decision": {"question": "Cancellation window?", "options": ["2 hours", "4 hours"],
                                         "recommendation": "2 hours", "why_it_matters": "billing"}},
                           0.1, session_id="s1")
    finish = with_session(done(**{"fee.py": "WINDOW = 1\n"}), "s1")
    return FakeRunner({"T01": [ask, finish], **(extra or {})})


class ScriptedInput:
    """Feeds answers to the live prompt; None entries block until cancelled (user away)."""
    def __init__(self, lines, delay=0.05):
        self.lines, self.delay, self.asked = list(lines), delay, 0

    async def readline(self):
        self.asked += 1
        await asyncio.sleep(self.delay)
        if not self.lines:
            return None
        nxt = self.lines.pop(0)
        if nxt is ...:
            await asyncio.Event().wait()  # never answers here
        return nxt

    def drain(self):
        pass


class TestDecisions(Base):
    def test_ask_answer_resume_same_session_and_worktree(self):
        self.add("T01", ["fee.py"])
        self.add("T02", ["other.py"])

        def ask(wt, n, task):
            (wt / "fee.py").write_text("# draft: half fee, window not decided\n")
            return AgentResult({"status": "needs_decision", "summary": "drafted fee module",
                                "files_changed": ["fee.py"], "tests": "", "blocker": "",
                                "decision": {"question": "Cancellation window: 2h or 4h before shift?",
                                             "options": ["2 hours", "4 hours"], "recommendation": "2 hours",
                                             "why_it_matters": "changes what restaurants are charged"}},
                               0.1, session_id="sess-1")

        def finish(wt, n, task):
            assert (wt / "fee.py").read_text().startswith("# draft"), "worktree must be reused"
            (wt / "fee.py").write_text("WINDOW_HOURS = 4\n")
            return AgentResult({"status": "done", "summary": "used 4h", "files_changed": ["fee.py"],
                                "tests": "", "blocker": ""}, 0.1, session_id="sess-1")

        runner = FakeRunner({"T01": [ask, finish], "T02": [done(**{"other.py": "x=1\n"})]})
        counts = self.run_orch(runner)
        t = self.store.get("T01")
        self.assertEqual(t.status, "waiting")
        self.assertEqual(counts["merged"], 1, "other cards keep going while one waits")
        self.assertEqual(t.decision["options"], ["2 hours", "4 hours"])
        self.assertTrue(Path(t.worktree).exists(), "worktree kept while waiting")
        self.assertTrue(any("needs your decision" in l for l in self.logs))

        with self.assertRaises(ValueError):
            self.store.answer("T02", "nope")
        self.store.answer("T01", "4 hours")
        self.run_orch(runner)
        t = self.store.get("T01")
        self.assertEqual(t.status, "merged", "\n".join(self.logs))
        resume_msg = runner.resumes["T01"][1]
        self.assertIn("4 hours", resume_msg)
        self.assertEqual(t.attempts, 1, "asking a question is not a failed attempt")
        self.assertEqual(t.decision["answer"], "4 hours")

    def test_gate_fix_is_sent_into_the_same_session(self):
        self.add("T01", ["calc.py", "test_calc.py"])
        bad = with_session(done(**{"calc.py": "def add(a, b):\n    return a - b\n"}), "s9")
        good = with_session(done(**{"calc.py": "def add(a, b):\n    return a + b  # fixed\n"}), "s9")
        runner = FakeRunner({"T01": [bad, good]})
        self.run_orch(runner)
        self.assertEqual(self.store.get("T01").status, "merged")
        first, second = runner.resumes["T01"]
        self.assertIsNone(first)
        self.assertIn("failed automated checks", second)


class TestLiveQuestions(Base):
    def test_question_pops_up_and_resumes_in_the_same_run(self):
        self.add("T01", ["fee.py"])
        self.add("T02", ["other.py"])
        runner = asking_runner({"T02": [done(**{"other.py": "x=1\n"})]})
        counts = self.run_orch(runner, asker=ScriptedInput(["2"]))
        self.assertEqual(counts["merged"], 2, "\n".join(self.logs))
        self.assertIn("4 hours", runner.resumes["T01"][1])
        shown = "\n".join(self.logs)
        self.assertIn("needs your decision", shown)
        self.assertIn("1. 2 hours", shown)
        self.assertIn("✓ T01 resumes with: 4 hours", shown)

    def test_invalid_then_recommended(self):
        self.add("T01", ["fee.py"])
        runner = asking_runner()
        self.run_orch(runner, asker=ScriptedInput(["9", "", "r"]))
        self.assertEqual(self.store.get("T01").status, "merged")
        self.assertIn("2 hours", runner.resumes["T01"][1])
        self.assertTrue(any("Pick 1-2" in l for l in self.logs))

    def test_later_leaves_it_waiting(self):
        self.add("T01", ["fee.py"])
        counts = self.run_orch(asking_runner(), asker=ScriptedInput(["later"]))
        self.assertEqual(counts["waiting"], 1)
        self.assertTrue(any("Left for later" in l for l in self.logs))

    def test_logs_are_held_while_question_is_on_screen(self):
        self.add("T01", ["fee.py"])
        self.add("T02", ["other.py"])
        runner = asking_runner({"T02": [done(**{"other.py": "x=1\n"})]})
        runner.delays = {"T02": 0.3}  # T02 finishes while the question is showing
        self.run_orch(runner, asker=ScriptedInput(["1"], delay=0.8))
        text = "\n".join(self.logs)
        q_start = text.index("needs your decision  ·")
        answered = text.index("✓ T01 resumes")
        between = text[q_start:answered]
        self.assertNotIn("[T02]", between, "other agents' logs must not interrupt the question")
        self.assertIn("[T02] merged", text)

    def test_answered_elsewhere_withdraws_the_prompt(self):
        self.add("T01", ["fee.py"])

        async def other_terminal():
            for _ in range(200):
                await asyncio.sleep(0.05)
                if self.store.get("T01").status == "waiting":
                    self.store.answer("T01", "4 hours")
                    return
        counts = self.run_orch(asking_runner(), asker=ScriptedInput([...]), during=other_terminal)
        self.assertEqual(counts["merged"], 1, "\n".join(self.logs))
        self.assertTrue(any("answered elsewhere" in l for l in self.logs))

    def test_background_run_waits_for_an_answer_given_elsewhere(self):
        self.cfg.decision_wait_minutes = 0.2
        self.add("T01", ["fee.py"])

        async def claude_code_decide():
            for _ in range(200):
                await asyncio.sleep(0.05)
                if self.store.get("T01").status == "waiting":
                    await asyncio.sleep(0.3)
                    self.store.answer("T01", "4 hours")
                    return
        counts = self.run_orch(asking_runner(), during=claude_code_decide)
        self.assertEqual(counts["merged"], 1, "\n".join(self.logs))
        self.assertTrue(any("waiting up to" in l for l in self.logs))

    def test_background_wait_times_out(self):
        self.cfg.decision_wait_minutes = 0.01  # 0.6s
        self.add("T01", ["fee.py"])
        started = time.time()
        counts = self.run_orch(asking_runner())
        self.assertEqual(counts["waiting"], 1)
        self.assertLess(time.time() - started, 5)

    def test_parse_answer(self):
        from orchestra.asking import parse_answer
        d = {"options": ["2 hours", "4 hours"], "recommendation": "2 hours"}
        self.assertEqual(parse_answer("2", d), ("answer", "4 hours"))
        self.assertEqual(parse_answer("1: but log it", d),
                         ("answer", "2 hours — with this change (overrides the option where they differ): but log it"))
        self.assertEqual(parse_answer("4 hours please", d), ("answer", "4 hours please"))
        self.assertEqual(parse_answer("r", d), ("answer", "2 hours"))
        self.assertEqual(parse_answer("Later", d), ("later", None))
        self.assertEqual(parse_answer("3", d)[0], "invalid")
        self.assertEqual(parse_answer("  ", d)[0], "invalid")


class TestLimits(Base):
    def test_limit_pauses_then_resumes_session_without_using_an_attempt(self):
        self.cfg.concurrency = 1
        self.add("T01", ["a.py"])
        self.add("T02", ["b.py"])

        def cut_off(wt, n, task):
            (wt / "a.py").write_text("partial = True\n")
            return AgentResult({}, 0.05, ok=False, error="rate limit", limited=True,
                               resets_at=time.time() + 1.0, session_id="s1")

        def resume(wt, n, task):
            assert (wt / "a.py").exists(), "partial work kept across the pause"
            (wt / "a.py").write_text("done = True\n")
            return AgentResult({"status": "done", "summary": "", "files_changed": ["a.py"],
                                "tests": "", "blocker": ""}, 0.1, session_id="s1")

        runner = FakeRunner({"T01": [cut_off, resume], "T02": [done(**{"b.py": "x=1\n"})]})
        started = time.time()
        counts = self.run_orch(runner)
        took = time.time() - started
        self.assertEqual(counts["merged"], 2, "\n".join(self.logs))
        self.assertEqual(self.store.get("T01").attempts, 1)
        self.assertEqual(runner.resumes["T01"][1], LIMIT_NOTE)
        self.assertGreaterEqual(took, 0.9, "waited for the reset")
        self.assertTrue(any("usage limit reached" in l for l in self.logs))
        self.assertTrue(any("resuming" in l for l in self.logs))

    def test_limit_during_review_resumes_at_review(self):
        self.add("T01", ["a.py"])
        limited = AgentResult({}, 0.0, ok=False, error="429", limited=True, resets_at=time.time() + 0.5)
        runner = FakeRunner({"T01": [with_session(done(**{"a.py": "x=1\n"}), "s1")]},
                            review={"T01": [limited, "approve"]})
        self.run_orch(runner)
        self.assertEqual(self.store.get("T01").status, "merged")
        self.assertEqual(runner.calls["T01"], 1, "worker not re-run; only the review resumed")
        self.assertEqual(runner.review_calls["T01"], 2)

    def test_no_wait_exits_and_next_run_resumes(self):
        self.cfg.limits.wait_for_reset = False
        self.add("T01", ["a.py"])
        cut = lambda wt, n, t: AgentResult({}, 0, ok=False, limited=True, resets_at=time.time() + 3600,
                                           session_id="s1", error="usage limit")
        runner = FakeRunner({"T01": [cut, with_session(done(**{"a.py": "x=1\n"}), "s1")]})
        self.run_orch(runner)
        t = self.store.get("T01")
        self.assertEqual(t.status, "pending")
        self.assertEqual(t.resume_note, LIMIT_NOTE)
        self.run_orch(runner)  # e.g. after the reset; a fresh process has no pause state
        self.assertEqual(self.store.get("T01").status, "merged")

    def test_tracker(self):
        now = [1000.0]
        lim = UsageLimits(Limits(resume_buffer_s=60, fallback_wait_min=30, stop_new_at=0.9,
                                 max_wait_hours=1), log=lambda m: None, clock=lambda: now[0])
        self.assertTrue(lim.can_dispatch())
        lim.observe(LimitSignal("allowed_warning", 0.95, None, "five_hour"))
        self.assertFalse(lim.can_dispatch())
        self.assertFalse(lim.paused)
        lim.observe(LimitSignal("rejected", 1.0, 1500.0, "five_hour"))
        self.assertEqual(lim.paused_until, 1560.0)
        lim.observe(LimitSignal("rejected", 1.0, None))  # unknown reset: never shortens the pause
        self.assertEqual(lim.paused_until, 2800.0)
        now[0] = 5000.0
        self.assertTrue(lim.gave_up())
        self.assertTrue(looks_like_limit("API Error: 429 rate_limit_error"))
        self.assertFalse(looks_like_limit("TypeError: x is undefined"))


class TestProfiles(unittest.TestCase):
    def test_profiles_and_overrides(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "orchestra.toml").write_text('[models]\nprofile = "economy"\nreviewer = "opus"\n')
        cfg = Config.load(tmp)
        self.assertEqual((cfg.planner_model, cfg.reviewer_model, cfg.small_worker_model,
                          cfg.large_worker_model), ("sonnet", "opus", "haiku", "sonnet"))
        (tmp / "orchestra.toml").write_text("")
        cfg = Config.load(tmp)
        self.assertEqual(cfg.model_profile, "balanced")
        self.assertEqual(cfg.reviewer_model, "sonnet", "default reviewer is no longer opus")
        (tmp / "orchestra.toml").write_text('[models]\nprofile = "cheap"\n')
        with self.assertRaises(ValueError):
            Config.load(tmp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
