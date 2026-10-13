"""Tests for keep-good-work: revise in place, conflict resolution, validators, reviewer model
rule, test-first cards, per-slot environment, dependency cloning, quick checks, planning."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestra.agents import AgentResult, drop_refuted, blocking_findings, pick_reviewer_model  # noqa: E402
from orchestra.config import Config, Gate  # noqa: E402
from orchestra.gates import run_gates  # noqa: E402
from orchestra.gitops import DiffStats  # noqa: E402
from orchestra.orchestrator import CONFLICT_NOTE, REVISE_NOTE  # noqa: E402
from orchestra.planning import build_tasks  # noqa: E402
from orchestra.store import Store, Task  # noqa: E402
from tests.test_orchestra import Base, FakeRunner, done, sh  # noqa: E402


def sess(step, sid="s1"):
    def wrapped(wt, n, task):
        r = step(wt, n, task)
        r.session_id = sid
        return r
    return wrapped


def verdict(v="reject", fixable=True, **kw):
    data = {"verdict": v, "criteria": [], "scope_creep": [] if v == "approve" else ["extra endpoint"],
            "issues": [], "feedback": "remove the extra endpoint", "fixable": fixable}
    data.update(kw)
    return AgentResult(data, 0.1)


def tests_done(**files):
    def step(wt, n, task):
        for name, content in files.items():
            p = wt / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return AgentResult({"status": "done", "test_files": list(files), "covered": [],
                            "assumptions": ["fee is 10%"], "blocker": ""}, 0.2, session_id="tw1")
    return step


FEE_TEST = ("import unittest\nfrom fee import fee\n\nclass T(unittest.TestCase):\n"
            "    def test_fee(self):\n        self.assertEqual(fee(100), 10)\n")


class Revise(Base):
    def test_fixable_rejection_is_revised_in_the_same_session(self):
        self.add("R", ["r.py"])
        runner = FakeRunner({"R": [sess(done(**{"r.py": "x=1\ny=2\n"})), sess(done(**{"r.py": "x=1\n"}))]},
                            review={"R": [verdict("reject"), "approve"]})
        self.run_orch(runner)
        t = self.store.get("R")
        self.assertEqual((t.status, t.attempts), ("merged", 1))
        self.assertIsNone(runner.resumes["R"][0])
        self.assertTrue(runner.resumes["R"][1].startswith(REVISE_NOTE))
        self.assertIn("This is a revision", runner.review_notes["R"][1])
        self.assertEqual(sh("git rev-list --count main..orchestra/integration", self.repo).strip(), "1")

    def test_unfixable_rejection_starts_over(self):
        self.add("U", ["u.py"])
        runner = FakeRunner({"U": [sess(done(**{"u.py": "x=1\n"})), sess(done(**{"u.py": "x=2\n"}))]},
                            review={"U": [verdict("reject", fixable=False), "approve"]})
        self.run_orch(runner)
        t = self.store.get("U")
        self.assertEqual((t.status, t.attempts), ("merged", 2))
        self.assertEqual(runner.resumes["U"], [None, None], "second attempt starts from the card")

    def test_validator_can_overturn_a_rejection(self):
        self.add("V", ["v.py"])
        runner = FakeRunner({"V": [done(**{"v.py": "x=1\n"})]}, review={"V": [verdict("reject")]},
                            validate={"V": False})
        self.run_orch(runner)
        self.assertEqual(self.store.get("V").status, "merged")
        self.assertEqual(runner.validations["V"], ["extra endpoint"])
        self.assertTrue(any(e["kind"] == "validate" for e in self.store.events("V")))


class Conflicts(Base):
    def test_worker_resolves_a_conflict_and_card_lands_as_one_commit(self):
        self.add("A", ["shared.txt"])
        self.add("B", ["shared.txt"])

        def resolve(wt, n, task):
            text = (wt / "shared.txt").read_text()
            assert "<<<<<<<" in text, text
            (wt / "shared.txt").write_text("line1\nA and B\nline3\n")
            return AgentResult({"status": "done", "summary": "resolved", "files_changed": ["shared.txt"],
                                "tests": "", "blocker": ""}, 0.2, session_id="sb")
        runner = FakeRunner({"A": [sess(done(**{"shared.txt": "line1\nA\nline3\n"}), "sa")],
                             "B": [sess(done(**{"shared.txt": "line1\nB\nline3\n"}), "sb"), resolve]},
                            delays={"B": 0.6})
        self.run_orch(runner)
        a, b = self.store.get("A"), self.store.get("B")
        self.assertEqual((a.status, b.status, b.attempts), ("merged", "merged", 1))
        self.assertTrue(runner.resumes["B"][1].startswith(CONFLICT_NOTE.split("{")[0]))
        self.assertIn("resolve merge conflicts", runner.review_notes["B"][1])
        self.assertEqual(self.integration_file("shared.txt"), "line1\nA and B\nline3\n")
        self.assertEqual(sh("git rev-list --count main..orchestra/integration", self.repo).strip(), "2")
        self.assertEqual(sh("git rev-list --merges --count main..orchestra/integration", self.repo).strip(), "0")

    def test_integration_moving_right_after_worktree_creation_loses_nothing(self):
        """Regression: the base was read from the integration ref after creating the worktree;
        if another card merged in between, squashing onto that newer base reverted its work."""
        from orchestra.orchestrator import Orchestrator
        self.add("S", ["s.py"])
        orch = Orchestrator(self.cfg, self.store, FakeRunner({"S": [done(**{"s.py": "x=1\n"})]}),
                            log=self.logs.append)
        real = orch.git.create_worktree

        async def create_then_someone_merges(task_id):
            wt, branch = await real(task_id)
            other = self.tmp / "other"
            sh(f"git worktree add -q {other} orchestra/integration && cd {other} && echo y > other.py && "
               "git add -A && git commit -qm other && git -C .. status >/dev/null 2>&1; true", self.repo)
            sh(f"git update-ref refs/heads/orchestra/integration $(git -C {other} rev-parse HEAD)", self.repo)
            return wt, branch
        orch.git.create_worktree = create_then_someone_merges
        asyncio.run(orch.run())
        self.assertEqual(self.store.get("S").status, "merged")
        self.assertEqual(self.integration_file("other.py"), "y\n", "the other card's work survives")
        self.assertEqual(self.integration_file("s.py"), "x=1\n")

    def test_leftover_markers_fail_the_gate(self):
        cfg = Config(repo=self.repo)
        stats = DiffStats(["a.py"], 3, 0, {"a.py": 3})
        diff = "+++ b/a.py\n+<<<<<<< HEAD\n+x = 1\n+>>>>>>> orchestra/integration\n"
        results = asyncio.run(run_gates(cfg, self.repo, stats, diff, []))
        self.assertEqual([r.name for r in results if not r.passed], ["conflict_markers"])


class TestFirst(Base):
    def setUp(self):
        super().setUp()
        self.add("F", ["fee.py", "test_fee.py"], test_first=True)

    def test_tests_written_first_are_locked_and_restored(self):
        def cheat(wt, n, task):
            (wt / "fee.py").write_text("def fee(x):\n    return x // 10\n")
            (wt / "test_fee.py").write_text("def test_nothing():\n    pass\n")  # tries to weaken the test
            return AgentResult({"status": "done", "summary": "fee", "files_changed": ["fee.py"],
                                "tests": "ok", "blocker": ""}, 0.2)
        runner = FakeRunner({"F": [cheat]}, tests={"F": [tests_done(**{"test_fee.py": FEE_TEST})]})
        self.run_orch(runner)
        t = self.store.get("F")
        self.assertEqual((t.status, t.locked_tests), ("merged", ["test_fee.py"]))
        self.assertEqual(self.integration_file("test_fee.py"), FEE_TEST)
        self.assertEqual(runner.cards["F"][0].locked_tests, ["test_fee.py"])
        self.assertTrue(any("restored locked" in e["detail"] for e in self.store.events("F")))
        self.assertIn("written before implementation", runner.review_notes["F"][0])
        info = json.loads((self.cfg.state_path / "handoffs" / "F.tests.json").read_text())
        self.assertFalse(info["already_pass"])

    def test_tests_are_reused_on_a_later_attempt(self):
        impl = done(**{"fee.py": "def fee(x):\n    return x // 10\n"})
        runner = FakeRunner({"F": [impl]}, review={"F": [verdict("reject", fixable=False), "approve"]},
                            tests={"F": [tests_done(**{"test_fee.py": FEE_TEST})]})
        self.run_orch(runner)
        self.assertEqual((self.store.get("F").status, self.store.get("F").attempts), ("merged", 2))
        self.assertEqual(runner.tests_calls["F"], 1, "second attempt re-applied the saved tests")

    def test_test_writer_non_test_changes_and_deletions_are_put_back(self):
        (self.repo / "test_old.py").write_text("import unittest\n")
        sh("git add -A && git commit -qm old", self.repo)

        def sloppy(wt, n, task):
            (wt / "fee.py").write_text("def fee(x): pass\n")  # not a test: must not be locked
            (wt / "test_old.py").unlink()                     # deletions aren't locked either
            return tests_done(**{"test_fee.py": FEE_TEST})(wt, n, task)
        runner = FakeRunner({"F": [done(**{"fee.py": "def fee(x):\n    return x // 10\n"})]},
                            tests={"F": [sloppy]})
        self.run_orch(runner)
        t = self.store.get("F")
        self.assertEqual((t.status, t.locked_tests, runner.tests_calls["F"]), ("merged", ["test_fee.py"], 1))
        self.assertEqual(self.integration_file("fee.py"), "def fee(x):\n    return x // 10\n")
        self.assertEqual(self.integration_file("test_old.py"), "import unittest\n")

    def test_locked_tests_dont_count_toward_scope_or_size(self):
        self.cfg.max_diff_lines = 5
        big_test = FEE_TEST + "".join(f"\n    def test_{i}(self):\n        self.assertTrue(True)\n" for i in range(5))
        runner = FakeRunner({"F": [done(**{"fee.py": "def fee(x):\n    return x // 10\n"})]},
                            tests={"F": [tests_done(**{"tests/test_fee_more.py": big_test,
                                                       "test_fee.py": FEE_TEST})]})
        self.run_orch(runner)
        self.assertEqual(self.store.get("F").status, "merged")


class Environment(Base):
    def test_each_card_gets_its_own_slot_env_visible_to_gates(self):
        self.cfg.env.vars = {"DB": "app_test_{slot}"}
        self.cfg.gates = [Gate("env", f"{sys.executable} -c \"import os,sys; "
                                      "sys.exit(0 if os.environ['DB'].startswith('app_test_') else 3)\"")]
        self.add("A", ["a.py"])
        self.add("B", ["b.py"])
        runner = FakeRunner({"A": [done(**{"a.py": "x=1\n"})], "B": [done(**{"b.py": "x=1\n"})]}, delay=0.3)
        self.run_orch(runner)
        self.assertEqual(self.store.counts()["merged"], 2)
        slots = {runner.envs[t]["ORCHESTRA_SLOT"] for t in ("A", "B")}
        self.assertEqual(slots, {"1", "2"})
        self.assertEqual(runner.envs["A"]["DB"], f"app_test_{runner.envs['A']['ORCHESTRA_SLOT']}")

    def test_cloned_dependencies_skip_setup(self):
        (self.repo / ".gitignore").write_text("deps/\n")
        (self.repo / "package-lock.json").write_text("{}\n")
        sh("git add -A && git commit -qm lock", self.repo)
        (self.repo / "deps").mkdir()
        (self.repo / "deps" / "lib.txt").write_text("cached\n")
        self.cfg.clone_paths = ["deps"]
        self.cfg.setup_cmd = "exit 7"  # would fail the attempt if it ran
        self.add("C", ["c.py"])

        def check_clone(wt, n, task):
            assert (wt / "deps" / "lib.txt").read_text() == "cached\n"
            return done(**{"c.py": "x=1\n"})(wt, n, task)
        self.run_orch(FakeRunner({"C": [check_clone]}))
        self.assertEqual(self.store.get("C").status, "merged")
        self.assertTrue(any("setup_cmd skipped" in e["detail"] for e in self.store.events("C")))

    def test_changed_cmd_fails_fast_in_fix_rounds(self):
        marker = self.tmp / "full_ran"
        cfg = Config(repo=self.repo, gates=[Gate("test", f"touch {marker}", changed_cmd="echo {files}; exit 1")])
        for f in ("a.py", "test_a.py", "README.md"):
            (self.repo / f).write_text("x=1\n")
        stats = DiffStats(["a.py", "test_a.py", "README.md"], 3, 0, {"a.py": 1, "test_a.py": 1, "README.md": 1})
        results = asyncio.run(run_gates(cfg, self.repo, stats, "+++ b/a.py\n+x=1\n", [], quick=True))
        self.assertEqual([r.name for r in results if not r.passed], ["test (changed files)"])
        self.assertIn("test_a.py", results[-1].detail, "only changed test files are passed")
        self.assertNotIn("README.md", results[-1].detail)
        self.assertFalse(marker.exists(), "full command skipped after the quick check failed")
        # "no tests collected" (pytest 4/5) or no changed tests: inconclusive, the full cmd decides
        cfg.gates[0].changed_cmd = "exit 5"
        results = asyncio.run(run_gates(cfg, self.repo, stats, "+++ b/a.py\n+x=1\n", [], quick=True))
        self.assertTrue(all(r.passed for r in results) and marker.exists())


class Planning(Base):
    def test_critic_replaces_the_draft_and_bad_corrections_are_ignored(self):
        from orchestra.cli import plan_with_critic
        card = {"key": "a", "title": "A", "spec": "", "acceptance_criteria": ["x"], "non_goals": [],
                "files_scope": ["a.py"], "depends_on": [], "complexity": "small"}
        draft = {"summary": "s", "open_questions": [], "tasks": [card]}
        fixed = {**draft, "tasks": [{**card, "test_first": True}]}

        class Planner(FakeRunner):
            def __init__(self, corrected):
                super().__init__({})
                self.corrected = corrected

            async def plan(self, cfg, goal, cwd=None, graph=None):
                self.cwd = cwd
                return AgentResult(draft, 0.1)

            async def critique_plan(self, cfg, goal, plan, cwd=None, graph=None):
                return AgentResult({"verdict": "revise", "issues": [
                    {"task_key": "a", "problem": "touches money", "fix": "test_first"}], "plan": self.corrected}, 0.1)
        r = Planner(fixed)
        plan, issues, cost, cwd, _ = plan_with_critic(self.cfg, self.store, "goal", r)
        self.assertTrue(plan["tasks"][0]["test_first"])
        self.assertEqual(issues[0]["fix"], "test_first")
        self.assertEqual(Path(cwd).name, "_plan", "drafted in a worktree at the integration head")
        self.assertAlmostEqual(cost, 0.2)
        cyclic = {**draft, "tasks": [{**card, "depends_on": ["a"]}]}
        plan, *_ = plan_with_critic(self.cfg, self.store, "goal", Planner(cyclic))
        self.assertEqual(plan, draft)

    def test_longest_chain_first_and_path_warnings(self):
        for tid, deps in (("A", []), ("B", []), ("C", ["B"]), ("D", ["C"])):
            self.add(tid, [f"{tid.lower()}.py"], deps=deps)
        self.assertEqual([t.id for t in self.store.claim_ready(1)], ["B"])
        plan = {"tasks": [{"key": "k", "title": "K", "spec": "", "acceptance_criteria": [], "non_goals": [],
                           "files_scope": ["nope/x.py", "calc.py", "src/**"], "depends_on": [],
                           "complexity": "small", "test_first": True}]}
        tasks, warnings = build_tasks(plan, root=self.repo)
        self.assertTrue(tasks[0].test_first)
        self.assertEqual([w for w in warnings if "doesn't exist" in w], ["T01: folder of 'nope/x.py' doesn't exist in the repo; check the path"])


class Units(unittest.TestCase):
    def test_reviewer_model_is_the_other_family(self):
        cfg = Config(repo=Path("/tmp/x"))  # balanced: reviewer sonnet
        self.assertEqual(pick_reviewer_model(cfg, "sonnet"), "opus")
        self.assertEqual(pick_reviewer_model(cfg, "claude-opus-5-5"), "sonnet")
        cfg.review.reviewer_rule = "profile"
        self.assertEqual(pick_reviewer_model(cfg, "sonnet"), "sonnet")

    def test_drop_refuted(self):
        v = {"verdict": "reject", "criteria": [{"criterion": "c", "met": False, "evidence": "no test"}],
             "issues": [{"severity": "major", "file": "f", "issue": "bug"}], "scope_creep": ["x"], "feedback": ""}
        findings = blocking_findings(v)
        self.assertEqual(len(findings), 3)
        partly = drop_refuted(v, [{**findings[0], "evidence": "test_c exists"}])
        self.assertEqual(partly["verdict"], "reject")
        self.assertTrue(partly["criteria"][0]["met"])
        allgone = drop_refuted(v, [{**f, "evidence": "e"} for f in findings])
        self.assertEqual((allgone["verdict"], allgone["issues"], allgone["scope_creep"]), ("approve", [], []))

    def test_cli_test_first_flag(self):
        from orchestra.cli import main
        tmp = Path(tempfile.mkdtemp())
        sh("git init -q -b main && git config user.email t@t && git config user.name t && "
           "echo x > a && git add -A && git commit -qm i", tmp)
        st = Store(tmp / ".orchestra" / "state.db")
        st.add_task(Task(id="T01", title="t", spec=""))
        main(["-C", str(tmp), "test-first", "T01"])
        self.assertTrue(Store(tmp / ".orchestra" / "state.db").get("T01").test_first)
        main(["-C", str(tmp), "test-first", "T01", "--off"])
        self.assertFalse(Store(tmp / ".orchestra" / "state.db").get("T01").test_first)


if __name__ == "__main__":
    unittest.main()


class ReviewFindingsRegressions(Base):
    """Cases found by the independent review of this change (see design.md, 'As built')."""

    def test_conflict_round_keeps_other_cards_edits_to_locked_tests(self):
        pad = "".join(f"# pad {i}\n" for i in range(20))
        base_test = ("import unittest\nfrom calc import add\n" + pad +
                     "class T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n")
        (self.repo / "test_calc.py").write_text(base_test)
        sh("git commit -qam longer-test", self.repo)
        self.add("A", ["shared.txt", "test_calc.py"])
        self.add("F", ["shared.txt", "test_calc.py", "fee.py"], test_first=True)

        def a_step(wt, n, task):
            (wt / "shared.txt").write_text("line1\nA\nline3\n")
            (wt / "test_calc.py").write_text("# A was here\n" + base_test)
            return AgentResult({"status": "done", "summary": "A", "files_changed": [], "tests": "",
                                "blocker": ""}, 0.1, session_id="sa")

        def writer(wt, n, task):
            p = wt / "test_calc.py"
            p.write_text(p.read_text() + "    def test_fee(self):\n        from fee import fee\n"
                                          "        self.assertEqual(fee(100), 10)\n")
            return AgentResult({"status": "done", "test_files": ["test_calc.py"], "covered": [],
                                "assumptions": [], "blocker": ""}, 0.1, session_id="tw")

        def f_impl(wt, n, task):
            (wt / "shared.txt").write_text("line1\nF\nline3\n")
            (wt / "fee.py").write_text("def fee(x):\n    return x // 10\n")
            return AgentResult({"status": "done", "summary": "F", "files_changed": [], "tests": "",
                                "blocker": ""}, 0.1, session_id="sf")

        def resolve(wt, n, task):
            (wt / "shared.txt").write_text("line1\nA and F\nline3\n")
            return AgentResult({"status": "done", "summary": "resolved", "files_changed": [], "tests": "",
                                "blocker": ""}, 0.1, session_id="sf")
        runner = FakeRunner({"A": [a_step], "F": [f_impl, resolve]}, tests={"F": [writer]}, delays={"F": 0.8})
        self.run_orch(runner)
        self.assertEqual((self.store.get("A").status, self.store.get("F").status), ("merged", "merged"))
        txt = self.integration_file("test_calc.py")
        self.assertIn("# A was here", txt)
        self.assertIn("test_fee", txt)

    def test_interrupted_regate_is_rerun_and_review_sees_only_this_card(self):
        from orchestra.orchestrator import Orchestrator
        log = self.tmp / "gates.log"
        self.cfg.gates = [Gate("test", f"sh -c 'echo run $(ls a.txt b.txt 2>/dev/null | tr \"\\n\" \" \") >> {log}; "
                                       f"if [ -f a.txt ] && [ -f b.txt ]; then sleep 30; fi'")]
        self.add("A", ["a.txt"])
        self.add("B", ["b.txt"])

        class Spy(FakeRunner):
            async def review(self, cfg, task, wt, diff, report, gate_log, **kw):
                self.diffs = getattr(self, "diffs", {})
                self.diffs.setdefault(task.id, []).append(diff)
                return await super().review(cfg, task, wt, diff, report, gate_log, **kw)
        r1 = Spy({"A": [sess(done(**{"a.txt": "a\n"}), "sa")], "B": [sess(done(**{"b.txt": "b\n"}), "sb")]},
                 delays={"B": 0.8})

        async def interrupted():
            try:
                await asyncio.wait_for(Orchestrator(self.cfg, self.store, r1, log=self.logs.append,
                                                    poll_s=0.2).run(), timeout=4)
            except asyncio.TimeoutError:
                pass
        asyncio.run(interrupted())
        self.assertEqual(self.store.get("B").status, "review")
        log.write_text("")
        self.cfg.gates = [Gate("test", f"sh -c 'echo run >> {log}'")]
        r2 = Spy({"B": [sess(done(**{"b.txt": "b\n"}), "sb")]})
        asyncio.run(Orchestrator(self.cfg, self.store, r2, log=self.logs.append, poll_s=0.2).run())
        self.assertEqual(self.store.get("B").status, "merged")
        self.assertNotIn("a.txt", r2.diffs["B"][0], "resumed review diffs against the rebased base")
        self.assertIn("run", log.read_text(), "the interrupted re-gate ran again before merging")

    def test_budget_reached_parks_a_revision_for_the_next_run(self):
        self.cfg.total_budget_usd = 0.25  # one worker run (0.2) + one review (0.1) passes it
        self.add("P", ["p.py"])
        r1 = FakeRunner({"P": [sess(done(**{"p.py": "x=1\ny=2\n"}))]}, review={"P": [verdict("reject")]})
        self.run_orch(r1)
        t = self.store.get("P")
        self.assertEqual((t.status, t.attempts), ("pending", 0))
        self.assertTrue(t.resume_note.startswith(REVISE_NOTE))
        self.cfg.total_budget_usd = 100
        r2 = FakeRunner({"P": [sess(done(**{"p.py": "x=1\n"}))]}, review={"P": ["approve"]})
        self.run_orch(r2)
        t = self.store.get("P")
        self.assertEqual((t.status, t.attempts), ("merged", 1))
        self.assertTrue(r2.resumes["P"][0].startswith(REVISE_NOTE), "resumed the parked revision")
        self.assertIn("This is a revision", r2.review_notes["P"][0], "round context survived the pause")
        self.assertIn("Later rounds", sh("git log -1 --format=%B orchestra/integration", self.repo))
