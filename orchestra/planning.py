"""Turn a planner's raw output into a validated, conflict-free task graph."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .store import Task


class PlanError(ValueError):
    pass


GLOB_CHARS = set("*?[")


def _literal_base(p: str) -> str:
    """The path before its first glob segment: 'src/**/*.py' -> 'src', 'src/api/x.py' -> itself."""
    parts = []
    for seg in p.strip().strip("/").split("/"):
        if GLOB_CHARS & set(seg):
            break
        parts.append(seg)
    return "/".join(parts)


def _scopes_overlap(a: list[str], b: list[str]) -> bool:
    """Conservative: entries overlap when their literal bases are equal or nested. A false
    positive only serializes two cards; a false negative makes them conflict at merge."""
    if not a or not b:
        return True  # unrestricted scope may touch anything
    for x in map(_literal_base, a):
        for y in map(_literal_base, b):
            if not x or not y or x == y or x.startswith(y + "/") or y.startswith(x + "/"):
                return True
    return False


def plan_fingerprint(plan: dict) -> str:
    """Stable id for a plan's task list, to refuse queueing the same plan twice."""
    tasks = plan.get("tasks", plan if isinstance(plan, list) else [])
    return hashlib.sha256(json.dumps(tasks, sort_keys=True).encode()).hexdigest()[:16]


def _ancestors(tid: str, deps: dict[str, list[str]], seen: set[str] | None = None) -> set[str]:
    seen = seen if seen is not None else set()
    for d in deps.get(tid, []):
        if d not in seen:
            seen.add(d)
            _ancestors(d, deps, seen)
    return seen


def build_tasks(plan: dict, id_prefix: str = "T", start: int = 1,
                graph=None, existing: list[Task] | None = None) -> tuple[list[Task], list[str]]:
    """Validate the plan, assign stable ids, and serialize tasks whose files overlap
    (or, with a code graph, whose code calls into each other) — with each other and with
    `existing` cards still open from earlier plans.

    Returns (tasks in topological order, warnings)."""
    raw = plan.get("tasks", [])
    if not raw:
        raise PlanError("plan has no tasks")
    warnings: list[str] = []
    key_to_id: dict[str, str] = {}
    for i, t in enumerate(raw):
        key = t["key"]
        if key in key_to_id:
            raise PlanError(f"duplicate task key {key!r}")
        key_to_id[key] = f"{id_prefix}{start + i:02d}"

    deps: dict[str, list[str]] = {}
    for t in raw:
        tid = key_to_id[t["key"]]
        deps[tid] = []
        for d in t.get("depends_on", []):
            if d not in key_to_id:
                warnings.append(f"{tid}: dropped unknown dependency {d!r}")
                continue
            deps[tid].append(key_to_id[d])

    # cycle check (DFS)
    state: dict[str, int] = {}
    order: list[str] = []
    def visit(n: str, path: list[str]) -> None:
        if state.get(n) == 1:
            raise PlanError(f"dependency cycle: {' -> '.join(path + [n])}")
        if state.get(n) == 2:
            return
        state[n] = 1
        for d in deps[n]:
            visit(d, path + [n])
        state[n] = 2
        order.append(n)
    for n in deps:
        visit(n, [])

    by_id = {key_to_id[t["key"]]: t for t in raw}
    # Parallel tasks must not share files: add an edge (earlier -> later) when they would.
    ids = [key_to_id[t["key"]] for t in raw]
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if a in _ancestors(b, deps) or b in _ancestors(a, deps):
                continue
            sa, sb = by_id[a].get("files_scope", []), by_id[b].get("files_scope", [])
            if _scopes_overlap(sa, sb):
                deps[b].append(a)
                warnings.append(f"{b} now waits for {a}: overlapping files_scope")
            elif graph is not None and (hits := graph.coupled(sa, sb)):
                deps[b].append(a)
                warnings.append(f"{b} now waits for {a}: code coupling ({'; '.join(hits[:2])})")

    # Cards already queued or running from earlier plans: new work waits for them. Edges only
    # point from new to existing cards, so no cycle can form.
    for b in ids:
        sb = by_id[b].get("files_scope", [])
        for e in existing or []:
            if e.id in deps[b]:
                continue
            if _scopes_overlap(e.files_scope, sb):
                deps[b].append(e.id)
                warnings.append(f"{b} now waits for {e.id} (already queued): overlapping files_scope")
            elif graph is not None and (hits := graph.coupled(e.files_scope, sb)):
                deps[b].append(e.id)
                warnings.append(f"{b} now waits for {e.id} (already queued): code coupling ({hits[0]})")

    tasks = [Task(id=tid, title=by_id[tid]["title"], spec=by_id[tid]["spec"],
                  acceptance=by_id[tid].get("acceptance_criteria", []),
                  non_goals=by_id[tid].get("non_goals", []),
                  files_scope=by_id[tid].get("files_scope", []),
                  depends_on=deps[tid],
                  complexity=by_id[tid].get("complexity", "small"),
                  model=by_id[tid].get("model") or None)
             for tid in order]
    return tasks, warnings


def save_plan(path: Path, goal: str, plan: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"goal": goal, **plan}, indent=2))


def render_plan(tasks: list[Task], warnings: list[str], plan: dict) -> str:
    lines = [f"Plan: {plan.get('summary', '')}", ""]
    for t in tasks:
        dep = f"  (after {', '.join(t.depends_on)})" if t.depends_on else ""
        model = f", model={t.model}" if t.model else ""
        lines.append(f"{t.id} [{t.complexity}{model}] {t.title}{dep}")
        lines.append(f"     scope: {', '.join(t.files_scope) or 'unrestricted'}")
        for a in t.acceptance:
            lines.append(f"     ✓ {a}")
    if warnings:
        lines += ["", "Adjustments:"] + [f"  - {w}" for w in warnings]
    if plan.get("open_questions"):
        lines += ["", "Open questions:"] + [f"  ? {q}" for q in plan["open_questions"]]
    return "\n".join(lines)
