"""Claude Code harness: the interactive ("thinking mode") side of Orchestra.

`orchestra harness install` writes into the target repo:

  .claude/agents/orchestra-{planner,worker,reviewer}.md   same prompts the SDK agents use
  .claude/skills/groom/                /groom   plan cards with you, then queue them
  .claude/skills/dispatch/             /dispatch   run the queue unattended, report back
  .claude/skills/orchestra-status/     /orchestra-status
  .claude/skills/decide/               /decide   answer questions agents are waiting on
  .claude/skills/review-integration/   /review-integration   review what landed, open a PR
  .claude/settings.json                PreToolUse guard hook (merged, never clobbered)
  .mcp.json                            Graphify MCP server for interactive sessions
  CLAUDE.md                            an Orchestra section between markers
  + `graphify claude install` and `graphify hook install` (graph-first nudges, and the
    graph rebuilt on every commit/checkout of your own checkout)

Prompts come from orchestra.agents so the interactive and unattended agents can't drift.
Re-running install is safe: managed files are rewritten, shared files are merged.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from .agents import PLANNER_SYSTEM, REVIEWER_SYSTEM, WORKER_APPEND, sizing_rules
from .config import Config
from .graph import GRAPH_GUIDE, graphify_available

MARK_START, MARK_END = "<!-- orchestra:start -->", "<!-- orchestra:end -->"
HOOK_TAG = "orchestra"  # identifies our entries inside settings.json


def _hook_command() -> str:
    exe = shutil.which("orchestra")
    return f"{exe} hook interactive" if exe else f"{sys.executable} -m orchestra.cli hook interactive"


def _agent(name: str, description: str, tools: list[str], model: str, body: str,
           extra: str = "") -> str:
    return (f"---\nname: {name}\ndescription: {description}\ntools: {', '.join(tools)}\n"
            f"model: {model}\n{extra}---\n\n{body.strip()}\n")


def _skill(name: str, description: str, body: str, extra: str = "") -> str:
    return f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n{body.strip()}\n"


def _plan_format() -> str:
    return """```json
{
  "summary": "one paragraph",
  "tasks": [{
    "key": "short-slug",
    "title": "imperative title",
    "spec": "what and why, with pointers to existing code",
    "acceptance_criteria": ["checkable behaviour + the test that proves it"],
    "non_goals": ["tempting adjacent work NOT to do"],
    "files_scope": ["src/exact/path.ts", "src/dir/**"],
    "depends_on": ["other-slug"],
    "complexity": "small | large",
    "estimated_lines": 250,
    "size_notes": "what dominates the size",
    "test_first": false,
    "model": "optional override, e.g. opus | sonnet | haiku; omit to use the defaults"
  }],
  "open_questions": []
}
```"""


def build_files(cfg: Config, graph: bool) -> dict[str, str]:
    g_tools = ["mcp__graph__query_graph", "mcp__graph__get_node", "mcp__graph__get_neighbors",
               "mcp__graph__shortest_path"] if graph else []
    guide = GRAPH_GUIDE if graph else ""
    integ, base = cfg.integration_branch, cfg.base_branch
    log = f"{cfg.state_dir.rstrip('/')}/run.log"
    files: dict[str, str] = {}

    files[".claude/agents/orchestra-planner.md"] = _agent(
        "orchestra-planner",
        "Decomposes a large goal into parallel-safe task cards for Orchestra. Use when asked to "
        "plan or break down a feature before dispatching it.",
        ["Read", "Glob", "Grep", *g_tools], cfg.planner_model,
        PLANNER_SYSTEM + sizing_rules(cfg) + guide + f"\n\nOutput the plan as JSON in exactly this shape:\n{_plan_format()}")

    files[".claude/agents/orchestra-worker.md"] = _agent(
        "orchestra-worker",
        "Implements exactly one Orchestra task card in an isolated worktree. Use for a single, "
        "well-scoped card with acceptance criteria.",
        ["Read", "Write", "Edit", "Glob", "Grep", "Bash", *g_tools], cfg.small_worker_model,
        WORKER_APPEND + guide +
        "\n\nWhen done, reply with: status (done|blocked), summary, files_changed, tests, blocker.",
        extra="isolation: worktree\n")

    files[".claude/agents/orchestra-reviewer.md"] = _agent(
        "orchestra-reviewer",
        "Independent reviewer that judges a diff against its task card. Use after work is done; "
        "never the same agent that wrote the code.",
        ["Read", "Glob", "Grep", "Bash", *g_tools], cfg.reviewer_model,
        REVIEWER_SYSTEM + guide +
        "\n\nUse Bash only for read-only git (git diff, git log, git show). Reply with: verdict "
        "(approve|reject), unmet criteria with evidence, scope creep, issues by severity, feedback.")

    graph_step = ("- Use the mcp__graph__* tools (or `graphify query`) to find the real modules, callers "
                  "and file paths before writing any card. Do not guess paths.\n") if graph else ""
    files[".claude/skills/groom/SKILL.md"] = _skill(
        "groom",
        "Plan a large feature with the user and turn it into Orchestra task cards ready for parallel "
        "agents. Use when the user wants to break down, scope, or plan work before dispatching it.",
        f"""
# Groom work into Orchestra task cards

This is thinking mode: the value is the conversation. Shape the work with the user before
any agent writes code. Tight cards are the most important quality gate in the system.

0. If the user names a Linear ticket (e.g. ENG-123), read it with `orchestra linear show ENG-123`
   and treat it as the goal (the user's own words take priority). Only read tickets the user
   names; never go looking for work on your own.
1. Restate the goal and ask about anything ambiguous: business rules, edge cases, what is
   explicitly out of scope. Ask before guessing.
2. Explore the code read-only to ground the plan.
{graph_step}3. Draft cards following these rules:
   - vertical slices, each reviewable as one small diff that fits the Sizing rules below;
   - `files_scope` specific; cards that could run at the same time must not share files;
   - shared foundations (schema, types, interfaces) first, dependents list them in `depends_on`;
   - acceptance criteria are concrete and testable; `non_goals` name tempting extras;
   - `test_first: true` on cards that change money, stored data or auth/permissions: a separate
     agent writes locked acceptance tests from the criteria before the worker starts, so make
     those criteria precise. Confirm these flags with the user;
   - every card has `estimated_lines` (see Sizing); split any card above the threshold before
     showing it.
   You can delegate a first draft to the `orchestra-planner` subagent, then refine it together.
4. Show the cards as a short table (id, title, scope, depends on, est. lines) and iterate with
   the user.
5. When the user approves, write the plan to `.orchestra/plan.json` in this shape:
{_plan_format()}
   Add `"goal": "<one-line goal>"` at the top (it becomes the PR title). For a Linear ticket
   also add `"source": {{"linear": "ENG-123"}}`, so the cards and the PR link back to it.
6. Run `orchestra approve`. It validates the graph, adds dependency edges where scopes overlap
   {"or code is coupled " if graph else ""}and queues the cards. Show its output.
7. Suggest `/dispatch` to run them.
""" + sizing_rules(cfg))

    files[".claude/skills/dispatch/SKILL.md"] = _skill(
        "dispatch",
        "Run queued Orchestra task cards with parallel agents and report the outcome. Use when the "
        "user says dispatch, run the queue, or start the agents.",
        f"""
# Dispatch the Orchestra queue

1. Run `orchestra status`. If nothing is pending, say so and suggest `/groom`.
2. Confirm concurrency and budget with the user if they haven't said (defaults come from
   orchestra.toml). Rate limits are shared with this session, so 2–4 workers is typical.
3. Start the run detached: `orchestra run --detach -j <N>`. It runs in its own process
   session, so it keeps going if this Claude Code session ends or restarts, and its output
   goes to `{log}`. Each card gets its own worktree, automated gates, an independent
   reviewer, and is merged into `{integ}`, never into `{base}`.
4. Keep an eye on the run while it works: read the end of `{log}` (`tail -n 40 {log}`) and
   `orchestra status` every few minutes. When a line says
   `needs your decision`, bring it to the user right away; don't wait for the run to end:
   run `orchestra decisions`, then ask with AskUserQuestion using the agent's options (mark
   the recommended one; the user can also type their own answer). Record it exactly with
   `orchestra answer <ID> "<decision>"` (or `--pick N`). The running process notices within
   seconds and the agent resumes its own session; no restart needed.
   Never answer on the user's behalf. If ntfy is set up, the user may answer from their phone
   instead; the card resumes the same way.
5. If a usage limit is hit, the run pauses itself and resumes after the reset; just tell
   the user when it expects to resume.
6. When it ends, report: merged cards, blocked cards with the reason from
   `orchestra status <ID>`, anything still waiting on a decision, and total spend. Blocked
   cards were already triaged once (rewritten, moved to a stronger model or split when that
   could help); show the triage note and propose a fix: re-groom it, or
   `orchestra retry <ID> --note "..."`.
7. Report the draft PR link and its CI result from the run's output (or `orchestra status`).
   Red CI became a fix card automatically (up to `[ship].ci_fix_rounds` times); say whether
   it went green.
8. Suggest `/review-integration` to review what landed.

If this session restarts, the run is still going: check `orchestra status` and the log. A run
that was killed anyway can simply be started again; it resumes interrupted cards, and if it was
killed while shipping, it ships again and waits for CI.

Tip for the user: running `orchestra run` in their own terminal instead shows questions
right there as they come up, with numbered options to answer inline.
""", extra="disable-model-invocation: true\n")

    files[".claude/skills/orchestra-status/SKILL.md"] = _skill(
        "orchestra-status",
        "Show the Orchestra task queue, spend, and why any card is blocked. Use when the user asks "
        "how the agents are doing or what is blocked.",
        """
# Orchestra status

1. Run `orchestra status` and summarize it: counts, what is running, total spend vs budget.
2. For every blocked card run `orchestra status <ID>` and explain the last feedback in one line.
   If any cards are waiting (`?`), say how many questions are open and suggest `/decide`.
3. If any cards were merged, run `orchestra stats` and report average cost and tokens per card
   (with vs without the code graph, when both exist).
""")

    files[".claude/skills/decide/SKILL.md"] = _skill(
        "decide",
        "Answer the questions Orchestra's agents stopped to ask (business rules, pricing, data "
        "choices). Use when cards are waiting on a decision or the user asks what needs input.",
        """
# Answer agents' open questions

Agents stop and ask instead of guessing when a choice affects users, money, or data.
Each one is paused with its work intact and resumes its own session once answered.

1. Run `orchestra decisions`. If there are none, say so.
2. For each question, show the card, the question, why it matters, the agent's work so
   far, and its recommendation. Ask the user using AskUserQuestion with the agent's options
   (mark the recommended one), so they can pick one or type their own answer.
   Use the codebase or the graph to add context if it helps the user decide, but the
   decision is the user's: never answer on their behalf.
3. Record each answer exactly as the user gave it:
   `orchestra answer <ID> "<decision>"` (or `--pick N` for a listed option).
4. When all are answered, offer to continue with `/dispatch`.
""")

    files[".claude/skills/review-integration/SKILL.md"] = _skill(
        "review-integration",
        f"Review everything Orchestra merged into {integ} before it goes to {base}, then help open "
        "a PR. Use when the user wants to review agent output or ship it.",
        f"""
# Review the integration branch

Automated gates and the per-card reviewer already ran. This pass is the human-level check
they can't do: architecture fit, business logic, and how the cards combine.

1. `git log --oneline {base}..{integ}` and `git diff --stat {base}...{integ}`.
2. Map commits to cards (commit subjects start with the card id); use `orchestra status <ID>`
   for each card's spec and history.
3. Spawn the `orchestra-reviewer` subagent on the combined diff (`git diff {base}...{integ}`),
   asking it to focus on cross-card interactions and anything touching money, auth, or data.
4. Check these yourself and report them plainly:
   - scope: anything built that no card asked for;
   - architecture: new patterns that don't match the codebase;
   - business logic: pricing, payouts, booking rules, cancellations;
   - comprehension: anything you or the user can't follow is too big or too novel.
5. Give a verdict: ship, fix first (list fixes; offer to write cards via `/groom`), or drop cards.
6. If shipping and the user agrees, run `orchestra ship`: it pushes `{integ}`, opens or
   updates the draft PR into `{base}`, and shows CI. Never push to `{base}` directly.
""")
    return files


def _claude_md_section(cfg: Config, graph: bool) -> str:
    g = ("- A code knowledge graph is kept in `graphify-out/` (rebuilt on every commit and checkout).\n"
         "  Query it before grepping or reading many files.\n") if graph else ""
    return f"""{MARK_START}
## Orchestra (multi-agent workflow)

- Big work goes through Orchestra: `/groom` to plan cards with the user, `/dispatch` to run
  them with parallel agents, `/review-integration` before shipping.
- Agent work lands on `{cfg.integration_branch}`, never directly on `{cfg.base_branch}`.
  Do not push to `{cfg.base_branch}` or force-push; ship through a reviewed PR.
- Never write credentials into files; use environment variables.
{g}- Project settings live in `orchestra.toml`; queue state in `.orchestra/` (not committed).
{MARK_END}
"""


def _merge_claude_md(path: Path, section: str) -> None:
    text = path.read_text() if path.exists() else ""
    if MARK_START in text and MARK_END in text:
        pre, rest = text.split(MARK_START, 1)
        _, post = rest.split(MARK_END, 1)
        text = pre + section.rstrip("\n") + post
    else:
        text = (text.rstrip() + "\n\n" if text.strip() else "") + section
    path.write_text(text)


def _merge_settings(path: Path, hook_cmd: str) -> None:
    data = json.loads(path.read_text()) if path.exists() else {}
    pre = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
    pre[:] = [m for m in pre if not any(f" hook interactive" in h.get("command", "") and HOOK_TAG in
                                        h.get("command", "") for h in m.get("hooks", []))]
    pre.append({"matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash",
                "hooks": [{"type": "command", "command": hook_cmd, "timeout": 10}]})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _merge_mcp(path: Path, interpreter: str) -> None:
    data = json.loads(path.read_text()) if path.exists() else {}
    data.setdefault("mcpServers", {})["graph"] = {
        "type": "stdio", "command": interpreter,
        "args": ["-m", "graphify.serve", "graphify-out/graph.json"]}
    path.write_text(json.dumps(data, indent=2) + "\n")


def stale_files(cfg: Config) -> list[str]:
    """Installed agent and skill files that differ from what this Orchestra would write (with
    or without the graph). Harness files are usually committed, so an upgrade doesn't reach
    them until `orchestra harness install` runs again. Files that aren't installed don't count."""
    fresh = [build_files(cfg, graph=g) for g in (True, False)]
    out = []
    for rel in fresh[0]:
        p = cfg.repo / rel
        if p.is_file() and all(p.read_text() != f[rel] for f in fresh):
            out.append(rel)
    return out


def install_harness(cfg: Config, graph: bool = True, git_hooks: bool = True) -> list[str]:
    repo, out = cfg.repo, []
    graph = graph and graphify_available(cfg.graph.python or None)
    for rel, content in build_files(cfg, graph).items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        out.append(f"wrote   {rel}")

    if graph:  # graphify's own Claude Code integration first; we merge on top of it
        gexe = [cfg.graph.interpreter, "-m", "graphify"]
        r = subprocess.run([*gexe, "claude", "install"], cwd=repo, capture_output=True, text=True)
        out.append("graphify claude install: " + ("ok" if r.returncode == 0 else r.stderr.strip()[-200:]))
        if git_hooks:
            r = subprocess.run([*gexe, "hook", "install"], cwd=repo, capture_output=True, text=True)
            out.append("graphify hook install: " + ("ok" if r.returncode == 0 else r.stderr.strip()[-200:]))
        _merge_mcp(repo / ".mcp.json", cfg.graph.interpreter)
        out.append("merged  .mcp.json (graph MCP server)")
        r = subprocess.run([*gexe, "update", "."], cwd=repo, capture_output=True, text=True)
        out.append("built   graphify-out/ for this checkout" if r.returncode == 0
                   else f"graph build failed: {r.stdout[-200:]}{r.stderr[-200:]}")
        _ensure_gitignore(repo, "graphify-out/")

    _merge_settings(repo / ".claude" / "settings.json", _hook_command())
    out.append("merged  .claude/settings.json (guard hook)")
    _merge_claude_md(repo / "CLAUDE.md", _claude_md_section(cfg, graph))
    out.append("merged  CLAUDE.md (orchestra section)")
    _ensure_gitignore(repo, ".orchestra/")
    out.append("Done. In Claude Code: /groom → /dispatch → /review-integration")
    return out


def _ensure_gitignore(repo: Path, line: str) -> None:
    gi = repo / ".gitignore"
    lines = gi.read_text().splitlines() if gi.exists() else []
    if line not in lines:
        gi.write_text("\n".join(lines + [line]) + "\n")
