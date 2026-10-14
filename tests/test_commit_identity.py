"""commit-identity: who Orchestra commits as, the co-author trailer, and running the project's
commit hooks on agent work."""
import asyncio
import io
import os
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestra.agents import AgentResult  # noqa: E402
from orchestra.cli import main  # noqa: E402
from orchestra.config import CONFIG_TEMPLATE, Config, GitSettings  # noqa: E402
from orchestra.gitops import ORCHESTRA_IDENTITY, Git, HookRejected, IdentityError, resolve_identity  # noqa: E402
from orchestra.orchestrator import Orchestrator  # noqa: E402
from tests.test_keep_good_work import sess  # noqa: E402
from tests.test_orchestra import Base, FakeRunner, done, sh  # noqa: E402


def landed(repo, fmt="%an <%ae>|%cn <%ce>"):
    """Author|committer (or another format) of the commit the card landed on integration."""
    return sh(f"git log -1 --format='{fmt}' orchestra/integration", repo).strip()


def isolated_git_env(home: Path) -> dict:
    """No global or system git config: only the repo's own config counts."""
    return {"HOME": str(home), "GIT_CONFIG_GLOBAL": str(home / "no-gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
            "XDG_CONFIG_HOME": str(home / "xdg")}


class AuthorModes(Base):
    def run_card(self, **git):
        self.cfg.git = GitSettings(**git)
        self.add("T01", ["calc.py", "test_calc.py"])
        self.run_orch(FakeRunner({"T01": [done(**{"calc.py": "def add(a, b):\n    return b + a\n"})]}))
        self.assertEqual(self.store.get("T01").status, "merged", "\n".join(self.logs))
        return landed(self.repo)

    def test_user_is_the_default_and_reads_git_config(self):
        self.assertEqual(GitSettings().author, "user")
        self.assertEqual(self.run_card(), "t <t@t>|t <t@t>")  # the test repo's git config

    def test_orchestra_keeps_the_old_identity(self):
        self.assertEqual(self.run_card(author="orchestra"),
                         "Orchestra <orchestra@local>|Orchestra <orchestra@local>")

    def test_literal_name_and_email(self):
        self.assertEqual(self.run_card(author="Ada Lovelace <ada@example.com>"),
                         "Ada Lovelace <ada@example.com>|Ada Lovelace <ada@example.com>")

    def test_committer_matches_author_despite_stray_environment(self):
        with mock.patch.dict(os.environ, {"GIT_COMMITTER_EMAIL": "stray@example.com",
                                          "GIT_AUTHOR_NAME": "Stray"}):
            self.assertEqual(self.run_card(), "t <t@t>|t <t@t>")

    def test_invalid_values_are_config_errors(self):
        for bad in ({"author": "Ada"}, {"author": "Ada <not-an-email>"}, {"co_author": "pair@example.com"}):
            with self.assertRaises(ValueError, msg=bad):
                GitSettings(**bad)
        (self.repo / "orchestra.toml").write_text('[git]\nauthor = "nobody"\n')
        with self.assertRaises(ValueError):
            Config.load(self.repo)
        (self.repo / "orchestra.toml").write_text('[git]\nauthor = "Ada <ada@example.com>"\nrun_hooks = true\n')
        g = Config.load(self.repo).git
        self.assertEqual((g.author, g.run_hooks, g.co_author), ("Ada <ada@example.com>", True, ""))
        self.assertEqual(resolve_identity(self.repo, "orchestra"), ORCHESTRA_IDENTITY)


class MissingIdentity(Base):
    def setUp(self):
        super().setUp()
        sh("git config --unset user.email", self.repo)
        self.env = mock.patch.dict(os.environ, isolated_git_env(self.tmp))
        self.env.start()

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    def test_resolution_names_what_is_missing(self):
        with self.assertRaises(IdentityError) as e:
            resolve_identity(self.repo, "user")
        msg = str(e.exception)
        self.assertIn("git config user.email is not set", msg)
        self.assertIn('git config --global user.email "you@example.com"', msg)
        self.assertIn('[git] author = "Name <email>"', msg)
        self.assertNotIn("user.name", msg.split("Set it")[0])  # the name is set; only the email is named

    def test_run_stops_before_taking_the_queue(self):
        self.add("T01", ["calc.py"])
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as e:
            main(["--repo", str(self.repo), "run"])
        self.assertIn("user.email is not set", str(e.exception.code))
        self.assertIsNone(self.store.get_meta("run_lease"))
        self.assertEqual((self.store.get("T01").status, self.store.get("T01").attempts), ("pending", 0))
        orch = Orchestrator(self.cfg, self.store, FakeRunner({}), log=self.logs.append)
        with self.assertRaises(IdentityError):
            asyncio.run(orch.run())
        self.assertIsNone(self.store.get_meta("run_lease"))
        self.assertEqual(self.store.get("T01").attempts, 0)

    def test_other_modes_need_no_git_config(self):
        self.assertEqual(str(resolve_identity(self.repo, "Ada <ada@example.com>")), "Ada <ada@example.com>")


class CoAuthor(Base):
    def trailers(self, co_author, author="user"):
        self.cfg.git = GitSettings(author=author, co_author=co_author)
        self.add("T01", ["calc.py", "test_calc.py"])
        self.run_orch(FakeRunner({"T01": [done(**{"calc.py": "def add(a, b):\n    return b + a\n"})]}))
        self.assertEqual(self.store.get("T01").status, "merged", "\n".join(self.logs))
        return [l for l in landed(self.repo, "%B").splitlines() if l.startswith("Co-authored-by:")]

    def test_trailer_on_the_landed_commit(self):
        self.assertEqual(self.trailers("Pair Bot <pair@example.com>"), ["Co-authored-by: Pair Bot <pair@example.com>"])

    def test_no_trailer_when_empty(self):
        self.assertEqual(self.trailers(""), [])

    def test_no_trailer_naming_the_author_again(self):
        self.assertEqual(self.trailers("t <t@t>"), [])


HOOK = """#!/bin/sh
echo run >> "{log}"
if git diff --cached | grep -q '^+.*BAD'; then
  echo "lint: BAD is not allowed (found in the staged change)"
  {fix}
  exit 1
fi
exit 0
"""


class Hooks(Base):
    def install_hook(self, fix=False):
        self.hook_log = self.tmp / "hook.log"
        hook = self.repo / ".git" / "hooks" / "pre-commit"  # worktrees share the repo's hooks
        fix_cmd = "for f in $(git diff --cached --name-only); do sed -i.bak 's/BAD/GOOD/' \"$f\"; rm -f \"$f.bak\"; done" \
            if fix else ""
        hook.write_text(HOOK.format(log=self.hook_log, fix=fix_cmd))
        hook.chmod(0o755)

    def hook_runs(self):
        return len(self.hook_log.read_text().splitlines()) if self.hook_log.exists() else 0

    def test_off_by_default_hooks_are_skipped(self):
        self.install_hook()
        self.add("H", ["h.py"])
        self.run_orch(FakeRunner({"H": [done(**{"h.py": "BAD = 1\n"})]}))
        self.assertEqual(self.store.get("H").status, "merged", "\n".join(self.logs))
        self.assertEqual(self.hook_runs(), 0)

    def test_on_a_failing_hook_goes_back_to_the_worker_like_a_gate(self):
        self.install_hook()
        self.cfg.git = GitSettings(run_hooks=True)
        self.add("H", ["h.py"])
        runner = FakeRunner({"H": [sess(done(**{"h.py": "BAD = 1\n"})), sess(done(**{"h.py": "OK = 1\n"}))]})
        self.run_orch(runner)
        t = self.store.get("H")
        self.assertEqual((t.status, t.attempts), ("merged", 1), "\n".join(self.logs))
        first, second = runner.resumes["H"]
        self.assertIsNone(first)
        self.assertIn("Your last change failed automated checks.", second)  # same session, gate round
        self.assertIn("[gate:git hooks] FAILED", second)
        self.assertIn("lint: BAD is not allowed", second)
        self.assertTrue(any("gates failed (round 1): git hooks" in line for line in self.logs))
        self.assertEqual(self.hook_runs(), 2)  # each worker commit; not the squash or the rebase

    def test_fixes_made_by_a_failing_hook_stay_for_the_next_round(self):
        self.install_hook(fix=True)  # like a formatter: rewrites the file, then fails
        self.cfg.git = GitSettings(run_hooks=True)
        self.add("H", ["h.py"])
        seen = []

        def look(wt, n, task):
            seen.append((wt / "h.py").read_text())
            return AgentResult({"status": "done", "summary": "kept the hook's fix", "files_changed": ["h.py"],
                                "tests": "ok", "blocker": ""}, 0.1)
        runner = FakeRunner({"H": [sess(done(**{"h.py": "BAD = 1\n"})), sess(look)]})
        self.run_orch(runner)
        self.assertEqual(seen, ["GOOD = 1\n"])
        self.assertEqual(self.store.get("H").status, "merged", "\n".join(self.logs))
        self.assertEqual(sh("git show orchestra/integration:h.py", self.repo), "GOOD = 1\n")

    def test_a_hook_that_keeps_failing_uses_up_the_rounds(self):
        self.install_hook()
        self.cfg.git = GitSettings(run_hooks=True)
        self.cfg.max_attempts = 1
        self.add("H", ["h.py"])
        runner = FakeRunner({"H": [sess(done(**{"h.py": "BAD = 1\n"}))]})
        self.run_orch(runner)
        t = self.store.get("H")
        self.assertEqual(t.status, "blocked")
        self.assertEqual(runner.calls["H"], 3)  # first try + FIX_ROUNDS (2) fix rounds
        self.assertIn("Still failing after 2 fix rounds", t.feedback[-1])
        self.assertIn("[gate:git hooks] FAILED", t.feedback[-1])

    def test_mechanical_commits_skip_hooks(self):
        self.install_hook()
        g = Git(self.repo, self.cfg.worktree_root, "main", "orchestra/integration", GitSettings(run_hooks=True))
        base = sh("git rev-parse HEAD", self.repo).strip()
        (self.repo / "a.py").write_text("BAD = 1\n")
        asyncio.run(g.commit_all(self.repo, "mechanical"))             # default: no hooks
        (self.repo / "b.py").write_text("BAD = 2\n")
        asyncio.run(g.commit_all(self.repo, "also mechanical"))
        asyncio.run(g.squash_onto(self.repo, base, "squashed"))        # re-records passed work
        self.assertEqual(self.hook_runs(), 0)
        (self.repo / "c.py").write_text("BAD = 3\n")
        with self.assertRaises(HookRejected) as e:
            asyncio.run(g.commit_all(self.repo, "agent work", hooks=True))
        self.assertIn("BAD is not allowed", e.exception.output)
        self.assertEqual(self.hook_runs(), 1)
        off = Git(self.repo, self.cfg.worktree_root, "main", "orchestra/integration", GitSettings(run_hooks=False))
        asyncio.run(off.commit_all(self.repo, "agent work, hooks off", hooks=True))
        self.assertEqual(self.hook_runs(), 1)

    def test_a_rejected_test_writer_commit_fails_the_attempt_with_the_output(self):
        self.install_hook()
        self.cfg.git = GitSettings(run_hooks=True)
        self.cfg.max_attempts = 1
        self.add("F", ["fee.py", "test_fee.py"], test_first=True)

        def tests(wt, n, task):
            (wt / "test_fee.py").write_text("BAD = 1\n")
            return AgentResult({"status": "done", "test_files": ["test_fee.py"], "covered": [], "assumptions": [],
                                "blocker": ""}, 0.1, session_id="tw")
        runner = FakeRunner({"F": [done(**{"fee.py": "x = 1\n"})]}, tests={"F": [tests]})
        self.run_orch(runner)
        t = self.store.get("F")
        self.assertEqual(t.status, "blocked")
        self.assertNotIn("F", runner.calls)  # the worker never started
        self.assertTrue(any("test writer's commit was rejected by the repository's commit hooks" in f
                            and "BAD is not allowed" in f for f in t.feedback))


class InitTemplate(Base):
    def test_init_writes_the_git_section(self):
        self.assertIn("[git]", CONFIG_TEMPLATE)
        with redirect_stderr(io.StringIO()), mock.patch("sys.stdout", new=io.StringIO()):
            main(["--repo", str(self.repo), "init"])
        text = (self.repo / "orchestra.toml").read_text()
        self.assertIn('author = "user"', text)
        self.assertIn("run_hooks = false", text)
        self.assertIn("Co-authored-by", text)
        g = Config.load(self.repo).git
        self.assertEqual((g.author, g.co_author, g.run_hooks), ("user", "", False))


if __name__ == "__main__":
    unittest.main()
