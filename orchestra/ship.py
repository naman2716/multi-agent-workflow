"""Ship the integration branch: push it, open or update one draft PR, and read CI.

Uses `git` and the GitHub CLI (`gh`) already set up on your machine, so it works in any repo
you have access to. Never force-pushes, never pushes the base branch, and opens PRs as drafts.
Without a remote, `gh` or authentication it does nothing and says why; the run still succeeds.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .gitops import run
from .store import Store, Task

RUN_ID_RE = re.compile(r"/actions/runs/(\d+)")
GITHUB_REMOTE_RE = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")
# "Fixes ENG-7" / "closes #12" in agent-written text would close tickets when the PR merges
CLOSING_RE = re.compile(r"\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)(\s*:?\s+)(#\d+|[A-Z][A-Z0-9]*-\d+)\b", re.I)
CARD_SUBJECT_RE = re.compile(r"([^\s:]+): ")
LOG_TAIL = 3000
BODY_LIMIT = 60_000  # GitHub's PR body limit is 65,536 characters
QUIET_ENV = {"GH_NO_UPDATE_NOTIFIER": "1", "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1",
             "GIT_TERMINAL_PROMPT": "0"}


class ShipError(RuntimeError):
    pass


def defuse(text: str) -> str:
    """Keep closing keywords in agent-written text from closing issues or tickets on merge."""
    return CLOSING_RE.sub(lambda m: f"{m.group(1)} → {m.group(3)}", text or "")


@dataclass
class ShipResult:
    status: str                      # "shipped" | "skipped" | "failed"
    note: str = ""
    pr_url: str = ""
    pr_number: int | None = None
    created: bool = False
    ci: str = ""                     # "passed" | "failed" | "pending" | "none" | "not checked"
    checks: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)  # failing checks, each with a "log" tail
    cards: list[str] = field(default_factory=list)


def first_json(text: str):
    """Parse the first JSON value in `text` (gh may print notices around it)."""
    for i, ch in enumerate(text):
        if ch in "[{":
            try:
                return json.JSONDecoder().raw_decode(text[i:])[0]
            except ValueError:
                continue
    raise ValueError("no JSON in output")


class Shipper:
    def __init__(self, cfg: Config, store: Store, check_poll_s: float = 30.0, grace_s: float = 120.0,
                 log=None):
        self.cfg, self.store = cfg, store
        self.log = log or (lambda m: None)
        self.check_poll_s = check_poll_s
        self.grace_s = grace_s  # checks can take a moment to appear after a push

    async def _gh(self, *args: str, timeout: int = 120) -> tuple[int, str]:
        repo = await self._github_repo()
        return await run([self.cfg.ship.gh, *args, *(["--repo", repo] if repo else [])], self.cfg.repo,
                         timeout=timeout, check=False, env=QUIET_ENV)

    async def _github_repo(self) -> str:
        """owner/name of [ship].remote when it is on github.com (so gh never picks another repo)."""
        if not hasattr(self, "_repo_slug"):
            rc, url = await run(["git", "remote", "get-url", self.cfg.ship.remote], self.cfg.repo, check=False)
            m = GITHUB_REMOTE_RE.search(url.strip()) if rc == 0 else None
            self._repo_slug = f"{m.group(1)}/{m.group(2)}" if m else ""
        return self._repo_slug

    async def base_ref(self) -> str:
        """The remote's base branch when it can be fetched (your local one may be behind)."""
        if not hasattr(self, "_base_ref"):
            remote, base = self.cfg.ship.remote, self.cfg.base_branch
            await run(["git", "fetch", "-q", remote, f"refs/heads/{base}:refs/remotes/{remote}/{base}"],
                      self.cfg.repo, timeout=120, check=False, env=QUIET_ENV)
            rc, _ = await run(["git", "rev-parse", "--verify", "-q", f"refs/remotes/{remote}/{base}"],
                              self.cfg.repo, check=False)
            self._base_ref = f"{remote}/{base}" if rc == 0 else base
        return self._base_ref

    async def preflight(self) -> str:
        """Why shipping can't happen here ("" if it can)."""
        rc, _ = await run(["git", "remote", "get-url", self.cfg.ship.remote], self.cfg.repo, check=False)
        if rc != 0:
            return f"no git remote named {self.cfg.ship.remote!r}"
        gh = self.cfg.ship.gh
        if not (shutil.which(gh) or Path(gh).is_file()):
            return f"GitHub CLI not found ({gh}); install it and run `gh auth login`"
        return ""

    async def merged_cards(self) -> list[Task]:
        """Cards on the integration branch that the base branch doesn't have yet, oldest first."""
        rc, out = await run(["git", "log", "--format=%s", f"{await self.base_ref()}..{self.cfg.integration_branch}"],
                            self.cfg.repo, check=False)
        by_id = {t.id: t for t in self.store.all()}
        ids: list[str] = []
        for line in reversed(out.splitlines() if rc == 0 else []):
            m = CARD_SUBJECT_RE.match(line)
            if m and m.group(1) in by_id and by_id[m.group(1)].status == "merged" and m.group(1) not in ids:
                ids.append(m.group(1))
        return [by_id[i] for i in ids]

    async def ahead(self) -> int:
        rc, out = await run(["git", "rev-list", "--count", f"{await self.base_ref()}..{self.cfg.integration_branch}"],
                            self.cfg.repo, check=False)
        return int(out.strip()) if rc == 0 and out.strip().isdigit() else 0

    async def changed_files(self) -> list[str]:
        rc, out = await run(["git", "diff", "--name-only", f"{await self.base_ref()}...{self.cfg.integration_branch}"],
                            self.cfg.repo, check=False)
        return [f for f in out.splitlines() if f.strip()] if rc == 0 else []

    async def push(self) -> tuple[bool, str]:
        ref = f"refs/heads/{self.cfg.integration_branch}"
        rc, out = await run(["git", "push", self.cfg.ship.remote, f"{ref}:{ref}"], self.cfg.repo,
                            timeout=300, check=False, env=QUIET_ENV)
        return rc == 0, out

    def title(self, cards: list[Task]) -> str:
        """The goal of the latest plan with cards in this PR, else a count."""
        plans = self.store.get_meta("plans", []) or []
        ids = {t.id for t in cards}
        goal = next((p.get("goal") for p in reversed(plans) if (p.get("goal") or "").strip()
                     and (not p.get("cards") or ids & set(p["cards"]))), "")
        line = goal.strip().splitlines()[0].strip() if goal.strip() else ""
        if line:
            line = defuse(line)
            return line if len(line) <= 72 else line[:71] + "…"
        return f"Orchestra: {len(cards)} card{'s' if len(cards) != 1 else ''}"

    def body(self, cards: list[Task]) -> str:
        integ = self.cfg.integration_branch
        total = sum(t.cost_usd for t in cards)
        lines = [f"Opened by Orchestra: {len(cards)} card{'s' if len(cards) != 1 else ''} merged into "
                 f"`{integ}` (${total:.2f} in agent cost). Each card passed its automated checks and an "
                 "independent review before it was merged.", ""]
        for t in cards:
            lines.append(f"### {t.id}: {defuse(t.title)}")
            if t.summary:
                s = defuse(t.summary.strip())
                lines.append(s if len(s) <= 1500 else s[:1500] + " …")
            meta = [f"{t.attempts} attempt{'s' if t.attempts != 1 else ''}", f"${t.cost_usd:.2f}"]
            if t.source:
                meta.append(f"from {t.source}")
            lines += ["", "_" + " · ".join(meta) + "_"]
            if t.locked_tests:
                lines.append("- Test-first: acceptance tests were written before the code and locked: "
                             + ", ".join(f"`{p}`" for p in t.locked_tests))
            for d in t.decisions:
                lines.append(f"- Decision: {defuse(d.get('question', ''))} → **{defuse(d.get('answer', ''))}**")
            lines.append("")
        tickets = sorted({t.source.split(":", 1)[1] for t in cards if (t.source or "").startswith("linear:")})
        if tickets:
            lines += ["---"] + [f"{self.cfg.ship.linear_magic_word} {tk}" for tk in tickets]
        text = "\n".join(lines).strip() + "\n"
        return text if len(text) <= BODY_LIMIT else text[:BODY_LIMIT] + "\n… (truncated)\n"

    async def upsert_pr(self, title: str, body: str) -> tuple[int | None, str, bool]:
        """Edit the open PR from the integration branch, or create a draft one."""
        cfg = self.cfg
        path = cfg.state_path / "pr_body.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        rc, out = await self._gh("pr", "list", "--head", cfg.integration_branch, "--base", cfg.base_branch,
                                 "--state", "open", "--json", "number,url,isCrossRepository")
        if rc != 0:
            raise ShipError(f"`gh pr list` failed (is gh logged in? run `gh auth login`): {out.strip()[-500:]}")
        try:
            prs = [p for p in first_json(out) if not p.get("isCrossRepository")]  # never a fork's PR
        except (ValueError, TypeError, AttributeError):
            prs = []
        if prs:
            n, url = prs[0]["number"], prs[0]["url"]
            rc, out = await self._gh("pr", "edit", str(n), "--title", title, "--body-file", str(path))
            if rc != 0:
                raise ShipError(f"`gh pr edit` failed: {out.strip()[-500:]}")
            return n, url, False
        args = ["pr", "create", "--base", cfg.base_branch, "--head", cfg.integration_branch,
                "--title", title, "--body-file", str(path)] + (["--draft"] if cfg.ship.draft else [])
        rc, out = await self._gh(*args)
        if rc != 0:
            raise ShipError(f"`gh pr create` failed: {out.strip()[-500:]}")
        url = next((w for w in reversed(out.split()) if w.startswith("http")), "")
        m = re.search(r"/pull/(\d+)", url)
        return (int(m.group(1)) if m else None), url, True

    def _parse_checks(self, rc: int, out: str) -> list[dict] | None:
        try:
            data = first_json(out)
            return data if isinstance(data, list) else None
        except ValueError:
            return [] if "no checks reported" in out.lower() else None

    async def _pr_head(self, pr: int) -> str:
        rc, out = await self._gh("pr", "view", str(pr), "--json", "headRefOid")
        try:
            return first_json(out).get("headRefOid", "") if rc == 0 else ""
        except (ValueError, AttributeError):
            return ""

    async def checks(self, pr: int, wait_s: float, sha: str = "") -> list[dict]:
        """Poll the PR's checks for the pushed commit `sha` until none are pending (and the set
        stays the same over two polls, as workflows can appear late) or `wait_s` passes."""
        start = time.monotonic()
        if sha:  # GitHub can take a moment to move the PR to the new commit
            while await self._pr_head(pr) != sha:
                if time.monotonic() - start >= min(self.grace_s, wait_s):
                    raise ShipError(f"the PR does not show the pushed commit {sha[:8]} yet")
                await asyncio.sleep(self.check_poll_s)
        previous = None
        while True:
            rc, out = await self._gh("pr", "checks", str(pr), "--json", "name,bucket,link,workflow")
            checks = self._parse_checks(rc, out)
            if checks is None:
                raise ShipError(f"`gh pr checks` failed: {out.strip()[-500:]}")
            elapsed = time.monotonic() - start
            pending = [c for c in checks if c.get("bucket") == "pending"]
            state = sorted((c.get("name", ""), c.get("bucket", "")) for c in checks)
            if checks and not pending and state == previous:
                return checks
            if not checks and elapsed >= min(self.grace_s, wait_s):
                return []
            if elapsed >= wait_s:
                return checks
            previous = state if checks and not pending else None
            await asyncio.sleep(self.check_poll_s)

    async def attach_logs(self, failed: list[dict]) -> None:
        """Add the tail of each failing GitHub Actions log (`gh run view --log-failed`)."""
        logs: dict[str, str] = {}
        for c in failed:
            m = RUN_ID_RE.search(c.get("link") or "")
            if not m:
                c["log"] = f"(no log available here; see {c.get('link') or 'the PR checks'})"
                continue
            rid = c["run_id"] = m.group(1)
            if rid not in logs:
                rc, out = await self._gh("run", "view", rid, "--log-failed", timeout=180)
                logs[rid] = out[-LOG_TAIL:] if rc == 0 else f"(could not read the log: {out.strip()[-300:]})"
            c["log"] = logs[rid]

    async def ship(self, wait_s: float | None = None) -> ShipResult:
        """Push, open/update the PR, and (with `wait_s`) wait for CI."""
        why = await self.preflight()
        if why:
            return ShipResult("skipped", why)
        if not await self.ahead():
            return ShipResult("skipped", f"{self.cfg.integration_branch} has nothing that "
                                         f"{self.cfg.base_branch} doesn't")
        cards = await self.merged_cards()
        sha = (await run(["git", "rev-parse", self.cfg.integration_branch], self.cfg.repo, check=False))[1].strip()
        ok, out = await self.push()
        if not ok:
            return ShipResult("failed", "git push was rejected (Orchestra never force-pushes): "
                                        + out.strip()[-500:])
        try:
            n, url, created = await self.upsert_pr(self.title(cards), self.body(cards))
        except ShipError as e:
            return ShipResult("failed", str(e))
        res = ShipResult("shipped", pr_url=url, pr_number=n, created=created, cards=[t.id for t in cards])
        if wait_s is None or n is None:
            res.ci = "not checked"
        else:
            self.log(f"draft PR {'opened' if created else 'updated'}: {url}; waiting up to "
                     f"{wait_s / 60:g} min for CI")
            try:
                res.checks = await self.checks(n, wait_s, sha)
            except ShipError as e:
                res.ci, res.note = "not checked", str(e)
            else:
                buckets = [c.get("bucket") for c in res.checks]
                res.failed = [c for c in res.checks if c.get("bucket") == "fail"]
                res.ci = ("failed" if res.failed else "pending" if "pending" in buckets
                          else "cancelled" if "cancel" in buckets
                          else "passed" if "pass" in buckets else "none")
                if res.failed:
                    await self.attach_logs(res.failed)
        self.store.set_meta("last_pr", {"url": url, "number": n, "ci": res.ci, "at": time.time(),
                                        "failed": [c.get("name") for c in res.failed]})
        return res
