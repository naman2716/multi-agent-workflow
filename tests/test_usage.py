"""`orchestra usage`: token and cost breakdown of an export of agent transcripts."""
import csv
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestra import usage  # noqa: E402
from orchestra.cli import main  # noqa: E402
from orchestra.pricing import cost_usd, price_of  # noqa: E402

CARD = "# Task T02: Models\n\n## Spec\nDo it.\n"


def enqueue(text, then="dequeue"):
    return [{"type": "queue-operation", "operation": "enqueue", "content": text, "timestamp": "2026-10-14T13:00:00Z"},
            {"type": "queue-operation", "operation": then, "timestamp": "2026-10-14T13:00:00Z"}]


def turn(mid, model="claude-sonnet-5-5", inp=0, out=100, read=0, w1h=0, w5m=0, blocks=None):
    u = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
         "cache_creation_input_tokens": w1h + w5m,
         "cache_creation": {"ephemeral_1h_input_tokens": w1h, "ephemeral_5m_input_tokens": w5m}}
    msg = {"id": mid, "model": model, "role": "assistant", "usage": u}
    # the CLI writes one line per content block, repeating the message's usage on each
    return [{"type": "assistant", "timestamp": "2026-10-14T13:01:00Z", "message": {**msg, "content": [b]}}
            for b in (blocks or [{"type": "text", "text": "ok"}])]


def synthetic(text="No response requested.", error=False):
    return {"type": "assistant", "isApiErrorMessage": error, "timestamp": "2026-10-14T13:02:00Z",
            "message": {"model": "<synthetic>", "role": "assistant", "content": [{"type": "text", "text": text}]}}


def tool_result(tid, text):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": text}]}}


def write(path, recs):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")


class UsageExport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "export"
        t = self.root / "agent-transcripts"
        bash = {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "cat big.md"}}
        write(t / "T02" / "worker.jsonl", [
            *enqueue(CARD), {"type": "user", "message": {"role": "user", "content": CARD}},
            *turn("m1", inp=2, read=0, w1h=30_000, blocks=[bash, {"type": "text", "text": "reading"}]),
            tool_result("tu1", "x" * 12_000),
            *enqueue("<task-notification>\n<task-id>b1</task-id>", then="remove"),  # folded into the turn
            *turn("m2", read=30_000, w1h=4_000),
            {"type": "cost-state", "totalCostUSD": 0.5},
            *enqueue("Your last change failed automated checks. Fix these:\n[gate:diff_size] FAILED"),
            synthetic(),
            *turn("m3", read=34_000, w1h=1_000),
            {"type": "cost-state", "totalCostUSD": 0.9},
        ])
        write(t / "T02" / "validator.jsonl", [
            *enqueue(CARD + "\n## Finding to verify\nX\n\n## Diff\n```diff\n```"),
            *turn("v1", model="claude-opus-5-5", out=200, read=5_000, w1h=20_000),
            synthetic("You've hit your session limit", error=True),
        ])
        write(t / "T02" / "reviewer.jsonl", [
            *enqueue(CARD + "\n## Worker's report\n```json\n{}\n```"),
            *turn("r1", model="claude-opus-5-5", read=1_000, w5m=1_000)])
        write(t / "triage" / "tri.jsonl", [
            *enqueue(CARD + "\n\nInvestigate, then choose the action."), *turn("t1", model="claude-opus-5-5")])
        write(t / "groom-dispatch-review-session" / "me.jsonl", [
            {"type": "user", "message": {"role": "user", "content": "/groom"}}, *turn("g1", read=50_000)])
        state = self.root / "state"
        state.mkdir()
        (state / "tasks.json").write_text(json.dumps([
            {"id": "T02", "title": "Models", "status": "merged", "attempts": 2, "triaged": 0, "test_first": 0,
             "cost_usd": 1.4}]))
        (state / "events.json").write_text(json.dumps([
            {"id": 1, "task_id": "T02", "kind": "gate", "detail": "gates failed (round 1): diff_size"},
            {"id": 2, "task_id": "T02", "kind": "review", "detail": "review rejected (opus)"},
            {"id": 3, "task_id": "T02", "kind": "revise", "detail": "revising in the same session (1/1)"},
            {"id": 4, "task_id": "T02", "kind": "review", "detail": "review approved"}]))

    def tearDown(self):
        self.tmp.cleanup()

    def test_sessions_are_counted_once_per_message_and_classified(self):
        r = usage.build(self.root)
        by_path = {s.path: s for s in r.sessions}
        w = by_path["T02/worker.jsonl"]
        self.assertEqual((w.role, w.card, w.turns), ("worker", "T02", 3))  # m1 spans two lines
        self.assertEqual(w.write_1h, 35_000)
        self.assertEqual(w.cache_read, 64_000)
        self.assertEqual(w.output, 300)
        self.assertEqual(w.cold_start, 30_000)
        self.assertEqual(w.resumes, ["gate_fix"])
        self.assertEqual(w.round_tokens, [2 + 30_000 + 100 + 30_000 + 4_000 + 100, 34_000 + 1_000 + 100])
        self.assertEqual(w.synthetic, ["resume_marker"])
        self.assertEqual(w.cli_usd, 0.9)
        self.assertEqual(by_path["T02/validator.jsonl"].role, "validator")
        self.assertTrue(by_path["T02/validator.jsonl"].synthetic[0].startswith("api_error"))
        self.assertEqual(by_path["T02/reviewer.jsonl"].role, "reviewer")
        tri = by_path["triage/tri.jsonl"]
        self.assertEqual((tri.role, tri.card), ("triage", "T02"))
        self.assertEqual(by_path["groom-dispatch-review-session/me.jsonl"].role, "interactive")

    def test_cost_prices_cache_writes_by_ttl(self):
        self.assertEqual(price_of("claude-opus-5-5"), (4.0, 20.0, 0.20))
        self.assertEqual(price_of("claude-sonnet-4-6-whatever"), price_of("claude-sonnet-5-5"))
        self.assertIsNone(price_of("gpt-x"))
        self.assertAlmostEqual(cost_usd("claude-sonnet-5-5", write_1h=1_000_000), 4.0)
        self.assertAlmostEqual(cost_usd("claude-sonnet-5-5", write_5m=1_000_000), 2.5)
        r = usage.build(self.root)
        v = next(s for s in r.sessions if s.role == "validator")
        self.assertAlmostEqual(v.usd, (200 * 20 + 5_000 * 0.2 + 20_000 * 8) / 1e6)

    def test_cards_join_queue_state_and_leave_out_interactive_sessions(self):
        d = usage.to_dict(usage.build(self.root))
        (card,) = d["cards"]
        self.assertEqual((card["id"], card["status"], card["recorded_usd"]), ("T02", "merged", 1.4))
        self.assertEqual((card["gate_failures"], card["reviews"], card["first_review"], card["revise_rounds"]),
                         (1, 2, "rejected", 1))
        self.assertEqual(card["sessions"], 4)  # worker, validator, reviewer, triage
        self.assertEqual(card["by_role"]["worker"], {"sessions": 1, "turns": 3})
        self.assertEqual(card["resumed_tokens"], 35_100)
        self.assertEqual(d["totals"]["sessions"], 4)
        self.assertEqual(d["totals"]["recorded_usd"], 1.4)
        self.assertEqual(d["interactive"]["sessions"], 1)
        self.assertEqual(d["resume_rounds"], {"gate_fix": {"rounds": 1, "tokens": 35_100}})

    def test_cap_what_if_counts_later_turns(self):
        c = usage.to_dict(usage.build(self.root), cap=8_000)["tool_output_cap"]
        self.assertEqual((c["results"], c["chars_cut"], c["by_tool"]), (1, 4_000, {"Bash": 1}))
        self.assertEqual(c["context_tokens_cut"], 1_000 * 2)  # re-read by m2 and m3

    def test_cli_writes_table_json_and_csv(self):
        js, cs = Path(self.tmp.name) / "r.json", Path(self.tmp.name) / "r.csv"
        out = io.StringIO()
        with redirect_stdout(out):
            main(["usage", str(self.root), "--json", str(js), "--csv", str(cs), "--markdown"])
        self.assertIn("| T02 | merged |", out.getvalue())
        self.assertIn("recorded in tasks.cost_usd $1.40", out.getvalue())
        self.assertEqual(json.loads(js.read_text())["totals"]["sessions"], 4)
        rows = list(csv.DictReader(io.StringIO(cs.read_text())))
        self.assertEqual(len(rows), 5)
        self.assertEqual({r["card_status"] for r in rows if r["role"] == "worker"}, {"merged"})

    def test_loose_transcripts_without_state(self):
        r = usage.build(self.root / "agent-transcripts")
        self.assertEqual(len(r.sessions), 5)
        self.assertIsNone(r.cards["T02"].recorded_usd)


if __name__ == "__main__":
    unittest.main()
