"""Tests for the stop-the-leaks change: run lease, recovery, verdict check, cross-plan
serialization, sandbox settings, guard hardening, checks on the card, Stop-hook pre-check."""
import asyncio
import json
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestra.agents import (agent_env, check_verdict, gate_commands, sandbox_settings,  # noqa: E402
                              task_card)
from orchestra.config import Config, Gate, Sandbox, detect_gates  # noqa: E402
from orchestra.gates import make_stop_gate  # noqa: E402
from orchestra.guards import check, secret_path  # noqa: E402
from orchestra.orchestrator import INTERRUPTED_NOTE, Orchestrator  # noqa: E402
from orchestra.planning import build_tasks, plan_fingerprint  # noqa: E402
from orchestra.store import RunLocked, Store, Task  # noqa: E402
from tests.test_orchestra import Base, FakeRunner, done, sh  # noqa: E402


class Lease(Base):
    def test_one_live_run_and_takeover_of_stale_leases(self):
        token = self.store.acquire_lease()
        with self.assertRaises(RunLocked):
            self.store.acquire_lease()
        self.assertTrue(self.store.heartbeat(token))
        self.store.release_lease(token)
        self.assertIsNone(self.store.live_lease())
        # stale heartbeat -> taken over
        self.store.set_meta("run_lease", {"token": "x", "pid": 1, "host": "elsewhere", "heartbeat": time.time() - 600})
        self.store.release_lease(self.store.acquire_lease())
        # dead pid on this host -> taken over even with a fresh heartbeat
        self.store.set_meta("run_lease", {"token": "y", "pid": 999_999_9, "host": socket.gethostname(),
                                          "heartbeat": time.time()})
        self.store.acquire_lease()

    def test_second_run_refuses_and_changes_nothing(self):
        self.add("A", ["a.py"])
        self.store.acquire_lease()  # this process holds it, so it's live
        with self.assertRaises(RunLocked):
            self.run_orch(FakeRunner({"A": [done(**{"a.py": "x=1\n"})]}))
        t = self.store.get("A")
        self.assertEqual((t.status, t.attempts), ("pending", 0))

    def test_lease_released_after_run(self):
        self.add("A", ["a.py"])
        self.run_orch(FakeRunner({"A": [done(**{"a.py": "x=1\n"})]}))
        self.assertIsNone(self.store.get_meta("run_lease"))


class Recovery(Base):
    def _interrupted(self, tid, status, with_change=False):
        self.add(tid, [f"{tid.lower()}.py"])
        sh("git branch -f orchestra/integration main", self.repo)
        wt = self.cfg.worktree_root / tid
        sh(f"git worktree add -q -B orchestra/task/{tid} {wt} orchestra/integration", self.repo)
        if with_change:
            (wt / f"{tid.lower()}.py").write_text("x=1\n")
            sh("git add -A && git commit -qm work", wt)
        base = sh("git rev-parse orchestra/integration", self.repo).strip()
        self.store.update(tid, status=status, attempts=1, worktree=str(wt), branch=f"orchestra/task/{tid}",
                          session_id="sess-1", base_commit=base)
        return wt

    def test_running_card_resumes_its_session(self):
        self._interrupted("R", "running")
        runner = FakeRunner({"R": [done(**{"r.py": "x=1\n"})]})
        self.run_orch(runner)
        self.assertEqual(runner.resumes["R"][0], INTERRUPTED_NOTE)
        t = self.store.get("R")
        self.assertEqual((t.status, t.attempts), ("merged", 1), "recovery gives the attempt back")

    def test_review_card_resumes_at_review(self):
        self._interrupted("V", "review", with_change=True)
        hd = self.cfg.state_path / "handoffs"
        hd.mkdir(parents=True, exist_ok=True)
        (hd / "V.pending_report.json").write_text(json.dumps(
            {"report": {"status": "done", "summary": "s", "files_changed": ["v.py"], "tests": "", "blocker": ""},
             "gate_log": "- test: pass"}))
        runner = FakeRunner({"V": [done(**{"v.py": "x=2\n"})]})
        self.run_orch(runner)
        self.assertNotIn("V", runner.calls, "worker not re-run")
        self.assertEqual(runner.review_calls["V"], 1)
        self.assertEqual(self.store.get("V").status, "merged")

    def test_interrupted_note_restates_decisions(self):
        from orchestra.orchestrator import _interrupted_note
        t = Task(id="X", title="x", spec="", decisions=[{"question": "Fee?", "answer": "12h"}])
        self.assertIn("Q: Fee? A: 12h", _interrupted_note(t))
        self.assertEqual(_interrupted_note(Task(id="Y", title="y", spec="")), INTERRUPTED_NOTE)

    def test_card_without_worktree_restarts(self):
        self.add("S", ["s.py"])
        self.store.update("S", status="running", attempts=1, session_id="sess", worktree="/nonexistent")
        runner = FakeRunner({"S": [done(**{"s.py": "x=1\n"})]})
        self.run_orch(runner)
        self.assertEqual(runner.resumes["S"][0], None, "cold start from the card")
        self.assertEqual(self.store.get("S").status, "merged")


class Units(unittest.TestCase):
    def test_check_verdict(self):
        ok = {"verdict": "approve", "criteria": [{"criterion": "c", "met": True, "evidence": ""}],
              "scope_creep": [], "issues": [{"severity": "minor", "file": "f", "issue": "nit"}]}
        self.assertEqual(check_verdict(ok), (True, []))
        self.assertFalse(check_verdict({**ok, "issues": [{"severity": "major", "file": "f", "issue": "bug"}]})[0])
        self.assertFalse(check_verdict({**ok, "scope_creep": ["pdf export"]})[0])
        self.assertFalse(check_verdict({**ok, "verdict": "reject"})[0])

    def test_new_cards_wait_for_open_cards_from_earlier_plans(self):
        old = Task(id="T01", title="old", spec="", files_scope=["src/api/**"], status="running")
        unrelated = Task(id="T02", title="other", spec="", files_scope=["web/**"])
        plan = {"tasks": [{"key": "n", "title": "N", "spec": "", "acceptance_criteria": [], "non_goals": [],
                           "files_scope": ["src/api/views.py"], "depends_on": [], "complexity": "small"}]}
        tasks, warnings = build_tasks(plan, start=3, existing=[old, unrelated])
        self.assertEqual(tasks[0].depends_on, ["T01"])
        self.assertTrue(any("already queued" in w for w in warnings))
        self.assertEqual(plan_fingerprint(plan), plan_fingerprint(json.loads(json.dumps(plan))))

    def test_sandbox_settings_and_env(self):
        cfg = Config(repo=Path("/tmp/x"))
        s = sandbox_settings(cfg)
        self.assertTrue(s["enabled"] and s["failIfUnavailable"])
        self.assertFalse(s["allowUnsandboxedCommands"])
        self.assertIn("registry.npmjs.org", s["network"]["allowedDomains"])
        self.assertIn("~/.ssh", s["filesystem"]["denyRead"])
        self.assertTrue(s["network"]["allowLocalBinding"])
        cfg.sandbox = Sandbox(enabled=False)
        self.assertIsNone(sandbox_settings(cfg))
        self.assertEqual(agent_env(), {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})

    def test_guard_allows_normal_work(self):
        root = Path("/tmp/wt")
        for cmd in ["git status", "git diff HEAD", "git log --oneline -5", "git config --get user.name",
                    "git branch -a", "npm test 2>/dev/null", "echo hi > notes.txt", "pytest > /tmp/out.log",
                    "cat .env.example", "cp .env.example .env", "cd src && ls", "ls | tee $TMPDIR/x"]:
            self.assertIsNone(check("Bash", {"command": cmd}, root, "worker"), cmd)

    def test_secret_reads_blocked_for_agents_not_humans(self):
        root = Path("/tmp/wt")
        self.assertTrue(secret_path(".env") and secret_path("config/.env.production"))
        self.assertFalse(secret_path(".env.example") or secret_path("src/environment.ts"))
        self.assertTrue(secret_path("~/.ssh/id_ed25519") and secret_path("/Users/me/.aws/credentials"))
        self.assertIsNotNone(check("Read", {"file_path": "/tmp/wt/.env"}, root, "worker"))
        self.assertIsNotNone(check("Glob", {"pattern": "**/.env"}, root, "readonly"))
        self.assertIsNone(check("Read", {"file_path": "/tmp/wt/.env.example"}, root, "readonly"))
        self.assertIsNone(check("Read", {"file_path": "/tmp/wt/.env"}, root, "interactive"))
        self.assertIsNone(check("Write", {"file_path": "/etc/x", "content": "y"}, root, "readonly"))

    def test_card_lists_checks_and_fast_gates_detected(self):
        cfg = Config(repo=Path("/tmp/x"), gates=[Gate("lint", "ruff check ."), Gate("test", "pytest -q")])
        card = task_card(Task(id="T1", title="t", spec="s"), gate_commands(cfg))
        self.assertIn("## Checks the orchestrator will run", card)
        self.assertIn("`ruff check .`", card)
        tmp = Path(tempfile.mkdtemp())
        (tmp / "pyproject.toml").write_text("[project]\nname='x'\n")
        _, gates = detect_gates(tmp)
        self.assertEqual({g.name: g.fast for g in gates}, {"lint": True, "test": False})


class StopGate(unittest.TestCase):
    def setUp(self):
        self.wt = Path(tempfile.mkdtemp())
        self.transcript = self.wt / "t.jsonl"

    def _report(self, status):
        entry = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "StructuredOutput",
             "input": {"status": status, "summary": "", "files_changed": [], "tests": "", "blocker": ""}}]}}
        self.transcript.write_text(json.dumps({"type": "user"}) + "\n" + json.dumps(entry) + "\n")
        return {"hook_event_name": "Stop", "stop_hook_active": False, "transcript_path": str(self.transcript)}

    def call(self, hook, data):
        return asyncio.run(hook(data, None, None))

    def test_blocks_once_on_failing_fast_gate(self):
        hook = make_stop_gate([Gate("lint", "echo 'E501 line too long'; exit 1", fast=True)], self.wt)
        out = self.call(hook, self._report("done"))
        self.assertEqual(out["decision"], "block")
        self.assertIn("E501", out["reason"])
        self.assertEqual(self.call(hook, self._report("done")), {}, "at most one block per session")

    def test_lets_go_when_not_done_passing_or_unknown(self):
        failing = [Gate("lint", "exit 1", fast=True)]
        self.assertEqual(self.call(make_stop_gate(failing, self.wt), self._report("needs_decision")), {})
        self.assertEqual(self.call(make_stop_gate([Gate("lint", "true", fast=True)], self.wt),
                                   self._report("done")), {})
        self.assertEqual(self.call(make_stop_gate(failing, self.wt), {"transcript_path": "/nope"}), {})
        msg = {"last_assistant_message": json.dumps({"status": "done", "files_changed": []})}
        self.assertEqual(self.call(make_stop_gate(failing, self.wt), msg)["decision"], "block")


if __name__ == "__main__":
    unittest.main()
