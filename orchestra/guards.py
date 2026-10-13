"""Tool-call guards, shared by unattended workers and interactive Claude Code sessions.

Two profiles:
  worker       strict: no writes outside the worktree, no git state changes at all,
               no destructive or privileged commands, no secrets in written content.
  interactive  for a human-driven Claude Code session: same write fence (project dir),
               secret check, and destructive-command blocks, but ordinary git
               (commit, branch, checkout) is allowed. Pushing to main/master and
               force-pushes are blocked: work reaches main through a reviewed PR.

Workers use it as an Agent SDK PreToolUse hook (make_worker_guard). Interactive sessions
use it as a Claude Code PreToolUse command hook: `orchestra hook interactive`, which reads
the hook JSON on stdin and exits 2 with a reason to block.
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
WORKER_ONLY = [
    (re.compile(r"\bgit\s+(push|checkout|switch|reset\s+--hard|rebase|merge|worktree|branch\s+-[dD]|"
                r"update-ref|commit|tag|remote|filter-branch|clean\s+-[a-z]*f)"),
     "git state is managed by the orchestrator; just edit files and stop"),
    (re.compile(r"\bcd\s+\.\.(/|\s|$)"), "leaving the worktree"),
]
INTERACTIVE_ONLY = [
    (re.compile(r"\bgit\s+push\b[^\n;&|]*(\s(-f|--force|--force-with-lease)\b)"), "force push"),
    (re.compile(r"\bgit\s+push\b[^\n;&|]*\b(main|master)\b"),
     "pushing to main/master: land work through orchestra/integration and a reviewed PR"),
    (re.compile(r"\bgit\s+reset\s+--hard\s+origin/"), "hard reset to remote"),
]
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def check(tool: str, args: dict[str, Any], root: Path, profile: str = "worker") -> str | None:
    """Return a reason to block the call, or None to allow it."""
    root = root.resolve()
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
        rules = DESTRUCTIVE + (WORKER_ONLY if profile == "worker" else INTERACTIVE_ONLY)
        for pat, why in rules:
            if pat.search(cmd):
                return f"Blocked command: {why}."
    return None


def make_worker_guard(worktree: Path):
    """Agent SDK PreToolUse hook for unattended workers."""
    async def guard(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        reason = check(input_data.get("tool_name", ""), input_data.get("tool_input", {}) or {},
                       worktree, "worker")
        if reason:
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                           "permissionDecision": "deny",
                                           "permissionDecisionReason": reason}}
        return {}
    return guard


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
