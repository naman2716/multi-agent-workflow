"""Git plumbing: worktree isolation and safe, serialized integration.

Integration never touches your checked-out working tree. A task branch is
rebased onto the integration branch inside its own worktree, re-gated, and
then the integration ref is fast-forwarded with a compare-and-swap
(`git update-ref <ref> <new> <expected-old>`), so a concurrent change can
never be silently overwritten.
"""
from __future__ import annotations

import asyncio
import fnmatch
import os
import shutil
import signal
from dataclasses import dataclass, field
from pathlib import Path


class GitError(RuntimeError):
    pass


async def run(cmd: list[str] | str, cwd: Path, timeout: int = 300, check: bool = True,
              env: dict | None = None) -> tuple[int, str]:
    # Graphify's git hooks (installed for interactive use) must never rebuild a graph inside
    # task worktrees; the orchestrator refreshes its own. Current graphify already skips linked
    # worktrees; this flag also covers older versions and hook variants.
    env = {**os.environ, "GRAPHIFY_SKIP_HOOK": "1", **(env or {})}
    shell = isinstance(cmd, str)
    # Own process group, so a timeout kills the whole tree (pytest workers, dev servers),
    # not just the shell that started it.
    if shell:
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=str(cwd), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=env, start_new_session=True)
    else:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=env, start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        await _kill_group(proc)
        return 124, f"timed out after {timeout}s"
    except asyncio.CancelledError:
        await _kill_group(proc)
        raise
    text = out.decode(errors="replace")
    if check and proc.returncode != 0:
        raise GitError(f"{cmd if shell else ' '.join(cmd)} failed ({proc.returncode}):\n{text}")
    return proc.returncode, text


async def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except asyncio.TimeoutError:
        pass


async def git(repo: Path, *args: str, check: bool = True) -> str:
    return (await run(["git", *args], repo, check=check))[1].strip()


@dataclass
class DiffStats:
    files: list[str]
    added: int
    removed: int
    per_file: dict[str, int] = field(default_factory=dict)  # changed lines per file

    @property
    def lines(self) -> int:
        return self.added + self.removed

    def counted(self, exclude: list[str] | tuple[str, ...] = ()) -> int:
        """Changed lines, leaving out files that match `exclude` (lockfiles, generated code)."""
        if not exclude or not self.per_file:
            return self.lines
        return sum(n for f, n in self.per_file.items() if not matches_any(f, exclude))


def matches_any(path: str, patterns) -> bool:
    """True if the path or its file name matches any glob in `patterns`."""
    name = Path(path).name
    return any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(name, p) for p in patterns)


class Git:
    def __init__(self, repo: Path, worktree_root: Path, base: str, integration: str):
        self.repo, self.root, self.base, self.integration = repo, worktree_root, base, integration
        self._merge_lock = asyncio.Lock()

    async def ensure_integration_branch(self) -> None:
        rc, _ = await run(["git", "rev-parse", "--verify", f"refs/heads/{self.integration}"],
                          self.repo, check=False)
        if rc != 0:
            await git(self.repo, "branch", self.integration, self.base)

    async def head(self, ref: str) -> str:
        return await git(self.repo, "rev-parse", ref)

    async def head_of(self, wt: Path) -> str:
        return await git(wt, "rev-parse", "HEAD")

    async def create_worktree(self, task_id: str) -> tuple[Path, str]:
        """Fresh worktree + branch from the current integration head."""
        branch = f"orchestra/task/{task_id}"
        path = self.root / task_id
        await self.remove_worktree(task_id, branch)
        self.root.mkdir(parents=True, exist_ok=True)
        await git(self.repo, "worktree", "add", "-B", branch, str(path), self.integration)
        return path, branch

    async def remove_worktree(self, task_id: str, branch: str | None = None) -> None:
        path = self.root / task_id
        if path.exists():
            await git(self.repo, "worktree", "remove", "--force", str(path), check=False)
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        await git(self.repo, "worktree", "prune", check=False)
        if branch:
            await git(self.repo, "branch", "-D", branch, check=False)

    async def commit_all(self, wt: Path, message: str) -> bool:
        """Commit whatever the worker changed. Returns False if nothing changed."""
        await git(wt, "add", "-A")
        rc, _ = await run(["git", "diff", "--cached", "--quiet"], wt, check=False)
        merging = (await run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], wt, check=False))[0] == 0
        if rc == 0 and not merging:  # a merge is committed even if it kept this side unchanged
            return False
        await git(wt, "-c", "user.name=Orchestra", "-c", "user.email=orchestra@local",
                  "commit", "-q", "--no-verify", "-m", message)
        return True

    async def discard_uncommitted(self, wt: Path) -> None:
        """Drop artifacts produced by gate commands (caches, build output, formatter
        rewrites) so they are never committed as the worker's change. Gitignored
        files such as node_modules are kept."""
        await git(wt, "reset", "-q", "--hard", "HEAD")
        await git(wt, "clean", "-fdq")

    async def diff_stats(self, wt: Path, since: str) -> DiffStats:
        # -z gives real paths. A rename is "<a>\t<r>\t\0<old>\0<new>\0", not "old => new".
        out = await git(wt, "diff", "--numstat", "-z", f"{since}...HEAD")
        files, added, removed, per_file = [], 0, 0, {}
        parts = out.split("\0")
        i = 0
        while i < len(parts):
            rec = parts[i]
            i += 1
            if not rec:
                continue
            a, r, f = rec.split("\t", 2)
            if f == "":  # rename or copy: next two fields are old and new paths
                old, f = parts[i], parts[i + 1]
                i += 2
                files.append(old)  # the old path is edited (deleted) too
            files.append(f)
            na, nr = (int(a) if a.isdigit() else 0), (int(r) if r.isdigit() else 0)
            added, removed = added + na, removed + nr
            per_file[f] = na + nr
        return DiffStats(files, added, removed, per_file)

    async def diff_text(self, wt: Path, since: str, limit_chars: int | None = 60_000) -> str:
        """The change as a unified diff; `limit_chars=None` returns it whole (for scanners)."""
        out = await git(wt, "diff", f"{since}...HEAD")
        if limit_chars is None or len(out) <= limit_chars:
            return out
        return out[:limit_chars] + "\n... [diff truncated]"

    async def merge_base(self, wt: Path) -> str:
        return await git(wt, "merge-base", "HEAD", self.integration)

    async def rebase_onto_integration(self, wt: Path) -> tuple[bool, str]:
        rc, out = await run(["git", "-c", "user.name=Orchestra", "-c", "user.email=orchestra@local",
                             "rebase", self.integration], wt, check=False)
        if rc != 0:
            await run(["git", "rebase", "--abort"], wt, check=False)
            return False, out
        return True, out

    async def squash_onto(self, wt: Path, base: str, message: str) -> None:
        """Make the card one commit on top of `base` (also linearizes a resolved merge)."""
        if await git(wt, "rev-list", "--count", f"{base}..HEAD") == "1" and \
                await git(wt, "rev-parse", "HEAD^") == await git(wt, "rev-parse", base):
            return  # already a single commit on base
        await git(wt, "reset", "-q", "--soft", base)
        await git(wt, "-c", "user.name=Orchestra", "-c", "user.email=orchestra@local",
                  "commit", "-q", "--no-verify", "--allow-empty", "-m", message)

    async def start_merge(self, wt: Path, ref: str) -> list[str] | None:
        """Merge `ref` into the worktree without committing. Returns the conflicted files
        ([] when the merge applied cleanly), or None if git failed for another reason."""
        rc, out = await run(["git", "-c", "user.name=Orchestra", "-c", "user.email=orchestra@local",
                             "merge", "--no-ff", "--no-commit", ref], wt, check=False)
        conflicted = (await git(wt, "diff", "--name-only", "--diff-filter=U", check=False)).split()
        if rc != 0 and not conflicted:
            await self.abort_merge(wt)
            return None
        return conflicted

    async def abort_merge(self, wt: Path) -> None:
        await run(["git", "merge", "--abort"], wt, check=False)

    async def commit_merge(self, wt: Path, message: str) -> None:
        await git(wt, "add", "-A")
        await git(wt, "-c", "user.name=Orchestra", "-c", "user.email=orchestra@local",
                  "commit", "-q", "--no-verify", "-m", message)

    async def changed_files(self, wt: Path, since: str, deleted: bool = False) -> list[str]:
        """Files changed since `since` (without deletions), or only the deleted ones."""
        flt = "--diff-filter=D" if deleted else "--diff-filter=d"
        return [f for f in (await git(wt, "diff", "--name-only", flt, f"{since}...HEAD")).splitlines() if f]

    async def revert_paths(self, wt: Path, ref: str, paths: list[str]) -> None:
        """Put `paths` back to their state at `ref` (deleting files that didn't exist there)."""
        for p in paths:
            rc, _ = await run(["git", "cat-file", "-e", f"{ref}:{p}"], wt, check=False)
            if rc == 0:
                await git(wt, "checkout", ref, "--", p)
            elif (wt / p).exists():
                (wt / p).unlink()

    async def detached_worktree(self, name: str, ref: str) -> Path:
        """A read-only-by-convention worktree at `ref` (e.g. for planning against integration)."""
        path = self.root / name
        head = await git(self.repo, "rev-parse", ref)
        if (path / ".git").exists():
            await git(path, "checkout", "-q", "--detach", "--force", head)
            await git(path, "clean", "-fdq", check=False)
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            await git(self.repo, "worktree", "prune", check=False)
            await git(self.repo, "worktree", "add", "-q", "--detach", str(path), head)
        return path

    async def fast_forward_integration(self, wt: Path, expected_old: str) -> None:
        new = await git(wt, "rev-parse", "HEAD")
        # CAS: fails if the integration branch moved since we rebased.
        await git(self.repo, "update-ref", f"refs/heads/{self.integration}", new, expected_old)

    @property
    def merge_lock(self) -> asyncio.Lock:
        return self._merge_lock


async def clone_into(src: Path, dst: Path) -> bool:
    """Copy-on-write copy of a directory (APFS clonefile on macOS, reflink where supported
    on Linux, plain copy otherwise). Returns False if it failed."""
    import platform
    if not src.exists() or dst.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["cp", "-c", "-R", str(src), str(dst)] if platform.system() == "Darwin" \
        else ["cp", "-R", "--reflink=auto", str(src), str(dst)]
    rc, _ = await run(cmd, dst.parent, timeout=600, check=False)
    if rc != 0 and dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    return rc == 0
