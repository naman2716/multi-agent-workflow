"""The three agent roles, each a fresh Agent SDK session with a structured contract.

  Planner  - read-only; turns a goal into a dependency graph of small, scoped tasks.
  Worker   - edits files inside one worktree; reports a structured handoff.
  Reviewer - read-only, separate context; judges the diff against the task spec.

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
from .guards import make_worker_guard
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
                    "model": {"type": "string", "description":
                              "optional worker model override; omit unless the card clearly needs it"},
                },
                "required": ["key", "title", "spec", "acceptance_criteria", "non_goals",
                             "files_scope", "depends_on", "complexity"],
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
    },
    "required": ["verdict", "criteria", "scope_creep", "issues", "feedback"],
}


# ---------------------------------------------------------------- prompts
PLANNER_SYSTEM = """You are the planning lead in a multi-agent coding system.
You decompose a large goal into tasks that independent workers execute IN PARALLEL,
each in its own git worktree, with no shared conversation. A worker only sees its task card.

Rules for good tasks:
- Vertical slices that are independently testable. Each task should be reviewable as one
  small diff (aim for under ~300 changed lines).
- files_scope must be specific (paths or globs). Two tasks that can run at the same time
  must NOT share files. If they must, add a depends_on edge.
- Shared foundations (schemas, types, interfaces, migrations) come first; dependents list them.
- acceptance_criteria are concrete and checkable (behaviour + tests), never "works well".
- non_goals explicitly name tempting adjacent work the worker must NOT do.
- complexity "large" only for multi-file work needing broad codebase reading.
- Explore the repo first (read-only) so paths and conventions are real, not guessed.
- If the goal is ambiguous, still produce the best plan and list open_questions."""

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
Your final answer must be the structured report."""

REVIEWER_SYSTEM = """You are an independent code reviewer in a multi-agent system. You did not
write this code. Judge the diff strictly against the task card.
Reject if: any acceptance criterion is unmet or untested; there is scope creep (features,
refactors or files beyond the card); it introduces bugs, security issues, or breaks existing
patterns of the codebase; business logic is wrong.
Approve only when it is correct, scoped, tested, and fits the architecture. Minor style nits
alone are not a reason to reject. Read surrounding code to check fit. Give feedback the next
worker can act on directly."""


def pick_worker_model(cfg: Config, task: Task) -> str:
    """Card override > escalation after a failed attempt > complexity default."""
    if task.model:
        return task.model
    if task.complexity == "large":
        return cfg.large_worker_model
    if cfg.escalate_on_retry and task.attempts > 1:
        return cfg.large_worker_model
    return cfg.small_worker_model


def task_card(t: Task) -> str:
    def bullets(xs: list[str]) -> str:
        return "\n".join(f"- {x}" for x in xs) or "- (none)"
    card = (f"# Task {t.id}: {t.title}\n\n## Spec\n{t.spec}\n\n"
            f"## Acceptance criteria\n{bullets(t.acceptance)}\n\n"
            f"## Non-goals (do NOT do these)\n{bullets(t.non_goals)}\n\n"
            f"## Files you may touch\n{bullets(t.files_scope) if t.files_scope else '- (unrestricted)'}\n")
    if t.feedback:
        card += ("\n## Feedback from previous attempts (address all of it)\n" +
                 "\n\n".join(f"### Attempt note {i+1}\n{f}" for i, f in enumerate(t.feedback[-3:])))
    return card


# ---------------------------------------------------------------- results
@dataclass
class AgentResult:
    data: dict[str, Any]
    cost_usd: float
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
    async def work(self, cfg: Config, task: Task, worktree: Path,
                   graph: Path | None = None, resume: str | None = None) -> AgentResult: ...
    async def review(self, cfg: Config, task: Task, worktree: Path, diff: str,
                     work_report: dict[str, Any], gate_log: str, impact: str = "",
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
        opts = ClaudeAgentOptions(
            cwd=str(cwd or cfg.repo), model=cfg.planner_model,
            system_prompt=PLANNER_SYSTEM + guide + _context(cfg),
            tools=base, allowed_tools=allowed, mcp_servers=servers, strict_mcp_config=True,
            setting_sources=["project"], max_turns=60, session_id=_new_session(),
            max_budget_usd=cfg.task_budget_usd * 2,
            output_format={"type": "json_schema", "schema": PLAN_SCHEMA},
        )
        return await self._run(f"Goal:\n{goal}\n\nExplore the repository, then produce the plan.",
                               opts, "planner")

    async def work(self, cfg: Config, task: Task, worktree: Path,
                   graph: Path | None = None, resume: str | None = None) -> AgentResult:
        """Run the worker. With `resume` (and a stored session), continue that session
        with the given message instead of starting cold: the agent keeps its context."""
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
        tools = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]
        servers, allowed, guide = _graph_opts(cfg, graph, tools)
        opts = ClaudeAgentOptions(
            cwd=str(worktree),
            model=pick_worker_model(cfg, task),
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": WORKER_APPEND + guide + _context(cfg)},
            tools=tools, allowed_tools=allowed, permission_mode="acceptEdits",
            mcp_servers=servers, strict_mcp_config=True,
            setting_sources=["project"],  # picks up the repo's CLAUDE.md
            max_turns=cfg.worker_max_turns, max_budget_usd=cfg.task_budget_usd,
            hooks={"PreToolUse": [HookMatcher(hooks=[make_worker_guard(worktree)])]},
            output_format={"type": "json_schema", "schema": WORK_SCHEMA},
            resume=task.session_id if (resume and task.session_id) else None,
            session_id=None if (resume and task.session_id) else _new_session(),
        )
        prompt = resume if (resume and task.session_id) else task_card(task)
        return await self._run(prompt, opts, task.id)

    async def review(self, cfg: Config, task: Task, worktree: Path, diff: str,
                     work_report: dict[str, Any], gate_log: str, impact: str = "",
                     graph: Path | None = None) -> AgentResult:
        from claude_agent_sdk import ClaudeAgentOptions
        impact_block = (f"## Impact (from the code graph: code outside this diff that depends on "
                        f"changed symbols; check these still work)\n{impact}\n\n" if impact else "")
        prompt = (f"{task_card(task)}\n\n## Worker's report\n```json\n{json.dumps(work_report, indent=2)}\n```\n\n"
                  f"## Automated gates\n{gate_log}\n\n{impact_block}## Diff\n```diff\n{diff}\n```\n\n"
                  "Read surrounding code as needed, then give your verdict.")
        base = ["Read", "Glob", "Grep"]
        servers, allowed, guide = _graph_opts(cfg, graph, base)
        opts = ClaudeAgentOptions(
            cwd=str(worktree), model=cfg.reviewer_model,
            system_prompt=REVIEWER_SYSTEM + guide + _context(cfg),
            tools=base, allowed_tools=allowed, mcp_servers=servers, strict_mcp_config=True,
            setting_sources=["project"], max_turns=40, max_budget_usd=cfg.task_budget_usd,
            session_id=_new_session(),
            output_format={"type": "json_schema", "schema": REVIEW_SCHEMA},
        )
        return await self._run(prompt, opts, f"{task.id}:review")


def _new_session() -> str:
    """Every agent gets its own session id. Without this, a run started from inside a Claude
    Code session (e.g. via /dispatch) inherits CLAUDE_CODE_SESSION_ID, so parallel agents
    would share one session and resumes could land in the wrong conversation."""
    return str(uuid.uuid4())


def _context(cfg: Config) -> str:
    return f"\n\n## Project context\n{cfg.project_context.strip()}" if cfg.project_context.strip() else ""
