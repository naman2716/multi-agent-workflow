"""Tests for the Graphify integration and the Claude Code harness.

Graph tests run the real `graphify` (local tree-sitter, no API) and are skipped if
it isn't installed. Agents are still the scripted FakeRunner.
"""
import asyncio
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_orchestra import FakeRunner, done, sh  # noqa: E402

from orchestra.config import Config, Gate  # noqa: E402
from orchestra.gitops import Git  # noqa: E402
from orchestra.graph import Graph, GraphData, GraphSettings, graphify_available  # noqa: E402
from orchestra.guards import hook_main  # noqa: E402
from orchestra.harness import MARK_START, install_harness  # noqa: E402
from orchestra.orchestrator import Orchestrator  # noqa: E402
from orchestra.planning import build_tasks  # noqa: E402
from orchestra.store import Store, Task  # noqa: E402

HAS_GRAPHIFY = graphify_available()


def make_app(root: Path) -> Path:
    repo = root / "app"
    repo.mkdir()
    sh("git init -q -b main && git config user.email t@t && git config user.name t", repo)
    (repo / "pricing.py").write_text(
        "def fare(hours, rate):\n    return hours * rate\n\n\ndef gst(amount):\n    return amount * 0.18\n")
    (repo / "booking.py").write_text(
        "from pricing import fare, gst\n\n\ndef quote(hours, rate):\n"
        "    base = fare(hours, rate)\n    return base + gst(base)\n")
    (repo / "staff.py").write_text("def roles():\n    return ['chef', 'cleaner']\n")
    (repo / "test_app.py").write_text(
        "import unittest\nfrom booking import quote\n\nclass T(unittest.TestCase):\n"
        "    def test_quote(self):\n        self.assertAlmostEqual(quote(2, 100), 236.0)\n")
    (repo / ".gitignore").write_text("__pycache__/\n")
    sh("git add -A && git commit -qm init", repo)
    return repo


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = make_app(self.tmp)
        self.cfg = Config(repo=self.repo, concurrency=2, total_budget_usd=100,
                          gates=[Gate("test", f"{sys.executable} -m unittest -q")],
                          graph=GraphSettings(enabled=True))
        self.store = Store(self.cfg.state_path / "state.db")
        self.logs: list[str] = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


@unittest.skipUnless(HAS_GRAPHIFY, "graphify not installed")
class TestGraph(Base):
    def test_run_keeps_graph_in_sync_and_feeds_agents(self):
        self.store.add_task(Task(id="T01", title="min hours", spec="", acceptance=["x"],
                                 files_scope=["pricing.py"]))
        self.store.add_task(Task(id="T02", title="roles", spec="", acceptance=["x"],
                                 files_scope=["staff.py"]))
        new_pricing = done(**{"pricing.py": "def fare(hours, rate):\n    return max(hours, 4) * rate\n\n\n"
                                            "def gst(amount):\n    return amount * 0.18\n\n\n"
                                            "def surge_multiplier(hour):\n    return 1.0\n"})

        def t01(wt, n, task):  # 4-hour minimum changes the existing expectation too
            test = wt / "test_app.py"
            test.write_text(test.read_text().replace("236.0", "472.0"))
            return new_pricing(wt, n, task)

        script = {"T01": [t01],
                  "T02": [done(**{"staff.py": "def roles():\n    return ['chef', 'cleaner', 'server']\n"})]}
        self.store.update("T01", files_scope=["pricing.py", "test_app.py"])
        runner = FakeRunner(script)
        orch = Orchestrator(self.cfg, self.store, runner, log=self.logs.append)
        asyncio.run(orch.run())

        st = {t.id: t for t in self.store.all()}
        self.assertEqual(st["T01"].status, "merged", "\n".join(self.logs))
        self.assertEqual(st["T02"].status, "merged")
        # every agent got a frozen graph snapshot
        for tid in ("T01", "T02"):
            snap = runner.graphs[tid]
            self.assertIsNotNone(snap)
            self.assertTrue(Path(snap).exists())
            self.assertEqual(st[tid].used_graph, 1)
        # reviewer saw who depends on the changed code
        self.assertIn("booking.py", runner.impacts["T01"])
        self.assertIn("fare", runner.impacts["T01"])
        # graph follows the integration head and contains the newly merged symbol
        g = orch.graph
        head = sh("git rev-parse orchestra/integration", self.repo).strip()
        self.assertEqual(sh("git rev-parse HEAD", g.wt).strip(), head)
        labels = set(GraphData.load(g.graph_json).node_label.values())
        self.assertTrue(any("surge_multiplier" in l for l in labels), labels)
        # graph output never leaks into commits
        files = sh("git log --name-only --format= main..orchestra/integration", self.repo)
        self.assertNotIn("graphify-out", files)
        self.assertIn("with_graph", self.store.efficiency())

    def test_coupling_serializes_plan(self):
        g = Graph(self.repo, self.cfg.worktree_root, "main", self.cfg.state_path, self.cfg.graph)
        self.assertTrue(asyncio.run(g.refresh()), g.error)
        gd = g.data()
        self.assertTrue(gd.coupled(["booking.py"], ["pricing.py"]))
        self.assertFalse(gd.coupled(["staff.py"], ["pricing.py"]))
        plan = {"tasks": [
            {"key": "price", "title": "p", "spec": "", "acceptance_criteria": [], "non_goals": [],
             "files_scope": ["pricing.py"], "depends_on": [], "complexity": "small"},
            {"key": "book", "title": "b", "spec": "", "acceptance_criteria": [], "non_goals": [],
             "files_scope": ["booking.py"], "depends_on": [], "complexity": "small"},
            {"key": "staff", "title": "s", "spec": "", "acceptance_criteria": [], "non_goals": [],
             "files_scope": ["staff.py"], "depends_on": [], "complexity": "small"},
        ]}
        no_graph, _ = build_tasks(plan)
        with_graph, warns = build_tasks(plan, graph=gd)
        self.assertEqual({t.id: t.depends_on for t in no_graph}["T02"], [])
        deps = {t.id: t.depends_on for t in with_graph}
        self.assertEqual(deps["T02"], ["T01"], warns)
        self.assertEqual(deps["T03"], [], "unrelated work stays parallel")
        self.assertTrue(any("code coupling" in w for w in warns))
        asyncio.run(g.close())

    def test_broken_graph_never_blocks_the_run(self):
        self.cfg.graph = GraphSettings(enabled=True, python="/nonexistent/python")
        self.store.add_task(Task(id="T01", title="roles", spec="", acceptance=["x"],
                                 files_scope=["staff.py"]))
        runner = FakeRunner({"T01": [done(**{"staff.py": "def roles():\n    return []\n"})]})
        asyncio.run(Orchestrator(self.cfg, self.store, runner, log=self.logs.append).run())
        self.assertEqual(self.store.get("T01").status, "merged")
        self.assertIsNone(runner.graphs["T01"])
        self.assertTrue(any("graph disabled" in l for l in self.logs))


@unittest.skipUnless(HAS_GRAPHIFY, "graphify not installed")
class TestHarness(Base):
    def test_install_is_complete_merging_and_idempotent(self):
        (self.repo / "CLAUDE.md").write_text("# Restocare\n\nUse pnpm.\n")
        settings = self.repo / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"permissions": {"allow": ["Bash(pnpm test)"]}}))
        for _ in range(2):
            out = install_harness(self.cfg, graph=True, git_hooks=True)
        self.assertTrue(any("Done" in l for l in out))

        for rel in ["agents/orchestra-planner.md", "agents/orchestra-worker.md",
                    "agents/orchestra-reviewer.md", "skills/groom/SKILL.md", "skills/dispatch/SKILL.md",
                    "skills/orchestra-status/SKILL.md", "skills/review-integration/SKILL.md",
                    "skills/decide/SKILL.md"]:
            text = (self.repo / ".claude" / rel).read_text()
            self.assertTrue(text.startswith("---\nname: "), rel)
        worker = (self.repo / ".claude/agents/orchestra-worker.md").read_text()
        self.assertIn("isolation: worktree", worker)
        self.assertIn("mcp__graph__query_graph", worker)
        self.assertIn("orchestra/integration", (self.repo / ".claude/skills/dispatch/SKILL.md").read_text())

        s = json.loads(settings.read_text())
        self.assertEqual(s["permissions"], {"allow": ["Bash(pnpm test)"]}, "existing settings kept")
        cmds = [h["command"] for m in s["hooks"]["PreToolUse"] for h in m["hooks"]]
        self.assertEqual(sum("hook interactive" in c for c in cmds), 1, "our hook once, even after 2 installs")
        self.assertTrue(any("graphify" in c for c in cmds), "graphify's graph-first hooks present")

        md = (self.repo / "CLAUDE.md").read_text()
        self.assertTrue(md.startswith("# Restocare"))
        self.assertEqual(md.count(MARK_START), 1)
        self.assertIn("## graphify", md)

        mcp = json.loads((self.repo / ".mcp.json").read_text())
        self.assertIn("graphify.serve", " ".join(mcp["mcpServers"]["graph"]["args"]))
        self.assertTrue((self.repo / "graphify-out" / "graph.json").exists())
        self.assertIn("graphify-out/", (self.repo / ".gitignore").read_text())

    def test_graphify_git_hooks_skip_orchestrator_worktrees(self):
        install_harness(self.cfg, graph=True, git_hooks=True)
        sh("git add -A && git commit -qm harness", self.repo)
        g = Git(self.repo, self.cfg.worktree_root, "main", "orchestra/integration")

        async def go():
            await g.ensure_integration_branch()
            wt, _ = await g.create_worktree("X1")
            (wt / "staff.py").write_text("def roles():\n    return ['x']\n")
            await g.commit_all(wt, "X1")
            return wt
        wt = asyncio.run(go())
        time.sleep(2)  # hooks may rebuild in the background
        # regression guard: graphify skips linked worktrees and we also set GRAPHIFY_SKIP_HOOK
        self.assertFalse((wt / "graphify-out").exists(), "graphify hook ran inside a task worktree")


class TestHookCli(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def call(self, profile, tool, inp, cwd=None):
        payload = {"tool_name": tool, "tool_input": inp, "cwd": str(cwd or self.root)}
        import os
        old = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.root)
        try:
            err = io.StringIO()
            real = sys.stderr
            sys.stderr = err
            try:
                return hook_main(profile, io.StringIO(json.dumps(payload)))
            finally:
                sys.stderr = real
        finally:
            if old is None:
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
            else:
                os.environ["CLAUDE_PROJECT_DIR"] = old

    def test_interactive_profile(self):
        r = self.root
        self.assertEqual(self.call("interactive", "Write", {"file_path": str(r / "a.py"), "content": "x=1"}), 0)
        self.assertEqual(self.call("interactive", "Write", {"file_path": "/etc/hosts", "content": "x"}), 2)
        self.assertEqual(self.call("interactive", "Edit", {"file_path": str(r / "a.py"),
                                                           "new_string": "token = 'sk-ant-abcdefghijklmnopqrstuvwx'"}), 2)
        self.assertEqual(self.call("interactive", "Bash", {"command": "git commit -m wip"}), 0)
        self.assertEqual(self.call("interactive", "Bash", {"command": "git push origin main"}), 2)
        self.assertEqual(self.call("interactive", "Bash", {"command": "git push --force origin feat"}), 2)
        self.assertEqual(self.call("interactive", "Bash", {"command": "git push origin orchestra/integration"}), 0)
        self.assertEqual(self.call("worker", "Bash", {"command": "git commit -m wip"}), 2)
        # a subagent working in its own worktree outside the project dir may write there
        wt = Path(tempfile.mkdtemp())
        self.assertEqual(self.call("interactive", "Write", {"file_path": str(wt / "b.py"), "content": "y"},
                                   cwd=wt), 0)
        self.assertEqual(hook_main("interactive", io.StringIO("not json")), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
