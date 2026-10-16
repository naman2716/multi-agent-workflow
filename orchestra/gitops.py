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
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


class GitError(RuntimeError):
    pass


class HookRejected(GitError):
    """The repository's pre-commit or commit-msg hook refused a commit; `output` says why."""

    def __init__(self, output: str):
        self.output = output
        super().__init__(f"git hooks rejected the commit:\n{output[-2000:]}")


class IdentityError(RuntimeError):
    """No usable commit identity (Orchestra never falls back to another one)."""


# ---------------------------------------------------------------- commit identity
@dataclass(frozen=True)
class Identity:
    name: str
    email: str

    def __str__(self) -> str:
        return f"{self.name} <{self.email}>"

    def env(self) -> dict[str, str]:
        """Author and committer both: GitHub shows one identity, and stray GIT_* variables in
        the environment can't split them."""
        return {"GIT_AUTHOR_NAME": self.name, "GIT_AUTHOR_EMAIL": self.email,
                "GIT_COMMITTER_NAME": self.name, "GIT_COMMITTER_EMAIL": self.email}


ORCHESTRA_IDENTITY = Identity("Orchestra", "orchestra@local")
_IDENTITY_RE = re.compile(r"^\s*([^<>\n]*?\S)\s*<([^<>\s@]+@[^<>\s]+)>\s*$")
HOOK_TIMEOUT_S = 600  # commits that run the project's hooks (pre-commit can be slow)


def parse_identity(value: str) -> Identity | None:
    """'Name <email>' -> Identity, else None."""
    m = _IDENTITY_RE.match(value or "")
    return Identity(m.group(1), m.group(2)) if m else None


def resolve_identity(repo: Path, author: str) -> Identity:
    """[git] author -> the identity Orchestra commits as. "user" reads git config in `repo`
    (local, global, system and includeIf all apply); a missing value is an error, not a fallback."""
    if author == "orchestra":
        return ORCHESTRA_IDENTITY
    if author != "user":
        ident = parse_identity(author)
        if ident is None:
            raise IdentityError(f'[git] author must be "user", "orchestra" or "Name <email>", not {author!r}')
        return ident

    def get(key: str) -> str:
        r = subprocess.run(["git", "config", "--get", key], cwd=repo, capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""
    name, email = get("user.name"), get("user.email")
    missing = [k for k, v in (("user.name", name), ("user.email", email)) if not v]
    if missing:
        sets = "\n".join(f'  git config --global {k} "{"Your Name" if k == "user.name" else "you@example.com"}"'
                         for k in missing)
        raise IdentityError(
            f"Orchestra commits as you ([git] author = \"user\"), but git config {' and '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} not set in {repo}. Set {'it' if len(missing) == 1 else 'them'} "
            f"(use the email on your GitHub account so commits are linked to you):\n{sets}\n"
            'or set [git] author = "Name <email>" (or "orchestra") in orchestra.toml.')
    return Identity(name, email)


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


def parse_numstat(out: str) -> "DiffStats":
    """`git diff --numstat -z` output. -z gives real paths: a rename is
    "<a>\t<r>\t\0<old>\0<new>\0", not "old => new"."""
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


async def working_diff_stats(wt: Path, base: str) -> "DiffStats":
    """What the change would be if it were committed now: tracked edits and new files against
    `base`. Stages into a throwaway index, so the worktree's own index is never touched."""
    rc, index = await run(["git", "rev-parse", "--git-path", "index"], wt, check=False)
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d) / "index"
        src = Path(index.strip()) if Path(index.strip()).is_absolute() else wt / index.strip()
        if rc == 0 and src.exists():
            shutil.copyfile(src, tmp)
        env = {"GIT_INDEX_FILE": str(tmp)}
        await run(["git", "add", "-A"], wt, check=True, env=env)
        _, out = await run(["git", "diff", "--cached", "--numstat", "-z", base], wt, check=True, env=env)
    return parse_numstat(out)


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


class SyncError(RuntimeError):
    """The integration branch and the base branch have diverged; a human decides what to do."""


@dataclass
class SyncResult:
    """How the integration branch stood against the base branch, and what sync did about it.
    action: created | up_to_date | ahead | fast_forwarded | behind | diverged | no_base"""
    action: str
    old: str = ""
    new: str = ""
    only_integration: int = 0
    only_base: int = 0
    contained: bool = False  # diverged, but base already has everything integration has
    note: str = ""

    def message(self, base: str, integration: str) -> str:
        short = lambda s: s[:7]  # noqa: E731
        if self.action == "created":
            return f"created {integration} from {base}"
        if self.action == "fast_forwarded":
            return f"{integration} fast-forwarded {short(self.old)} → {short(self.new)} ({base} moved)"
        if self.action == "behind":
            return (f"{integration} is behind {base} ({self.only_base} commit(s)); "
                    + (self.note or "it will be fast-forwarded when nothing else is using it"))
        if self.action == "diverged":
            msg = (f"{integration} and {base} have diverged ({self.only_integration} commit(s) only on "
                   f"{integration}, {self.only_base} only on {base}). ")
            if self.contained:
                msg += (f"{base} already contains every change on {integration}, so its PR was probably "
                        f"squash-merged: with no cards open, reset it with "
                        f"`git branch -f {integration} {base}`. ")
            else:
                msg += f"Merge {base} into {integration} (or reset it if its PR was squash-merged). "
            return msg + "Then retry, or pass --allow-diverged to work on it as it is."
        if self.action == "no_base":
            return f"base branch {base} not found; {integration} left as it is"
        return ""


class Git:
    def __init__(self, repo: Path, worktree_root: Path, base: str, integration: str, settings=None):
        """`settings` is the [git] section (author, co_author, run_hooks); defaults when None."""
        from .config import GitSettings
        self.repo, self.root, self.base, self.integration = repo, worktree_root, base, integration
        self.settings = settings or GitSettings()
        self._identity: Identity | None = None
        self._merge_lock = asyncio.Lock()

    @property
    def identity(self) -> Identity:
        """Resolved on first use; raises IdentityError rather than committing as anyone else."""
        if self._identity is None:
            self._identity = resolve_identity(self.repo, self.settings.author)
        return self._identity

    async def _commit(self, wt: Path, message: str, *extra: str, hooks: bool = False,
                      env: dict | None = None) -> None:
        """Every Orchestra commit: as the configured identity, with the co-author trailer.
        `hooks` (commits of new agent work) runs the project's pre-commit/commit-msg hooks when
        [git] run_hooks is on and raises HookRejected if they refuse; all other commits re-record
        content that already passed and skip them (--no-verify)."""
        ident = self.identity
        args = ["git", "commit", "-q", *extra, "-m", message]
        co = self.settings.co_author
        if co and parse_identity(co) != ident:
            args += ["--trailer", f"Co-authored-by: {co}"]
        verify = hooks and self.settings.run_hooks
        if not verify:
            args.insert(2, "--no-verify")
        rc, out = await run(args, wt, timeout=HOOK_TIMEOUT_S if verify else 300, check=not verify,
                            env={**(env or {}), **ident.env()})
        if rc != 0:
            raise HookRejected(out)

    async def ensure_integration_branch(self) -> None:
        rc, _ = await run(["git", "rev-parse", "--verify", f"refs/heads/{self.integration}"],
                          self.repo, check=False)
        if rc != 0:
            await git(self.repo, "branch", self.integration, self.base)

    async def sync_integration(self, allow_diverged: bool = False, move: bool = True,
                               create: bool = True) -> SyncResult:
        """Bring the integration branch up to the base branch (local refs only, no fetch).
        Missing: created from base (with `create`). Behind: fast-forwarded with a compare-and-swap
        (with `move`, and only if no checkout has it). Ahead or equal: left alone. Diverged:
        SyncError unless `allow_diverged`."""
        integ_ref, base_ref = f"refs/heads/{self.integration}", f"refs/heads/{self.base}"
        rc, _ = await run(["git", "rev-parse", "--verify", "-q", integ_ref], self.repo, check=False)
        if rc != 0:
            if not create:
                return SyncResult("up_to_date")
            await git(self.repo, "branch", self.integration, self.base)
            return SyncResult("created")
        rc, base_sha = await run(["git", "rev-parse", "--verify", "-q", base_ref], self.repo, check=False)
        if rc != 0:
            return SyncResult("no_base")
        base_sha = base_sha.strip()
        integ_sha = await git(self.repo, "rev-parse", integ_ref)
        if base_sha == integ_sha:
            return SyncResult("up_to_date", integ_sha, integ_sha)

        async def ancestor(a: str, b: str) -> bool:
            return (await run(["git", "merge-base", "--is-ancestor", a, b], self.repo, check=False))[0] == 0
        if await ancestor(base_sha, integ_sha):
            return SyncResult("ahead", integ_sha, integ_sha)
        counts = (await git(self.repo, "rev-list", "--left-right", "--count",
                            f"{integ_sha}...{base_sha}")).split()
        only_i, only_b = int(counts[0]), int(counts[1])
        if await ancestor(integ_sha, base_sha):
            holder = await self._checked_out_at(integ_ref)
            if not move or holder:
                return SyncResult("behind", integ_sha, integ_sha, only_base=only_b,
                                  note=f"it is checked out in {holder}; fast-forward it there" if holder else "")
            await git(self.repo, "update-ref", integ_ref, base_sha, integ_sha)  # CAS
            return SyncResult("fast_forwarded", integ_sha, base_sha, only_base=only_b)
        # diverged: does base already hold integration's content (a squash-merged PR)?
        rc, tree = await run(["git", "merge-tree", "--write-tree", base_sha, integ_sha], self.repo, check=False)
        base_tree = await git(self.repo, "rev-parse", f"{base_sha}^{{tree}}")
        res = SyncResult("diverged", integ_sha, integ_sha, only_i, only_b,
                         contained=rc == 0 and tree.split()[:1] == [base_tree])
        if not allow_diverged:
            raise SyncError(res.message(self.base, self.integration))
        return res

    async def _checked_out_at(self, ref: str) -> str:
        """The worktree that has `ref` checked out, or ''."""
        out = await git(self.repo, "worktree", "list", "--porcelain")
        path = ""
        for line in out.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):]
            elif line == f"branch {ref}":
                return path
        return ""

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

    async def commit_all(self, wt: Path, message: str, hooks: bool = False, env: dict | None = None) -> bool:
        """Commit whatever the worker changed. Returns False if nothing changed. With `hooks`
        (new agent work) the project's hooks may run and raise HookRejected; the worktree is
        left as the hooks left it (formatters' fixes included)."""
        await git(wt, "add", "-A")
        rc, _ = await run(["git", "diff", "--cached", "--quiet"], wt, check=False)
        merging = (await run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], wt, check=False))[0] == 0
        if rc == 0 and not merging:  # a merge is committed even if it kept this side unchanged
            return False
        await self._commit(wt, message, hooks=hooks, env=env)
        return True

    async def discard_uncommitted(self, wt: Path) -> None:
        """Drop artifacts produced by gate commands (caches, build output, formatter
        rewrites) so they are never committed as the worker's change. Gitignored
        files such as node_modules are kept."""
        await git(wt, "reset", "-q", "--hard", "HEAD")
        await git(wt, "clean", "-fdq")

    async def diff_stats(self, wt: Path, since: str) -> DiffStats:
        return parse_numstat(await git(wt, "diff", "--numstat", "-z", f"{since}...HEAD"))

    async def diff_text(self, wt: Path, since: str, limit_chars: int | None = 60_000) -> str:
        """The change as a unified diff; `limit_chars=None` returns it whole (for scanners)."""
        out = await git(wt, "diff", f"{since}...HEAD")
        if limit_chars is None or len(out) <= limit_chars:
            return out
        return out[:limit_chars] + "\n... [diff truncated]"

    async def merge_base(self, wt: Path) -> str:
        return await git(wt, "merge-base", "HEAD", self.integration)

    async def rebase_onto_integration(self, wt: Path) -> tuple[bool, str]:
        rc, out = await run(["git", "rebase", self.integration], wt, check=False, env=self.identity.env())
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
        await self._commit(wt, message, "--allow-empty")

    async def start_merge(self, wt: Path, ref: str) -> list[str] | None:
        """Merge `ref` into the worktree without committing. Returns the conflicted files
        ([] when the merge applied cleanly), or None if git failed for another reason."""
        rc, out = await run(["git", "merge", "--no-ff", "--no-commit", ref], wt, check=False,
                            env=self.identity.env())
        conflicted = (await git(wt, "diff", "--name-only", "--diff-filter=U", check=False)).split()
        if rc != 0 and not conflicted:
            await self.abort_merge(wt)
            return None
        return conflicted

    async def abort_merge(self, wt: Path) -> None:
        await run(["git", "merge", "--abort"], wt, check=False)

    async def commit_merge(self, wt: Path, message: str) -> None:
        await git(wt, "add", "-A")
        await self._commit(wt, message)

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
