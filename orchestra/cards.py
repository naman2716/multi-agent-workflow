"""Card surgery shared by triage, answers and the CLI.

Every change here keeps the queue's graph valid: scopes only name paths inside the repo, a
card whose scope grows waits for open cards that touch the same files, and edits are refused
on cards an agent is working on (unless forced).
"""
from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath

from .gates import in_scope
from .planning import PlanError, _scopes_overlap, build_tasks
from .store import OPEN_STATUSES, Store, Task

MAX_SCOPE_ADD = 10  # paths one decision option may add


class CardError(ValueError):
    """A card change that would break the queue or reach outside the repo."""


def clean_scope_paths(paths, state_dir: str = ".orchestra") -> tuple[list[str], list[str]]:
    """(paths that may go in a files_scope, refused ones): repo-relative, no `..`, nothing
    under .git/ or the state directory."""
    ok: list[str] = []
    bad: list[str] = []
    for p in paths or []:
        s = str(p).strip()
        parts = PurePosixPath(s).parts
        if (not parts or s.startswith(("/", "~")) or "\\" in s or ".." in parts
                or parts[0] in (".git", state_dir.strip("/"))):
            bad.append(s)
        elif s not in ok:
            ok.append(s)
    return ok, bad


def check_editable(store: Store, t: Task, force: bool = False) -> None:
    """Merged cards, and cards an agent is working on in a live run, need --force."""
    if force:
        return
    if t.status == "merged":
        raise CardError(f"{t.id} is merged; change it only with --force")
    if t.status in ("running", "review") and store.live_lease():
        raise CardError(f"{t.id} is {t.status} in a live run; stop the run first, or use --force")


def dependents(task_id: str, tasks: dict[str, Task]) -> set[str]:
    """Cards that depend on `task_id`, directly or through other cards."""
    out: set[str] = set()
    frontier = [task_id]
    while frontier:
        cur = frontier.pop()
        for t in tasks.values():
            if cur in t.depends_on and t.id not in out:
                out.add(t.id)
                frontier.append(t.id)
    return out


def serialize_against_open(store: Store, task_id: str, scope: list[str]) -> list[str]:
    """After a scope change: wait for open cards that touch the same files (never for a card
    that waits on this one, so no cycle). Returns the cards it now waits for."""
    tasks = {t.id: t for t in store.all()}
    me, deps_on_me = tasks[task_id], dependents(task_id, tasks)
    added = [t.id for t in tasks.values()
             if t.id != task_id and t.status in OPEN_STATUSES and t.id not in deps_on_me
             and t.id not in me.depends_on and _scopes_overlap(t.files_scope, scope)]
    if added:
        store.update(task_id, depends_on=me.depends_on + added)
    return added


def widen_scope(store: Store, task_id: str, paths: list[str], why: str = "") -> list[str]:
    """Add paths to a card's files_scope, log it and re-serialize. An unrestricted card (empty
    scope) stays unrestricted. Returns the paths actually added."""
    t = store.get(task_id)
    added = [p for p in paths if p not in t.files_scope] if t.files_scope else []
    if not added:
        return []
    store.update(task_id, files_scope=t.files_scope + added)
    waits = serialize_against_open(store, task_id, added)
    store.log(task_id, "scope", f"scope widened{f' {why}' if why else ''}: +{', '.join(added)}"
              + (f"; now waits for {', '.join(waits)}" if waits else ""))
    return added


def narrow_scope(store: Store, task_id: str, paths: list[str]) -> list[str]:
    """Remove paths from a card's files_scope. Refuses to leave it empty (that would mean
    "anything"). Returns the paths removed."""
    t = store.get(task_id)
    removed = [p for p in paths if p in t.files_scope]
    left = [p for p in t.files_scope if p not in removed]
    if removed and not left:
        raise CardError(f"removing {', '.join(removed)} would leave {task_id} with no files_scope "
                        "(unrestricted); set a new scope instead")
    if removed:
        store.update(task_id, files_scope=left)
        store.log(task_id, "scope", f"scope narrowed: -{', '.join(removed)}")
    return removed


def _discard_locked_tests(store: Store, t: Task, handoff_dir: Path | None) -> None:
    """New criteria or spec on a test-first card: its locked tests were written for the old ones."""
    if not (t.test_first and t.locked_tests):
        return
    store.update(t.id, locked_tests=[])
    if handoff_dir is not None:
        (handoff_dir / f"{t.id}.tests.patch").unlink(missing_ok=True)
        shutil.rmtree(handoff_dir / f"{t.id}.locked", ignore_errors=True)


def edit_card(store: Store, task_id: str, *, depends_on: list[str] | None = None,
              scope: list[str] | None = None, scope_add: list[str] | None = None,
              scope_remove: list[str] | None = None, acceptance: list[str] | None = None,
              acceptance_add: list[str] | None = None, spec: str | None = None, title: str | None = None,
              non_goals: list[str] | None = None, force: bool = False, state_dir: str = ".orchestra",
              handoff_dir: Path | None = None) -> list[str]:
    """Change an open card by hand. Every change is logged and noted on the card, so the next
    worker sees it. Returns what changed, e.g. ["depends_on: T15, T29"]."""
    t = store.get(task_id)
    check_editable(store, t, force)
    tasks = {x.id: x for x in store.all()}
    changes: list[str] = []
    fields: dict = {}
    if depends_on is not None:
        deps = list(dict.fromkeys(d.strip() for d in depends_on if d.strip()))
        unknown = [d for d in deps if d not in tasks or d == task_id]
        if unknown:
            raise CardError(f"unknown or self dependency: {', '.join(unknown)}")
        cycle = sorted(set(deps) & dependents(task_id, tasks))
        if cycle:
            raise CardError(f"{task_id} can't wait for {', '.join(cycle)}: "
                            f"{'it waits' if len(cycle) == 1 else 'they wait'} for {task_id} (cycle)")
        if deps != t.depends_on:
            fields["depends_on"] = deps
            changes.append(f"depends_on: {', '.join(deps) or '-'}")
    for name, value in (("scope", scope), ("scope_add", scope_add)):
        if value is not None:
            ok, bad = clean_scope_paths(value, state_dir)
            if bad:
                raise CardError(f"can't put {', '.join(bad)} in a files_scope: use repo-relative "
                                "paths, no '..', nothing under .git/ or the state directory")
            if name == "scope":
                if not ok:
                    raise CardError("an empty files_scope would mean 'anything'; give at least one path")
                scope = ok
            else:
                scope_add = ok
    if acceptance is not None or acceptance_add:
        crit = list(acceptance if acceptance is not None else t.acceptance) + list(acceptance_add or [])
        if crit != t.acceptance:
            fields["acceptance"] = crit
            changes.append(f"acceptance ({len(crit)} criteria)")
    for name, value in (("spec", spec), ("title", title)):
        if value is not None and value.strip() and value.strip() != getattr(t, name):
            fields[name] = value.strip()
            changes.append(name)
    if non_goals is not None and list(non_goals) != t.non_goals:
        fields["non_goals"] = list(non_goals)
        changes.append("non_goals")
    if fields:
        store.update(task_id, **fields)
    if "acceptance" in fields or "spec" in fields:
        _discard_locked_tests(store, t, handoff_dir)
    if scope is not None and scope != t.files_scope:
        store.update(task_id, files_scope=scope)
        waits = serialize_against_open(store, task_id, scope)
        changes.append(f"files_scope: {', '.join(scope)}" + (f" (now waits for {', '.join(waits)})" if waits else ""))
    if scope_remove:
        if removed := narrow_scope(store, task_id, scope_remove):
            changes.append(f"files_scope -{', '.join(removed)}")
    if scope_add:
        if added := widen_scope(store, task_id, scope_add, "by hand"):
            changes.append(f"files_scope +{', '.join(added)}")
    if changes:
        store.log(task_id, "edit", "; ".join(changes))
        store.add_feedback(task_id, "The human lead edited this card: " + "; ".join(changes))
    return changes


def split_card(store: Store, task: Task, items: list, why: str, split_at: int | None = None,
               source: str = "triage") -> tuple[list[str], list[str]]:
    """Replace a card with 2+ cards (plan format). Children wait for the parent's dependencies,
    cards that waited for the parent wait for all children, and the parent is cancelled as
    "split into ...". Triage must stay inside the parent's files_scope; a human split may
    reach outside it (noted). Returns (child ids, notes)."""
    if not isinstance(items, list) or len(items) < 2:
        raise CardError("a split needs at least two cards")
    tasks = {t.id: t for t in store.all()}
    deps_on_me = dependents(task.id, tasks)
    existing = [t for t in store.open_tasks() if t.id != task.id and t.id not in deps_on_me]
    try:
        new, notes = build_tasks({"tasks": items}, start=len(tasks) + 1, existing=existing, split_at=split_at)
    except (PlanError, KeyError, TypeError, AttributeError) as e:
        raise CardError(f"its split was invalid ({e})") from None
    if task.files_scope:
        outside = [f"{n.title}: {p}" for n in new for p in (n.files_scope or ["(unrestricted)"])
                   if not in_scope(p, task.files_scope)]
        if outside and source == "triage":
            raise CardError("the split reaches outside the card's files_scope ("
                            + "; ".join(outside[:3]) + ")")
        if outside:
            notes.append("reaches outside the original files_scope: " + "; ".join(outside[:3]))
    ids = [n.id for n in new]
    for n in new:
        n.depends_on = list(dict.fromkeys(n.depends_on + task.depends_on))
        # triage's children aren't triaged again (no split loops); a human's split earns triage
        n.test_first, n.source, n.triaged = task.test_first, task.source, source == "triage"
        n.model = n.model or task.model
        store.add_task(n)
        store.add_feedback(n.id, f"Split from {task.id} by {source}: {why}")
    for t in tasks.values():
        if task.id in t.depends_on:
            store.update(t.id, depends_on=[d for d in t.depends_on if d != task.id] + ids)
    store.update(task.id, status="cancelled", summary=f"split into {', '.join(ids)}",
                 worktree=None, session_id=None, resume_note=None)
    store.log(task.id, "split", f"split by {source} into {', '.join(ids)}: {why}"[:500])
    return ids, notes
