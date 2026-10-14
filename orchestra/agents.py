"""The agent roles, each a fresh Agent SDK session with a structured contract.

  Planner  - read-only; turns a goal into a dependency graph of small, scoped tasks.
  Critic   - read-only; checks the plan and returns a corrected one.
  Worker   - edits files inside one worktree; reports a structured handoff.
  Test writer - writes locked acceptance tests before the worker (test-first cards).
  Reviewer - separate context, no edits; judges the diff against the task spec.
  Validator - checks one blocking review finding.
  Triage   - read-only; before a card is blocked, picks rewrite / escalate / split / human.

Every agent returns JSON validated against a schema (`output_format`), so the
orchestrator treats each output as a *candidate* to check, never as prose to trust.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .config import Config
from .graph import GRAPH_GUIDE, mcp_tool_names
from .limits import LimitSignal, looks_like_limit
from .guards import make_read_guard, make_reviewer_guard, make_worker_guard
from .store import Task

# ---------------------------------------------------------------- schemas
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "short slug, unique, e.g. 'shift-model'"},
                    "title": {"type": "string"},
                    "spec": {"type": "string"},
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                    "non_goals": {"type": "array", "items": {"type": "string"}},
                    "files_scope": {"type": "array", "items": {"type": "string"},
                                    "description": "paths or globs this task may touch"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "complexity": {"type": "string", "enum": ["small", "large"]},
                    "estimated_lines": {"type": "integer", "description":
                                        "changed lines the diff will need (code, tests, docs, committed data)"},
                    "size_notes": {"type": "string", "description": "what dominates the size"},
                    "model": {"type": "string", "description":
                              "optional worker model override; omit unless the card clearly needs it"},
                    "test_first": {"type": "boolean", "description":
                                   "true if the card changes money, stored data, or auth/permissions"},
                },
                "required": ["key", "title", "spec", "acceptance_criteria", "non_goals",
                             "files_scope", "depends_on", "complexity", "estimated_lines"],
            },
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "tasks", "open_questions"],
}

WORK_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "needs_decision", "blocked"]},
        "decision": {
            "type": "object",
            "description": "only when status is needs_decision",
            "properties": {
                "question": {"type": "string"},
                "options": {"type": "array", "items": {"type": "string"}},
                "recommendation": {"type": "string"},
                "why_it_matters": {"type": "string"},
            },
            "required": ["question", "options", "recommendation", "why_it_matters"],
        },
        "summary": {"type": "string", "description": "what changed and why, for the reviewer"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "tests": {"type": "string", "description": "tests added/run and their result"},
        "blocker": {"type": "string", "description": "if blocked: what is missing or ambiguous"},
    },
    "required": ["status", "summary", "files_changed", "tests", "blocker"],
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "reject"]},
        "criteria": {"type": "array", "items": {
            "type": "object",
            "properties": {"criterion": {"type": "string"}, "met": {"type": "boolean"},
                           "evidence": {"type": "string"}},
            "required": ["criterion", "met", "evidence"]}},
        "scope_creep": {"type": "array", "items": {"type": "string"}},
        "issues": {"type": "array", "items": {
            "type": "object",
            "properties": {"severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
                           "file": {"type": "string"}, "issue": {"type": "string"}},
            "required": ["severity", "file", "issue"]}},
        "feedback": {"type": "string", "description": "concrete instructions for the next attempt"},
        "fixable": {"type": "boolean", "description":
                    "true if the problems can be fixed by editing this change; false if the approach "
                    "is wrong and starting over is better (true when approving)"},
    },
    "required": ["verdict", "criteria", "scope_creep", "issues", "feedback", "fixable"],
}

VALIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "confirmed": {"type": "boolean", "description":
                      "false only if you showed the finding is wrong; true if it holds or you can't tell"},
        "evidence": {"type": "string", "description": "what you read or ran, and what it showed"},
    },
    "required": ["confirmed", "evidence"],
}

TESTS_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "blocked"]},
        "test_files": {"type": "array", "items": {"type": "string"}},
        "covered": {"type": "array", "items": {
            "type": "object",
            "properties": {"criterion": {"type": "string"}, "test": {"type": "string"}},
            "required": ["criterion", "test"]}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "blocker": {"type": "string"},
    },
    "required": ["status", "test_files", "covered", "assumptions", "blocker"],
}

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["rewrite", "escalate", "split", "human"]},
        "reason": {"type": "string", "description": "why the card failed and why this action fixes it"},
        "card": {
            "type": "object",
            "description": "rewrite only: the fields to replace (omit fields that stay the same)",
            "properties": {
                "title": {"type": "string"},
                "spec": {"type": "string"},
                "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                "non_goals": {"type": "array", "items": {"type": "string"}},
                "files_scope": {"type": "array", "items": {"type": "string"}},
                "regenerate_tests": {"type": "boolean", "description":
                                     "test-first cards: true if a locked acceptance test is wrong and "
                                     "must be written again"},
            },
        },
        "model": {"type": "string", "description": "escalate only: the stronger model, e.g. opus"},
        "cards": {"type": "array", "description": "split only: 2-4 smaller cards, inside the original "
                  "files_scope; depends_on uses their keys",
                  "items": PLAN_SCHEMA["properties"]["tasks"]["items"]},
        "note_for_human": {"type": "string", "description":
                           "one or two sentences for the human lead: what is wrong and what they should decide"},
    },
    "required": ["action", "reason", "note_for_human"],
}

CRITIC_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["ok", "revise"]},
        "issues": {"type": "array", "items": {
            "type": "object",
            "properties": {"task_key": {"type": "string"}, "problem": {"type": "string"},
                           "fix": {"type": "string"}},
            "required": ["task_key", "problem", "fix"]}},
        "plan": PLAN_SCHEMA,
    },
    "required": ["verdict", "issues", "plan"],
}


# ---------------------------------------------------------------- prompts
PLANNER_SYSTEM = """You are the planning lead in a multi-agent coding system.
You decompose a large goal into tasks that independent workers execute IN PARALLEL,
each in its own git worktree, with no shared conversation. A worker only sees its task card.

Rules for good tasks:
- Vertical slices that are independently testable. Each task should be reviewable as one
  small diff that fits the diff-size limit (see Sizing).
- files_scope must be specific (paths or globs). Two tasks that can run at the same time
  must NOT share files. If they must, add a depends_on edge.
- Shared foundations (schemas, types, interfaces, migrations) come first; dependents list them.
- acceptance_criteria are concrete and checkable (behaviour + tests), never "works well".
- non_goals explicitly name tempting adjacent work the worker must NOT do.
- complexity "large" only for multi-file work needing broad codebase reading.
- Explore the repo first (read-only) so paths and conventions are real, not guessed.
- Set test_first true on cards that change money (pricing, fees, payouts, refunds), stored data
  (migrations, deletes, backfills) or auth/permissions: their acceptance tests are written by a
  separate agent before implementation, so make those criteria precise and testable.
- If the goal is ambiguous, still produce the best plan and list open_questions."""

CRITIC_SYSTEM = """You are the plan critic in a multi-agent coding system. A planner drafted the
task graph below; workers will implement each card in parallel with no shared conversation.
Check it against the goal and the actual repository (read-only):
- every part of the goal is covered, nothing beyond it is added;
- files_scope paths are real (or clearly new files in existing folders) and specific;
- cards that would touch the same files or call into each other are ordered with depends_on;
- each card is one reviewable diff that fits the Sizing rules; check estimated_lines against the
  repository and split cards that don't fit;
- acceptance criteria are concrete and testable; non_goals name tempting extras;
- test_first is set on cards changing money, stored data or auth/permissions.
Return verdict "ok" with the plan unchanged if it is sound; otherwise "revise", list each
problem with its fix, and return the full corrected plan (same keys where cards are unchanged)."""

TRIAGE_SYSTEM = """You triage a task card in a multi-agent coding system that is about to be
marked blocked: its attempts are used up, or its worker reported it can't proceed. Read the card,
its history and the repository (read-only), find the real cause, and choose ONE action:
- rewrite: the card is unclear, contradictory, mis-scoped (files_scope misses files the work must
  touch) or its criteria can't be tested, and the repository shows how to fix that. Return only the
  fields that change. Keep the card's intent; never add features beyond it.
- escalate: the card is clear and right-sized, but the worker model kept getting it wrong
  (subtle bugs, wrong approach each time). Name a stronger model (usually opus).
- split: the card is too big for one reviewable diff (diff-size failures, partial work each time).
  Return 2-4 smaller cards in the plan format, every files_scope inside the original card's
  files_scope, ordered with depends_on (by key). Together they must cover the original card.
- human: anything a rewrite can't fix: a product or business decision, missing access, secrets
  or services, a broken environment or flaky tests, or a card that should not be done.
For a test-first card, changing the spec or acceptance criteria also discards its locked
acceptance tests so they are written again; if a locked test itself is wrong, fix the criterion
it came from or set regenerate_tests.
Prefer the smallest change that would make the next attempt succeed. If no automatic action is
clearly right, choose human. Always write note_for_human."""

WORKER_APPEND = """
You are a worker agent in a multi-agent orchestrator. You own exactly ONE task card.
- Work only inside the current directory (your private git worktree).
- Do not run git commands that change state; the orchestrator commits, rebases and merges.
- Implement exactly the acceptance criteria. Do NOT add features listed in non-goals or
  anything not asked for. Smaller, focused diffs get approved; scope creep gets rejected.
- Only edit files inside the task's files_scope (plus lockfiles if you add a dependency).
- Add or update tests that prove each acceptance criterion. Run the relevant tests.
- If you hit a choice the card doesn't settle and that changes behaviour users or money
  depend on (a business rule, pricing, data migration, a public API shape, deleting data),
  do NOT guess: stop early and report status "needs_decision" with one clear question,
  2-4 concrete options, your recommendation, and why it matters. Keep any work done so far;
  you will resume this same session with the answer. For minor choices (naming, internal
  structure), decide yourself and mention it in the summary.
- If the task is impossible as written (missing access, contradictory requirements),
  report status "blocked" with a precise blocker.
- Prefer fast, targeted test runs over full suites; don't loop on slow commands.
- Before reporting "done", run the checks listed on the card and fix what fails; the
  orchestrator runs exactly those commands next.
Your final answer must be the structured report."""

SANDBOX_NOTE = """
Shell commands run in an OS sandbox: you can write only inside this directory, temp and
package caches, and reach only package registries and GitHub. If a command fails with
"Operation not permitted" or a network error, don't work around it; finish what you can and
mention it in your report (or report "blocked" if the task needs it)."""

# The orchestrator's message when gates fail; also used to keep only the latest one on the card.
GATE_FIX_PREFIX = "Your last change failed automated checks."
LEGACY_DECISION_PREFIX = "Decision from the lead"

REVIEWER_SYSTEM = """You are an independent code reviewer in a multi-agent system. You did not
write this code. Judge the diff strictly against the task card.
Reject if: any acceptance criterion is unmet or untested; there is scope creep (features,
refactors or files beyond the card); it introduces bugs, security issues, or breaks existing
patterns of the codebase; business logic is wrong.
Approve only when it is correct, scoped, tested, and fits the architecture. Minor style nits
alone are not a reason to reject. Read surrounding code to check fit. Give feedback the next
worker can act on directly.
When you can, check behaviour instead of guessing: run the relevant tests or a short script
(read-only; never edit files, never run git commands that change state). Report only real
problems, each with where it is; every blocking finding will be independently verified.
Set fixable to false only when the approach itself is wrong and starting over is better."""

VALIDATOR_SYSTEM = """You verify one finding from a code review before it can block a change.
Read the code and, when useful, run tests or a short script (read-only; never edit files or
run state-changing git commands). Return confirmed=false only if you can show the finding is
wrong (the criterion is met, the bug can't happen, the change is within the card's scope),
with the evidence. If it holds, or you can't tell, return confirmed=true."""

TEST_WRITER_APPEND = """
You write the acceptance tests for ONE task card BEFORE anyone implements it.
- Write tests only: new or updated test files that follow the repository's test conventions.
  Do not change application code, fixtures outside test folders, or configuration.
- One or more tests per acceptance criterion; they must fail now (the behaviour doesn't exist
  yet) and pass once the card is implemented correctly. Test behaviour through public
  interfaces, not implementation details.
- Run them to confirm they fail for the right reason (missing behaviour, not syntax errors).
- If a criterion is ambiguous, test the clearest reading and list it under assumptions; do not
  ask questions. Report "blocked" only if the card can't be tested at all.
- These files will be locked: the implementer can't edit them.
Your final answer must be the structured report."""


def sizing_rules(cfg: Config) -> str:
    """How big a card may be, from the project's diff gate (planner, critic and triage)."""
    limit = cfg.max_diff_lines
    split_at = int(limit * cfg.split_above)
    return f"""

## Sizing
The diff gate rejects a card whose change is over {limit} changed lines (lockfiles and generated
files listed in diff_exclude don't count; tests, docs and committed data do). A card rejected for
size is thrown away and split, so size cards before work starts:
- Give every card estimated_lines: the code, tests, docs and committed data its diff will need.
  Look at the files it touches to estimate; don't guess low.
- Split any card estimated above {split_at} lines into cards ordered with depends_on.
- Generated or example data (JSON payloads, fixtures, snapshots, seed data) counts in full. Keep
  committed examples small and hand-trimmed, and build full data at test time instead.
- Say in size_notes what dominates the size."""


def model_family(model: str) -> str:
    m = (model or "").lower()
    return next((f for f in ("opus", "sonnet", "haiku") if f in m), m)


def pick_reviewer_model(cfg: Config, worker_model: str) -> str:
    """With reviewer_rule = "opposite", never review a change with its author's model family."""
    reviewer = cfg.reviewer_model
    if cfg.review.reviewer_rule != "opposite" or model_family(reviewer) != model_family(worker_model):
        return reviewer
    return "sonnet" if model_family(worker_model) == "opus" else "opus"


def blocking_findings(v: dict) -> list[dict]:
    """The findings that make a review a rejection, in a form a validator can check."""
    out = [{"kind": "criterion", "text": c.get("criterion", ""), "detail": c.get("evidence", ""), "ref": c}
           for c in v.get("criteria", []) if not c.get("met")]
    out += [{"kind": "issue", "text": i.get("issue", ""), "detail": f"[{i.get('severity')}] {i.get('file', '')}",
             "ref": i} for i in v.get("issues", []) if i.get("severity") in ("blocker", "major")]
    out += [{"kind": "scope", "text": s, "detail": "reported as scope creep", "ref": s}
            for s in v.get("scope_creep", [])]
    return out


def drop_refuted(v: dict, refuted: list[dict]) -> dict:
    """The verdict without findings a validator disproved; approve if nothing blocking is left."""
    ids = {id(f["ref"]) for f in refuted}
    evidence = {id(f["ref"]): f.get("evidence", "") for f in refuted}
    out = dict(v)
    out["criteria"] = [({**c, "met": True, "evidence": f"validator: {evidence[id(c)]}"} if id(c) in ids else c)
                       for c in v.get("criteria", [])]
    out["issues"] = [i for i in v.get("issues", []) if id(i) not in ids]
    out["scope_creep"] = [s for s in v.get("scope_creep", []) if not any(s is f["ref"] for f in refuted)]
    if refuted and not blocking_findings(out):
        out["verdict"] = "approve"
    return out


def pick_worker_model(cfg: Config, task: Task) -> str:
    """Card override > escalation after a failed attempt > complexity default."""
    if task.model:
        return task.model
    if task.complexity == "large":
        return cfg.large_worker_model
    if cfg.escalate_on_retry and task.attempts > 1:
        return cfg.large_worker_model
    return cfg.small_worker_model


def check_verdict(v: dict) -> tuple[bool, list[str]]:
    """An approval only counts if the reviewer's own findings agree with it."""
    reasons = []
    unmet = [c.get("criterion", "?") for c in v.get("criteria", []) if not c.get("met")]
    if unmet:
        reasons.append(f"{len(unmet)} acceptance criteria not met")
    serious = [i for i in v.get("issues", []) if i.get("severity") in ("blocker", "major")]
    if serious:
        reasons.append(f"{len(serious)} blocker/major issue(s)")
    if v.get("scope_creep"):
        reasons.append("scope creep reported")
    if v.get("verdict") != "approve":
        return False, reasons or ["reviewer rejected"]
    return not reasons, reasons


def _card_feedback(feedback: list[str], keep: int = 3, cap: int = 2000) -> list[str]:
    """Latest notes for the card: only the most recent gate-failure log, then the last few."""
    gate_idx = [i for i, f in enumerate(feedback) if f.startswith(GATE_FIX_PREFIX)]
    kept = [f for i, f in enumerate(feedback)
            if not f.startswith(LEGACY_DECISION_PREFIX)
            and (not f.startswith(GATE_FIX_PREFIX) or i == gate_idx[-1])]
    return [f if len(f) <= cap else f[:cap] + "\n... [truncated]" for f in kept[-keep:]]


def task_card(t: Task, checks: list[str] | None = None) -> str:
    def bullets(xs: list[str]) -> str:
        return "\n".join(f"- {x}" for x in xs) or "- (none)"
    card = (f"# Task {t.id}: {t.title}\n\n## Spec\n{t.spec}\n\n"
            f"## Acceptance criteria\n{bullets(t.acceptance)}\n\n"
            f"## Non-goals (do NOT do these)\n{bullets(t.non_goals)}\n\n"
            f"## Files you may touch\n{bullets(t.files_scope) if t.files_scope else '- (unrestricted)'}\n")
    decisions = [f"- Q: {d.get('question', '')}\n  A: {d.get('answer', '')}" for d in t.decisions]
    decisions += [f"- {f[len(LEGACY_DECISION_PREFIX):].lstrip(' —-:')}" for f in t.feedback
                  if f.startswith(LEGACY_DECISION_PREFIX)]  # answers stored by older versions
    if decisions:
        card += "\n## Decisions already made (binding; do not ask again)\n" + "\n".join(decisions) + "\n"
    if t.locked_tests:
        card += ("\n## Acceptance tests (written first by another agent; LOCKED)\n"
                 "Make the code pass these. You can't edit them; if one is wrong, report \"blocked\".\n"
                 + "\n".join(f"- `{p}`" for p in t.locked_tests) + "\n")
    if checks:
        card += ("\n## Checks the orchestrator will run (run them before reporting done)\n"
                 + "\n".join(f"- `{c}`" for c in checks) + "\n")
    notes = _card_feedback(t.feedback)
    if notes:
        card += ("\n## Feedback from previous attempts (address all of it)\n" +
                 "\n\n".join(f"### Attempt note {i+1}\n{f}" for i, f in enumerate(notes)))
    return card


def agent_env() -> dict[str, str]:
    """Environment for every agent session. Auto memory is per repository and shared by all
    worktrees, so parallel agents (and your own sessions) would read each other's notes."""
    return {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}


def sandbox_settings(cfg: Config) -> dict | None:
    """Claude Code sandbox settings for worker Bash commands, or None when disabled.
    The SDK merges this dict into --settings verbatim, so CLI keys missing from its
    SandboxSettings type (failIfUnavailable, filesystem) still apply."""
    s = cfg.sandbox
    if not s.enabled:
        return None
    return {
        "enabled": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,   # no dangerouslyDisableSandbox escape hatch
        "failIfUnavailable": True,           # never fall back to running unsandboxed
        "excludedCommands": list(s.excluded_commands),
        "network": {"allowedDomains": list(s.allowed_domains),
                    "allowLocalBinding": bool(s.allow_local_binding)},
        "filesystem": {"allowWrite": list(s.allow_write), "denyRead": list(s.deny_read)},
    }


# ---------------------------------------------------------------- results
@dataclass
class AgentResult:
    data: dict[str, Any]
    cost_usd: float  # the session's running total: a resumed session includes its earlier runs
    ok: bool = True
    error: str = ""
    tokens: int = 0  # input + output + cache tokens across all models in the call
    session_id: str | None = None
    limited: bool = False            # cut off by a usage/rate limit
    resets_at: float | None = None   # when that limit resets (unix seconds), if known


def _tokens(model_usage: dict | None) -> int:
    total = 0
    for u in (model_usage or {}).values():
        total += sum(int(u.get(k, 0) or 0) for k in
                     ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"))
    return total


class Runner(Protocol):
    async def plan(self, cfg: Config, goal: str, cwd: Path | None = None,
                   graph: Path | None = None) -> AgentResult: ...
    async def critique_plan(self, cfg: Config, goal: str, plan: dict, cwd: Path | None = None,
                            graph: Path | None = None) -> AgentResult: ...
    async def work(self, cfg: Config, task: Task, worktree: Path, graph: Path | None = None,
                   resume: str | None = None, env: dict | None = None) -> AgentResult: ...
    async def write_tests(self, cfg: Config, task: Task, worktree: Path, graph: Path | None = None,
                          resume: str | None = None, env: dict | None = None) -> AgentResult: ...
    async def review(self, cfg: Config, task: Task, worktree: Path, diff: str,
                     work_report: dict[str, Any], gate_log: str, impact: str = "",
                     graph: Path | None = None, note: str = "", model: str | None = None,
                     env: dict | None = None) -> AgentResult: ...
    async def validate(self, cfg: Config, task: Task, worktree: Path, diff: str, finding: dict,
                       env: dict | None = None) -> AgentResult: ...
    async def triage(self, cfg: Config, task: Task, context: str, cwd: Path,
                     graph: Path | None = None) -> AgentResult: ...


def _graph_opts(cfg: Config, graph: Path | None, base_tools: list[str]) -> tuple[dict, list[str], str]:
    """MCP server config, auto-approved tools, and prompt guide for the graph, if any."""
    if not graph:
        return {}, base_tools, ""
    servers = {"graph": {"type": "stdio", "command": cfg.graph.interpreter,
                         "args": ["-m", "graphify.serve", str(graph)]}}
    return servers, base_tools + mcp_tool_names(), GRAPH_GUIDE


# ---------------------------------------------------------------- real runner
class ClaudeRunner:
    """Runs each role as an independent Claude Agent SDK session."""

    def __init__(self, verbose: bool = False, on_limit: Callable[[LimitSignal], None] | None = None):
        self.verbose = verbose
        self.on_limit = on_limit  # receives every usage-limit update from every session

    async def _run(self, prompt: str, options: Any, label: str) -> AgentResult:
        from claude_agent_sdk import (AssistantMessage, RateLimitEvent, ResultMessage, TextBlock,
                                      ToolUseBlock, query)
        result: Any = None
        session_id: str | None = None
        limited, resets_at = False, None
        try:
            async for msg in query(prompt=prompt, options=options):
                session_id = getattr(msg, "session_id", None) or session_id
                if isinstance(msg, RateLimitEvent):
                    info = msg.rate_limit_info
                    sig = LimitSignal(info.status, info.utilization, info.resets_at, info.rate_limit_type)
                    if info.status == "rejected":
                        limited, resets_at = True, info.resets_at
                    if self.on_limit:
                        self.on_limit(sig)
                elif isinstance(msg, ResultMessage):
                    result = msg
                elif isinstance(msg, AssistantMessage):
                    if msg.error == "rate_limit":
                        limited = True
                    if self.verbose:
                        for b in msg.content:
                            if isinstance(b, ToolUseBlock):
                                print(f"  [{label}] {b.name} {json.dumps(b.input)[:120]}")
                            elif isinstance(b, TextBlock) and b.text.strip():
                                print(f"  [{label}] {b.text.strip()[:160]}")
        except Exception as e:  # SDK raises ResultError/ProcessError on failed runs
            raw = getattr(e, "data", None) or {}
            cost = raw.get("total_cost_usd") or getattr(result, "total_cost_usd", 0.0) or 0.0
            reason = getattr(e, "terminal_reason", None) or getattr(e, "subtype", None) or ""
            text = f"{type(e).__name__} {reason}: {str(e)[:800]}"
            limited = limited or getattr(e, "api_error_status", None) == 429 or looks_like_limit(text)
            return AgentResult({}, float(cost), ok=False, error=text, limited=limited,
                               resets_at=resets_at, session_id=raw.get("session_id") or session_id)
        if result is None:
            return AgentResult({}, 0.0, ok=False, error="no result message", limited=limited,
                               resets_at=resets_at, session_id=session_id)
        cost = result.total_cost_usd or 0.0
        tokens = _tokens(result.model_usage)
        data = result.structured_output
        session_id = result.session_id or session_id
        if result.is_error or not isinstance(data, dict):
            reason = result.terminal_reason or result.subtype
            text = f"{reason}: {(result.result or '')[:500]} {result.errors or ''}"
            limited = limited or result.api_error_status == 429 or \
                (result.terminal_reason == "api_error" and looks_like_limit(text))
            return AgentResult({}, cost, ok=False, tokens=tokens, error=text, limited=limited,
                               resets_at=resets_at, session_id=session_id)
        return AgentResult(data, cost, tokens=tokens, session_id=session_id)

    async def plan(self, cfg: Config, goal: str, cwd: Path | None = None,
                   graph: Path | None = None) -> AgentResult:
        from claude_agent_sdk import ClaudeAgentOptions
        base = ["Read", "Glob", "Grep"]
        servers, allowed, guide = _graph_opts(cfg, graph, base)
        from claude_agent_sdk import HookMatcher
        root = cwd or cfg.repo
        opts = ClaudeAgentOptions(
            cwd=str(root), model=cfg.planner_model,
            system_prompt=PLANNER_SYSTEM + sizing_rules(cfg) + guide + _context(cfg),
            tools=base, allowed_tools=allowed, mcp_servers=servers, strict_mcp_config=True,
            setting_sources=["project"], max_turns=60, session_id=_new_session(),
            max_budget_usd=cfg.task_budget_usd * 2, env=agent_env(),
            hooks={"PreToolUse": [HookMatcher(hooks=[make_read_guard(Path(root))])]},
            output_format={"type": "json_schema", "schema": PLAN_SCHEMA},
        )
        return await self._run(f"Goal:\n{goal}\n\nExplore the repository, then produce the plan.",
                               opts, "planner")

    def _editor_options(self, cfg: Config, task: Task, worktree: Path, graph: Path | None,
                        resume: str | None, env: dict | None, model: str, append: str,
                        schema: dict, locked: list[str] | None = None, stop_gate: bool = False):
        """Options for an agent that edits files in the worktree (worker, test writer)."""
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
        from .gates import make_stop_gate
        tools = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]
        servers, allowed, guide = _graph_opts(cfg, graph, tools)
        sandbox = sandbox_settings(cfg)
        hooks = {"PreToolUse": [HookMatcher(hooks=[make_worker_guard(worktree, locked)])]}
        fast = [g for g in cfg.gates if g.fast]
        if stop_gate and cfg.gates_on_stop and fast:
            hooks["Stop"] = [HookMatcher(hooks=[make_stop_gate(fast, worktree, env=env)],
                                         timeout=sum(g.timeout_s for g in fast) + 30)]
        return ClaudeAgentOptions(
            cwd=str(worktree), model=model,
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": append + (SANDBOX_NOTE if sandbox else "") + guide + _context(cfg)},
            tools=tools, allowed_tools=allowed, permission_mode="acceptEdits",
            mcp_servers=servers, strict_mcp_config=True,
            setting_sources=["project"],  # picks up the repo's CLAUDE.md
            max_turns=cfg.worker_max_turns, max_budget_usd=cfg.task_budget_usd,
            env={**agent_env(), **(env or {})}, sandbox=sandbox, hooks=hooks,
            output_format={"type": "json_schema", "schema": schema},
            resume=task.session_id if (resume and task.session_id) else None,
            session_id=None if (resume and task.session_id) else _new_session(),
        )

    async def work(self, cfg: Config, task: Task, worktree: Path, graph: Path | None = None,
                   resume: str | None = None, env: dict | None = None) -> AgentResult:
        """Run the worker. With `resume` (and a stored session), continue that session
        with the given message instead of starting cold: the agent keeps its context."""
        opts = self._editor_options(cfg, task, worktree, graph, resume, env, pick_worker_model(cfg, task),
                                    WORKER_APPEND, WORK_SCHEMA, locked=task.locked_tests, stop_gate=True)
        prompt = resume if (resume and task.session_id) else task_card(task, gate_commands(cfg))
        return await self._run(prompt, opts, task.id)

    async def write_tests(self, cfg: Config, task: Task, worktree: Path, graph: Path | None = None,
                          resume: str | None = None, env: dict | None = None) -> AgentResult:
        """Test-first: write failing acceptance tests for the card (its own session id is
        returned; the caller keeps it separate from the worker's)."""
        opts = self._editor_options(cfg, task, worktree, graph, resume, env, cfg.test_writer_model,
                                    TEST_WRITER_APPEND, TESTS_SCHEMA)
        prompt = resume if (resume and task.session_id) else (
            task_card(task) + "\n\nWrite the acceptance tests for this card now (tests only).")
        return await self._run(prompt, opts, f"{task.id}:tests")

    def _checker_options(self, cfg: Config, worktree: Path, graph: Path | None, model: str,
                         system: str, schema: dict, env: dict | None, max_turns: int):
        """Options for an agent that reads and may run commands but never edits (reviewer, validator)."""
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
        shell = cfg.review.reviewer_shell
        base = ["Read", "Glob", "Grep"] + (["Bash"] if shell else [])
        servers, allowed, guide = _graph_opts(cfg, graph, base)
        sandbox = sandbox_settings(cfg) if shell else None
        guard = make_reviewer_guard(worktree) if shell else make_read_guard(worktree)
        return ClaudeAgentOptions(
            cwd=str(worktree), model=model,
            system_prompt=system + (SANDBOX_NOTE if sandbox else "") + guide + _context(cfg),
            tools=base, allowed_tools=allowed, mcp_servers=servers, strict_mcp_config=True,
            setting_sources=["project"], max_turns=max_turns, max_budget_usd=cfg.task_budget_usd,
            session_id=_new_session(), env={**agent_env(), **(env or {})}, sandbox=sandbox,
            hooks={"PreToolUse": [HookMatcher(hooks=[guard])]},
            output_format={"type": "json_schema", "schema": schema},
        )

    async def review(self, cfg: Config, task: Task, worktree: Path, diff: str,
                     work_report: dict[str, Any], gate_log: str, impact: str = "",
                     graph: Path | None = None, note: str = "", model: str | None = None,
                     env: dict | None = None) -> AgentResult:
        impact_block = (f"## Impact (from the code graph: code outside this diff that depends on "
                        f"changed symbols; check these still work)\n{impact}\n\n" if impact else "")
        note_block = f"## Context for this review\n{note}\n\n" if note else ""
        prompt = (f"{task_card(task)}\n\n{note_block}## Worker's report\n```json\n"
                  f"{json.dumps(work_report, indent=2)}\n```\n\n"
                  f"## Automated gates\n{gate_log}\n\n{impact_block}## Diff\n```diff\n{diff}\n```\n\n"
                  "Read surrounding code and run what helps, then give your verdict.")
        opts = self._checker_options(cfg, worktree, graph, model or cfg.reviewer_model, REVIEWER_SYSTEM,
                                     REVIEW_SCHEMA, env, max_turns=40)
        return await self._run(prompt, opts, f"{task.id}:review")

    async def validate(self, cfg: Config, task: Task, worktree: Path, diff: str, finding: dict,
                       env: dict | None = None) -> AgentResult:
        what = {"criterion": "Reviewer says this acceptance criterion is NOT met",
                "issue": "Reviewer reports this blocking problem",
                "scope": "Reviewer says this is scope creep (outside the card)"}[finding["kind"]]
        prompt = (f"{task_card(task)}\n\n## Finding to verify\n{what}:\n{finding['text']}\n"
                  f"({finding.get('detail', '')})\n\n## Diff\n```diff\n{diff}\n```\n\n"
                  "Verify this one finding, then answer.")
        opts = self._checker_options(cfg, worktree, None, cfg.validator_model, VALIDATOR_SYSTEM,
                                     VALIDATE_SCHEMA, env, max_turns=25)
        return await self._run(prompt, opts, f"{task.id}:validate")

    async def triage(self, cfg: Config, task: Task, context: str, cwd: Path,
                     graph: Path | None = None) -> AgentResult:
        """Read-only look at a card that is about to be blocked; returns one action."""
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
        base = ["Read", "Glob", "Grep"]
        servers, allowed, guide = _graph_opts(cfg, graph, base)
        opts = ClaudeAgentOptions(
            cwd=str(cwd), model=cfg.planner_model,
            system_prompt=TRIAGE_SYSTEM + sizing_rules(cfg) + guide + _context(cfg),
            tools=base, allowed_tools=allowed, mcp_servers=servers, strict_mcp_config=True,
            setting_sources=["project"], max_turns=30, session_id=_new_session(),
            max_budget_usd=cfg.task_budget_usd, env=agent_env(),
            hooks={"PreToolUse": [HookMatcher(hooks=[make_read_guard(Path(cwd))])]},
            output_format={"type": "json_schema", "schema": TRIAGE_SCHEMA},
        )
        return await self._run(context + "\n\nInvestigate, then choose the action.", opts, f"{task.id}:triage")

    async def critique_plan(self, cfg: Config, goal: str, plan: dict, cwd: Path | None = None,
                            graph: Path | None = None) -> AgentResult:
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
        base = ["Read", "Glob", "Grep"]
        servers, allowed, guide = _graph_opts(cfg, graph, base)
        root = cwd or cfg.repo
        opts = ClaudeAgentOptions(
            cwd=str(root), model=cfg.planner_model,
            system_prompt=CRITIC_SYSTEM + sizing_rules(cfg) + guide + _context(cfg),
            tools=base, allowed_tools=allowed, mcp_servers=servers, strict_mcp_config=True,
            setting_sources=["project"], max_turns=40, session_id=_new_session(),
            max_budget_usd=cfg.task_budget_usd * 2, env=agent_env(),
            hooks={"PreToolUse": [HookMatcher(hooks=[make_read_guard(Path(root))])]},
            output_format={"type": "json_schema", "schema": CRITIC_SCHEMA},
        )
        prompt = f"Goal:\n{goal}\n\n## Draft plan\n```json\n{json.dumps(plan, indent=2)}\n```"
        return await self._run(prompt, opts, "critic")


def gate_commands(cfg: Config) -> list[str]:
    return [g.cmd for g in cfg.gates]


def _new_session() -> str:
    """Every agent gets its own session id. Without this, a run started from inside a Claude
    Code session (e.g. via /dispatch) inherits CLAUDE_CODE_SESSION_ID, so parallel agents
    would share one session and resumes could land in the wrong conversation."""
    return str(uuid.uuid4())


def _context(cfg: Config) -> str:
    return f"\n\n## Project context\n{cfg.project_context.strip()}" if cfg.project_context.strip() else ""
