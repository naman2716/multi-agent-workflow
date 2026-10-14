"""Tests for spend-less-per-card: spend counted once, card sizing, cache TTL, early oversize
stop, batched validators, session records, Bash output cap, stable prompt order."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestra.agents import (PLAN_SCHEMA, TRIAGE_SCHEMA, AgentResult, ClaudeRunner, agent_env,  # noqa: E402
                              sizing_rules)
from orchestra.config import AgentSettings, Config  # noqa: E402
from orchestra.harness import build_files  # noqa: E402
from orchestra.planning import build_tasks, render_plan  # noqa: E402
from orchestra.store import Store, Task  # noqa: E402
from tests.test_orchestra import Base, FakeRunner, done  # noqa: E402


class CapturingRunner(ClaudeRunner):
    """The real runner's options, without starting a session."""

    async def _run(self, prompt, options, label):
        self.prompt, self.options = prompt, options
        return AgentResult({}, 0.0)


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


def item(key, lines, **kw):
    return {"key": key, "title": key, "spec": "s", "acceptance_criteria": ["a"], "non_goals": [],
            "files_scope": [f"{key}.py"], "depends_on": [], "complexity": "small",
            "estimated_lines": lines, **kw}


class CardSizing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(repo=Path(self.tmp.name), max_diff_lines=600, split_above=0.7)

    def tearDown(self):
        self.tmp.cleanup()

    def test_plans_must_estimate_size(self):
        card = PLAN_SCHEMA["properties"]["tasks"]["items"]
        self.assertIn("estimated_lines", card["required"])
        self.assertIs(TRIAGE_SCHEMA["properties"]["cards"]["items"], card)  # triage splits estimate too

    def test_planner_critic_and_triage_get_the_projects_limit(self):
        rules = sizing_rules(self.cfg)
        self.assertIn("over 600 changed lines", rules)
        self.assertIn("above 420 lines", rules)
        self.assertIn("Generated or example data", rules)
        r = CapturingRunner()
        asyncio.run(r.plan(self.cfg, "goal"))
        self.assertIn(rules, r.options.system_prompt)
        self.assertNotIn("~300", r.options.system_prompt)
        asyncio.run(r.critique_plan(self.cfg, "goal", {"tasks": []}))
        self.assertIn(rules, r.options.system_prompt)
        asyncio.run(r.triage(self.cfg, Task(id="T1", title="t", spec="s"), "ctx", Path(self.tmp.name)))
        self.assertIn(rules, r.options.system_prompt)
        planner = build_files(self.cfg, graph=False)[".claude/agents/orchestra-planner.md"]
        self.assertIn("over 600 changed lines", planner)
        self.assertIn('"estimated_lines"', planner)

    def test_oversized_cards_are_flagged_and_estimates_kept(self):
        tasks, warnings = build_tasks({"tasks": [item("small", 300), item("big", 900)]}, split_at=420)
        self.assertEqual([t.estimated_lines for t in tasks], [300, 900])
        flagged = [w for w in warnings if "split threshold" in w]
        self.assertEqual(len(flagged), 1)
        self.assertIn("T02: estimated 900 changed lines", flagged[0])
        self.assertIn("~900 lines", render_plan(tasks, warnings, {"summary": ""}))
        _, quiet = build_tasks({"tasks": [item("big", 900)]})  # no threshold given: no flag
        self.assertFalse([w for w in quiet if "split threshold" in w])
        bad, _ = build_tasks({"tasks": [item("x", "lots")]}, split_at=420)
        self.assertEqual(bad[0].estimated_lines, 0)

    def test_estimate_is_stored_on_the_card(self):
        store = Store(Path(self.tmp.name) / "s.db")
        tasks, _ = build_tasks({"tasks": [item("a", 250)]})
        store.add_task(tasks[0])
        self.assertEqual(store.get("T01").estimated_lines, 250)


class CacheTTL(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_five_minutes_by_default_and_configurable(self):
        cfg = Config(repo=self.root)
        self.assertEqual(agent_env(cfg), {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "FORCE_PROMPT_CACHING_5M": "1"})
        cfg.agents.cache_ttl = "1h"
        self.assertEqual(agent_env(cfg)["ENABLE_PROMPT_CACHING_1H"], "1")
        self.assertNotIn("FORCE_PROMPT_CACHING_5M", agent_env(cfg))
        cfg.agents.cache_ttl = "auto"
        self.assertEqual(agent_env(cfg), {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
        with self.assertRaises(ValueError):
            AgentSettings(cache_ttl="10m")

    def test_read_from_orchestra_toml(self):
        (self.root / "orchestra.toml").write_text('[agents]\ncache_ttl = "1h"\n')
        self.assertEqual(Config.load(self.root).agents.cache_ttl, "1h")
        (self.root / "orchestra.toml").write_text("")
        self.assertEqual(Config.load(self.root).agents.cache_ttl, "5m")

    def test_every_role_gets_it(self):
        cfg, r = Config(repo=self.root), CapturingRunner()
        task = Task(id="T1", title="t", spec="s")
        calls = {"planner": lambda: r.plan(cfg, "g"), "critic": lambda: r.critique_plan(cfg, "g", {"tasks": []}),
                 "triage": lambda: r.triage(cfg, task, "ctx", self.root),
                 "worker": lambda: r.work(cfg, task, self.root, env={"SLOT": "1"}),
                 "test writer": lambda: r.write_tests(cfg, task, self.root),
                 "reviewer": lambda: r.review(cfg, task, self.root, "diff", {}, "log"),
                 "validator": lambda: r.validate(cfg, task, self.root, "diff", {"kind": "issue", "text": "x"})}
        for role, call in calls.items():
            asyncio.run(call())
            self.assertEqual(r.options.env.get("FORCE_PROMPT_CACHING_5M"), "1", role)
        self.assertEqual(r.options.env.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"), "1")


if __name__ == "__main__":
    unittest.main()
