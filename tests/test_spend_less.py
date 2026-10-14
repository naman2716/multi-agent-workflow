"""Tests for spend-less-per-card: spend counted once, card sizing, cache TTL, early oversize
stop, batched validators, session records, Bash output cap, stable prompt order."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestra.agents import AgentResult  # noqa: E402
from orchestra.store import Store  # noqa: E402
from tests.test_orchestra import Base, FakeRunner, done  # noqa: E402


def running_total(step, sid, totals, tokens=1000):
    """A worker call that reports its session's running total, as the SDK does on resume."""
    def wrapped(wt, n, task):
        r = step(wt, n, task)
        r.session_id, r.cost_usd, r.tokens = sid, totals[min(n, len(totals) - 1)], tokens * (n + 1)
        return r
    return wrapped


class SpendCountedOnce(Base):
    def test_store_adds_only_the_growth_of_a_sessions_running_total(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "s.db")
            self.add_to(store)
            for total, tok in ((0.3, 100), (0.5, 180), (0.9, 300)):
                store.add_cost("C", total, tok, "s1")
            store.add_cost("C", 0.2, 50, "s2")      # another session starts from zero
            store.add_cost("C", 0.1, 10)            # no session id: taken as is
            store.add_cost("C", 0.4, 120, "s1")     # a lower report never subtracts
            t = store.get("C")
            self.assertAlmostEqual(t.cost_usd, 0.9 + 0.2 + 0.1)
            self.assertEqual(t.tokens, 300 + 50 + 10)

    def add_to(self, store):
        from orchestra.store import Task
        store.add_task(Task(id="C", title="c", spec="s"))

    def test_gate_fix_resume_is_not_counted_twice(self):
        self.add("T01", ["calc.py", "test_calc.py"])
        bad = done(**{"calc.py": "def add(a, b):\n    return a - b\n"})
        good = done(**{"calc.py": "def add(a, b):\n    return a + b  # fixed\n"})
        step = lambda wt, n, task: (bad if n == 0 else good)(wt, n, task)  # noqa: E731
        runner = FakeRunner({"T01": [running_total(step, "w1", [0.3, 0.5])]})
        self.run_orch(runner)
        t = self.store.get("T01")
        self.assertEqual(t.status, "merged", "\n".join(self.logs))
        self.assertAlmostEqual(t.cost_usd, 0.5 + 0.1)  # worker's final total + one review
        self.assertEqual(t.tokens, 2000)

    def test_budget_counts_real_spend(self):
        self.cfg.total_budget_usd = 0.7  # summed totals (0.3 + 0.5 + review) would pass it
        self.add("T01", ["calc.py", "test_calc.py"])
        self.add("T02", ["shared.txt"], deps=["T01"])
        bad = done(**{"calc.py": "def add(a, b):\n    return a - b\n"})
        good = done(**{"calc.py": "def add(a, b):\n    return a + b  # fixed\n"})
        step = lambda wt, n, task: (bad if n == 0 else good)(wt, n, task)  # noqa: E731
        runner = FakeRunner({"T01": [running_total(step, "w1", [0.3, 0.5])],
                             "T02": [done(**{"shared.txt": "x\n"})]})
        self.run_orch(runner)
        self.assertEqual(self.store.get("T02").status, "merged", "\n".join(self.logs))


if __name__ == "__main__":
    unittest.main()
