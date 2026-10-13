"""Set 3 (run-without-you): triage, shipping with CI fix cards, ntfy answers, Linear intake."""
import asyncio
import io
import json
import os
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestra.agents import AgentResult  # noqa: E402
from orchestra.cli import main  # noqa: E402
from orchestra.linear import Linear, LinearError, normalize_id, ticket_goal  # noqa: E402
from orchestra.ntfy import Ntfy, parse_reply, question_tag  # noqa: E402
from orchestra.orchestrator import Orchestrator  # noqa: E402
from orchestra.ship import Shipper  # noqa: E402
from orchestra.store import Store, Task  # noqa: E402
from tests.test_orchestra import Base, FakeRunner, blocked, done, sh  # noqa: E402


def item(key, scope, deps=(), **kw):
    return {"key": key, "title": f"part {key}", "spec": f"do {key}", "acceptance_criteria": [f"{key} works"],
            "non_goals": [], "files_scope": list(scope), "depends_on": list(deps), "complexity": "small", **kw}


class TriageRunner(FakeRunner):
    def __init__(self, script, triage=None, **kw):
        super().__init__(script, **kw)
        self.triage_script = triage or {}
        self.triage_calls, self.triage_contexts = {}, {}

    async def triage(self, cfg, task, context, cwd, graph=None):
        self.triage_calls[task.id] = self.triage_calls.get(task.id, 0) + 1
        self.triage_contexts[task.id] = (context, Path(cwd).name)
        data = self.triage_script.get(task.id, {"action": "human", "reason": "unclear",
                                                "note_for_human": "decide the gateway"})
        return AgentResult({"reason": "r", "note_for_human": "n", **data}, 0.05)


class Triage(Base):
    def test_rewrite_requeues_the_card_once(self):
        self.add("A", ["a.py"])
        runner = TriageRunner({"A": [blocked, done(**{"a.py": "x = 1\n"})]},
                              triage={"A": {"action": "rewrite", "reason": "gateway is Razorpay per repo",
                                            "card": {"spec": "Use Razorpay (see payments/)."}}})
        self.run_orch(runner)
        t = self.store.get("A")
        self.assertEqual((t.status, t.triaged), ("merged", True))
        self.assertEqual(runner.cards["A"][-1].spec, "Use Razorpay (see payments/).")
        self.assertTrue(any(f.startswith("Triage rewrote this card (spec)") for f in t.feedback))
        context, cwd = runner.triage_contexts["A"]
        self.assertIn("payment gateway", context)  # the block reason reaches triage
        self.assertEqual(cwd, "_triage")
        # a second block on the same card goes straight to the human
        self.add("B", ["b.py"])
        runner2 = TriageRunner({"B": [blocked]}, triage={"B": {"action": "rewrite", "card": {"spec": "v2"}}})
        self.run_orch(runner2)
        self.assertEqual(self.store.get("B").status, "blocked")
        self.assertEqual(runner2.triage_calls["B"], 1)

    def test_rewrite_with_new_scope_waits_for_overlapping_cards(self):
        self.add("A", ["a.py"])
        self.add("C", ["c.py"], deps=["A"])  # depends on A: must not become a dependency of A
        self.add("D", ["d.py"])
        runner = TriageRunner({"A": [blocked], "C": [done(**{"c.py": "c\n"})], "D": [done(**{"d.py": "d\n"})]},
                              triage={"A": {"action": "rewrite", "card": {"files_scope": ["a.py", "c.py", "d.py"]}}},
                              delays={"A": 0.01, "D": 0.5})
        orch = Orchestrator(self.cfg, self.store, runner, log=self.logs.append)
        asyncio.run(orch.run())
        a = self.store.get("A")
        self.assertIn("D", a.depends_on)
        self.assertNotIn("C", a.depends_on)

    def test_escalate_moves_to_a_stronger_model_unless_it_already_ran_there(self):
        self.add("A", ["a.py"])
        self.add("L", ["l.py"], complexity="large")  # large cards run on opus already
        runner = TriageRunner({"A": [blocked, done(**{"a.py": "x\n"})], "L": [blocked]},
                              triage={"A": {"action": "escalate", "model": "opus"},
                                      "L": {"action": "escalate", "model": "opus",
                                            "note_for_human": "needs a product call"}})
        notes = []
        orch = Orchestrator(self.cfg, self.store, runner, log=self.logs.append)
        orch.notify = lambda title, message, url=None: notes.append((title, message))
        asyncio.run(orch.run())
        a, l = self.store.get("A"), self.store.get("L")
        self.assertEqual((a.status, a.model), ("merged", "opus"))
        self.assertEqual(l.status, "blocked")
        self.assertIsNone(l.model)
        self.assertTrue(any("already ran on opus" in f for f in l.feedback))
        self.assertTrue(any(t == "Orchestra: L blocked" and "Triage:" in m for t, m in notes))

    def test_split_rewires_dependents_and_cancels_the_original(self):
        self.add("A", ["a.py", "b.py"])
        self.add("B", ["c.py"], deps=["A"])
        self.add("X", ["a.py"])  # unrelated card touching a.py: the new cards wait for it
        script = {"A": [blocked], "B": [done(**{"c.py": "c\n"})], "X": [done(**{"a.py": "x\n"})],
                  "T04": [done(**{"a.py": "a\n"})], "T05": [done(**{"b.py": "b\n"})]}
        runner = TriageRunner(script, triage={"A": {"action": "split", "cards": [
            item("one", ["a.py"]), item("two", ["b.py"], deps=["one"])]}},
                              delays={"A": 0.01, "X": 0.4})
        self.store.update("A", test_first=False, source="linear:ENG-7")
        self.run_orch(runner)
        a = self.store.get("A")
        self.assertEqual((a.status, a.summary), ("cancelled", "split into T04, T05"))
        t4, t5, b = self.store.get("T04"), self.store.get("T05"), self.store.get("B")
        self.assertEqual(sorted(b.depends_on), ["T04", "T05"])
        self.assertIn("T04", t5.depends_on)
        self.assertIn("X", t4.depends_on)
        self.assertTrue(t4.triaged and t5.triaged)
        self.assertEqual(t4.source, "linear:ENG-7")
        self.assertEqual([self.store.get(i).status for i in ("T04", "T05", "B", "X")], ["merged"] * 4)

    def test_split_outside_scope_or_disallowed_goes_to_the_human(self):
        self.add("A", ["a.py"])
        self.add("B", ["b.py"])
        self.cfg.autonomy.triage_actions = ["rewrite", "split"]
        runner = TriageRunner({"A": [blocked], "B": [blocked]}, triage={
            "A": {"action": "split", "cards": [item("one", ["a.py"]), item("two", ["z.py"])]},
            "B": {"action": "escalate", "model": "opus"}})
        self.run_orch(runner)
        a, b = self.store.get("A"), self.store.get("B")
        self.assertEqual((a.status, b.status), ("blocked", "blocked"))
        self.assertTrue(any("outside the card's files_scope" in f for f in a.feedback))
        self.assertTrue(any("needs your approval" in f for f in b.feedback))
        self.assertEqual(len(self.store.all()), 2, "no cards were added")

    def test_no_triage_when_off_or_over_budget(self):
        self.add("A", ["a.py"])
        self.cfg.autonomy.triage = False
        runner = TriageRunner({"A": [blocked]}, triage={"A": {"action": "rewrite", "card": {"spec": "x"}}})
        self.run_orch(runner)
        self.assertEqual(runner.triage_calls, {})
        self.assertEqual(self.store.get("A").status, "blocked")
        self.cfg.autonomy.triage, self.cfg.total_budget_usd = True, 0.01  # the blocked call spends 0.05
        self.add("B", ["b.py"])
        runner = TriageRunner({"B": [blocked]}, triage={"B": {"action": "rewrite", "card": {"spec": "x"}}})
        self.run_orch(runner)
        self.assertEqual((runner.triage_calls, self.store.get("B").status), ({}, "blocked"))

    def test_triage_cut_off_by_a_usage_limit_runs_next_time(self):
        self.add("A", ["a.py"])

        class Limited(TriageRunner):
            async def triage(self, cfg, task, context, cwd, graph=None):
                self.triage_calls[task.id] = self.triage_calls.get(task.id, 0) + 1
                return AgentResult({}, 0.0, ok=False, error="rate limit", limited=True, resets_at=time.time() - 1)
        self.run_orch(Limited({"A": [blocked]}))
        a = self.store.get("A")
        self.assertEqual((a.status, a.triaged), ("blocked", False))
        self.assertEqual(self.store.get_meta("triage_later"), ["A"])
        runner = TriageRunner({"A": [done(**{"a.py": "x\n"})]},
                              triage={"A": {"action": "rewrite", "card": {"spec": "clearer"}}})
        self.run_orch(runner)
        self.assertEqual(self.store.get("A").status, "merged")
        self.assertEqual(self.store.get_meta("triage_later"), [])

    def test_a_split_original_is_not_retried_by_accident(self):
        self.add("A", ["a.py"])
        self.store.update("A", status="cancelled", summary="split into T04, T05")
        with self.assertRaises(SystemExit) as e:
            main(["-C", str(self.repo), "retry", "A"])
        self.assertIn("split into", str(e.exception))

    def test_exhausted_attempts_are_triaged(self):
        self.cfg.max_attempts = 1
        self.add("A", ["a.py"])
        outside = done(**{"z.py": "z = 1\n"})  # fails the scope gate in every fix round
        runner = TriageRunner({"A": [outside, outside, outside, done(**{"a.py": "x = 1\n"})]},
                              triage={"A": {"action": "rewrite", "card": {"acceptance_criteria": ["a.py sets x"]}}})
        self.run_orch(runner)
        self.assertEqual(runner.triage_calls.get("A"), 1)
        context, _ = runner.triage_contexts["A"]
        self.assertIn("Attempts used: 1 of 1", context)
        self.assertIn("edited files outside task scope", context)
        a = self.store.get("A")
        self.assertEqual((a.status, a.acceptance), ("merged", ["a.py sets x"]))


FAKE_GH = '''#!{python}
import json, pathlib, subprocess, sys
STATE = pathlib.Path({state!r})
s = json.loads(STATE.read_text())
a = sys.argv[1:]
s["calls"].append(a)
code = 0
def arg(name):
    return a[a.index(name) + 1]
if a[:2] == ["pr", "list"]:
    print(json.dumps([{{"number": p["number"], "url": p["url"], "isCrossRepository": False}} for p in s["prs"]]))
elif a[:2] == ["pr", "view"]:
    head = subprocess.run(["git", "--git-dir", s["remote"], "rev-parse", "orchestra/integration"],
                          capture_output=True, text=True).stdout.strip()
    print(json.dumps({{"headRefOid": head}}))
elif a[:2] == ["pr", "create"]:
    n = len(s["prs"]) + 1
    p = {{"number": n, "url": "https://github.com/o/r/pull/%d" % n, "title": arg("--title"),
         "body": pathlib.Path(arg("--body-file")).read_text(), "draft": "--draft" in a,
         "head": arg("--head"), "base": arg("--base")}}
    s["prs"].append(p)
    print("Creating pull request\\n" + p["url"])
elif a[:2] == ["pr", "edit"]:
    p = s["prs"][int(a[2]) - 1]
    p["title"], p["body"] = arg("--title"), pathlib.Path(arg("--body-file")).read_text()
    s["edits"] += 1
elif a[:2] == ["pr", "checks"]:
    seq = s["checks"]
    cur = seq.pop(0) if len(seq) > 1 else seq[0]
    print(json.dumps(cur))
    code = 8 if any(c["bucket"] == "pending" for c in cur) else 1 if any(c["bucket"] == "fail" for c in cur) else 0
elif a[:2] == ["run", "view"]:
    print("FAILED test_calc.py::test_add - AssertionError: 4 != 3")
STATE.write_text(json.dumps(s))
sys.exit(code)
'''

FAIL = {"name": "tests", "bucket": "fail", "link": "https://github.com/o/r/actions/runs/42/job/7", "workflow": "CI"}
PASS = {"name": "tests", "bucket": "pass", "link": "https://github.com/o/r/actions/runs/43/job/8", "workflow": "CI"}
PENDING = {"name": "tests", "bucket": "pending", "link": "", "workflow": "CI"}


class Shipping(Base):
    def setUp(self):
        super().setUp()
        self.remote = self.tmp / "remote.git"
        sh(f"git init -q --bare {self.remote}", self.tmp)
        sh(f"git remote add origin {self.remote}", self.repo)
        self.state = self.tmp / "gh_state.json"
        self.gh = self.tmp / "gh"
        self.gh.write_text(FAKE_GH.format(python=sys.executable, state=str(self.state)))
        self.gh.chmod(0o755)
        self.cfg.ship.gh = str(self.gh)
        self.cfg.ship.auto = True

    def gh_state(self, checks):
        self.state.write_text(json.dumps({"calls": [], "prs": [], "edits": 0, "checks": checks,
                                          "remote": str(self.remote)}))

    def orch(self, runner):
        o = Orchestrator(self.cfg, self.store, runner, log=self.logs.append)
        o.shipper.check_poll_s = 0.01
        return o

    def test_draft_pr_then_red_ci_becomes_a_fix_card_that_goes_green(self):
        self.gh_state([[PENDING], [FAIL], [FAIL], [PASS]])  # a verdict needs two equal polls
        self.add("A", ["calc.py"])
        self.add("W", ["docs.md"])  # left waiting on a question: the fix card must not wait for it
        self.store.update("W", status="waiting", decision={"question": "?", "options": [], "asked_at": 1})
        self.cfg.decision_wait_minutes = 0
        fix = done(**{"calc.py": "def add(a, b):\n    return a + b  # fixed\n"})
        runner = FakeRunner({"A": [done(**{"calc.py": "def add(a, b):\n    return b + a\n"})], "T03": [fix]})
        o = self.orch(runner)
        asyncio.run(o.run())
        s = json.loads(self.state.read_text())
        self.assertEqual(len(s["prs"]), 1)
        pr = s["prs"][0]
        self.assertEqual((pr["draft"], pr["head"], pr["base"], s["edits"]), (True, "orchestra/integration", "main", 1))
        self.assertIn("### A: task A", pr["body"])
        self.assertIn("### T03: Fix CI: tests", pr["body"])
        t2 = self.store.get("T03")
        self.assertEqual((t2.status, t2.source, t2.complexity), ("merged", "ci", "large"))
        self.assertIn("AssertionError: 4 != 3", t2.spec)
        self.assertIn("calc.py", t2.files_scope)
        self.assertEqual(o.pr["ci"], "passed")
        remote_head = sh("git rev-parse orchestra/integration", self.remote).strip()
        self.assertEqual(remote_head, sh("git rev-parse orchestra/integration", self.repo).strip())
        self.assertEqual(self.store.get_meta("last_pr")["ci"], "passed")
        self.assertEqual(t2.depends_on, [])
        self.assertTrue(any(c[:2] == ["pr", "view"] for c in s["calls"]), "waits for the PR to show the push")

    def test_an_answer_during_the_ci_wait_runs_and_ships_again(self):
        self.gh_state([[PENDING]] * 6 + [[PASS]])
        self.cfg.decision_wait_minutes = 0  # the first drain doesn't wait for the answer
        self.add("A", ["calc.py"])
        self.add("T02", ["fee.py"])
        self.store.update("T02", status="waiting", decision={"question": "Round?", "options": ["up", "down"],
                                                             "recommendation": "up", "asked_at": 1.0})
        pr_open = lambda: bool(json.loads(self.state.read_text())["prs"])  # noqa: E731
        phone = FakeNtfy(lambda now: [{"id": "r", "time": int(now), "event": "message", "message": "T02 1"}]
                         if pr_open() else [])
        runner = FakeRunner({"A": [done(**{"calc.py": "def add(a, b):\n    return b + a\n"})],
                             "T02": [done(**{"fee.py": "def fee(x):\n    return x\n"})]})
        o = self.orch(runner)
        o.shipper.check_poll_s = 0.05
        o.ntfy, o.ntfy_poll_s = phone, 0.02
        asyncio.run(o.run())
        s = json.loads(self.state.read_text())
        self.assertEqual(self.store.get("T02").status, "merged")
        self.assertEqual(s["edits"], 1, "shipped again with the answered card")
        self.assertIn("### T02:", s["prs"][0]["body"])

    def test_fix_rounds_are_capped(self):
        self.cfg.ship.ci_fix_rounds = 1
        self.gh_state([[FAIL]])
        self.add("A", ["calc.py"])
        runner = FakeRunner({"A": [done(**{"calc.py": "def add(a, b):\n    return b + a\n"})],
                             "T02": [done(**{"calc.py": "def add(a, b):\n    return a + b\n"})]})
        o = self.orch(runner)
        asyncio.run(o.run())
        fixes = [t for t in self.store.all() if t.source == "ci"]
        self.assertEqual(len(fixes), 1)
        self.assertEqual(o.pr["ci"], "failed")

    def test_push_is_never_forced(self):
        self.gh_state([[PASS]])
        sh("git checkout -q -b other && git commit -q --allow-empty -m other && "
           "git push -q origin other:refs/heads/orchestra/integration && git checkout -q main", self.repo)
        before = sh("git rev-parse orchestra/integration", self.remote).strip()
        self.add("A", ["calc.py"])
        o = self.orch(FakeRunner({"A": [done(**{"calc.py": "def add(a, b):\n    return b + a\n"})]}))
        asyncio.run(o.run())
        self.assertEqual(sh("git rev-parse orchestra/integration", self.remote).strip(), before)
        self.assertTrue(any("never force-pushes" in l for l in self.logs))
        self.assertEqual(json.loads(self.state.read_text())["prs"], [])
        self.assertIsNone(o.pr)

    def test_without_remote_or_gh_shipping_is_skipped(self):
        sh("git remote remove origin", self.repo)
        self.add("A", ["calc.py"])
        counts = self.run_orch(FakeRunner({"A": [done(**{"calc.py": "def add(a, b):\n    return b + a\n"})]}))
        self.assertEqual(counts["merged"], 1)
        self.assertTrue(any("not shipped: no git remote named 'origin'" in l for l in self.logs))
        sh(f"git remote add origin {self.remote}", self.repo)
        self.cfg.ship.gh = str(self.tmp / "missing-gh")
        res = asyncio.run(Shipper(self.cfg, self.store).ship())
        self.assertEqual(res.status, "skipped")
        self.assertIn("GitHub CLI not found", res.note)

    def test_title_and_linear_links(self):
        self.store.set_meta("plans", [{"goal": "Linear ENG-7: Add surge pricing\n\nmore", "source": None}])
        t = Task(id="T01", title="Surge fee", spec="", source="linear:ENG-7", summary="adds fee",
                 decisions=[{"question": "Round?", "answer": "up"}], locked_tests=["test_fee.py"],
                 cost_usd=1.5, attempts=2)
        sh_ = Shipper(self.cfg, self.store)
        self.assertEqual(sh_.title([t]), "Linear ENG-7: Add surge pricing")
        body = sh_.body([t])
        self.assertIn("Part of ENG-7", body)
        self.assertIn("Decision: Round? → **up**", body)
        self.assertIn("`test_fee.py`", body)
        self.store.set_meta("plans", [])
        self.assertEqual(sh_.title([t, t]), "Orchestra: 2 cards")


class FakeNtfy(Ntfy):
    def __init__(self, replies):
        super().__init__("https://ntfy.example", "orchestra-in", "orchestra-reply")
        self.replies, self.published = replies, []

    async def publish(self, title, message, actions=None, priority=3, tags=None, topic=None):
        self.published.append({"title": title, "message": message, "actions": actions or []})
        return {}

    async def poll(self, since):
        return [m for m in self.replies(time.time()) if m]


def asks(option_count=3):
    def step(wt, n, task):
        return AgentResult({"status": "needs_decision", "summary": "half done", "files_changed": [], "tests": "",
                            "blocker": "", "decision": {"question": "Round fees?", "options": ["up", "down", "none"][:option_count],
                                                        "recommendation": "down", "why_it_matters": "money"}}, 0.1)
    return step


class Phone(Base):
    def test_question_buttons_and_reply_parsing(self):
        n = Ntfy("https://ntfy.sh", "in", "out", token="tok", label="restocare")
        title, msg, actions = n.question("T03", "Fees", {"question": "Round?", "options": ["up", "down", "none"],
                                                         "recommendation": "down"}, tag="ab12cd")
        self.assertLessEqual(len(actions), 3)
        self.assertEqual([a["body"] for a in actions if a["action"] == "http"], ["T03#ab12cd r", "T03#ab12cd 1"])
        self.assertEqual(actions[0]["headers"], {"Authorization": "Bearer tok"})
        self.assertEqual(actions[-1]["action"], "view")
        self.assertIn("T03 2", msg)
        self.assertEqual(parse_reply(" t03: use 4h "), ("T03", "", "use 4h"))
        self.assertEqual(parse_reply("T03#AB12CD r"), ("T03", "ab12cd", "r"))
        self.assertIsNone(parse_reply("thanks!"))
        same = Ntfy("https://ntfy.sh", "same", "same")
        self.assertFalse(same.can_receive)
        _, msg, actions = same.question("T03", "Fees", {"question": "Round?", "options": ["up"], "recommendation": "up"})
        self.assertEqual(actions, [])  # no buttons when nobody reads the replies
        self.assertIn("orchestra answer T03", msg)

    def test_a_phone_reply_resumes_the_waiting_card(self):
        self.add("T01", ["fee.py"])
        self.cfg.decision_wait_minutes = 1
        sent = time.time()
        msgs = lambda now: [  # noqa: E731 - replayed on every poll; each must count once
            {"id": "m0", "time": int(sent), "event": "message", "message": "hello"},
            {"id": "m1", "time": int(sent), "event": "message", "message": "T09 1"},
            {"id": "m2", "time": int(sent), "event": "message", "message": "T01 2"} if now - sent > 0.3 else None]
        phone = FakeNtfy(msgs)
        runner = FakeRunner({"T01": [asks(), done(**{"fee.py": "def fee(x):\n    return x\n"})]})
        o = Orchestrator(self.cfg, self.store, runner, log=self.logs.append, poll_s=0.05)
        o.ntfy, o.ntfy_poll_s = phone, 0.05
        asyncio.run(o.run())
        t = self.store.get("T01")
        self.assertEqual(t.status, "merged")
        self.assertEqual(t.decisions[0]["answer"], "down")
        self.assertEqual(runner.cards["T01"][-1].decisions[0]["answer"], "down")  # the worker sees it
        q = next(p for p in phone.published if p["title"].startswith("T01 needs your decision"))
        self.assertEqual(len(q["actions"]), 3)
        texts = [p["message"] for p in phone.published]
        self.assertEqual(sum(1 for m in texts if m.startswith("✓ T01 resumes with: down")), 1)
        self.assertIn("There is no card T09.", texts)
        self.assertEqual(sum(1 for m in texts if m.startswith("Couldn't read that reply")), 1)
        self.assertTrue(any(p["title"] == "Orchestra run finished" for p in phone.published))

    def test_questions_from_an_earlier_run_are_pushed_and_answered(self):
        self.add("T01", ["fee.py"])
        self.store.update("T01", status="waiting", decision={"question": "Round fees?", "options": ["up", "down"],
                                                             "recommendation": "up"})
        self.cfg.decision_wait_minutes = 1
        phone = FakeNtfy(lambda now: [{"id": "r1", "time": int(now), "event": "message", "message": "T01 r"}])
        runner = FakeRunner({"T01": [done(**{"fee.py": "def fee(x):\n    return x\n"})]})
        o = Orchestrator(self.cfg, self.store, runner, log=self.logs.append, poll_s=0.05)
        o.ntfy, o.ntfy_poll_s = phone, 0.05
        asyncio.run(o.run())
        self.assertTrue(phone.published[0]["title"].startswith("T01 needs your decision"))
        t = self.store.get("T01")
        self.assertEqual((t.status, t.decisions[0]["answer"]), ("merged", "up"))

    def test_reply_for_a_card_not_waiting_is_ignored(self):
        self.add("T01", ["a.py"])
        o = Orchestrator(self.cfg, self.store, FakeRunner({}), log=self.logs.append)
        o.ntfy = FakeNtfy(lambda now: [])

        async def go():
            o._handle_reply("T01 1")
            await o._flush_pushes()
        asyncio.run(go())
        self.assertEqual(self.store.get("T01").status, "pending")
        self.assertIn("isn't waiting", o.ntfy.published[0]["message"])

    def test_stale_button_does_not_answer_a_newer_question(self):
        self.add("T01", ["a.py"])
        q2 = {"question": "Second?", "options": ["x", "y"], "recommendation": "x", "asked_at": 2.0}
        self.store.update("T01", status="waiting", decision=q2)
        o = Orchestrator(self.cfg, self.store, FakeRunner({}), log=self.logs.append)
        o.ntfy = FakeNtfy(lambda now: [])
        old = question_tag(str(self.cfg.repo), "T01", {"asked_at": 1.0})
        new = question_tag(str(self.cfg.repo), "T01", q2)

        async def go():
            o._handle_reply(f"T01#{old} 2")
            self.assertEqual(self.store.get("T01").status, "waiting")
            o._handle_reply(f"T01#{new} 2")
            await o._flush_pushes()
        asyncio.run(go())
        self.assertIn("earlier question", o.ntfy.published[0]["message"])
        self.assertEqual(self.store.get("T01").decisions[0]["answer"], "y")

    def test_setup_keeps_topics_out_of_the_committed_config(self):
        (self.repo / "orchestra.toml").write_text('[orchestra]\nbase_branch = "main"\n[ntfy]\nserver = "https://ntfy.sh"\n')
        (self.repo / ".gitignore").write_text(".orchestra/\n")
        with redirect_stdout(io.StringIO()) as out:
            main(["-C", str(self.repo), "ntfy", "setup"])
        from orchestra.config import Config
        s = Config.load(self.repo).ntfy
        self.assertRegex(s.topic, r"^orchestra-[a-z0-9]{16}$")
        self.assertNotEqual(s.topic, s.reply_topic)
        self.assertNotIn("orchestra-", (self.repo / "orchestra.toml").read_text())
        self.assertNotIn("WARNING", out.getvalue())
        with redirect_stdout(io.StringIO()):
            main(["-C", str(self.repo), "ntfy", "setup"])  # keeps the existing topics
        self.assertEqual(Config.load(self.repo).ntfy.topic, s.topic)


ISSUE = {"id": "uuid-7", "identifier": "ENG-7", "title": "Add surge pricing", "url": "https://linear.app/x/ENG-7",
         "description": "Charge 1.5x on Friday nights.",
         "comments": [{"body": "Cap at 2x", "createdAt": "2026-10-02", "user": {"name": "Asha"}},
                      {"body": "Only Bangalore", "createdAt": "2026-10-01", "user": {"name": "Ravi"}}]}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class LinearIntake(Base):
    def test_client_uses_the_raw_key_and_sorts_comments(self):
        seen = []

        def urlopen(req, timeout=0):
            seen.append(req)
            body = json.loads(req.data)
            if "commentCreate" in body["query"]:
                return FakeResponse(json.dumps({"data": {"commentCreate": {"success": True}}}).encode())
            issue = {**ISSUE, "comments": {"nodes": ISSUE["comments"]}}
            return FakeResponse(json.dumps({"data": {"issue": issue}}).encode())
        with mock.patch("orchestra.linear.urllib.request.urlopen", urlopen):
            client = Linear("lin_api_123")
            issue = client.issue("eng-7")
            client.comment(issue["id"], "hi")
        self.assertEqual(seen[0].get_header("Authorization"), "lin_api_123")
        self.assertEqual(json.loads(seen[0].data)["variables"], {"id": "ENG-7"})
        self.assertEqual([c["user"]["name"] for c in issue["comments"]], ["Ravi", "Asha"])
        self.assertEqual(json.loads(seen[1].data)["variables"]["input"], {"issueId": "uuid-7", "body": "hi"})
        with self.assertRaises(LinearError):
            normalize_id("not a ticket")
        with mock.patch.dict(os.environ, {"LINEAR_API_KEY": ""}):
            with self.assertRaisesRegex(LinearError, "LINEAR_API_KEY"):
                Linear.from_settings(self.cfg.linear)

    def test_plan_from_a_ticket_keeps_the_source_on_cards(self):
        goal = ticket_goal(ISSUE, "skip the admin UI")
        self.assertTrue(goal.startswith("Linear ENG-7: Add surge pricing\nhttps://linear.app/x/ENG-7"))
        self.assertIn("- Asha: Cap at 2x", goal)
        self.assertTrue(goal.rstrip().endswith("skip the admin UI"))
        plan = {"summary": "s", "open_questions": [], "tasks": [item("fee", ["fee.py"])]}
        seen_goals, comments = [], []

        class Planner(FakeRunner):
            def __init__(self, verbose=False):
                super().__init__({})

            async def plan(self, cfg, goal, cwd=None, graph=None):
                seen_goals.append(goal)
                return AgentResult(plan, 0.1)
        (self.repo / "orchestra.toml").write_text('[orchestra]\nbase_branch = "main"\n[linear]\ncomment = true\n'
                                                  '[planning]\ncritic = false\n')
        with mock.patch("orchestra.cli.ClaudeRunner", Planner), \
                mock.patch.object(Linear, "issue", lambda self, i: ISSUE), \
                mock.patch.object(Linear, "comment", lambda self, i, b: comments.append((i, b))), \
                mock.patch.dict(os.environ, {"LINEAR_API_KEY": "k"}), redirect_stdout(io.StringIO()) as out:
            main(["-C", str(self.repo), "plan", "--linear", "ENG-7", "skip the admin UI", "--approve"])
        self.assertIn("Charge 1.5x on Friday nights.", seen_goals[0])
        st = Store(self.cfg.state_path / "state.db")
        card = st.get("T01")
        self.assertEqual(card.source, "linear:ENG-7")
        self.assertEqual(st.get_meta("plans")[-1]["source"]["linear"], "ENG-7")
        saved = json.loads((self.cfg.state_path / "plan.json").read_text())
        self.assertEqual(saved["source"]["linear"], "ENG-7")
        self.assertEqual(comments[0][0], "uuid-7")
        self.assertIn("T01: part fee", comments[0][1])
        self.assertIn("Commented on ENG-7", out.getvalue())

    def test_plan_needs_a_goal_or_a_ticket(self):
        with self.assertRaises(SystemExit) as e:
            main(["-C", str(self.repo), "plan"])
        self.assertIn("--linear", str(e.exception))


if __name__ == "__main__":
    unittest.main()
