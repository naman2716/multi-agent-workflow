"""Where agent sessions spend tokens, from an export of Claude Code transcripts.

`orchestra usage <export-dir>` reads `agent-transcripts/<card>/*.jsonl` (one session per file)
and, when present, the queue state next to it (`tasks.json` + `events.json`, or `state.db`, at
the top level or under `state/`). It reports per session and per card: tokens by type, an
estimated USD cost (orchestra/pricing.py), turns, peak context, cold starts, the prompts that
resumed a session and what each round cost, and the card's outcome from the queue.

Claude Code writes one transcript line per content block and repeats the message's `usage` on
each, so usage is counted once per message id. `<synthetic>` assistant entries carry no usage:
"No response requested." marks a session resumed with a new prompt, and API errors (usage
limits) are recorded the same way with isApiErrorMessage set.
"""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .pricing import cost_usd

COLD_MIN_CONTEXT = 20_000  # a later turn is cold when it re-writes most of a context this big
ROLES = ("worker", "reviewer", "validator", "test_writer", "triage", "planner", "critic", "interactive")
RESUME_KINDS = (  # how the orchestrator words each message that resumes a session
    ("gate_fix", "Your last change failed automated checks."),
    ("revise", "An independent reviewer found problems"),
    ("conflict", "Your approved change conflicts"),
    ("usage_limit", "You were paused by a usage limit."),
    ("interrupted", "The orchestrator was interrupted"),
    ("task_notification", "<task-notification>"),  # the CLI: a background command finished
)


@dataclass
class Session:
    path: str
    card: str
    role: str
    models: list[str] = field(default_factory=list)
    start: str = ""
    end: str = ""
    turns: int = 0
    input: int = 0
    output: int = 0
    cache_read: int = 0
    write_5m: int = 0
    write_1h: int = 0
    peak: int = 0
    usd: float = 0.0
    cli_usd: float | None = None      # the CLI's own running total (last cost-state record)
    cold_start: int = 0               # tokens written to cache by the first turn
    cold_mid: int = 0                 # ... by later cold turns (resume after the cache expired)
    resumes: list[str] = field(default_factory=list)   # kind of each prompt that resumed it
    round_tokens: list[int] = field(default_factory=list)  # tokens per round (first run, resumes)
    synthetic: list[str] = field(default_factory=list)  # "resume_marker" / "api_error: <text>"
    unpriced: bool = False

    @property
    def cache_write(self) -> int:
        return self.write_5m + self.write_1h

    @property
    def tokens(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write


@dataclass
class Card:
    id: str
    title: str = ""
    status: str = ""
    attempts: int = 0
    triaged: bool = False
    test_first: bool = False
    recorded_usd: float | None = None  # tasks.cost_usd, what Orchestra's budget counted
    gate_failures: int = 0
    reviews: int = 0
    first_review: str = ""             # approved / rejected
    revise_rounds: int = 0
    blocked: int = 0
    split_into: str = ""


@dataclass
class ToolResult:
    chars: int
    tool: str
    path: str
    turns_after: int                   # assistant turns that re-read it from context


@dataclass
class Report:
    sessions: list[Session]
    cards: dict[str, Card]
    results: list[ToolResult]


# ---------------------------------------------------------------- transcripts
def _records(path: Path):
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                yield rec


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def classify(prompt: str, folder: str = "") -> str:
    """The role of a session, from its first prompt (the shapes agents.py builds)."""
    if folder in ("groom-dispatch-review-session", "subagents", "interactive"):
        return "interactive"
    if "\n## Finding to verify\n" in prompt or "\n## Findings to verify\n" in prompt:
        return "validator"
    if "\n## Worker's report\n" in prompt:
        return "reviewer"
    if prompt.rstrip().endswith("Write the acceptance tests for this card now (tests only)."):
        return "test_writer"
    if prompt.rstrip().endswith("Investigate, then choose the action."):
        return "triage"
    if prompt.startswith("Goal:"):
        return "critic" if "\n## Draft plan\n" in prompt else "planner"
    return "worker" if prompt.startswith("# Task ") else "interactive"


def resume_kind(prompt: str) -> str:
    return next((k for k, prefix in RESUME_KINDS if prompt.startswith(prefix)), "other")


def read_session(path: Path, root: Path, results: list[ToolResult] | None = None) -> Session | None:
    rel = str(path.relative_to(root))
    folder = rel.split("/")[0]
    prompts: list[str] = []
    queued: str | None = None
    first_user = ""
    seen: set = set()
    names: dict[str, str] = {}
    pending: list[ToolResult] = []
    s = Session(path=rel, card=folder, role="")
    models: set[str] = set()
    for rec in _records(path):
        ts = rec.get("timestamp")
        if ts:
            s.start, s.end = s.start or ts, ts
        kind = rec.get("type")
        if kind == "cost-state":
            s.cli_usd = rec.get("totalCostUSD", s.cli_usd)
            continue
        if kind == "queue-operation":  # a prompt starts a round once dequeued; "remove" = folded into a turn
            op = rec.get("operation")
            if op == "enqueue" and isinstance(rec.get("content"), str):
                queued = rec["content"]
            elif op == "dequeue" and queued is not None:
                prompts.append(queued)
                if len(prompts) > 1:
                    s.resumes.append(resume_kind(queued))
                    s.round_tokens.append(0)
                queued = None
            elif op == "remove":
                queued = None
            continue
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        content = msg.get("content")
        if kind == "user":
            first_user = first_user or _text(content)
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_result" and results is not None:
                    tr = ToolResult(len(json.dumps(b.get("content"), ensure_ascii=False)
                                        if not isinstance(b.get("content"), str) else b["content"]),
                                    names.get(b.get("tool_use_id"), "?"), rel, 0)
                    pending.append(tr)
                    results.append(tr)
            continue
        if kind != "assistant":
            continue
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                names[b.get("id")] = b.get("name", "?")
        if msg.get("model") == "<synthetic>":
            text = _text(content)
            s.synthetic.append(f"api_error: {text[:120]}" if rec.get("isApiErrorMessage")
                               else "resume_marker" if text == "No response requested." else f"other: {text[:120]}")
            continue
        usage = msg.get("usage")
        mid = msg.get("id") or rec.get("uuid")
        if not isinstance(usage, dict) or mid in seen:
            continue
        seen.add(mid)
        model = msg.get("model") or "?"
        models.add(model)
        inp = usage.get("input_tokens", 0) or 0
        out = usage.get("output_tokens", 0) or 0
        cr = usage.get("cache_read_input_tokens", 0) or 0
        cw = usage.get("cache_creation_input_tokens", 0) or 0
        split = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
        w1h = split.get("ephemeral_1h_input_tokens", 0) or 0
        w5m = cw - w1h  # without a split the API default (5 minutes) applies
        usd = cost_usd(model, inp, out, cr, w5m, w1h)
        s.unpriced = s.unpriced or usd is None
        s.usd += usd or 0.0
        s.turns += 1
        s.input, s.output, s.cache_read = s.input + inp, s.output + out, s.cache_read + cr
        s.write_5m, s.write_1h = s.write_5m + w5m, s.write_1h + w1h
        ctx = inp + cw + cr
        s.peak = max(s.peak, ctx)
        if s.turns == 1:
            s.cold_start = cw
        elif ctx >= COLD_MIN_CONTEXT and cw > 0.5 * ctx:
            s.cold_mid += cw
        if not s.round_tokens:
            s.round_tokens.append(0)
        s.round_tokens[-1] += inp + out + cr + cw
        for tr in pending:
            tr.turns_after += 1
    if not s.turns:
        return None
    s.models = sorted(models)
    prompt = prompts[0] if prompts else first_user
    s.role = classify(prompt, folder)
    if not re.fullmatch(r"T\d+", folder):  # triage folder: the card is named in the prompt
        m = re.match(r"# Task (\S+):", prompt)
        s.card = m.group(1) if m else folder
    return s


# ---------------------------------------------------------------- queue state
def _state_rows(root: Path) -> tuple[list[dict], list[dict]]:
    """Tasks and events from tasks.json/events.json or state.db, at root or root/state."""
    for d in (root, root / "state"):
        if (d / "tasks.json").exists():
            events = json.loads((d / "events.json").read_text()) if (d / "events.json").exists() else []
            return json.loads((d / "tasks.json").read_text()), events
        if (d / "state.db").exists():
            conn = sqlite3.connect(f"file:{d / 'state.db'}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                return ([dict(r) for r in conn.execute("SELECT * FROM tasks")],
                        [dict(r) for r in conn.execute("SELECT * FROM events ORDER BY id")])
            finally:
                conn.close()
    return [], []


def load_cards(root: Path) -> dict[str, Card]:
    tasks, events = _state_rows(root)
    cards = {t["id"]: Card(t["id"], t.get("title", ""), t.get("status", ""), t.get("attempts", 0),
                           bool(t.get("triaged")), bool(t.get("test_first")), t.get("cost_usd"))
             for t in tasks}
    for e in events:
        c = cards.get(e.get("task_id"))
        if not c:
            continue
        kind, detail = e.get("kind", ""), e.get("detail", "") or ""
        if kind == "gate":
            c.gate_failures += 1
        elif kind in ("review", "review-override") and (kind == "review-override" or detail.startswith("review ")):
            verdict = "approved" if kind == "review" and detail.startswith("review approved") else "rejected"
            c.reviews += 1
            c.first_review = c.first_review or verdict
        elif kind == "revise":
            c.revise_rounds += 1
        elif kind == "blocked":
            c.blocked += 1
        elif kind == "triage" and detail.startswith("triage → split into"):
            c.split_into = detail.split("split into", 1)[1].strip()
    return cards


def build(root: Path) -> Report:
    root = Path(root)
    tdir = root / "agent-transcripts" if (root / "agent-transcripts").is_dir() else root
    results: list[ToolResult] = []
    sessions = [s for p in sorted(tdir.rglob("*.jsonl")) if (s := read_session(p, tdir, results))]
    cards = load_cards(root)
    for s in sessions:
        if s.role != "interactive":
            cards.setdefault(s.card, Card(s.card))
    return Report(sessions, cards, results)


# ---------------------------------------------------------------- summaries
def _sum(sessions: list[Session]) -> dict:
    return {"sessions": len(sessions), "turns": sum(s.turns for s in sessions),
            "input": sum(s.input for s in sessions), "output": sum(s.output for s in sessions),
            "cache_read": sum(s.cache_read for s in sessions),
            "cache_write": sum(s.cache_write for s in sessions),
            "write_1h": sum(s.write_1h for s in sessions),
            "tokens": sum(s.tokens for s in sessions), "usd": round(sum(s.usd for s in sessions), 4),
            "cli_usd": round(sum(s.cli_usd or 0.0 for s in sessions), 4),
            "cold_start": sum(s.cold_start for s in sessions), "cold_mid": sum(s.cold_mid for s in sessions),
            "peak": max((s.peak for s in sessions), default=0)}


def card_rows(r: Report) -> list[dict]:
    by_card = defaultdict(list)
    for s in r.sessions:
        if s.role != "interactive":
            by_card[s.card].append(s)
    rows = []
    for cid in sorted(r.cards, key=lambda c: (len(c), c)):
        c, ss = r.cards[cid], by_card.get(cid, [])
        roles = defaultdict(lambda: [0, 0])
        for s in ss:
            roles[s.role][0] += 1
            roles[s.role][1] += s.turns
        rows.append({**asdict(c), **_sum(ss), "transcripts": bool(ss),
                     "by_role": {k: {"sessions": v[0], "turns": v[1]} for k, v in roles.items()},
                     "resumed_tokens": sum(sum(s.round_tokens[1:]) for s in ss)})
    return rows


def group(r: Report, key) -> dict[str, dict]:
    groups = defaultdict(list)
    for s in r.sessions:
        groups[key(s)].append(s)
    return {k: _sum(v) for k, v in sorted(groups.items(), key=lambda kv: -sum(s.tokens for s in kv[1]))}


def resume_costs(r: Report) -> dict[str, dict]:
    """Tokens spent in rounds that a resume prompt started, by what the prompt was."""
    out = defaultdict(lambda: {"rounds": 0, "tokens": 0})
    for s in r.sessions:
        if s.role == "interactive":
            continue  # its later prompts are the human's, not resumes
        for kind, toks in zip(s.resumes, s.round_tokens[1:]):
            out[kind]["rounds"] += 1
            out[kind]["tokens"] += toks
    return dict(out)


def cap_what_if(r: Report, cap: int) -> dict:
    """Tool results over `cap` chars: how much text a cap would cut and how often it was re-read."""
    over = [t for t in r.results if t.chars > cap]
    return {"cap": cap, "results": len(over), "chars_cut": sum(t.chars - cap for t in over),
            "context_tokens_cut": sum((t.chars - cap) // 4 * t.turns_after for t in over),
            "by_tool": {k: sum(1 for t in over if t.tool == k) for k in sorted({t.tool for t in over})}}


def to_dict(r: Report, cap: int = 8000) -> dict:
    agents = [s for s in r.sessions if s.role != "interactive"]
    recorded = [c.recorded_usd for c in r.cards.values() if c.recorded_usd is not None]
    return {"totals": {**_sum(agents), "recorded_usd": round(sum(recorded), 2) if recorded else None},
            "interactive": _sum([s for s in r.sessions if s.role == "interactive"]),
            "by_role": group(r, lambda s: s.role), "by_model": group(r, lambda s: ",".join(s.models)),
            "cards": card_rows(r), "resume_rounds": resume_costs(r), "tool_output_cap": cap_what_if(r, cap),
            "sessions": [{**asdict(s), "tokens": s.tokens, "cache_write": s.cache_write} for s in r.sessions],
            "largest_tool_results": [asdict(t) for t in sorted(r.results, key=lambda t: -t.chars)[:20]]}


# ---------------------------------------------------------------- output
def _fmt(n: float) -> str:
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"


def _usd(x: float | None) -> str:
    return "" if x is None else f"${x:,.2f}"


def _table(head: list[str], rows: list[list], md: bool) -> str:
    rows = [[str(c) for c in row] for row in rows]
    if md:
        return "\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
                         + ["| " + " | ".join(row) + " |" for row in rows])
    widths = [max(len(x) for x in col) for col in zip(head, *rows)]
    return "\n".join("  ".join(x.ljust(w) if i == 0 else x.rjust(w) for i, (x, w) in enumerate(zip(row, widths)))
                     for row in [head, *rows])


def render(d: dict, md: bool = False) -> str:
    h = (lambda t: f"\n## {t}\n") if md else (lambda t: f"\n== {t} ==\n")
    t, i = d["totals"], d["interactive"]
    out = [h("Totals (Orchestra agents)")]
    out.append(f"{t['sessions']} sessions · {t['turns']} turns · {_fmt(t['tokens'])} tokens "
               f"(cache read {t['cache_read'] / max(t['tokens'], 1):.0%}, cache write "
               f"{t['cache_write'] / max(t['tokens'], 1):.0%}, 1-hour writes {_fmt(t['write_1h'])}) · "
               f"priced from transcripts {_usd(t['usd'])} · CLI's final totals {_usd(t['cli_usd'])}"
               + (f" · recorded in tasks.cost_usd {_usd(t['recorded_usd'])}" if t["recorded_usd"] is not None else ""))
    out.append(f"\nCold starts: {_fmt(t['cold_start'])} written at session start, {_fmt(t['cold_mid'])} mid-session.")
    if i["sessions"]:
        out.append(f"\nInteractive sessions (not in Orchestra's spend): {i['sessions']} · {i['turns']} turns · "
                   f"{_fmt(i['tokens'])} tokens · peak context {_fmt(i['peak'])} · priced {_usd(i['usd'])}")
    out.append(h("Cards"))
    out.append(_table(
        ["card", "status", "recorded", "CLI", "priced", "tokens", "c.write", "cold", "peak", "sessions (role:n/turns)",
         "gate fails", "reviews", "1st review", "revise", "resumed tok"],
        [[c["id"], c["status"] + (f" → {c['split_into']}" if c["split_into"] else ""), _usd(c["recorded_usd"]),
          _usd(c["cli_usd"]) if c["transcripts"] else "-",
          _usd(c["usd"]) if c["transcripts"] else "-", _fmt(c["tokens"]), _fmt(c["cache_write"]),
          _fmt(c["cold_start"]), _fmt(c["peak"]),
          " ".join(f"{k[:4]}:{v['sessions']}/{v['turns']}" for k, v in sorted(c["by_role"].items())),
          c["gate_failures"], c["reviews"], c["first_review"], c["revise_rounds"], _fmt(c["resumed_tokens"])]
         for c in d["cards"]], md))
    for title, key in (("By role", "by_role"), ("By model", "by_model")):
        out.append(h(title))
        out.append(_table([key[3:], "sessions", "turns", "tokens", "c.read", "c.write", "cold start", "priced"],
                          [[k, v["sessions"], v["turns"], _fmt(v["tokens"]), _fmt(v["cache_read"]),
                            _fmt(v["cache_write"]), _fmt(v["cold_start"]), _usd(v["usd"])]
                           for k, v in d[key].items()], md))
    out.append(h("Rounds started by a resume prompt"))
    out.append(_table(["prompt", "rounds", "tokens"], [[k, v["rounds"], _fmt(v["tokens"])]
                                                        for k, v in d["resume_rounds"].items()], md))
    out.append(h("Sessions"))
    out.append(_table(["session", "card", "role", "model", "turns", "tokens", "c.write", "peak", "priced", "CLI",
                       "resumed by", "synthetic"],
                      [[s["path"][:40], "-" if s["role"] == "interactive" else s["card"], s["role"], ",".join(m.replace("claude-", "") for m in s["models"]),
                        s["turns"], _fmt(s["tokens"]), _fmt(s["cache_write"]), _fmt(s["peak"]), _usd(s["usd"]),
                        _usd(s["cli_usd"]), "" if s["role"] == "interactive" else ",".join(s["resumes"]),
                        len(s["synthetic"])]
                       for s in sorted(d["sessions"], key=lambda s: -s["tokens"])], md))
    c = d["tool_output_cap"]
    out.append(h(f"Tool output over {c['cap']:,} chars"))
    out.append(f"{c['results']} results ({c['by_tool']}) · {c['chars_cut']:,} chars over the cap · "
               f"~{_fmt(c['context_tokens_cut'])} context tokens if each was re-read on every later turn")
    out.append(_table(["chars", "tool", "session", "later turns"],
                      [[f"{x['chars']:,}", x["tool"], x["path"][:40], x["turns_after"]]
                       for x in d["largest_tool_results"][:10]], md))
    return "\n".join(out) + "\n"


def to_csv(d: dict) -> str:
    """One row per session, with its card's outcome."""
    cards = {c["id"]: c for c in d["cards"]}
    cols = ["path", "card", "role", "models", "start", "end", "turns", "input", "output", "cache_read",
            "write_5m", "write_1h", "tokens", "peak", "usd", "cli_usd", "cold_start", "cold_mid", "resumes"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols + ["card_status", "card_recorded_usd", "card_first_review", "card_revise_rounds"])
    for s in d["sessions"]:
        c = cards.get(s["card"], {})
        w.writerow([";".join(s[k]) if isinstance(s[k], list) else s[k] for k in cols]
                   + [c.get("status", ""), c.get("recorded_usd", ""), c.get("first_review", ""),
                      c.get("revise_rounds", "")])
    return buf.getvalue()
