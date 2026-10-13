"""Command line interface.

  orchestra init                      write orchestra.toml (auto-detects test/lint commands)
  orchestra plan "<goal>"             planner proposes a task graph -> .orchestra/plan.json
  orchestra approve [plan.json]       load the (optionally hand-edited) plan into the queue
  orchestra add task.json             add hand-written task cards
  orchestra run                       execute the queue with parallel workers
  orchestra status [TASK]             queue, costs, and a task's history/feedback
  orchestra retry TASK [--note ...]   send a blocked task back to the queue with guidance
  orchestra test-first TASK [--off]   locked acceptance tests written before the worker starts
  orchestra cancel TASK
  orchestra decisions                 questions agents are waiting on you for
  orchestra answer TASK "..."         answer one (or --pick N / --recommended); it resumes next run
  orchestra models                    which model each role uses (profile + overrides)
  orchestra graph build|status|query|affected|explain|benchmark
  orchestra stats                     avg cost/tokens per task, with vs without the graph
  orchestra harness install           add the Claude Code harness (agents, skills, hooks)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from .agents import ClaudeRunner
from .config import CONFIG_TEMPLATE, Config, detect_gates, render_gates
from .graph import Graph, GraphData, graphify_available
from .orchestrator import Orchestrator
from .planning import PlanError, build_tasks, plan_fingerprint, render_plan, save_plan
from .store import RunLocked, Store, Task


def _repo(args) -> Path:
    p = Path(args.repo).resolve()
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=p, capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"{p} is not a git repository")
    return Path(out.stdout.strip())


def _store(cfg: Config) -> Store:
    return Store(cfg.state_path / "state.db")


def _graph(cfg: Config) -> Graph:
    return Graph(cfg.repo, cfg.worktree_root, cfg.integration_branch, cfg.state_path, cfg.graph,
                 log=lambda m: print(f"[graph] {m}"))


async def cfg_integration(cfg: Config) -> None:
    from .gitops import Git
    await Git(cfg.repo, cfg.worktree_root, cfg.base_branch, cfg.integration_branch).ensure_integration_branch()


def _graph_data(cfg: Config) -> GraphData | None:
    if not cfg.graph.enabled:
        return None
    return _graph(cfg).data()


def _already_queued(store: Store, plan: dict, force: bool) -> bool:
    """True (and explain) if this exact plan was queued before and --force wasn't given."""
    if force or plan_fingerprint(plan) not in store.get_meta("approved_plans", []):
        return False
    print("This plan was already queued (same tasks). Nothing added; use --force to queue it again.")
    return True


def _queue(store: Store, tasks: list[Task], plan: dict) -> None:
    for t in tasks:
        store.add_task(t)
    store.set_meta("approved_plans", store.get_meta("approved_plans", []) + [plan_fingerprint(plan)])


def cmd_init(args) -> None:
    repo = _repo(args)
    path = repo / "orchestra.toml"
    if path.exists() and not args.force:
        sys.exit(f"{path} exists (use --force to overwrite)")
    base = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip() or "main"
    setup, gates = detect_gates(repo)
    has_graph = graphify_available()
    path.write_text(CONFIG_TEMPLATE.format(base=base, setup=setup, gates=render_gates(gates),
                                           graph_enabled="true" if has_graph else "false"))
    gi = repo / ".gitignore"
    lines = gi.read_text().splitlines() if gi.exists() else []
    if ".orchestra/" not in lines:
        gi.write_text("\n".join(lines + [".orchestra/"]) + "\n")
    print(f"Wrote {path}\n  base branch: {base}\n  setup: {setup or '(none)'}\n"
          f"  gates: {', '.join(g.name for g in gates) or '(none detected — add some!)'}\n"
          f"  code graph: {'enabled (graphify found)' if has_graph else 'off — pip install \'graphifyy[mcp]\' to enable'}\n"
          "Edit [project].context with your stack and conventions before planning.")


def plan_with_critic(cfg: Config, store: Store, goal: str, runner, verbose: bool = False):
    """Draft a plan against the integration head (not your checkout), then let a critic
    correct it. Returns (plan, critic_issues, cost, cwd, graph_data) or exits on failure."""
    from .gitops import Git
    state: dict = {}

    async def _plan():
        await cfg_integration(cfg)
        g = _graph(cfg)
        snap = await g.snapshot("planner") if cfg.graph.enabled else None
        cwd = g.wt if snap else await Git(cfg.repo, cfg.worktree_root, cfg.base_branch,
                                          cfg.integration_branch).detached_worktree("_plan", cfg.integration_branch)
        state.update(cwd=cwd, graph=GraphData.load(snap) if snap else None)
        print(f"Planning against {cfg.integration_branch} (read-only"
              + (", using the code graph" if snap else "") + ")…")
        res = await runner.plan(cfg, goal, cwd=cwd, graph=snap)
        critic = None
        if res.ok and cfg.planning.critic:
            print("Critic reviewing the plan…")
            critic = await runner.critique_plan(cfg, goal, res.data, cwd=cwd, graph=snap)
        return res, critic

    res, critic = asyncio.run(_plan())
    cost = res.cost_usd + (critic.cost_usd if critic else 0.0)
    store.set_meta("planner_cost", store.get_meta("planner_cost", 0.0) + cost)
    if not res.ok:
        sys.exit(f"Planner failed: {res.error}")
    plan, issues = res.data, []
    if critic is not None and not critic.ok:
        print(f"Critic failed ({critic.error[:200]}); keeping the planner's draft.")
    elif critic is not None:
        issues = critic.data.get("issues", [])
        if critic.data.get("verdict") == "revise":
            try:
                build_tasks(critic.data["plan"], start=len(store.all()) + 1)
                plan = critic.data["plan"]
            except (PlanError, KeyError, TypeError) as e:
                print(f"Critic's corrected plan was invalid ({e}); keeping the planner's draft.")
    return plan, issues, cost, state["cwd"], state["graph"]


def cmd_plan(args) -> None:
    cfg = Config.load(_repo(args))
    store = _store(cfg)
    plan, issues, cost, cwd, graph_data = plan_with_critic(cfg, store, args.goal,
                                                           ClaudeRunner(verbose=args.verbose))
    plan_path = cfg.state_path / "plan.json"
    save_plan(plan_path, args.goal, plan)
    try:
        tasks, warnings = build_tasks(plan, start=len(store.all()) + 1, graph=graph_data,
                                      existing=store.open_tasks(), root=cwd)
    except PlanError as e:
        sys.exit(f"Plan invalid: {e}\nEdit {plan_path} and run `orchestra approve`.")
    print(render_plan(tasks, warnings, plan))
    if issues:
        print("\nCritic:\n" + "\n".join(f"  - {i.get('task_key', '?')}: {i.get('problem', '')} → {i.get('fix', '')}"
                                        for i in issues))
    print(f"\nPlanning cost ${cost:.2f}. Plan saved to {plan_path}")
    if args.approve or (sys.stdin.isatty() and input("\nQueue these tasks? [y/N] ").lower() == "y"):
        _queue(store, tasks, plan)
        print(f"Queued {len(tasks)} tasks. Run `orchestra run`.")
    else:
        print("Not queued. Edit the plan if needed, then `orchestra approve`.")


def cmd_approve(args) -> None:
    cfg = Config.load(_repo(args))
    store = _store(cfg)
    path = Path(args.plan) if args.plan else cfg.state_path / "plan.json"
    plan = json.loads(path.read_text())
    if _already_queued(store, plan, args.force):
        return
    tasks, warnings = build_tasks(plan, start=len(store.all()) + 1, graph=_graph_data(cfg),
                                  existing=store.open_tasks())
    print(render_plan(tasks, warnings, plan))
    _queue(store, tasks, plan)
    print(f"\nQueued {len(tasks)} tasks.")


def cmd_add(args) -> None:
    cfg = Config.load(_repo(args))
    store = _store(cfg)
    data = json.loads(Path(args.file).read_text())
    items = data if isinstance(data, list) else data.get("tasks", [data])
    if _already_queued(store, {"tasks": items}, args.force):
        return
    tasks, warnings = build_tasks({"tasks": items}, start=len(store.all()) + 1, graph=_graph_data(cfg),
                                  existing=store.open_tasks())
    _queue(store, tasks, {"tasks": items})
    for w in warnings:
        print("note:", w)
    print(f"Added {', '.join(t.id for t in tasks)}")


def cmd_run(args) -> None:
    cfg = Config.load(_repo(args))
    if args.concurrency:
        cfg.concurrency = args.concurrency
    if args.budget:
        cfg.total_budget_usd = args.budget
    store = _store(cfg)
    if args.no_wait:
        cfg.limits.wait_for_reset = False
    if live := store.live_lease():
        print(f"{RunLocked(live)}.\nWatch it with `orchestra status`; only one run works the queue "
              "at a time.", file=sys.stderr)
        sys.exit(3)
    # running/review cards with no live run were interrupted; the run will recover them
    if not any(t.status in ("pending", "running", "review") for t in store.all()):
        waiting = store.waiting()
        sys.exit(f"Nothing pending; {len(waiting)} card(s) wait for your decision (orchestra decisions)."
                 if waiting else "Nothing pending. Use `orchestra plan` or `orchestra add` first.")
    try:
        sys.stdout.reconfigure(line_buffering=True)  # live output when piped (e.g. /dispatch)
    except (AttributeError, ValueError):
        pass
    interactive = cfg.ask_in_terminal and not args.no_ask and sys.stdin.isatty()
    from .asking import TerminalInput
    orch = Orchestrator(cfg, store, ClaudeRunner(verbose=args.verbose),
                        asker=TerminalInput() if interactive else None)
    if interactive:
        print("Agents' questions will appear here as they come up.")
    try:
        counts = asyncio.run(orch.run())
    except RunLocked as e:
        print(f"{e}.\nWatch it with `orchestra status`; only one run works the queue at a time.",
              file=sys.stderr)
        sys.exit(3)
    print(f"\nReview the result: git log {cfg.base_branch}..{cfg.integration_branch} "
          f"then open a PR from {cfg.integration_branch}.")
    sys.exit(0 if counts["blocked"] == 0 else 2)


def cmd_status(args) -> None:
    cfg = Config.load(_repo(args))
    store = _store(cfg)
    if args.task:
        t = store.get(args.task)
        from .agents import pick_worker_model
        print(f"{t.id} [{t.status}] {t.title}  attempts={t.attempts}  cost=${t.cost_usd:.2f}  "
              f"worker model={pick_worker_model(cfg, t)}")
        print(f"depends on: {', '.join(t.depends_on) or '-'}   scope: {', '.join(t.files_scope) or '-'}")
        if t.summary:
            print(f"\nsummary: {t.summary}")
        for i, f in enumerate(t.feedback, 1):
            print(f"\n--- feedback {i} ---\n{f}")
        print("\nrecent events:")
        for e in reversed(store.events(t.id, 20)):
            print(f"  {e['kind']:8} {e['detail'][:150]}")
        return
    icons = {"pending": "·", "running": "▶", "review": "◎", "waiting": "?", "merged": "✓",
             "blocked": "✗", "cancelled": "-"}
    for t in store.all():
        dep = f" ← {','.join(t.depends_on)}" if t.depends_on else ""
        print(f"{icons.get(t.status, '?')} {t.id} {t.status:9} {t.title[:60]}{dep}  "
              f"(try {t.attempts}, ${t.cost_usd:.2f})")
    last = store.get_meta("last_run") or {}
    live = store.live_lease()
    print(f"\n{store.counts()}")
    print(f"spend: lifetime ${store.total_cost():.2f} · last run ${last.get('spent_usd', 0):.2f} "
          f"· per-run cap ${cfg.total_budget_usd:.2f}"
          + (f" · run in progress (pid {live.get('pid')} on {live.get('host')})" if live else ""))


def cmd_retry(args) -> None:
    cfg = Config.load(_repo(args))
    store = _store(cfg)
    t = store.get(args.task)
    if t.status in ("merged", "running", "review") and not args.force:
        sys.exit(f"{t.id} is {t.status}; retry is for blocked, cancelled or pending cards "
                 "(use --force to re-queue it anyway).")
    if args.note:
        store.add_feedback(args.task, f"Guidance from the human lead: {args.note}")
    fields = {"status": "pending", "attempts": 0}
    if args.model:
        fields["model"] = args.model
    store.update(args.task, **fields)
    store.log(args.task, "retry", "re-queued by human")
    print(f"{args.task} re-queued.")


def cmd_model(args) -> None:
    cfg = Config.load(_repo(args))
    store = _store(cfg)
    store.update(args.task, model=None if args.model == "default" else args.model)
    t = store.get(args.task)
    from .agents import pick_worker_model
    print(f"{t.id} will run on {pick_worker_model(cfg, t)}")


def cmd_cancel(args) -> None:
    cfg = Config.load(_repo(args))
    store = _store(cfg)
    t = store.get(args.task)
    if t.status in ("running", "review") and store.live_lease() and not args.force:
        sys.exit(f"{t.id} is {t.status} in a live run; an agent is working in its worktree. "
                 "Stop the run first, or use --force.")
    if t.worktree:
        from .gitops import Git
        asyncio.run(Git(cfg.repo, cfg.worktree_root, cfg.base_branch, cfg.integration_branch)
                    .remove_worktree(t.id, t.branch))
    store.update(args.task, status="cancelled", worktree=None, session_id=None, resume_note=None)
    print(f"{args.task} cancelled.")


def cmd_test_first(args) -> None:
    cfg = Config.load(_repo(args))
    store = _store(cfg)
    t = store.get(args.task)
    if t.status not in ("pending", "blocked", "cancelled") and not args.force:
        sys.exit(f"{t.id} is {t.status}; change test-first only before an attempt starts (or --force).")
    if args.off:
        store.update(t.id, test_first=False, locked_tests=[])
        print(f"{t.id}: test-first off (acceptance tests will be the worker's own).")
    else:
        store.update(t.id, test_first=True)
        print(f"{t.id}: test-first on (a test writer writes locked acceptance tests before the worker).")


def _print_decision(t) -> None:
    d = t.decision or {}
    print(f"\n{t.id}  {t.title}")
    print(f"  Q: {d.get('question', '')}")
    if d.get("why_it_matters"):
        print(f"  why it matters: {d['why_it_matters']}")
    for i, o in enumerate(d.get("options", []), 1):
        print(f"   {i}. {o}")
    if d.get("recommendation"):
        print(f"  agent recommends: {d['recommendation']}")
    if d.get("work_so_far"):
        print(f"  work so far: {d['work_so_far'][:300]}")


def cmd_decisions(args) -> None:
    cfg = Config.load(_repo(args))
    waiting = _store(cfg).waiting()
    if not waiting:
        print("No open decisions.")
        return
    for t in waiting:
        _print_decision(t)
    print("\nAnswer with: orchestra answer <ID> \"your decision\"  (or --pick N, --recommended)")
    print("Then `orchestra run` resumes each agent where it stopped.")


def cmd_answer(args) -> None:
    cfg = Config.load(_repo(args))
    store = _store(cfg)
    t = store.get(args.task)
    d = t.decision or {}
    if args.pick:
        opts = d.get("options", [])
        if not 1 <= args.pick <= len(opts):
            sys.exit(f"--pick must be 1..{len(opts)}")
        answer = opts[args.pick - 1] + (f" — {args.text}" if args.text else "")
    elif args.recommended:
        answer = d.get("recommendation") or sys.exit("no recommendation recorded")
    elif args.text:
        answer = args.text
    else:
        _print_decision(t)
        answer = input("\nYour decision: ").strip() if sys.stdin.isatty() else ""
        if not answer:
            sys.exit("no answer given")
    try:
        store.answer(args.task, answer)
    except ValueError as e:
        sys.exit(str(e))
    print(f"{args.task} will resume with: {answer}\nRun `orchestra run` (or /dispatch) to continue.")


def cmd_models(args) -> None:
    cfg = Config.load(_repo(args))
    print(f"profile: {cfg.model_profile}")
    for role, m in [("planner", cfg.planner_model), ("reviewer", cfg.reviewer_model),
                    ("small worker", cfg.small_worker_model), ("large worker", cfg.large_worker_model),
                    ("validator", cfg.validator_model), ("test writer", cfg.test_writer_model)]:
        print(f"  {role:13} {m}")
    print(f"  escalate failed small cards to large worker: {cfg.escalate_on_retry}")
    if cfg.review.reviewer_rule == "opposite":
        from .agents import pick_reviewer_model
        print(f"  reviewer rule: opposite family (small-worker cards reviewed by "
              f"{pick_reviewer_model(cfg, cfg.small_worker_model)}, large-worker cards by "
              f"{pick_reviewer_model(cfg, cfg.large_worker_model)})")
    over = [(t.id, t.model) for t in _store(cfg).all() if t.model and t.status not in ("merged", "cancelled")]
    for tid, m in over:
        print(f"  card override: {tid} -> {m}")


def cmd_graph(args) -> None:
    cfg = Config.load(_repo(args))
    if not graphify_available(cfg.graph.python or None):
        sys.exit("graphify not installed: pip install 'graphifyy[mcp]'")
    cfg.graph.enabled = True
    g = _graph(cfg)
    if args.action in ("build", "status"):
        async def _b():
            await cfg_integration(cfg)
            return await g.refresh()
        if args.action == "build" and not asyncio.run(_b()):
            sys.exit(f"graph build failed: {g.error}")
        if not g.graph_json.exists():
            sys.exit("no graph yet: run `orchestra graph build`")
        d = g.data()
        print(f"graph: {g.graph_json}\n  nodes {len(d.node_file)} · edges {len(d.edges)}"
              f" · tracks {cfg.integration_branch}")
        return
    if not g.graph_json.exists():
        sys.exit("no graph yet: run `orchestra graph build`")
    sub = {"query": ["query", args.text, "--budget", "1500"], "affected": ["affected", args.text],
           "explain": ["explain", args.text], "benchmark": ["benchmark"]}[args.action]
    subprocess.run([cfg.graph.interpreter, "-m", "graphify", *[x for x in sub if x is not None],
                    "--graph", str(g.graph_json)] if args.action != "benchmark" else
                   [cfg.graph.interpreter, "-m", "graphify", "benchmark", str(g.graph_json)], cwd=g.wt)


def cmd_stats(args) -> None:
    cfg = Config.load(_repo(args))
    eff = _store(cfg).efficiency()
    if not eff:
        print("No merged tasks yet.")
    for k, v in eff.items():
        print(f"{k:14} tasks={v['tasks']:3}  avg cost ${v['avg_cost_usd']:.3f}  avg tokens {v['avg_tokens']:,}")
    if len(eff) == 2:
        a, b = eff["without_graph"], eff["with_graph"]
        if a["avg_tokens"]:
            print(f"\ngraph changed avg tokens per task by "
                  f"{(b['avg_tokens'] - a['avg_tokens']) / a['avg_tokens']:+.0%} "
                  "(compare similar-sized tasks before drawing conclusions)")


def cmd_harness(args) -> None:
    from .harness import install_harness
    cfg = Config.load(_repo(args))
    for line in install_harness(cfg, graph=not args.no_graph, git_hooks=not args.no_git_hooks):
        print(line)


def cmd_hook(args) -> None:
    from .guards import hook_main
    sys.exit(hook_main(args.profile))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="orchestra", description="Multi-agent coding orchestrator")
    p.add_argument("-C", "--repo", default=".", help="path to the git repo (default: cwd)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_init)
    s = sub.add_parser("plan"); s.add_argument("goal"); s.add_argument("--approve", action="store_true")
    s.add_argument("-v", "--verbose", action="store_true"); s.set_defaults(fn=cmd_plan)
    s = sub.add_parser("approve"); s.add_argument("plan", nargs="?")
    s.add_argument("--force", action="store_true", help="queue even if this plan was queued before")
    s.set_defaults(fn=cmd_approve)
    s = sub.add_parser("add"); s.add_argument("file")
    s.add_argument("--force", action="store_true", help="queue even if these cards were queued before")
    s.set_defaults(fn=cmd_add)
    s = sub.add_parser("run"); s.add_argument("-j", "--concurrency", type=int)
    s.add_argument("--budget", type=float); s.add_argument("-v", "--verbose", action="store_true")
    s.add_argument("--no-ask", action="store_true",
                   help="don't ask questions in this terminal (answer later with orchestra answer)")
    s.add_argument("--no-wait", action="store_true",
                   help="exit instead of waiting when a usage limit is hit (re-run later to resume)")
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("status"); s.add_argument("task", nargs="?"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("retry"); s.add_argument("task"); s.add_argument("--note")
    s.add_argument("--model", help="run this card on a specific model, e.g. opus")
    s.add_argument("--force", action="store_true", help="re-queue even a merged or running card")
    s.set_defaults(fn=cmd_retry)
    s = sub.add_parser("test-first", help="have locked acceptance tests written before the worker starts")
    s.add_argument("task"); s.add_argument("--off", action="store_true", help="turn it off")
    s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_test_first)
    s = sub.add_parser("model", help="set the worker model for a queued card")
    s.add_argument("task"); s.add_argument("model", help="alias or full id; 'default' to clear")
    s.set_defaults(fn=cmd_model)
    s = sub.add_parser("cancel"); s.add_argument("task")
    s.add_argument("--force", action="store_true", help="cancel even while a live run works on it")
    s.set_defaults(fn=cmd_cancel)
    s = sub.add_parser("decisions", help="list questions agents are waiting on")
    s.set_defaults(fn=cmd_decisions)
    s = sub.add_parser("answer", help="answer an agent's question; it resumes on the next run")
    s.add_argument("task"); s.add_argument("text", nargs="?")
    s.add_argument("--pick", type=int, help="choose option N"); s.add_argument("--recommended", action="store_true")
    s.set_defaults(fn=cmd_answer)
    s = sub.add_parser("models", help="show the model used by each role"); s.set_defaults(fn=cmd_models)
    s = sub.add_parser("graph", help="build/inspect the Graphify code graph")
    s.add_argument("action", choices=["build", "status", "query", "affected", "explain", "benchmark"])
    s.add_argument("text", nargs="?"); s.set_defaults(fn=cmd_graph)
    s = sub.add_parser("stats", help="cost/token efficiency with vs without the graph")
    s.set_defaults(fn=cmd_stats)
    s = sub.add_parser("harness", help="install the Claude Code harness into this repo")
    s.add_argument("action", choices=["install"]); s.add_argument("--no-graph", action="store_true")
    s.add_argument("--no-git-hooks", action="store_true"); s.set_defaults(fn=cmd_harness)
    s = sub.add_parser("hook", help="(used by Claude Code hooks) guard a tool call from stdin")
    s.add_argument("profile", choices=["interactive", "worker"]); s.set_defaults(fn=cmd_hook)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
