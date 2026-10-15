"""Tests for wave-2-fixes: groom sizing, scope from answers, fresh triage on retry, card surgery,
integration sync, test-writer reuse and size check, data-file hint, detached runs and ship
resume, and the usage report."""
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestra.agents import WORK_SCHEMA, WORKER_APPEND, AgentResult  # noqa: E402
from orchestra.asking import render_question  # noqa: E402
from orchestra.cards import clean_scope_paths  # noqa: E402
from orchestra.config import Config  # noqa: E402
from orchestra.gates import in_scope  # noqa: E402
from orchestra.harness import build_files, install_harness, stale_files  # noqa: E402
from orchestra.orchestrator import Orchestrator  # noqa: E402
from orchestra.store import Store  # noqa: E402
from tests.test_decisions_limits import with_session  # noqa: E402
from tests.test_orchestra import Base, FakeRunner, done  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
