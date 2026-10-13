"""Repro tests for the Oct 2026 review of Orchestra.

Each test PASSES while the bug is present. When you fix a bug, invert its assertion and
move it into tests/ as a regression test.

Run from the repo root:  python -m unittest discover -s review -t . -v
"""
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
from orchestra.gitops import DiffStats, run  # noqa: E402
from orchestra.guards import check  # noqa: E402
from orchestra.orchestrator import REVIEW_STAGE  # noqa: E402
from orchestra.planning import _scopes_overlap  # noqa: E402
from tests.test_orchestra import Base, FakeRunner, done, sh  # noqa: E402


class Findings(Base):
    def test_budget_is_lifetime_not_per_run(self):
        self.cfg.total_budget_usd = 0.5
        self.add("A", ["a.py"])
        self.run_orch(FakeRunner({"A": [done(**{"a.py": "x=1\n"})]}))  # spends 0.3
        self.add("B", ["b.py"])
        self.run_orch(FakeRunner({"B": [done(**{"b.py": "x=1\n"})]}))
        self.add("C", ["c.py"])
        self.run_orch(FakeRunner({"C": [done(**{"c.py": "x=1\n"})]}))
        # After ~$0.6 lifetime spend, every later run dispatches nothing.
        self.assertEqual(self.store.get("C").status, "pending")
        self.assertEqual(self.store.get("C").attempts, 0)

    def test_lockfile_counts_toward_diff_size(self):
        self.cfg.max_attempts = 1
        self.cfg.max_diff_lines = 600
        self.add("L", ["a.py"])
        lock = "\n".join(f'"pkg{i}": "1.0.{i}",' for i in range(700)) + "\n"
        self.run_orch(FakeRunner({"L": [done(**{"a.py": "x=1\n", "package-lock.json": lock})]}))
        t = self.store.get("L")
        self.assertEqual(t.status, "blocked")
        self.assertIn("exceeds limit", "\n".join(t.feedback))

    def test_reviewer_approve_with_unmet_criteria_still_merges(self):
        self.add("V", ["v.py"])
        verdict = AgentResult({"verdict": "approve",
                               "criteria": [{"criterion": "works", "met": False, "evidence": "no test"}],
                               "scope_creep": ["added PDF export"],
                               "issues": [{"severity": "blocker", "file": "v.py", "issue": "SQL injection"}],
                               "feedback": ""}, 0.1)
        self.run_orch(FakeRunner({"V": [done(**{"v.py": "x=1\n"})]}, review={"V": [verdict]}))
        self.assertEqual(self.store.get("V").status, "merged")

    def test_secret_after_60k_chars_of_diff_is_missed(self):
        wt = self.repo
        long_line = "x = '" + "a" * 70_000 + "'\n"
        secret = "AWS_KEY = 'AKIAABCDEFGHIJKLMNOP'\n"
        (wt / "big.py").write_text(long_line + secret)
        sh("git add -A && git commit -qm big", wt)

        async def go():
            from orchestra.gitops import Git
            g = Git(self.repo, self.cfg.worktree_root, "main", "orchestra/integration")
            diff = await g.diff_text(wt, "HEAD~1")
            stats = await g.diff_stats(wt, "HEAD~1")
            return await run_gates(self.cfg.__class__(repo=self.repo), wt, stats, diff, [])
        results = asyncio.run(go())
        self.assertTrue(all(r.passed for r in results), [(r.name, r.detail) for r in results])

    def test_review_stage_marker_sent_to_worker_when_report_missing(self):
        self.add("M", ["m.py"])
        wt = self.cfg.worktree_root / "M"
        sh(f"git worktree add -q -B orchestra/task/M {wt} main", self.repo)
        base = sh("git rev-parse main", self.repo).strip()
        sh("git branch orchestra/integration main", self.repo)
        self.store.update("M", worktree=str(wt), branch="orchestra/task/M", session_id="s1",
                          base_commit=base, resume_note=REVIEW_STAGE)
        runner = FakeRunner({"M": [done(**{"m.py": "x=1\n"})]})
        self.run_orch(runner)
        self.assertEqual(runner.resumes["M"][0], REVIEW_STAGE)

    def test_cli_approve_twice_duplicates_cards(self):
        from orchestra.cli import main
        plan = {"summary": "s", "open_questions": [], "tasks": [
            {"key": "a", "title": "A", "spec": "", "acceptance_criteria": [], "non_goals": [],
             "files_scope": ["a.py"], "depends_on": [], "complexity": "small"}]}
        p = self.tmp / "plan.json"
        p.write_text(json.dumps(plan))
        self.store.conn.close()
        main(["-C", str(self.repo), "approve", str(p)])
        main(["-C", str(self.repo), "approve", str(p)])
        from orchestra.store import Store
        st = Store(self.cfg.state_path / "state.db")
        self.assertEqual([t.title for t in st.all()], ["A", "A"])

    def test_human_decision_falls_off_the_card(self):
        self.cfg.max_attempts = 2
        self.cfg.decision_wait_minutes = 0
        self.add("D", ["d.py", "test_d.py"])
        cards = []
        def ask(wt, n, task):
            return AgentResult({"status": "needs_decision", "summary": "half done", "files_changed": [],
                                "tests": "", "blocker": "", "decision": {"question": "Fee threshold?",
                                "options": ["24h", "12h"], "recommendation": "24h", "why_it_matters": "money"}}, 0.1)
        failing = done(**{"d.py": "x=1\n", "test_d.py": "import unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.assertEqual(1, 2)\n"})
        def capture(wt, n, task):
            cards.append(task_card(task))
            return failing(wt, n, task)
        runner = FakeRunner({"D": [ask, failing, failing, failing, capture]})
        self.run_orch(runner)                       # asks, goes to waiting
        self.store.answer("D", "12h threshold")
        self.run_orch(runner)                       # resumes, fails gates 3x, new attempt
        self.assertTrue(cards, "second attempt ran")
        self.assertNotIn("12h threshold", cards[0])


class Static(unittest.TestCase):
    def test_bash_guard_bypasses(self):
        root = Path("/tmp/wt")
        allowed = [
            "echo hacked > ../../outside.txt",
            "git -C . commit -qam sneaky",
            "git -c user.name=x push origin HEAD",
            "git branch -f orchestra/integration HEAD",
            "git stash",
            "git config core.hooksPath /tmp/evil",
            "python3 -c \"open('/Users/me/.zshrc','a').write('x')\"",
            "cd /Users/me/other-repo && touch x",
        ]
        for cmd in allowed:
            self.assertIsNone(check("Bash", {"command": cmd}, root, "worker"), cmd)

    def test_glob_scope_overlap_false_negative(self):
        self.assertFalse(_scopes_overlap(["src/**/*.py"], ["src/api/views.py"]))
        self.assertFalse(_scopes_overlap(["src/*/models.py"], ["src/booking/models.py"]))

    def test_gate_timeout_orphans_children(self):
        marker = f"orchestra_orphan_{os.getpid()}"
        async def go():
            return await run(f"bash -c 'exec -a {marker} sleep 30' ; echo done", Path("/tmp"), timeout=1, check=False)
        rc, _ = asyncio.run(go())
        self.assertEqual(rc, 124)
        time.sleep(0.3)
        alive = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.strip()
        subprocess.run(["pkill", "-f", marker])
        self.assertTrue(alive, "child survived the timeout kill")


if __name__ == "__main__":
    unittest.main()
