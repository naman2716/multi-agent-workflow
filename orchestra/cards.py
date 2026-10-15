"""Card surgery shared by triage, answers and the CLI.

Every change here keeps the queue's graph valid: scopes only name paths inside the repo, a
card whose scope grows waits for open cards that touch the same files, and edits are refused
on cards an agent is working on (unless forced).
"""
from __future__ import annotations

from pathlib import PurePosixPath

from .planning import _scopes_overlap
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
