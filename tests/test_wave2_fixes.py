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

from orchestra.config import Config  # noqa: E402
from orchestra.harness import build_files, install_harness, stale_files  # noqa: E402
from orchestra.store import Store  # noqa: E402
from tests.test_orchestra import Base  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
