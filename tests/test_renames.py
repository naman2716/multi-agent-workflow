"""Renames and moves: diff stats report real paths, so the scope gate checks them correctly."""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestra.agents import AgentResult  # noqa: E402
from orchestra.gitops import Git  # noqa: E402
from tests.test_orchestra import Base, FakeRunner, sh  # noqa: E402


def move(src, dst):
    def step(wt, n, task):
        (wt / dst).parent.mkdir(parents=True, exist_ok=True)
        (wt / src).rename(wt / dst)
        return AgentResult({"status": "done", "summary": f"moved {src} to {dst}", "files_changed": [src, dst],
                            "tests": "ok", "blocker": ""}, 0.1)
    return step


class Renames(Base):
    def test_diff_stats_lists_both_paths_of_a_rename(self):
        sh("git checkout -q -b side && mkdir -p mvp 'with space' && git mv shared.txt mvp/shared.txt && "
           "git mv calc.py 'with space/calc é.py' && echo x >> mvp/shared.txt && git add mvp/shared.txt && git commit -qm move", self.repo)
        g = Git(self.repo, self.cfg.worktree_root, "main", "orchestra/integration")
        stats = asyncio.run(g.diff_stats(self.repo, "main"))
        self.assertEqual(sorted(stats.files), ["calc.py", "mvp/shared.txt", "shared.txt", "with space/calc é.py"])
        self.assertFalse(any("=>" in f or f.startswith('"') for f in stats.files))
        self.assertEqual(stats.per_file["mvp/shared.txt"], 1)
        self.assertEqual(stats.lines, 1)

    def test_a_pure_move_card_passes_the_scope_gate(self):
        self.add("A", ["shared.txt", "mvp/**"])
        self.run_orch(FakeRunner({"A": [move("shared.txt", "mvp/shared.txt")]}))
        self.assertEqual(self.store.get("A").status, "merged")
        self.assertEqual(self.integration_file("mvp/shared.txt"), "line1\nline2\nline3\n")

    def test_moving_a_file_out_of_scope_is_still_caught_by_its_real_path(self):
        self.cfg.max_attempts = 1
        self.add("A", ["mvp/**"])  # the old location isn't in scope
        self.run_orch(FakeRunner({"A": [move("shared.txt", "mvp/shared.txt")]}))
        a = self.store.get("A")
        self.assertEqual(a.status, "blocked")
        self.assertTrue(any("['shared.txt']" in f for f in a.feedback), a.feedback[-1])
        self.assertFalse(any("=>" in f for f in a.feedback))


if __name__ == "__main__":
    unittest.main()
