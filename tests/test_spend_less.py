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
from orchestra.gates import make_size_guard  # noqa: E402
from orchestra.orchestrator import Orchestrator  # noqa: E402
from tests.test_orchestra import Base, FakeRunner, done, sh  # noqa: E402
from tests.test_keep_good_work import sess  # noqa: E402
from tests.test_run_without_you import TriageRunner  # noqa: E402


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
                 "validator": lambda: r.validate(cfg, task, self.root, "diff", [{"kind": "issue", "text": "x"}])}
        for role, call in calls.items():
            asyncio.run(call())
            self.assertEqual(r.options.env.get("FORCE_PROMPT_CACHING_5M"), "1", role)
        self.assertEqual(r.options.env.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"), "1")


def lines(n):
    return "".join(f"x{i} = {i}\n" for i in range(n))


class OversizeStopsEarly(Base):
    def guard(self, limit=100, exclude=()):
        base = sh("git rev-parse HEAD", self.repo).strip()
        hook = make_size_guard(self.repo, base, limit, list(exclude))
        return lambda: asyncio.run(hook({}, None, None)).get("hookSpecificOutput", {}).get("additionalContext")

    def test_size_check_warns_near_and_over_the_limit_once_each(self):
        check = self.guard(limit=100, exclude=["*.lock"])
        (self.repo / "big.lock").write_text(lines(500))          # excluded, like the gate
        (self.repo / "new.py").write_text(lines(50))             # untracked new file counts
        self.assertIsNone(check())
        (self.repo / "calc.py").write_text(lines(35))            # tracked edit: 35 added + 2 removed
        msg = check()
        self.assertIn("near the 100-line limit", msg)
        self.assertIsNone(check())                               # not repeated
        (self.repo / "more.py").write_text(lines(30))
        msg = check()
        self.assertIn("117 changed lines", msg)
        self.assertIn('report status "blocked"', msg)
        self.assertIsNone(check())
        untracked = {l[3:] for l in sh("git status --porcelain", self.repo).splitlines() if l.startswith("??")}
        self.assertTrue({"big.lock", "new.py", "more.py"} <= untracked, "worktree index untouched")
        self.assertEqual(sh("git diff --cached --name-only", self.repo), "")

    def test_size_check_never_breaks_the_worker(self):
        hook = make_size_guard(self.repo, "not-a-commit", 10, [])
        self.assertEqual(asyncio.run(hook({}, None, None)), {})

    def test_worker_sessions_get_the_size_check(self):
        r, task = CapturingRunner(), Task(id="T1", title="t", spec="s", base_commit="abc")
        asyncio.run(r.work(self.cfg, task, self.repo))
        (matcher,) = r.options.hooks["PostToolUse"]
        self.assertIn("Write", matcher.matcher)
        self.assertIn("Bash", matcher.matcher)
        asyncio.run(r.write_tests(self.cfg, task, self.repo))
        self.assertIn("PostToolUse", r.options.hooks)  # test writers have their own budget (wave-2-fixes 6)

    def run_oversized(self, n_lines):
        self.cfg.max_diff_lines = 100
        self.add("T01", ["big.py"])
        runner = TriageRunner({"T01": [sess(done(big__py=lines(n_lines)))]},
                              triage={"T01": {"action": "human", "note_for_human": "split it"}})
        self.run_orch(runner)
        return runner

    def test_far_oversized_diff_goes_straight_to_triage(self):
        runner = self.run_oversized(300)                         # 3x the limit
        self.assertEqual(runner.calls["T01"], 1, "no trim round, no second attempt")
        self.assertEqual(runner.triage_calls.get("T01"), 1)
        t = self.store.get("T01")
        self.assertEqual((t.status, t.attempts), ("blocked", 1))  # triage said a human must decide
        self.assertTrue(any("must be split" in f for f in t.feedback))

    def test_oversized_diff_without_triage_retries_as_before(self):
        self.cfg.autonomy.triage = False
        runner = self.run_oversized(300)
        self.assertEqual(runner.calls["T01"], 3)
        self.assertEqual(runner.resumes["T01"], [None, None, None], "no trim rounds")
        self.assertEqual(self.store.get("T01").status, "blocked")

    def test_slightly_oversized_diff_still_gets_a_trim_round(self):
        runner = self.run_oversized(120)                         # 1.2x the limit
        self.assertGreaterEqual(runner.calls["T01"], 2)
        self.assertIn("failed automated checks", runner.resumes["T01"][1] or "")


class OneValidatorPerReview(Base):
    VERDICT = {"verdict": "reject", "fixable": True, "feedback": "f", "scope_creep": [],
               "criteria": [{"criterion": "C1", "met": False, "evidence": "e"}],
               "issues": [{"severity": "blocker", "file": "a.py", "issue": "I1"},
                          {"severity": "major", "file": "b.py", "issue": "I2"}]}

    def validate_with(self, data=None, **kw):
        class V(FakeRunner):
            async def validate(inner, cfg, task, wt, diff, findings, env=None):
                inner.seen = findings
                return AgentResult(data or {}, 0.05, **kw)
        runner = V({})
        orch = Orchestrator(self.cfg, self.store, runner, log=self.logs.append)
        self.add("V", ["a.py"])
        out = asyncio.run(orch._validate(self.store.get("V"), self.repo, "diff", self.VERDICT, {}))
        return out, runner

    def test_all_findings_go_to_one_session_and_verdicts_map_back(self):
        out, runner = self.validate_with({"findings": [
            {"index": 2, "confirmed": False, "evidence": "I1 can't happen"},
            {"index": 1, "confirmed": True, "evidence": "holds"},
            {"index": 2, "confirmed": True, "evidence": "a second verdict is ignored"}]})
        self.assertEqual([f["text"] for f in runner.seen], ["C1", "I1", "I2"])
        self.assertEqual([i["issue"] for i in out["issues"]], ["I2"])   # 3 had no verdict: it stands
        self.assertFalse(out["criteria"][0]["met"])
        self.assertTrue(any("refuted: I1" in e["detail"] for e in self.store.events("V")))

    def test_a_failed_validator_refutes_nothing(self):
        out, _ = self.validate_with(ok=False, error="boom")
        self.assertEqual(len(out["issues"]), 2)
        self.assertTrue(any("validator failed" in line for line in self.logs))

    def test_all_refuted_turns_into_an_approval(self):
        out, _ = self.validate_with({"findings": [{"index": i, "confirmed": False, "evidence": "no"}
                                                  for i in (1, 2, 3)]})
        self.assertEqual(out["verdict"], "approve")

    def test_one_call_per_review_in_a_run(self):
        self.add("R", ["r.py"])
        reject = AgentResult({**self.VERDICT, "issues": self.VERDICT["issues"]}, 0.1)
        runner = FakeRunner({"R": [sess(done(**{"r.py": "x = 1\n"}))]}, review={"R": [reject, "approve"]})
        self.run_orch(runner)
        self.assertEqual(runner.validate_calls, 1)
        self.assertEqual(runner.validations["R"], ["C1", "I1", "I2"])

    def test_prompt_puts_card_and_diff_before_the_findings(self):
        r = CapturingRunner()
        findings = [{"kind": "issue", "text": "I1", "detail": "x"}, {"kind": "scope", "text": "S1"}]
        asyncio.run(r.validate(self.cfg, Task(id="T1", title="t", spec="s"), self.repo, "DIFFTEXT", findings))
        p = r.prompt
        self.assertLess(p.index("# Task T1"), p.index("DIFFTEXT"))
        self.assertLess(p.index("DIFFTEXT"), p.index("### Finding 1"))
        self.assertLess(p.index("### Finding 1"), p.index("### Finding 2"))
        self.assertEqual(r.options.max_turns, 35)
        from orchestra.usage import classify
        self.assertEqual(classify(p), "validator")  # orchestra usage still recognizes the session


class SessionRecords(Base):
    def ids(self, runner_cls=FakeRunner):
        class R(runner_cls):
            async def review(inner, *a, **kw):
                r = await super().review(*a, **kw)
                r.session_id, r.model = f"rev{inner.review_calls[a[1].id]}", "claude-opus-5-5"
                return r
        return R

    def test_store_keeps_one_row_per_session(self):
        self.add("C", ["c.py"])
        self.store.add_cost("C", 0.3, 100, "s1", role="worker", model="m", attempt=1, started_at=10.0,
                            end_reason="gate_failed")
        self.store.add_cost("C", 0.5, 150, "s1", role="worker", attempt=1, end_reason="done")
        (row,) = self.store.sessions("C")
        self.assertEqual((row["role"], row["model"], row["runs"], row["started_at"], row["end_reason"]),
                         ("worker", "m", 2, 10.0, "done"))
        self.assertAlmostEqual(row["cost_usd"], 0.5)
        self.store.end_session("s1", "review_rejected")
        self.assertEqual(self.store.sessions("C")[0]["end_reason"], "review_rejected")

    def test_old_sessions_table_gets_the_new_columns(self):
        import sqlite3
        db = self.tmp / "old.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, task_id TEXT, "
                     "cost_usd REAL NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0)")
        conn.commit()
        conn.close()
        cols = {r[1] for r in Store(db).conn.execute("PRAGMA table_info(sessions)")}
        self.assertTrue({"role", "model", "end_reason", "runs", "started_at", "ended_at"} <= cols)

    def test_a_run_records_roles_runs_and_end_reasons(self):
        self.add("T01", ["calc.py", "test_calc.py"])
        bad = done(**{"calc.py": "def add(a, b):\n    return a - b\n"})
        good = done(**{"calc.py": "def add(a, b):\n    return a + b  # fixed\n"})
        step = lambda wt, n, task: (bad if n == 0 else good)(wt, n, task)  # noqa: E731
        self.run_orch(self.ids()({"T01": [running_total(step, "w1", [0.3, 0.5])]}))
        rows = {r["session_id"]: r for r in self.store.sessions("T01")}
        self.assertEqual((rows["w1"]["role"], rows["w1"]["runs"], rows["w1"]["end_reason"]), ("worker", 2, "done"))
        self.assertEqual((rows["rev1"]["role"], rows["rev1"]["model"], rows["rev1"]["end_reason"]),
                         ("reviewer", "claude-opus-5-5", "done"))
        self.assertEqual(rows["w1"]["attempt"], 1)

    def test_rejected_and_interrupted_sessions_say_so(self):
        self.cfg.max_attempts, self.cfg.review.fix_rounds, self.cfg.review.validate = 1, 0, False
        self.add("R", ["r.py"])
        self.run_orch(self.ids()({"R": [sess(done(**{"r.py": "x = 1\n"}), "w9")]}, review={"R": ["reject"]}))
        self.assertEqual({r["session_id"]: r["end_reason"] for r in self.store.sessions("R")}["w9"], "review_rejected")
        self.add("I", ["i.py"])
        self.store.add_cost("I", 0.1, 1, "w10", role="worker", end_reason="done")
        self.store.update("I", status="running", attempts=1, session_id="w10")
        orch = Orchestrator(self.cfg, self.store, FakeRunner({}), log=self.logs.append)
        asyncio.run(orch._recover())
        self.assertEqual(self.store.sessions("I")[0]["end_reason"], "interrupted")

    def test_a_blocked_report_ends_the_session_as_blocked(self):
        from tests.test_orchestra import blocked
        self.add("B", ["b.py"])
        self.run_orch(FakeRunner({"B": [sess(blocked, "wb")]}))
        self.assertEqual(self.store.sessions("B")[0]["end_reason"], "blocked")

    def test_main_model_and_usage_prefers_recorded_roles(self):
        from orchestra.agents import _main_model
        self.assertEqual(_main_model({"claude-haiku-5-5": {"inputTokens": 10},
                                      "claude-opus-5-5": {"inputTokens": 5, "cacheReadInputTokens": 900}}),
                         "claude-opus-5-5")
        from tests.test_usage import CARD, enqueue, turn, write
        root = self.tmp / "export"
        write(root / "agent-transcripts" / "misc" / "abc.jsonl", [*enqueue(CARD), *turn("m1")])
        (root / "state").mkdir()
        (root / "state" / "tasks.json").write_text("[]")
        (root / "state" / "sessions.json").write_text('[{"session_id": "abc", "task_id": "T02", "role": "reviewer"}]')
        from orchestra import usage
        (sess_,) = usage.build(root).sessions
        self.assertEqual((sess_.role, sess_.card), ("reviewer", "T02"))

    def test_status_lists_a_cards_sessions(self):
        import io
        from contextlib import redirect_stdout
        from orchestra.cli import main
        self.add("S", ["s.py"])
        self.store.add_cost("S", 0.42, 10, "abcdef123", role="validator", model="claude-opus-5-5", end_reason="done")
        out = io.StringIO()
        with redirect_stdout(out):
            main(["--repo", str(self.repo), "status", "S"])
        self.assertRegex(out.getvalue(), r"validator\s+opus-5-5\s+abcdef12 .* 1 run\(s\)  \$0.42  done")


SHELLS = [sh_ for sh_ in ("/bin/bash", "/bin/zsh") if Path(sh_).exists()]


class BashOutputCap(unittest.TestCase):
    def run_sh(self, script, shell, cwd):
        import subprocess
        return subprocess.run([shell, "-c", script], cwd=cwd, capture_output=True, text=True)

    def test_long_output_keeps_head_and_tail_with_a_note(self):
        from orchestra.guards import cap_command
        noisy = ("python3 -c \"print('collected 40 items'); print('x' * 30000); "
                 "print('FAILED tests/test_a.py::test_fee - assert 9 == 10'); print('=== 1 failed, 39 passed ===')\"; "
                 "(exit 1)")
        for shell in SHELLS:
            with tempfile.TemporaryDirectory() as d:
                r = self.run_sh(cap_command(noisy, 8000), shell, d)
                self.assertEqual(r.returncode, 1, shell)
                self.assertTrue(r.stdout.startswith("collected 40 items"), shell)
                self.assertIn("FAILED tests/test_a.py::test_fee", r.stdout)
                self.assertIn("=== 1 failed, 39 passed ===", r.stdout)
                self.assertRegex(r.stdout, r"\[truncated \d+ chars: the middle of this output was cut")
                self.assertLess(len(r.stdout), 8400)
                self.assertEqual(list(Path(d).iterdir()), [])

    def test_short_output_cd_heredoc_and_exit_behave_as_before(self):
        from orchestra.guards import cap_command
        for shell in SHELLS:
            with tempfile.TemporaryDirectory() as d:
                (Path(d) / "sub").mkdir()
                r = self.run_sh(cap_command("echo hi >&2; cd sub", 8000) + "\npwd", shell, d)
                self.assertEqual(r.stdout.splitlines()[0], "hi", shell)     # stderr shown too
                self.assertTrue(r.stdout.strip().endswith("/sub"), shell)   # cd sticks: same shell
                r = self.run_sh(cap_command("cat <<'EOF'\nline $HOME\nEOF", 8000), shell, d)
                self.assertEqual(r.stdout, "line $HOME\n", shell)
                r = self.run_sh(cap_command("echo before; exit 4", 8000), shell, d)
                self.assertEqual((r.returncode, r.stdout), (4, "before\n"), shell)
                r = self.run_sh(cap_command("false", 8000) + "\necho rc=$?", shell, d)
                self.assertEqual(r.stdout, "rc=1\n", shell)

    def test_a_killed_command_still_shows_its_output_so_far(self):
        import os
        import signal
        import subprocess
        import time
        from orchestra.guards import cap_command
        for shell in SHELLS:
            with tempfile.TemporaryDirectory() as d:
                proc = subprocess.Popen([shell, "-c", cap_command("echo partial; sleep 30", 8000)], cwd=d,
                                        stdout=subprocess.PIPE, text=True, start_new_session=True,
                                        env={**os.environ, "TMPDIR": d})
                time.sleep(1)
                os.killpg(proc.pid, signal.SIGTERM)  # what a tool timeout does to the command's tree
                out, _ = proc.communicate(timeout=10)
                self.assertIn("partial", out, shell)
                self.assertEqual(proc.returncode, 143, shell)
                self.assertEqual([f for f in Path(d).iterdir() if f.name.startswith("orchestra-out")], [])

    def test_hook_rewrites_foreground_bash_only(self):
        from orchestra.guards import make_output_cap
        hook = make_output_cap(8000)
        out = asyncio.run(hook({"tool_name": "Bash", "tool_input": {"command": "pytest", "timeout": 5}}, None, None))
        new = out["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(new["timeout"], 5)
        self.assertIn("pytest", new["command"])
        self.assertIn("__oc_show", new["command"])
        self.assertEqual(asyncio.run(hook({"tool_name": "Bash", "tool_input": {
            "command": "npm run dev", "run_in_background": True}}, None, None)), {})
        self.assertEqual(asyncio.run(hook({"tool_name": "Read", "tool_input": {"file_path": "x"}}, None, None)), {})
        self.assertEqual(asyncio.run(hook({"tool_name": "Bash", "tool_input": {"command": new["command"]}},
                                          None, None)), {})               # never wrapped twice

    def test_workers_get_it_by_default_and_it_can_be_turned_off(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, r, task = Config(repo=Path(d)), CapturingRunner(), Task(id="T1", title="t", spec="s")
            self.assertEqual(cfg.agents.bash_output_max_chars, 8000)
            asyncio.run(r.work(cfg, task, Path(d)))
            self.assertEqual([m.matcher for m in r.options.hooks["PreToolUse"]], [None, "Bash"])
            asyncio.run(r.review(cfg, task, Path(d), "diff", {}, "log"))
            self.assertEqual([m.matcher for m in r.options.hooks["PreToolUse"]], [None, "Bash"])  # reviewers too (wave-2 9b)
            cfg.agents.bash_output_max_chars = 0
            asyncio.run(r.work(cfg, task, Path(d)))
            self.assertEqual(len(r.options.hooks["PreToolUse"]), 1)
            (Path(d) / "orchestra.toml").write_text("[agents]\nbash_output_max_chars = 12000\n")
            self.assertEqual(Config.load(Path(d)).agents.bash_output_max_chars, 12000)


class StablePromptOrder(unittest.TestCase):
    def test_reviewer_note_comes_after_the_diff(self):
        with tempfile.TemporaryDirectory() as d:
            r, task = CapturingRunner(), Task(id="T1", title="t", spec="s")
            asyncio.run(r.review(Config(repo=Path(d)), task, Path(d), "DIFFTEXT", {"summary": "s"}, "GATES",
                                 impact="IMPACT", note="This is a revision."))
            p = r.prompt
            order = [p.index(x) for x in ("# Task T1", "## Worker's report", "GATES", "IMPACT", "DIFFTEXT",
                                          "## Context for this review")]
            self.assertEqual(order, sorted(order))
            first = r.prompt
            asyncio.run(r.review(Config(repo=Path(d)), task, Path(d), "DIFFTEXT", {"summary": "s"}, "GATES",
                                 impact="IMPACT", note="Resolved conflicts in a.py."))
            shared = first.index("## Context for this review")
            self.assertEqual(r.prompt[:shared], first[:shared])  # only the note differs, at the end


if __name__ == "__main__":
    unittest.main()
