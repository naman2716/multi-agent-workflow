"""Tests for wave-2-fixes: groom sizing, scope from answers, fresh triage on retry, card surgery,
integration sync, test-writer reuse and size check, data-file hint, detached runs and ship
resume, and the usage report."""
import asyncio
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestra.agents import WORK_SCHEMA, WORKER_APPEND, AgentResult, parent_tests_note  # noqa: E402
from orchestra.gates import make_size_guard  # noqa: E402
from orchestra.store import Task  # noqa: E402
from orchestra.asking import render_question  # noqa: E402
from orchestra.cards import clean_scope_paths  # noqa: E402
from orchestra.config import Config  # noqa: E402
from orchestra.gates import in_scope  # noqa: E402
from orchestra.gitops import Git, SyncError  # noqa: E402
from orchestra.harness import build_files, install_harness, stale_files  # noqa: E402
from orchestra.orchestrator import Orchestrator  # noqa: E402
from orchestra.store import Store  # noqa: E402
from tests.test_decisions_limits import with_session  # noqa: E402
from tests.test_orchestra import Base, FakeRunner, blocked, done, sh  # noqa: E402
from tests.test_keep_good_work import FEE_TEST, tests_done  # noqa: E402
from tests.test_run_without_you import TriageRunner  # noqa: E402
from tests.test_spend_less import CapturingRunner, lines  # noqa: E402


def card(key, est=None, scope=None, deps=()):
    c = {"key": key, "title": key.upper(), "spec": "s", "acceptance_criteria": ["works"], "non_goals": [],
         "files_scope": scope or [f"{key}.py"], "depends_on": list(deps), "complexity": "small"}
    if est is not None:
        c["estimated_lines"] = est
    return c


def cli(repo, *args) -> tuple[str, int]:
    """Run the CLI; returns (stdout, exit code)."""
    from orchestra.cli import main
    out, code = io.StringIO(), 0
    with redirect_stdout(out):
        try:
            main(["-C", str(repo), *args])
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            if isinstance(e.code, str):
                out.write(e.code)
    return out.getvalue(), code


class GroomSizing(Base):
    def setUp(self):
        super().setUp()
        (self.repo / "orchestra.toml").write_text("[graph]\nenabled = false\n")
        self.plan = self.tmp / "plan.json"
        self.plan.write_text(json.dumps({"summary": "s", "tasks": [card("a"), card("b", 0), card("c", 300)]}))

    def queued(self):
        return [t.id for t in Store(self.cfg.state_path / "state.db").all()]

    def test_approve_warns_once_per_card_without_an_estimate(self):
        out, code = cli(self.repo, "approve", str(self.plan))
        self.assertEqual(code, 0)
        self.assertEqual(out.count("no estimated_lines"), 2, out)
        self.assertIn("T01: no estimated_lines", out)
        self.assertIn("T02: no estimated_lines", out)
        self.assertEqual(self.queued(), ["T01", "T02", "T03"])

    def test_add_warns_too(self):
        out, code = cli(self.repo, "add", str(self.plan))
        self.assertEqual((code, out.count("no estimated_lines")), (0, 2), out)

    def test_require_estimates_refuses_and_queues_nothing(self):
        (self.repo / "orchestra.toml").write_text("[graph]\nenabled = false\n[planning]\nrequire_estimates = true\n")
        for cmd in ("approve", "add"):
            out, code = cli(self.repo, cmd, str(self.plan))
            self.assertNotEqual(code, 0)
            self.assertIn("Not queued: T01, T02 have no estimated_lines", out)
        self.assertEqual(self.queued(), [])
        self.plan.write_text(json.dumps({"tasks": [card("a", 100)]}))
        self.assertEqual(cli(self.repo, "approve", str(self.plan))[1], 0)

    def test_groom_skill_has_the_sizing_rules(self):
        cfg = Config(repo=self.repo, max_diff_lines=600, split_above=0.7)
        groom = build_files(cfg, graph=False)[".claude/skills/groom/SKILL.md"]
        for text in ("## Sizing", "over 600 changed lines", "above 420 lines", "estimated_lines",
                     "diff_exclude", "est. lines"):
            self.assertIn(text, groom)
        self.assertNotIn("~300", groom)

    def test_stale_harness_files_are_reported(self):
        cfg = Config.load(self.repo)
        install_harness(cfg, graph=False, git_hooks=False)
        self.assertEqual(stale_files(cfg), [])
        self.assertNotIn("harness files differ", cli(self.repo, "approve", str(self.plan))[0])
        skill = self.repo / ".claude/skills/groom/SKILL.md"
        skill.write_text(skill.read_text().replace("## Sizing", "## Old"))
        self.assertEqual(stale_files(cfg), [".claude/skills/groom/SKILL.md"])
        out, _ = cli(self.repo, "approve", str(self.plan), "--force")
        self.assertIn("harness files differ from this Orchestra (groom)", out)
        self.assertIn("orchestra harness install", out)

    def test_no_harness_installed_is_not_stale(self):
        self.assertEqual(stale_files(Config.load(self.repo)), [])


def widen_ask(scope_add):
    """T17 asks whether it may edit models/core.py and add a migration (option 1)."""
    def ask(wt, n, task):
        (wt / "seed").mkdir(exist_ok=True)
        (wt / "seed/data.py").write_text("ROWS = []\n")
        return AgentResult({"status": "needs_decision", "summary": "seed drafted", "files_changed": [],
                            "tests": "", "blocker": "",
                            "decision": {"question": "May I widen scope to edit models/core.py?",
                                         "options": ["Widen scope: edit models/core.py and add a migration",
                                                     "Split the card"],
                                         "recommendation": "Widen scope: edit models/core.py and add a migration",
                                         "why_it_matters": "the model lacks 'fytd'", "scope_add": scope_add}},
                           0.1, session_id="s17")
    return ask


class ScopeFromAnswers(Base):
    def setUp(self):
        super().setUp()
        (self.repo / "orchestra.toml").write_text("[graph]\nenabled = false\n")
        self.cfg.decision_wait_minutes = 0  # don't wait for the answer inside the run
        self.add("T17", ["seed/**"])

    def ask(self, scope_add=None):
        finish = with_session(done(**{"seed__data.py": "ROWS = [1]\n", "models__core.py": "G = ['fytd']\n",
                                      "migrations__0002.py": "UP = 1\n"}), "s17")
        runner = FakeRunner({"T17": [widen_ask(scope_add or [{"option": 1, "paths": [
            "models/core.py", "migrations/*.py"]}]), finish]})
        self.run_orch(runner)
        return runner

    def test_worker_can_name_the_files_an_option_needs(self):
        self.assertIn("scope_add", WORK_SCHEMA["properties"]["decision"]["properties"])
        self.assertIn("scope_add", WORKER_APPEND)

    def test_picking_the_scope_option_widens_the_card_and_the_gate_accepts_it(self):
        runner = self.ask()
        t = self.store.get("T17")
        self.assertEqual(t.status, "waiting")
        self.assertEqual(t.decision["scope_add"], {"1": ["models/core.py", "migrations/*.py"]})
        out, _ = cli(self.repo, "decisions")
        self.assertIn("(adds to scope: models/core.py, migrations/*.py)", out)
        self.assertIn("adds to scope: models/core.py", render_question("T17", "t", t.decision, 0))
        self.assertFalse(in_scope("models/core.py", t.files_scope))

        out, code = cli(self.repo, "answer", "T17", "--pick", "1")
        self.assertEqual(code, 0, out)
        self.assertIn("files_scope now also includes: models/core.py, migrations/*.py", out)
        t = self.store.get("T17")
        self.assertEqual(t.files_scope, ["seed/**", "models/core.py", "migrations/*.py"])
        self.assertIn("Your files_scope now also includes: models/core.py", t.resume_note)
        self.assertTrue(any(e["kind"] == "scope" and "+models/core.py" in e["detail"]
                            for e in self.store.events("T17")))
        self.run_orch(runner)  # the resumed worker edits the model; the scope gate lets it merge
        self.assertEqual(self.store.get("T17").status, "merged")
        self.assertEqual(self.integration_file("models/core.py"), "G = ['fytd']\n")

    def test_a_typed_or_recommended_pick_counts_too(self):
        self.ask()
        self.store.answer("T17", "Widen scope: edit models/core.py and add a migration — keep it small")
        self.assertIn("models/core.py", self.store.get("T17").files_scope)

    def test_other_options_and_free_text_leave_the_scope_alone(self):
        self.ask()
        cli(self.repo, "answer", "T17", "--pick", "2")
        self.assertEqual(self.store.get("T17").files_scope, ["seed/**"])

    def test_manual_scope_add_with_the_answer(self):
        self.ask()
        out, code = cli(self.repo, "answer", "T17", "split it", "--scope-add", "docs/notes.md")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.store.get("T17").files_scope, ["seed/**", "docs/notes.md"])

    def test_paths_outside_the_repo_are_refused(self):
        self.assertEqual(clean_scope_paths(["a/b.py", "a/b.py", "../x", "/etc/passwd", "~/x", ".git/config",
                                            ".orchestra/state.db", "", "src/**"]),
                         (["a/b.py", "src/**"], ["../x", "/etc/passwd", "~/x", ".git/config",
                                                 ".orchestra/state.db", ""]))
        self.ask()
        for bad in ("../x.py", "/etc/x", ".git/hooks/pre-commit", ".orchestra/state.db"):
            out, code = cli(self.repo, "answer", "T17", "--pick", "2", "--scope-add", bad)
            self.assertNotEqual(code, 0, bad)
            self.assertEqual(self.store.get("T17").status, "waiting")

    def test_the_question_drops_paths_it_cant_grant(self):
        self.ask([{"option": 1, "paths": ["models/core.py", "../secrets.py"]}, {"option": 9, "paths": ["x.py"]}])
        self.assertEqual(self.store.get("T17").decision["scope_add"], {"1": ["models/core.py"]})

    def test_triage_split_inside_the_widened_scope_is_accepted(self):
        self.ask()
        self.store.answer("T17", "", pick=1)
        orch = Orchestrator(self.cfg, self.store, FakeRunner({}), log=self.logs.append)
        cards = [card("model", 80, ["models/core.py", "migrations/*.py"]), card("seed", 200, ["seed/**"], ["model"])]
        msg = orch._triage_split(self.store.get("T17"), {"cards": cards}, "too big")
        self.assertTrue(msg.startswith("split into"), msg)

    def test_widened_card_waits_for_open_cards_on_those_files(self):
        self.ask()
        self.add("T18", ["models/**"])  # queued while T17 waits
        self.store.answer("T17", "", pick=1)
        self.assertIn("T18", self.store.get("T17").depends_on)

    def test_scope_command(self):
        out, code = cli(self.repo, "scope", "T17", "--add", "models/core.py", "--remove", "nope")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.store.get("T17").files_scope, ["seed/**", "models/core.py"])
        cli(self.repo, "scope", "T17", "--remove", "seed/**")
        self.assertEqual(self.store.get("T17").files_scope, ["models/core.py"])
        out, code = cli(self.repo, "scope", "T17", "--remove", "models/core.py")
        self.assertNotEqual(code, 0)
        self.assertIn("no files_scope", out)
        self.store.update("T17", status="merged")
        self.assertNotEqual(cli(self.repo, "scope", "T17", "--add", "x.py")[1], 0)
        self.assertEqual(cli(self.repo, "scope", "T17", "--add", "x.py", "--force")[1], 0)


class RetryResetsTriage(Base):
    def block_once(self):
        runner = TriageRunner({"A": [blocked]})  # triage says: human
        self.run_orch(runner)
        return runner

    def test_a_retried_card_is_triaged_again_on_its_next_block(self):
        self.add("A", ["a.py"])
        self.assertEqual(self.block_once().triage_calls, {"A": 1})
        self.assertEqual((self.store.get("A").status, self.store.get("A").triaged), ("blocked", True))
        out, code = cli(self.repo, "retry", "A")
        self.assertEqual(code, 0, out)
        self.assertIn("triaged again", out)
        self.assertFalse(self.store.get("A").triaged)
        self.assertEqual(self.block_once().triage_calls, {"A": 1})  # a fresh triage, not straight to you
        self.assertTrue(any("triage reset" in (e["detail"] or "") for e in self.store.events("A")))

    def test_keep_triage_keeps_the_old_behaviour(self):
        self.add("A", ["a.py"])
        self.block_once()
        cli(self.repo, "retry", "A", "--keep-triage")
        self.assertEqual(self.block_once().triage_calls, {})


class CardSurgery(Base):
    """Groom transcript 217-222: T15 split by hand, T16 made to wait for T29, T15's criteria cut."""

    def setUp(self):
        super().setUp()
        (self.repo / "orchestra.toml").write_text("[graph]\nenabled = false\n")
        self.add("T15", ["docs/contracts/**"])
        self.store.update("T15", acceptance=["contracts", "README"])
        self.add("T16", ["docs/README.md"], deps=["T15"])
        self.add("T29", ["docs/other.md"])

    def test_edit_depends_on_scope_and_acceptance(self):
        out, code = cli(self.repo, "edit", "T16", "--depends-on", "T15,T29")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.store.get("T16").depends_on, ["T15", "T29"])
        crit = self.tmp / "crit.json"
        crit.write_text(json.dumps(["contracts only"]))
        cli(self.repo, "edit", "T15", "--acceptance-file", str(crit), "--acceptance-add", "no README")
        t = self.store.get("T15")
        self.assertEqual(t.acceptance, ["contracts only", "no README"])
        self.assertTrue(any(f.startswith("The human lead edited this card: acceptance") for f in t.feedback))
        cli(self.repo, "edit", "T29", "--scope", "docs/contracts/x.md", "--title", "New title")
        t29 = self.store.get("T29")
        self.assertEqual((t29.files_scope, t29.title), (["docs/contracts/x.md"], "New title"))
        self.assertIn("T15", t29.depends_on)  # now overlaps an open card -> waits for it
        self.assertEqual([e["kind"] for e in self.store.events("T29") if e["kind"] == "edit"], ["edit"])

    def test_cycles_unknown_ids_and_bad_paths_are_refused(self):
        for args, msg in ((["T15", "--depends-on", "T16"], "cycle"), (["T16", "--depends-on", "T99"], "unknown"),
                          (["T16", "--depends-on", "T16"], "unknown"), (["T16", "--scope", "../x"], "repo-relative")):
            out, code = cli(self.repo, "edit", *args)
            self.assertNotEqual(code, 0, args)
            self.assertIn(msg, out)
        self.assertEqual(self.store.get("T15").depends_on, [])

    def test_merged_and_live_cards_need_force(self):
        self.store.update("T15", status="merged")
        self.assertNotEqual(cli(self.repo, "edit", "T15", "--title", "x")[1], 0)
        self.assertEqual(cli(self.repo, "edit", "T15", "--title", "x", "--force")[1], 0)
        self.store.update("T16", status="running")
        self.store.acquire_lease()
        out, code = cli(self.repo, "edit", "T16", "--title", "y")
        self.assertNotEqual(code, 0)
        self.assertIn("live run", out)

    def test_new_criteria_discard_locked_tests(self):
        handoffs = self.cfg.state_path / "handoffs"
        handoffs.mkdir(parents=True, exist_ok=True)
        (handoffs / "T15.tests.patch").write_text("diff")
        self.store.update("T15", test_first=True, locked_tests=["tests/test_c.py"])
        cli(self.repo, "edit", "T15", "--acceptance-add", "one more")
        self.assertEqual(self.store.get("T15").locked_tests, [])
        self.assertFalse((handoffs / "T15.tests.patch").exists())

    def test_manual_split_rewires_dependents(self):
        plan = self.tmp / "split.json"
        plan.write_text(json.dumps({"tasks": [card("a1", 300, ["docs/contracts/a/**"]),
                                              card("a1b", 200, ["docs/contracts/b/**", "docs/extra.md"], ["a1"])]}))
        out, code = cli(self.repo, "split", "T15", str(plan), "--why", "621 lines")
        self.assertEqual(code, 0, out)
        self.assertIn("T15 split into T04, T05", out)
        self.assertIn("reaches outside the original files_scope", out)  # a human may widen; it's noted
        self.assertEqual(self.store.get("T15").status, "cancelled")
        self.assertEqual(self.store.get("T05").depends_on, ["T04"])
        self.assertEqual(self.store.get("T16").depends_on, ["T04", "T05"])
        self.assertFalse(self.store.get("T04").triaged)  # a human's split earns triage
        self.assertIn("Split from T15 by human: 621 lines", self.store.get("T04").feedback)
        self.assertNotEqual(cli(self.repo, "split", "T15", str(plan))[1], 0)  # already split

    def test_a_split_needs_two_cards(self):
        plan = self.tmp / "one.json"
        plan.write_text(json.dumps([card("only", 100)]))
        out, code = cli(self.repo, "split", "T15", str(plan))
        self.assertNotEqual(code, 0)
        self.assertIn("at least two cards", out)


class IntegrationSync(Base):
    """#29/#39/#54: integration was cut at 50422e7 and main moved to d1aa457."""

    def setUp(self):
        super().setUp()
        (self.repo / "orchestra.toml").write_text("[graph]\nenabled = false\n")
        sh("git branch orchestra/integration main", self.repo)

    def sync(self, **kw):
        g = Git(self.repo, self.cfg.worktree_root, "main", "orchestra/integration")
        return asyncio.run(g.sync_integration(**kw))

    def commit(self, branch, name, text="x\n"):
        sh(f"git checkout -q {branch} && printf '{text}' > {name} && git add {name} && "
           f"git commit -qm {name} && git checkout -q main", self.repo)

    def head(self, ref):
        return sh(f"git rev-parse {ref}", self.repo).strip()

    def test_up_to_date_and_ahead_are_left_alone(self):
        self.assertEqual(self.sync().action, "up_to_date")
        self.commit("orchestra/integration", "merged_card.py")
        before = self.head("orchestra/integration")
        self.assertEqual(self.sync().action, "ahead")
        self.assertEqual(self.head("orchestra/integration"), before)

    def test_behind_is_fast_forwarded(self):
        old = self.head("orchestra/integration")
        self.commit("main", "addendum.md")
        res = self.sync()
        self.assertEqual((res.action, res.old, res.new), ("fast_forwarded", old, self.head("main")))
        self.assertEqual(self.head("orchestra/integration"), self.head("main"))
        self.assertIn("fast-forwarded", res.message("main", "orchestra/integration"))

    def test_report_only_does_not_move(self):
        self.commit("main", "addendum.md")
        self.assertEqual(self.sync(move=False).action, "behind")
        self.assertNotEqual(self.head("orchestra/integration"), self.head("main"))

    def test_a_checked_out_integration_branch_is_not_moved(self):
        self.commit("main", "addendum.md")
        wt = self.tmp / "integ-checkout"
        sh(f"git worktree add -q {wt} orchestra/integration", self.repo)
        res = self.sync()
        self.assertEqual(res.action, "behind")
        self.assertIn("checked out in", res.message("main", "orchestra/integration"))

    def test_diverged_stops_unless_allowed(self):
        self.commit("orchestra/integration", "card.py")
        self.commit("main", "hotfix.py")
        with self.assertRaises(SyncError) as e:
            self.sync()
        self.assertIn("1 commit(s) only on orchestra/integration, 1 only on main", str(e.exception))
        self.assertIn("--allow-diverged", str(e.exception))
        self.assertEqual(self.sync(allow_diverged=True).action, "diverged")

    def test_squash_merged_pr_is_recognised(self):
        self.commit("orchestra/integration", "card.py", "y\n")
        self.commit("main", "hotfix.py")
        self.commit("main", "card.py", "y\n")  # the same change, squashed onto main as a new commit
        with self.assertRaises(SyncError) as e:
            self.sync()
        self.assertIn("squash-merged", str(e.exception))
        self.assertIn("git branch -f orchestra/integration main", str(e.exception))

    def test_run_fast_forwards_before_dispatch(self):
        self.commit("main", "addendum.md", "rules\n")
        self.add("T15", ["c.py"])
        runner = FakeRunner({"T15": [done(**{"c.py": "open('addendum.md').read()\n"})]})
        self.run_orch(runner)
        self.assertEqual(self.store.get("T15").status, "merged")
        self.assertEqual(self.integration_file("addendum.md"), "rules\n")
        self.assertTrue(any(e["kind"] == "sync" and "fast-forwarded" in e["detail"] for e in self.store.events()))

    def test_run_stops_on_diverged(self):
        self.commit("orchestra/integration", "card.py")
        self.commit("main", "hotfix.py")
        self.add("T15", ["c.py"])
        orch = Orchestrator(self.cfg, self.store, FakeRunner({}), log=self.logs.append)
        with self.assertRaises(SyncError):
            asyncio.run(orch.run())
        self.assertEqual(self.store.get("T15").status, "pending")
        self.assertIsNone(self.store.live_lease())  # the lease is released

    def test_approve_syncs_refuses_diverged_and_only_reports_under_a_live_run(self):
        plan = self.tmp / "p.json"
        plan.write_text(json.dumps({"tasks": [card("a", 100)]}))
        self.commit("main", "addendum.md")
        token = self.store.acquire_lease()
        out, code = cli(self.repo, "approve", str(plan))
        self.assertIn("is behind main", out)
        self.assertNotEqual(self.head("orchestra/integration"), self.head("main"))
        self.store.release_lease(token)
        out, code = cli(self.repo, "approve", str(plan), "--force")
        self.assertIn("fast-forwarded", out)
        self.assertEqual(self.head("orchestra/integration"), self.head("main"))
        self.commit("orchestra/integration", "card.py")
        self.commit("main", "hotfix.py")
        out, code = cli(self.repo, "approve", str(plan), "--force")
        self.assertNotEqual(code, 0)
        self.assertIn("have diverged", out)
        self.assertEqual(cli(self.repo, "approve", str(plan), "--force", "--allow-diverged")[1], 0)


BIG_JSON = "[\n" + "".join(f"  {i},\n" for i in range(400)) + "  0\n]\n"


class ReferenceRunner(TriageRunner):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.references = {}

    async def write_tests(self, cfg, task, wt, graph=None, resume=None, env=None, reference=""):
        self.references[task.id] = reference
        return await super().write_tests(cfg, task, wt, graph=graph, resume=resume, env=env)


class TestWriterReuse(Base):
    """T20: tests written (2,268 lines, 1,528 of them one JSON fixture), then the card was split and
    T30-T33's test writers started from nothing."""

    def test_children_of_a_split_test_first_card_get_the_parents_tests(self):
        self.add("P", ["fee.py", "tests/*"], test_first=True)
        split = {"action": "split", "reason": "862 lines", "cards": [
            card("fee-core", 200, ["fee.py", "tests/test_fee.py"]),
            card("fee-parity", 200, ["tests/test_parity.py"], ["fee-core"])]}
        fee = done(**{"fee.py": "def fee(x):\n    return x // 10\n"})
        runner = ReferenceRunner(
            {"P": [blocked], "T02": [fee], "T03": [done(**{"tests__notes.txt": "n\n"})]},
            tests={"P": [tests_done(**{"tests/test_fee.py": FEE_TEST, "tests/expected.json": BIG_JSON})],
                   "T02": [tests_done(**{"tests/test_fee.py": FEE_TEST})],
                   "T03": [tests_done(**{"tests/test_parity.py": "PARITY = 1\n"})]},
            triage={"P": split})
        self.run_orch(runner)
        self.assertEqual(self.store.get("P").summary, "split into T02, T03")
        self.assertEqual(runner.references["P"], "")
        for child in ("T02", "T03"):
            ref = runner.references[child]
            self.assertIn("## Tests written for P before it was split", ref)
            self.assertIn("self.assertEqual(fee(100), 10)", ref)          # test code: shown
            self.assertIn("tests/expected.json (403 lines, data: not shown)", ref)
            self.assertNotIn("  399,", ref)                                # data: not copied
        self.assertEqual(self.store.get("T02").status, "merged")

    def test_children_of_a_card_without_written_tests_get_nothing(self):
        orch = Orchestrator(self.cfg, self.store, FakeRunner({}), log=self.logs.append)
        self.add("P", ["fee.py"], test_first=True)
        orch._triage_split(self.store.get("P"), {"cards": [card("a", 10, ["fee.py"]), card("b", 10, ["fee.py"])]}, "x")
        self.assertEqual(self.store.get_meta("tests_from:T02"), "P")
        self.assertEqual(orch._parent_tests(self.store.get("T02")), "")

    def test_note_caps_code_and_lists_data(self):
        patch = "".join(f"diff --git a/tests/t{i}.py b/tests/t{i}.py\n--- /dev/null\n+++ b/tests/t{i}.py\n"
                        + "".join(f"+line {j}\n" for j in range(100)) for i in range(5))
        patch += "diff --git a/tests/fx.csv b/tests/fx.csv\n+++ b/tests/fx.csv\n+a,b\n"
        note = parent_tests_note("T20", patch, limit_chars=2_500)
        self.assertIn("+++ b/tests/t0.py", note)
        self.assertIn("tests/t4.py (100 lines, not shown: over the size cap)", note)
        self.assertIn("tests/fx.csv (1 lines, data: not shown)", note)
        self.assertEqual(parent_tests_note("T20", ""), "")

    def test_test_writer_gets_its_own_size_budget(self):
        self.cfg.test_max_lines = 100
        base = sh("git rev-parse HEAD", self.repo).strip()
        hook = make_size_guard(self.repo, base, 100, [], role="test_writer")
        check = lambda: asyncio.run(hook({}, None, None)).get("hookSpecificOutput", {}).get("additionalContext")  # noqa: E731
        (self.repo / "expected.json").write_text(lines(90))
        self.assertIn("near the 100-line budget", check())
        (self.repo / "more.json").write_text(lines(30))
        msg = check()
        self.assertIn("over the 100-line budget for acceptance tests", msg)
        self.assertIn("generate that data at test time", msg)
        r = CapturingRunner()
        asyncio.run(r.write_tests(self.cfg, Task(id="T1", title="t", spec="s", base_commit=base), self.repo,
                                  reference="## Tests written for T0 before it was split\nx"))
        self.assertIn("PostToolUse", r.options.hooks)
        self.assertIn("## Tests written for T0", r.prompt)
        self.assertTrue(r.prompt.rstrip().endswith("Write the acceptance tests for this card now (tests only)."))


class DataFileHint(unittest.TestCase):
    """#139: T17 copied a ~1,500-line JSON file; triage proposed a compact form + round-trip test."""

    def test_workers_test_writers_and_triage_get_the_hint(self):
        from orchestra.agents import DATA_FILE_HINT, TEST_WRITER_APPEND, TRIAGE_SYSTEM, sizing_rules
        for prompt in (WORKER_APPEND, TEST_WRITER_APPEND, TRIAGE_SYSTEM):
            self.assertIn(DATA_FILE_HINT, prompt)
        self.assertIn("round-trips", DATA_FILE_HINT)
        self.assertIn("diff_exclude", DATA_FILE_HINT)
        for prompt in (WORKER_APPEND, TEST_WRITER_APPEND):
            self.assertTrue(prompt.rstrip().endswith("Your final answer must be the structured report."))
        rules = sizing_rules(Config(repo=Path("/tmp")))
        self.assertIn("A data file copied into the repo is a sizing risk", rules)
        groom = build_files(Config(repo=Path("/tmp")), graph=False)[".claude/skills/groom/SKILL.md"]
        self.assertIn("A data file copied into the repo is a sizing risk", groom)


if __name__ == "__main__":
    unittest.main()
