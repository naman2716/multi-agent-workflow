"""Tool-call guards, shared by unattended workers and interactive Claude Code sessions.

Two profiles:
  worker       strict: no writes outside the worktree, no git state changes at all,
               no destructive or privileged commands, no secrets in written content.
  interactive  for a human-driven Claude Code session: same write fence (project dir),
               secret check, and destructive-command blocks, but ordinary git
               (commit, branch, checkout) is allowed. Pushing to main/master and
               force-pushes are blocked: work reaches main through a reviewed PR.

  readonly     planner and critic: only the secret-file read block.
  reviewer     reviewer and validators: secret-read block, no file-editing tools, and shell
               commands checked like a worker's (they may run tests, never change the change).

A worker guard can also lock paths (a test-first card's acceptance tests): edits to them are denied.

Workers use it as an Agent SDK PreToolUse hook (make_worker_guard). Interactive sessions
use it as a Claude Code PreToolUse command hook: `orchestra hook interactive`, which reads
the hook JSON on stdin and exits 2 with a reason to block.

This is a second layer. The first, for worker shell commands, is the OS sandbox
(see agents.sandbox_settings); regexes alone can always be bypassed.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from .gates import find_secrets

DESTRUCTIVE = [
    (re.compile(r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\$HOME|\.\.)(\s|/|$)"), "destructive rm outside the project"),
    (re.compile(r"\b(sudo|shutdown|reboot|mkfs)\b|\bdd\s+if="), "privileged/destructive command"),
    (re.compile(r"curl[^|]*\|\s*(ba)?sh"), "piping remote scripts to a shell"),
    (re.compile(r"\b(npm|yarn|pnpm)\s+publish\b"), "publishing packages"),
]
# `git` followed by any global options (-C dir, -c key=val, --git-dir=..., --no-pager)
_GIT = r"\bgit(?:\s+(?:-[Cc]\s+\S+|--[\w-]+(?:=\S+)?))*\s+"
_GIT_STATE = "git state is managed by the orchestrator; just edit files and stop"
WORKER_ONLY = [
    (re.compile(_GIT + r"(push|checkout|switch|reset\s+--hard|rebase|merge|worktree|update-ref|commit|"
                r"tag|remote|filter-branch|clean\s+-[a-z]*f|stash|symbolic-ref|cherry-pick|revert|am|"
                r"gc|prune|replace|notes)\b"), _GIT_STATE),
    (re.compile(_GIT + r"branch\s+(-[a-zA-Z]*[dDfmMcC]\b|--(delete|force|move|copy)\b)"), _GIT_STATE),
    (re.compile(_GIT + r"config(?!\s+(--get|--get-all|--get-regexp|--list|-l)\b)"),
     "git config is shared by every worktree of this repository; don't change it"),
    (re.compile(r"\bcd\s+\.\.(/|\s|$)"), "leaving the worktree"),
]
# Shell redirects and other write targets checked against the worktree (worker profile).
_REDIRECT = re.compile(r"(?:^|[^<>&\d])>>?\s*(['\"]?)([^\s;&|'\"<>()]+)\1")
_TEE = re.compile(r"\btee\s+(?:-a\s+)?(['\"]?)([^\s;&|'\"]+)\1")
_CD = re.compile(r"\bcd\s+(['\"]?)([^\s;&|'\"]+)\1")
_SAFE_ABS = ("/dev/", "/tmp", "/private/tmp", "/private/var/folders", "/var/folders")
_READ_VERBS = re.compile(r"\b(cat|less|more|head|tail|bat|strings|xxd|base64|od|nl|source|grep|rg|"
                         r"awk|sed|python\d?(\.\d+)?|node|ruby|perl)\b")
READ_TOOLS = {"Read", "Grep", "Glob"}
_SECRET_DIRS = ("/.ssh/", "/.aws/", "/.gnupg/", "/.config/gcloud/", "/.docker/")
_SECRET_NAMES = (".netrc", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
_ENV_OK_SUFFIXES = (".example", ".sample", ".template", ".dist", ".defaults")
_WRITE_CMDS = {"rm", "mv", "cp", "truncate", "chmod", "ln", "touch", "dd", "patch", "tee", "install"}
_SEGMENTS = re.compile(r"&&|\|\||[;|&\n]")
LOCKED_MSG = ("{p} is a locked acceptance test written before implementation. Make the code pass "
              "it; don't edit it. If the test itself is wrong, report status \"blocked\" and say why.")


def secret_path(raw: str) -> bool:
    """True for .env files (not .env.example and friends) and credential files/dirs."""
    s = (raw or "").strip().strip("'\"").replace("\\", "/")
    if not s:
        return False
    name = s.rstrip("/").rsplit("/", 1)[-1]
    if (name == ".env" or name.startswith(".env.")) and not name.endswith(_ENV_OK_SUFFIXES):
        return True
    if name in _SECRET_NAMES or name.endswith(".p12"):
        return True
    expanded = "/" + os.path.expanduser(s).lstrip("/") + "/"
    return any(d in expanded for d in _SECRET_DIRS)


def _outside(target: str, root: Path) -> bool:
    """Would writing to / cd-ing into `target` leave the worktree (temp and /dev are fine)?"""
    if target.startswith(("$TMPDIR", "${TMPDIR")):
        return False
    if target.startswith(("~", "$HOME", "${HOME")):
        return True
    if target.startswith("$"):
        return False  # other variables can't be resolved here; the sandbox covers them
    p = Path(target)
    if p.is_absolute() and str(p).startswith(_SAFE_ABS):
        return False
    resolved = Path(os.path.normpath(p if p.is_absolute() else root / p))
    try:
        resolved.relative_to(root)
        return False
    except ValueError:
        return True


def _bash_worker_checks(cmd: str, root: Path) -> str | None:
    for rx in (_REDIRECT, _TEE):
        for m in rx.finditer(cmd):
            if _outside(m.group(2), root):
                return f"Blocked command: writes outside the worktree ({m.group(2)})."
    for m in _CD.finditer(cmd):
        if _outside(m.group(2), root):
            return f"Blocked command: leaving the worktree ({m.group(2)})."
    if _READ_VERBS.search(cmd) and any(secret_path(tok) for tok in re.split(r"[\s;&|<>()=]+", cmd)):
        return "Blocked command: reading secrets (.env or credential files). Use the example file or ask."
    return None
INTERACTIVE_ONLY = [
    (re.compile(r"\bgit\s+push\b[^\n;&|]*(\s(-f|--force|--force-with-lease)\b)"), "force push"),
    (re.compile(r"\bgit\s+push\b[^\n;&|]*\b(main|master)\b"),
     "pushing to main/master: land work through orchestra/integration and a reviewed PR"),
    (re.compile(r"\bgit\s+reset\s+--hard\s+origin/"), "hard reset to remote"),
]
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _rel(raw: str, root: Path) -> str | None:
    p = Path(raw)
    resolved = Path(os.path.normpath(p if p.is_absolute() else root / p))
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return None


def _locked_hit(tool: str, args: dict[str, Any], root: Path, locked: list[str]) -> str | None:
    if not locked:
        return None
    locked_set = {Path(p).as_posix() for p in locked}
    if tool in WRITE_TOOLS:
        rel = _rel(args.get("file_path") or args.get("notebook_path") or "", root)
        if rel in locked_set:
            return LOCKED_MSG.format(p=rel)
    if tool == "Bash":
        cmd = args.get("command", "")
        targets = [m.group(2) for rx in (_REDIRECT, _TEE) for m in rx.finditer(cmd)]
        for seg in _SEGMENTS.split(cmd):  # only the arguments of commands that write files
            words = [w for w in re.split(r"[\s()'\"]+", seg) if w]
            while words and "=" in words[0] and not words[0].startswith("-"):
                words = words[1:]  # leading VAR=value assignments
            if not words:
                continue
            verb = words[0].rsplit("/", 1)[-1]
            if verb in _WRITE_CMDS or (verb in ("sed", "perl") and any(w.startswith("-i") or
                                                                       (w.startswith("-") and "i" in w[1:] and verb == "perl")
                                                                       for w in words[1:])):
                targets += words[1:]
        for tok in targets:
            rel = _rel(tok, root) if tok else None
            if rel in locked_set:
                return LOCKED_MSG.format(p=rel)
    return None


def check(tool: str, args: dict[str, Any], root: Path, profile: str = "worker",
          locked: list[str] | None = None) -> str | None:
    """Return a reason to block the call, or None to allow it."""
    root = root.resolve()
    if (hit := _locked_hit(tool, args, root, locked or [])):
        return hit
    if profile in ("worker", "readonly", "reviewer") and tool in READ_TOOLS:
        for cand in (args.get("file_path"), args.get("path"), args.get("glob"),
                     args.get("pattern") if tool == "Glob" else None):
            if cand and secret_path(str(cand)):
                return (f"Reading secrets is blocked ({cand}). Use .env.example or the project "
                        "context; never copy credentials into code or reports.")
    if profile == "readonly":
        return None
    if profile == "reviewer" and tool in WRITE_TOOLS:
        return "Reviewers don't edit files; report the problem in your verdict instead."
    if tool in WRITE_TOOLS:
        raw = args.get("file_path") or args.get("notebook_path") or ""
        p = Path(raw)
        p = (root / p) if not p.is_absolute() else p
        resolved = Path(os.path.normpath(p))
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            return f"Writes must stay inside {root}. Blocked: {raw}"
        if ".git" in rel.parts:
            return "Do not modify .git internals."
        content = args.get("content") or args.get("new_string") or ""
        for edit in args.get("edits", []) or []:
            content += "\n" + (edit.get("new_string") or "")
        if hits := find_secrets(content):
            return (f"Content looks like it contains secrets ({', '.join(hits)}). "
                    "Use environment variables / config, never literal credentials.")
    if tool == "Bash":
        cmd = args.get("command", "")
        strict = profile in ("worker", "reviewer")
        rules = DESTRUCTIVE + (WORKER_ONLY if strict else INTERACTIVE_ONLY)
        for pat, why in rules:
            if pat.search(cmd):
                return f"Blocked command: {why}."
        if strict:
            return _bash_worker_checks(cmd, root)
    return None


def _make_guard(root: Path, profile: str, locked: list[str] | None = None):
    async def guard(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        reason = check(input_data.get("tool_name", ""), input_data.get("tool_input", {}) or {},
                       root, profile, locked)
        if reason:
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                           "permissionDecision": "deny",
                                           "permissionDecisionReason": reason}}
        return {}
    return guard


def make_worker_guard(worktree: Path, locked: list[str] | None = None):
    """Agent SDK PreToolUse hook for unattended workers (optionally with locked paths)."""
    return _make_guard(worktree, "worker", locked)


def make_reviewer_guard(worktree: Path):
    """PreToolUse hook for the reviewer and validators: may run commands, never edit."""
    return _make_guard(worktree, "reviewer")


def make_read_guard(root: Path):
    """PreToolUse hook for read-only agents (planner, reviewer): blocks reading secrets."""
    return _make_guard(root, "readonly")


def hook_main(profile: str, stdin=None) -> int:
    """Claude Code command-hook entry point. Exit 2 blocks the tool call; stderr is shown to Claude."""
    try:
        data = json.load(stdin or sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # never break the session on malformed input
    tool, args = data.get("tool_name", ""), data.get("tool_input", {}) or {}
    # A write is allowed inside the project or inside the session's own cwd (a subagent
    # running in a Claude Code worktree has that worktree as its cwd).
    roots = [Path(r) for r in (os.environ.get("CLAUDE_PROJECT_DIR"), data.get("cwd")) if r] or [Path.cwd()]
    reasons = [check(tool, args, r, profile) for r in roots]
    reason = None if any(r is None for r in reasons) else reasons[0]
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0
