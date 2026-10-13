"""Regression tests for the bugs found in the Oct 2026 review (formerly review/ repro tests,
now asserting the fixed behaviour). Bug numbers refer to the review's table."""
import asyncio
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestra.agents import AgentResult, task_card  # noqa: E402
from orchestra.gates import run_gates  # noqa: E402
from orchestra.gitops import Git, run  # noqa: E402
from orchestra.guards import check  # noqa: E402
from orchestra.orchestrator import INTERRUPTED_NOTE, REVIEW_STAGE  # noqa: E402
from orchestra.planning import _scopes_overlap  # noqa: E402
from orchestra.store import Store  # noqa: E402
from tests.test_orchestra import Base, FakeRunner, done, sh  # noqa: E402


class Regressions(Base):
    def test_bug1_budget_is_per_run(self):
        self.cfg.total_budget_usd = 0.5
        for tid in ("A", "B", "C"):  # each run spends ~0.3; lifetime passes the cap
            self.add(tid, [f"{tid.lower()}.py"])
            self.run_orch(FakeRunner({tid: [done(**{f"{tid.lower()}.py": "x=1\n"})]}))
            self.assertEqual(self.store.get(tid).status, "merged", tid)
        self.assertGreater(self.store.total_cost(), self.cfg.total_budget_usd)
        self.assertAlmostEqual(self.store.get_meta("last_run")["spent_usd"], 0.3, places=3)

    def test_bug2_decision_stays_on_the_card(self):
        self.cfg.max_attempts = 2
        self.cfg.decision_wait_minutes = 0
        self.add("D", ["d.py", "test_d.py"])
        cards = []

        def ask(wt, n, task):
            return AgentResult({"status": "needs_decision", "summary": "half done", "files_changed": [],
                                "tests": "", "blocker": "", "decision": {
                                    "question": "Fee threshold?", "options": ["24h", "12h"],
                                    "recommendation": "24h", "why_it_matters": "money"}}, 0.1)
        failing = done(**{"d.py": "x=1\n", "test_d.py": "import unittest\nclass T(unittest.TestCase):\n"
                          "    def test_x(self):\n        self.assertEqual(1, 2)\n"})

        def capture(wt, n, task):
            cards.append(task_card(task))
            return failing(wt, n, task)
        runner = FakeRunner({"D": [ask, failing, failing, failing, capture]})
        self.run_orch(runner)
        self.store.answer("D", "12h threshold")
        self.run_orch(runner)
        self.assertTrue(cards, "second attempt ran")
        self.assertIn("## Decisions already made", cards[0])
        self.assertIn("12h threshold", cards[0])
        self.assertEqual(cards[0].count("[gate:test] FAILED"), 1, "only the latest gate log is kept")

    def test_bug3_contradictory_approval_is_a_reject(self):
        self.cfg.max_attempts = 1
        self.add("V", ["v.py"])
        verdict = AgentResult({"verdict": "approve",
                               "criteria": [{"criterion": "works", "met": False, "evidence": "no test"}],
                               "scope_creep": [], "issues": [], "feedback": ""}, 0.1)
        self.run_orch(FakeRunner({"V": [done(**{"v.py": "x=1\n"})]}, review={"V": [verdict]}))
        t = self.store.get("V")
        self.assertEqual(t.status, "blocked")
        self.assertIn("acceptance criteria not met", "\n".join(t.feedback))
        self.assertTrue(any(e["kind"] == "review-override" for e in self.store.events("V")))

    def test_bug6_lockfiles_dont_count_toward_diff_size(self):
        self.add("L", ["a.py"])
        lock = "\n".join(f'"pkg{i}": "1.0.{i}",' for i in range(700)) + "\n"
        self.run_orch(FakeRunner({"L": [done(**{"a.py": "x=1\n", "package-lock.json": lock})]}))
        self.assertEqual(self.store.get("L").status, "merged")
        self.assertTrue(any("not counted" in e["detail"] for e in self.store.events("L")))

    def test_bug7_secret_scan_reads_the_whole_diff(self):
        (self.repo / "big.py").write_text("x = '" + "a" * 70_000 + "'\nAWS_KEY = 'AKIAABCDEFGHIJKLMNOP'\n")
        sh("git add -A && git commit -qm big", self.repo)

        async def go():
            g = Git(self.repo, self.cfg.worktree_root, "main", "orchestra/integration")
            diff = await g.diff_text(self.repo, "HEAD~1", limit_chars=None)
            stats = await g.diff_stats(self.repo, "HEAD~1")
            return await run_gates(self.cfg.__class__(repo=self.repo), self.repo, stats, diff, [])
        results = asyncio.run(go())
        self.assertEqual([r.name for r in results if not r.passed], ["secrets"])

    def test_bug8_approving_twice_is_refused_and_new_cards_wait(self):
        from orchestra.cli import main
        plan = {"summary": "s", "open_questions": [], "tasks": [
            {"key": "a", "title": "A", "spec": "", "acceptance_criteria": [], "non_goals": [],
             "files_scope": ["a.py"], "depends_on": [], "complexity": "small"}]}
        p = self.tmp / "plan.json"
        p.write_text(json.dumps(plan))
        main(["-C", str(self.repo), "approve", str(p)])
        main(["-C", str(self.repo), "approve", str(p)])
        self.assertEqual([t.title for t in Store(self.cfg.state_path / "state.db").all()], ["A"])
        main(["-C", str(self.repo), "approve", str(p), "--force"])
        tasks = Store(self.cfg.state_path / "state.db").all()
        self.assertEqual([t.id for t in tasks], ["T01", "T02"])
        self.assertEqual(tasks[1].depends_on, ["T01"], "same file as an open card -> waits for it")

    def test_bug9_glob_scopes_overlap(self):
        self.assertTrue(_scopes_overlap(["src/**/*.py"], ["src/api/views.py"]))
        self.assertTrue(_scopes_overlap(["src/*/models.py"], ["src/booking/models.py"]))
        self.assertFalse(_scopes_overlap(["src/a.py"], ["src/b.py"]))
        self.assertFalse(_scopes_overlap(["api/**"], ["web/**"]))

    def test_bug12_no_marker_leak_when_report_missing(self):
        self.add("M", ["m.py"])
        wt = self.cfg.worktree_root / "M"
        sh("git branch orchestra/integration main", self.repo)
        sh(f"git worktree add -q -B orchestra/task/M {wt} orchestra/integration", self.repo)
        base = sh("git rev-parse main", self.repo).strip()
        self.store.update("M", status="pending", worktree=str(wt), branch="orchestra/task/M",
                          session_id="s1", base_commit=base, resume_note=REVIEW_STAGE)
        runner = FakeRunner({"M": [done(**{"m.py": "x=1\n"})]})
        self.run_orch(runner)
        self.assertEqual(runner.resumes["M"][0], INTERRUPTED_NOTE)
        self.assertEqual(self.store.get("M").status, "merged")


class StaticRegressions(unittest.TestCase):
    def test_bug5_guard_blocks_known_bypasses(self):
        root = Path("/tmp/wt")
        for cmd in ["echo hacked > ../../outside.txt",
                    "git -C . commit -qam sneaky",
                    "git -c user.name=x push origin HEAD",
                    "git branch -f orchestra/integration HEAD",
                    "git stash",
                    "git config core.hooksPath /tmp/evil",
                    "cd /Users/me/other-repo && touch x",
                    "cat .env"]:
            self.assertIsNotNone(check("Bash", {"command": cmd}, root, "worker"), cmd)

    def test_bug11_gate_timeout_kills_the_process_group(self):
        marker = f"orchestra_orphan_{os.getpid()}"

        async def go():
            return await run(f"bash -c 'exec -a {marker} sleep 30' ; echo done", Path("/tmp"),
                             timeout=1, check=False)
        rc, _ = asyncio.run(go())
        self.assertEqual(rc, 124)
        time.sleep(0.3)
        alive = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.strip()
        subprocess.run(["pkill", "-f", marker])
        self.assertFalse(alive, "child survived the timeout kill")


if __name__ == "__main__":
    unittest.main()
