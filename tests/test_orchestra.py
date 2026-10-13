"""End-to-end tests of the orchestration loop with scripted fake agents.

No API calls: a FakeRunner edits files in the worktree exactly as a worker
would, so git isolation, gates, retries, review, conflicts and merges all run for real.
"""
import asyncio
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestra.agents import AgentResult  # noqa: E402
from orchestra.config import Config, Gate  # noqa: E402
from orchestra.gates import find_secrets, in_scope  # noqa: E402
from orchestra.guards import make_worker_guard  # noqa: E402
from orchestra.orchestrator import Orchestrator  # noqa: E402
from orchestra.planning import PlanError, build_tasks  # noqa: E402
from orchestra.store import Store, Task  # noqa: E402


def sh(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, shell=True, check=True, capture_output=True, text=True).stdout


def make_repo(root: Path) -> Path:
    repo = root / "app"
    repo.mkdir()
    sh("git init -q -b main && git config user.email t@t && git config user.name t", repo)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "shared.txt").write_text("line1\nline2\nline3\n")
    (repo / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n\n"
        "class T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n")
    sh("git add -A && git commit -qm init", repo)
    return repo


class FakeRunner:
    """script[task_id] = list of callables(wt, call_no) -> AgentResult, one per worker call.
    review[task_id] = list of verdicts per review call."""

    def __init__(self, script, review=None, delay=0.05, delays=None, validate=None, tests=None):
        self.script, self.review_script, self.delay = script, review or {}, delay
        self.delays = delays or {}
        self.validate_script, self.tests_script = validate or {}, tests or {}
        self.graphs, self.impacts, self.resumes = {}, {}, {}
        self.calls, self.review_calls = {}, {}
        self.envs, self.review_notes, self.review_models, self.validations = {}, {}, {}, {}
        self.tests_calls, self.cards = {}, {}
        self.active, self.max_active = 0, 0

    async def plan(self, cfg, goal, cwd=None, graph=None):
        raise NotImplementedError

    async def critique_plan(self, cfg, goal, plan, cwd=None, graph=None):
        return AgentResult({"verdict": "ok", "issues": [], "plan": plan}, 0.0)

    async def write_tests(self, cfg, task, wt, graph=None, resume=None, env=None):
        n = self.tests_calls.get(task.id, 0)
        self.tests_calls[task.id] = n + 1
        steps = self.tests_script[task.id]
        return steps[min(n, len(steps) - 1)](wt, n, task)

    async def validate(self, cfg, task, wt, diff, finding, env=None):
        self.validations.setdefault(task.id, []).append(finding["text"])
        confirmed = self.validate_script.get(task.id, True)
        return AgentResult({"confirmed": confirmed, "evidence": "checked by fake validator"}, 0.05)

    async def work(self, cfg, task, wt, graph=None, resume=None, env=None):
        self.graphs[task.id] = graph
        self.envs[task.id] = env
        self.cards.setdefault(task.id, []).append(task)
        self.resumes.setdefault(task.id, []).append(resume)
        n = self.calls.get(task.id, 0)
        self.calls[task.id] = n + 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(self.delays.get(task.id, self.delay))
        self.active -= 1
        steps = self.script[task.id]
        return steps[min(n, len(steps) - 1)](wt, n, task)

    async def review(self, cfg, task, wt, diff, report, gate_log, impact="", graph=None, note="",
                     model=None, env=None):
        self.impacts[task.id] = impact
        self.review_notes.setdefault(task.id, []).append(note)
        self.review_models.setdefault(task.id, []).append(model)
        n = self.review_calls.get(task.id, 0)
        self.review_calls[task.id] = n + 1
        verdicts = self.review_script.get(task.id, ["approve"])
        v = verdicts[min(n, len(verdicts) - 1)]
        if isinstance(v, AgentResult):
            return v
        return AgentResult({"verdict": v, "criteria": [], "scope_creep": [] if v == "approve"
                            else ["added PDF export"], "issues": [], "feedback": "only CSV please"}, 0.1)


def done(**files):
    def step(wt, n, task):
        for name, content in files.items():
            p = wt / name.replace("__", "/")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return AgentResult({"status": "done", "summary": f"wrote {list(files)}", "files_changed": list(files),
                            "tests": "ok", "blocker": ""}, 0.2)
    return step


def blocked(wt, n, task):
    return AgentResult({"status": "blocked", "summary": "", "files_changed": [], "tests": "",
                        "blocker": "spec does not say which payment gateway"}, 0.05)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = make_repo(self.tmp)
        self.cfg = Config(repo=self.repo, concurrency=3, max_attempts=3, total_budget_usd=100,
                          gates=[Gate("test", f"{sys.executable} -m unittest -q")])
        self.store = Store(self.cfg.state_path / "state.db")
        self.logs = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, tid, scope, deps=(), **kw):
        self.store.add_task(Task(id=tid, title=f"task {tid}", spec="do it", acceptance=["works"],
                                 files_scope=list(scope), depends_on=list(deps), **kw))

    def run_orch(self, runner):
        orch = Orchestrator(self.cfg, self.store, runner, log=self.logs.append)
        return asyncio.run(orch.run())

    def integration_file(self, name):
        return sh(f"git show orchestra/integration:{name}", self.repo)


class TestLoop(Base):
    def test_parallel_deps_retries_review_and_merge(self):
        self.add("T01", ["mul.py", "test_mul.py"])
        self.add("T02", ["sub.py", "test_sub.py"])
        self.add("T03", ["calc.py", "test_calc2.py"], deps=["T01"])
        self.add("T04", ["div.py", "test_div.py"])  # gate fails first, then fixed
        self.add("T05", ["export.py"])               # reviewer rejects once
        self.add("T06", ["pay.py"])                  # worker blocked
        self.add("T07", ["late.py"], deps=["T06"])   # can never run

        script = {
            "T01": [done(**{"mul.py": "def mul(a,b):\n    return a*b\n",
                            "test_mul.py": "import unittest\nfrom mul import mul\nclass T(unittest.TestCase):\n"
                                           "    def test(self): self.assertEqual(mul(2,3),6)\n"})],
            "T02": [done(**{"sub.py": "def sub(a,b):\n    return a-b\n"})],
            "T03": [done(**{"calc.py": "from mul import mul\n\ndef add(a, b):\n    return a + b\n\n"
                                       "def square(a):\n    return mul(a, a)\n"})],
            "T04": [done(**{"div.py": "def div(a,b):\n    return a*b\n",
                            "test_div.py": "import unittest\nfrom div import div\nclass T(unittest.TestCase):\n"
                                           "    def test(self): self.assertEqual(div(6,3),2)\n"}),
                    done(**{"div.py": "def div(a,b):\n    return a/b\n"})],
            "T05": [done(**{"export.py": "CSV=1\nPDF=1\n"}), done(**{"export.py": "CSV=1\n"})],
            "T06": [blocked],
            "T07": [done(**{"late.py": "x=1\n"})],
        }
        runner = FakeRunner(script, review={"T05": ["reject", "approve"]})
        counts = self.run_orch(runner)

        st = {t.id: t for t in self.store.all()}
        self.assertEqual({k for k, v in st.items() if v.status == "merged"}, {"T01", "T02", "T03", "T04", "T05"})
        self.assertEqual(st["T06"].status, "blocked")
        self.assertEqual(st["T07"].status, "pending")
        self.assertEqual(counts["merged"], 5)
        self.assertGreaterEqual(runner.max_active, 2, "workers should run in parallel")
        self.assertEqual(runner.calls["T04"], 2, "gate failure fixed within the same attempt")
        self.assertEqual(st["T04"].attempts, 1)
        self.assertEqual(st["T05"].attempts, 2, "review rejection starts a fresh attempt")
        self.assertIn("added PDF export", "\n".join(st["T05"].feedback))
        self.assertIn("square", self.integration_file("calc.py"))
        self.assertEqual(self.integration_file("export.py"), "CSV=1\n")
        self.assertEqual(self.integration_file("div.py").strip(), "def div(a,b):\n    return a/b")
        # base branch untouched; worktrees cleaned
        self.assertEqual(sh("git rev-list --count main", self.repo).strip(), "1")
        self.assertEqual(sh("git worktree list", self.repo).count("\n"), 1)
        sh(f"{sys.executable} -m unittest -q", self.repo)  # main still green

    def test_merge_conflict_is_retried_on_new_base(self):
        # bypass planner overlap protection to force a real textual conflict
        self.add("A", [])
        self.add("B", [])
        script = {"A": [done(**{"shared.txt": "line1\nA-CHANGE\nline3\n"})],
                  "B": [done(**{"shared.txt": "line1\nB-CHANGE\nline3\n"}),
                        lambda wt, n, task: done(**{"shared.txt": (wt / "shared.txt").read_text()
                                                    .replace("line3", "B-CHANGE")})(wt, n, task)]}
        self.run_orch(FakeRunner(script, delays={"B": 0.5}))
        st = {t.id: t for t in self.store.all()}
        self.assertEqual(st["A"].status, "merged")
        self.assertEqual(st["B"].status, "merged")
        self.assertEqual(st["B"].attempts, 2)
        self.assertIn("conflicted", "\n".join(st["B"].feedback))
        self.assertEqual(self.integration_file("shared.txt"), "line1\nA-CHANGE\nB-CHANGE\n")

    def test_scope_and_size_gates_and_budget(self):
        self.cfg.max_attempts = 1
        self.add("S", ["only.py"])
        self.add("Z", ["big.py"])
        self.cfg.max_diff_lines = 50
        script = {"S": [done(**{"only.py": "x=1\n", "other.py": "y=2\n"})],
                  "Z": [done(**{"big.py": "x=1\n" * 200})]}
        self.run_orch(FakeRunner(script))
        st = {t.id: t for t in self.store.all()}
        self.assertEqual(st["S"].status, "blocked")
        self.assertIn("outside task scope", "\n".join(st["S"].feedback))
        self.assertEqual(st["Z"].status, "blocked")
        self.assertIn("exceeds limit", "\n".join(st["Z"].feedback))

    def test_budget_stops_new_dispatch(self):
        self.cfg.concurrency = 1
        self.cfg.total_budget_usd = 0.25
        for i in range(3):
            self.add(f"B{i}", [f"f{i}.py"])
        script = {f"B{i}": [done(**{f"f{i}.py": "x=1\n"})] for i in range(3)}
        self.run_orch(FakeRunner(script))
        self.assertEqual(self.store.counts()["merged"], 1)
        self.assertEqual(self.store.counts()["pending"], 2)

    def test_crash_recovery(self):
        self.add("R", ["r.py"])
        self.store.claim_ready(1)
        self.assertEqual(self.store.get("R").status, "running")
        self.run_orch(FakeRunner({"R": [done(**{"r.py": "x=1\n"})]}))
        self.assertEqual(self.store.get("R").status, "merged")


class TestModels(Base):
    def test_model_selection_and_persistence(self):
        from orchestra.agents import pick_worker_model
        cfg = self.cfg
        cfg.small_worker_model, cfg.large_worker_model = "sonnet", "opus"
        t = Task(id="M", title="t", spec="", complexity="small", attempts=1)
        self.assertEqual(pick_worker_model(cfg, t), "sonnet")
        t.attempts = 2
        self.assertEqual(pick_worker_model(cfg, t), "opus", "escalates after a failed attempt")
        cfg.escalate_on_retry = False
        self.assertEqual(pick_worker_model(cfg, t), "sonnet")
        t.complexity = "large"
        self.assertEqual(pick_worker_model(cfg, t), "opus")
        t.model = "haiku"
        self.assertEqual(pick_worker_model(cfg, t), "haiku", "card override wins")
        tasks, _ = build_tasks({"tasks": [{"key": "a", "title": "a", "spec": "", "acceptance_criteria": [],
                                           "non_goals": [], "files_scope": ["a.py"], "depends_on": [],
                                           "complexity": "small", "model": "claude-opus-5-5"}]})
        self.store.add_task(tasks[0])
        self.assertEqual(self.store.get("T01").model, "claude-opus-5-5")
        self.store.update("T01", model=None)
        self.assertIsNone(self.store.get("T01").model)

    def test_old_database_migrates(self):
        import sqlite3
        path = self.tmp / "old.db"
        con = sqlite3.connect(path)
        con.executescript("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, spec TEXT NOT NULL,"
                          " acceptance TEXT NOT NULL DEFAULT '[]', non_goals TEXT NOT NULL DEFAULT '[]',"
                          " files_scope TEXT NOT NULL DEFAULT '[]', depends_on TEXT NOT NULL DEFAULT '[]',"
                          " complexity TEXT NOT NULL DEFAULT 'small', status TEXT NOT NULL DEFAULT 'pending',"
                          " attempts INTEGER NOT NULL DEFAULT 0, feedback TEXT NOT NULL DEFAULT '[]', branch TEXT,"
                          " worktree TEXT, cost_usd REAL NOT NULL DEFAULT 0, summary TEXT, claimed_at REAL,"
                          " updated_at REAL NOT NULL, created_at REAL NOT NULL, seq INTEGER NOT NULL DEFAULT 0);"
                          "INSERT INTO tasks (id,title,spec,updated_at,created_at) VALUES ('X','x','',0,0);")
        con.commit(); con.close()
        st = Store(path)
        self.assertIsNone(st.get("X").model)
        self.assertEqual(st.get("X").tokens, 0)


class TestGuardsAndPlanning(unittest.TestCase):
    def test_guard(self):
        wt = Path(tempfile.mkdtemp())
        g = make_worker_guard(wt)
        call = lambda tool, inp: asyncio.run(g({"tool_name": tool, "tool_input": inp}, None, None))
        self.assertEqual(call("Write", {"file_path": str(wt / "a.py"), "content": "x=1"}), {})
        self.assertIn("deny", str(call("Write", {"file_path": "/etc/passwd", "content": "x"})))
        self.assertIn("deny", str(call("Edit", {"file_path": str(wt / "../other/a.py"), "new_string": "x"})))
        self.assertIn("deny", str(call("Bash", {"command": "git push origin main"})))
        self.assertIn("deny", str(call("Bash", {"command": "cd .. && ls"})))
        self.assertEqual(call("Bash", {"command": "npm test && git status"}), {})
        self.assertIn("deny", str(call("Write", {"file_path": str(wt / "k.py"),
                                                 "content": "KEY='AKIAABCDEFGHIJKLMNOP'"})))

    def test_secrets_and_scope(self):
        self.assertTrue(find_secrets('password = "hunter2hunter2"'))
        self.assertFalse(find_secrets("password = os.environ['PW']"))
        self.assertTrue(in_scope("src/api/shifts.ts", ["src/api/**"]))
        self.assertTrue(in_scope("package-lock.json", ["src/**"]))
        self.assertFalse(in_scope("src/web/x.ts", ["src/api/**"]))

    def test_plan_build(self):
        plan = {"tasks": [
            {"key": "schema", "title": "s", "spec": "", "acceptance_criteria": [], "non_goals": [],
             "files_scope": ["db/schema.sql"], "depends_on": [], "complexity": "small"},
            {"key": "api", "title": "a", "spec": "", "acceptance_criteria": [], "non_goals": [],
             "files_scope": ["src/api/**"], "depends_on": ["schema"], "complexity": "large"},
            {"key": "api2", "title": "b", "spec": "", "acceptance_criteria": [], "non_goals": [],
             "files_scope": ["src/api/shifts.ts"], "depends_on": ["schema", "ghost"], "complexity": "small"},
        ]}
        tasks, warns = build_tasks(plan)
        by = {t.id: t for t in tasks}
        self.assertEqual(by["T02"].depends_on, ["T01"])
        self.assertIn("T02", by["T03"].depends_on, "overlapping scopes get serialized")
        self.assertTrue(any("ghost" in w for w in warns))
        plan["tasks"][0]["depends_on"] = ["api"]
        with self.assertRaises(PlanError):
            build_tasks(plan)


if __name__ == "__main__":
    unittest.main(verbosity=2)
